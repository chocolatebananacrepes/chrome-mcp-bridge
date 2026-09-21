"""Opt-in integration probe on Linux: isolated headless Chrome, synthetic only.

Not included in unittest discovery. All target requests are fulfilled locally;
an unreachable proxy additionally fails closed for browser background traffic.
No real profiles, cookies, website visits, bridge restarts or production changes.
"""
import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import urllib.request

import websocket
from chrome_mcp_bridge import CDPClient
from chrome_vision import ChromeVision


class Interceptor:
    def __init__(self,url):
        self.ws=websocket.create_connection(url,timeout=1)
        self.lock=threading.Lock(); self.counter=0; self.responses={}; self.requests=[]
        self.running=True
        self.thread=threading.Thread(target=self.run,daemon=True); self.thread.start()

    def emit(self,method,params):
        with self.lock:
            self.counter+=1; ident=self.counter
            self.ws.send(json.dumps({'id':ident,'method':method,'params':params}))
        return ident

    def send(self,method,params):
        ident=self.emit(method,params); deadline=time.monotonic()+10
        while ident not in self.responses and time.monotonic()<deadline: time.sleep(.01)
        if ident not in self.responses: raise RuntimeError('Fixture CDP timeout')
        value=self.responses.pop(ident)
        if 'error' in value: raise RuntimeError(value['error'])
        return value.get('result',{})

    def run(self):
        while self.running:
            try: message=json.loads(self.ws.recv())
            except websocket.WebSocketTimeoutException: continue
            except Exception: break
            if 'id' in message: self.responses[message['id']]=message; continue
            if message.get('method')!='Fetch.requestPaused': continue
            params=message['params']; url=params['request']['url']; self.requests.append(url)
            if url=='https://www.xiaohongshu.com/explore/chrome-mcp-synthetic':
                body='''<!doctype html><title>Synthetic lazy loading test</title>
<div id="noteContainer"><div class="note-slider">
<div class="swiper-slide" data-swiper-slide-index="0" style="position:absolute;top:100000px">
<img loading="lazy" crossorigin="anonymous" width="200" height="200" src="https://sns-webpic-qc.xhscdn.com/chrome-mcp-synthetic.svg">
</div></div></div>'''; content_type='text/html'; status=200
            elif url=='https://sns-webpic-qc.xhscdn.com/chrome-mcp-synthetic.svg':
                body='<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200"><rect width="200" height="200" fill="green"/></svg>'
                content_type='image/svg+xml'; status=200
            else: body=''; content_type='text/plain'; status=404
            self.emit('Fetch.fulfillRequest',{'requestId':params['requestId'],'responseCode':status,
                'responseHeaders':[{'name':'Content-Type','value':content_type},{'name':'Cache-Control','value':'no-store'},
                                   {'name':'Access-Control-Allow-Origin','value':'*'}],
                'body':base64.b64encode(body.encode()).decode()})

    def close(self):
        self.running=False; self.ws.close(); self.thread.join(timeout=2)


def main():
    # Snap Chromium can access user-owned non-hidden directories under the home.
    profile=Path(tempfile.mkdtemp(prefix='chrome-mcp-synthetic-',dir=str(Path.home())))
    browser=subprocess.Popen(['/usr/bin/chromium-browser','--headless=new',
        '--remote-debugging-port=0','--remote-debugging-address=127.0.0.1',
        '--remote-allow-origins=http://127.0.0.1','--user-data-dir='+str(profile),
        '--disable-background-networking','--disable-component-update','--disable-sync',
        '--disable-extensions','--no-first-run','--no-default-browser-check',
        '--proxy-server=socks5://127.0.0.1:9','about:blank'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    intercept=None
    try:
        deadline=time.monotonic()+20
        while not (profile/'DevToolsActivePort').exists() and time.monotonic()<deadline: time.sleep(.1)
        port=int((profile/'DevToolsActivePort').read_text().splitlines()[0])
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def get(path):
            with opener.open(f'http://127.0.0.1:{port}'+path,timeout=5) as response: return json.load(response)
        page=next(p for p in get('/json') if p['type']=='page')
        # Suppress Origin for this test's CDP connections; production stays unchanged.
        real_connect=websocket.create_connection
        def connect(url,**kwargs): return real_connect(url,suppress_origin=True,**kwargs)
        websocket.create_connection=connect
        intercept=Interceptor(page['webSocketDebuggerUrl'])
        intercept.send('Fetch.enable',{'patterns':[{'urlPattern':'*','requestStage':'Request'}]})
        intercept.send('Page.navigate',{'url':'https://www.xiaohongshu.com/explore/chrome-mcp-synthetic'})
        service=ChromeVision(CDPClient,lambda:page,port=port,allow_image_loading=True)
        preview=service.preview_opened_page(page['id'],3000)
        listing=json.loads(preview['content'][0]['text'])
        assert listing['total']==1,listing
        assert listing['loaded_count']==0,listing
        image_id=listing['images'][0]['image_id']
        assert not any('synthetic.svg' in u for u in intercept.requests),intercept.requests
        args={'mode':'images','page_id':page['id'],'image_ids':[image_id],'wait_ms':300}
        passive=json.loads(service.call(args)['content'][0]['text'])
        assert passive['returned_count']==0,passive
        assert not any('synthetic.svg' in u for u in intercept.requests),intercept.requests
        args.update(loading_policy='native_lazy',wait_ms=3000)
        active=service.call(args); result=json.loads(active['content'][0]['text'])
        assert result['returned_count']==1,result
        assert result['results'][0]['loading_action']=='eager_requested',result
        assert len([u for u in intercept.requests if 'synthetic.svg' in u])==1,intercept.requests
        assert len([b for b in active['content'] if b['type']=='image'])==1
        print(json.dumps({'result':'PASS','browser':get('/json/version')['Browser'],
            'passive_returned':0,'active_returned':1,'intercepted_image_requests':1,
            'real_website_requests':0,'isolated_profile':str(profile),
            'preview_stop_reason':listing['wait_stop_reason']}),flush=True)
    finally:
        if intercept: intercept.close()
        browser.terminate()
        try: browser.wait(timeout=10)
        except subprocess.TimeoutExpired: browser.kill(); browser.wait(timeout=5)
        # Retain the synthetic-only profile for audit; never recursively delete
        # an environment-derived or existing browser profile.
        print('Synthetic-only profile retained: '+str(profile),flush=True)


if __name__=='__main__': main()
