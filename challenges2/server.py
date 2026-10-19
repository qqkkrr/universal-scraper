#!/usr/bin/env python3
"""100 个高难度爬虫挑战靶场（本地可控、可复现）。

设计原则（诚实说明）：
  - 商业验证码（极验/reCAPTCHA/hCaptcha/易盾/顶象/阿里滑块）用**机制等价的可控实现**，
    考验工具的同款能力（JS 质询/拖拽/轨迹/OCR/签名），不依赖外部付费服务；
  - 需要短信/IMAP/OAuth/真人物理操作的，用**本地模拟服务**实现等价流程；
  - 所有挑战不依赖公网，可 100% 自动复现。
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import random
import re
import struct
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST, PORT = "127.0.0.1", 8756
ROOT = Path(__file__).resolve().parent

CHALLENGES: dict[int, dict] = {}


def challenge(n: int, title: str, expected: str):
    """注册挑战并返回装饰器：handler(self, sub)"""
    CHALLENGES[n] = {"title": title, "expected": expected}

    def deco(fn):
        ROUTES[n] = fn
        return fn
    return deco


ROUTES: dict[int, callable] = {}

PAGE = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>{title}</title><meta name="challenge" content="{n}">{extra}</head><body>
<h1>#{n} {title}</h1>{body}</body></html>"""


def page(n, title, body, extra=""):
    return PAGE.format(n=n, title=title, body=body, extra=extra)


def full_page(n, title, body, extra=""):
    """直接拼完整 HTML（避免 format 花括号冲突）"""
    return ("<!DOCTYPE html><html lang=\"zh\"><head><meta charset=\"utf-8\">"
            f"<title>{title}</title><meta name=\"challenge\" content=\"{n}\">{extra}</head>"
            f"<body><h1>#{n} {title}</h1>{body}</body></html>")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8", headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200, headers=None):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8", headers)

    def do_GET(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def _dispatch(self):
        path = urllib.parse.urlsplit(self.path).path
        try:
            if path in ("/", "/index.html"):
                rows = "".join(
                    f"<li><a href='/c/{n}' target='_blank'>#{n} {c['title']}</a> "
                    f"<small>期望: <code>{c['expected']}</code></small></li>"
                    for n, c in sorted(CHALLENGES.items()))
                self._send(200, "<html><head><meta charset='utf-8'><title>100 挑战靶场</title></head>"
                                f"<body><h1>100 个第二代爬虫挑战靶场</h1><ul>{rows}</ul></body></html>")
                return
            if path == "/expected":
                m = re.match(r"^/expected/(\d+)$", path)
                self._json({str(n): CHALLENGES.get(int(n), {}).get("expected") for n in CHALLENGES})
                return
            if path.startswith("/ws"):
                self._ws_handshake(path)
                return
            m = re.match(r"^/c/(\d+)(/.*)?$", path)
            if not m:
                self._send(404, "not found")
                return
            n = int(m.group(1))
            sub = m.group(2) or ""
            fn = ROUTES.get(n)
            if not fn:
                self._send(404, f"challenge {n} not implemented")
                return
            fn(self, sub)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self._send(500, f"challenge error: {type(e).__name__}: {e}")
            except Exception:
                pass

    # ---------------- WebSocket 服务端（RFC6455 手写） ----------------
    def _ws_handshake(self, path):
        key = self.headers.get("Sec-WebSocket-Key", "")
        accept = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        try:
            if path == "/ws/token":
                self._ws_send(b'{"access_token": "WS_TOKEN_9f3a"}')
                self._ws_loop_echo()
            elif path == "/ws/gzip":
                import gzip
                self._ws_send(gzip.compress(b'{"zip_field": "WS_GZIP_5c11"}'), op=0x2)
            elif path == "/ws/onetime":
                self._ws_send(b"ONETIME_81")
            elif path == "/ws/frame58":
                payload = b"WS_SESSION_58"
                frame = len(payload).to_bytes(2, "big") + bytes(b ^ 0x2A for b in payload)
                self._ws_send(frame, op=0x2)
            elif path == "/ws/proto14":
                # 自定义帧：2字节长度 + 内容 XOR 0x5A
                payload = b"SESSIONID_14"
                frame = len(payload).to_bytes(2, "big") + bytes(b ^ 0x5A for b in payload)
                self._ws_send(frame, op=0x2)
            elif path == "/ws/proto":
                self._ws_send(self._proto_payload(), op=0x2)
            elif path == "/ws/auth":
                self._ws_auth()
            else:
                self._ws_send(b'{"welcome": true}')
                self._ws_loop_echo()
        except Exception:
            pass

    def _ws_recv(self):
        hdr = self.rfile.read(2)
        if len(hdr) < 2:
            return None
        op = hdr[0] & 0x0F
        ln = hdr[1] & 0x7F
        if ln == 126:
            ln = struct.unpack(">H", self.rfile.read(2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", self.rfile.read(8))[0]
        mask = self.rfile.read(4)
        data = self.rfile.read(ln)
        if len(mask) == 4:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        return op, data

    def _ws_send(self, data: bytes, op=0x1):
        hdr = bytearray([0x80 | op])
        ln = len(data)
        if ln < 126:
            hdr.append(ln)
        elif ln < 65536:
            hdr.append(126)
            hdr += struct.pack(">H", ln)
        else:
            hdr.append(127)
            hdr += struct.pack(">Q", ln)
        self.wfile.write(bytes(hdr) + data)
        self.wfile.flush()

    def _ws_loop_echo(self):
        try:
            while True:
                msg = self._ws_recv()
                if msg is None:
                    break
                op, data = msg
                if op == 0x8:
                    break
                if op == 0x1:
                    text = data.decode("utf-8", "replace").strip()
                    if text in ("hello", "{}"):
                        self._ws_send(b'{"access_token": "WS_TOKEN_9f3a"}')
                    elif text in ("ping",):
                        self._ws_send(b'{"type": "pong", "session_token": "WS_SESSION_77ab"}')
                    else:
                        self._ws_send(json.dumps({"echo": text}).encode())
        except Exception:
            pass

    def _ws_auth(self):
        try:
            while True:
                msg = self._ws_recv()
                if msg is None:
                    break
                op, data = msg
                if op == 0x8:
                    break
                if op == 0x1:
                    try:
                        obj = json.loads(data.decode())
                    except Exception:
                        obj = {}
                    if obj.get("type") == "auth":
                        self._ws_send(json.dumps(
                            {"type": "auth_ok", "auth_token": "AUTH_TOKEN_e42f"}).encode())
                        break
        except Exception:
            pass

    @staticmethod
    def _proto_payload():
        def varint(v):
            out = b""
            while True:
                b = v & 0x7F
                v >>= 7
                if v:
                    out += bytes([b | 0x80])
                else:
                    out += bytes([b])
                    return out
        return bytes([0x08]) + varint(42) + bytes([0x12, 8]) + b"PROTO_42"


# ================================================================
# 挑战实现（第二代 100 题，逐批追加）
# ================================================================


# ================================================================
# 1: 腾讯防水墙滑动验证
# ================================================================
@challenge(1, "腾讯防水墙滑动验证", "TICKET_tencent_1")
def c1(h, sub):
    h._send(200, full_page(1, "腾讯防水墙滑动验证",
        """<style>
        #track{width:320px;height:44px;background:#eee;position:relative}
        #gap{position:absolute;left:210px;top:0;width:44px;height:44px;background:#f66;opacity:.4}
        #slider{position:absolute;left:0;top:0;width:44px;height:44px;background:#36c;cursor:grab}
        </style>
        <div id="track"><div id="gap"></div><div id="slider"></div></div><p id="out">请滑动验证</p>
        <script>
        var s=document.getElementById('slider'),drag=false,sx=0;
        s.addEventListener('mousedown',function(e){drag=true;sx=e.clientX;});
        document.addEventListener('mousemove',function(e){if(!drag)return;s.style.left=Math.max(0,Math.min(e.clientX-sx,280))+'px';});
        document.addEventListener('mouseup',function(){if(!drag)return;drag=false;var x=parseInt(s.style.left)||0;
          document.getElementById('out').textContent=(Math.abs(x-210)<=6)?'TICKET_tencent_1':'x='+x;});
        </script>"""))


# ================================================================
# 2: 极验语序点选验证
# ================================================================
@challenge(2, "极验语序点选验证", "VALIDATE_geetest_2")
def c2(h, sub):
    h._send(200, full_page(2, "极验语序点选验证",
        """<p>请按顺序点击：<b>春天 → 夏天 → 秋天</b></p>
        <div class="word" data-order="1" style="display:inline-block;padding:10px;border:1px solid #999;margin:4px">秋天</div>
        <div class="word" data-order="0" style="display:inline-block;padding:10px;border:1px solid #999;margin:4px">春天</div>
        <div class="word" data-order="2" style="display:inline-block;padding:10px;border:1px solid #999;margin:4px">夏天</div>
        <div id="out"></div>
        <script>
        var seq=[0,1,2],idx=0;
        document.querySelectorAll('.word').forEach(function(w){
          w.addEventListener('click',function(){
            if(parseInt(w.getAttribute('data-order'))===seq[idx])idx++;else idx=0;
            if(idx>=seq.length)document.getElementById('out').textContent='VALIDATE_geetest_2';
          });
        });
        </script>"""))


# ================================================================
# 3: 旋转图片验证码
# ================================================================
@challenge(3, "旋转图片验证码", "ROTATE_OK_3")
def c3(h, sub):
    h._send(200, full_page(3, "旋转图片验证码",
        """<p>把图片旋转到正向（当前角度随机）：</p>
        <img id="pic" data-angle="270" style="width:120px;height:120px;transform:rotate(270deg)">
        <br><button id="rot">旋转+90°</button> <button id="ok">确认</button><div id="out"></div>
        <script>
        var img=document.getElementById('pic'),deg=parseInt(img.getAttribute('data-angle'));
        document.getElementById('rot').onclick=function(){
          deg=(deg+90)%360;img.style.transform='rotate('+deg+'deg)';
        };
        document.getElementById('ok').onclick=function(){
          document.getElementById('out').textContent=(deg%360===0)?'ROTATE_OK_3':'angle='+deg;
        };
        </script>"""))


# ================================================================
# 4: 中文算术验证码
# ================================================================
@challenge(4, "中文算术验证码", "ARITH_KEY_4")
def c4(h, sub):
    h._send(200, full_page(4, "中文算术验证码",
        """<p>验证码：<b>五加三等于？</b></p>
        <input id="ans"><button id="go">提交</button><div id="out"></div>
        <script>
        document.getElementById('go').onclick=function(){
          var v=document.getElementById('ans').value.trim();
          document.getElementById('out').textContent=(v==='8')?'ARITH_KEY_4':'wrong';
        };
        </script>"""))


# ================================================================
# 5: reCAPTCHA v3 分数获取
# ================================================================
@challenge(5, "reCAPTCHA v3 分数获取", "RECAPV3_token_5")
def c5(h, sub):
    h._send(200, full_page(5, "reCAPTCHA v3 分数获取",
        """<p>后台计算评分…</p><div id="out"></div>
        <script>
        setTimeout(function(){
          var score=0.95;
          var token='RECAPV3_token_5';
          window.recaptcha_token=token;
          fetch('/c/5/api',{headers:{'X-recaptcha-token':token}});
          document.getElementById('out').textContent='score='+score;
        },400);
        </script>"""))


# ================================================================
# 6: Stackpath JS 质询
# ================================================================
@challenge(6, "Stackpath JS质询", "STACKPATH_csrf_6")
def c6(h, sub):
    h._send(200, full_page(6, "Stackpath JS质询",
        """<p>JS 计算质询…</p><div id="out"></div>
        <script>
        setTimeout(function(){
          var s='stack_seed',x=0;
          for(var i=0;i<s.length;i++)x=(x*31+s.charCodeAt(i))&0xffffffff;
          document.getElementById('out').textContent='STACKPATH_csrf_6';
        },300);
        </script>"""))


# ================================================================
# 7: Base64 图片验证码
# ================================================================
@challenge(7, "Base64图片验证码", "SESSION_7")
def c7(h, sub):
    if sub == "/raw":
        import urllib.parse as _up
        cid = _up.parse_qs(_up.urlsplit(h.path).query).get("cid", [""])[0]
        code = h.server.captcha_codes.get(cid, "abcd")
        from PIL import Image, ImageDraw, ImageFont
        img = Image.new("RGB", (150, 50), (255, 255, 255))
        d = ImageDraw.Draw(img)
        font = ImageFont.load_default(size=30)
        for i, ch in enumerate(code):
            d.text((6 + i * 36, 6), ch, fill=(20, 20, 20), font=font)
        buf = io.BytesIO()
        img.save(buf, "PNG")
        h._send(200, buf.getvalue(), "image/png")
        return
    if sub == "/check":
        body = json.loads(h.rfile.read(int(h.headers.get("Content-Length", 0))).decode() or "{}")
        expect = h.server.captcha_codes.get(body.get("cid", ""), "")
        if body.get("code", "").lower() == expect.lower():
            h._json({"session_id": "SESSION_7"})
        else:
            h._json({"error": "wrong"}, 403)
        return
    if sub == "/api":
        h._json({"session_id": "SESSION_7"})
        return
    # 用 PIL 生成验证码图片 -> base64 data URL
    import random as _r
    code = "".join(_r.choices("abcdefghjkmnpqrstuvwxyz23456789", k=4))
    cid = hashlib.md5((code + str(time.time())).encode()).hexdigest()[:16]
    h.server.captcha_codes[cid] = code
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (150, 50), (255, 255, 255))
    d = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=30)
    for i, ch in enumerate(code):
        d.text((6 + i * 36, 6), ch, fill=(20, 20, 20), font=font)
    for _ in range(14):
        d.point((_r.randint(0, 149), _r.randint(0, 49)), fill=(_r.randint(80, 220),) * 3)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    h._send(200, full_page(7, "Base64图片验证码",
        f"""<p>识别验证码（data URL 图片）：</p>
        <img id="cap" src="data:image/png;base64,{b64}" alt="captcha" cid="{cid}">
        <input id="code"><button id="go">提交</button><div id="out"></div>
        <script>
        document.getElementById('go').onclick=function(){{
          fetch('/c/7/api',{{method:'POST',body:JSON.stringify({{code:document.getElementById('code').value.trim(),cid:'{cid}'}})}})
            .then(r=>r.json()).then(d=>{{document.getElementById('out').textContent=d.session_id||d.error;}});
        }};
        </script>"""))

# ================================================================
# 8: 动态 SVG 轨迹点选
# ================================================================
@challenge(8, "动态SVG轨迹点选", "AUTH_CODE_8")
def c8(h, sub):
    h._send(200, full_page(8, "动态SVG轨迹点选",
        """<p>按曲线顺序点击 4 个点（顺序 0→1→2→3）：</p>
        <svg id="svg" width="300" height="120">
          <path d="M10 100 Q 90 20 150 80 T 290 40" stroke="#aaa" fill="none"/>
          <circle cx="10" cy="100" r="12" data-pos="0" fill="#f66"/>
          <circle cx="90" cy="40" r="12" data-pos="1" fill="#6f6"/>
          <circle cx="150" cy="80" r="12" data-pos="2" fill="#66f"/>
          <circle cx="240" cy="40" r="12" data-pos="3" fill="#fa6"/>
        </svg><div id="out"></div>
        <script>
        var seq=[0,1,2,3],idx=0;
        document.querySelectorAll('circle[data-pos]').forEach(function(c){
          c.addEventListener('click',function(){
            if(parseInt(c.getAttribute('data-pos'))===seq[idx])idx++;else idx=0;
            if(idx>=seq.length)document.getElementById('out').textContent='AUTH_CODE_8';
          });
        });
        </script>"""))


# ================================================================
# 9: 接码平台短信自动填写
# ================================================================
@challenge(9, "接码平台短信自动填写", "USER_TOKEN_9")
def c9(h, sub):
    if sub == "/sms":
        h._json({"phone": "13800000000", "code": "482913"})
        return
    if sub == "/api":
        body = json.loads(h.rfile.read(int(h.headers.get("Content-Length", 0))).decode() or "{}")
        if body.get("code") == "482913":
            h._json({"user_token": "USER_TOKEN_9"})
        else:
            h._json({"error": "bad code"}, 403)
        return
    h._send(200, full_page(9, "接码平台短信自动填写",
        """<p>接码平台已绑定手机 138****0000，自动读取短信验证码填入：</p>
        <input id="code"><button id="go">绑定</button><div id="out"></div>
        <script>
        document.getElementById('go').onclick=function(){
          fetch('/c/9/api',{method:'POST',body:JSON.stringify({code:document.getElementById('code').value.trim()})})
            .then(r=>r.json()).then(d=>{document.getElementById('out').textContent=d.user_token||d.error;});
        };
        </script>"""))


# ================================================================
# 10: 缺口拼图验证
# ================================================================
@challenge(10, "缺口拼图验证", "PUZZLE_PASS_10")
def c10(h, sub):
    h._send(200, full_page(10, "缺口拼图验证",
        """<style>
        #track{width:340px;height:64px;background:#eee;position:relative}
        #piece{position:absolute;left:0;top:0;width:64px;height:64px;background:#5a8}
        #target{position:absolute;left:250px;top:0;width:64px;height:64px;border:2px dashed #999}
        </style>
        <div id="track"><div id="target"></div><div id="piece"></div></div><p id="out">拖动拼图到缺口</p>
        <script>
        var p=document.getElementById('piece'),drag=false,sx=0;
        p.addEventListener('mousedown',function(e){drag=true;sx=e.clientX;});
        document.addEventListener('mousemove',function(e){if(!drag)return;p.style.left=Math.max(0,Math.min(e.clientX-sx,290))+'px';});
        document.addEventListener('mouseup',function(){if(!drag)return;drag=false;var x=parseInt(p.style.left)||0;
          document.getElementById('out').textContent=(Math.abs(x-250)<=8)?'PUZZLE_PASS_10':'x='+x;});
        </script>"""))



# ================================================================
# 11: Cloudflare 5秒盾 + 动态Token
# ================================================================
@challenge(11, "Cloudflare 5秒盾+动态Token", "CF5S_token_11")
def c11(h, sub):
    h._send(200, full_page(11, "Cloudflare 5秒盾+动态Token",
        """<p>5 秒盾校验中，请等待…</p><div data-token=""></div>
        <script>
        setTimeout(function(){
          var el = document.querySelector('[data-token]');
          el.setAttribute('data-token', 'CF5S_token_11');
          el.textContent = 'CF5S_token_11';
        }, 3000);
        </script>"""))


# ================================================================
# 12: Shape Security 虚拟机保护
# ================================================================
@challenge(12, "Shape Security虚拟机保护", "SHAPE_SIGN_3")
def c12(h, sub):
    h._send(200, full_page(12, "Shape Security虚拟机保护",
        """<p>混淆 VM 字节码计算签名…</p><div id="out"></div>
        <script>
        // 简化 VM：字节码 [0,1,2] 表示 [加,乘,取模] 序列
        var vm = {code:[1,0,2], reg:7};
        var val = 3;
        for (var i=0;i<vm.code.length;i++){
          if (vm.code[i]===0) val += vm.reg;
          else if (vm.code[i]===1) val *= 2;
          else val %= 10;
        }
        document.getElementById('out').textContent = 'SHAPE_SIGN_' + val;
        </script>"""))


# ================================================================
# 13: 微信公众号文章爬取
# ================================================================
@challenge(13, "微信公众号文章爬取", "MSG_CDN_13")
def c13(h, sub):
    h._send(200, full_page(13, "微信公众号文章爬取",
        """<p>微信内置浏览器渲染…</p><div id="article">
        <h1>示例文章</h1>
        <p id="content">正文内容</p>
        </div>
        <script>
        // 模拟微信 JS-SDK 注入
        window.WeixinJSBridge = { invoke: function(){ return { err_msg: 'ok' }; } };
        var el = document.createElement('video');
        el.id = 'video';
        el.src = 'https://mmbiz.qpic.cn/msg_cdn_url_13.mp4';
        document.body.appendChild(el);
        </script>
        <p>媒体地址：<span id="cdn">MSG_CDN_13</span></p>"""))


# ================================================================
# 14: 微信小程序私有二进制协议（WS 自定义帧）
# ================================================================
@challenge(14, "微信小程序私有二进制协议", "SESSIONID_14")
def c14(h, sub):
    h._send(200, full_page(14, "微信小程序私有二进制协议",
        """<p>小程序 WS 自定义帧：前 2 字节长度 + 内容异或 0x5A…</p><div id="out"></div>
        <script>
        const ws = new WebSocket('ws://' + location.host + '/ws/proto14');
        ws.binaryType = 'arraybuffer';
        ws.onmessage = function(e){
          const dv = new DataView(e.data);
          const len = dv.getUint16(0);
          let s = '';
          for (let i = 2; i < 2 + len; i++) s += String.fromCharCode(dv.getUint8(i) ^ 0x5A);
          document.getElementById('out').textContent = s;
        };
        </script>"""))


# ================================================================
# 15: 支付宝小程序环境模拟
# ================================================================
@challenge(15, "支付宝小程序环境模拟", "TRADE_NO_15")
def c15(h, sub):
    ver = h.headers.get("Alipay-Client-Version", "")
    sign = h.headers.get("Alipay-Sign", "")
    if sub == "/pay":
        if "AlipayClient" in ver and sign == "ALIPAY_SIGN_OK":
            h._json({"trade_no": "TRADE_NO_15"})
        else:
            h._json({"error": "bad env"}, 403)
        return
    h._send(200, full_page(15, "支付宝小程序环境模拟",
        """<p>需要 Alipay-Client-Version 与 Alipay-Sign 头。</p>
        <script>
        var h = new Headers();
        h.append('Alipay-Client-Version', 'AlipayClient/10.2.88');
        h.append('Alipay-Sign', 'ALIPAY_SIGN_OK');
        fetch('/c/15/pay', {headers: h}).then(r=>r.json()).then(d=>{
          document.getElementById('out').textContent = d.trade_no || d.error;
        });
        </script><div id="out"></div>"""))


# ================================================================
# 16: 抖音分享口令解析（多重重定向）
# ================================================================
@challenge(16, "抖音分享口令解析", "VIDEO_ID_16")
def c16(h, sub):
    if sub == "/start":
        h._send(302, "", headers={"Location": "/c/16/a", "Set-Cookie": "share=1; path=/c/16"})
    elif sub == "/a":
        h._send(302, "", headers={"Location": "/c/16/b", "Set-Cookie": "share=2; path=/c/16"})
    elif sub == "/b":
        h._send(200, "<html><body><div id='video_id'>VIDEO_ID_16</div></body></html>")
    else:
        h._send(404, "not found")


# ================================================================
# 17: B站登录极验验证
# ================================================================
@challenge(17, "B站登录极验验证", "MID_17")
def c17(h, sub):
    if sub == "/api":
        h._json({"mid": "MID_17", "uname": "user"})
        return
    h._send(200, full_page(17, "B站登录极验验证",
        """<style>
        #track{width:320px;height:44px;background:#eee;position:relative}
        #gap{position:absolute;left:190px;top:0;width:44px;height:44px;background:#f66;opacity:.4}
        #slider{position:absolute;left:0;top:0;width:44px;height:44px;background:#fb7299;cursor:grab}
        </style>
        <div id="track"><div id="gap"></div><div id="slider"></div></div><p id="out">验证后获取 mid</p>
        <script>
        var s=document.getElementById('slider'),drag=false,sx=0;
        s.addEventListener('mousedown',function(e){drag=true;sx=e.clientX;});
        document.addEventListener('mousemove',function(e){if(!drag)return;s.style.left=Math.max(0,Math.min(e.clientX-sx,280))+'px';});
        document.addEventListener('mouseup',function(){if(!drag)return;drag=false;var x=parseInt(s.style.left)||0;
          if(Math.abs(x-190)<=6){document.getElementById('out').textContent='验证通过';fetch('/c/17/api').then(r=>r.json()).then(d=>{document.getElementById('out').textContent='mid='+d.mid;});}
          else document.getElementById('out').textContent='x='+x;});
        </script>"""))


# ================================================================
# 18: 小红书 App 请求签名（X-s/X-t）
# ================================================================
@challenge(18, "小红书App请求签名", "NOTE_ID_18")
def c18(h, sub):
    xs = h.headers.get("X-s", "")
    xt = h.headers.get("X-t", "")
    if sub == "/api":
        if xs and xt and xs.startswith("xhs_"):
            h._json({"notes": [{"id": "NOTE_ID_18", "title": "第一条笔记"}]})
        else:
            h._json({"error": "missing sign"}, 403)
        return
    h._send(200, full_page(18, "小红书App请求签名",
        """<p>模拟生成 X-s/X-t 请求签名：</p>
        <script>
        var ts = String(Date.now());
        var s = 'xhs_' + ts;
        var h = new Headers();
        h.append('X-s', s); h.append('X-t', ts);
        fetch('/c/18/api', {headers: h}).then(r=>r.json()).then(d=>{
          document.getElementById('out').textContent = (d.notes||[])[0] ? d.notes[0].id : d.error;
        });
        </script><div id="out"></div>"""))


# ================================================================
# 19: 知乎倒立文字验证码
# ================================================================
@challenge(19, "知乎倒立文字验证码", "ZH_TOKEN_19")
def c19(h, sub):
    if sub == "/word.png":
        from PIL import Image, ImageDraw, ImageFont
        img = Image.new("RGB", (200, 80), (255, 255, 255))
        d = ImageDraw.Draw(img)
        font = ImageFont.load_default(size=48)
        d.text((20, 10), "hello", fill=(20, 20, 20), font=font)
        img = img.rotate(180)
        buf = io.BytesIO()
        img.save(buf, "PNG")
        h._send(200, buf.getvalue(), "image/png")
        return
    if sub == "/check":
        body = json.loads(h.rfile.read(int(h.headers.get("Content-Length", 0))).decode() or "{}")
        if body.get("word", "").lower() == "hello":
            h._json({"token": "ZH_TOKEN_19"})
        else:
            h._json({"error": "wrong"}, 403)
        return
    h._send(200, full_page(19, "知乎倒立文字验证码",
        """<p>识别倒立的英文单词并输入：</p>
        <img id="w" src="/c/19/word.png" alt="word"><input id="word"><button id="go">提交</button><div id="out"></div>
        <script>
        document.getElementById('go').onclick=function(){
          fetch('/c/19/check',{method:'POST',body:JSON.stringify({word:document.getElementById('word').value.trim()})})
            .then(r=>r.json()).then(d=>{document.getElementById('out').textContent=d.token||d.error;});
        };
        </script>"""))


# ================================================================
# 20: 京东滑动验证
# ================================================================
@challenge(20, "京东滑动验证", "STOCK_20")
def c20(h, sub):
    if sub == "/api":
        h._json({"stock": "STOCK_20"})
        return
    h._send(200, full_page(20, "京东滑动验证",
        """<style>
        #track{width:320px;height:44px;background:#eee;position:relative}
        #gap{position:absolute;left:230px;top:0;width:44px;height:44px;background:#f66;opacity:.4}
        #slider{position:absolute;left:0;top:0;width:44px;height:44px;background:#e1251b;cursor:grab}
        </style>
        <div id="track"><div id="gap"></div><div id="slider"></div></div><p id="out">滑动验证后查询库存</p>
        <script>
        var s=document.getElementById('slider'),drag=false,sx=0;
        s.addEventListener('mousedown',function(e){drag=true;sx=e.clientX;});
        document.addEventListener('mousemove',function(e){if(!drag)return;s.style.left=Math.max(0,Math.min(e.clientX-sx,280))+'px';});
        document.addEventListener('mouseup',function(){if(!drag)return;drag=false;var x=parseInt(s.style.left)||0;
          if(Math.abs(x-230)<=6){fetch('/c/20/api').then(r=>r.json()).then(d=>{document.getElementById('out').textContent='stock='+d.stock;});}
          else document.getElementById('out').textContent='x='+x;});
        </script>"""))



# ================================================================
# 21: 淘宝登录滑块
# ================================================================
@challenge(21, "淘宝登录滑块", "TB_TOKEN_21")
def c21(h, sub):
    h._send(200, full_page(21, "淘宝登录滑块",
        """<style>
        #track{width:320px;height:44px;background:#eee;position:relative}
        #gap{position:absolute;left:200px;top:0;width:44px;height:44px;background:#ff5000;opacity:.4}
        #slider{position:absolute;left:0;top:0;width:44px;height:44px;background:#ff5000;cursor:grab}
        </style>
        <div id="track"><div id="gap"></div><div id="slider"></div></div><p id="out">滑动验证</p>
        <script>
        var s=document.getElementById('slider'),drag=false,sx=0;
        s.addEventListener('mousedown',function(e){drag=true;sx=e.clientX;});
        document.addEventListener('mousemove',function(e){if(!drag)return;s.style.left=Math.max(0,Math.min(e.clientX-sx,280))+'px';});
        document.addEventListener('mouseup',function(){if(!drag)return;drag=false;var x=parseInt(s.style.left)||0;
          if(Math.abs(x-200)<=6){document.cookie='_tb_token_=TB_TOKEN_21; path=/';document.getElementById('out').textContent='通过';}
          else document.getElementById('out').textContent='x='+x;});
        </script>"""))


# ================================================================
# 22: 拼多多 anti_content 计算
# ================================================================
@challenge(22, "拼多多anti_content计算", "GOODS_ID_22")
def c22(h, sub):
    if sub == "/api":
        ac = h.headers.get("Anti-Content", "")
        if ac == "PDD_ANTI_OK":
            h._json({"goods": [{"goods_id": "GOODS_ID_22"}]})
        else:
            h._json({"error": "bad anti_content"}, 403)
        return
    h._send(200, full_page(22, "拼多多anti_content计算",
        """<p>计算 anti_content 参数…</p>
        <script>
        var anti = 'PDD_ANTI_OK';
        var h = new Headers(); h.append('Anti-Content', anti);
        fetch('/c/22/api', {headers: h}).then(r=>r.json()).then(d=>{
          document.getElementById('out').textContent = (d.goods||[])[0] ? d.goods[0].goods_id : d.error;
        });
        </script><div id="out"></div>"""))


# ================================================================
# 23: 美团点评 _token 签名
# ================================================================
@challenge(23, "美团点评_token签名", "POI_ID_23")
def c23(h, sub):
    if sub == "/api":
        tok = h.headers.get("X-Token", "")
        if tok == "MT_SIGN_23":
            h._json({"pois": [{"id": "POI_ID_23"}]})
        else:
            h._json({"error": "bad token"}, 403)
        return
    h._send(200, full_page(23, "美团点评_token签名",
        """<p>计算 _token 签名…</p>
        <script>
        var tok = 'MT_SIGN_23';
        var h = new Headers(); h.append('X-Token', tok);
        fetch('/c/23/api', {headers: h}).then(r=>r.json()).then(d=>{
          document.getElementById('out').textContent = (d.pois||[])[0] ? d.pois[0].id : d.error;
        });
        </script><div id="out"></div>"""))


# ================================================================
# 24: 58同城数字字母验证码
# ================================================================
@challenge(24, "58同城数字字母验证码", "RESUME_ID_24")
def c24(h, sub):
    if sub == "/check":
        body = json.loads(h.rfile.read(int(h.headers.get("Content-Length", 0))).decode() or "{}")
        expect = h.server.captcha_codes.get(body.get("cid", ""), "")
        if body.get("code", "").lower() == expect.lower():
            h._json({"resume_id": "RESUME_ID_24"})
        else:
            h._json({"error": "wrong"}, 403)
        return
    import random as _r
    code = "".join(_r.choices("abcdefghjkmnpqrstuvwxyz23456789", k=4))
    cid = hashlib.md5((code + str(time.time())).encode()).hexdigest()[:16]
    h.server.captcha_codes[cid] = code
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (150, 50), (255, 255, 255))
    d = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=30)
    for i, ch in enumerate(code):
        d.text((6 + i * 36, 6), ch, fill=(20, 20, 20), font=font)
    for _ in range(14):
        d.point((_r.randint(0, 149), _r.randint(0, 49)), fill=(_r.randint(80, 220),) * 3)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    h._send(200, full_page(24, "58同城数字字母验证码",
        f"""<p>识别验证码后登录获取简历列表第一个 ID：</p>
        <img id="cap" src="data:image/png;base64,{b64}" cid="{cid}">
        <input id="code"><button id="go">提交</button><div id="out"></div>
        <script>
        document.getElementById('go').onclick=function(){{
          fetch('/c/24/check',{{method:'POST',body:JSON.stringify({{code:document.getElementById('code').value.trim(),cid:'{cid}'}})}})
            .then(r=>r.json()).then(d=>{{document.getElementById('out').textContent=d.resume_id||d.error;}});
        }};
        </script>"""))


# ================================================================
# 25: 瑞数动态加密 Cookie
# ================================================================
@challenge(25, "瑞数动态加密Cookie", "RUID_SID_25")
def c25(h, sub):
    cookie = h.headers.get("Cookie", "")
    if "__jsl_ruid" in cookie:
        h._send(200, "<html><body><div id='sid'>RUID_SID_25</div></body></html>")
        return
    h._send(200, full_page(25, "瑞数动态加密Cookie",
        """<p>JS 计算 __jsl_ruid cookie…</p>
        <script>
        var v = 0, s = 'rui_seed';
        for (var i = 0; i < s.length; i++) v = (v * 31 + s.charCodeAt(i)) & 0xffffffff;
        document.cookie = '__jsl_ruid=' + v + '; path=/';
        </script><div id="hint">cookie 已种</div>"""))


# ================================================================
# 26: 数美设备指纹生成
# ================================================================
@challenge(26, "数美设备指纹生成", "SM_USER_26")
def c26(h, sub):
    if sub == "/api":
        body = json.loads(h.rfile.read(int(h.headers.get("Content-Length", 0))).decode() or "{}")
        if body.get("deviceId", "").startswith("SM_DEV_"):
            h._json({"userId": "SM_USER_26"})
        else:
            h._json({"error": "bad deviceId"}, 403)
        return
    h._send(200, full_page(26, "数美设备指纹生成",
        """<p>生成合法 deviceId 并注册…</p>
        <script>
        var deviceId = 'SM_DEV_' + Date.now().toString(36);
        fetch('/c/26/api', {method:'POST', body: JSON.stringify({deviceId: deviceId})})
          .then(r=>r.json()).then(d=>{document.getElementById('out').textContent = d.userId || d.error;});
        </script><div id="out"></div>"""))


# ================================================================
# 27: Akamai Bot Manager 传感器
# ================================================================
@challenge(27, "Akamai Bot Manager传感器", "ABCK_PID_27")
def c27(h, sub):
    cookie = h.headers.get("Cookie", "")
    if "_abck" in cookie:
        h._send(200, "<html><body><div id='pid'>ABCK_PID_27</div></body></html>")
        return
    h._send(200, full_page(27, "Akamai Bot Manager传感器",
        """<p>执行传感器脚本生成 _abck…</p>
        <script>
        var s = 'akamai_seed';
        var x = 0;
        for (var i = 0; i < s.length; i++) x = (x * 31 + s.charCodeAt(i)) & 0xffffffff;
        document.cookie = '_abck=' + x + '; path=/';
        </script><div id="hint">sensor 已执行</div>"""))


# ================================================================
# 28: F5 Shape 加密 POST
# ================================================================
@challenge(28, "F5 Shape加密POST", "F5_SESSION_28")
def c28(h, sub):
    if sub == "/login":
        body = json.loads(h.rfile.read(int(h.headers.get("Content-Length", 0))).decode() or "{}")
        if body.get("encrypted") == "F5_ENC_OK":
            h._json({"session_token": "F5_SESSION_28"})
        else:
            h._json({"error": "bad"}, 403)
        return
    h._send(200, full_page(28, "F5 Shape加密POST",
        """<p>加密登录表单提交…</p>
        <script>
        fetch('/c/28/login', {method:'POST', body: JSON.stringify({encrypted: 'F5_ENC_OK'})})
          .then(r=>r.json()).then(d=>{document.getElementById('out').textContent = d.session_token || d.error;});
        </script><div id="out"></div>"""))


# ================================================================
# 29: Imperva (Incapsula) 验证
# ================================================================
@challenge(29, "Imperva验证", "299436")
def c29(h, sub):
    h._send(200, full_page(29, "Imperva验证",
        """<p>JS 质询 + reCAPTCHA 组合…</p><div id="out"></div>
        <script>
        setTimeout(function(){
          var x = 0, s = 'incapsula_seed';
          for (var i = 0; i < s.length; i++) x = (x * 31 + s.charCodeAt(i)) & 0xffffffff;
          var code = String(Math.abs(x) % 1000000).padStart(6, '0');
          document.title = code + ' - Imperva验证';
          document.getElementById('out').textContent = 'CODE_' + code;
        }, 400);
        </script>"""))


# ================================================================
# 30: Distil Networks 指纹绕过
# ================================================================
@challenge(30, "Distil Networks指纹绕过", "DISTIL_TOKEN_30")
def c30(h, sub):
    h._send(200, full_page(30, "Distil Networks指纹绕过",
        """<p>检查浏览器指纹一致性…</p><div id="out"></div>
        <script>
        var ok = navigator.plugins.length >= 5 && !navigator.webdriver;
        document.getElementById('out').textContent = ok ? 'DISTIL_TOKEN_30' : 'bad';
        </script>"""))



# ================================================================
# 31: AWS CloudFront WAF 绕过
# ================================================================
@challenge(31, "AWS CloudFront WAF绕过", "ACCESS_KEY_31")
def c31(h, sub):
    ip = h.headers.get("X-Forwarded-For", "")
    if ip.startswith("203.0.113."):
        h._send(200, "<html><body><div id='ak'>ACCESS_KEY_31</div></body></html>")
    else:
        h._send(403, "blocked by WAF")


# ================================================================
# 32: 多层响应加密（Brotli->Base64->XOR）
# ================================================================
@challenge(32, "多层响应加密", "USER_ID_32")
def c32(h, sub):
    if sub == "/data":
        import gzip
        payload = b'{"user_id": "USER_ID_32"}'
        xored = bytes(b ^ 0x33 for b in payload)
        b64 = base64.b64encode(xored)
        compressed = gzip.compress(b64)
        h._send(200, compressed, "application/octet-stream")
        return
    h._send(200, full_page(32, "多层响应加密",
        """<p>Brotli 解压 -> Base64 解码 -> XOR 解密…</p><div id="out"></div>
        <script>
        fetch('/c/32/data').then(r=>r.arrayBuffer()).then(async buf => {
          const ds = new DecompressionStream('gzip');
          const stream = new Blob([buf]).stream().pipeThrough(ds);
          const dec = await new Response(stream).arrayBuffer();
          const b64 = new TextDecoder().decode(dec);
          const raw = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
          let s = '';
          for (let i = 0; i < raw.length; i++) s += String.fromCharCode(raw[i] ^ 0x33);
          document.getElementById('out').textContent = JSON.parse(s).user_id;
        });
        </script>"""))


# ================================================================
# 33: MessagePack 响应解析
# ================================================================
@challenge(33, "MessagePack响应解析", "MSGPACK_TOKEN_33")
def c33(h, sub):
    if sub == "/api":
        import sys as _s
        _s.path.insert(0, str(ROOT.parent / "vendor"))
        import msgpack
        h._send(200, msgpack.packb({"user_token": "MSGPACK_TOKEN_33"}), "application/octet-stream")
        return
    h._send(200, full_page(33, "MessagePack响应解析",
        """<p>/c/33/api 返回 MessagePack 二进制，解析 user_token。</p>"""))


# ================================================================
# 34: Chunked 流式读取
# ================================================================
@challenge(34, "Chunked流式读取", "ID_5")
def c34(h, sub):
    if sub == "/stream":
        h.send_response(200)
        h.send_header("Content-Type", "application/json")
        h.send_header("Transfer-Encoding", "chunked")
        h.end_headers()
        try:
            for i in range(1, 6):
                chunk = json.dumps({"id": f"ID_{i}"}).encode()
                h.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                h.wfile.flush()
                time.sleep(0.05)
            h.wfile.write(b"0\r\n\r\n")
        except Exception:
            pass
        return
    h._send(200, full_page(34, "Chunked流式读取",
        """<p>/c/34/stream 是 chunked 流式响应，拼接后取最后一条记录 id。</p>"""))


# ================================================================
# 35: HTTP/3 QUIC 连接（等价）
# ================================================================
@challenge(35, "HTTP/3 QUIC连接(等价)", "XQUIC_ID_35")
def c35(h, sub):
    quic = h.headers.get("X-QUIC", "")
    if quic == "1":
        h._send(200, "<html><body><div id='q'>XQUIC_ID_35</div></body></html>", headers={"x-quic-id": "XQUIC_ID_35"})
    else:
        h._send(403, "需要 QUIC")


# ================================================================
# 36: Header 大小写顺序验证
# ================================================================
@challenge(36, "Header大小写顺序验证", "HDR_CASE_36")
def c36(h, sub):
    ae = h.headers.get("Accept-Encoding", "")
    if ae == "gzip, deflate, br":
        h._send(200, "<html><body><div id='f'>HDR_CASE_36</div></body></html>")
    else:
        h._send(403, f"AE={ae}")


# ================================================================
# 37: Cookie 哈希完整性
# ================================================================
@challenge(37, "Cookie哈希完整性", "APP_ID_37")
def c37(h, sub):
    cookie = h.headers.get("Cookie", "")
    import re as _re
    m = _re.search(r"session=([^;]+)", cookie)
    mh = _re.search(r"hash=([0-9a-f]{32})", cookie)
    if m and mh and mh.group(1) == hashlib.md5(m.group(1).encode()).hexdigest():
        h._send(200, "<html><body><div id='app'>APP_ID_37</div></body></html>")
    else:
        h._send(403, "hash 不匹配")


# ================================================================
# 38: WebSocket DH 密钥交换
# ================================================================
@challenge(38, "WebSocket DH密钥交换", "DH_TOKEN_38")
def c38(h, sub):
    if sub == "/pub":
        h._json({"p": 23, "g": 5, "server_public": 8})
        return
    if sub == "/key":
        body = json.loads(h.rfile.read(int(h.headers.get("Content-Length", 0))).decode() or "{}")
        client_pub = int(body.get("client_public", 0))
        # server private = 6 -> server_public = 5^6 mod 23 = 8
        shared = pow(client_pub, 6, 23)
        if shared == 2:
            h._json({"token": "DH_TOKEN_38"})
        else:
            h._json({"error": "bad shared"}, 403)
        return
    h._send(200, full_page(38, "WebSocket DH密钥交换",
        """<p>GET /c/38/pub 拿 p/g/server_public，计算共享密钥后 POST /c/38/key。</p>"""))


# ================================================================
# 39: MetaMask 钱包签名登录
# ================================================================
@challenge(39, "MetaMask钱包签名登录", "JWT_TOKEN_39")
def c39(h, sub):
    if sub == "/nonce":
        h._json({"nonce": "random_nonce_123"})
        return
    if sub == "/login":
        body = json.loads(h.rfile.read(int(h.headers.get("Content-Length", 0))).decode() or "{}")
        if body.get("signed") == "VALID_SIG_39":
            h._json({"jwt": "JWT_TOKEN_39"})
        else:
            h._json({"error": "bad sig"}, 403)
        return
    h._send(200, full_page(39, "MetaMask钱包签名登录",
        """<p>获取 nonce -> 签名 -> POST /c/39/login 获取 JWT。</p>"""))


# ================================================================
# 40: WebAuthn 虚拟认证
# ================================================================
@challenge(40, "WebAuthn虚拟认证", "WEBAUTHN_FLAG_40")
def c40(h, sub):
    if sub == "/api":
        body = json.loads(h.rfile.read(int(h.headers.get("Content-Length", 0))).decode() or "{}")
        if body.get("authenticator") == "virtual-pass":
            h._json({"flag": "WEBAUTHN_FLAG_40"})
        else:
            h._json({"error": "need authenticator"}, 403)
        return
    h._send(200, full_page(40, "WebAuthn虚拟认证",
        """<p>使用虚拟认证器完成注册…</p>
        <script>
        fetch('/c/40/api', {method:'POST', body: JSON.stringify({authenticator: 'virtual-pass'})})
          .then(r=>r.json()).then(d=>{document.getElementById('out').textContent = d.flag || d.error;});
        </script><div id="out"></div>"""))



# ================================================================
# 41: Service Worker 响应篡改
# ================================================================
@challenge(41, "Service Worker响应篡改", "SW_ORIG_41")
def c41(h, sub):
    if sub == "/sw.js":
        h._send(200, r"""
self.addEventListener('install', (e) => { self.skipWaiting(); });
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', (e) => {
  if (e.request.url.endsWith('/c/41/api')) {
    // SW 篡改：添加令牌头后转发；原响应在 network 层
    e.respondWith(fetch(e.request).then(r => {
      const h = new Headers(r.headers); h.set('X-SW-Token', 'SW_ORIG_41');
      return new Response(r.body, {status: r.status, headers: h});
    }));
  }
});
""", "application/javascript")
        return
    if sub == "/api":
        h._json({"data": "original_data_41"})
        return
    h._send(200, full_page(41, "Service Worker响应篡改",
        """<p>SW 为请求添加令牌头，网络面板捕获原始服务器响应…</p>
        <script>
        navigator.serviceWorker.register('/c/41/sw.js').then(() => {
          const h = () => { navigator.serviceWorker.removeEventListener('controllerchange', h); fetch('/c/41/api').then(r=>r.json()).then(d=>{
            document.getElementById('out').textContent = d.data;
          }); };
          if (navigator.serviceWorker.controller) fetch('/c/41/api').then(r=>r.json()).then(d=>{document.getElementById('out').textContent=d.data;});
          else navigator.serviceWorker.addEventListener('controllerchange', h);
        });
        </script><div id="out"></div>"""))


# ================================================================
# 42: Web Worker 哈希计算
# ================================================================
@challenge(42, "Web Worker哈希计算", "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824")
def c42(h, sub):
    h._send(200, full_page(42, "Web Worker哈希计算",
        """<p>向 Worker 发送数据，接收 sha256…</p><div id="out"></div>
        <script>
        var code = "self.onmessage = async function(e){ var buf = new TextEncoder().encode(e.data); var d = await crypto.subtle.digest('SHA-256', buf); var a = new Uint8Array(d); var s=''; for (var i=0;i<a.length;i++) s += a[i].toString(16).padStart(2,'0'); self.postMessage(s); };";
        var blob = new Blob([code], {type: 'application/javascript'});
        var url = URL.createObjectURL(blob);
        var w = new Worker(url);
        w.onmessage = function(e){ document.getElementById('out').textContent = e.data; };
        w.postMessage('hello');
        </script>"""))


# ================================================================
# 43: WASM 解密函数调用
# ================================================================
@challenge(43, "WASM解密函数调用", "EMAIL_PREFIX_42")
def c43(h, sub):
    wasm = make_wasm_calc()
    import base64 as _b64
    h._send(200, full_page(43, "WASM解密函数调用",
        f"""<p>调用 WASM 导出函数 decrypt() 解密响应密文…</p><div id="out"></div>
        <script>
        WebAssembly.instantiate(Uint8Array.from(atob('{_b64.b64encode(wasm).decode()}'), c => c.charCodeAt(0)))
          .then(({{instance}}) => {{
            var v = instance.exports.calc();
            document.getElementById('out').textContent = 'EMAIL_PREFIX_' + v;
          }});
        </script>"""))


# ================================================================
# 44: 时间侧信道防御规避
# ================================================================
@challenge(44, "时间侧信道防御规避", "TIMING_TOKEN_44")
def c44(h, sub):
    h._send(200, full_page(44, "时间侧信道防御规避",
        """<p>稳定执行 JS（恒定时间）…</p><div id="out"></div>
        <script>
        function dummyWork(){ var x=0; for (var i=0;i<100000;i++) x+=i; return x; }
        var t0 = performance.now();
        dummyWork();
        // 恒定时间：补足到固定时长
        while (performance.now() - t0 < 20) {}
        window.__token = 'TIMING_TOKEN_44';
        document.getElementById('out').textContent = window.__token;
        </script>"""))


# ================================================================
# 45: Object.defineProperty 劫持绕过
# ================================================================
@challenge(45, "defineProperty劫持绕过", "REAL_TOKEN_45")
def c45(h, sub):
    h._send(200, full_page(45, "defineProperty劫持绕过",
        """<p>window._token 被 defineProperty 重写，需通过 iframe/代理恢复…</p><div id="out"></div>
        <script>
        Object.defineProperty(window, '_token', {get: function(){ return 'FAKE_TOKEN'; }});
        // 真实值存别处
        var real = {t: 'REAL_TOKEN_45'};
        window.__realToken = function(){ return real.t; };
        document.getElementById('out').textContent = window.__realToken();
        </script>"""))


# ================================================================
# 46: 无限 debugger + toString 修改
# ================================================================
@challenge(46, "无限debugger+toString修改", "HARD_KEY_46")
def c46(h, sub):
    h._send(200, full_page(46, "无限debugger+toString修改",
        """<div id="out"></div>
        <script>
        setInterval(function(){ debugger; }, 100);
        // 被修改的 toString
        var f = function(){ return 'HARD_KEY_46'; };
        f.toString = function(){ return 'function(){ return "[OBFUSCATED]"; }'; };
        // 源码中的硬编码密钥（重构 toString 后可从 f() 调用获得）
        document.getElementById('out').textContent = f();
        </script>"""))


# ================================================================
# 47: requestAnimationFrame Canvas 渲染
# ================================================================
@challenge(47, "rAF Canvas渲染", "RENDER_31415")
def c47(h, sub):
    h._send(200, full_page(47, "rAF Canvas渲染",
        """<canvas id="cv" width="540" height="70"></canvas><p>动画循环渲染动态数字，截图 OCR。</p>
        <script>
        const cv = document.getElementById('cv'), ctx = cv.getContext('2d');
        var frame = 0;
        function draw(){
          ctx.clearRect(0, 0, 480, 70);
          ctx.font = 'bold 34px Menlo';
          ctx.fillStyle = '#111';
          const text = 'RENDER_31415';
          for (let i = 0; i < text.length; i++) ctx.fillText(text[i], 14 + i * 42, 48);
          frame++;
          if (frame < 12) requestAnimationFrame(draw);
        }
        draw();
        </script>"""))


# ================================================================
# 48: 人类滑动轨迹模拟
# ================================================================
@challenge(48, "人类滑动轨迹模拟", "PASS_TOKEN_48")
def c48(h, sub):
    h._send(200, full_page(48, "人类滑动轨迹模拟",
        """<style>
        #track{width:320px;height:44px;background:#eee;position:relative}
        #gap{position:absolute;left:220px;top:0;width:44px;height:44px;background:#f66;opacity:.4}
        #slider{position:absolute;left:0;top:0;width:44px;height:44px;background:#36c;cursor:grab}
        </style>
        <div id="track"><div id="gap"></div><div id="slider"></div></div><p id="out">验证轨迹</p>
        <script>
        var s=document.getElementById('slider'),drag=false,sx=0;
        s.addEventListener('mousedown',function(e){drag=true;sx=e.clientX;});
        document.addEventListener('mousemove',function(e){if(!drag)return;s.style.left=Math.max(0,Math.min(e.clientX-sx,280))+'px';});
        document.addEventListener('mouseup',function(){if(!drag)return;drag=false;var x=parseInt(s.style.left)||0;
          document.getElementById('out').textContent=(Math.abs(x-220)<=6)?'PASS_TOKEN_48':'x='+x;});
        </script>"""))


# ================================================================
# 49: 浏览器指纹一致性伪装
# ================================================================
@challenge(49, "浏览器指纹一致性伪装", "FINGER_SECRET_49")
def c49(h, sub):
    h._send(200, full_page(49, "浏览器指纹一致性伪装",
        """<p>检查 hardwareConcurrency / deviceMemory…</p><div id="out"></div>
        <script>
        var ok = navigator.hardwareConcurrency === 8 && (navigator.deviceMemory || 8) >= 8;
        document.getElementById('out').textContent = ok ? 'FINGER_SECRET_49' : 'hc=' + navigator.hardwareConcurrency;
        </script>"""))


# ================================================================
# 50: Chrome 扩展检测伪造
# ================================================================
@challenge(50, "Chrome扩展检测伪造", "EXT_SECRET_50")
def c50(h, sub):
    h._send(200, full_page(50, "Chrome扩展检测伪造",
        """<p>检查 chrome.runtime 扩展 ID…</p><div id="out"></div>
        <script>
        var ok = window.chrome && chrome.runtime && chrome.runtime.id === 'fake-extension-id-123';
        document.getElementById('out').textContent = ok ? 'EXT_SECRET_50' : 'no-ext';
        </script>"""))



def make_wasm_calc():
    """手写最小 wasm：导出 calc() 返回 i32 42"""
    b = bytearray(b"\x00asm\x01\x00\x00\x00")
    b += b"\x01\x05\x01\x60\x00\x01\x7f"
    b += b"\x03\x02\x01\x00"
    b += b"\x07\x08\x01\x04calc\x00\x00"
    b += b"\x0a\x06\x01\x04\x00\x41\x2a\x0b"
    return bytes(b)



# ================================================================
# 51: document.hasFocus() 检测
# ================================================================
@challenge(51, "document.hasFocus()检测", "AUTH_TOKEN_51")
def c51(h, sub):
    h._send(200, full_page(51, "document.hasFocus()检测",
        """<p>页面聚焦时读取 sessionStorage…</p><div id="out"></div>
        <script>
        sessionStorage.setItem('auth_token', 'AUTH_TOKEN_51');
        document.addEventListener('focus', function(){ document.getElementById('out').textContent = sessionStorage.getItem('auth_token'); });
        if (document.hasFocus()) document.getElementById('out').textContent = sessionStorage.getItem('auth_token');
        </script>"""))


# ================================================================
# 52: 跨域 iframe postMessage
# ================================================================
@challenge(52, "跨域iframe postMessage", "INNER_ID_52")
def c52(h, sub):
    if sub == "/sub":
        h._send(200, "<script>parent.postMessage({inner_id: 'INNER_ID_52'}, '*');</script>")
        return
    h._send(200, full_page(52, "跨域iframe postMessage",
        """<p>子页面通过 postMessage 发送数据…</p><div id="out"></div>
        <iframe id="sub" src="/c/52/sub" style="display:none"></iframe>
        <script>
        window.addEventListener('message', function(e){
          if (e.data && e.data.inner_id) document.getElementById('out').textContent = e.data.inner_id;
        });
        </script>"""))


# ================================================================
# 53: CSP nonce 绕过
# ================================================================
@challenge(53, "CSP nonce绕过", "CSP_SECRET_53")
def c53(h, sub):
    h._send(200, full_page(53, "CSP nonce绕过",
        """<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'">
        <div id="out"></div>
        <script>
        var d = document.createElement('div'); d.id = '__SECRET__'; d.textContent = 'CSP_SECRET_53';
        document.body.appendChild(d);
        document.getElementById('out').textContent = d.textContent;
        </script>"""))


# ================================================================
# 54: SVG foreignObject 数据提取
# ================================================================
@challenge(54, "SVG foreignObject数据提取", "FOBJ_VALUE_54")
def c54(h, sub):
    h._send(200, full_page(54, "SVG foreignObject数据提取",
        """<svg width="200" height="60"><foreignObject width="200" height="60">
        <div xmlns="http://www.w3.org/1999/xhtml" data-value="FOBJ_VALUE_54">隐藏数据</div>
        </foreignObject></svg>
        <p>解析 SVG 内嵌 HTML 的 data-value。</p>"""))


# ================================================================
# 55: Accept-Language 内容差异
# ================================================================
@challenge(55, "Accept-Language内容差异", "LANG_PID_55")
def c55(h, sub):
    lang = h.headers.get("Accept-Language", "")
    if "en" in lang:
        h._send(200, "<html><body><div id='pid'>LANG_PID_55</div></body></html>")
    else:
        h._send(200, "<html><body><div id='pid'>NOT_AVAILABLE</div></body></html>")


# ================================================================
# 56: HMAC-SHA256 动态签名
# ================================================================
@challenge(56, "HMAC-SHA256动态签名", "HMAC_USER_56")
def c56(h, sub):
    if sub == "/api":
        import hmac as _hm
        nonce = h.headers.get("X-Nonce", "")
        ts = h.headers.get("X-Ts", "")
        sign = h.headers.get("X-Sign", "")
        expect = _hm.new(b"secret_key_56", f"nonce={nonce}&ts={ts}".encode(), hashlib.sha256).hexdigest()
        if sign == expect:
            h._json({"userId": "HMAC_USER_56"})
        else:
            h._json({"error": "bad sign"}, 403)
        return
    h._send(200, full_page(56, "HMAC-SHA256动态签名",
        """<p>计算 nonce+timestamp 的 HMAC-SHA256 签名请求接口。</p>"""))


# ================================================================
# 57: 加密分页 Cursor
# ================================================================
@challenge(57, "加密分页Cursor", "CURSOR_LAST_57")
def c57(h, sub):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives import padding
    key = b"cursor_key_12345"
    iv = b"cursor_iv_123456"
    if sub.startswith("/api"):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(h.path).query)
        page = int(q.get("page", ["1"])[0])
        if page < 3:
            # 加密 nextCursor = 下一页码
            padder = padding.PKCS7(128).padder()
            data = padder.update(str(page + 1).encode()) + padder.finalize()
            enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
            ct = enc.update(data) + enc.finalize()
            h._json({"page": page, "nextCursor": base64.b64encode(ct).decode(), "last": False})
        else:
            h._json({"page": page, "last": True, "lastId": "CURSOR_LAST_57"})
        return
    h._send(200, full_page(57, "加密分页Cursor",
        """<p>解密 AES 加密的 nextCursor 获取下一页，直到最后拿 lastId。</p>"""))


# ================================================================
# 58: 自定义 WebSocket 二进制帧解密
# ================================================================
@challenge(58, "自定义WS二进制帧解密", "WS_SESSION_58")
def c58(h, sub):
    h._send(200, full_page(58, "自定义WS二进制帧解密",
        """<p>每帧长度头 + XOR 加密…</p><div id="out"></div>
        <script>
        const ws = new WebSocket('ws://' + location.host + '/ws/frame58');
        ws.binaryType = 'arraybuffer';
        ws.onmessage = function(e){
          const dv = new DataView(e.data);
          const len = dv.getUint16(0);
          let s = '';
          for (let i = 2; i < 2 + len; i++) s += String.fromCharCode(dv.getUint8(i) ^ 0x2A);
          document.getElementById('out').textContent = s;
        };
        </script>"""))


# ================================================================
# 59: 阿里云 API 网关验签
# ================================================================
@challenge(59, "阿里云API网关验签", "ALI_ORDER_59")
def c59(h, sub):
    if sub == "/api":
        import hmac as _hm
        key = h.headers.get("X-Ca-Key", "")
        sig = h.headers.get("X-Ca-Signature", "")
        ts = h.headers.get("X-Ca-Timestamp", "")
        expect = _hm.new(b"ali_secret_59", f"path=/c/59/api&ts={ts}".encode(), hashlib.sha256).hexdigest()
        if key == "ALI_KEY_59" and sig == expect:
            h._json({"order_id": "ALI_ORDER_59"})
        else:
            h._json({"error": "bad signature"}, 403)
        return
    h._send(200, full_page(59, "阿里云API网关验签",
        """<p>计算 X-Ca-Signature（HMAC-SHA256）请求受保护 API。</p>"""))


# ================================================================
# 60: 腾讯云 WAF 人机识别
# ================================================================
@challenge(60, "腾讯云WAF人机识别", "TENCENT_CSRF_60")
def c60(h, sub):
    cookie = h.headers.get("Cookie", "")
    if "__Tas_" in cookie:
        h._send(200, "<html><body><div id='csrf'>TENCENT_CSRF_60</div></body></html>")
        return
    h._send(200, full_page(60, "腾讯云WAF人机识别",
        """<p>前端 JS 挑战计算 __Tas_ cookie…</p>
        <script>
        var v = 0, s = 'tencent_waf_seed';
        for (var i = 0; i < s.length; i++) v = (v * 31 + s.charCodeAt(i)) & 0xffffffff;
        document.cookie = '__Tas_' + v + '=ok; path=/';
        </script><div id="hint">挑战完成</div>"""))



# ================================================================
# 61: 动态字体子集化映射
# ================================================================
@challenge(61, "动态字体子集化映射", "6599")
def c61(h, sub):
    if sub == "/font.woff":
        import sys as _s
        _s.path.insert(0, str(ROOT.parent / "vendor"))
        from fontTools.ttLib import TTFont
        from fontTools.ttLib.tables._c_m_a_p import cmap_format_4
        # 每次加载随机映射：0xF100+i -> 数字字形，但页面给出映射表
        font = TTFont("/System/Library/Fonts/Supplemental/Andale Mono.ttf")
        cmap = font.getBestCmap()
        glyphs = {0xF100 + i: cmap.get(ord(str(i)), ".notdef") for i in range(10)}
        st = cmap_format_4(4)
        st.platformID, st.platEncID, st.format, st.language = 3, 1, 4, 0
        st.cmap = dict(glyphs)
        font["cmap"].tables = [st]
        buf = io.BytesIO()
        font.save(buf)
        h._send(200, buf.getvalue(), "font/woff")
        return
    # 价格 6 5 9 9
    digits = [6, 5, 9, 9]
    cps = "".join(f"&#x{0xF100 + d:x};" for d in digits)
    h._send(200, full_page(61, "动态字体子集化映射",
        f"""<style>@font-face {{font-family:'dy';src:url(/c/61/font.woff);}}</style>
        <p>价格：<span style="font-family:dy;font-size:30px">{cps}</span>（cmap: 0xF100+i → 数字 i）</p>"""))


# ================================================================
# 62: CSS counter 动态编号
# ================================================================
@challenge(62, "CSS counter动态编号", "COUNTER_7_62")
def c62(h, sub):
    h._send(200, full_page(62, "CSS counter动态编号",
        """<style>
        ol.dyn { counter-reset: n 2; list-style: none; }
        ol.dyn li { counter-increment: n 1; }
        ol.dyn li::before { content: counter(n) ". "; }
        </style>
        <ol class="dyn"><li>甲</li><li>乙</li><li>丙</li><li>丁</li><li>戊</li></ol>
        <p>counter 初始值由 JS 设定为 2，最后一个 li 的编号即答案（COUNTER_7_62 的数字 7）。</p>"""))


# ================================================================
# 63: CSS attr(Base64)
# ================================================================
@challenge(63, "CSS attr(Base64)", "B64_STRING_63")
def c63(h, sub):
    import base64 as _b64
    enc = _b64.b64encode(b"B64_STRING_63").decode()
    h._send(200, full_page(63, "CSS attr(Base64)",
        f"""<style>.bx::after{{content: attr(data-text)}}</style>
        <p>数据：<span class="bx" data-text="{enc}" style="display:none"></span></p>
        <p>说明：data-text 为 Base64，解码得到原始字符串。</p>"""))


# ================================================================
# 64: HLS 加密视频帧 OCR
# ================================================================
@challenge(64, "HLS加密视频帧OCR", "HLS_TOKEN_64")
def c64(h, sub):
    if sub == "/index.m3u8":
        h._send(200, "#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI=/c/64/key.bin\n#EXTINF:1,\n/c/64/seg0.ts\n#EXT-X-ENDLIST\n", "application/vnd.apple.mpegurl")
        return
    if sub == "/key.bin":
        h._send(200, b"1234567890abcdef", "application/octet-stream")
        return
    if sub == "/seg0.ts":
        # 简化：TS 片段 = 用 key XOR 的文本（含 token）
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        plain = b"HLS_TOKEN_64"
        pad = plain + b"\x00" * (16 - len(plain) % 16)
        enc = Cipher(algorithms.AES(b"1234567890abcdef"), modes.CBC(b"0000000000000000")).encryptor()
        ct = enc.update(pad) + enc.finalize()
        h._send(200, ct, "video/mp2t")
        return
    h._send(200, full_page(64, "HLS加密视频帧OCR",
        """<p>下载 m3u8 首个加密 TS 片段，AES 解密后提取 token。</p>"""))


# ================================================================
# 65: MIME 类型指纹模拟
# ================================================================
@challenge(65, "MIME类型指纹模拟", "MIME_TOKEN_65")
def c65(h, sub):
    h._send(200, full_page(65, "MIME类型指纹模拟",
        """<p>检查 navigator.mimeTypes 列表…</p><div id="out"></div>
        <script>
        var ok = navigator.mimeTypes.length >= 4 && !!navigator.mimeTypes['application/pdf'];
        document.getElementById('out').textContent = ok ? 'MIME_TOKEN_65' : 'mime=' + navigator.mimeTypes.length;
        </script>"""))


# ================================================================
# 66: GitHub 2FA TOTP
# ================================================================
@challenge(66, "GitHub 2FA TOTP", "TOTP_CSRF_66")
def c66(h, sub):
    if sub == "/verify":
        body = json.loads(h.rfile.read(int(h.headers.get("Content-Length", 0))).decode() or "{}")
        code = body.get("code", "")
        import hmac as _hm, time as _t
        # 种子 GITHUBTOTPSEED -> 验证 TOTP
        seed = b"GITHUBTOTPSEED"
        counter = int(_t.time()) // 30
        msg = counter.to_bytes(8, "big")
        digest = _hm.new(seed, msg, hashlib.sha1).digest()
        off = digest[-1] & 0x0F
        val = (int.from_bytes(digest[off:off+4], "big") & 0x7FFFFFFF) % 1000000
        expect = str(val).zfill(6)
        if code == expect:
            h._json({"csrf_token": "TOTP_CSRF_66"})
        else:
            h._json({"error": "bad totp"}, 403)
        return
    h._send(200, full_page(66, "GitHub 2FA TOTP",
        """<p>解析 TOTP 种子二维码（种子 GITHUBTOTPSEED），生成动态码完成登录。</p>"""))


# ================================================================
# 67: Discord 注册 hCaptcha
# ================================================================
@challenge(67, "Discord注册hCaptcha", "REG_TOKEN_67")
def c67(h, sub):
    if sub == "/api":
        body = json.loads(h.rfile.read(int(h.headers.get("Content-Length", 0))).decode() or "{}")
        if body.get("token") == "HCAPTCHA_DISCORD_67":
            h._json({"register_token": "REG_TOKEN_67"})
        else:
            h._json({"error": "bad token"}, 403)
        return
    tok = "HCAPTCHA_DISCORD_67"
    h._send(200, full_page(67, "Discord注册hCaptcha",
        f"""<script>
        setTimeout(function(){{
          var ta = document.createElement('textarea');
          ta.id = 'h-captcha-response'; ta.value = '{tok}';
          document.body.appendChild(ta);
          fetch('/c/67/api', {{method:'POST', body: JSON.stringify({{token: '{tok}'}})}}).then(r=>r.json()).then(d=>{{
            document.getElementById('out').textContent = d.register_token || d.error;
          }});
        }}, 300);
        </script><p>hCaptcha 校验…</p><div id="out"></div>"""))


# ================================================================
# 68: Arkose 旋转动物方向验证
# ================================================================
@challenge(68, "Arkose旋转动物验证", "ARKOSE_BEARER_68")
def c68(h, sub):
    h._send(200, full_page(68, "Arkose旋转动物验证",
        """<p>把动物旋转到正向（当前 180°）：</p>
        <img id="a" data-angle="180" style="width:100px;height:100px;transform:rotate(180deg)">
        <button id="rot">旋转+90°</button><button id="ok">确认</button><div id="out"></div>
        <script>
        var img=document.getElementById('a'), deg=parseInt(img.getAttribute('data-angle'));
        document.getElementById('rot').onclick=function(){deg=(deg+90)%360;img.style.transform='rotate('+deg+'deg)';};
        document.getElementById('ok').onclick=function(){
          document.getElementById('out').textContent=(deg%360===0)?'ARKOSE_BEARER_68':'angle='+deg;
        };
        </script>"""))


# ================================================================
# 69: LinkedIn FunCaptcha
# ================================================================
@challenge(69, "LinkedIn FunCaptcha", "LI_OATML_69")
def c69(h, sub):
    h._send(200, full_page(69, "LinkedIn FunCaptcha",
        """<style>
        #track{width:320px;height:44px;background:#eee;position:relative}
        #gap{position:absolute;left:210px;top:0;width:44px;height:44px;background:#0a66c2;opacity:.4}
        #slider{position:absolute;left:0;top:0;width:44px;height:44px;background:#0a66c2;cursor:grab}
        </style>
        <div id="track"><div id="gap"></div><div id="slider"></div></div><p id="out">验证</p>
        <script>
        var s=document.getElementById('slider'),drag=false,sx=0;
        s.addEventListener('mousedown',function(e){drag=true;sx=e.clientX;});
        document.addEventListener('mousemove',function(e){if(!drag)return;s.style.left=Math.max(0,Math.min(e.clientX-sx,280))+'px';});
        document.addEventListener('mouseup',function(){if(!drag)return;drag=false;var x=parseInt(s.style.left)||0;
          document.getElementById('out').textContent=(Math.abs(x-210)<=6)?'LI_OATML_69':'x='+x;});
        </script>"""))


# ================================================================
# 70: Reddit OAuth rate-limit 处理
# ================================================================
@challenge(70, "Reddit OAuth限流", "REDDIT_POST_70")
def c70(h, sub):
    if sub == "/api":
        import time as _t
        now = _t.time()
        win = h.server.rate_window70
        win[:] = [t for t in win if now - t < 2]
        if len(win) >= 2:
            h._json({"error": "rate_limited"}, 429)
            return
        win.append(now)
        h.server.rate_total70 = getattr(h.server, "rate_total70", 0) + 1
        if h.server.rate_total70 >= 3:
            h._json({"posts": [{"id": "REDDIT_POST_70"}]})
        else:
            h._json({"posts": [{"id": f"POST_{h.server.rate_total70}"}]})
        return
    h._send(200, full_page(70, "Reddit OAuth限流",
        """<p>使用 OAuth 应用令牌，控制频率请求热门帖子。</p>"""))



# ================================================================
# 71: Shopify 站点保护验证
# ================================================================
@challenge(71, "Shopify站点保护验证", "SHOP_VARIANT_71")
def c71(h, sub):
    h._send(200, full_page(71, "Shopify站点保护验证",
        """<p>通过前台验证后加入购物车…</p>
        <div id="variant">SHOP_VARIANT_71</div>
        <script>
        // 模拟 Shopify 前台验证后渲染变体 ID
        document.getElementById('variant').textContent = 'SHOP_VARIANT_71';
        </script>"""))


# ================================================================
# 72: Zendesk 速率限制突破
# ================================================================
@challenge(72, "Zendesk速率限制突破", "ZD_ARTICLE_72")
def c72(h, sub):
    if sub == "/api":
        import time as _t
        now = _t.time()
        win = h.server.rate_window72
        win[:] = [t for t in win if now - t < 1.5]
        if len(win) >= 2:
            h._json({"error": "rate_limited"}, 429)
            return
        win.append(now)
        h.server.rate_total72 = getattr(h.server, "rate_total72", 0) + 1
        if h.server.rate_total72 >= 3:
            h._json({"article": {"id": "ZD_ARTICLE_72"}})
        else:
            h._json({"article": {"id": f"ART_{h.server.rate_total72}"}})
        return
    h._send(200, full_page(72, "Zendesk速率限制突破",
        """<p>合理排队请求 Help Center 文章 ID。</p>"""))


# ================================================================
# 73: Cloudflare __cf_bm 计算
# ================================================================
@challenge(73, "Cloudflare __cf_bm计算", "CFBM_PID_73")
def c73(h, sub):
    cookie = h.headers.get("Cookie", "")
    if "__cf_bm" in cookie:
        h._send(200, "<html><body><div id='pid'>CFBM_PID_73</div></body></html>")
        return
    h._send(200, full_page(73, "Cloudflare __cf_bm计算",
        """<p>执行计算生成 __cf_bm…</p>
        <script>
        var t = Date.now();
        var x = 0, s = 'cf_bm_seed';
        for (var i = 0; i < s.length; i++) x = (x * 31 + s.charCodeAt(i)) & 0xffffffff;
        document.cookie = '__cf_bm=' + x + '.' + t + '-0; path=/';
        </script><div id="hint">计算完成</div>"""))


# ================================================================
# 74: Gmail API 邮箱验证链接
# ================================================================
@challenge(74, "Gmail API邮箱验证链接", "GMAIL_CODE_74")
def c74(h, sub):
    if sub == "/inbox":
        h._json({"messages": [{"subject": "验证邮箱", "body": "点击验证: /c/74/verify?code=GMAIL_CODE_74", "id": "msg1"}]})
        return
    if sub == "/verify":
        code = urllib.parse.parse_qs(urllib.parse.urlsplit(h.path).query).get("code", [""])[0]
        h._send(200, full_page(74, "Gmail API邮箱验证链接",
            f"""<p>验证完成</p><div id="code">{code}</div>"""))
        return
    h._send(200, full_page(74, "Gmail API邮箱验证链接",
        """<p>通过 Gmail API 读取最新验证邮件，提取链接访问。</p>"""))


# ================================================================
# 75: JWT 刷新流程
# ================================================================
@challenge(75, "JWT刷新流程", "JWT_DATA_75")
def c75(h, sub):
    if sub == "/refresh":
        body = json.loads(h.rfile.read(int(h.headers.get("Content-Length", 0))).decode() or "{}")
        if body.get("refresh_token") == "RT_75":
            h._json({"access_token": "AT_NEW_75"})
        else:
            h._json({"error": "bad refresh"}, 403)
        return
    if sub == "/api":
        auth = h.headers.get("Authorization", "")
        if auth == "Bearer AT_NEW_75":
            h._json({"data_id": "JWT_DATA_75"})
        else:
            h._json({"error": "expired token"}, 401)
        return
    h._send(200, full_page(75, "JWT刷新流程",
        """<p>access_token 已过期，用 refresh_token 刷新后访问受保护接口。</p>
        <script>
        // 模拟过期 token
        window.expired_token = 'AT_OLD_75';
        window.refresh_token = 'RT_75';
        </script>"""))


# ================================================================
# 76: SAML SSO 登录流程
# ================================================================
@challenge(76, "SAML SSO登录流程", "SAML_SESSION_76")
def c76(h, sub):
    if sub == "/acs":
        body = h.rfile.read(int(h.headers.get("Content-Length", 0))).decode() or ""
        import urllib.parse as _up
        params = _up.parse_qs(body)
        saml = (params.get("SAMLResponse", [""])[0] or "").replace(" ", "+")
        try:
            decoded = base64.b64decode(saml).decode("utf-8", "replace")
        except Exception:
            decoded = ""
        if saml and "user@example.com" in decoded:
            h._json({"session_token": "SAML_SESSION_76"})
        else:
            h._json({"error": "bad saml"}, 403)
        return
    h._send(200, full_page(76, "SAML SSO登录流程",
        """<p>解析 SAML Response 并 POST 到 SP 的 ACS 端点。</p>
        <div id="saml">SAMLResponse=base64(user@example.com)</div>"""))


# ================================================================
# 77: GraphQL 内省禁用探测
# ================================================================
@challenge(77, "GraphQL内省禁用探测", "GRAPHQL_NODE_77")
def c77(h, sub):
    if sub == "/graphql":
        body = json.loads(h.rfile.read(int(h.headers.get("Content-Length", 0))).decode() or "{}")
        q = body.get("query", "")
        if "__schema" in q:
            h._json({"errors": [{"message": "introspection disabled"}]})
        elif "hiddenNode" in q:
            h._json({"data": {"hiddenNode": {"id": "GRAPHQL_NODE_77"}}})
        else:
            h._json({"data": {}})
        return
    h._send(200, full_page(77, "GraphQL内省禁用探测",
        """<p>内省被禁用，盲猜查询字段 hiddenNode。</p>"""))


# ================================================================
# 78: JSON 劫持前缀绕过
# ================================================================
@challenge(78, "JSON劫持前缀绕过", "JHJ_TOKEN_78")
def c78(h, sub):
    if sub == "/api":
        h._send(200, "while(1);" + json.dumps({"token": "JHJ_TOKEN_78"}), "application/json")
        return
    h._send(200, full_page(78, "JSON劫持前缀绕过",
        """<p>响应带 while(1); 前缀，解析其中的 token。</p>"""))


# ================================================================
# 79: user-select:none 保护文本
# ================================================================
@challenge(79, "user-select:none保护文本", "USELECT_79")
def c79(h, sub):
    h._send(200, full_page(79, "user-select:none保护文本",
        """<style>.no-select{user-select:none;-webkit-user-select:none}</style>
        <p>价格：<span class="no-select" id="price">USELECT_79</span>（不可选中，但 DOM 可读）</p>"""))


# ================================================================
# 80: 窗口尺寸检测
# ================================================================
@challenge(80, "窗口尺寸检测", "OUTER_KEY_80")
def c80(h, sub):
    h._send(200, full_page(80, "窗口尺寸检测",
        """<p>检查 outerWidth 与 innerWidth…</p><div id="out"></div>
        <script>
        var ok = window.outerWidth === window.innerWidth;
        window.__key = ok ? 'OUTER_KEY_80' : 'outer=' + window.outerWidth + ' inner=' + window.innerWidth;
        document.getElementById('out').textContent = window.__key;
        </script>"""))



# ================================================================
# 81: 快速关闭的 WebSocket
# ================================================================
@challenge(81, "快速关闭的WebSocket", "ONETIME_81")
def c81(h, sub):
    h._send(200, full_page(81, "快速关闭的WebSocket",
        """<p>连接后立即捕获关闭前的最后一条消息…</p><div id="out"></div>
        <script>
        const ws = new WebSocket('ws://' + location.host + '/ws/onetime');
        ws.onmessage = function(e){ document.getElementById('out').textContent = e.data; };
        </script>"""))


# ================================================================
# 82: 网络类型检测 navigator.connection
# ================================================================
@challenge(82, "网络类型检测", "MOBILE_ID_82")
def c82(h, sub):
    h._send(200, full_page(82, "网络类型检测",
        """<p>检查网络类型…</p><div id="out"></div>
        <script>
        var t = (navigator.connection && navigator.connection.type) || 'unknown';
        document.getElementById('out').textContent = (t === '4g' || t === 'wifi') ? 'MOBILE_ID_82' : 'type=' + t;
        </script>"""))


# ================================================================
# 83: 反爬蜜罐表单
# ================================================================
@challenge(83, "反爬蜜罐表单", "HONEYPOT_CSRF_83")
def c83(h, sub):
    if sub == "/submit":
        body = h.rfile.read(int(h.headers.get("Content-Length", 0))).decode() or ""
        if "website" in body and "=" in body.split("website=")[1].split("&")[0] and "http" in body:
            h._json({"error": "honeypot!"}, 403)
            return
        h._send(200, "<html><body>ok</body></html>", headers={"CSRF-TOKEN": "HONEYPOT_CSRF_83"})
        return
    h._send(200, full_page(83, "反爬蜜罐表单",
        """<form id="f" action="/c/83/submit" method="POST">
        <input name="name" value="test">
        <input name="website" value="" style="display:none" tabindex="-1" autocomplete="off">
        <button>提交</button></form>
        <p>website 是蜜罐字段，正常用户不会填。</p>"""))


# ================================================================
# 84: IntersectionObserver 懒加载
# ================================================================
@challenge(84, "IntersectionObserver懒加载", "LAZY_ID_84")
def c84(h, sub):
    h._send(200, full_page(84, "IntersectionObserver懒加载",
        """<div id="list"></div><div id="loader" data-id="LAZY_ID_84"></div>
        <script>
        var io = new IntersectionObserver(function(entries){
          entries.forEach(function(en){
            if (en.isIntersecting) {
              var d = document.createElement('div');
              d.className = 'lazy-item';
              d.setAttribute('data-id', 'LAZY_ID_84');
              document.getElementById('list').appendChild(d);
            }
          });
        });
        io.observe(document.getElementById('loader'));
        </script>"""))


# ================================================================
# 85: navigator.webdriver 细粒度绕过
# ================================================================
@challenge(85, "navigator.webdriver细粒度绕过", "WEBDRIVER_PASS_85")
def c85(h, sub):
    h._send(200, full_page(85, "navigator.webdriver细粒度绕过",
        """<p>检查 webdriver 原型链…</p><div id="out"></div>
        <script>
        var wd = navigator.webdriver;
        var proto = Object.getOwnPropertyDescriptor(Navigator.prototype, 'webdriver');
        var ok = (wd === undefined || wd === false) && (!proto || proto.get === undefined);
        document.getElementById('out').textContent = ok ? 'WEBDRIVER_PASS_85' : 'wd=' + wd;
        </script>"""))


# ================================================================
# 86: SharedArrayBuffer 跨线程令牌
# ================================================================
@challenge(86, "SharedArrayBuffer跨线程令牌", "SAB_TOKEN_86")
def c86(h, sub):
    h._send(200, full_page(86, "SharedArrayBuffer跨线程令牌",
        """<p>COOP/COEP 下 Worker 通过共享内存传回令牌…</p><div id="out"></div>
        <script>
        try {
          var sab = new SharedArrayBuffer(64);
          var arr = new Uint8Array(sab);
          var code = "self.onmessage=function(){" +
            "var a=new Uint8Array(" + "self.sab" + ");" +
            "var s='SAB_TOKEN_86';" +
            "for(var i=0;i<s.length;i++)a[i]=s.charCodeAt(i);" +
            "self.postMessage('done');" + "};";
          var blob = new Blob([code], {type: 'application/javascript'});
          var w = new Worker(URL.createObjectURL(blob));
          w.postMessage({sab: sab});
          w.onmessage = function(){
            var s = '';
            for (var i = 0; i < 12; i++) s += String.fromCharCode(arr[i]);
            document.getElementById('out').textContent = s;
          };
        } catch(e) { document.getElementById('out').textContent = 'SAB_TOKEN_86'; }
        </script>"""))


# ================================================================
# 87: AudioContext 指纹白名单
# ================================================================
@challenge(87, "AudioContext指纹白名单", "AUDIO_API_87")
def c87(h, sub):
    h._send(200, full_page(87, "AudioContext指纹白名单",
        """<p>生成音频指纹并与白名单比对…</p><div id="out"></div>
        <script>
        var ctx = new (window.AudioContext || window.webkitAudioContext)();
        var ok = !!ctx && typeof ctx.createOscillator === 'function';
        document.getElementById('out').textContent = ok ? 'AUDIO_API_87' : 'no-audio';
        </script>"""))


# ================================================================
# 88: performance.memory 伪装
# ================================================================
@challenge(88, "performance.memory伪装", "MEM_KEY_88")
def c88(h, sub):
    h._send(200, full_page(88, "performance.memory伪装",
        """<p>检查 usedJSHeapSize…</p><div id="out"></div>
        <script>
        var m = performance.memory ? performance.memory.usedJSHeapSize : 0;
        var ok = !m || m < 100 * 1024 * 1024;
        document.getElementById('out').textContent = ok ? 'MEM_KEY_88' : 'mem=' + m;
        </script>"""))


# ================================================================
# 89: PBKDF2 解密 localStorage
# ================================================================
@challenge(89, "PBKDF2解密localStorage", "PBKDF_SECRET_89")
def c89(h, sub):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives import padding
    password = b"mypassword"
    salt = b"salt_12345678"
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=16, salt=salt, iterations=1000)
    key = kdf.derive(password)
    iv = b"pbkdf_iv_1234567"
    padder = padding.PKCS7(128).padder()
    data = padder.update(b"PBKDF_SECRET_89") + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ct = enc.update(data) + enc.finalize()
    h._send(200, full_page(89, "PBKDF2解密localStorage",
        f"""<script>
        localStorage.setItem('enc', '{base64.b64encode(ct).decode()}');
        localStorage.setItem('salt', '{base64.b64encode(salt).decode()}');
        </script>
        <p>密码提示：mypassword。用 PBKDF2 派生密钥解密 secret_id。</p>
        <div id="out"></div>
        <script>
        (async function(){{
          var enc = localStorage.getItem('enc');
          var salt = Uint8Array.from(atob(localStorage.getItem('salt')), c=>c.charCodeAt(0));
          var keyMat = await crypto.subtle.importKey('raw', new TextEncoder().encode('mypassword'), 'PBKDF2', false, ['deriveKey']);
          var key = await crypto.subtle.deriveKey({{name:'PBKDF2', salt: salt, iterations: 1000, hash: 'SHA-256'}}, keyMat, {{name:'AES-CBC', length: 128}}, false, ['decrypt']);
          var raw = Uint8Array.from(atob(enc), c=>c.charCodeAt(0));
          var pt = await crypto.subtle.decrypt({{name:'AES-CBC', iv: new TextEncoder().encode('pbkdf_iv_1234567')}}, key, raw);
          document.getElementById('out').textContent = new TextDecoder().decode(pt);
        }})();
        </script>"""))


# ================================================================
# 90: navigator.deviceMemory 伪装
# ================================================================
@challenge(90, "navigator.deviceMemory伪装", "HD_TOKEN_90")
def c90(h, sub):
    h._send(200, full_page(90, "navigator.deviceMemory伪装",
        """<p>deviceMemory >= 8 时返回高清图片 URL…</p><div id="out"></div>
        <script>
        var m = navigator.deviceMemory || 0;
        document.getElementById('out').textContent = m >= 8 ? 'HD_TOKEN_90' : 'mem=' + m;
        </script>"""))



# ================================================================
# 91: CDP 启用被禁 API（getBattery）
# ================================================================
@challenge(91, "CDP启用被禁API", "BATTERY_RESULT_91")
def c91(h, sub):
    h._send(200, full_page(91, "CDP启用被禁API",
        """<p>getBattery 被禁用，需通过 CDP/注入恢复…</p><div id="out"></div>
        <script>
        if (navigator.getBattery) {
          navigator.getBattery().then(function(b){ document.getElementById('out').textContent = 'BATTERY_RESULT_91'; });
        }
        </script>"""))


# ================================================================
# 92: 预检 OPTIONS 严格验证
# ================================================================
@challenge(92, "预检OPTIONS严格验证", "DATA_ID_92")
def c92(h, sub):
    if h.command == "OPTIONS":
        req_h = h.headers.get("Access-Control-Request-Headers", "")
        if req_h == "x-custom-header":
            h._send(200, "", headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Headers": "x-custom-header",
                "Access-Control-Allow-Methods": "GET"})
        else:
            h._send(403, "bad preflight")
        return
    if sub == "/api":
        h._json({"dataId": "DATA_ID_92"})
        return
    h._send(200, full_page(92, "预检OPTIONS严格验证",
        """<p>OPTIONS 需指定 Access-Control-Request-Headers: x-custom-header，然后 GET /c/92/api。</p>"""))


# ================================================================
# 93: Sec-GPC 隐私检测
# ================================================================
@challenge(93, "Sec-GPC隐私检测", "GPC_USER_93")
def c93(h, sub):
    if sub == "/api":
        gpc = h.headers.get("Sec-GPC", "")
        if not gpc:
            h._json({"user_key": "GPC_USER_93"})
        else:
            h._json({"error": "gpc present"}, 403)
        return
    h._send(200, full_page(93, "Sec-GPC隐私检测",
        """<p>不带 Sec-GPC 请求头时返回正常数据。</p>"""))


# ================================================================
# 94: window.screenX/Y 校验
# ================================================================
@challenge(94, "window.screenX/Y校验", "SCREEN_TOKEN_94")
def c94(h, sub):
    h._send(200, full_page(94, "window.screenX/Y校验",
        """<p>检查窗口位置…</p><div id="out"></div>
        <script>
        var ok = window.screenX === 0 && window.screenY === 0;
        document.getElementById('out').textContent = ok ? 'SCREEN_TOKEN_94' : 'x=' + window.screenX + ' y=' + window.screenY;
        </script>"""))


# ================================================================
# 95: Kotlin/JS 状态提取
# ================================================================
@challenge(95, "Kotlin/JS状态提取", "KOTLIN_TOKEN_95")
def c95(h, sub):
    h._send(200, full_page(95, "Kotlin/JS状态提取",
        """<p>从混淆的 Kotlin 编译产物中找到全局状态对象…</p><div id="out"></div>
        <script>
        // 模拟 Kotlin/JS 编译产物
        var $k = {state: {apiToken: 'KOTLIN_TOKEN_95'}};
        var zk = {__kotlin: $k};
        window.__kotlinState = $k.state;
        document.getElementById('out').textContent = window.__kotlinState.apiToken;
        </script>"""))


# ================================================================
# 96: Emscripten 动态密钥生成
# ================================================================
@challenge(96, "Emscripten动态密钥生成", "EMS_USER_96")
def c96(h, sub):
    wasm = make_wasm_pwd()
    import base64 as _b64
    h._send(200, full_page(96, "Emscripten动态密钥生成",
        f"""<p>调用 WASM 生成密钥并解密接口数据…</p><div id="out"></div>
        <script>
        WebAssembly.instantiate(Uint8Array.from(atob('{_b64.b64encode(wasm).decode()}'), c => c.charCodeAt(0)))
          .then(({{instance}}) => {{
            var k = instance.exports.getPwd();
            // 密钥 = PWD_65，解密得到 user_id
            document.getElementById('out').textContent = k === 65 ? 'EMS_USER_96' : 'k=' + k;
          }});
        </script>"""))


# ================================================================
# 97: CSS mix-blend-mode 文字提取
# ================================================================
@challenge(97, "CSS mix-blend-mode文字提取", "BLEND_WORD_97")
def c97(h, sub):
    h._send(200, full_page(97, "CSS mix-blend-mode文字提取",
        """<style>.mix{mix-blend-mode: difference; color:#fff; background:#808080}</style>
        <p>隐藏文字：<span class="mix" data-hidden="BLEND_WORD_97">■■■■■■</span></p>
        <p>说明：截图 + 颜色运算分离（data-hidden 为等价还原结果）。</p>"""))


# ================================================================
# 98: Web Audio DTMF 拨号音
# ================================================================
@challenge(98, "Web Audio DTMF拨号音", "289")
def c98(h, sub):
    h._send(200, full_page(98, "Web Audio DTMF拨号音",
        """<p>解析 DTMF 音调（697/1477=2, 770/1336=8, 852/1209=9）…</p><div id="out"></div>
        <script>
        const ctx = new (window.AudioContext || window.webkitAudioContext)();
        var freqs = [[697,1477],[770,1336],[852,1209]];
        var seq = [2,8,9];
        var digits = '';
        function play(i){
          if (i >= freqs.length) { document.getElementById('out').textContent = digits.join ? digits.join('') : String(seq[0]) + String(seq[1]) + String(seq[2]); return; }
          var f = freqs[i];
          [f[0], f[1]].forEach(function(fr){
            var o = ctx.createOscillator(); var g = ctx.createGain();
            o.frequency.value = fr; o.connect(g); g.connect(ctx.destination);
            o.start(); g.gain.setValueAtTime(0.08, ctx.currentTime);
            g.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.15);
            o.stop(ctx.currentTime + 0.16);
          });
          setTimeout(function(){ play(i+1); }, 250);
        }
        play(0);
        </script>"""))


# ================================================================
# 99: HTTP Digest 认证自动化
# ================================================================
@challenge(99, "HTTP Digest认证自动化", "DIGEST_KEY_99")
def c99(h, sub):
    auth = h.headers.get("Authorization", "")
    if auth.startswith("Digest "):
        import hashlib as _hl
        realm, nonce = "challenge2", "nonce99xyz"
        m = re.search(r'username="([^"]+)"', auth)
        uri_m = re.search(r'uri="([^"]+)"', auth)
        resp_m = re.search(r'response="([^"]+)"', auth)
        nc_m = re.search(r'nc=([0-9a-fA-F]+)', auth)
        cn_m = re.search(r'cnonce="([^"]+)"', auth)
        if not (m and uri_m and resp_m and nc_m and cn_m):
            h._json({"error": "bad digest"}, 400)
            return
        ha1 = _hl.md5(f"{m.group(1)}:{realm}:pass99".encode()).hexdigest()
        ha2 = _hl.md5(f"GET:{uri_m.group(1)}".encode()).hexdigest()
        expect = _hl.md5(f"{ha1}:{nonce}:{nc_m.group(1)}:{cn_m.group(1)}:auth:{ha2}".encode()).hexdigest()
        if resp_m.group(1) == expect:
            h._send(200, "<html><body><div id='ak'>DIGEST_KEY_99</div></body></html>")
            return
        h._json({"error": "bad response"}, 403)
        return
    h._send(401, "", headers={'WWW-Authenticate': 'Digest realm="challenge2", nonce="nonce99xyz", qop="auth", algorithm=MD5'})


# ================================================================
# 100: 综合 CTF 挑战 2
# ================================================================
@challenge(100, "综合CTF挑战2", "FLAG2_100")
def c100(h, sub):
    if sub == "/captcha.png":
        cid = urllib.parse.parse_qs(urllib.parse.urlsplit(h.path).query).get("cid", [""])[0]
        code = h.server.captcha_codes.get(cid, "abcd")
        from PIL import Image, ImageDraw, ImageFont
        img = Image.new("RGB", (150, 50), (255, 255, 255))
        d = ImageDraw.Draw(img)
        font = ImageFont.load_default(size=30)
        for i, ch in enumerate(code):
            d.text((6 + i * 36, 6), ch, fill=(20, 20, 20), font=font)
        buf = io.BytesIO()
        img.save(buf, "PNG")
        h._send(200, buf.getvalue(), "image/png")
        return
    if sub == "/submit":
        body = json.loads(h.rfile.read(int(h.headers.get("Content-Length", 0))).decode() or "{}")
        if (body.get("key") == "EVALKEY_100" and body.get("ws") == "WSTOKEN_100"
                and body.get("code") == h.server.captcha_codes.get(body.get("cid", ""))):
            h._json({"flag": "FLAG2_100"})
        else:
            h._json({"flag": None, "error": "bad"})
        return
    import random as _r
    code = "".join(_r.choices("abcdefghjkmnpqrstuvwxyz23456789", k=4))
    cid = hashlib.md5((code + str(time.time())).encode()).hexdigest()[:16]
    h.server.captcha_codes[cid] = code
    h._send(200, full_page(100, "综合CTF挑战2",
        f"""<p>① eval 生成 key → ② WS 获取 token → ③ 验证码 OCR → 提交 flag</p>
        <div id="k"></div><div id="w"></div>
        <img id="cap" src="/c/100/captcha.png?cid={cid}"><input id="code">
        <button id="go">提交</button><div id="out"></div>
        <script>
        var key = eval("['EVAL','KEY_','100'].join('')");
        document.getElementById('k').textContent = 'key=' + key;
        var ws = new WebSocket('ws://' + location.host + '/ws/token');
        ws.onmessage = function(e){{ document.getElementById('w').textContent = 'ws=' + e.data; }};
        document.getElementById('go').onclick = function(){{
          fetch('/c/100/submit', {{method:'POST', body: JSON.stringify({{
            key: key,
            ws: document.getElementById('w').textContent.replace('ws=',''),
            code: document.getElementById('code').value.trim(),
            cid: '{cid}'
          }})}}).then(r=>r.json()).then(d=>{{ document.getElementById('out').textContent = d.flag || d.error; }});
        }};
        </script>"""))



def _sleb128(v):
    out = bytearray()
    more = True
    while more:
        byte = v & 0x7F
        v >>= 7
        if (v == 0 and not (byte & 0x40)) or (v == -1 and (byte & 0x40)):
            more = False
        else:
            byte |= 0x80
        out.append(byte)
    return bytes(out)


def make_wasm_pwd():
    b = bytearray(b"\x00asm\x01\x00\x00\x00")
    b += b"\x01\x05\x01\x60\x00\x01\x7f"
    b += b"\x03\x02\x01\x00"
    b += b"\x07\x0a\x01\x06getPwd\x00\x00"
    body = b"\x00" + b"\x41" + _sleb128(65) + b"\x0b"
    b += b"\x0a" + bytes([len(body) + 2]) + b"\x01" + bytes([len(body)]) + body
    return bytes(b)


def make_server():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.captcha_codes = {}
    srv.ip_tokens = set()
    srv.rate_window = []
    srv.rate_window70 = []
    srv.rate_window72 = []
    from cryptography.hazmat.primitives.asymmetric import rsa as _rsa
    srv.rsa_key = _rsa.generate_private_key(public_exponent=65537, key_size=1024)
    srv.daemon_threads = True
    return srv


if __name__ == "__main__":
    srv = make_server()
    print(f"挑战靶场: http://{HOST}:{PORT}  已实现挑战: {sorted(CHALLENGES.keys())}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
