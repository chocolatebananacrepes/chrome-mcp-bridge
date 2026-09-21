"""Offline delivery/fill separation contracts; no live browser or source requests."""

import base64
import copy
import json
import shutil
import unittest
from unittest.mock import patch

import chrome_vision as vision
import test_note_dom as note_dom_fixture
from test_batch_images import Clock
from test_chrome_vision import FakeCDP, PAGE, encoded, inventory, jpeg


class DeliveryCDP(FakeCDP):
    """Expose read/hint ordering while retaining the production JS boundaries."""

    def __init__(self):
        super().__init__()
        self.events = []
        self.hints = []
        self.reads = []
        self.read_errors = {}
        self.hint_errors = {}
        self.hint_status = {}
        self.payloads = {}
        self.deliver_after_hint = True
        self.after_read = None
        self.after_hint = None

    def evaluate(self, script, await_promise=False):
        if script == vision.INVENTORY_JS:
            return super().evaluate(script, await_promise)
        self.calls.append(("Runtime.evaluate", script))
        selected, _ = json.JSONDecoder().raw_decode(script.split("const selected = ", 1)[1])
        slot = selected["slot"]
        number = int(slot.rsplit(":", 1)[1])
        current = next((i for i in self.inventory["images"] if i["slot"] == slot), None)
        if "img.loading = 'eager'" in script:
            self.hints.append(selected)
            self.events.append(("hint", slot))
            if number in self.hint_errors:
                value = {"error": self.hint_errors[number]}
            else:
                if current is not None and self.deliver_after_hint:
                    current["loaded"] = True
                value = {"status": self.hint_status.get(number, "eager_requested")}
            if self.after_hint:
                self.after_hint(number)
        else:
            self.reads.append(selected)
            self.events.append(("read", slot))
            if number in self.read_errors:
                value = {"error": self.read_errors[number]}
            elif current is None:
                value = {"error": "selected_source_changed"}
            elif current["loaded"]:
                value = {"data": self.payloads.get(number, encoded(640 + number, 480))}
            else:
                value = {"error": "not_loaded"}
            if self.after_read:
                self.after_read(number)
        return {"result": {"value": copy.deepcopy(value)}}


class DeliveryFillTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.source = DeliveryCDP()
        self.service = vision.ChromeVision(lambda _: self.source, lambda: PAGE,
                                           allow_image_loading=True)
        patch.object(self.service, "_targets", return_value=[PAGE]).start()
        self.network = patch.object(vision.urllib.request, "urlopen",
                                    side_effect=AssertionError("No network allowed")).start()
        patch.object(vision.time, "monotonic", side_effect=self.clock.monotonic).start()
        patch.object(vision.time, "sleep", side_effect=self.clock.sleep).start()
        self.timer = patch.object(vision.threading, "Timer",
                                  side_effect=AssertionError("No automatic timed retry")).start()
        self.addCleanup(patch.stopall)

    def tearDown(self):
        self.network.assert_not_called()
        self.timer.assert_not_called()

    @staticmethod
    def info(result):
        return json.loads(result["content"][0]["text"])

    def listed(self, count=12, missing=()):
        self.source.inventory = inventory("note", "loading" if missing else "ready", count)
        self.source.inventory["document_id"] = "123:https://www.xiaohongshu.com/explore/synthetic"
        for n, item in enumerate(self.source.inventory["images"]):
            item.update(url=f"https://sns-webpic-qc.xhscdn.com/synthetic-{n}", loaded=n not in missing)
        return self.list_inventory()

    def list_inventory(self):
        result = self.service.call({"mode": "list", "page_id": PAGE["id"], "wait_ms": 0,
                                    "limit": 50})
        self.assertNotIn("isError", result, result)
        self.ids = [i["image_id"] for i in self.info(result)["images"]]
        self.source.calls.clear()
        self.source.events.clear()
        self.source.hints.clear()
        self.source.reads.clear()
        self.clock.sleeps.clear()
        return self.info(result)

    def batch(self, ids=None, **extra):
        args = {"mode": "images", "page_id": PAGE["id"],
                "image_ids": self.ids if ids is None else ids, "wait_ms": 1000,
                "loading_policy": "native_lazy"}
        args.update(extra)
        return self.service.call(args)

    def assert_envelope(self, result, requested, delivered, fetched=()):
        info = self.info(result)
        self.assertEqual(info["delivered"], list(delivered))
        self.assertEqual(info["fetched_extra"], list(fetched))
        self.assertTrue(set(info["fetched_extra"]).issubset(info["delivered"]))
        self.assertEqual(info["requested_count"], len(requested))
        self.assertEqual(info["returned_count"], len(delivered))
        self.assertEqual([r["image_id"] for r in info["results"]], list(requested))
        self.assertEqual([r["image_id"] for r in info["results"] if r["status"] == "returned"],
                         list(delivered))
        missing = [i for i in requested if i not in delivered]
        self.assertEqual([r["image_id"] for r in info["still_missing"]], missing)
        self.assertIsInstance(info["cooldown_remaining"], (int, float))
        self.assertGreaterEqual(info["cooldown_remaining"], 0)
        for row in info["still_missing"]:
            self.assertTrue({"image_id", "index", "error", "message", "cooldown_remaining"} <= row.keys())
            self.assertEqual(row["index"], self.ids.index(row["image_id"]) + 1)
            self.assertTrue(row["error"])
            self.assertTrue(row["message"])
            self.assertGreaterEqual(row["cooldown_remaining"], 0)
        labels = [json.loads(c["text"])["image_id"] for c in result["content"][1:]
                  if c["type"] == "text"]
        self.assertEqual(labels, list(delivered))
        self.assertEqual(sum(c["type"] == "image" for c in result["content"]), len(delivered))
        self.assertEqual(info["source_refetches"], 0)
        if delivered:
            self.assertNotIn("isError", result, result)
        self.assertNotIn("private-signature", json.dumps(result))
        self.assertNotIn("private-document", json.dumps(result))
        return info

    def assert_fatal(self, result, error):
        self.assertTrue(result.get("isError"), result)
        self.assertEqual(self.info(result)["error"], error)
        self.assertEqual([c["type"] for c in result["content"]], ["text"])

    def assert_fill_gate_keeps_eleven(self, gate):
        for missing in (0, 5, 11):
            with self.subTest(gate=gate, missing_index=missing + 1):
                self.listed(12, missing=(missing,))
                self.service.load_last.clear()
                self.service.load_attempts.clear()
                if gate == "loading_cooldown":
                    self.service.load_last[PAGE["id"]] = self.clock.value
                elif gate == "loading_busy":
                    self.service.load_lock.acquire()
                else:
                    self.service.load_attempts.update(("other-document", f"note:{n}", f"source:{n}")
                                                      for n in range(vision.MAX_LOAD_LEDGER))
                started = self.clock.value
                try:
                    result = self.batch()
                finally:
                    if gate == "loading_busy":
                        self.service.load_lock.release()
                delivered = self.ids[:missing] + self.ids[missing + 1:]
                info = self.assert_envelope(result, self.ids, delivered)
                self.assertEqual(info["state"], "partial")
                self.assertEqual(info["still_missing"][0]["error"], gate)
                if gate == "loading_cooldown":
                    self.assertGreater(info["cooldown_remaining"], 0)
                    self.assertLessEqual(info["cooldown_remaining"], vision.LOAD_COOLDOWN_SECONDS)
                    self.assertGreater(info["still_missing"][0]["cooldown_remaining"], 0)
                self.assertEqual(self.source.hints, [])
                self.assertEqual(info["loading_attempt_count"], 0)
                self.assertEqual(self.clock.sleeps, [])
                self.assertEqual(self.clock.value, started)
                self.assertEqual([r["slot"] for r in self.source.reads[:12]],
                                 [f"note:{n}" for n in range(12)])
                self.assertTrue(all(r["slot"] == f"note:{missing}" for r in self.source.reads[12:]))

    def test_t_regress_01_cooldown_keeps_eleven_for_first_middle_and_last_missing(self):
        self.assert_fill_gate_keeps_eleven("loading_cooldown")

    def test_busy_fill_lock_keeps_eleven_for_first_middle_and_last_missing(self):
        self.assert_fill_gate_keeps_eleven("loading_busy")

    def test_full_256_entry_ledger_keeps_eleven_for_first_middle_and_last_missing(self):
        self.assertEqual(vision.MAX_LOAD_LEDGER, 256)
        self.assert_fill_gate_keeps_eleven("loading_session_limit")

    def test_all_loaded_delivers_during_all_three_fill_gates(self):
        self.listed(12)
        self.service.load_last[PAGE["id"]] = self.clock.value
        self.service.load_attempts.update(range(vision.MAX_LOAD_LEDGER))
        self.service.load_lock.acquire()
        try:
            result = self.batch()
        finally:
            self.service.load_lock.release()
        self.assert_envelope(result, self.ids, self.ids)
        self.assertEqual(self.source.hints, [])
        self.assertEqual(self.clock.sleeps, [])

    def test_stale_unloaded_inventory_does_not_hide_existing_pixels_during_cooldown(self):
        self.listed(12, missing=(0,))
        self.service.load_last[PAGE["id"]] = self.clock.value
        original = self.source.evaluate

        def pixels_now_available(script, await_promise=False):
            if script != vision.INVENTORY_JS and "img.loading = 'eager'" not in script:
                self.source.inventory["images"][0]["loaded"] = True
            return original(script, await_promise)

        with patch.object(self.source, "evaluate", side_effect=pixels_now_available):
            result = self.batch()
        self.assert_envelope(result, self.ids, self.ids)
        self.assertEqual(self.source.hints, [])

    def test_native_lazy_reads_entire_batch_without_wait_before_any_fill(self):
        self.listed(4, missing=(0, 2))
        order = [self.ids[n] for n in (2, 1, 0, 3)]
        with patch.object(self.service, "_image", wraps=self.service._image) as read:
            result = self.batch(order)
        self.assert_envelope(result, order, order, [self.ids[2], self.ids[0]])
        self.assertEqual(self.source.events[:4], [("read", f"note:{n}") for n in (2, 1, 0, 3)])
        self.assertTrue(all(c.kwargs["wait_ms"] == 0 for c in read.call_args_list[:4]))
        self.assertEqual([r["slot"] for r in self.source.hints], ["note:2", "note:0"])

    def test_already_loaded_hint_outcome_is_not_reported_as_fetched_extra(self):
        self.listed(2, missing=(0,))
        self.source.hint_status[0] = "already_loaded"
        result = self.batch()
        self.assert_envelope(result, self.ids, self.ids)
        self.assertEqual(len(self.source.hints), 1)

    def test_a_single_hint_failure_is_local_and_later_fill_still_succeeds(self):
        self.listed(3, missing=(0, 1))
        self.source.hint_errors[0] = "not_native_lazy"
        result = self.batch()
        info = self.assert_envelope(result, self.ids, self.ids[1:], [self.ids[1]])
        self.assertEqual(info["still_missing"][0]["error"], "not_native_lazy")
        self.assertEqual([r["slot"] for r in self.source.hints], ["note:0", "note:1"])

    def test_non_loading_pixel_failure_is_local_and_never_requests_fill(self):
        self.listed(4)
        self.source.read_errors[1] = "cors_unavailable"
        result = self.batch()
        info = self.assert_envelope(result, self.ids, [self.ids[n] for n in (0, 2, 3)])
        self.assertEqual(info["still_missing"][0]["error"], "cors_unavailable")
        self.assertEqual(self.source.hints, [])

    def test_disappeared_source_is_local_and_never_substituted_or_filled(self):
        self.listed(3)
        self.source.inventory["images"].pop(0)
        result = self.batch()
        info = self.assert_envelope(result, self.ids, self.ids[1:])
        self.assertEqual(info["still_missing"][0]["error"], "selected_source_changed")
        self.assertEqual(self.source.hints, [])

    def test_login_or_document_change_after_pixel_read_discards_entire_batch(self):
        for error in ("page_blocked", "document_changed"):
            with self.subTest(error=error):
                self.listed(3)

                def changed(number):
                    if number == 0:
                        if error == "page_blocked":
                            self.source.inventory["state"] = "blocked"
                        else:
                            self.source.inventory["document_id"] = "456:https://www.xiaohongshu.com/explore/other"

                self.source.after_read = changed
                self.assert_fatal(self.batch(), error)
                self.assertEqual(self.source.hints, [])
        self.source.after_read = None

    def test_login_or_document_change_during_fill_discards_cached_pixels(self):
        for error in ("page_blocked", "document_changed"):
            with self.subTest(error=error):
                self.listed(3, missing=(0,))
                self.service.load_last.clear()
                self.service.load_attempts.clear()
                self.source.hint_errors[0] = error
                result = self.batch()
                self.assert_fatal(result, error)
                self.assertEqual(self.source.events[:3], [("read", f"note:{n}") for n in range(3)])
                self.assertEqual(len(self.source.hints), 1)

    def test_late_login_after_successful_fill_discards_cached_pixels(self):
        self.listed(3, missing=(0,))
        self.source.after_hint = lambda _: self.source.inventory.update(state="blocked")
        self.assert_fatal(self.batch(), "page_blocked")
        self.assertEqual(len(self.source.hints), 1)

    def test_fill_cannot_spend_bytes_reserved_for_already_read_images(self):
        self.listed(6, missing=(0,))
        size = 1_400_000
        raw = jpeg()
        payload = base64.b64encode(raw + b"\x00" * (size - len(raw))).decode()
        self.source.payloads = {n: payload for n in range(6)}
        result = self.batch()
        info = self.assert_envelope(result, self.ids, self.ids[1:])
        self.assertEqual(info["still_missing"][0]["error"], "batch_output_limit")
        self.assertEqual(info["total_bytes"], size * 5)
        self.assertLessEqual(info["total_bytes"], vision.MAX_BATCH_BYTES)
        self.assertEqual(sum(len(base64.b64decode(c["data"])) for c in result["content"]
                             if c["type"] == "image"), info["total_bytes"])
        self.assertEqual(self.source.events[:6], [("read", f"note:{n}") for n in range(6)])

    def test_thirteen_images_in_twelve_plus_one_keep_delivery_during_new_cooldown(self):
        self.listed(13, missing=(0,))
        first = self.batch(self.ids[:12])
        self.assert_envelope(first, self.ids[:12], self.ids[:12], self.ids[:1])
        self.assertEqual(len(self.source.hints), 1)
        second = self.batch(self.ids[12:])
        self.assert_envelope(second, self.ids[12:], self.ids[12:])
        self.assertEqual(len(self.source.hints), 1)
        self.assertEqual(self.clock.sleeps, [])

    def test_no_timed_retry_and_no_repeated_hint_for_same_missing_source(self):
        self.listed(2, missing=(0,))
        self.source.deliver_after_hint = False
        first = self.batch(wait_ms=1000)
        self.assert_envelope(first, self.ids, self.ids[1:])
        self.assertEqual(len(self.source.hints), 1)
        self.assertLessEqual(sum(self.clock.sleeps), 1.000001)
        self.clock.sleeps.clear()
        second = self.batch()
        info = self.assert_envelope(second, self.ids, self.ids[1:])
        self.assertEqual(info["still_missing"][0]["error"], "loading_cooldown")
        self.assertEqual(self.clock.sleeps, [])
        self.clock.value += vision.LOAD_COOLDOWN_SECONDS + 1
        third = self.batch()
        info = self.assert_envelope(third, self.ids, self.ids[1:])
        self.assertEqual(info["still_missing"][0]["error"], "loading_already_attempted")
        self.assertEqual(info["cooldown_remaining"], 0)
        self.assertEqual(len(self.source.hints), 1)

    @unittest.skipUnless(shutil.which("node"), "Node required for fixed DOM fixtures")
    def test_passive_fixed_dom_counts_four_six_eight_eleven_thirteen(self):
        dom = note_dom_fixture.NoteDOMTests(methodName="runTest")
        for count in (4, 6, 8, 11, 13):
            with self.subTest(count=count):
                self.source.inventory = dom.run_dom(
                    slides=[dom.slide(n) for n in reversed(range(count))], pagers=[count])
                listing = self.list_inventory()
                self.assertEqual(listing["total"], count)
                self.assertEqual(listing["expected_total"], count)
                self.assertEqual([i["index"] for i in listing["images"]], list(range(1, count + 1)))
                returned = []
                for offset in range(0, count, 12):
                    chunk = self.ids[offset:offset + 12]
                    result = self.batch(chunk, loading_policy="passive", wait_ms=0)
                    info = self.assert_envelope(result, chunk, chunk)
                    self.assertEqual(info["cooldown_remaining"], 0)
                    self.assertLessEqual(info["total_bytes"], vision.MAX_BATCH_BYTES)
                    self.assertLessEqual(info["total_pixels"], vision.MAX_BATCH_PIXELS)
                    returned.extend(info["delivered"])
                self.assertEqual(returned, self.ids)
                self.assertEqual(self.source.hints, [])
                self.assertEqual(self.clock.sleeps, [])


if __name__ == "__main__":
    unittest.main()
