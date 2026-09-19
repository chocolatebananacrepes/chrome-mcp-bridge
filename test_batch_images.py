"""Offline batch-image contracts; no browser, network, page actions, or real waits."""
import base64
import copy
import json
import unittest
from unittest.mock import patch

import chrome_vision as vision
from test_chrome_vision import FakeCDP, PAGE, OTHER, encoded, inventory, jpeg


class Clock:
    def __init__(self):
        self.value = 100.0
        self.sleeps = []

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += seconds


class BatchCDP(FakeCDP):
    def __init__(self):
        super().__init__()
        self.extract_requests = []
        self.extract_handler = None

    def evaluate(self, script, await_promise=False):
        if script == vision.INVENTORY_JS:
            return super().evaluate(script, await_promise)
        self.calls.append(("Runtime.evaluate", script))
        selected, _ = json.JSONDecoder().raw_decode(script.split("const selected = ", 1)[1])
        self.extract_requests.append(selected)
        number = int(selected["slot"].rsplit(":", 1)[1])
        value = (self.extract_handler(selected, number) if self.extract_handler else
                 {"data": encoded(640+number, 480)})
        return {"result": {"value": copy.deepcopy(value)}}


class BatchImageTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.source = BatchCDP()
        self.factories = []
        def factory(ws):
            self.factories.append(ws)
            return self.source
        self.service = vision.ChromeVision(factory, lambda: PAGE)
        self.targets = patch.object(self.service, "_targets", return_value=[PAGE,OTHER]).start()
        self.network = patch.object(vision.urllib.request, "urlopen",
                                    side_effect=AssertionError("No source request allowed")).start()
        patch.object(vision.time, "monotonic", side_effect=self.clock.monotonic).start()
        patch.object(vision.time, "sleep", side_effect=self.clock.sleep).start()
        self.addCleanup(patch.stopall)

    @staticmethod
    def metadata(result):
        return json.loads(result["content"][0]["text"])

    def listed(self, count=2, *, loaded=True):
        self.source.inventory = inventory("note", "ready" if loaded else "loading", count)
        for item in self.source.inventory["images"]:
            item["loaded"] = loaded
        result = self.service.call({"mode":"list", "page_id":PAGE["id"], "wait_ms":0})
        self.assertNotIn("isError", result, result)
        self.source.calls.clear()
        self.source.extract_requests.clear()
        self.factories.clear()
        self.clock.sleeps.clear()
        return self.metadata(result)

    def batch(self, listing, order=None, **kwargs):
        order = list(range(len(listing["images"]))) if order is None else order
        args = {"mode":"images", "page_id":PAGE["id"],
                "image_ids":[listing["images"][n]["image_id"] for n in order]}
        args.update(kwargs)
        return self.service.call(args)

    def assert_fatal(self, result, code):
        self.assertTrue(result.get("isError"), result)
        self.assertEqual([c["type"] for c in result["content"]], ["text"])
        self.assertEqual(self.metadata(result)["error"],code)
        self.assertNotIn("private-signature", json.dumps(result))
        self.assertNotIn("private-document", json.dumps(result))

    def test_two_images_have_separate_native_envelopes_and_labels(self):
        listing = self.listed(2)
        result = self.batch(listing)
        info = self.metadata(result)
        self.assertNotIn("isError",result,result)
        self.assertEqual(info["state"],"complete")
        self.assertEqual((info["requested_count"],info["returned_count"]),(2,2))
        self.assertEqual([c["type"] for c in result["content"]],
                         ["text","text","image","text","image"])
        for n in range(2):
            label = json.loads(result["content"][1+n*2]["text"])
            self.assertEqual(label["image_id"],listing["images"][n]["image_id"])
            self.assertEqual(label["index"],n+1)
            self.assertEqual(result["content"][2+n*2],
                             {"type":"image","mimeType":"image/jpeg","data":encoded(640+n,480)})
        self.assertNotIn("private-signature",json.dumps(result))
        self.assertEqual(info["source_refetches"],0)
        self.assertEqual(self.clock.sleeps,[])
        self.assertTrue(self.source.closed)

    def test_twelve_images_preserve_requested_reverse_order_not_dom_order(self):
        listing = self.listed(12)
        self.source.inventory["images"].reverse()
        result = self.batch(listing,order=list(reversed(range(12))))
        info = self.metadata(result)
        self.assertEqual(info["returned_count"],12)
        self.assertEqual(info["state"],"complete")
        self.assertEqual([row["index"] for row in info["results"]],list(range(12,0,-1)))
        self.assertEqual([item["slot"] for item in self.source.extract_requests],
                         [f"note:{n}" for n in reversed(range(12))])
        images = [c["data"] for c in result["content"] if c["type"] == "image"]
        self.assertEqual(images,[encoded(640+n,480) for n in reversed(range(12))])
        self.assertEqual(info["effective_max_dimension"],1000)
        self.assertEqual(len(result["content"]),25)

    def test_invalid_duplicate_empty_and_excessive_ids_rejected_before_browser(self):
        listing = self.listed(12)
        valid = [row["image_id"] for row in listing["images"]]
        for ids in ([],[valid[0],valid[0]],valid+["extra"],"not-an-array",[1],[""],["x"*101]):
            with self.subTest(ids_type=type(ids).__name__,length=len(ids)):
                self.assert_fatal(self.batch(listing,image_ids=ids),"invalid_request")
                self.assertEqual(self.factories,[])
                self.assertEqual(self.source.extract_requests,[])

    def test_unknown_id_rejects_all_requested_images_before_any_extraction(self):
        listing = self.listed(2)
        ids = [listing["images"][0]["image_id"],"unknown-id"]
        self.assert_fatal(self.batch(listing,image_ids=ids),"snapshot_expired")
        self.assert_fatal(self.batch(listing,image_ids=ids,
                                     snapshot_id=listing["snapshot_id"]),"image_id_mismatch")
        self.assertEqual(self.source.extract_requests,[])

    def test_cross_page_and_mixed_selectors_are_rejected(self):
        listing = self.listed(2)
        self.assert_fatal(self.batch(listing,page_id=OTHER["id"]),"snapshot_expired")
        for extra in ({"image_id":listing["images"][0]["image_id"]},{"index":1}):
            self.assert_fatal(self.batch(listing,**extra),"invalid_request")
        self.assertEqual(self.source.extract_requests,[])

    def test_partially_unloaded_batch_returns_only_correct_available_image(self):
        listing = self.listed(2,loaded=False)
        self.source.extract_handler = lambda selected, n: ({"error":"not_loaded"} if n == 0
                                                         else {"data":encoded(641,480)})
        result = self.batch(listing,wait_ms=0)
        info = self.metadata(result)
        self.assertNotIn("isError",result,result)
        self.assertEqual(info["state"],"partial")
        self.assertEqual(info["returned_count"],1)
        self.assertEqual(info["results"][0]["error"],"not_loaded")
        self.assertEqual(info["results"][1]["status"],"returned")
        self.assertEqual(json.loads(result["content"][1]["text"])["index"],2)
        self.assertEqual(result["content"][2]["data"],encoded(641,480))
        self.assertEqual(self.clock.sleeps,[])

    def test_all_unloaded_is_explicit_unavailable_with_no_native_images(self):
        listing = self.listed(2,loaded=False)
        self.source.extract_handler = lambda *_: {"error":"not_loaded"}
        result = self.batch(listing,wait_ms=0)
        self.assertTrue(result.get("isError"))
        info = self.metadata(result)
        self.assertEqual((info["state"],info["returned_count"]),("unavailable",0))
        self.assertEqual([row["error"] for row in info["results"]],["not_loaded"]*2)
        self.assertEqual([c["type"] for c in result["content"]],["text"])

    def test_missing_current_source_is_not_substituted_and_other_images_continue(self):
        listing = self.listed(2)
        self.source.inventory["images"].pop(0)
        result = self.batch(listing,wait_ms=0)
        info = self.metadata(result)
        self.assertEqual(info["state"],"partial")
        self.assertEqual(info["results"][0]["error"],"selected_source_changed")
        self.assertEqual(info["results"][1]["status"],"returned")
        self.assertEqual([row["slot"] for row in self.source.extract_requests],["note:1"])

    def test_document_change_after_first_image_discards_all_previously_read_pixels(self):
        listing = self.listed(2)
        def extract(selected,n):
            if n == 0:
                self.source.inventory["document_id"] = "456:https://site.test/different"
            return {"data":encoded(640+n,480)}
        self.source.extract_handler = extract
        result = self.batch(listing)
        self.assert_fatal(result,"document_changed")
        self.assertEqual(len(self.source.extract_requests),1)

    def test_challenge_after_first_image_discards_all_previously_read_pixels(self):
        listing = self.listed(2)
        def extract(selected,n):
            if n == 0:
                self.source.inventory["state"] = "blocked"
            return {"data":encoded(640+n,480)}
        self.source.extract_handler = extract
        result = self.batch(listing)
        self.assert_fatal(result,"page_blocked")
        self.assertEqual(len(self.source.extract_requests),1)

    def test_document_change_after_final_extraction_discards_entire_batch(self):
        listing = self.listed(2)
        def extract(selected,n):
            if n == 1:
                self.source.inventory["document_id"] = "456:https://site.test/different"
            return {"data":encoded(640+n,480)}
        self.source.extract_handler = extract
        self.assert_fatal(self.batch(listing),"document_changed")

    def test_challenge_after_final_extraction_discards_entire_batch(self):
        listing = self.listed(2)
        def extract(selected,n):
            if n == 1:
                self.source.inventory["state"] = "blocked"
            return {"data":encoded(640+n,480)}
        self.source.extract_handler = extract
        self.assert_fatal(self.batch(listing),"page_blocked")

    def test_total_byte_budget_never_returns_over_limit(self):
        listing = self.listed(6)
        one = jpeg(640,480)
        target_bytes = 1_400_000
        payload = base64.b64encode(one+b"\x00"*(target_bytes-len(one))).decode()
        self.source.extract_handler = lambda *_: {"data":payload}
        result = self.batch(listing)
        info = self.metadata(result)
        self.assertEqual(info["state"],"partial")
        self.assertEqual(info["returned_count"],5)
        self.assertEqual(info["results"][-1]["error"],"batch_output_limit")
        actual = sum(len(base64.b64decode(c["data"])) for c in result["content"] if c["type"] == "image")
        self.assertEqual(actual,info["total_bytes"])
        self.assertEqual(actual,target_bytes*5)
        self.assertLessEqual(actual,vision.MAX_BATCH_BYTES)

    def test_twelve_image_pixel_cap_is_enforced_before_public_output(self):
        listing = self.listed(12)
        self.source.extract_handler = lambda *_: {"data":encoded(1000,1000)}
        result = self.batch(listing,max_dimension=1600)
        info = self.metadata(result)
        self.assertEqual(info["returned_count"],12)
        self.assertEqual(info["effective_max_dimension"],1000)
        self.assertEqual(info["total_pixels"],12_000_000)
        self.assertLessEqual(info["total_pixels"],vision.MAX_BATCH_PIXELS)
        self.source.extract_handler = lambda *_: {"data":encoded(1001,1000)}
        rejected = self.batch(listing,max_dimension=1600)
        rejected_info = self.metadata(rejected)
        self.assertEqual(rejected_info["returned_count"],0)
        self.assertTrue(all(row["error"] == "output_limit" for row in rejected_info["results"]))
        self.assertEqual([c["type"] for c in rejected["content"]],["text"])

    def test_defense_in_depth_aggregate_pixel_check(self):
        listing = self.listed(12)
        # Even if an extraction helper supplies oversized accounting metadata,
        # the aggregate output check must never cross its independent budget.
        image = {"type":"image","data":encoded(),"mimeType":"image/jpeg"}
        details = dict(width=1100,height=1000,bytes=len(jpeg()),local_poll_count=1)
        with patch.object(self.service,"_image",return_value=(image,details)):
            result = self.batch(listing)
        info = self.metadata(result)
        self.assertEqual(info["returned_count"],10)
        self.assertEqual(info["total_pixels"],11_000_000)
        self.assertLessEqual(info["total_pixels"],vision.MAX_BATCH_PIXELS)
        self.assertEqual([r["error"] for r in info["results"] if r["status"] == "unavailable"],
                         ["batch_output_limit"]*2)

    def test_one_shared_wait_budget_not_a_fresh_wait_for_every_image(self):
        listing = self.listed(3,loaded=False)
        self.source.extract_handler = lambda selected,n: ({"error":"not_loaded"} if n < 2
                                                         else {"data":encoded(642,480)})
        start = self.clock.value
        result = self.batch(listing,wait_ms=2000)
        info = self.metadata(result)
        self.assertEqual(info["returned_count"],1)
        self.assertEqual(info["wait_budget_ms"],2000)
        self.assertAlmostEqual(self.clock.value-start,2.0)
        self.assertAlmostEqual(sum(self.clock.sleeps),2.0)
        self.assertEqual(len(self.clock.sleeps),8)
        slots = [r["slot"] for r in self.source.extract_requests]
        self.assertEqual(slots.count("note:0"),9)
        self.assertEqual(slots.count("note:1"),1)
        self.assertEqual(slots.count("note:2"),1)
        self.assertEqual(info["results"][-1]["status"],"returned")

    def test_ready_during_local_wait_returns_early_without_next_list(self):
        listing = self.listed(2,loaded=False)
        calls = {0:0,1:0}
        def extract(selected,n):
            calls[n] += 1
            return {"error":"not_loaded"} if calls[n] < 2 else {"data":encoded(640+n,480)}
        self.source.extract_handler = extract
        result = self.batch(listing,wait_ms=2000)
        self.assertEqual(self.metadata(result)["returned_count"],2)
        self.assertAlmostEqual(sum(self.clock.sleeps),0.5)
        self.assertEqual(calls,{0:2,1:2})

    def test_wait_ms_bounds_are_validated(self):
        listing = self.listed(2)
        for wait_ms in (-1,15001,True,1.5,"6000",None):
            self.assert_fatal(self.batch(listing,wait_ms=wait_ms),"invalid_request")
        self.assertEqual(self.source.extract_requests,[])

    def test_batch_executes_no_source_fetch_or_page_interactions(self):
        listing = self.listed(2)
        result = self.batch(listing)
        self.assertEqual(self.metadata(result)["returned_count"],2)
        self.assertEqual(set(self.factories),{"ws://source"})
        self.assertEqual({method for method,_ in self.source.calls},{"Runtime.evaluate"})
        scripts = [s for _,s in self.source.calls if s != vision.INVENTORY_JS]
        for script in scripts:
            for forbidden in ("fetch(","XMLHttpRequest","new Image",".src =",".src=",
                              "appendChild",".click(","scrollTo(","scrollBy("):
                self.assertNotIn(forbidden,script)
        self.network.assert_not_called()


if __name__ == "__main__":
    unittest.main()
