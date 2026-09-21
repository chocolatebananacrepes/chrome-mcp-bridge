"""Explicit loading contracts; all requests are mocked, no real website access."""
import copy
import json
import unittest
from unittest.mock import patch
import chrome_vision as vision
from test_chrome_vision import FakeCDP, PAGE, inventory, encoded
from test_batch_images import Clock


class NativeCDP(FakeCDP):
    def __init__(self):
        super().__init__()
        self.hints=[]
        self.hint_error=None
        self.after_hint=None
        self.deliver=True

    def evaluate(self, script, await_promise=False):
        if script==vision.INVENTORY_JS:
            return super().evaluate(script,await_promise)
        if "img.loading = 'eager'" in script:
            selected,_=json.JSONDecoder().raw_decode(script.split('const selected = ',1)[1])
            self.hints.append(selected)
            if self.after_hint: self.after_hint()
            if self.hint_error: value={'error':self.hint_error}
            else:
                for item in self.inventory['images']:
                    if item['slot']==selected['slot'] and self.deliver: item['loaded']=True
                value={'status':'eager_requested'}
            return {'result':{'value':value}}
        selected,_=json.JSONDecoder().raw_decode(script.split('const selected = ',1)[1])
        loaded=any(i['slot']==selected['slot'] and i['loaded'] for i in self.inventory['images'])
        return {'result':{'value':{'data':encoded()} if loaded else {'error':'not_loaded'}}}


class NativeLoadingTests(unittest.TestCase):
    def setUp(self):
        self.source=NativeCDP()
        self.clock=Clock()
        self.service=vision.ChromeVision(lambda _:self.source,lambda:PAGE,allow_image_loading=True)
        patch.object(self.service,'_targets',return_value=[PAGE]).start()
        patch.object(vision.urllib.request,'urlopen',side_effect=AssertionError('No Python source fetch')).start()
        patch.object(vision.time,'monotonic',side_effect=self.clock.monotonic).start()
        patch.object(vision.time,'sleep',side_effect=self.clock.sleep).start()
        self.addCleanup(patch.stopall)

    def info(self,r): return json.loads(r['content'][0]['text'])

    def listed(self,count=2,loaded=False):
        self.source.inventory=inventory('note','ready' if loaded else 'loading',count)
        self.source.inventory['document_id']='123:https://www.xiaohongshu.com/explore/synthetic'
        for n,i in enumerate(self.source.inventory['images']):
            i.update(url=f'https://sns-webpic-qc.xhscdn.com/synthetic-{n}',loaded=loaded)
        r=self.service.call({'mode':'list','page_id':PAGE['id'],'wait_ms':0})
        self.ids=[i['image_id'] for i in self.info(r)['images']]
        return r

    def batch(self,**extra):
        args={'mode':'images','page_id':PAGE['id'],'image_ids':self.ids,'wait_ms':1000,
              'loading_policy':'native_lazy'}
        args.update(extra)
        return self.service.call(args)

    def test_server_disabled_by_default(self):
        self.assertFalse(vision.ChromeVision(None,None).allow_image_loading)
        self.listed(); self.service.allow_image_loading=False
        self.assertEqual(self.info(self.batch())['error'],'loading_disabled')
        self.assertEqual(self.source.hints,[])

    def test_passive_default_makes_no_hints(self):
        self.listed()
        self.assertEqual(self.info(self.batch(loading_policy='passive'))['returned_count'],0)
        self.assertEqual(self.source.hints,[])

    def test_serial_explicit_loading_returns_native_images(self):
        self.listed()
        r=self.batch(); data=self.info(r)
        self.assertEqual(data['returned_count'],2)
        self.assertEqual(data['loading_attempt_count'],2)
        self.assertTrue(data['browser_requests_may_increase'])
        self.assertIsNone(data['browser_request_count'])
        self.assertEqual([x['slot'] for x in self.source.hints],['note:0','note:1'])
        self.assertEqual(len([x for x in r['content'] if x['type']=='image']),2)

    def test_already_loaded_is_not_changed(self):
        self.listed(2,loaded=True)
        self.assertEqual(self.info(self.batch())['returned_count'],2)
        self.assertEqual(self.source.hints,[])

    def test_four_candidate_cap_and_shared_budget(self):
        self.listed(8); self.source.deliver=False
        data=self.info(self.batch())
        self.assertEqual(len(self.source.hints),4)
        self.assertEqual(data['returned_count'],0)
        self.assertLessEqual(sum(self.clock.sleeps),1.00001)
        self.assertEqual(data['results'][-1]['error'],'loading_budget_exhausted')

    def test_failed_non_native_hints_also_count_towards_cap(self):
        self.listed(8); self.source.hint_error='not_native_lazy'
        data=self.info(self.batch())
        self.assertEqual(len(self.source.hints),4)
        self.assertEqual(data['loading_attempt_count'],4)

    def test_wait_budget_is_shared_by_eligible_fill_candidates_not_all_missing(self):
        self.listed(12); self.source.deliver=False
        data=self.info(self.batch(wait_ms=6000))
        self.assertEqual(len(self.source.hints),4)
        self.assertEqual(data['returned_count'],0)
        self.assertAlmostEqual(sum(self.clock.sleeps),6.0)
        self.assertEqual(data['results'][-1]['error'],'loading_budget_exhausted')

    def test_same_source_is_never_attempted_again(self):
        self.listed(1); self.source.deliver=False
        self.batch(); self.clock.value+=31
        data=self.info(self.batch())
        self.assertEqual(data['results'][0]['error'],'loading_already_attempted')
        self.assertEqual(len(self.source.hints),1)

    def test_page_cooldown_is_not_a_sleep_or_retry(self):
        self.listed(2); self.source.deliver=False
        self.batch(image_ids=self.ids[:1])
        data=self.info(self.batch(image_ids=self.ids[1:]))
        self.assertEqual(data['results'][0]['error'],'loading_cooldown')
        self.assertGreater(data['cooldown_remaining'],0)
        self.assertEqual(data['still_missing'][0]['image_id'],self.ids[1])
        self.assertEqual(len(self.source.hints),1)

    def test_no_sources_outside_pinned_note_cdn(self):
        self.listed(1)
        snapshot=next(iter(self.service.snapshots.values()))
        for url in ('http://sns.xhscdn.com/a','https://xhscdn.com.evil.test/a',
                    'https://127.0.0.1/a','data:image/png;base64,a','https://sns.xhscdn.com:444/a'):
            item=copy.deepcopy(snapshot['images'][0]); item['url']=url
            with self.assertRaises(vision.VisionError): self.service._request_native_load(self.source,snapshot,item)
        self.assertEqual(self.source.hints,[])

    def test_blocked_or_document_change_discards_batch(self):
        for error in ('page_blocked','document_changed'):
            self.setUp()
            self.listed(); self.source.hint_error=error
            r=self.batch()
            self.assertEqual(self.info(r)['error'],error)
            self.assertEqual([b['type'] for b in r['content']],['text'])
            self.assertEqual(len(self.source.hints),1)
            patch.stopall()

    def test_challenge_after_hint_stops_remaining_batch(self):
        self.listed()
        self.source.after_hint=lambda:self.source.inventory.update(state='blocked')
        r=self.batch()
        self.assertEqual(self.info(r)['error'],'page_blocked')
        self.assertEqual(len(self.source.hints),1)

    def test_session_limit_and_concurrency_fail_closed(self):
        self.listed()
        self.service.load_lock.acquire()
        try: self.assertTrue(all(r['error']=='loading_busy' for r in self.info(self.batch())['results']))
        finally: self.service.load_lock.release()
        self.service.load_attempts=set(range(vision.MAX_LOAD_LEDGER))
        self.assertTrue(all(r['error']=='loading_session_limit' for r in self.info(self.batch())['results']))
        self.assertEqual(self.source.hints,[])

    def test_invalid_mode_and_zero_budget_before_mutation(self):
        self.listed()
        for extra in ({'mode':'list'},{'mode':'screenshot'},{'wait_ms':0},{'loading_policy':True}):
            self.assertEqual(self.info(self.batch(**extra))['error'],'invalid_request')
        self.assertEqual(self.source.hints,[])

    def test_invalid_id_no_partial_mutation(self):
        self.listed()
        self.assertEqual(self.info(self.batch(image_ids=[self.ids[0],'unknown']))['error'],'snapshot_expired')
        self.assertEqual(self.source.hints,[])

    def test_wait_diagnostics_and_empty_preview(self):
        self.listed(0)
        data=self.info(self.service.preview_opened_page(PAGE['id'],1000))
        self.assertEqual(data['wait_stop_reason'],'wait_budget_exhausted')
        self.assertEqual(data['wait_elapsed_ms'],1000)
        self.assertEqual(data['next_step'],'bounded_list_if_still_loading')

    def test_preview_can_return_stable_ids_before_pixels(self):
        self.listed(2)
        data=self.info(self.service.preview_opened_page(PAGE['id'],12000))
        self.assertEqual(data['wait_stop_reason'],'inventory_stable_pixels_pending')
        self.assertEqual(data['state'],'partial')
        self.assertEqual(data['loaded_count'],0)
        self.assertEqual(data['missing_image_ids'],self.ids)
        self.assertEqual(data['next_step'],'images_with_known_ids')
        self.assertEqual(data['wait_elapsed_ms'],500)


if __name__=='__main__': unittest.main()
