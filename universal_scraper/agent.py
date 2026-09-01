#!/usr/bin/env python3
"""🤖 LLM 浏览器代理模式（browser-use 路线）。

不预写选择器：LLM 观察真实页面（URL/标题/可见文本/可点击元素）→ 自主决定
点击/输入/滚动/提取 → 循环直到完成任务。对没精配的网站自适应，配合 CDP
可复用用户已登录 Chrome（淘宝/京东/小红书等强反爬站）。

用法:
  from universal_scraper.agent import agent_task
  out = agent_task("抓取 quotes.toscrape.com 的 5 条名言、作者、标签",
                   "https://quotes.toscrape.com/", max_steps=12)
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent

SYSTEM_PROMPT = """你是浏览器操作代理。你的任务由用户给出。你要通过观察真实页面来决定动作，逐步完成任务。
每轮我会给你：当前页面 URL/标题、可见文本摘要、可点击链接、按钮、输入框、以及最近操作历史。
你只输出一个 JSON 动作对象，不要解释。动作类型：
- {"action":"goto","url":"绝对URL"}  跳转（用历史/链接里的绝对URL，不要编造）
- {"action":"click","selector":"CSS选择器"}  点击（selector 必须用我给的）
- {"action":"type","selector":"CSS选择器","value":"文本"}  在输入框输入
- {"action":"press","key":"Enter"}  按键
- {"action":"scroll","dir":"down|up|bottom"}  滚动
- {"action":"wait","ms":1500}  等待
- {"action":"extract","schema":{"字段名":"字段说明"}}  把当前页面的可见内容按 schema 抽取成 JSON 数组（用于列表页/详情页收集数据）
- {"action":"done"}  任务已完成或无法继续

规则：
1. 先找到数据所在页面：列表/搜索页通常有多个相似链接，先 extract 列表页收集条目，需要详情再逐个点开。
2. extract 一次尽量多抽（当前页所有相关条目），不要每个条目单独 extract。
3. 需要翻页/加载更多时用 scroll 或 click 下一页。
4. 页面需要登录/滑块（文本含"请登录/滑块/验证码"）→ 输出 {"action":"done","note":"需要登录"}。
5. 连续 2 次同样动作没效果就换策略。
6. 不要编造链接；跳转只用品类页/翻页链接，别点进无关广告。"""


class AgentError(RuntimeError):
    pass


def debug_chrome_alive() -> bool:
    """调试 Chrome（用户已登录/过验证的真实浏览器，固定 9222 端口）是否在运行。"""
    try:
        import urllib.request as _ur
        # 固定本机调试端点（非用户输入），无 SSRF 面
        with _ur.urlopen("http://127.0.0.1:9222/json/version", timeout=2) as _r:
            return getattr(_r, "status", 200) == 200
    except Exception:
        return False


class AgentSession:
    """agent_browser.cjs 长驻子进程封装。"""

    def __init__(self, cdp: str = "", headless: bool = True, log=None):
        self.log = log or (lambda m: None)
        from .runtime import resolve_node, resolve_node_path
        node = os.environ.get("UNIVERSAL_SCRAPER_NODE", resolve_node())
        npath = os.environ.get("UNIVERSAL_SCRAPER_NODE_PATH", resolve_node_path())
        bridge = ROOT / "scripts" / "agent_browser.cjs"
        cmd = [node, str(bridge)]
        if cdp:
            cmd += ["--cdp", cdp]
        else:
            cmd += ["--headless", "1" if headless else "0"]
        env = {**os.environ, "NODE_PATH": npath}
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, env=env,
                                     bufsize=1)
        self._buf: List[str] = []
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        # 等 ready（浏览器冷启动可能 1-5 秒，别只看一次）
        import time as _t
        _deadline = _t.time() + 60
        while _t.time() < _deadline:
            m = self._next(timeout=0.5)
            if m is None:
                continue
            if m.get("type") == "ready":
                self.log(f"🖥️ 代理浏览器就绪（{m.get('mode','')}）")
                return
            if m.get("type") == "error":
                raise AgentError(m.get("message", "代理浏览器启动失败"))
        raise AgentError("代理浏览器启动超时（60s）")

    def _read_loop(self):
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.strip()
            if line:
                try:
                    self._buf.append(json.loads(line))
                except Exception:
                    pass

    def _next(self, timeout: float = 20.0) -> Optional[Dict[str, Any]]:
        import time
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self._buf:
                return self._buf.pop(0)
            if self.proc.poll() is not None and not self._buf:
                raise AgentError("代理浏览器进程已退出")
            time.sleep(0.1)
        return None

    def send(self, op: Dict[str, Any], timeout: float = 60.0) -> Dict[str, Any]:
        if self.proc.poll() is not None:
            raise AgentError("代理浏览器进程已退出")
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(op, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        while True:
            m = self._next(timeout=timeout)
            if m is None:
                raise AgentError(f"指令超时: {op.get('op')}")
            if m.get("type") == "error":
                raise AgentError(m.get("message", "代理浏览器错误"))
            if m.get("type") == "result" and m.get("op") == op.get("op"):
                return m
            # 其它类型忽略（容错）

    def close(self):
        try:
            self.send({"op": "close"}, timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass

    def snapshot(self, max_links: int = 30, max_text: int = 1500) -> Dict[str, Any]:
        r = self.send({"op": "snapshot", "max_links": max_links, "max_text": max_text}, timeout=30)
        return {k: v for k, v in r.items() if k not in ("type", "op")}

    def html_text(self, max_chars: int = 8000) -> str:
        r = self.send({"op": "html", "max_chars": max_chars}, timeout=30)
        return str(r.get("text") or "")


def _llm(messages: List[Dict[str, str]], timeout: int = 150) -> str:
    from .auto import _llm_chat
    return _llm_chat(messages, timeout=timeout)


def _parse_action(raw: str) -> Dict[str, Any]:
    import re
    mm = re.search(r"\{.*\}", raw or "", re.S)
    if not mm:
        raise AgentError("LLM 未返回动作 JSON: " + (raw or "")[:120])
    d = json.loads(mm.group(0))
    if not isinstance(d, dict) or "action" not in d:
        raise AgentError("动作缺少 action 字段")
    return d


def _extract_items(description: str, page_text: str, schema: Dict[str, Any],
                   history: List[str], log) -> List[Dict[str, Any]]:
    """把当前页文本按 schema 抽成条目数组。"""
    schema_s = json.dumps(schema, ensure_ascii=False)
    prompt = (
        f"任务：{description}\n"
        f"从下面的页面内容中提取所有相关条目，输出 JSON 数组（不要解释）。\n"
        f"字段定义：{schema_s}\n"
        f"页面里没有相关条目就输出 []。\n\n页面内容：\n{page_text[:8000]}"
    )
    raw = _llm([
        {"role": "system", "content": "你是数据抽取引擎，只输出合法 JSON 数组。"},
        {"role": "user", "content": prompt},
    ], timeout=150)
    import re as _re
    m = _re.search(r"\[.*\]", raw or "", _re.S)
    arr = json.loads(m.group(0)) if m else []
    if not isinstance(arr, list):
        return []
    items = [it for it in arr if isinstance(it, dict) and any(str(v or "").strip() for v in it.values())]
    if items:
        log(f"📦 抽取到 {len(items)} 条")
    return items


def agent_task(description: str, start_url: str = "", max_steps: int = 12,
               cdp: str = "", headless: bool = True,
               limit: Optional[int] = None, log_cb=None,
               stop_file: Optional[str] = None) -> Dict[str, Any]:
    """LLM 浏览器代理执行。返回 {items, log, steps, error}。
    stop_file: 该路径存在（内容非空）时，代理会尽快停止（WebUI「停止任务」用）。
    """
    import os as _os
    lines: List[str] = []

    def log(m):
        lines.append(m)
        if log_cb:
            log_cb(m)

    def _stopped():
        try:
            if stop_file and _os.path.exists(stop_file):
                with open(stop_file, encoding="utf-8", errors="replace") as _f:
                    return bool(str(_f.read() or "").strip())
        except Exception:
            pass
        return False

    items: List[Dict[str, Any]] = []
    history: List[str] = []
    sess = None
    try:
        sess = AgentSession(cdp=cdp, headless=headless, log=log)
        if start_url:
            log(f"🌐 打开入口: {start_url}")
            r = sess.send({"op": "goto", "url": start_url}, timeout=75)
            if not r.get("ok"):
                raise AgentError(r.get("message", "打开入口失败"))

        failed = 0
        for step in range(1, max_steps + 1):
            if _stopped():
                log("⏹ 收到停止信号，代理已停止")
                break
            if limit and len(items) >= limit:
                log(f"✅ 已达数量上限 {limit} 条，停止")
                break
            snap = sess.snapshot()
            text = str(snap.get("text") or "")
            # 登录/滑块检测
            if cdp and re_need_login(text):
                log("🔑 页面要求登录/滑块——请在调试 Chrome 里完成登录/滑块，然后我会继续")
                # 给 30 秒人工操作机会
                import time as _t
                for _ in range(6):
                    _t.sleep(5)
                    snap2 = sess.snapshot()
                    if not re_need_login(str(snap2.get("text") or "")):
                        snap = snap2
                        log("✅ 登录/验证已通过，继续")
                        break
                else:
                    raise AgentError("等待登录超时，请完成登录后重试")
            hist = "\n".join(history[-8:]) if history else "(无)"
            prompt = (
                f"任务：{description}\n\n"
                f"当前页面：\n- URL: {snap.get('url')}\n- 标题: {snap.get('title')}\n"
                f"- 可见文本摘要: {text[:1200]}\n"
                f"- 链接(前30): {json.dumps(snap.get('links', []), ensure_ascii=False)[:1600]}\n"
                f"- 按钮: {json.dumps(snap.get('buttons', []), ensure_ascii=False)[:600]}\n"
                f"- 输入框: {json.dumps(snap.get('inputs', []), ensure_ascii=False)[:400]}\n\n"
                f"最近操作历史：\n{hist}\n\n"
                f"（已收集 {len(items)} 条）请输出下一步动作 JSON。"
            )
            log(f"🧠 第 {step}/{max_steps} 步：让 LLM 决策...")
            try:
                act = _parse_action(_llm([
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ], timeout=150))
            except Exception as e:
                failed += 1
                log(f"⚠️ LLM 决策失败: {e}")
                if failed >= 3:
                    break
                history.append(f"决策失败: {e}")
                continue

            a = act.get("action")
            note = str(act.get("note") or "")
            if a == "done":
                if "需要登录" in note:
                    raise AgentError("需要登录/滑块，代理无法继续（请提供 CDP 已登录浏览器）")
                log("🏁 LLM 判定任务完成")
                break
            if a == "extract":
                schema = act.get("schema") or {}
                if not schema:
                    schema = {"标题": "标题", "链接": "链接"}
                pt = sess.html_text(8000)
                got = _extract_items(description, pt, schema, history, log)
                for it in got:
                    it.setdefault("_url", str(snap.get("url") or ""))
                    it.setdefault("_parser", "agent")
                    if it not in items:
                        items.append(it)
                history.append(f"extract -> {len(got)} 条")
                continue
            if a == "goto":
                u = act.get("url", "")
                if not u.startswith(("http://", "https://")):
                    history.append("goto: URL 非法，忽略")
                    continue
                try:
                    r = sess.send({"op": "goto", "url": u}, timeout=75)
                    history.append(f"goto {u[:60]} -> {'ok' if r.get('ok') else 'fail'}")
                except Exception as e:
                    history.append(f"goto 失败: {str(e)[:80]}")
                continue
            if a == "click":
                try:
                    r = sess.send({"op": "click", "selector": act.get("selector", "")}, timeout=30)
                    history.append(f"click {act.get('selector','')[:50]} -> {'ok' if r.get('ok') else 'fail'}")
                except Exception as e:
                    history.append(f"click 失败: {str(e)[:80]}")
                continue
            if a == "type":
                try:
                    sess.send({"op": "type", "selector": act.get("selector", ""),
                               "text": act.get("value", "")}, timeout=20)
                    history.append("type -> ok")
                except Exception as e:
                    history.append(f"type 失败: {str(e)[:80]}")
                continue
            if a == "press":
                try:
                    sess.send({"op": "press", "key": act.get("key", "Enter")}, timeout=20)
                    history.append("press Enter")
                except Exception as e:
                    history.append(f"press 失败: {str(e)[:80]}")
                continue
            if a == "scroll":
                try:
                    sess.send({"op": "scroll", "dir": act.get("dir", "down")}, timeout=20)
                    history.append(f"scroll {act.get('dir','down')}")
                except Exception as e:
                    history.append(f"scroll 失败: {str(e)[:80]}")
                continue
            if a == "wait":
                try:
                    sess.send({"op": "wait", "ms": int(act.get("ms", 1500))}, timeout=15)
                except Exception:
                    pass
                history.append("wait")
                continue
            history.append(f"未知动作 {a}")
        else:
            log("⏱️ 达到最大步数")

        # 输出
        out = {
            "ok": bool(items),
            "items": items,
            "total": len(items),
            "steps": min(max_steps, len(history) + 1),
            "log": lines,
            "error": "",
        }
        if not items:
            out["error"] = "代理未收集到条目（最后页面：" + str(snap.get("url") if 'snap' in dir() else start_url) + "）"
        return out
    except AgentError as e:
        return {"ok": False, "items": items, "total": len(items), "steps": 0,
                "log": lines, "error": str(e)}
    finally:
        if sess is not None:
            try:
                sess.close()
            except Exception:
                pass


def re_need_login(text: str) -> bool:
    import re
    return bool(re.search(r"请登录|免费注册|拖动.*滑块|滑块.*验证|安全验证|访问过于频繁|验证码", text or ""))


def run_agent_cli(description: str, url: str = "", max_steps: int = 12,
                  cdp: str = "", limit=None) -> Dict[str, Any]:
    import hashlib as _hl
    lines = []

    def log(m):
        lines.append(m)
        print(m, flush=True)

    try:
        out = agent_task(description, start_url=url, max_steps=max_steps,
                         cdp=cdp, limit=limit, log_cb=log)
        out["messages"] = lines
        name = f"agent_{_hl.md5(description.encode(), usedforsecurity=False).hexdigest()[:8]}"
        out["name"] = name
        out["result"] = {"total": out.get("total", 0), "fetched": 0,
                         "errors": 0, "agent_mode": True}
        if out.get("items"):
            items_dir = ROOT / "outputs" / "items"
            items_dir.mkdir(parents=True, exist_ok=True)
            (items_dir / f"{name}.jsonl").write_text(
                "".join(json.dumps(it, ensure_ascii=False) + "\n" for it in out["items"]),
                encoding="utf-8")
            fp = ROOT / "outputs" / f"{name}.json"
            fp.write_text(json.dumps(out["items"], ensure_ascii=False, indent=2), encoding="utf-8")
            out["files"] = {"json": f"outputs/{name}.json"}
        return out
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "messages": lines}


if __name__ == "__main__":
    import sys
    desc = sys.argv[1] if len(sys.argv) > 1 else "抓取当前页面所有链接标题"
    url = sys.argv[2] if len(sys.argv) > 2 else ""
    cdp = sys.argv[3] if len(sys.argv) > 3 else ""
    r = run_agent_cli(desc, url, cdp=cdp)
    print("RESULT:", json.dumps({k: v for k, v in r.items() if k != "items"}, ensure_ascii=False)[:500])
    for it in r.get("items", [])[:5]:
        print(json.dumps(it, ensure_ascii=False))
