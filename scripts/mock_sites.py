#!/usr/bin/env python3
"""本地演示站点（浏览器任务自测用，不联网）。
用法: python3 scripts/mock_sites.py [port]   默认 8941
路由:
  /spa.html      纯 JS 渲染 3 条 .item（演示 JS 渲染取数）
  /actions.html  初始 3 条 + "加载更多"按钮；点击后经 /more 追加 3 条
  /more          JSON 接口，返回更多条目片段
  /p1.html..p5.html  静态页各 2 条 .item（演示会话池多页）
"""
import gzip
import json
import sys
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAGES = []
for i in range(1, 6):
    PAGES.append(f"<html><body><h1>page {i}</h1>"
                 + "".join(f'<div class="item"><span class="t">p{i}-title-{j}</span><span class="p">2025-01-0{j}</span></div>' for j in range(1, 3))
                 + "</body></html>")

SPA = """<html><body><div id="app"></div><div id="links"></div><script>
const items = [1,2,3].map(i => `<div class="item"><span class="t">spa-item-${i}</span><span class="p">2025-01-0${i}</span></div>`);
document.getElementById('app').innerHTML = items.join('');
const links = [1,2,3].map(i => `<a href="/p${i}.html">p${i}</a>`).join('');
document.getElementById('links').innerHTML = links;
</script></body></html>"""

ACTIONS = """<html><head><style>
#cookie-banner{position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,.55);
display:flex;align-items:center;justify-content:center}
#cookie-box{background:#fff;padding:24px;border-radius:8px;text-align:center}
</style></head><body>
<div id="cookie-banner"><div id="cookie-box"><p>本站使用 Cookie</p>
<button id="accept-cookie">接受</button></div></div>
<div id="list">
  <div class="item"><span class="t">initial-1</span><span class="p">2025-01-01</span></div>
  <div class="item"><span class="t">initial-2</span><span class="p">2025-01-02</span></div>
  <div class="item"><span class="t">initial-3</span><span class="p">2025-01-03</span></div>
</div>
<button id="more">加载更多</button>
<script>
document.getElementById('more').addEventListener('click', async () => {
  const r = await fetch('/more');
  const rows = await r.json();
  const list = document.getElementById('list');
  rows.forEach((t, i) => {
    const d = document.createElement('div');
    d.className = 'item';
    d.innerHTML = `<span class="t">${t}</span><span class="p">2025-02-0${i + 1}</span>`;
    list.appendChild(d);
  });
});
</script>
</body></html>"""


FLAKY = """<html><body><h1>flaky ok</h1><div class="item"><span class="t">recovered</span></div></body></html>"""
HUB2 = """<html><body><h1>hub2</h1><a href="/p1.html">p1</a><a href="/p2.html">p2</a><a href="/p3.html">p3</a></body></html>"""
_RATE = {"n": 0}
_RATE_LOCK = threading.Lock()
_PAGED = {"n": 0}
_PAGED_LOCK = threading.Lock()
_MON = {"n": 0}
_MON_LOCK = threading.Lock()
_COUNTER = {"n": 0}
_COUNTER_LOCK = threading.Lock()
_SHRINK = {"n": 0}
_SHRINK_LOCK = threading.Lock()
_CHANGE = {"n": 0}
_CHANGE_LOCK = threading.Lock()
HUB = """<html><body><h1>hub</h1>
<a href="/p1.html">p1</a><a href="/p2.html">p2</a><a href="/p3.html">p3</a>
<a href="/p4.html">p4</a><a href="/p5.html">p5</a><a href="/out.html">out</a>
</body></html>"""
FOLLOW = """<html><body><h1>follow-test</h1>
<a href="/p1.html">p1</a><a href="/p2.html">p2</a>
</body></html>"""
ROBOTS = """User-agent: *
Disallow: /p2.html
Crawl-delay: 1
"""
SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>http://127.0.0.1:{port}/p1.html</loc></url>
  <url><loc>http://127.0.0.1:{port}/p2.html</loc></url>
  <url><loc>http://127.0.0.1:{port}/p3.html</loc></url>
</urlset>"""


_FLAKY = {"count": 0}
_FLAKY_LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/spa.html":
            self._send(SPA, "text/html; charset=utf-8")
        elif path == "/actions.html":
            self._send(ACTIONS, "text/html; charset=utf-8")
        elif path == "/more":
            self._send(json.dumps(["loaded-1", "loaded-2", "loaded-3"]), "application/json; charset=utf-8")
        elif path == "/flaky":
            with _FLAKY_LOCK:
                _FLAKY["count"] += 1
                n = _FLAKY["count"]
            if n <= 2:
                self._send("boom", "text/plain; charset=utf-8", status=500)
            else:
                self._send(FLAKY, "text/html; charset=utf-8")
        elif path == "/sitemap.xml":
            self._send(SITEMAP.format(port=self.server.server_address[1]), "application/xml; charset=utf-8")
        elif path == "/robots.txt":
            self._send(ROBOTS, "text/plain; charset=utf-8")
        elif path == "/hub.html":
            self._send(HUB, "text/html; charset=utf-8")
        elif path == "/follow.html":
            self._send(FOLLOW, "text/html; charset=utf-8")
        elif path == "/api/list":
            page = int(self.path.split("page=")[1].split("&")[0]) if "page=" in self.path else 1
            all_records = [
                {"id": 1, "title": "api-item-1"}, {"id": 2, "title": "api-item-2"},
                {"id": 3, "title": "api-item-3"}, {"id": 4, "title": "api-item-4"},
            ]
            start = (page - 1) * 2
            self._send(json.dumps({"total": 4, "page": page, "records": all_records[start:start + 2]}),
                       "application/json; charset=utf-8")
        elif path == "/api/offset":
            offset = int(self.path.split("offset=")[1].split("&")[0]) if "offset=" in self.path else 0
            all_records = [{"id": i, "title": f"off-{i}"} for i in range(1, 6)]
            self._send(json.dumps({"total": 5, "offset": offset, "records": all_records[offset:offset + 2]}),
                       "application/json; charset=utf-8")
        elif path == "/api/next":
            page = int(self.path.split("page=")[1].split("&")[0]) if "page=" in self.path else 1
            if page > 2:
                self._send(json.dumps({"records": [], "next": None}), "application/json; charset=utf-8")
            else:
                recs = [{"id": page * 2 - 1, "title": f"next-{page * 2 - 1}"},
                        {"id": page * 2, "title": f"next-{page * 2}"}]
                self._send(json.dumps({"records": recs, "next": f"/api/next?page={page + 1}"}),
                           "application/json; charset=utf-8")
        elif path == "/ratelimit":
            with _RATE_LOCK:
                _RATE["n"] += 1
                n = _RATE["n"]
            if n == 1:
                self.send_response(429)
                self.send_header("Retry-After", "1")
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                body = b'<html><body><div class="item"><span class="t">after-429</span></div></body></html>'
                self._send(body, "text/html; charset=utf-8")
        elif path == "/hub2.html":
            self._send(HUB2, "text/html; charset=utf-8")
        elif path == "/hub3.html":
            self._send("<html><body><a href='/p1.html'>a</a><a href='/p1.html#x'>a#</a><a href='/p1.html?utm=1'>a?</a></body></html>", "text/html; charset=utf-8")
        elif path == "/site/level1.html":
            self._send("<html><body><a href='/site/level2/a.html'>a</a><a href='/site/level2/b.html'>b</a></body></html>", "text/html; charset=utf-8")
        elif path in ("/site/level2/a.html", "/site/level2/b.html"):
            self._send("<html><body><div class='item'><span class='t'>L2</span></div><a href='/site/level3/x.html'>deep</a></body></html>", "text/html; charset=utf-8")
        elif path == "/site/level3/x.html":
            self._send("<html><body><div class='item'><span class='t'>L3</span></div></body></html>", "text/html; charset=utf-8")
        elif path == "/article.html":
            self._send("<html><head><title>测试文章</title></head><body><nav>导航垃圾</nav><article><h1>标题一</h1><p>这是正文第一段，包含足够长度的文字内容用于抽取验证。</p><p>这是正文第二段，再次提供足够长度的文字内容用于抽取验证。</p></article><footer>页脚垃圾</footer></body></html>", "text/html; charset=utf-8")
        elif path == "/hubdocs.html":
            self._send("<html><body><a href='/docs/a.html'>doc</a><a href='/blog/b.html'>blog</a></body></html>", "text/html; charset=utf-8")
        elif path == "/docs/a.html":
            self._send("<html><body><div class='item'><span class='t'>doc-a</span></div></body></html>", "text/html; charset=utf-8")
        elif path == "/blog/b.html":
            self._send("<html><body><div class='item'><span class='t'>blog-b</span></div></body></html>", "text/html; charset=utf-8")
        elif path == "/search":
            import urllib.parse as _up
            q = _up.unquote(self.path.split("q=")[1].split("&")[0]) if "q=" in self.path else ""
            self._send(json.dumps({"query": q, "records": [{"id": 1, "title": "hit-" + q}]}), "application/json; charset=utf-8")
        elif path == "/signed":
            if self.headers.get("X-Sign") == "ok":
                self._send("<html><body><div class='item'><span class='t'>signed-ok</span></div></body></html>", "text/html; charset=utf-8")
            else:
                self._send("forbidden", "text/plain; charset=utf-8", status=403)
        elif path == "/paginated.html":
            self._send("<html><body><div class='item'><span class='t'>pg-1</span></div><div class='item'><span class='t'>pg-2</span></div><a id='next' href='/paginated2.html'>next</a></body></html>", "text/html; charset=utf-8")
        elif path == "/paginated2.html":
            self._send("<html><body><div class='item'><span class='t'>pg-3</span></div><div class='item'><span class='t'>pg-4</span></div></body></html>", "text/html; charset=utf-8")
        elif path == "/list.html":
            self._send("<html><body><div class='item'><span class='t'>dl-1</span><a class='u' href='/detail/1.html'>d</a><a class='f' href='/file.bin'>f</a></div><div class='item'><span class='t'>dl-2</span><a class='u' href='/detail/1.html'>d</a><a class='f' href='/file.bin'>f</a></div></body></html>", "text/html; charset=utf-8")
        elif path == "/detail/1.html":
            self._send("<html><body><h1>详情内容-1</h1></body></html>", "text/html; charset=utf-8")
        elif path == "/file.bin":
            self._send(b"HELLO-BINARY-FILE-12345", "application/octet-stream")
        elif path == "/monitor.html":
            with _MON_LOCK:
                _MON["n"] += 1
                n = _MON["n"]
            items = "".join(f"<div class='item'><span class='t'>mon-{i}</span></div>" for i in range(1, n + 1))
            self._send(f"<html><body>{items}</body></html>", "text/html; charset=utf-8")
        elif path.startswith("/slow/"):
            import time as _t
            _t.sleep(0.5)
            n = path.split("/")[-1]
            self._send(f"<html><body><div class='item'><span class='t'>slow-{n}</span></div></body></html>",
                       "text/html; charset=utf-8")
        elif path == "/api/post":
            n = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(n).decode() if n else ""
            self._send(json.dumps({"received": body, "records": [{"title": "post-ok"}]}),
                       "application/json; charset=utf-8")
        elif path == "/api/odd":
            page = int(self.path.split("page=")[1].split("&")[0]) if "page=" in self.path else 1
            recs = [{"id": 1, "title": "odd-1"}, {"id": 2, "title": "odd-2"}] if page == 1 else [{"id": 3, "title": "odd-3"}]
            self._send(json.dumps({"total": 3, "records": recs}), "application/json; charset=utf-8")
        elif path == "/dup.html":
            self._send("<html><body><div class='item'><span class='t'>same</span></div><div class='item'><span class='t'>same</span></div></body></html>", "text/html; charset=utf-8")
        elif path == "/cookies":
            if self.headers.get("Cookie", "").find("us_cookie=1") >= 0:
                self._send("<html><body><div class='item'><span class='t'>cookie-ok</span></div></body></html>", "text/html; charset=utf-8")
            else:
                self._send("no cookie", "text/plain; charset=utf-8", status=403)
        elif path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/p1.html")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif path == "/gzip":
            body = b"<html><body><div class='item'><span class='t'>gzip-ok</span></div></body></html>"
            gz = gzip.compress(body)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(gz)))
            self.end_headers()
            self.wfile.write(gz)
        elif path == "/gbk.html":
            body = "<html><body><div class='item'><span class='t'>中文标题</span></div></body></html>".encode("gbk")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=gbk")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/setcookie.html":
            self._send("<html><body><script>document.cookie='us_session=1; path=/';</script><div class='item'><span class='t'>set-ok</span></div></body></html>", "text/html; charset=utf-8")
        elif path == "/needcookie.html":
            if self.headers.get("Cookie", "").find("us_session=1") >= 0:
                self._send("<html><body><div class='item'><span class='t'>need-ok</span></div></body></html>", "text/html; charset=utf-8")
            else:
                self._send("no session", "text/plain; charset=utf-8", status=403)
        elif path == "/dual.html":
            self._send("<html><body><span class='t'>first-parser</span><span class='s'>second-parser</span></body></html>", "text/html; charset=utf-8")
        elif path == "/api/list2":
            import urllib.parse as _up
            qs = _up.parse_qs(_up.urlsplit(self.path).query)
            typ = qs.get("type", ["0"])[0]
            page = int(qs.get("page", ["1"])[0])
            recs = [{"id": page * 2 - 1, "title": "t2-" + str(page * 2 - 1), "type": typ},
                    {"id": page * 2, "title": "t2-" + str(page * 2), "type": typ}]
            self._send(json.dumps({"total": 4, "type": typ, "records": recs}), "application/json; charset=utf-8")
        elif path == "/ratelimit-date":
            import datetime as _dt, email.utils as _eu
            n = int(self.headers.get("X-N", "0"))
            if n == 0:
                self.send_response(429)
                self.send_header("Retry-After", _eu.formatdate(timeval=time.time() + 1, usegmt=True))
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self._send("<html><body><div class='item'><span class='t'>after-date</span></div></body></html>", "text/html; charset=utf-8")
        elif path == "/form.html":
            self._send("""<html><body><input id='q' name='q' onkeydown='if(event.key==="Enter"){location.href="/formresult?q="+this.value}'><button id='go' onclick='location.href="/formresult?q="+document.getElementById("q").value'>go</button></body></html>""", "text/html; charset=utf-8")
        elif path == "/formresult":
            import urllib.parse as _up
            q = _up.unquote(self.path.split("q=")[1].split("&")[0]) if "q=" in self.path else ""
            self._send("<html><body><div class='item'><span class='t'>form-" + q + "</span></div></body></html>", "text/html; charset=utf-8")
        elif path == "/select.html":
            self._send("""<html><body><select id='s'></select><div id='out'></div><script>
var opts = [1, 2], sel = document.getElementById('s');
opts.forEach(function (v) { var o = document.createElement('option'); o.value = v; o.text = v === 1 ? 'One' : 'Two'; sel.appendChild(o); });
sel.onchange = function () { document.getElementById('out').innerHTML = "<div class='item'><span class='t'>sel-" + sel.value + "</span></div>"; };
</script></body></html>""", "text/html; charset=utf-8")
        elif path == "/gbkmeta.html":
            body = "<html><head><meta charset='gbk'></head><body><div class='item'><span class='t'>元标签中文</span></div></body></html>".encode("gbk")
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/multitable.html":
            self._send("<html><body><table><tr><th>A</th></tr><tr><td>a1</td></tr></table><table><tr><th>B</th></tr><tr><td>b1</td></tr></table></body></html>", "text/html; charset=utf-8")
        elif path == "/htmlp1.html":
            self._send("<html><body><div class='item'><span class='t'>hp-1</span></div><div class='item'><span class='t'>hp-2</span></div><a class='next' href='/paginated2.html'>next</a></body></html>", "text/html; charset=utf-8")
        elif path == "/counter.html":
            with _COUNTER_LOCK:
                _COUNTER["n"] += 1
                n = _COUNTER["n"]
            self._send(f"<html><body><div class='item'><span class='t'>cnt-{n}</span></div></body></html>", "text/html; charset=utf-8")
        elif path == "/cycle1.html":
            self._send("<html><body><a href='/cycle2.html'>c2</a><div class='item'><span class='t'>c1</span></div></body></html>", "text/html; charset=utf-8")
        elif path == "/cycle2.html":
            self._send("<html><body><a href='/cycle1.html'>c1</a><div class='item'><span class='t'>c2</span></div></body></html>", "text/html; charset=utf-8")
        elif path == "/hdrcheck":
            if self.headers.get("X-Plug") == "1":
                self._send("<html><body><div class='item'><span class='t'>hdr-ok</span></div></body></html>", "text/html; charset=utf-8")
            else:
                self._send("no header", "text/plain; charset=utf-8", status=403)
        elif path == "/shrink.html":
            with _SHRINK_LOCK:
                _SHRINK["n"] += 1
                n = _SHRINK["n"]
            items = "".join(f"<div class='item'><span class='t'>sh-{i}</span></div>" for i in range(1, 3 if n == 1 else 2))
            self._send(f"<html><body>{items}</body></html>", "text/html; charset=utf-8")
        elif path == "/api/big":
            recs = [{"id": i, "title": "big-" + str(i)} for i in range(1, 2001)]
            self._send(json.dumps({"records": recs}), "application/json; charset=utf-8")
        elif path == "/hdrcheck2":
            if self.headers.get("X-R") == "1":
                self._send("<html><body><div class='item'><span class='t'>req-hdr-ok</span></div></body></html>", "text/html; charset=utf-8")
            else:
                self._send("no header", "text/plain; charset=utf-8", status=403)
        elif path == "/static.html":
            self._send("<html><body><div class='item'><span class='t'>static-1</span></div><div class='item'><span class='t'>static-2</span></div></body></html>", "text/html; charset=utf-8")
        elif path == "/change.html":
            with _CHANGE_LOCK:
                _CHANGE["n"] += 1
                n = _CHANGE["n"]
            val = "a" if n == 1 else "b"
            self._send(f"<html><body><div class='item'><span class='t'>k</span><span class='p'>{val}</span></div></body></html>", "text/html; charset=utf-8")
        elif path == "/table.html":
            self._send("<html><body><table><tr><th>Name</th><th>Age</th></tr><tr><td>Alice</td><td>30</td></tr><tr><td>Bob</td><td>25</td></tr></table></body></html>", "text/html; charset=utf-8")
        elif path == "/api/nested":
            self._send(json.dumps({"data": {"list": [
                {"id": 1, "items": [{"x": "a"}]}, {"id": 2, "items": [{"x": "b"}]}]}}),
                       "application/json; charset=utf-8")
        elif path == "/api/emptypage":
            page = int(self.path.split("page=")[1].split("&")[0]) if "page=" in self.path else 1
            recs = [{"id": 1, "title": "ep-1"}, {"id": 2, "title": "ep-2"}] if page == 1 else []
            self._send(json.dumps({"records": recs}), "application/json; charset=utf-8")
        elif path == "/api/paged429":
            with _PAGED_LOCK:
                _PAGED["n"] += 1
                n = _PAGED["n"]
            page = int(self.path.split("page=")[1].split("&")[0]) if "page=" in self.path else 1
            if page == 2 and n == 2:
                self.send_response(429)
                self.send_header("Retry-After", "1")
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                recs = [{"id": page * 2 - 1, "title": "p429-" + str(page * 2 - 1)},
                        {"id": page * 2, "title": "p429-" + str(page * 2)}]
                self._send(json.dumps({"total": 6, "records": recs}), "application/json; charset=utf-8")
        elif path in (f"/p{i}.html" for i in range(1, 6)):
            self._send(PAGES[int(path[2]) - 1], "text/html; charset=utf-8")
        else:
            self._send("404", "text/plain; charset=utf-8", status=404)

    def do_POST(self):
        if self.path.split("?")[0] == "/api/post":
            n = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(n).decode() if n else ""
            self._send(json.dumps({"received": body, "records": [{"title": "post-ok"}]}),
                       "application/json; charset=utf-8")
        else:
            self._send("404", "text/plain; charset=utf-8", status=404)

    def _send(self, body, ctype, status=200):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8941
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"mock site on http://127.0.0.1:{port}", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
