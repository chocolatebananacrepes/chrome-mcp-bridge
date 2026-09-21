"""Execute the actual hint mutation JS in a minimal Node fixture."""
import json
import shutil
import subprocess
import unittest
from chrome_vision import EAGER_JS


@unittest.skipUnless(shutil.which('node'),'Node required for JS contracts')
class NativeDOMTests(unittest.TestCase):
    def run_fixture(self,**options):
        selected={'document_id':'123:https://www.xiaohongshu.com/explore/test','slot':'note:0',
                  'url':'https://sns.xhscdn.com/fixture.png'}
        script=EAGER_JS.replace('__SELECTED__',json.dumps(selected)).replace('__INVENTORY__',
            json.dumps({'document_id':options.get('document',selected['document_id']),
                        'scope':'note','state':options.get('state','partial')}))
        harness=r'''
const f=__FIXTURE__, selected=__PINNED__;
let changes=0, hint=f.loading || 'lazy';
const forbidden=()=>{throw Error('forbidden operation')};
globalThis.fetch=forbidden; globalThis.Image=forbidden;
const slide={getAttribute:n=>n==='data-swiper-slide-index'?'0':null,
             classList:{contains:()=>!!f.clone}};
const img={src:f.changed?'https://sns.xhscdn.com/other.png':selected.url,
  currentSrc:f.changed?'https://sns.xhscdn.com/other.png':selected.url,
  srcset:f.srcset || '',complete:!!f.complete,naturalWidth:f.loaded?100:0,naturalHeight:f.loaded?100:0,
  closest:s=>s==='.swiper-slide'?slide:s==='picture'&&f.picture?{}:null,
  click:forbidden,scrollIntoView:forbidden};
Object.defineProperty(img,'loading',{get:()=>hint,set:v=>{changes++;hint=v}});
Object.defineProperty(img,'src',{value:img.src,writable:false});
const root={querySelectorAll:()=>f.duplicate?[img,img]:[img]};
globalThis.document={querySelector:()=>f.noRoot?null:root,createElement:forbidden};
const result=__SCRIPT__;
console.log(JSON.stringify({result,changes,hint}));
'''.replace('__FIXTURE__',json.dumps(options)).replace('__PINNED__',json.dumps(selected)).replace('__SCRIPT__',script)
        result=subprocess.run([shutil.which('node'),'-'],input=harness,text=True,encoding='utf-8',capture_output=True,check=True,timeout=10)
        return json.loads(result.stdout)

    def test_native_lazy_changes_only_one_existing_hint(self):
        r=self.run_fixture()
        self.assertEqual(r['result'],{'status':'eager_requested'})
        self.assertEqual((r['changes'],r['hint']),(1,'eager'))

    def test_no_mutation_for_unsupported_or_changed_cases(self):
        cases=[({'state':'blocked'},'page_blocked'),({'document':'changed'},'document_changed'),
               ({'noRoot':True},'selected_source_changed'),({'changed':True},'ambiguous_or_changed_source'),
               ({'clone':True},'ambiguous_or_changed_source'),({'duplicate':True},'ambiguous_or_changed_source'),
               ({'complete':True},'broken_image_no_retry'),({'loading':'eager'},'not_native_lazy'),
               ({'srcset':'https://other.test/img 2x'},'responsive_source_not_supported'),
               ({'picture':True},'responsive_source_not_supported')]
        for options,error in cases:
            with self.subTest(options=options):
                r=self.run_fixture(**options)
                self.assertEqual(r['result'],{'error':error})
                self.assertEqual(r['changes'],0)

    def test_loaded_image_untouched(self):
        r=self.run_fixture(complete=True,loaded=True)
        self.assertEqual(r['result'],{'status':'already_loaded'})
        self.assertEqual(r['changes'],0)


if __name__=='__main__': unittest.main()
