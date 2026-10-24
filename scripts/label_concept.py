# scripts/label_concept.py
"""Label the concept batch: how good does it look, and is it sugar water?

Two judgements per picture on purpose. A 1-10 score alone cannot separate
"badly shot" from "polished but empty", and sugar water is exactly the
second one -- pretty, familiar, no idea. Collapsing both into one number is
what made the existing ratings unable to teach that concept.

The page is blind: it never says whether the batch picked a picture for
being sugar-prone. Labels are written after every click, so the tab can be
closed and reopened without losing work.

    .venv\\Scripts\\python scripts\\label_concept.py
    # then open http://127.0.0.1:8099
"""
from __future__ import annotations

import argparse
import json
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent

PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>糖水标注</title>
<style>
 :root { color-scheme: dark; }
 body { margin:0; background:#111; color:#eee; font:14px/1.5 system-ui,sans-serif; }
 #wrap { display:flex; flex-direction:column; align-items:center; height:100vh; }
 #bar { width:100%; padding:6px 12px; background:#1b1b1b; font-size:13px; display:flex;
        justify-content:space-between; }
 #stage { flex:1; display:flex; align-items:center; justify-content:center; min-height:0; }
 img { max-width:96vw; max-height:calc(100vh - 190px); object-fit:contain;
       background:#000; box-shadow:0 0 24px #000; }
 .row { display:flex; gap:6px; padding:6px; flex-wrap:wrap; justify-content:center; }
 button { background:#262626; color:#eee; border:1px solid #3a3a3a; border-radius:8px;
          padding:8px 14px; font-size:15px; cursor:pointer; min-width:44px; }
 button:hover { background:#333; }
 button.on { background:#2f6f4f; border-color:#49a377; }
 button.sugar.on { background:#8a3535; border-color:#c05050; }
 button.unsure.on { background:#6a5a20; border-color:#a08a30; }
 .hint { color:#888; font-size:12px; padding-bottom:8px; }
 #done { display:none; text-align:center; padding:40px; }
</style></head><body><div id="wrap">
 <div id="bar"><span id="prog">…</span><span id="hint">1–9,0 打分 · S 糖水 · N 不是 · U 说不好 · ← 回退</span></div>
 <div id="stage"><img id="pic" alt=""></div>
 <div class="row" id="looks"></div>
 <div class="row" id="sugar">
   <button class="sugar" data-s="yes">S 糖水</button>
   <button data-s="no">N 不是糖水</button>
   <button class="unsure" data-s="unsure">U 说不好</button>
 </div>
 <div class="hint">糖水 = 好看但空洞、一眼能归类的套路图（霓虹街、星空、雪山、动漫少女、高饱和奇幻）</div>
 <div id="done"><h2>标完了 🎉</h2><p id="sum"></p></div>
</div>
<script>
let item=null, looks=null, sugar=null, done=0, total=0, history=[];
const pic=document.getElementById('pic');
function paint(){
  document.querySelectorAll('#looks button').forEach(b=>
    b.classList.toggle('on', Number(b.dataset.n)===looks));
  document.querySelectorAll('#sugar button').forEach(b=>
    b.classList.toggle('on', b.dataset.s===sugar));
}
async function next(){
  const r=await fetch('/api/next?skip='+encodeURIComponent(item?item.key:''));
  const j=await r.json(); done=j.done; total=j.total;
  document.getElementById('prog').textContent=`${j.done} / ${j.total}`;
  if(!j.item){ showDone(j.total); return; }
  item=j.item; looks=null; sugar=null;
  pic.src='/thumbs/'+encodeURIComponent(item.key);
  paint();
}
function showDone(n){ document.getElementById('wrap').innerHTML=
  `<div id="done"><h2>标完了 🎉</h2><p>共 ${n} 张，结果已保存到 data/concept_batch/labels.json</p></div>`; }
async function submit(){
  if(!item||looks===null||sugar===null) return;
  const body={key:item.key, looks, sugar};
  history.push(body);
  await fetch('/api/label',{method:'POST',headers:{'content-type':'application/json'},
    body:JSON.stringify(body)});
  next();
}
function choose(n){ looks=n; paint(); if(sugar!==null) setTimeout(submit,120); }
function chooseSugar(s){ sugar=s; paint(); if(looks!==null) setTimeout(submit,120); }
for(let i=1;i<=10;i++){
  const b=document.createElement('button'); b.textContent=i; b.dataset.n=i;
  b.onclick=()=>choose(i); document.getElementById('looks').appendChild(b);
}
document.querySelectorAll('#sugar button').forEach(b=> b.onclick=()=>chooseSugar(b.dataset.s));
document.addEventListener('keydown',e=>{
  if(e.key>='1'&&e.key<='9'){ choose(Number(e.key)); }
  else if(e.key==='0'){ choose(10); }
  else if(e.key.toLowerCase()==='s'){ chooseSugar('yes'); }
  else if(e.key.toLowerCase()==='n'){ chooseSugar('no'); }
  else if(e.key.toLowerCase()==='u'){ chooseSugar('unsure'); }
});
next();
</script></body></html>"""


class Store:
    """Labels live on disk after every click; the tab can die at any time."""

    def __init__(self, batch: Path):
        self.batch = batch
        self.meta = json.loads((batch / "meta.json").read_text(encoding="utf-8"))
        self.items = [item for item in self.meta["items"] if self._thumb(item).exists()]
        self.labels_path = batch / "labels.json"
        self.labels = {}
        if self.labels_path.exists():
            try:
                self.labels = json.loads(self.labels_path.read_text(encoding="utf-8"))
            except ValueError:
                self.labels = {}
        self.lock = threading.Lock()

    def _thumb(self, item) -> Path:
        if item["thumb"].startswith("thumbs/"):
            return self.batch / item["thumb"]
        return self.batch / "thumbs" / f"{item['key']}.jpg"

    def next_item(self, skip: str = ""):
        with self.lock:
            for item in self.items:
                if item["key"] in self.labels or item["key"] == skip:
                    continue
                return item
            return None

    def progress(self) -> int:
        return sum(1 for item in self.items if item["key"] in self.labels)

    def save(self, key: str, looks: int, sugar: str) -> None:
        with self.lock:
            self.labels[key] = {
                "looks": int(looks),
                "sugar": sugar,
                "at": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
            self.labels_path.write_text(
                json.dumps(self.labels, ensure_ascii=False, indent=1), encoding="utf-8")


def handler_for(store: Store):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep the console quiet
            pass

        def _send(self, code, body: bytes, kind="text/html; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urlparse(self.path)
            if url.path == "/":
                return self._send(200, PAGE.encode("utf-8"))
            if url.path == "/api/next":
                skip = (url.query.split("skip=", 1) + [""])[1] if "skip=" in url.query else ""
                item = store.next_item(skip)
                payload = {"done": store.progress(), "total": len(store.items),
                           "item": item}
                return self._send(200, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                  "application/json; charset=utf-8")
            if url.path.startswith("/thumbs/"):
                key = url.path[len("/thumbs/"):]
                for item in store.items:
                    if item["key"] == key:
                        path = store._thumb(item)
                        if path.exists():
                            return self._send(200, path.read_bytes(), "image/jpeg")
                return self._send(404, b"")
            return self._send(404, b"")

        def do_POST(self):
            if urlparse(self.path).path != "/api/label":
                return self._send(404, b"")
            length = int(self.headers.get("Content-Length", "0"))
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                store.save(body["key"], body["looks"], body["sugar"])
            except Exception as exc:
                return self._send(400, str(exc).encode("utf-8"), "text/plain; charset=utf-8")
            return self._send(200, b'{"ok":true}', "application/json")

    return Handler


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=Path, default=ROOT / "data" / "concept_batch")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)

    if not (args.batch / "meta.json").exists():
        print(f"先跑 scripts/build_concept_batch.py（找不到 {args.batch / 'meta.json'}）")
        return 1
    store = Store(args.batch)
    if not store.items:
        print("批次里没有可用的缩略图")
        return 1

    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(store))
    url = f"http://127.0.0.1:{args.port}"
    print(f"{len(store.items)} 张待标（已标 {store.progress()} 张）")
    print(f"打开 {url}    Ctrl+C 结束")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已保存到", store.labels_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
