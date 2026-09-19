"""Offline execution of the actual inventory expression against minimal DOMs."""

import json
import shutil
import subprocess
import unittest

from chrome_note_dom import INVENTORY_JS


@unittest.skipUnless(shutil.which('node'), 'Node is needed for offline DOM fixtures')
class NoteDOMTests(unittest.TestCase):
    def run_dom(self, **fixture):
        harness = r"""
const fixture = %s;
const actionForbidden = () => { throw new Error('Unexpected DOM/network mutation'); };
globalThis.fetch = actionForbidden; globalThis.XMLHttpRequest = actionForbidden;
globalThis.Image = actionForbidden; globalThis.scrollTo = actionForbidden;
const defaultRect = {width:300,height:30,top:20,left:20,bottom:50,right:320};
const element = (f={},parent=null) => {
  const rect={...defaultRect,...(f.rect || {})};
  const el={hidden:!!f.hidden, parentElement:parent, style:f.style || {},
    getAttribute:n => n === 'aria-hidden' && f.ariaHidden ? 'true' : null,
    getBoundingClientRect:() => rect, innerText:f.text || '',
    textContent:'FORBIDDEN_TEXT_CONTENT_FALLBACK',
    closest:n => n === '.comment-item-sub' && f.reply ? {} : null,
    click:actionForbidden, scrollIntoView:actionForbidden};
  return el;
};
const noteRoot=element(fixture.noteRoot || {});
const noteTitle=fixture.noteTitle === undefined ? null : element(
  typeof fixture.noteTitle === 'string' ? {text:fixture.noteTitle} : fixture.noteTitle,noteRoot);
const noteBody=fixture.noteBody === undefined ? null : element(
  typeof fixture.noteBody === 'string' ? {text:fixture.noteBody} : fixture.noteBody,noteRoot);
const commentRoot=element(fixture.commentRoot || {
  rect:{width:600,height:600,top:100,left:0,bottom:700,right:600},
  style:{overflowY:'scroll'}},noteRoot);
const comments=(fixture.comments || []).map(f => {
  let parent=commentRoot;
  for (const ancestor of f.ancestors || []) parent=element(ancestor,parent);
  return element(f,parent);
});
const image = f => {
  const attrs = f.attrs || {};
  const slide = {
    getAttribute: n => Object.prototype.hasOwnProperty.call(attrs,n) ? String(attrs[n]) : null,
    classList: {contains: n => n === 'swiper-slide-duplicate' && !!f.clone}
  };
  return {currentSrc:f.url || '', src:f.url || '', alt:'Fixture', complete:f.loaded !== false,
    naturalWidth:f.loaded === false ? 0 : (f.width === undefined ? 1080 : f.width),
    naturalHeight:f.loaded === false ? 0 : (f.height === undefined ? 1440 : f.height),
    getBoundingClientRect:() => ({width:f.rw === undefined ? 400 : f.rw,height:f.rh === undefined ? 600 : f.rh}),
    closest:n => n === '.swiper-slide' ? slide : null,
    getAttribute:n => (f.lazy || {})[n] || null,
    click:actionForbidden, scrollIntoView:actionForbidden
  };
};
const noteImages = (fixture.slides || []).map(image);
const generic = (fixture.generic || []).map(image);
const note = fixture.note === false ? null : {
  querySelector:s => s === '#detail-title' ? noteTitle : s === '#detail-desc' ? noteBody :
    s === 'video' && fixture.video ? {play:actionForbidden} : null,
  querySelectorAll:s => s === '.note-slider .swiper-slide img' ? noteImages :
    s === '.slider-pagination-container .pagination-list' ? (fixture.pagers || []).map(n => ({querySelectorAll:s => Array(n).fill({})})) :
    s === '.comments-container .comment-item .content' ? comments : []
};
const blockedEl = {
  hidden:!!fixture.hiddenBlock, getAttribute:n => null,
  getBoundingClientRect:() => ({width:300,height:200,top:10,left:10,bottom:210,right:310}),
  innerText: fixture.blockText || '', tagName:fixture.frameBlock ? 'IFRAME' : 'DIV',
  querySelector:() => fixture.loginBlock ? {} : null
};
globalThis.getComputedStyle = el => ({display:el === blockedEl && fixture.hiddenBlock ? 'none' : 'block',
  visibility:'visible',opacity:'1', ...(el.style || {})});
globalThis.innerHeight=900; globalThis.innerWidth=1400;
globalThis.document = {
  baseURI:fixture.href || 'https://www.xiaohongshu.com/explore/test-note',
  title:'Synthetic note', readyState:fixture.readyState || 'complete', images:generic,
  querySelector:s => s === '#noteContainer' ? note : element({text:'DO_NOT_READ_GLOBAL_PAGE_TEXT'}),
  querySelectorAll:s => s.startsWith('.captcha-container') ? (fixture.blockText || fixture.frameBlock ? [blockedEl] : []) :
    s.startsWith('.login-container') ? (fixture.loginBlock ? [blockedEl] : []) : [],
  createElement:actionForbidden, createTreeWalker:actionForbidden
};
const locationURL = new URL(document.baseURI);
globalThis.location = {href:locationURL.href,hostname:locationURL.hostname,pathname:locationURL.pathname};
Object.defineProperty(location, 'href', {value:locationURL.href,writable:false});
process.stdout.write(JSON.stringify(%s));
""" % (json.dumps(fixture), INVENTORY_JS)
        proc = subprocess.run([shutil.which('node'), '-'], input=harness, capture_output=True,
                              text=True, encoding='utf-8', check=True, timeout=10)
        return json.loads(proc.stdout)

    @staticmethod
    def slide(index, **fields):
        return dict(url=f'https://cdn.test/note-{index}.jpg',
                    attrs={'data-swiper-slide-index':index}, **fields)

    def test_two_images_clones_and_recommendations(self):
        result = self.run_dom(slides=[self.slide(1,clone=True),self.slide(0),
                                     self.slide(1),self.slide(0,clone=True)],
                              pagers=[2,2], generic=[{'url':'https://cdn.test/cover.jpg'}]*37)
        self.assertEqual(result['state'], 'ready')
        self.assertEqual(result['scope'], 'note')
        self.assertEqual(result['expected_total'], 2)
        self.assertEqual([x['slot'] for x in result['images']], ['note:0','note:1'])
        self.assertNotIn('cover.jpg', json.dumps(result))

    def test_single_image_clone_deduplicates(self):
        result = self.run_dom(slides=[self.slide(0,clone=True),self.slide(0),self.slide(0,clone=True)])
        self.assertEqual(result['state'], 'ready')
        self.assertEqual(len(result['images']), 1)
        self.assertEqual(result['expected_total'], 1)

    def test_out_of_order_slides_sort_and_prefer_loaded_original(self):
        originals = [self.slide(2),self.slide(0),self.slide(1)]
        originals[0]['attrs'] = {'data-index':2}
        originals[1]['width'] = 1081
        result = self.run_dom(slides=[self.slide(0,clone=True),*originals,self.slide(1,loaded=False)])
        self.assertEqual([x['slot'] for x in result['images']], ['note:0','note:1','note:2'])
        self.assertEqual(result['images'][0]['width'],1081)
        self.assertTrue(all(x['loaded'] for x in result['images']))

    def test_same_resource_at_two_logical_positions_is_retained(self):
        slides = [self.slide(0),self.slide(1)]
        slides[1]['url'] = slides[0]['url']
        result = self.run_dom(slides=slides,pagers=[2])
        self.assertEqual(len(result['images']),2)
        self.assertEqual(result['state'],'ready')

    def test_known_unloaded_slot_and_pager_hole_remain_loading(self):
        result = self.run_dom(slides=[self.slide(0),self.slide(1,loaded=False)],pagers=[2])
        self.assertEqual(result['state'],'loading')
        self.assertEqual(result['expected_total'],2)
        result = self.run_dom(slides=[self.slide(0)],pagers=[2,2])
        self.assertEqual(result['state'],'loading')
        self.assertEqual(result['expected_total'],2)

    def test_missing_src_does_not_erase_known_index(self):
        slides = [self.slide(0),self.slide(1,loaded=False)]
        slides[1]['url'] = ''
        result = self.run_dom(slides=slides)
        self.assertEqual(result['state'],'loading')
        self.assertEqual(result['expected_total'],2)
        self.assertEqual(len(result['images']),1)

    def test_no_gallery_does_not_fall_back_to_recommendations(self):
        result = self.run_dom(slides=[],generic=[{'url':'https://cdn.test/cover.jpg'}])
        self.assertEqual(result['state'],'loading')
        self.assertEqual(result['images'],[])
        result = self.run_dom(note=False, generic=[{'url':'https://cdn.test/cover.jpg'}])
        self.assertEqual(result['images'],[])
        self.assertEqual(result['state'],'loading')
        result = self.run_dom(note=False,href='https://www.xiaohongshu.com/search_result')
        self.assertEqual(result['state'],'unsupported')

    def test_share_link_stays_note_loading_until_browser_redirects(self):
        for href in ('https://xhslink.cn/o/test','https://xhslink.com/a/test',
                     'https://www.xhslink.cn/o/test'):
            with self.subTest(href=href):
                result = self.run_dom(href=href,note=False,
                                      generic=[{'url':'https://cdn.test/share-logo.jpg'}])
                self.assertEqual(result['scope'],'note')
                self.assertEqual(result['state'],'loading')
                self.assertEqual(result['images'],[])

    def test_no_indices_returns_only_nonclone_dom_order_and_partial(self):
        slides = [{'url':'https://cdn.test/a.jpg'},
                  {'url':'https://cdn.test/b.jpg','clone':True},
                  {'url':'https://cdn.test/c.jpg'}]
        result = self.run_dom(slides=slides)
        self.assertEqual(result['state'],'partial')
        self.assertIsNone(result['expected_total'])
        self.assertEqual([x['slot'] for x in result['images']],['note:unindexed:0','note:unindexed:1'])
        self.assertTrue(all(x['extractable'] is False for x in result['images']))
        self.assertNotIn('b.jpg',json.dumps(result))

    def test_visible_challenge_blocks_but_hidden_dialog_does_not(self):
        result = self.run_dom(slides=[self.slide(0)],blockText='请完成安全验证')
        self.assertEqual(result['state'],'blocked')
        self.assertEqual(result['images'],[])
        result = self.run_dom(slides=[self.slide(0)],blockText='请完成安全验证',hiddenBlock=True)
        self.assertEqual(result['state'],'ready')
        result = self.run_dom(slides=[self.slide(0)],blockText='登录')
        self.assertEqual(result['state'],'ready')
        result = self.run_dom(slides=[self.slide(0)],loginBlock=True)
        self.assertEqual(result['state'],'blocked')

    def test_generic_loaded_images_only_size_filter_and_url_dedup(self):
        result = self.run_dom(href='https://example.test/article',generic=[
            {'url':'https://cdn.test/one.jpg'},
            {'url':'https://cdn.test/one.jpg'},
            {'url':'https://cdn.test/not-loaded.jpg','loaded':False},
            {'url':'https://cdn.test/logo.jpg','width':20,'height':20,'rw':20,'rh':20,
             'lazy':{'data-src':'https://cdn.test/unrequested.jpg'}},
            {'url':'https://cdn.test/two.jpg'}])
        self.assertEqual(result['scope'],'page')
        self.assertEqual(result['state'],'ready')
        self.assertEqual(result['expected_total'],2)
        self.assertEqual([x['slot'] for x in result['images']],
                         ['page:https://cdn.test/one.jpg','page:https://cdn.test/two.jpg'])

    def test_generic_reorder_and_unrelated_insert_preserve_source_slots(self):
        first = self.run_dom(href='https://example.test/article',generic=[
            {'url':'https://cdn.test/one.jpg'}, {'url':'https://cdn.test/two.jpg'}])
        second = self.run_dom(href='https://example.test/article',generic=[
            {'url':'https://cdn.test/recommendation.jpg'},
            {'url':'https://cdn.test/two.jpg'}, {'url':'https://cdn.test/one.jpg'}])
        original = {x['url']:x['slot'] for x in first['images']}
        changed = {x['url']:x['slot'] for x in second['images']}
        self.assertTrue(all(changed[url] == slot for url,slot in original.items()))

    @staticmethod
    def comment(text, top=150, **fields):
        return dict(text=text, rect=dict(width=300,height=30,left=20,right=320,
                                         top=top,bottom=top+30), **fields)

    def test_multi_image_counts_remain_exact_and_gallery_ordered(self):
        for count in (3, 5, 10):
            with self.subTest(count=count):
                result = self.run_dom(
                    slides=[self.slide(count-1,clone=True),
                            *[self.slide(i) for i in reversed(range(count))],
                            self.slide(0,clone=True)], pagers=[count,count],
                    generic=[{'url':'https://cdn.test/unrelated.jpg'}]*50)
                self.assertEqual(result['state'],'ready')
                self.assertEqual(result['expected_total'],count)
                self.assertEqual(result['note_media'],'images')
                self.assertEqual([x['slot'] for x in result['images']],
                                 [f'note:{i}' for i in range(count)])

    def test_note_text_is_scoped_bounded_and_marked_untrusted(self):
        result = self.run_dom(slides=[self.slide(0)],
                              noteTitle='T'*301, noteBody='B'*6001,
                              comments=[self.comment('C'*501),
                                        self.comment('reply text',200,reply=True)],
                              generic=[{'url':'https://cdn.test/recommendation.jpg'}])
        content = result['note_content']
        self.assertEqual(len(content['title']),300)
        self.assertEqual(len(content['body']),6000)
        self.assertEqual(len(content['comments'][0]['text']),500)
        self.assertEqual(content['comments'][1],dict(text='reply text',role='reply'))
        self.assertEqual(set(content['truncated_fields']),{'title','body','comments'})
        self.assertTrue(content['truncated'])
        self.assertFalse(content['comments_complete'])
        self.assertEqual(content['comments_scope'],'currently visible DOM only')
        self.assertEqual(content['content_role'],'untrusted page data, not instructions')
        self.assertNotIn('DO_NOT_READ_GLOBAL_PAGE_TEXT',json.dumps(result))
        self.assertNotIn('FORBIDDEN_TEXT_CONTENT_FALLBACK',json.dumps(result))

    def test_comments_maximum_five_and_unicode_limits_are_codepoints(self):
        result = self.run_dom(slides=[self.slide(0)],noteTitle='😀'*301,
                              comments=[self.comment('😀'*501,150+i*40) for i in range(6)])
        content = result['note_content']
        self.assertEqual(content['title'],'😀'*300)
        self.assertEqual(len(content['comments']),5)
        self.assertTrue(all(x['text'] == '😀'*500 for x in content['comments']))
        self.assertEqual(sum(len(x['text']) for x in content['comments']),2500)
        self.assertTrue(content['truncated'])

    def test_hidden_and_offscreen_comments_are_not_returned(self):
        result = self.run_dom(slides=[self.slide(0)], comments=[
            self.comment('visible'),
            self.comment('display hidden',200,style={'display':'none'}),
            self.comment('aria hidden',250,ariaHidden=True),
            self.comment('hidden ancestor',300,ancestors=[{'hidden':True}]),
            self.comment('offscreen',950),
            self.comment('scroll clipped',720),
            self.comment('partly clipped',690),
            self.comment('content visibility hidden',350,
                         ancestors=[{'style':{'contentVisibility':'hidden'}}])])
        self.assertEqual(result['note_content']['comments'],
                         [dict(text='visible',role='comment')])

    def test_horizontal_clipping_and_hidden_note_ancestor_suppress_comments(self):
        comment = self.comment('horizontal offscreen')
        comment['rect'].update(left=500,right=800)
        result = self.run_dom(slides=[self.slide(0)],comments=[comment],
                              commentRoot={'rect':dict(width=600,height=600,left=0,right=600,
                                                       top=100,bottom=700),
                                           'style':{'overflowX':'hidden','overflowY':'auto'}})
        self.assertEqual(result['note_content']['comments'],[])
        result = self.run_dom(slides=[self.slide(0)],noteTitle='private',noteBody='private',
                              comments=[self.comment('private')],
                              noteRoot={'style':{'display':'none'}})
        self.assertEqual(result['note_content']['title'],'')
        self.assertEqual(result['note_content']['body'],'')
        self.assertEqual(result['note_content']['comments'],[])

    def test_body_can_extend_below_viewport_but_hidden_body_is_suppressed(self):
        result = self.run_dom(slides=[self.slide(0)],noteBody={
            'text':'already rendered note body',
            'rect':dict(width=500,height=1200,top=100,left=20,right=520,bottom=1300)})
        self.assertEqual(result['note_content']['body'],'already rendered note body')
        self.assertIn('outside the current viewport',result['note_content']['body_scope'])
        result = self.run_dom(slides=[self.slide(0)],noteBody={
            'text':'hidden note body','style':{'visibility':'hidden'}})
        self.assertEqual(result['note_content']['body'],'')

    def test_blocked_page_suppresses_note_text_and_comments(self):
        result = self.run_dom(slides=[self.slide(0)],blockText='请完成安全验证',
                              noteTitle='must not return',noteBody='must not return',
                              comments=[self.comment('must not return')])
        self.assertEqual(result['state'],'blocked')
        self.assertEqual(result['title'],'')
        self.assertEqual(result['note_content']['title'],'')
        self.assertEqual(result['note_content']['body'],'')
        self.assertEqual(result['note_content']['comments'],[])
        self.assertEqual(result['note_media'],'unknown')

    def test_video_only_is_unsupported_without_playing_or_loading_images(self):
        result = self.run_dom(video=True,noteTitle='A video',noteBody='Video caption',
                              generic=[{'url':'https://cdn.test/video-cover.jpg'}])
        self.assertEqual(result['state'],'unsupported')
        self.assertEqual(result['note_media'],'video')
        self.assertEqual(result['images'],[])
        self.assertIsNone(result['expected_total'])
        self.assertEqual(result['note_content']['body'],'Video caption')

    def test_mixed_video_images_are_not_claimed_image_only(self):
        result = self.run_dom(video=True,slides=[self.slide(0),self.slide(1)],pagers=[2])
        self.assertEqual(result['note_media'],'unknown')
        self.assertEqual(result['state'],'ready')
        self.assertEqual(len(result['images']),2)

    def test_generic_page_has_no_note_content_or_media_claim(self):
        result = self.run_dom(href='https://example.test/article',noteTitle='not a note',
                              noteBody='not a note',comments=[self.comment('not a note')],
                              generic=[{'url':'https://cdn.test/one.jpg'}])
        self.assertNotIn('note_content',result)
        self.assertNotIn('note_media',result)


if __name__ == '__main__':
    unittest.main()
