#!/usr/bin/env python3
"""内置取数器：HTTP（连接池） / 浏览器 / 桥。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

from ..protocols import BaseFetcher, Request, Response


class HttpFetcher(BaseFetcher):
    """HTTP 取数器：curl_cffi（TLS 指纹伪装）→ requests → urllib 自动选后端；
    支持代理池轮换（anti._proxy_pool）与 429 Retry-After 限流重试。"""
    name = "http"

    def __init__(self, config, task_vars, anti):
        super().__init__(config, task_vars, anti)
        self._cache: dict = {} if config.get("cache") else None
        from ..core import make_http_client
        self.client = make_http_client(anti)
        self.proxy_pool = anti.get("_proxy_pool")
        if self.proxy_pool is None and anti.get("proxies"):
            from ..proxy import ProxyPool
            self.proxy_pool = ProxyPool(anti.get("proxies"), anti.get("proxy_mode", "round_robin"))
        self._last_proxy = None

    def _pick_proxy(self) -> str:
        """从代理池取一个可用代理；没有则直连（None）。"""
        if self.proxy_pool is None:
            return self.anti.get("proxy") or None
        p = self.proxy_pool.next()
        self._last_proxy = p
        return p

    def _report_proxy(self, ok: bool) -> None:
        if self.proxy_pool is not None and self._last_proxy:
            if ok:
                self.proxy_pool.mark_ok(self._last_proxy)
            else:
                self.proxy_pool.mark_fail(self._last_proxy)
            self._last_proxy = None

    def _sign_headers(self, req: Request) -> dict:
        """请求头：source.headers 静态头 + 签名注入钩子（source.sign_hook = "module:function"，
        函数签名 fn(headers, url, body) -> headers，用于 x-s / x-t 等签名头）。"""
        headers = dict(req.headers or {})
        headers.update(self.config.get("headers", {}) or {})
        hook = self.config.get("sign_hook")
        if not hook:
            return headers
        mod_name, fn_name = hook.split(":", 1)
        import importlib
        try:
            fn = getattr(importlib.import_module(mod_name), fn_name)
        except Exception as e:
            raise RuntimeError(f"签名钩子导入失败 {hook}: {e}")
        return fn(headers, req.url, req.body) or headers

    def fetch(self, req: Request) -> Response:
        if self._cache is not None and req.method == "GET":
            hit = self._cache.get(req.url)
            if hit is not None:
                return hit
        url = self._build_url(req)
        headers = self._sign_headers(req)
        proxy = self._pick_proxy()
        try:
            res = self.client.request(url, method=req.method, data=req.body,
                                      headers=headers, allow_html_404=True, proxy=proxy)
        except Exception:
            # 客户端抛异常（网络层/自定义客户端）：同样标记代理失败后继续抛出
            self._report_proxy(False)
            raise
        # 请求失败：抛错交给引擎队列重试（客户端内部重试耗尽后不再静默吞掉）
        if not res.get("ok"):
            status = res.get("status", 0)
            self._report_proxy(False)
            from ..protocols import RateLimitedError
            if status == 429:
                ra = self._retry_after(res.get("headers") or {})
                raise RateLimitedError(req.url, retry_after=ra, status=429)
            raise RuntimeError(f"HTTP {status}: {str(res.get('text', ''))[:200]}")
        self._report_proxy(True)
        body = res.get("body", b"")
        text = res.get("text", "")
        # 响应大小限制：防内存爆（max_size 字节，默认 20MB）
        max_size = int(self.config.get("max_size", 20 * 1024 * 1024))
        if len(body) > max_size:
            body = body[:max_size]
            text = text[:max_size]
            res["truncated"] = True
        resp = Response(request=req, status=res.get("status", 0),
                        body=body, text=text,
                        json=res.get("json"), url=res.get("url", req.url))
        if self._cache is not None and req.method == "GET" and res.get("ok"):
            self._cache[req.url] = resp
        return resp

    def _build_url(self, req: Request) -> str:
        """合并 source.query 静态参数 + 请求 meta.params（分页等）到 URL。"""
        import urllib.parse
        params = dict(self.config.get("query", {}) or {})
        params.update(req.meta.get("params", {}) or {})
        if not params:
            return req.url
        parts = urllib.parse.urlsplit(req.url)
        # 同名参数替换（分页时把旧 page 换成新 page），避免 ?page=1&page=2
        qdict = dict(urllib.parse.parse_qsl(parts.query))
        for k, v in params.items():
            qdict[k] = str(v)
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path,
                                        urllib.parse.urlencode(qdict), parts.fragment))

    @staticmethod
    def _retry_after(headers: dict) -> float:
        """解析 Retry-After（秒 或 HTTP-date）。"""
        import email.utils
        v = (headers or {}).get("retry-after", "")
        if not v:
            return 0.0
        v = str(v).strip()
        try:
            return max(0.0, float(v))
        except ValueError:
            try:
                dt = email.utils.parsedate_to_datetime(v)
                import time as _t
                return max(0.0, (dt.timestamp() - _t.time()))
            except Exception:
                return 0.0


class BridgeFetcher(BaseFetcher):
    """桥取数器：调用 Node 桥（过 WAF / 驱动 Vue 等复杂页面），一次性返回整批记录。

    任务配置 source: {"type": "bridge", "bridge": "../scripts/ggzy_bridge.cjs",
                      "bridge_params": {"keyword": "{{keyword}}", "stage": "{{stage}}"}}
    桥协议（stdout JSONL）：{"type":"meta"|"page"|"captcha"|"error"|"done", ...}
    验证码：自动解（captchaDir 协议，见 antibot）。
    """
    name = "bridge"

    def __init__(self, config, task_vars, anti):
        super().__init__(config, task_vars, anti)
        self.bridge = config.get("bridge")
        self.params = config.get("bridge_params", {})
        self.task_dir = config.get("_task_dir")

    def fetch(self, req: Request) -> Response:
        raise NotImplementedError("桥取数器是一次性取整批（fetch_all），不走请求队列")

    def fetch_all(self) -> list:
        """调用桥，返回全部原始记录。"""
        import json as _json, subprocess, re as _re
        bridge_path = Path(self.bridge)
        if not bridge_path.is_absolute():
            cands = []
            if self.task_dir:
                cands.append(Path(self.task_dir) / self.bridge)
                cands.append(Path(self.task_dir) / "modules" / self.bridge)
            cands.append(Path(__file__).resolve().parent.parent.parent / self.bridge)
            bridge_path = next((c for c in cands if c.exists()), cands[0])
        # 模板替换
        params = {}
        for k, v in (self.params or {}).items():
            if isinstance(v, str):
                v = _re.sub(r"\{\{(\w+)\}\}", lambda m: str(self.vars.get(m.group(1), m.group(0))), v)
            params[k] = v
        cap_dir = Path(self.anti.get("captcha_dir") or "/tmp/us_captcha")
        cap_dir.mkdir(parents=True, exist_ok=True)
        params.setdefault("captchaDir", str(cap_dir))
        cmd = [_NODE_BIN, str(bridge_path)]
        for k, v in params.items():
            if v is not None:
                cmd += [f"--{k}", str(v)]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8",
                                env={**os.environ, "NODE_PATH": _NODE_PATH})
        records = []
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                obj = _json.loads(line)
            except Exception:
                continue
            t = obj.get("type")
            if t == "page":
                records.extend(obj.get("records") or [])
            elif t == "captcha" and obj.get("imageFile"):
                from ..antibot import solve_captcha_file
                solve_captcha_file(obj["imageFile"], self.anti,
                                   answer_file=str(obj["imageFile"]) + ".answer", seq=obj.get("seq", 0))
            elif t == "error":
                proc.terminate()
                raise RuntimeError(obj.get("message"))
        rc = proc.wait(timeout=1800)
        if rc != 0:
            err = proc.stderr.read() if proc.stderr else ""
            raise RuntimeError(f"桥退出码 {rc}: {err[-300:]}")
        return records


class BrowserFetcher(BaseFetcher):
    """浏览器取数器（会话池，长驻复用）。

    - 默认用长驻浏览器池（scripts/browser_pool.cjs）：一次启动、多页复用，比每次重启快数倍
    - pool=false 时回退单页桥（scripts/browser_single.cjs）
    适用：SPA / JS 渲染 / 多页面的浏览器任务（配合 v3 parser 的 CSS/LLM 解析）。
    """
    name = "browser"

    def __init__(self, config, task_vars, anti):
        super().__init__(config, task_vars, anti)
        self.scripts_dir = Path(__file__).resolve().parent.parent.parent / "scripts"
        self.session_dir = Path(anti.get("session_dir") or "/tmp/us_session")
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self._pool = None
        self._pool_enabled = config.get("pool", True)
        self._req_id = 0
        self._pending: dict = {}
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        # 代理：单代理走池（启动时注入）；多代理走单页桥逐请求轮换
        self.proxy_pool = anti.get("_proxy_pool")
        if self.proxy_pool is None and anti.get("proxies"):
            from ..proxy import ProxyPool
            self.proxy_pool = ProxyPool(anti.get("proxies"), anti.get("proxy_mode", "round_robin"))
        if self.proxy_pool is None and anti.get("proxy"):
            from ..proxy import ProxyPool
            self.proxy_pool = ProxyPool([anti["proxy"]])
        self._single_proxy_mode = bool(self.proxy_pool is not None and self.proxy_pool.size > 1)
        self._proxy = anti.get("proxy") or (self.proxy_pool.next() if self.proxy_pool else None)

    # ---- 会话池 ----
    def _ensure_pool(self):
        if not self._pool_enabled:
            return None
        # 池进程已死 → 清掉，重新拉起（自愈）
        if self._pool is not None and self._pool.poll() is not None:
            self._pool = None
        if self._pool is not None:
            return self._pool
        import subprocess, threading
        env = {**os.environ, "NODE_PATH": _NODE_PATH}
        ss = self.session_dir / "session.json"
        if ss.exists():
            env["US_STORAGE_STATE"] = str(ss)
        if self._proxy and not self._single_proxy_mode:
            env["US_PROXY"] = self._proxy
        self._pool = subprocess.Popen(
            [_NODE_BIN, str(self.scripts_dir / "browser_pool.cjs")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", env=env,
        )
        self._reader = threading.Thread(target=self._read_pool, daemon=True)
        self._reader.start()
        return self._pool

    def _read_pool(self):
        import json as _json
        if not self._pool or not self._pool.stdout:
            return
        my_pool = self._pool
        for line in my_pool.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                obj = _json.loads(line)
            except Exception:
                continue
            with self._cond:
                rid = obj.get("id")
                if rid in self._pending:
                    self._pending[rid] = obj
                    self._cond.notify_all()
        # EOF：池进程退出（崩溃/被杀）→ 挂起请求立即报错，下一次 fetch 自愈重启。
        # 仅当 self._pool 仍是本 reader 的池时才清理，避免误清新拉起的池。
        with self._cond:
            if self._pool is my_pool:
                dead = [rid for rid, obj in self._pending.items() if obj is None]
                self._pool = None
                for rid in dead:
                    self._pending[rid] = {"id": rid, "error": "浏览器池进程已退出", "html": "", "url": ""}
                self._cond.notify_all()

    def fetch(self, req: Request) -> Response:
        # 人工交互场景（登录 / 整页验证码 / headless=false 弹窗）→ browser_generic.cjs
        if (self.config.get("login") or {}).get("enabled") or \
           (self.config.get("verify") or {}).get("enabled") or \
           self.config.get("headless") is False:
            return self._fetch_interactive(req)
        # 多代理轮换：逐请求换代理，必须走单页桥（池是固定代理）
        if self._single_proxy_mode:
            return self._fetch_single(req)
        pool = self._ensure_pool()
        if pool is not None:
            return self._fetch_pool(pool, req)
        return self._fetch_single(req)

    def _fetch_interactive(self, req: Request) -> Response:
        """人工交互浏览器取数：登录 / 整页验证码（大众点评/美团验证中心）/ headless=false 弹窗。
        走 scripts/browser_generic.cjs（支持 login + verify + 会话持久化 + 等真人过验证）。"""
        import json as _json, subprocess, tempfile
        spec = {
            "url": req.url,
            "js_pre": self.config.get("js_pre"),
            "login": self.config.get("login"),
            "verify": self.config.get("verify"),
            "fingerprint": self.config.get("fingerprint"),
            "capture": self.config.get("capture"),
        }
        wait_sel = self.config.get("wait_selector")
        wait_to = int(self.config.get("wait_timeout") or 30000)
        for a in (self.config.get("actions") or []):
            if isinstance(a, dict) and a.get("type") == "wait" and a.get("selector"):
                wait_sel = a["selector"]
                wait_to = int(a.get("timeout") or wait_to)
        if wait_sel:
            spec["wait"] = {"selector": wait_sel, "timeout": wait_to}
        with tempfile.TemporaryDirectory(prefix="us_int_") as tmp:
            spec_file = Path(tmp) / "spec.json"
            out_dir = Path(tmp) / "pages"
            out_dir.mkdir()
            spec_file.write_text(_json.dumps(spec, ensure_ascii=False), encoding="utf-8")
            ss = self.session_dir / "session.json"
            cmd = [_NODE_BIN, str(self.scripts_dir / "browser_generic.cjs"),
                   "--spec", str(spec_file), "--out", str(out_dir),
                   "--headless", "0",
                   "--storageState", str(ss),
                   "--scrollCount", str(self.config.get("scroll_count", 0)),
                   "--scrollWait", str(self.config.get("scroll_wait_ms", 2000))]
            env = {**os.environ, "NODE_PATH": _NODE_PATH}
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, encoding="utf-8", env=env)
            html = ""
            final_url = req.url
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = _json.loads(line)
                except Exception:
                    continue
                t = obj.get("type")
                msg = obj.get("message") or ""
                if t in ("login", "verify_required"):
                    print(f"⚠️ {msg}", flush=True)
                elif t in ("login_ok", "verify_ok"):
                    print(f"✅ 会话已保存: {obj.get('storageState', '')}", flush=True)
                elif t == "verify_passed":
                    print(f"✅ {msg}", flush=True)
                elif t == "page":
                    f = obj.get("file")
                    if f and Path(f).exists():
                        html = Path(f).read_text(encoding="utf-8", errors="replace")
                elif t == "error":
                    proc.terminate()
                    raise RuntimeError(msg or "浏览器交互桥错误")
            proc.wait(timeout=600)
            if not html:
                files = sorted(out_dir.glob("*.html"))
                if files:
                    html = files[0].read_text(encoding="utf-8", errors="replace")
        return Response(request=req, status=200, body=html.encode("utf-8"),
                        text=html, json=None, url=final_url)

    def _fetch_pool(self, pool, req: Request) -> Response:
        import json as _json, threading
        if pool.poll() is not None:
            pool = self._ensure_pool()
            if pool is None:
                return self._fetch_single(req)
        with self._lock:
            self._req_id += 1
            rid = self._req_id
            self._pending[rid] = None
            payload = {"id": rid, "url": req.url,
                       "scroll": self.config.get("scroll_count", 0),
                       "actions": self.config.get("actions", []),
                       "stealth": bool(self.config.get("stealth", False)),
                       "remove_overlays": bool(self.config.get("remove_overlays", False))}
            if self.config.get("js_pre"):
                payload["js"] = self.config["js_pre"]
            if self.config.get("wait_selector"):
                payload["wait"] = self.config["wait_selector"]
            ss = self.session_dir / "session.json"
            if ss.exists():
                payload["storageState"] = str(ss)
            pool.stdin.write(_json.dumps(payload, ensure_ascii=False) + "\n")
            pool.stdin.flush()
        # 等待结果（带超时）
        deadline = time.time() + 120
        with self._cond:
            while self._pending.get(rid) is None:
                if time.time() > deadline:
                    with self._lock:
                        self._pending.pop(rid, None)
                    raise TimeoutError(f"浏览器池渲染超时: {req.url}")
                self._cond.wait(1.0)
            obj = self._pending.pop(rid)
        if "error" in obj and obj["error"]:
            raise RuntimeError(obj["error"])
        html = obj.get("html", "")
        return Response(request=req, status=200, body=html.encode("utf-8"),
                        text=html, json=None, url=obj.get("url") or req.url)

    def _fetch_single(self, req: Request) -> Response:
        # 单页桥（pool=false 后备）
        import json as _json, subprocess, tempfile
        with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as tf:
            out_file = tf.name
        cmd = [_NODE_BIN, str(self.scripts_dir / "browser_single.cjs"), "--url", req.url, "--out", out_file,
               "--scrollCount", str(self.config.get("scroll_count", 0)),
               "--storageState", str(self.session_dir / "session.json")]
        if self.config.get("js_pre"):
            cmd += ["--js", self.config["js_pre"]]
        if self.config.get("wait_selector"):
            cmd += ["--wait", self.config["wait_selector"]]
        if self.config.get("actions"):
            cmd += ["--actions", _json.dumps(self.config["actions"], ensure_ascii=False)]
        if self.config.get("stealth"):
            cmd += ["--stealth", "1"]
        if self.config.get("remove_overlays"):
            cmd += ["--removeOverlays", "1"]
        proxy = None
        if self._single_proxy_mode and self.proxy_pool is not None:
            proxy = self.proxy_pool.next()
        elif self._proxy and not self._single_proxy_mode:
            proxy = self._proxy
        if proxy:
            cmd += ["--proxy", proxy]
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              env={**os.environ, "NODE_PATH": _NODE_PATH}, timeout=180)
        try:
            obj = _json.loads(proc.stdout.strip().splitlines()[-1])
        except Exception:
            raise RuntimeError(f"浏览器桥输出异常: {proc.stderr[-300:]}")
        if obj.get("type") == "error":
            raise RuntimeError(obj.get("message"))
        html = Path(out_file).read_text(encoding="utf-8", errors="replace")
        return Response(request=req, status=200, body=html.encode("utf-8"),
                        text=html, json=None, url=obj.get("url") or req.url)

    def close(self) -> None:
        if self._pool is not None:
            try:
                if self._pool.stdin:
                    self._pool.stdin.write('{"type":"close"}\n')
                    self._pool.stdin.flush()
                self._pool.wait(timeout=10)
            except Exception:
                try:
                    self._pool.terminate()
                except Exception:
                    pass
            self._pool = None


import os as _os_environ  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import os as _os_mod  # noqa: E402
_NODE_BIN = _os_mod.environ.get("UNIVERSAL_SCRAPER_NODE",
                                "/Users/kairanqin/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node")
_NODE_PATH = _os_mod.environ.get("UNIVERSAL_SCRAPER_NODE_PATH",
                                 "/Users/kairanqin/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules")

