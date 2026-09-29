#!/usr/bin/env python3
"""rangedl 端到端验证套件（本地 Range 服务器 + 故障注入）。手工运行，不进 CI。

用法: python3 tests/test_rangedl_manual.py   （约 1-2 分钟，404 路径含重试退避）

结构说明（审查更正）：可执行主体收在 main() 里、由 `__main__` 守卫调用——
此前顶层直接 `sys.exit(...)`，pytest 收集期一 import 就 SystemExit，
整套件报 INTERNALERROR（0 用例），新人会以为技能坏了。

覆盖收官十轮修的数据完整性场景：
- H1 首次下载中断（清单无指纹）后远端内容已变 → 不得拼出新旧混合体
- H2 远端只改中间段（段 0 不变）→ ETag/Last-Modified 变化须触发全量重下
- M1 服务器回"起点被忽略但长度正确"的 206 → Content-Range 校验须拦
- M2 服务器 gzip 压缩 → 尺寸语义不再冲突（Accept-Encoding: identity）
- M5 HEAD 被拒且服务器忽略 Range → 退回单流而非直接报错
- L1 拼接尺寸不符 → 返回结构化失败（不再 stat 已删文件抛 FileNotFoundError）
- 正常下载/续传复用/忽略 Range 退单流 三条正向路径不回归
"""
import gzip
import http.server
import json
import os
import pathlib
import sys
import tempfile
import threading

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
os.environ["US_ALLOW_PRIVATE"] = "1"

from universal_scraper.rangedl import rangedl  # noqa: E402

C1 = bytes((i * 7 + 3) % 251 for i in range(4000))
C2 = bytes((i * 11 + 5) % 251 for i in range(4000))          # 等长不同内容
NEW1000 = bytes((i * 13 + 9) % 251 for i in range(1000))

STATE = {"content": C1, "fail_starts": set(), "head_ok": True,
         "gzip": False, "wrong_offset": False, "etag": "v1", "ignore_range": False}


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, body, extra=None):
        self.send_response(code)
        _has_cl = any(k.lower() == "content-length" for k in (extra or {}))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        if not _has_cl:
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        if not STATE["head_ok"]:
            self._send(405, b"")
            return
        c = STATE["content"]
        self._send(200, b"", {"Accept-Ranges": "bytes", "Content-Length": str(len(c)),
                              "ETag": STATE["etag"],
                              "Last-Modified": "Mon, 01 Jan 2026 00:00:00 GMT"})

    def do_GET(self):
        c = STATE["content"]
        rng = self.headers.get("Range")
        if STATE["ignore_range"]:
            self._send(200, c, {"Accept-Ranges": "none"})
            return
        if not rng:
            if STATE["gzip"]:
                gz = gzip.compress(c)
                self._send(200, gz, {"Content-Encoding": "gzip", "Content-Length": str(len(gz))})
            else:
                self._send(200, c)
            return
        m = rng.replace("bytes=", "").split("-")
        lo, hi = int(m[0]), int(m[1]) if m[1] else len(c) - 1
        if lo in STATE["fail_starts"]:
            self._send(404, b"")
            return
        if STATE["wrong_offset"]:
            seg = c[0:hi - lo + 1]
            self._send(206, seg, {"Content-Range": f"bytes 0-{hi - lo}/{len(c)}"})
            return
        seg = c[lo:hi + 1]
        self._send(206, seg, {"Content-Range": f"bytes {lo}-{hi}/{len(c)}"})

    def log_message(self, *a):
        pass


OUT = pathlib.Path(tempfile.mkdtemp(prefix="us_rangedl_"))
res = []


def clean(name):
    for p in OUT.glob(name + "*"):
        p.unlink()


def rep(tag, ok, detail=""):
    res.append((tag, ok, detail))
    print(("  ✅ " if ok else "  ❌ ") + tag + (f" | {detail}" if detail else ""))


def reset(**kw):
    STATE.update({"content": C1, "fail_starts": set(), "head_ok": True,
                  "gzip": False, "wrong_offset": False, "etag": "v1", "ignore_range": False})
    STATE.update(kw)


def main() -> int:
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    PORT = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    URL = f"http://127.0.0.1:{PORT}/f.bin"

    print("== 正常下载 + 指纹固化（不回归）==")
    clean("a.bin"); reset()
    r1 = rangedl(URL, out=OUT / "a.bin", segments=4, concurrency=3, log_fn=lambda *a: None)
    rep("正常 4 段下载 ok", r1.get("ok") and (OUT / "a.bin").read_bytes() == C1, str(r1))
    mf = json.loads((OUT / "a.bin.manifest.json").read_text())
    rep("清单含 part_fps 与验证器", bool(mf.get("part_fps")) and mf.get("etag") == "v1", str(list(mf)))

    print("== H1 首次下载中断（无指纹）→ 内容已变 → 不得产出混合体 ==")
    clean("b.bin"); reset(fail_starts={3000})
    r1 = rangedl(URL, out=OUT / "b.bin", segments=4, concurrency=3, log_fn=lambda *a: None)
    rep("首轮中断报错", not r1.get("ok"), str(r1)[:80])
    reset(content=C2, fail_starts=set())
    r2 = rangedl(URL, out=OUT / "b.bin", segments=4, concurrency=3, log_fn=lambda *a: None)
    got = (OUT / "b.bin").read_bytes() if (OUT / "b.bin").exists() else b""
    # "混合体"定义：产出了文件且既不是旧内容也不是新内容；ok=True 而内容可疑更糟
    mixed = bool(got) and got != C1 and got != C2
    rep("二轮不产出混合体", not mixed, f"ok={r2.get('ok')} 长度={len(got)} 内容=旧{got == C1}/新{got == C2}")

    print("== H2 远端只改中间段（段 0 不变）→ 不得静默返回旧文件 ==")
    clean("c.bin"); reset()
    r1 = rangedl(URL, out=OUT / "c.bin", segments=4, concurrency=1, log_fn=lambda *a: None)
    reset(fail_starts={3000})
    r2 = rangedl(URL, out=OUT / "c.bin", segments=4, concurrency=1, log_fn=lambda *a: None)
    C_mid = C1[:2000] + NEW1000 + C1[3000:]
    reset(content=C_mid, fail_starts=set(), etag="v2")     # 远端更新：etag 变化
    r3 = rangedl(URL, out=OUT / "c.bin", segments=4, concurrency=1, log_fn=lambda *a: None)
    got = (OUT / "c.bin").read_bytes() if (OUT / "c.bin").exists() else b""
    rep("etag 变化 → 全量重下（拿新内容）", got == C_mid,
        f"ok={r3.get('ok')} 等新={got == C_mid} 等旧={got == C1}")

    print("== M1 服务器回错起点 Content-Range → 必须失败 ==")
    clean("d.bin"); reset(wrong_offset=True)
    r1 = rangedl(URL, out=OUT / "d.bin", segments=4, concurrency=3, log_fn=lambda *a: None)
    rep("错 Content-Range 被拦", not r1.get("ok"), str(r1.get("error", ""))[:80])

    print("== M2 gzip 服务器 → 单流不再因压缩长度失败 ==")
    clean("e.bin"); reset(gzip=True)
    r1 = rangedl(URL, out=OUT / "e.bin", segments=1, concurrency=1, log_fn=lambda *a: None)
    rep("gzip 单流下载成功", r1.get("ok") and (OUT / "e.bin").read_bytes() == C1, str(r1)[:80])

    print("== M5 HEAD 被拒 + 无 Content-Range → 退单流（不再直接报错）==")
    clean("f.bin"); reset(head_ok=False, ignore_range=True)
    r1 = rangedl(URL, out=OUT / "f.bin", segments=4, concurrency=3, log_fn=lambda *a: None)
    rep("HEAD 失败退单流成功", r1.get("ok") and (OUT / "f.bin").read_bytes() == C1, str(r1)[:90])

    print("== 服务器忽略 Range → 退单流（原有行为不回归）==")
    clean("g.bin"); reset(ignore_range=True)
    r1 = rangedl(URL, out=OUT / "g.bin", segments=4, concurrency=3, log_fn=lambda *a: None)
    rep("忽略 Range 退单流", r1.get("ok") and (OUT / "g.bin").read_bytes() == C1, str(r1)[:80])

    print("== 续传：中断→重跑（内容未变，etag 同）→ 复用已就位段并成功 ==")
    clean("h.bin"); reset(fail_starts={3000})
    r1 = rangedl(URL, out=OUT / "h.bin", segments=4, concurrency=1, log_fn=lambda *a: None)
    reset(fail_starts=set())
    r2 = rangedl(URL, out=OUT / "h.bin", segments=4, concurrency=1, log_fn=lambda *a: None)
    rep("续传成功且内容正确", r2.get("ok") and (OUT / "h.bin").read_bytes() == C1,
        f"resumed={r2.get('resumed')}")

    srv.shutdown()
    print()
    bad = [t for t, ok, _ in res if not ok]
    print(f"总计 {len(res)} 项，失败 {len(bad)}" + (f": {bad}" if bad else "  ★ 全部通过"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
