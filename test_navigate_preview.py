"""Offline navigation receipt and exact-page contracts. Never opens a real URL."""
import io
import importlib.util
import json
import sys
import unittest
from unittest.mock import MagicMock, patch

# Navigation contracts mock CDP entirely. The integration probe on Ubuntu uses
# the real websocket-client; Windows offline tests need not install it.
if importlib.util.find_spec("websocket") is None:
    with patch.dict(sys.modules, {"websocket": MagicMock()}):
        import chrome_mcp_bridge as bridge
else:
    import chrome_mcp_bridge as bridge
import chrome_vision as vision
from test_chrome_vision import FakeCDP, PAGE, inventory


class NavigationTests(unittest.TestCase):
    def setUp(self):
        self.handler = object.__new__(bridge.MCPHandler)
        self.cdp = MagicMock()
        self.cdp.evaluate.return_value = {"result": {"value": "Fixture title"}}
        self.open = patch.object(bridge.urllib.request, "urlopen", side_effect=self.response).start()
        self.factory = patch.object(bridge, "CDPClient", return_value=self.cdp).start()
        self.sleep = patch.object(bridge.time, "sleep").start()
        self.preview = patch.object(bridge.vision, "preview_opened_page", return_value={"content": [{
            "type": "text", "text": json.dumps({"state": "partial", "page_id": "new-page", "images": [
                {"image_id": "one", "index": 1}, {"image_id": "two", "index": 2}],
                "note_content": {"title": "Note title", "body": "Untrusted fixture"}})}]}).start()
        self.addCleanup(patch.stopall)

    def response(self, request, **kwargs):
        self.assertEqual(request.method, "PUT")
        self.assertTrue(request.full_url.startswith("http://127.0.0.1:9223/json/new?"))
        return io.BytesIO(json.dumps({"id": "new-page", "title": "", "webSocketDebuggerUrl": "ws://new-page"}).encode())

    def info(self, result):
        return json.loads(result["content"][0]["text"])

    def test_note_returns_ids_and_body_in_one_navigation(self):
        result = self.info(self.handler._execute_tool("chrome_navigate", {"url": "https://xhslink.cn/o/fixture"}))
        self.assertEqual(result["navigation_status"], "opened")
        self.assertEqual(result["page_id"], "new-page")
        self.assertEqual(result["title"], "Note title")
        self.assertEqual([x["image_id"] for x in result["note_preview"]["images"]], ["one", "two"])
        self.preview.assert_called_once_with("new-page", 12000)
        self.open.assert_called_once()
        self.factory.assert_not_called()
        self.sleep.assert_not_called()

    def test_custom_wait_and_opt_out(self):
        self.handler._execute_tool("chrome_navigate", {"url": "https://www.xiaohongshu.com/explore/fixture", "wait_ms": 15000})
        self.preview.assert_called_once_with("new-page", 15000)
        self.preview.reset_mock()
        self.handler._execute_tool("chrome_navigate", {"url": "https://xhslink.cn/o/fixture", "include_note": False})
        self.preview.assert_not_called()

    def test_preview_failure_preserves_acknowledged_page_no_retry(self):
        self.preview.side_effect = TimeoutError("fixture")
        result = self.handler._chrome_navigate("https://xhslink.cn/o/fixture")
        self.assertNotIn("isError", result)
        data = self.info(result)
        self.assertEqual(data["navigation_status"], "opened")
        self.assertEqual(data["preview_status"], "unavailable")
        self.assertEqual(data["page_id"], "new-page")
        self.open.assert_called_once()

    def test_blocked_preview_is_explicit_and_never_reopened(self):
        self.preview.return_value = {"isError": True, "content": [{"type": "text", "text": '{"error":"page_blocked"}'}]}
        result = self.info(self.handler._chrome_navigate("https://xhslink.cn/o/fixture"))
        self.assertEqual(result["preview_status"], "error")
        self.assertEqual(result["note_preview"]["error"], "page_blocked")
        self.open.assert_called_once()

    def test_unconfirmed_open_does_not_retry_or_preview(self):
        self.open.side_effect = TimeoutError("fixture")
        result = self.handler._chrome_navigate("https://xhslink.cn/o/fixture")
        self.assertTrue(result["isError"])
        self.assertEqual(self.info(result)["navigation_status"], "unconfirmed")
        self.open.assert_called_once()
        self.preview.assert_not_called()

    def test_invalid_arguments_rejected_before_open(self):
        for args in ({"url": "javascript:alert(1)"}, {"url": "file:///private"}, {"url": 123},
                     {"url": "https://user:secret@example.test"}, {"url": "https://example.test", "wait_ms": True},
                     {"url": "https://example.test", "wait_ms": -1}, {"url": "https://example.test", "wait_ms": 15001},
                     {"url": "https://example.test", "include_note": "yes"}, {"url": "https://example.test", "extra": 1}):
            with self.subTest(args=args):
                self.assertTrue(self.handler._execute_tool("chrome_navigate", args)["isError"])
        self.open.assert_not_called()

    def test_generic_site_and_lookalike_not_note_preview(self):
        for url in ("https://example.test", "https://xiaohongshu.com.attacker.test", "https://evilxhslink.cn"):
            result = self.info(self.handler._chrome_navigate(url))
            self.assertEqual(result["preview_status"], "not_requested")
        self.preview.assert_not_called()

    def test_title_failure_does_not_lose_page_or_retry(self):
        self.cdp.evaluate.side_effect = TimeoutError("fixture")
        result = self.info(self.handler._chrome_navigate("https://example.test"))
        self.assertEqual(result["page_id"], "new-page")
        self.open.assert_called_once()

    def test_get_content_after_url_uses_acknowledged_exact_page(self):
        with patch.object(self.handler, "_chrome_get_web_content", return_value="ok") as reader:
            data = self.info(self.handler._execute_tool("chrome_get_web_content", {"url": "https://example.test"}))
            self.assertEqual(data["content_result"], "ok")
            self.assertEqual(data["navigation_status"], "opened")
            self.assertEqual(data["page_id"], "new-page")
            reader.assert_called_once_with("new-page")
        self.preview.assert_not_called()

    def test_get_content_failure_after_url_preserves_page_receipt(self):
        with patch.object(self.handler, "_chrome_get_web_content", return_value="错误: read failed"):
            result = self.info(self.handler._execute_tool("chrome_get_web_content", {"url": "https://example.test"}))
        self.assertEqual(result["page_id"], "new-page")
        self.assertEqual(result["navigation_status"], "opened")
        self.assertIn("read failed", result["content_result"])
        self.open.assert_called_once()

    def test_get_content_exact_page_not_first_target(self):
        with patch.object(bridge.vision, "_page", return_value={"webSocketDebuggerUrl": "ws://chosen", "url": "https://example.test"}) as page:
            with patch.object(bridge, "get_page_metadata", return_value={}), patch.object(bridge, "get_page_content", return_value="fixture"):
                result = self.handler._execute_tool("chrome_get_web_content", {"page_id": "chosen"})
        page.assert_called_once_with("chosen")
        self.factory.assert_called_once_with("ws://chosen")
        self.assertIn("fixture", result)

    def test_content_bad_or_conflicting_selection_rejected(self):
        for args in ({"url": "https://example.test", "page_id": "chosen"}, {"page_id": ""}, {"page_id": 123}):
            self.assertTrue(self.handler._execute_tool("chrome_get_web_content", args)["isError"])
        self.open.assert_not_called()


class PreviewWaitTests(unittest.TestCase):
    def test_new_blank_document_waits_for_note_then_stable(self):
        source = FakeCDP()
        blank = inventory(count=0)
        blank["document_id"] = "123:about:blank"
        ready = inventory("note", "ready", 3)
        ready["note_content"] = {"title": "Fixture title", "body": "Fixture body"}
        ready["note_media"] = "images"
        source.inventory_sequence = [blank, ready, ready, ready]
        service = vision.ChromeVision(lambda ws: source, lambda: PAGE)
        with patch.object(service, "_targets", return_value=[PAGE]), patch.object(vision.time, "sleep") as sleep:
            response = service.preview_opened_page(PAGE["id"], 12000)
        self.assertNotIn("isError", response)
        data = json.loads(response["content"][0]["text"])
        self.assertEqual(data["total"], 3)
        self.assertEqual(data["state"], "ready")
        self.assertEqual(data["note_content"]["body"], "Fixture body")
        self.assertEqual(data["wait_budget_ms"], 12000)
        self.assertEqual(sleep.call_count, 3)

    def test_redirect_to_unrelated_page_does_not_inventory_other_images(self):
        source = FakeCDP()
        service = vision.ChromeVision(lambda ws: source, lambda: PAGE)
        with patch.object(service, "_targets", return_value=[PAGE]), patch.object(vision.time, "sleep") as sleep:
            response = service.preview_opened_page(PAGE["id"], 12000)
        data = json.loads(response["content"][0]["text"])
        self.assertEqual(data["state"], "unsupported")
        self.assertEqual(data["images"], [])
        sleep.assert_not_called()

    def test_zero_wait_is_one_observation(self):
        source = FakeCDP()
        source.inventory = inventory("note", "loading", 0)
        service = vision.ChromeVision(lambda ws: source, lambda: PAGE)
        with patch.object(vision.time, "sleep") as sleep:
            data = service._inventory(source, wait=True, wait_ms=0)
        self.assertEqual(data["local_poll_count"], 1)
        self.assertEqual(data["wait_budget_ms"], 0)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
