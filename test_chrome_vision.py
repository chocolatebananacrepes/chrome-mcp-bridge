"""Offline 2.2 contracts. No real browser, user pages, source requests or sleep."""
import base64
import copy
import json
import shutil
import struct
import subprocess
import unittest
from unittest.mock import patch
import chrome_vision as vision


def jpeg(w=640, h=480):
    return b"\xff\xd8\xff\xc0" + struct.pack(">HBHHB", 8, 8, h, w, 0) + b"\xff\xd9"


def encoded(w=640, h=480):
    return base64.b64encode(jpeg(w, h)).decode()


PAGE = {"id": "source", "type": "page", "webSocketDebuggerUrl": "ws://source"}
OTHER = {"id": "other", "type": "page", "webSocketDebuggerUrl": "ws://other"}


def inventory(scope="page", state="ready", count=4):
    return {"document_id": "123:https://site.test/article?session=private-document-value",
            "scope": scope, "state": state, "expected_total": count,
            "images": [{"url": f"https://cdn.test/{n}.jpg?signature=private-signature-{n}",
                        "kind": "note-image" if scope == "note" else "img",
                        "slot": f"note:{n - 1}" if scope == "note" else f"page:{n}",
                        "alt": f"Fixture {n}", "width": 900, "height": 600, "loaded": True}
                       for n in range(1, count + 1)]}


class FakeCDP:
    def __init__(self):
        self.inventory = inventory()
        self.inventory_sequence = []
        self.extract_sequence = []
        self.extracted = {"data": encoded(), "source_width": 900, "source_height": 600}
        self.shot = encoded()
        self.calls = []
        self.closed = False
        self.connect_exception = self.extract_exception = self.inventory_exception = self.capture_exception = None
        self.metrics = {"cssVisualViewport": {"clientWidth": 2000, "clientHeight": 1200, "pageX": 5, "pageY": 1300}}

    def connect(self):
        if self.connect_exception:
            raise self.connect_exception

    def close(self):
        self.closed = True

    def evaluate(self, script, await_promise=False):
        self.calls.append(("Runtime.evaluate", script))
        if script == vision.INVENTORY_JS:
            if self.inventory_exception:
                raise self.inventory_exception
            if self.inventory_sequence:
                self.inventory = self.inventory_sequence.pop(0)
            value = self.inventory
        else:
            if self.extract_exception:
                raise self.extract_exception
            if self.extract_sequence:
                self.extracted = self.extract_sequence.pop(0)
            value = self.extracted
        return {"result": {"value": copy.deepcopy(value)}}

    def send(self, method, params=None):
        self.calls.append((method, params))
        if method == "Page.getLayoutMetrics":
            return copy.deepcopy(self.metrics)
        if method == "Page.captureScreenshot":
            if self.capture_exception:
                raise self.capture_exception
            return {"data": self.shot}
        raise AssertionError(f"Unexpected CDP operation: {method}")


class ChromeVisionTests(unittest.TestCase):
    def setUp(self):
        self.source = FakeCDP()
        self.factories = []
        def factory(ws):
            self.factories.append(ws)
            return self.source
        self.service = vision.ChromeVision(factory, lambda: PAGE)
        patch.object(self.service, "_targets", return_value=[PAGE, OTHER]).start()
        self.network = patch.object(vision.urllib.request, "urlopen", side_effect=AssertionError("Network forbidden")).start()
        self.sleep = patch.object(vision.time, "sleep").start()
        self.addCleanup(patch.stopall)

    def info(self, result):
        return json.loads(result["content"][0]["text"])

    def listed(self, **kwargs):
        result = self.service.call({"mode": "list", **kwargs})
        self.assertNotIn("isError", result, result)
        self.assertEqual([p["type"] for p in result["content"]], ["text"])
        return self.info(result)

    def image(self, first, index=1, stable=True, **kwargs):
        args = {"mode": "image", "page_id": first["page_id"]}
        if stable:
            args["image_id"] = first["images"][index - 1]["image_id"]
        else:
            args.update(snapshot_id=first["snapshot_id"], index=index)
        args.update(kwargs)
        return self.service.call(args)

    def assert_error(self, result, code):
        self.assertTrue(result.get("isError"), result)
        self.assertEqual([p["type"] for p in result["content"]], ["text"])
        info = self.info(result)
        self.assertEqual(info["error"], code, result)
        self.assertFalse(info["automatic_refetch"])
        self.assertNotIn("private-signature", json.dumps(result))
        self.assertNotIn("private-document", json.dumps(result))

    def scripts(self):
        return [s for m, s in self.source.calls if m == "Runtime.evaluate" and s != vision.INVENTORY_JS]

    def test_schema_read_only_rejects_external_url(self):
        # Default remains passive, but explicit native_lazy can change img.loading.
        self.assertFalse(vision.TOOL_SCHEMA["annotations"]["readOnlyHint"])
        self.assertEqual(vision.TOOL_SCHEMA['inputSchema']['properties']['loading_policy']['default'],'passive')
        self.assertFalse(vision.TOOL_SCHEMA["annotations"]["destructiveHint"])
        self.assertFalse(vision.TOOL_SCHEMA["inputSchema"]["additionalProperties"])
        self.assertNotIn("url", vision.TOOL_SCHEMA["inputSchema"]["properties"])
        self.assert_error(self.service.call({"url": "https://outside.test/image.jpg"}), "invalid_request")
        self.assertFalse(self.factories)

    def test_stable_id_no_snapshot_returns_native_mcp_image_without_signed_url_leak(self):
        first = self.listed()
        result = self.image(first, 2)
        self.assertNotIn("isError", result, result)
        self.assertEqual([p["type"] for p in result["content"]], ["text", "image"])
        self.assertEqual(result["content"][1], {"type": "image", "data": encoded(), "mimeType": "image/jpeg"})
        self.assertEqual(self.info(result)["index"], 2)
        self.assertEqual(self.info(result)["source_refetches"], 0)
        self.assertNotIn(encoded(), result["content"][0]["text"])
        self.assertNotIn("private-signature", json.dumps(first) + json.dumps(result))
        self.assertNotIn("document_id", first)
        self.network.assert_not_called()

    def test_stable_and_legacy_selection_survive_unrelated_reorder(self):
        first = self.listed()
        selected = self.source.inventory["images"][1]["url"]
        self.source.inventory["images"].reverse()
        self.source.inventory["images"].insert(0, {"url": "https://cdn.test/recommendation.jpg", "kind": "img", "slot": "page:new", "loaded": True})
        for stable in (True, False):
            result = self.image(first, 2, stable)
            self.assertNotIn("isError", result, result)
            self.assertEqual(self.info(result)["index"], 2)
            self.assertIn(json.dumps(selected), self.scripts()[-1])
            self.assertNotIn("recommendation.jpg", self.scripts()[-1])

    def test_unrelated_removal_does_not_invalidate_selection(self):
        first = self.listed()
        self.source.inventory["images"] = self.source.inventory["images"][:1]
        self.assertNotIn("isError", self.image(first))

    def test_selected_url_changed_or_missing_never_remaps(self):
        first = self.listed()
        self.source.inventory["images"][0]["url"] = "https://cdn.test/replacement.jpg"
        for stable in (True, False):
            self.assert_error(self.image(first, stable=stable), "selected_source_changed")
        del self.source.inventory["images"][0]
        self.assert_error(self.image(first), "selected_source_changed")
        self.assertFalse(self.scripts())

    def test_navigation_invalidates_same_url(self):
        first = self.listed()
        self.source.inventory["document_id"] = "456:https://site.test/article"
        self.assert_error(self.image(first), "document_changed")
        self.assertFalse(self.scripts())

    def test_navigation_during_extraction_returns_no_pixels(self):
        first = self.listed()
        self.source.extracted = {"error": "document_changed"}
        self.assert_error(self.image(first), "document_changed")

    def test_image_id_index_mismatch_and_foreign_snapshot_rejected(self):
        first = self.listed()
        self.assert_error(self.service.call({"mode": "image", "page_id": PAGE["id"],
            "image_id": first["images"][1]["image_id"], "index": 1}), "image_id_mismatch")
        self.assert_error(self.service.call({"mode": "image", "page_id": PAGE["id"],
            "snapshot_id": first["snapshot_id"], "image_id": "unknown"}), "image_id_mismatch")
        self.assertFalse(self.scripts())

    def test_ids_cannot_cross_pages(self):
        first = self.listed()
        for stable in (True, False):
            self.assert_error(self.image(first, stable=stable, page_id=OTHER["id"]), "snapshot_expired")

    def test_ttl_does_not_slide_on_success(self):
        with patch.object(vision.time, "monotonic", return_value=100):
            first = self.listed()
        with patch.object(vision.time, "monotonic", return_value=399):
            self.assertNotIn("isError", self.image(first))
        with patch.object(vision.time, "monotonic", return_value=401):
            for stable in (True, False):
                self.assert_error(self.image(first, stable=stable), "snapshot_expired")

    def test_cache_is_eight_and_evicts_oldest(self):
        first = self.listed()
        for _ in range(vision.SNAPSHOT_COUNT):
            self.listed()
        self.assertEqual(len(self.service.snapshots), 8)
        self.assert_error(self.image(first, stable=False), "snapshot_expired")

    def test_pagination_keeps_original_order(self):
        first = self.listed(limit=2)
        self.source.inventory["images"].reverse()
        last = self.listed(page_id=PAGE["id"], snapshot_id=first["snapshot_id"], offset=2, limit=2)
        self.assertEqual(first["next_offset"], 2)
        self.assertEqual([i["alt"] for i in last["images"]], ["Fixture 3", "Fixture 4"])
        self.assertEqual([i["index"] for i in last["images"]], [3, 4])
        self.assertIsNone(last["next_offset"])

    def test_note_loading_to_three_stable_ready_polls(self):
        ready = inventory("note", "ready", 2)
        self.source.inventory_sequence = [inventory("note", "loading", 0), ready, ready, ready]
        first = self.listed()
        self.assertEqual((first["state"], first["total"], first["local_poll_count"]), ("ready", 2, 4))
        self.assertEqual(self.sleep.call_count, 3)

    def test_changed_expected_total_resets_ready_stability(self):
        old = inventory("note", "ready", 2)
        new = copy.deepcopy(old)
        new["expected_total"] = 3
        self.source.inventory_sequence = [old, old, new, new, new]
        first = self.listed()
        self.assertEqual(first["local_poll_count"], 5)

    def test_bounded_loading_and_partial_do_not_claim_ready(self):
        for count, state in ((0, "loading"), (1, "partial")):
            self.sleep.reset_mock()
            self.source.inventory = inventory("note", "loading", count)
            self.source.inventory["expected_total"] = 2
            first = self.listed()
            self.assertEqual(first["state"], state)
            self.assertEqual(first["local_poll_count"], vision.MAX_POLLS)
            self.assertEqual(self.sleep.call_count, vision.MAX_POLLS - 1)
        self.assertFalse(self.scripts())

    def test_elapsed_inventory_deadline_stops_before_poll_count_limit(self):
        self.source.inventory = inventory("note", "loading", 0)
        with patch.object(vision.time, "monotonic", side_effect=[0, vision.WAIT_SECONDS + 1, vision.WAIT_SECONDS + 1]):
            result = self.service._inventory(self.source, wait=True)
        self.assertEqual(result["local_poll_count"], 1)
        self.assertEqual(result["state"], "loading")
        self.sleep.assert_not_called()

    def test_elapsed_image_deadline_stops_before_poll_count_limit(self):
        self.source.extracted = {"error": "not_loaded"}
        with patch.object(vision.time, "monotonic", side_effect=[0, vision.WAIT_SECONDS + 1]):
            with self.assertRaises(vision.VisionError) as raised:
                self.service._image(self.source, "123:doc", self.source.inventory["images"][0], 1280)
        self.assertEqual(raised.exception.code, "not_loaded")
        self.assertEqual(len(self.scripts()), 1)
        self.sleep.assert_not_called()

    def test_url_template_placeholder_is_not_rewritten(self):
        url = "https://cdn.test/__DIMENSION__/image.jpg?literal=__SELECTED__"
        self.source.inventory["images"][0]["url"] = url
        result = self.image(self.listed())
        self.assertNotIn("isError", result, result)
        self.assertIn(json.dumps(url), self.scripts()[0])

    def test_generic_and_unsupported_do_not_wait(self):
        self.assertEqual(self.listed()["local_poll_count"], 1)
        self.source.inventory = inventory("note", "unsupported", 0)
        self.assertEqual(self.listed()["local_poll_count"], 1)
        self.sleep.assert_not_called()

    def test_blocked_list_stops_without_extraction(self):
        self.source.inventory = inventory("note", "blocked", 0)
        self.assert_error(self.service.call({"mode": "list"}), "page_blocked")
        self.assertEqual(len(self.source.calls), 1)
        self.assertFalse(self.scripts())
        self.sleep.assert_not_called()

    def test_newly_blocked_page_rejects_cached_image(self):
        first = self.listed()
        self.source.inventory["state"] = "blocked"
        self.assert_error(self.image(first), "page_blocked")
        self.assertFalse(self.scripts())

    def test_unknown_slide_order_rejected_before_extraction(self):
        self.source.inventory = inventory("note", "partial", 1)
        self.source.inventory["images"][0]["slot"] = "note:unindexed:0"
        first = self.listed()
        self.assert_error(self.image(first), "unknown_slide_order")
        self.assertFalse(self.scripts())

    def test_distinct_pixel_errors_no_source_retry_or_screenshot_fallback(self):
        first = self.listed()
        for code in ("cors_unavailable", "source_pixel_limit", "output_limit", "selected_source_changed", "canvas_unavailable", "pixel_read_failed"):
            self.source.extracted = {"error": code}
            before = len(self.scripts())
            self.assert_error(self.image(first), code)
            self.assertEqual(len(self.scripts()), before + 1)
        self.assertFalse(any(m == "Page.captureScreenshot" for m, _ in self.source.calls))
        self.network.assert_not_called()

    def test_not_loaded_has_bounded_local_poll_only(self):
        first = self.listed()
        self.source.extracted = {"error": "not_loaded"}
        self.assert_error(self.image(first), "not_loaded")
        self.assertEqual(len(self.scripts()), vision.MAX_POLLS)
        self.assertEqual(self.sleep.call_count, vision.MAX_POLLS - 1)
        self.assertEqual({m for m, _ in self.source.calls}, {"Runtime.evaluate"})
        self.network.assert_not_called()

    def test_local_loading_then_ready_returns_native_pixels(self):
        first = self.listed()
        self.source.extract_sequence = [{"error": "not_loaded"}, {"data": encoded()}]
        result = self.image(first)
        self.assertNotIn("isError", result, result)
        self.assertEqual(self.info(result)["local_poll_count"], 2)
        self.assertEqual(self.sleep.call_count, 1)

    def test_challenge_during_pending_image_stops_local_poll(self):
        first = self.listed()
        self.source.inventory_sequence = [inventory(), inventory("note", "blocked", 0)]
        self.source.extracted = {"error": "not_loaded"}
        self.assert_error(self.image(first), "page_blocked")
        self.assertEqual(len(self.scripts()), 1)

    def test_navigation_during_pending_image_stops_local_poll(self):
        first = self.listed()
        changed = inventory()
        changed["document_id"] = "456:changed"
        self.source.inventory_sequence = [inventory(), changed]
        self.source.extracted = {"error": "not_loaded"}
        self.assert_error(self.image(first), "document_changed")
        self.assertEqual(len(self.scripts()), 1)

    def test_transfer_and_jpeg_bounds(self):
        first = self.listed()
        cases = [(encoded(2000, 2000), "output_limit"), (encoded(1600, 1600), "output_limit"),
                 (base64.b64encode(b"x" * (vision.MAX_BYTES + 1)).decode(), "output_limit"),
                 ("!!!", "invalid_pixels"), (base64.b64encode(b"not jpeg").decode(), "invalid_pixels")]
        for data, code in cases:
            self.source.extracted = {"data": data}
            self.assert_error(self.image(first, max_dimension=1600), code)

    def test_no_target_creation_navigation_source_refetch_or_interaction(self):
        first = self.listed()
        self.assertNotIn("isError", self.image(first))
        self.assertEqual(set(self.factories), {"ws://source"})
        self.assertEqual({m for m, _ in self.source.calls}, {"Runtime.evaluate"})
        for forbidden in ("fetch(", "XMLHttpRequest", "new Image", ".src =", ".src=", "appendChild", ".click(", "scrollTo(", "scrollBy("):
            self.assertNotIn(forbidden, self.scripts()[0])
        self.network.assert_not_called()

    def test_extract_exception_sanitized_and_connection_closed(self):
        first = self.listed()
        self.source.closed = False
        self.source.extract_exception = RuntimeError("https://cdn.test/?signature=private-signature-leak")
        self.assert_error(self.image(first), "browser_unavailable")
        self.assertTrue(self.source.closed)

    def test_connect_and_inventory_exception_close_source(self):
        for name in ("connect_exception", "inventory_exception"):
            self.source.closed = False
            setattr(self.source, name, RuntimeError("private-signature"))
            self.assert_error(self.service.call({"mode": "list"}), "browser_unavailable")
            self.assertTrue(self.source.closed)
            setattr(self.source, name, None)

    def test_invalid_inventory_and_evaluation_error_are_explicit(self):
        self.source.inventory = {"images": "invalid"}
        self.assert_error(self.service.call({"mode": "list"}), "inventory_failed")
        with patch.object(self.source, "evaluate", return_value={"exceptionDetails": {"text": "private-signature"}}):
            self.assert_error(self.service.call({"mode": "list"}), "evaluation_failed")

    def test_default_screenshot_native_image_viewport_bounds(self):
        result = self.service.call({})
        self.assertNotIn("isError", result, result)
        self.assertEqual(result["content"][1], {"type": "image", "data": encoded(), "mimeType": "image/jpeg"})
        self.assertEqual([m for m, _ in self.source.calls], ["Page.getLayoutMetrics", "Page.captureScreenshot"])
        params = self.source.calls[-1][1]
        self.assertFalse(params["captureBeyondViewport"])
        clip = params["clip"]
        self.assertEqual((clip["x"], clip["y"]), (5, 1300))
        self.assertLessEqual(clip["width"] * clip["height"] * clip["scale"] ** 2, vision.MAX_PIXELS)
        self.assertTrue(self.source.closed)

    def test_screenshot_bounds_three_local_attempts(self):
        self.source.shot = encoded(2000, 2000)
        self.assert_error(self.service.call({}), "output_limit")
        captures = [p for m, p in self.source.calls if m == "Page.captureScreenshot"]
        self.assertEqual([c["quality"] for c in captures], [78, 55, 35])
        self.assertGreater(captures[0]["clip"]["scale"], captures[1]["clip"]["scale"])

    def test_invalid_screenshot_no_retry_and_exception_closes(self):
        self.source.shot = base64.b64encode(b"not jpeg").decode()
        self.assert_error(self.service.call({}), "invalid_pixels")
        self.assertEqual(sum(m == "Page.captureScreenshot" for m, _ in self.source.calls), 1)
        self.source.capture_exception = RuntimeError("private-signature")
        self.assert_error(self.service.call({}), "browser_unavailable")
        self.assertTrue(self.source.closed)

    def test_invalid_viewport_never_captures(self):
        for width in (0, -1, float("nan"), float("inf"), 100001, "500"):
            self.source.calls.clear()
            self.source.metrics["cssVisualViewport"]["clientWidth"] = width
            self.assert_error(self.service.call({}), "invalid_request")
            self.assertNotIn("Page.captureScreenshot", [m for m, _ in self.source.calls])

    def test_invalid_arguments_and_missing_page(self):
        for args in ([], {"max_dimension": 0}, {"max_dimension": True}, {"mode": "other"}, {"mode": "list", "limit": 51},
                     {"mode": "list", "offset": -1}, {"mode": "list", "offset": 1}, {"mode": "image"},
                     {"mode": "image", "page_id": "source", "index": 1}, {"image_id": ""}, {"snapshot_id": 1}, {"snapshot_id": "x"}):
            self.assert_error(self.service.call(args), "invalid_request")
        self.assert_error(self.service.call({"page_id": "missing"}), "page_unavailable")

    def test_source_url_boundaries_and_blob_no_fetch(self):
        bad = ["file:///etc/passwd", "javascript:alert(1)", "data:text/html,hi", "http://localhost/a", "http://127.0.0.1/a",
               "http://127.1/a", "http://2130706433/a", "http://0x7f000001/a", "http://10.0.0.1/a", "http://[::1]/a",
               "http://169.254.169.254/a", "https://name:secret@host.test/a", "https://host.local/a", "https://host.internal/a"]
        for url in bad:
            self.assertFalse(vision._allowed_url(url), url)
        for url in ("https://cdn.test/a", "data:image/png;base64,aA==", "blob:https://site.test/uuid"):
            self.assertTrue(vision._allowed_url(url), url)
        self.source.inventory["images"].extend({"url": url} for url in bad)
        self.assertEqual(self.listed()["total"], 4)
        self.source.inventory["images"][0]["url"] = "blob:https://site.test/observed"
        self.assertNotIn("isError", self.image(self.listed()))
        self.assertIn("blob:https://site.test/observed", self.scripts()[0])
        self.assertNotIn("fetch(", self.scripts()[0])
        self.network.assert_not_called()


@unittest.skipUnless(shutil.which("node"), "Node used only for offline JS DOM mocks")
class ExtractionJavaScriptTests(unittest.TestCase):
    def run_extract(self, case=None):
        case = case or {}
        selected = {"document_id": "123:https://site.test/article", "url": "https://cdn.test/second.jpg", "slot": "note:1"}
        selected.update(case.get("selected", {}))
        script = vision.EXTRACT_JS.replace("__DIMENSION__", "1280").replace("__SELECTED__", json.dumps(selected))
        harness = r"""
const cfg=__CONFIG__, operations=[];
globalThis.performance={timeOrigin:123};globalThis.location={href:'https://site.test/article'};
for(const name of ['fetch','XMLHttpRequest','Image','scrollTo','scrollBy'])globalThis[name]=function(){throw new Error('Forbidden '+name)};
const makeImg=(url,index,marker,ready=true)=>{
 const img={currentSrc:url,complete:ready,naturalWidth:cfg.width||900,naturalHeight:cfg.height||1280,marker,
 closest:selector=>({getAttribute:name=>name==='data-swiper-slide-index'?(cfg.invalidFirstAttribute?'invalid':String(index)):(name==='data-index'?String(index):null)})};
 Object.defineProperty(img,'src',{get:()=>url,set:()=>{throw new Error('src assignment forbidden')}});return img;};
const pictures=[makeImg('https://cdn.test/second.jpg',0,'wrong-slot'),makeImg('https://cdn.test/second.jpg',1,'unloaded-clone',false),
 makeImg('https://cdn.test/first.jpg',0,'first'),makeImg('https://cdn.test/second.jpg',1,'second',!cfg.notLoaded)];
if(cfg.missing)pictures.splice(1,3);
const canvas={width:0,height:0,getContext:()=>cfg.noContext?null:({fillRect:()=>{},drawImage:(img,x,y,w,h)=>operations.push({draw:img.marker,width:w,height:h})}),
 toDataURL:()=>{if(cfg.cors){const e=new Error('CORS');e.name='SecurityError';throw e;}if(cfg.pixelFailure)throw new Error('pixels');
 if(cfg.tooLarge)return 'data:image/jpeg;base64,'+'A'.repeat(2000024);return 'data:image/jpeg;base64,__JPEG__';}};
const root={querySelectorAll:selector=>{operations.push({selector});return pictures;}};
globalThis.document={querySelector:selector=>selector==='#noteContainer'?root:null,createElement:name=>{
 if(name!=='canvas')throw new Error('Element forbidden');operations.push({create:name});return canvas;}};
const result=__SCRIPT__;process.stdout.write(JSON.stringify({result,operations,canvas:{width:canvas.width,height:canvas.height}}));
""".replace("__CONFIG__", json.dumps(case)).replace("__JPEG__", encoded()).replace("__SCRIPT__", script)
        run = subprocess.run([shutil.which("node"), "-e", harness], capture_output=True, text=True, check=True, timeout=10)
        return json.loads(run.stdout)

    def test_existing_image_exact_slot_only_detached_canvas_and_no_new_requests(self):
        result = self.run_extract()
        self.assertEqual(result["result"]["data"], encoded())
        self.assertEqual([o["draw"] for o in result["operations"] if "draw" in o], ["second"])
        self.assertEqual([o["create"] for o in result["operations"] if "create" in o], ["canvas"])
        self.assertEqual(result["canvas"], {"width": 0, "height": 0})

    def test_invalid_first_index_attribute_uses_valid_fallback(self):
        result = self.run_extract({"invalidFirstAttribute": True})
        self.assertEqual(result["result"]["data"], encoded())
        self.assertEqual([o["draw"] for o in result["operations"] if "draw" in o], ["second"])

    def test_cors_unavailable_clears_canvas(self):
        result = self.run_extract({"cors": True})
        self.assertEqual(result["result"], {"error": "cors_unavailable"})
        self.assertEqual(result["canvas"], {"width": 0, "height": 0})

    def test_not_loaded_and_source_pixel_limit_are_distinct_no_canvas(self):
        for case, code in (({"notLoaded": True}, "not_loaded"), ({"width": 9000, "height": 9000}, "source_pixel_limit")):
            result = self.run_extract(case)
            self.assertEqual(result["result"], {"error": code})
            self.assertFalse(any("create" in o for o in result["operations"]))

    def test_output_limit_only_three_local_draws(self):
        result = self.run_extract({"tooLarge": True})
        self.assertEqual(result["result"], {"error": "output_limit"})
        self.assertEqual(sum("draw" in o for o in result["operations"]), 3)
        self.assertEqual(result["canvas"], {"width": 0, "height": 0})

    def test_document_change_or_source_missing_never_substitutes(self):
        for case, code in (({"selected": {"document_id": "456:other"}}, "document_changed"), ({"missing": True}, "selected_source_changed")):
            result = self.run_extract(case)
            self.assertEqual(result["result"], {"error": code})
            self.assertFalse(any("draw" in o for o in result["operations"]))

    def test_canvas_unavailable_and_other_pixel_failure_distinct(self):
        for case, code in (({"noContext": True}, "canvas_unavailable"), ({"pixelFailure": True}, "pixel_read_failed")):
            result = self.run_extract(case)
            self.assertEqual(result["result"], {"error": code})
            self.assertEqual(result["canvas"], {"width": 0, "height": 0})


if __name__ == "__main__":
    unittest.main()
