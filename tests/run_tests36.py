#!/usr/bin/env python3
"""第二十五轮回归（9 个）：第二轮 review 修复——
代理反馈回调打通、死代理快速失败、login 不算封禁、config 自定义解析器、
WebUI 进度回传（_notify / engine log_cb）、浏览器桥停止信号。
用法: python3 tests/run_tests36.py
"""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from universal_scraper.proxy import ProxyPool  # noqa: E402
from universal_scraper.session import SessionPool  # noqa: E402
from universal_scraper.config import validate_task, ConfigError  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  {detail}" if detail and not cond else ""))


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/loginwall"):
            body = "请先登录 扫码登录 账号登录".encode("utf-8")
        elif self.path.startswith("/list"):
            body = "".join(f"<div class='i'><span class='t'>item-{i}</span></div>" for i in range(10)).encode("utf-8")
        else:
            body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    from universal_scraper.protocols import Request

    print("== #1 代理反馈回调打通 ==")
    pp = ProxyPool(["http://127.0.0.1:1", "http://127.0.0.1:2"], cooldown=60)
    sp = SessionPool(proxies=None, rotate_on_errors=2)
    sp._proxy_source = pp.next
    sp._proxy_ok_cb = pp.mark_ok
    sp._proxy_fail_cb = pp.mark_fail
    s1 = sp.acquire(base + "/")
    sp.report(s1, ok=False, blocked=True)
    check("失败回调触发 ProxyPool 冷却", pp.alive_count < pp.size, f"{pp.alive_count}/{pp.size}")
    check("冷却的是实际用到的代理", pp._last_fail == s1.proxy, str(pp._last_fail))
    s2 = sp.acquire(base + "/")
    sp.report(s2, ok=False, blocked=True)
    check("两个代理都冷却", pp.alive_count == 0, f"{pp.alive_count}/{pp.size}")
    sp._proxy_ok_cb(pp.proxies[0])
    check("成功回调恢复代理", pp.alive_count == 1, f"{pp.alive_count}/{pp.size}")

    print("== #3 死代理快速失败 + 标记 ==")
    from universal_scraper.modules.fetchers import HttpFetcher
    f = HttpFetcher({"type": "http", "url": base + "/x"}, {},
                    {"min_interval": 0.01, "proxies": ["http://127.0.0.1:1"]})
    pp2 = f.proxy_pool
    import time
    t0 = time.time()
    raised = False
    try:
        f.fetch(Request(url=base + "/list"))
    except Exception:
        raised = True
    dt = time.time() - t0
    check("死代理快速失败（<8s）", raised and dt < 8, f"raised={raised} dt={dt:.1f}s")
    check("死代理已标记冷却", pp2.alive_count == 0, f"{pp2.alive_count}/{pp2.size}")

    print("== #5 login 不算会话封禁 ==")
    f2 = HttpFetcher({"type": "http", "url": base + "/loginwall"}, {"min_interval": 0.01}, {})
    resp = f2.fetch(Request(url=base + "/loginwall"))
    sess = f2._cur_session
    check("login 墙返回正常 Response", resp.status == 200 and "登录" in resp.text, resp.text[:40])
    check("login 不污染会话", sess.errors == 0, f"errors={sess.errors}")

    print("== #8 config 自定义解析器 ==")
    base_cfg = {
        "name": "t", "start_urls": ["http://x/"], "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/", "parser": "default"}],
        "parsers": {"default": {"type": "custom_my_parser"}}, "storage": {"type": "jsonl"},
    }
    try:
        validate_task(dict(base_cfg), has_custom_parser=True)
        check("有自定义解析器时放行", True)
    except ConfigError as e:
        check("有自定义解析器时放行", False, str(e))
    try:
        validate_task(dict(base_cfg), has_custom_parser=False)
        check("无自定义解析器时拒绝", False, "未拒绝")
    except ConfigError:
        check("无自定义解析器时拒绝", True)

    print("== #4 WebUI 进度回传 ==")
    from universal_scraper.modules.fetchers import BrowserFetcher
    msgs = []
    bf = BrowserFetcher({"type": "browser", "url": base + "/x"}, {}, {"_log_cb": msgs.append})
    bf._notify("⚠️ 请在弹出的浏览器中完成验证")
    check("_notify 进 job 消息", any("请在弹出的浏览器中完成验证" in m for m in msgs), str(msgs))

    from universal_scraper.engine_v3 import run_task
    td = ROOT / "outputs" / ".test_tmp" / "notify_task"
    (td / "modules").mkdir(parents=True, exist_ok=True)
    (td / "config.json").write_text(json.dumps({
        "name": "notify_task", "start_urls": [base + "/list"],
        "queue": {"max_depth": 1, "max_requests": 1, "max_concurrency": 1},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/", "parser": "default"}],
        "parsers": {"default": {"type": "html", "row_css": ".i", "fields": {"t": {"css": ".t::text"}}}},
        "storage": {"type": "jsonl", "name": "notify_task"},
        "output": {"dir": "outputs/.test_tmp", "base_name": "notify_task"},
        "anti_bot": {"min_interval": 0.01},
    }), encoding="utf-8")
    eng_msgs = []
    run_task(td, log_cb=eng_msgs.append)
    check("engine log_cb 收到启动/完成", any("任务启动" in m for m in eng_msgs)
          and any("完成" in m for m in eng_msgs), str(eng_msgs[:3]))

    print()
    print(f"===== 结果: {len(PASS)}/{len(PASS)+len(FAIL)} 通过 =====")
    if FAIL:
        for n, d in FAIL:
            print("  ❌", n, d)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
