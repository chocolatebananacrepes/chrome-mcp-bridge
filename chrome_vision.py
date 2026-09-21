"""Passive browser pixels by default; separately gated native lazy-load opt-in."""
import base64
import hashlib
import ipaddress
import json
import math
import secrets
import struct
import threading
import time
import urllib.parse
import urllib.request
from collections import OrderedDict
from chrome_note_dom import INVENTORY_JS

MAX_BYTES = 1_500_000
MAX_PIXELS = 1_800_000
MAX_SOURCE_PIXELS = 64_000_000
MAX_URL_CHARS = 2_000_000
SNAPSHOT_TTL = 300
SNAPSHOT_COUNT = 8
POLL_SECONDS = 0.25
MAX_POLLS = 25
WAIT_SECONDS = 6.0
MAX_WAIT_MS = 15000
MAX_BATCH_IMAGES = 12
MAX_BATCH_BYTES = 8_000_000
MAX_BATCH_PIXELS = 12_000_000
MAX_LOAD_ATTEMPTS = 4
MAX_LOAD_LEDGER = 256
LOAD_COOLDOWN_SECONDS = 30

# Only changes the loading hint of an existing, indexed img. No src assignment,
# fetch, new Image, node movement, scroll, click, or website API calls.
EAGER_JS = r"""(() => {
  const selected = __SELECTED__;
  const guard = __INVENTORY__;
  if (guard.state === 'blocked') return {error:'page_blocked'};
  if (guard.document_id !== selected.document_id) return {error:'document_changed'};
  if (guard.scope !== 'note') return {error:'unsupported_loading'};
  const root = document.querySelector('#noteContainer');
  if (!root) return {error:'selected_source_changed'};
  const candidates = Array.from(root.querySelectorAll('.note-slider .swiper-slide img'));
  const matching = candidates.filter(img => {
    if ((img.currentSrc || img.src) !== selected.url) return false;
    const slide = img.closest('.swiper-slide');
    if (slide?.classList.contains('swiper-slide-duplicate')) return false;
    let index = null;
    for (const attr of ['data-swiper-slide-index','data-index']) {
      const raw = slide?.getAttribute(attr);
      if (raw != null && /^\d+$/.test(String(raw)) && Number(raw)<256) { index=Number(raw); break; }
    }
    return 'note:'+index === selected.slot;
  });
  if (matching.length !== 1) return {error:'ambiguous_or_changed_source'};
  const img = matching[0];
  if (img.complete && img.naturalWidth && img.naturalHeight) return {status:'already_loaded'};
  if (img.complete) return {error:'broken_image_no_retry'};
  // Avoid activating responsive candidates different from the pinned URL.
  if (img.src !== selected.url || img.srcset || img.closest('picture'))
    return {error:'responsive_source_not_supported'};
  if (img.loading !== 'lazy') return {error:'not_native_lazy'};
  img.loading = 'eager';
  return {status:'eager_requested'};
})()"""

TOOL_SCHEMA = {
    "name": "chrome_view",
    "description": (
        "Browser vision, passive by default. Xiaohongshu navigate may return a note preview; reuse its image_ids, "
        "do not list again when the IDs you need are already known. list combines ordered note images with "
        "bounded title/body/currently visible comments, excluding recommendations. Use images with page_id + "
        "image_ids to return up to 12 separate native images per call (8MB decoded JPEG/12MP total limits); or image for one. "
        "Partial/loading does not require another list when stable IDs are known: image/images wait locally "
        "within one shared wait_ms budget. Results label each returned/missing image; never infer unseen ones. "
        "IDs pin the same document "
        "and source for up to five minutes (eight-snapshot cache may evict earlier); unrelated recommendations do not invalidate them. "
        "By default only already-loaded pixels are read: no image/CDN refetch, scrolling, carousel clicks, "
        "likes, comments or messages. If blocked/unavailable, STOP; do not reload or repeatedly retry. "
        "screenshot explicitly captures the current viewport, not hidden carousel images. "
        "Normal page background traffic is not controlled by this tool. OPTIONAL loading_policy=native_lazy "
        "requires server opt-in on a dedicated profile and explicit caller selection, only with images. "
        "Read up to 12 IDs together; do not split into groups of four or wait/retry on a timer. "
        "Existing readable images are delivered before optional filling. Loading cooldown, busy or quota "
        "only skip fill, never discard readable images. Results include delivered, fetched_extra (a subset), "
        "still_missing and cooldown_remaining. Expired IDs: list the same page without old snapshot/IDs, not reload. "
        "It can initiate browser/CDN requests for at most 4 known missing native-lazy images, serially, "
        "once per source per server process; it never guarantees all images or zero account risk. "
        "Not a fallback to invoke automatically. Stop on login/challenge/rate limit; do not retry."
    ),
    "inputSchema": {"type": "object", "properties": {
        "mode": {"type": "string", "enum": ["screenshot", "list", "image", "images"], "default": "screenshot"},
        "page_id": {"type": "string", "description": "Existing page ID returned by navigate/list; required for image/pagination."},
        "image_id": {"type": "string", "description": "Stable image ID from list; preferred over numeric index."},
        "image_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 12,
                      "uniqueItems": True, "description": "For images: exact image_ids from one note preview/list; returned in this order, each separately labelled."},
        "snapshot_id": {"type": "string", "description": "Required only for index-based retrieval/pagination."},
        "index": {"type": "integer", "minimum": 1, "description": "Legacy 1-based index pinned to snapshot_id; never remapped."},
        "offset": {"type": "integer", "minimum": 0, "default": 0},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
        "max_dimension": {"type": "integer", "minimum": 128, "maximum": 1600, "default": 1280,
                          "description": "Requested longest edge. Batch additionally caps at 1024px (1000px for 12 images) and total pixel/byte budgets; effective size is returned."},
        "wait_ms": {"type": "integer", "minimum": 0, "maximum": 15000, "default": 6000,
                    "description": "Local loading wait budget. 0 checks once; ready returns early. Shared across a batch, not per image. No source refetch or page refresh."},
        "loading_policy": {"type":"string", "enum":["passive","native_lazy"], "default":"passive",
                           "description":"Explicit opt-in only: native_lazy may cause additional browser requests by changing existing img.loading, only with images and server permission. Not scroll/flip/fetch; not a guarantee of success. wait_ms must be positive."},
    }, "additionalProperties": False},
    "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True},
}

# Detached canvas; no appendChild, new Image, src assignment or fetching API.
EXTRACT_JS = r"""(() => {
  const selected = __SELECTED__, dimension = __DIMENSION__;
  if (String(performance.timeOrigin)+':'+location.href !== selected.document_id)
    return {error:'document_changed'};
  const note = selected.slot.startsWith('note:');
  const root = note ? document.querySelector('#noteContainer') : document;
  if (!root) return {error:'selected_source_changed'};
  const candidates = Array.from(root.querySelectorAll(note ? '.note-slider .swiper-slide img' : 'img'));
  const matching = candidates.filter(img => {
    if ((img.currentSrc || img.src) !== selected.url) return false;
    if (!note) return true;
    const slide=img.closest('.swiper-slide');
    let index=null;
    for (const attr of ['data-swiper-slide-index','data-index']) {
      const raw=slide?.getAttribute(attr);
      if (raw != null && /^\d+$/.test(String(raw)) && Number(raw)<256) { index=Number(raw); break; }
    }
    return 'note:'+index === selected.slot;
  });
  if (!matching.length) return {error:'selected_source_changed'};
  const img=matching.find(i => i.complete && i.naturalWidth && i.naturalHeight);
  if (!img) return {error:'not_loaded'};
  const sw=img.naturalWidth, sh=img.naturalHeight;
  if (sw*sh > 64000000) return {error:'source_pixel_limit'};
  let scale=Math.min(1,dimension/Math.max(sw,sh),Math.sqrt(1800000/(sw*sh)));
  const canvas=document.createElement('canvas');
  try {
    for (const quality of [0.78,0.55,0.35]) {
      canvas.width=Math.max(1,Math.floor(sw*scale)); canvas.height=Math.max(1,Math.floor(sh*scale));
      const context=canvas.getContext('2d'); if (!context) return {error:'canvas_unavailable'};
      context.fillStyle='white'; context.fillRect(0,0,canvas.width,canvas.height);
      context.drawImage(img,0,0,canvas.width,canvas.height);
      const data=canvas.toDataURL('image/jpeg',quality);
      if (data.startsWith('data:image/jpeg;base64,') && data.length <= 2000023)
        return {data:data.slice(23),source_width:sw,source_height:sh};
      scale*=0.7;
    }
    return {error:'output_limit'};
  } catch(e) { return {error:e.name === 'SecurityError' ? 'cors_unavailable' : 'pixel_read_failed'}; }
  finally { canvas.width=0; canvas.height=0; }
})()"""

class VisionError(Exception):
    def __init__(self, message, code="invalid_request"):
        super().__init__(message)
        self.code=code

def _value(result):
    if result.get("exceptionDetails"):
        raise VisionError("Page evaluation failed. Stop rather than reloading.", "evaluation_failed")
    return result.get("result", {}).get("value")

def _integer(args, name, default, minimum, maximum):
    value=args.get(name,default)
    if type(value) is not int or not minimum <= value <= maximum:
        raise VisionError(f"{name} must be an integer from {minimum} to {maximum}.")
    return value

def _allowed_url(url):
    # Identifiers only, never fetched; retain previous source-boundary checks.
    if not isinstance(url,str) or not url or len(url)>MAX_URL_CHARS: return False
    try:
        p=urllib.parse.urlsplit(url)
        if p.scheme == "data": return url.lower().startswith("data:image/") and "," in url
        if p.scheme == "blob": return _allowed_url(url[5:]) and urllib.parse.urlsplit(url[5:]).scheme in ("http","https")
        if p.scheme not in ("http","https") or not p.hostname or p.username or p.password: return False
        host=p.hostname.lower().rstrip(".")
        if host == "localhost" or host.endswith((".localhost",".local",".internal")): return False
        try: return ipaddress.ip_address(host).is_global
        except ValueError: return not all(c in "0123456789abcdefx." for c in host)
    except ValueError: return False

def _jpeg_dimensions(data):
    if not data.startswith(b"\xff\xd8"): raise VisionError("Invalid JPEG.","invalid_pixels")
    at=2
    while at+4 <= len(data):
        if data[at] != 255: break
        while at<len(data) and data[at]==255: at+=1
        if at>=len(data): break
        marker=data[at]; at+=1
        if marker in (0xD8,0xD9) or 0xD0<=marker<=0xD7: continue
        if at+2>len(data): break
        length=struct.unpack(">H",data[at:at+2])[0]
        if length<2 or at+length>len(data): break
        if marker in (0xC0,0xC1,0xC2) and length>=8:
            height,width=struct.unpack(">HH",data[at+3:at+7]); return width,height
        at+=length
    raise VisionError("JPEG dimensions unavailable.","invalid_pixels")

class ChromeVision:
    def __init__(self,cdp_factory,get_active_tab,host="127.0.0.1",port=9223,*,allow_image_loading=False):
        self.cdp_factory=cdp_factory; self.get_active_tab=get_active_tab
        self.endpoint=f"http://{host}:{port}"
        self.snapshots=OrderedDict(); self.lock=threading.Lock()
        self.allow_image_loading=allow_image_loading is True
        self.load_lock=threading.Lock()
        self.load_attempts=set()
        self.load_last={}

    def preview_opened_page(self, page_id, wait_ms=12000):
        """For an acknowledged navigation to a note/share URL, including initial blank DOM."""
        return self.call({"mode":"list", "page_id":page_id, "wait_ms":wait_ms}, expected_note=True)

    def call(self,args,*,expected_note=False):
        try:
            if not isinstance(args,dict) or set(args)-set(TOOL_SCHEMA["inputSchema"]["properties"]):
                raise VisionError("Arguments must contain only chrome_view schema fields.")
            mode=args.get("mode","screenshot")
            if mode not in ("screenshot","list","image","images"): raise VisionError("Invalid mode.")
            dimension=_integer(args,"max_dimension",1280,128,1600)
            wait_ms=_integer(args,"wait_ms",int(WAIT_SECONDS*1000),0,MAX_WAIT_MS)
            policy=args.get("loading_policy","passive")
            if policy not in ("passive","native_lazy"):
                raise VisionError("Invalid loading_policy.")
            if policy=="native_lazy":
                if mode!="images" or not wait_ms:
                    raise VisionError("native_lazy requires images and positive wait_ms.")
                if not self.allow_image_loading:
                    raise VisionError("Server has not enabled image loading on a dedicated profile.","loading_disabled")
            if mode=="images":
                ids=args.get("image_ids")
                if (not args.get("page_id") or not isinstance(ids,list) or not 1<=len(ids)<=MAX_BATCH_IMAGES or
                    any(not isinstance(i,str) or not i or len(i)>100 for i in ids) or len(set(ids))!=len(ids)):
                    raise VisionError("images requires page_id and 1..12 distinct image_ids from a note preview/list.")
                if "index" in args or "image_id" in args:
                    raise VisionError("Use image_ids only for batch selection; do not mix single-image selectors.")
            elif "image_ids" in args:
                raise VisionError("image_ids is only for mode=images.")
            for k in ("snapshot_id","image_id"):
                if k in args and (not isinstance(args[k],str) or not args[k] or len(args[k])>100):
                    raise VisionError(f"Invalid {k}.")
            if mode=="image" and not (args.get("page_id") and (args.get("image_id") or (args.get("snapshot_id") and args.get("index")))):
                raise VisionError("image requires page_id + image_id (or snapshot_id + index) from list.")
            if args.get("snapshot_id") and not args.get("page_id"): raise VisionError("snapshot_id requires explicit page_id.")
            tab=self._page(args.get("page_id")); cdp=self.cdp_factory(tab["webSocketDebuggerUrl"])
            try:
                cdp.connect()
                if mode=="screenshot":
                    result=self._capture(cdp,dimension)
                    info={"mode":mode,"page_id":tab["id"],"scope":"current browser viewport"}
                else:
                    inventory_options={}
                    if wait_ms!=int(WAIT_SECONDS*1000) or expected_note:
                        inventory_options={"wait_ms":wait_ms,"expected_note":expected_note}
                    data=self._inventory(cdp,wait=mode=="list" and not args.get("snapshot_id"),**inventory_options)
                    if data.get("state")=="blocked":
                        raise VisionError("Login/security challenge visible. Stop; user intervention required.","page_blocked")
                    sid,snapshot=self._snapshot(args,tab["id"],data)
                    if mode=="list": return self._list(args,tab["id"],sid,snapshot)
                    if mode=="images":
                        return self._batch(cdp,args,tab["id"],sid,snapshot,data,dimension,wait_ms)
                    selected,index=self._select(args,snapshot)
                    if not any(i["image_id"]==selected["image_id"] for i in data["images"]):
                        raise VisionError("Selected source disappeared/changed; no other image substituted.","selected_source_changed")
                    result=self._image(cdp,snapshot["document_id"],selected,dimension,wait_ms=wait_ms)
                    info={"mode":mode,"page_id":tab["id"],"snapshot_id":sid,"image_id":selected["image_id"],
                          "index":index,"total":len(snapshot["images"]),"kind":selected["kind"],
                          "scope":snapshot.get("scope","page"),"pixel_source":"existing DOM image","source_refetches":0}
                pixels,size=result; info.update(size)
                return {"content":[{"type":"text","text":json.dumps(info,ensure_ascii=False)},pixels]}
            finally: cdp.close()
        except VisionError as exc: code,message=exc.code,str(exc)
        except Exception: code,message="browser_unavailable","Browser operation failed. Stop automatic retries; check page manually."
        return {"isError":True,"content":[{"type":"text","text":json.dumps({"error":code,"message":message,
                "automatic_refetch":False,"retry":"do not reload or repeatedly retry"},ensure_ascii=False)}]}

    def _targets(self):
        with urllib.request.urlopen(self.endpoint+"/json",timeout=5) as r: return json.loads(r.read(2_000_000))

    def _page(self,page_id):
        if page_id is not None and (not isinstance(page_id,str) or not page_id): raise VisionError("Invalid page_id.")
        tab=next((t for t in self._targets() if t.get("id")==page_id and t.get("type")=="page"),None) if page_id else self.get_active_tab()
        if not tab or not tab.get("id") or not tab.get("webSocketDebuggerUrl"): raise VisionError("Requested page unavailable.","page_unavailable")
        return tab

    def _inventory(self,cdp,wait=False,wait_ms=None,expected_note=False):
        previous,stable=None,0
        stop_reason="single_observation"
        budget=WAIT_SECONDS if wait_ms is None else wait_ms/1000
        max_polls=MAX_POLLS if wait_ms is None else max(1,int(budget/POLL_SECONDS)+1)
        started=time.monotonic()
        deadline=started+budget
        for attempt in range(max_polls if wait else 1):
            data=_value(cdp.evaluate(INVENTORY_JS))
            if not isinstance(data,dict) or not isinstance(data.get("images"),list) or not data.get("document_id"):
                raise VisionError("Could not read image inventory.","inventory_failed")
            if expected_note and data.get("scope")!="note":
                # A newly acknowledged browser tab may still contain about:blank.
                # Do not treat its empty generic inventory as a completed note.
                initial_url=data["document_id"].split(":",1)[-1]
                data.update(scope="note",state="loading" if initial_url.startswith("about:blank") else "unsupported",
                            images=[],expected_total=None,note_content=None)
            data["images"]=[i for i in data["images"][:256] if isinstance(i,dict) and _allowed_url(i.get("url"))]
            for item in data["images"]:
                item.setdefault("slot","url:"+item["url"])
                item["image_id"]=hashlib.sha256(json.dumps([data["document_id"],item["slot"],item["url"]],ensure_ascii=True).encode()).hexdigest()[:32]
            sig=(data.get("state"),data.get("expected_total"),[(i["image_id"],bool(i.get("loaded"))) for i in data["images"]])
            stable=stable+1 if sig==previous else 1; previous=sig
            if not wait: break
            if data.get("scope")!="note" or data.get("state") in ("blocked","unsupported"):
                stop_reason=data.get("state","unsupported"); break
            if data.get("state")=="ready" and stable>=3:
                stop_reason="ready_stable"; break
            # Navigation needs stable IDs, not necessarily every decoded pixel.
            # The following images call owns its own bounded pixel wait.
            if (expected_note and stable>=3 and data["images"] and
                len(data["images"])==data.get("expected_total") and
                all(i.get("slot")=="note:"+str(n) for n,i in enumerate(data["images"]))):
                stop_reason="inventory_stable_pixels_pending"; break
            if time.monotonic()>=deadline:
                stop_reason="wait_budget_exhausted"; break
            stop_reason="poll_limit"
            if attempt+1<max_polls: time.sleep(min(POLL_SECONDS,max(0,deadline-time.monotonic())))
        data["local_poll_count"]=attempt+1
        data["wait_budget_ms"]=int(budget*1000) if wait else 0
        data["wait_elapsed_ms"]=max(0,round((time.monotonic()-started)*1000))
        data["wait_stop_reason"]=stop_reason
        if wait and data.get("scope")=="note" and (stable<3 or data.get("state")!="ready") and data.get("state") not in ("blocked","unsupported"):
            data["state"]="partial" if data["images"] else "loading"
        return data

    def _snapshot(self,args,page_id,inventory):
        now=time.monotonic()
        with self.lock:
            for key in list(self.snapshots):
                if now-self.snapshots[key]["created"]>SNAPSHOT_TTL: del self.snapshots[key]
            key=args.get("snapshot_id")
            wanted=set(args.get("image_ids") or ([args["image_id"]] if args.get("image_id") else []))
            if not key and wanted:
                key=next((k for k,v in reversed(self.snapshots.items()) if v["page_id"]==page_id and wanted.issubset({i["image_id"] for i in v["images"]})),None)
                if not key: raise VisionError("Image ID expired or was never listed for this page.","snapshot_expired")
            if key:
                old=self.snapshots.get(key)
                if not old or old["page_id"]!=page_id: raise VisionError("Snapshot expired or belongs to another page.","snapshot_expired")
                if old["document_id"]!=inventory["document_id"]: raise VisionError("Document changed; IDs were not remapped.","document_changed")
                return key,old
            if args.get("offset",0): raise VisionError("Pagination requires page_id and snapshot_id from first list.")
            key=secrets.token_urlsafe(18); inventory.update(created=now,page_id=page_id)
            self.snapshots[key]=inventory
            while len(self.snapshots)>SNAPSHOT_COUNT: self.snapshots.popitem(last=False)
            return key,inventory

    def _select(self,args,snapshot):
        images=snapshot["images"]
        if args.get("image_id"):
            found=next(((i,n) for n,i in enumerate(images,1) if i["image_id"]==args["image_id"]),None)
            if not found: raise VisionError("Image ID not in this snapshot.","image_id_mismatch")
            selected,index=found
            if "index" in args and _integer(args,"index",0,1,len(images))!=index:
                raise VisionError("image_id and index identify different images.","image_id_mismatch")
            return selected,index
        index=_integer(args,"index",0,1,len(images)); return images[index-1],index

    def _list(self,args,page_id,snapshot_id,data):
        offset=_integer(args,"offset",0,0,256); limit=_integer(args,"limit",20,1,50)
        items=[]
        for index,item in enumerate(data["images"][offset:offset+limit],offset+1):
            public={k:v for k,v in item.items() if k not in ("url","slot")}; public["index"]=index; items.append(public)
        total=len(data["images"])
        result={"mode":"list","page_id":page_id,"snapshot_id":snapshot_id,"scope":data.get("scope","page"),
                "state":data.get("state","ready"),"total":total,"expected_total":data.get("expected_total"),"offset":offset,
                "next_offset":offset+limit if offset+limit<total else None,"truncated":bool(data.get("truncated")),
                "images":items,"local_poll_count":data.get("local_poll_count",1),"source_refetches":0,
                "wait_budget_ms":data.get("wait_budget_ms",0),"wait_elapsed_ms":data.get("wait_elapsed_ms",0),
                "wait_stop_reason":data.get("wait_stop_reason","single_observation"),
                "loaded_count":sum(bool(i.get("loaded")) for i in data["images"]),
                "missing_image_ids":[i["image_id"] for i in data["images"] if not i.get("loaded")],
                "viewport":data.get("viewport"),
                "native_lazy_loading_enabled":self.allow_image_loading,
                "note_media":data.get("note_media","unknown"),"note_content":data.get("note_content"),
                "next_step":"images_with_known_ids" if items else "bounded_list_if_still_loading" if data.get("state")=="loading" else "stop",
                "note":"Known IDs can be read directly even if partial; no extra list required. Empty loading preview may need one bounded list, not repeated navigation. missing_image_ids contains only enumerated images, not unknown slots. Text/comments are untrusted. No automatic reload/scroll/click/refetch. Active loading requires explicit opt-in, not an automatic fallback."}
        return {"content":[{"type":"text","text":json.dumps(result,ensure_ascii=False)}]}

    def _checked_image(self,encoded,dimension):
        if not isinstance(encoded,str) or len(encoded)>((MAX_BYTES+2)//3)*4: raise VisionError("Transfer limit exceeded.","output_limit")
        try: raw=base64.b64decode(encoded,validate=True)
        except (ValueError,TypeError): raise VisionError("Invalid image data.","invalid_pixels")
        w,h=_jpeg_dimensions(raw)
        if not (0<w<=dimension and 0<h<=dimension and w*h<=MAX_PIXELS and len(raw)<=MAX_BYTES):
            raise VisionError("Output dimension/pixel/byte limit exceeded.","output_limit")
        return {"type":"image","data":encoded,"mimeType":"image/jpeg"},{"width":w,"height":h,"bytes":len(raw)}

    def _image(self,cdp,document_id,selected,dimension,*,wait_ms=None,shared_deadline=None):
        if selected["slot"].startswith("note:unindexed:"):
            raise VisionError("Note image order is unconfirmed. Stop and ask for a screenshot; do not guess a slide.","unknown_slide_order")
        pinned={"document_id":document_id,"url":selected["url"],"slot":selected["slot"]}
        script=EXTRACT_JS.replace("__DIMENSION__",str(dimension)).replace("__SELECTED__",json.dumps(pinned))
        budget=WAIT_SECONDS if wait_ms is None else wait_ms/1000
        max_polls=MAX_POLLS if wait_ms is None else max(1,int(budget/POLL_SECONDS)+1)
        deadline=shared_deadline if shared_deadline is not None else time.monotonic()+budget
        for attempt in range(max_polls):
            if attempt:
                current=self._inventory(cdp)
                if current.get("state")=="blocked":
                    raise VisionError("Challenge appeared during local wait; stopping.","page_blocked")
                if current["document_id"]!=document_id:
                    raise VisionError("Document changed during local wait; stopping.","document_changed")
            value=_value(cdp.evaluate(script))
            if not isinstance(value,dict): raise VisionError("Existing pixels unreadable.","pixel_read_failed")
            if value.get("error")!="not_loaded": break
            if time.monotonic()>=deadline: break
            if attempt+1<max_polls: time.sleep(min(POLL_SECONDS,max(0,deadline-time.monotonic())))
        if value.get("error"):
            code=value["error"]
            messages={
                "cors_unavailable":"Browser security does not permit pixel extraction. No bypass/refetch attempted; ask user for screenshot.",
                "not_loaded":"Pixels unavailable after bounded wait. No direct source fetch; browser loading may have occurred. Stop automatic retries.",
                "selected_source_changed":"Selected DOM source changed/disappeared; no other image substituted.",
                "document_changed":"Document changed during extraction; no pixels returned.",
                "source_pixel_limit":"Source exceeds 64 megapixel input limit.",
                "output_limit":"Image could not fit output pixel/byte limits."}
            raise VisionError(messages.get(code,"Existing pixels unavailable; no refetch attempted."),code)
        image,info=self._checked_image(value.get("data"),dimension); info["local_poll_count"]=attempt+1
        return image,info

    def _batch(self,cdp,args,page_id,snapshot_id,snapshot,current,dimension,wait_ms):
        """Deliver first; optional fill limits never gate safe existing pixels."""
        ids=args["image_ids"]
        selected=[self._select({"image_id":i},snapshot) for i in ids]
        current_ids={i["image_id"] for i in current["images"]}
        effective_dimension=min(dimension,1024,math.isqrt(MAX_BATCH_PIXELS//len(ids)))
        deadline=time.monotonic()+wait_ms/1000
        active=args.get("loading_policy","passive")=="native_lazy"
        load_count=0
        if active:
            document_url=snapshot["document_id"].split(":",1)[-1]
            host=urllib.parse.urlsplit(document_url).hostname or ""
            if snapshot.get("scope")!="note" or not (host=="xiaohongshu.com" or host.endswith(".xiaohongshu.com")):
                raise VisionError("Explicit loading is limited to known Xiaohongshu note images.","unsupported_loading")
        details=[{"image_id":item["image_id"],"index":index} for item,index in selected]
        images={}; total_bytes=0; total_pixels=0

        def guard(item=None):
            state=self._inventory(cdp)
            if state.get("state")=="blocked":
                raise VisionError("Challenge visible; entire batch stopped.","page_blocked")
            if state["document_id"]!=snapshot["document_id"]:
                raise VisionError("Document changed; entire batch stopped.","document_changed")
            if item is not None and (item["image_id"] not in current_ids or
                not any(i["image_id"]==item["image_id"] for i in state["images"])):
                raise VisionError("Selected source changed/disappeared; no substitution.","selected_source_changed")

        def unavailable(row,exc):
            if exc.code in ("page_blocked","document_changed"):
                raise exc
            row.update(status="unavailable",error=exc.code,message=str(exc))

        def retain(row,pixels,info):
            nonlocal total_bytes,total_pixels
            if total_bytes+info["bytes"]>MAX_BATCH_BYTES or total_pixels+info["width"]*info["height"]>MAX_BATCH_PIXELS:
                raise VisionError("Batch output budget reached; select fewer remaining IDs in a later call.","batch_output_limit")
            total_bytes+=info["bytes"]; total_pixels+=info["width"]*info["height"]
            for key in ("error","message"):
                row.pop(key,None)
            row.update(status="returned",**info)
            images[row["image_id"]]=pixels

        pending=[]
        for position,(item,index) in enumerate(selected):
            row=details[position]
            try:
                guard(item)
                # Preserve the passive wait path. Explicit mode instead reserves
                # output budget for every currently readable image before fill.
                pixels,info=self._image(cdp,snapshot["document_id"],item,effective_dimension,
                                       wait_ms=0 if active else wait_ms,shared_deadline=deadline)
                retain(row,pixels,info)
                if active: row["loading_action"]="already_loaded"
            except VisionError as exc:
                unavailable(row,exc)
                if active and exc.code=="not_loaded": pending.append(position)

        if active and pending:
            # This lock protects only fill and its ledger, not ordinary delivery.
            # Never wait for the lock or enqueue a retry after this call returns.
            locked=self.load_lock.acquire(blocking=False)
            try:
                skip=None
                if not locked:
                    skip=VisionError("Another fill is active; filling skipped, readable images retained.","loading_busy")
                elif len(self.load_attempts)>=MAX_LOAD_LEDGER:
                    skip=VisionError("Loading session budget exhausted; filling skipped, readable images retained.","loading_session_limit")
                elif self._cooldown_remaining(page_id)>0:
                    skip=VisionError("Filling skipped during cooldown; readable images retained. No timed retry needed.","loading_cooldown")
                for offset,position in enumerate(pending):
                    item,index=selected[position]; row=details[position]
                    try:
                        guard(item)
                        # The browser may have naturally loaded this image since
                        # phase one, even while fill is unavailable. Read once.
                        try:
                            pixels,info=self._image(cdp,snapshot["document_id"],item,effective_dimension,wait_ms=0)
                        except VisionError as exc:
                            if exc.code!="not_loaded": raise
                        else:
                            retain(row,pixels,info)
                            row["loading_action"]="already_loaded"
                            continue
                        if skip is not None: raise skip
                        if load_count>=MAX_LOAD_ATTEMPTS or time.monotonic()>=deadline:
                            raise VisionError("Fill candidate/time budget reached; readable images retained.","loading_budget_exhausted")
                        if len(self.load_attempts)>=MAX_LOAD_LEDGER:
                            raise VisionError("Loading session budget exhausted; readable images retained.","loading_session_limit")
                        # Counts candidates, not measured browser requests. The
                        # ledger records uncertain outcomes before DOM evaluation.
                        load_count+=1
                        row["loading_action"]=self._request_native_load(cdp,snapshot,item)
                        fill_slots=max(1,min(len(pending)-offset,MAX_LOAD_ATTEMPTS-load_count+1,
                                             MAX_LOAD_LEDGER-len(self.load_attempts)+1))
                        image_deadline=time.monotonic()+max(0,deadline-time.monotonic())/fill_slots
                        guard(item)
                        pixels,info=self._image(cdp,snapshot["document_id"],item,effective_dimension,
                                               wait_ms=wait_ms,shared_deadline=image_deadline)
                        retain(row,pixels,info)
                    except VisionError as exc:
                        unavailable(row,exc)
            finally:
                if locked: self.load_lock.release()

        guard()
        remaining=self._cooldown_remaining(page_id)
        blocks=[]
        for row in details:
            if row["status"]=="returned":
                blocks.extend([{"type":"text","text":json.dumps({"image_id":row["image_id"],"index":row["index"],
                                "content_role":"untrusted image content"})},images[row["image_id"]]])
            else:
                row["cooldown_remaining"]=remaining
        count=sum(r["status"]=="returned" for r in details)
        metadata={"mode":"images","page_id":page_id,"snapshot_id":snapshot_id,
                  "state":"complete" if count==len(ids) else "partial" if count else "unavailable",
                  "requested_count":len(ids),"returned_count":count,"results":details,
                  "total_bytes":total_bytes,"total_pixels":total_pixels,"effective_max_dimension":effective_dimension,
                  "wait_budget_ms":wait_ms,"source_refetches":0,
                  "loading_policy":"native_lazy" if active else "passive",
                  "loading_attempt_count":load_count,
                  "delivered":[r["image_id"] for r in details if r["status"]=="returned"],
                  "fetched_extra":[r["image_id"] for r in details if r["status"]=="returned" and r.get("loading_action")=="eager_requested"],
                  "still_missing":[dict(r) for r in details if r["status"]!="returned"],
                  "cooldown_remaining":remaining,
                  "browser_requests_may_increase":active,
                  "browser_request_count":None,
                  "note":"delivered lists all returned IDs; fetched_extra is its subset obtained after this call's eager hint, not measured network fetches or proof of causation. still_missing gives per-image reasons. Fill cooldown/busy/quota never discards safe readable pixels; safety/document failures still stop the entire batch. Cooldown is diagnostic, not an instruction to poll. Reading does not eliminate normal page background traffic."}
        result={"content":[{"type":"text","text":json.dumps(metadata,ensure_ascii=False)},*blocks]}
        if not count: result["isError"]=True
        return result

    def _cooldown_remaining(self,page_id):
        last=self.load_last.get(page_id)
        return 0.0 if last is None else math.ceil(max(0.0,LOAD_COOLDOWN_SECONDS-(time.monotonic()-last))*1000)/1000

    def _request_native_load(self,cdp,snapshot,item):
        """Under load_lock. Remember attempts before evaluation: uncertain != retryable."""
        if not item["slot"].startswith("note:") or not item["slot"][5:].isdigit():
            raise VisionError("Unknown logical slide index; no loading attempted.","unknown_slide_order")
        parsed=urllib.parse.urlsplit(item["url"]); host=parsed.hostname or ""
        if (parsed.scheme!="https" or parsed.username or parsed.password or
            parsed.port not in (None,443) or not (host=="xhscdn.com" or host.endswith(".xhscdn.com"))):
            raise VisionError("Loading only permits an existing HTTPS Xiaohongshu image CDN source.","unsupported_loading_source")
        key=(snapshot["document_id"],item["slot"],item["url"])
        if key in self.load_attempts:
            raise VisionError("This source already had its one loading attempt; no retry.","loading_already_attempted")
        if len(self.load_attempts)>=MAX_LOAD_LEDGER:
            raise VisionError("Loading session budget exhausted.","loading_session_limit")
        self.load_attempts.add(key)
        self.load_last[snapshot["page_id"]]=time.monotonic()
        pinned={"document_id":snapshot["document_id"],"url":item["url"],"slot":item["slot"]}
        script=EAGER_JS.replace("__INVENTORY__",INVENTORY_JS).replace("__SELECTED__",json.dumps(pinned))
        value=_value(cdp.evaluate(script))
        if not isinstance(value,dict):
            raise VisionError("Loading outcome unknown; do not retry.","loading_outcome_unknown")
        if value.get("error"):
            raise VisionError("Native lazy loading not performed: "+value["error"],value["error"])
        if value.get("status") not in ("eager_requested","already_loaded"):
            raise VisionError("Loading outcome unknown; do not retry.","loading_outcome_unknown")
        return value["status"]

    def _capture(self,cdp,dimension):
        # Explicit screenshot only; not an automatic image failure fallback.
        metrics=cdp.send("Page.getLayoutMetrics"); view=metrics.get("cssVisualViewport") or metrics.get("visualViewport") or {}
        w,h=view.get("clientWidth",0),view.get("clientHeight",0)
        if not all(isinstance(v,(int,float)) and math.isfinite(v) and 0<v<=100000 for v in (w,h)): raise VisionError("Viewport dimensions unavailable.")
        scale=min(1,dimension/max(w,h),math.sqrt(MAX_PIXELS/(w*h)))
        for quality in (78,55,35):
            shot=cdp.send("Page.captureScreenshot",{"format":"jpeg","quality":quality,"fromSurface":True,"captureBeyondViewport":False,
                "clip":{"x":view.get("pageX",0),"y":view.get("pageY",0),"width":w,"height":h,"scale":scale}})
            try: return self._checked_image(shot.get("data",""),dimension)
            except VisionError as exc:
                if exc.code!="output_limit": raise
                scale*=0.7
        raise VisionError("Screenshot cannot fit output limits.","output_limit")
