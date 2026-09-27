#!/usr/bin/env python3
"""一键抓取（对标 Firecrawl CLI）：URL → 干净文本 / Markdown / 结构化 JSON。

用法（CLI）:
  python3 -m universal_scraper.cli fetch <url> [--browser] [--selector "h1"] [--article] [--table] [--json] [--out file.md] [--proxy http://...]

也可作库调用:
  from universal_scraper.quick import fetch_url
  result = fetch_url("https://example.com", browser=True, selector=".content")
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional


def fetch_url(url: str, browser: bool = False, selector: Optional[str] = None,
              article: bool = False, table: bool = False, proxy: Optional[str] = None,
              actions: Optional[List[dict]] = None, js: Optional[str] = None,
              wait_selector: Optional[str] = None, stealth: bool = True,
              remove_overlays: bool = True, timeout: float = 60,
              links: bool = False, links_allow: Optional[str] = None,
              links_deny: Optional[str] = None,
              screenshot: Optional[str] = None,
              cookie: Optional[str] = None,
              headers: Optional[Dict[str, str]] = None,
              cdp: Optional[str] = None,
              capture: Optional[str] = None) -> Dict[str, Any]:
    """抓取一个 URL，返回 {url, status, text, markdown?, selector?, article?, tables?, links?}。
    links=True 时额外提取页面所有外链（对标 Firecrawl scrape links）。
    capture=路径 时启用浏览器捕获模式（capture_all），捕获文件复制到该路径。
    - browser=False: 走 HTTP（curl_cffi→requests→urllib 自动选后端）
    - browser=True : 走浏览器桥（JS 渲染 / SPA / 需要动作链的页面）
    """
    result: Dict[str, Any] = {"url": url, "status": 0, "text": "", "error": ""}
    if screenshot and not browser:
        browser = True  # 截图需要浏览器渲染
    if capture and not browser:
        browser = True  # 商标网战训：--capture 文档暗示过但从未实现——现在真实现
    # 审查三轮（H）：URL 守卫原只覆盖 HTTP 路径——browser=True（webui/mcp 的
    # 自动流程）同样可被指向 169.254.169.254。守卫提到分支前，两条路径共用
    import ipaddress as _ipa
    from urllib.parse import urlsplit as _usp
    _sp = _usp(url)
    if _sp.scheme not in ("http", "https"):
        return {"url": url, "status": 0, "text": "",
                "error": f"仅允许 http/https: {url}"}
    try:
        import socket as _sock
        for _info in _sock.getaddrinfo(_sp.hostname, None):
            _ip = _ipa.ip_address(_info[4][0])
            if _ip.is_private or _ip.is_loopback or _ip.is_reserved or _ip.is_link_local:
                return {"url": url, "status": 0, "text": "",
                        "error": f"拒绝私有/保留地址: {_sp.hostname}"}
    except Exception as _e:
        # 审查三轮（M）：曾只捕 gaierror——IDN 域名的 UnicodeError 等穿透崩掉
        # fetch_url。解析失败归一为 error dict
        return {"url": url, "status": 0, "text": "", "error": f"域名解析失败: {_e}"}
    if not browser:
        # OCR R131 二轮（CRITICAL）：HTTP 路径曾无 SSRF 校验直连（js_recon 有、
        # 这里没有）——auto/agent 流程的 URL 可指向 169.254.169.254 云元数据。
        # 与 js_recon 同口径：仅 http/https + 解析地址拒绝私网/环回/保留段
        from .core import make_http_client, set_request_budget, request_budget
        # 审查修复 P1：quick 入口（webui/mcp 长驻进程调用）曾继承上一个任务的
        # 预算残留——从未选择加入预算的代码不能被别的任务留下的硬闸误杀。
        # 审查修复（P1，R5）：set_request_budget(0) 会清掉并发 engine 任务的
        # 进程级预算（WebUI 多线程实证）——持引擎串行锁 + 限值/计数还原。
        from .engine_v3 import _ENGINE_RUN_LOCK
        _saved = request_budget()
        _ENGINE_RUN_LOCK.acquire()
        try:
            set_request_budget(0)
            _anti = {"min_interval": 0.2, "timeout": timeout,
                     "http_backend": "auto", "proxy": proxy}
            _hdrs = dict(headers or {})
            if cookie:
                _hdrs["Cookie"] = cookie
            if _hdrs:
                _anti["headers"] = _hdrs
            client = make_http_client(_anti)
            res = client.get(url)
            result["status"] = res.get("status", 0)
            result["url"] = res.get("url") or url
            # NBS 夜测事故（launchd 残血 python 静默降级 urllib 跑了一宿）：
            # 降级必须写进结果本体，让编排器/脚本可程序化发现，而不是只在 stderr 喊
            result["backend"] = getattr(client, "_backend_name", "unknown")
            result["degraded_backend"] = result["backend"] != "curl_cffi"
            if not res.get("ok"):
                result["error"] = res.get("text", "请求失败")[:300]
                return result
            result["text"] = res.get("text", "")
            # 实战反馈四#6（aqistudy）：SPA catch-all 壳页（200 + 几 KB 骨架）
            # 曾被误判"有内容"——补正文量/骨架标记辅助判型。
            # r5 自查：判定式曾引用 diagnose 的 _st 变量名（NameError）——本函数
            # 状态码取 res["status"]
            _txt = result["text"]
            result["text_len"] = len(_txt)
            result["shell_suspect"] = bool(
                int(res.get("status", 0) or 0) == 200 and len(_txt) < 6000
                and len(re.findall(r"<script", _txt, re.I)) >= 3
                and len(re.findall(r"<(?:p|td|li|article)[\s>]", _txt, re.I)) < 5)
        finally:
            # 审查五轮（H）：restore 曾裸调——它若抛异常会跳过下方 release，
            # _ENGINE_RUN_LOCK 永久持有 = 全进程引擎任务死锁
            try:
                set_request_budget(_saved["limit"])
            except Exception:
                pass
            try:
                from . import core as _core
                with _core._REQUEST_STATS_LOCK:
                    _core._HTTP_REQUEST_STATS["count"] += _saved["used"]
            except Exception:
                pass
            _ENGINE_RUN_LOCK.release()
    else:
        from .modules.fetchers import BrowserFetcher
        from .protocols import Request
        cfg: Dict[str, Any] = {"type": "browser", "pool": True, "scroll_count": 0,
                               "stealth": stealth, "remove_overlays": remove_overlays}
        if screenshot:
            actions = list(actions or []) + [{"type": "screenshot", "path": screenshot}]
        if actions:
            cfg["actions"] = actions
        if js:
            cfg["js_pre"] = js
        if wait_selector:
            cfg["wait_selector"] = wait_selector
        if cdp:
            cfg["cdp"] = cdp  # 附加调试 Chrome：侦察与正式采集同通道、过 Cloudflare
        if capture:
            # 商标网战训：--capture 走交互桥（capture_all 支持所在），产物复制到目标路径
            cfg["capture"] = True
            cfg["pool"] = False
            # R1 复查修复（P2-6）：尊重 headless 配置——capture 不再强制弹可见窗口
            # （spec-schema 文档 capture+headless:true 组合；无头服务器上强制 headful 必挂）
            cfg.setdefault("headless", True)
            import tempfile as _tf
            _cap_dir = Path(_tf.mkdtemp(prefix="us_fetch_capture_"))
            cfg["_task_dir"] = str(_cap_dir)
        anti = {"session_dir": "/tmp/us_fetch_session", "min_interval": 0.1,
                "http_backend": "auto"}
        if proxy:
            anti["proxy"] = proxy
        fetcher = BrowserFetcher(cfg, {}, anti)
        # R2 复查修复（P2-2）：capture 产物复制+清理必须覆盖 fetch 异常路径——
        # RateLimitedError 发生时 capture_all.json 往往已含真实 XHR 数据，
        # 提前 return 会把数据级临时目录漏在磁盘上
        try:
            resp = fetcher.fetch(Request(url=url))
        except KeyboardInterrupt:
            # OCR R131（H）：Ctrl+C 曾在清理前直接 raise——capture 临时目录泄漏
            if capture:
                import shutil as _shk
                _shk.rmtree(_cap_dir, ignore_errors=True)
            raise
        except Exception as e:
            # R1 复查修复（P1-3）：挑战壳/空渲染路径会抛 RateLimitedError
            # （empty_shell 检测）——曾裸栈崩掉 fetch --capture 的主失败模式。
            # 降级为 result["error"]，与 HTTP 路径错误契约一致
            result["error"] = f"{type(e).__name__}: {str(e)[:200]}"
            if capture:
                try:
                    _cap_src = _cap_dir / "capture_all.json"
                    if _cap_src.exists():
                        import shutil as _sh
                        _dst = Path(capture)
                        _dst.parent.mkdir(parents=True, exist_ok=True)
                        _sh.copy2(_cap_src, _dst)
                        result["capture_file"] = str(_dst)
                except Exception:
                    pass
                finally:
                    import shutil as _sh2
                    _sh2.rmtree(_cap_dir, ignore_errors=True)
            return result
        finally:
            fetcher.close()
        if capture:
            try:
                _cap_src = _cap_dir / "capture_all.json"
                if _cap_src.exists():
                    import shutil as _sh
                    _dst = Path(capture)
                    _dst.parent.mkdir(parents=True, exist_ok=True)
                    _sh.copy2(_cap_src, _dst)
                    result["capture_file"] = str(_dst)
                else:
                    result["capture_error"] = "捕获文件未生成（页面可能无 XHR/fetch，或挑战未过）"
            finally:
                # R1 复查修复（P2-4）：临时目录用完即删（重复 --capture 不再累积垃圾）
                import shutil as _sh2
                _sh2.rmtree(_cap_dir, ignore_errors=True)
        result["status"] = resp.status
        result["url"] = resp.url or url
        result["text"] = resp.text
        # 统一契约（审查 P2）：浏览器是高级通道不是降级——显式标注，
        # 让消费方区分"HTTP+curl_cffi"与"browser"，而不是靠键缺失猜
        result["backend"] = "browser"
        result["degraded_backend"] = False
        if not resp.text:
            result["error"] = "浏览器渲染后无内容"

    text = result["text"]
    if selector:
        from .selectors import css_text
        result["selector"] = css_text(text, selector, limit=2_000_000)
    if article:
        from .extractors import extract_article
        result["article"] = extract_article(text)
    if table:
        from .extractors import extract_tables
        result["tables"] = extract_tables(text)
    if not (selector or article or table):
        from .extractors import html_to_markdown
        # 收官五轮（审查 M）：base_url 未传——相对链接/图片全部保持相对路径，
        # 违背自身 docstring 承诺；传 result["url"]（跟随重定向后的最终 URL）
        result["markdown"] = html_to_markdown(text, base_url=result.get("url") or url) or text[:200_000]
        # SPA 壳判型提示（版权中心战例：3.9KB 壳完全靠人眼识别）
        # 收官五轮（审查 M）：旧壳判定 6144 上限漏判 Next.js/Nuxt 大壳（内联
        # bootstrap JSON 常达 8-50KB）——改用"实质内容稀疏"判据：可见文本占比极低
        _spa_root = re.search(r'id="(?:app|root|__next|nuxt)"', text)
        if _spa_root:
            _vis = len(re.sub(r'<(script|style)[^>]*>.*?</\1>', '', text, flags=re.S))
            _vis = len(re.sub(r'<[^>]+>', '', re.sub(r'<(script|style)[^>]*>.*?</\1>', '', text, flags=re.S)).strip())
            if len(text) > 0 and _vis < max(len(text) * 0.05, 200):
                result["recon_hint"] = ("页面为 SPA 壳（可见文本占比 "
                                        f"{_vis}/{len(text)}={_vis*100//max(len(text),1)}%）——"
                                        "数据靠 JS/接口，先 jsrecon 找接口或 capture 捕获（配方 R8/R13）")
    if links:
        from .queue import extract_links
        result["links"] = extract_links(text, url, links_allow, links_deny)
    if screenshot:
        result["screenshot"] = screenshot if Path(screenshot).exists() else None
    return result


def pagefn_recon(url: str, min_len: int = 3, limit: int = 60) -> Dict[str, Any]:
    """实战反馈四#5（aqistudy 战训固化）：枚举页面非原生全局函数。

    传统 jQuery 页面的加密/签名逻辑挂在 window 全局函数上（如 aqistudy 的
    getVariable 等）——"页面自己算签名自己解密"打法的入口清单。经调试 Chrome
    (CDP) 渲染后枚举 window 上 typeof=function 且非 JS 内置/非 native 的名字，
    附 toString() 前 120 字符供判断用途。需先起调试 Chrome（open-debug-chrome.sh）。
    返回 {ok, url, functions: [{name, src}], count, hint}。
    """
    import json as _json
    import subprocess as _sp
    import tempfile as _tf
    import shutil as _sh
    from .runtime import resolve_node, resolve_node_path
    from .cookies import find_cdp_port
    port = find_cdp_port(9222)
    if port is None:
        return {"ok": False, "error": "调试 Chrome 未运行（bash scripts/open-debug-chrome.sh）"}
    out_path = Path(_tf.mkdtemp(prefix="pagefn_")) / "fns.json"
    node_script = r'''
const fs = require("node:fs");
const path = require("node:path");
const NP = process.env.NODE_PATH || "";
const cands = NP.split(":").filter(Boolean).map(p => path.join(p, "playwright")).concat(["patchright", "playwright"]);
let chromium = null;
for (const c of cands) { try { chromium = require(c).chromium; break; } catch (e) {} }
if (!chromium) { console.error("no-playwright"); process.exit(2); }
(async () => {
  const browser = await chromium.connectOverCDP("http://127.0.0.1:PORT");
  const ctx = browser.contexts()[0];
  const page = await ctx.newPage();
  await page.goto(process.argv[1], { timeout: 45000, waitUntil: "domcontentloaded" }).catch(() => {});
  await new Promise(r => setTimeout(r, 3000));  // 等内联脚本挂载全局函数
  const fns = await page.evaluate(() => {
    // 过滤依据 = toString 含 [native code]（内置函数必命中）。页面自写
    // Function.prototype.bind 的产物也会含此串被误杀——已知漏报，换来零内置噪声
    const out = [];
    for (const k of Object.getOwnPropertyNames(window)) {
      try {
        const v = window[k];
        if (typeof v !== "function") continue;
        const src = String(v);
        if (src.includes("[native code]")) continue;
        out.push({ name: k, src: src.slice(0, 120) });
      } catch (e) { /* 跨域/代理对象——跳过 */ }
    }
    return out;
  });
  fs.writeFileSync(process.argv[2], JSON.stringify(fns));
  process.exit(0);
})()
  .catch(e => { console.error(String(e).slice(0, 200)); process.exitCode = 1; })
  .finally(async () => {
    // 审查五轮（LOW）：错误/超时路径曾不关页——共享调试 Chrome 里每次失败
    // 残留一个已加载完成的标签页，反复重跑持续累积
    try { await page.close(); } catch (e) {}
  });
'''.replace("PORT", str(port))
    env = {**__import__("os").environ, "NODE_PATH": resolve_node_path()}
    try:
        try:
            p = _sp.run([resolve_node(), "-e", node_script, url, str(out_path)],
                        capture_output=True, text=True, env=env, timeout=90,
                        cwd=str(Path(__file__).resolve().parent.parent))
        except Exception as e:
            return {"ok": False, "error": f"pagefn 采集执行失败: {type(e).__name__}: {e}"}
        if p.returncode != 0 or not out_path.exists():
            return {"ok": False, "error": f"pagefn 采集失败(rc={p.returncode}): {(p.stderr or '')[:120]}"}
        try:
            fns = _json.loads(out_path.read_text(encoding="utf-8"))
        except Exception as e:
            return {"ok": False, "error": f"pagefn 产物解析失败: {e}"}
        fns = [f for f in fns if len(f.get("name", "")) >= min_len]
        fns.sort(key=lambda f: len(f.get("src", "")), reverse=True)  # 实现长的先看（加密/签名逻辑通常不短）
        fns = fns[:limit]
        return {"ok": True, "url": url, "functions": fns, "count": len(fns),
                "hint": "找加密/签名入口：看 src 里含 CryptoJS/AES/MD5/encode/decrypt/"
                        "replace 字样的函数——在页面上下文调用它们，勿逆向重写（合规红线）"}
    finally:
        _sh.rmtree(out_path.parent, ignore_errors=True)


def _js_recon_via_cdp(url: str, max_scripts: int, log=None) -> Dict[str, Any]:
    """商标网战训（2026-09）：挑战壳站的 jsrecon 降级通道——调试 Chrome(CDP)
    加载页面（复用已过挑战的会话），从网络流量收 JS 包体。失败返回 error。"""
    import json as _json
    import subprocess as _sp
    import tempfile as _tf
    from .runtime import resolve_node, resolve_node_path
    from .cookies import find_cdp_port
    port = find_cdp_port(9222)
    if port is None:
        return {"ok": False, "error": "调试 Chrome 未运行（open-debug-chrome.sh）"}
    out_path = Path(_tf.mkdtemp(prefix="jsrecon_cdp_")) / "bodies.json"
    node_script = r'''
const fs = require("node:fs");
const path = require("node:path");
const NP = process.env.NODE_PATH || "";
const cands = NP.split(":").filter(Boolean).map(p => path.join(p, "playwright")).concat(["patchright", "playwright"]);
let chromium = null;
for (const c of cands) { try { chromium = require(c).chromium; break; } catch (e) {} }
if (!chromium) { console.error("no-playwright"); process.exit(2); }
(async () => {
  const browser = await chromium.connectOverCDP("http://127.0.0.1:PORT");
  const ctx = browser.contexts()[0];
  const page = await ctx.newPage();
  const bodies = {};
  const want = (u, ct) => /\.js(\?|$)/.test(u) || /javascript|ecmascript/i.test(ct || "");
  page.on("response", async (resp) => {
    try {
      const u = resp.url();
      if (!want(u, resp.headers()["content-type"] || "")) return;
      if (Object.keys(bodies).length >= 25) return;  // 上限防内存爆
      const txt = await resp.text();
      if (txt && txt.length > 500) bodies[u] = txt.slice(0, 2000000);
    } catch (e) { /* body 已释放/跨域——跳过 */ }
  });
  await page.goto(process.argv[1], { timeout: 45000, waitUntil: "domcontentloaded" }).catch(() => {});
  await new Promise(r => setTimeout(r, 6000));  // 等 SPA 懒加载 JS
  fs.writeFileSync(process.argv[2], JSON.stringify(bodies));
  process.exit(0);
})()
  .catch(e => { console.error(String(e).slice(0, 200)); process.exitCode = 1; })
  .finally(async () => {
    // 审查五轮（LOW）：错误路径曾不关页——调试 Chrome 标签页持续累积（pagefn 同款）
    try { await page.close(); } catch (e) {}
  });
'''.replace("PORT", str(port))
    import os as _os
    import shutil as _sh
    env = {**_os.environ, "NODE_PATH": resolve_node_path()}
    # R2 复查修复（P2-1）：mkdtemp 之后所有路径（含失败）统一 finally 清理——
    # 此前 subprocess 异常/rc!=0 提前 return，临时目录恰在失败路径上泄漏
    try:
        try:
            p = _sp.run([resolve_node(), "-e", node_script, url, str(out_path)],
                        capture_output=True, text=True, env=env, timeout=90,
                        cwd=str(Path(__file__).resolve().parent.parent))
        except Exception as e:
            return {"ok": False, "error": f"CDP 采集执行失败: {type(e).__name__}: {e}"}
        if p.returncode != 0 or not out_path.exists():
            return {"ok": False, "error": f"CDP 采集失败(rc={p.returncode}): {(p.stderr or '')[:120]}"}
        try:
            bodies = _json.loads(out_path.read_text(encoding="utf-8"))
        except Exception as e:
            return {"ok": False, "error": f"CDP 产物解析失败: {e}"}
        if not bodies:
            return {"ok": False, "error": "CDP 加载后未捕获到任何 JS 包（页面可能未过挑战）"}
        return {"ok": True, "bodies": bodies}
    finally:
        _sh.rmtree(out_path.parent, ignore_errors=True)


def js_recon(url: str, max_scripts: int = 6, out: Optional[str] = None) -> Dict[str, Any]:
    """接口侦察（版权中心战例）：下载页面 JS 包 → 提取候选 API 端点。
    安全边界：仅 http/https；host 解析到私网/环回/保留地址即拒绝。"""
    import ipaddress
    import socket
    from urllib.parse import urljoin, urlsplit
    sp = urlsplit(url)
    if sp.scheme not in ("http", "https"):
        return {"error": f"仅允许 http/https: {url}"}
    try:
        for info in socket.getaddrinfo(sp.hostname, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_link_local:
                return {"error": f"拒绝私有/保留地址: {sp.hostname} -> {ip}"}
    except Exception as e:
        # 审查五轮（H）：曾只捕 gaierror——中文 IDN 域名的 UnicodeError 穿透
        # （与 fetch_url 同款，此文件两处口径已统一）
        return {"error": f"域名解析失败: {e}"}

    from .core import make_http_client
    client = make_http_client({"min_interval": 0.5, "timeout": 20, "http_backend": "auto"})

    # batch2400 战训：fetch 也查 robots（此前仅 run 引擎查，fetch 漏了）
    try:
        from .robots import RobotsTxt
        _rt = RobotsTxt(user_agent="universal-scraper/1.0")
        if not _rt.allowed(url):
            return {"error": f"robots.txt Disallow: {url}（如确认有合法权请忽略此限制，"
                    "或使用 run --config 路线）"}
    except Exception:
        pass

    res = client.get(url)
    _st = int(res.get("status", 0) or 0)
    _txt = res.get("text", "") or ""
    # 商标网战训（2026-09）：挑战壳站（瑞数/FE 406/412）HTTP 拉 JS 全军覆没。
    # R1 复查修复（P0）：判型必须在 ok 门控【之前】——406/412 时 ok=False 曾
    # 提前返回错误，CDP 降级对新修的 406/412 场景恰是死代码；且 detect_block
    # 对 status>=400 先判 http_error，须把 406/412（挑战码）也视为挑战触发。
    from .antibot import detect_block as _db
    _bd = _db(_st, _txt[:20000], res.get("headers"), url)
    _challenge = (_bd.get("kind") not in ("none", "login", "http_error")) or \
                 (_st in (406, 412) and _txt.strip())
    if _challenge:
        _cdp_fallback = _js_recon_via_cdp(url, max_scripts, log=None)
        if _cdp_fallback.get("ok"):
            cdp_bodies = _cdp_fallback["bodies"]
            urls = sorted(cdp_bodies.keys(),
                          key=lambda u: (any(k in u.lower() for k in ("vendor", "sdk", "runtime", "polyfill")),
                                         not any(k in u.lower() for k in ("chunk", "page", "view", "module"))))
            # OCR R131（L）：原 `html = _txt` 在下方被同值赋值（res["text"]）覆盖，删冗余
        else:
            return {"url": url, "error": f"页面命中挑战壳[HTTP {_st}/{_bd.get('kind')}]且 CDP 降级失败: "
                                         f"{_cdp_fallback.get('error', '?')}",
                    "hint": "先 bash scripts/open-debug-chrome.sh 起调试 Chrome 并手动过一次挑战，再重跑 jsrecon"}
    elif not res.get("ok"):
        # 审查修复：核心客户端失败时 text 非空（装着错误信息），按 text 判永远不报错——
        # 被 WAF 拦的页面曾返回"成功形态的 0 候选"误导侦察方向。必须按 ok 门控。
        return {"error": f"页面获取失败: HTTP {_st} {_txt[:120]}"}
    else:
        cdp_bodies = None
    html = res.get("text", "")
    if not html and cdp_bodies is None:
        return {"error": f"页面获取失败: HTTP {_st}"}

    lazy_urls: list = []  # 懒加载分块 URL（入口 prefetch 声明 + 包体引用），脚本发现块之前就绪
    if cdp_bodies is None:
        # OCR R131 终审（实战）：html-webpack-plugin minify 会剥属性引号
        # （src=/js/app.js）——原正则要求引号曾漏掉全部脚本。兼容有/无引号
        scripts = re.findall(r'<script[^>]+src=["\']?([^"\'\s>]+)', html)
        # 实战补强：link rel=prefetch/preload 的 JS 是构建期声明的懒加载分块
        # （webpack 在入口 HTML 直接列出全部路由 chunk），一并入候选，
        # 并标注进 lazy_chunks 保持报告语义完整
        _prefetch = re.findall(
            r'<link[^>]+href=["\']?([^"\'\s>]+\.js(?:\?[^"\'\s>]*)?)["\']?[^>]*>', html)
        scripts.extend(_prefetch)
        lazy_urls.extend(urljoin(url, p) for p in _prefetch)
        urls = [urljoin(url, s) for s in scripts if s]
        # 互动易考核反馈 P0：默认抓 6 个脚本会先吃掉 SDK/vendor 包，配额全废。
        # chunk 优先级：业务包（chunk/page/view/module/index/app/数字名）置前，
        # vendor/sdk/runtime/polyfill 沉底（仍保留兜底位，不彻底丢弃）
        def _rank(u: str):
            low = u.lower()
            junk = any(k in low for k in ("vendor", "sdk", "runtime", "polyfill",
                                          "node_modules", "/lib/", "locales"))
            biz = any(k in low for k in ("chunk", "page", "view", "module", "business"))
            return (junk, not biz)  # False 排前：业务非 vendor 最先
        urls = sorted(urls, key=_rank)
    api_pat = re.compile(
        r'(?:["\'])(/[A-Za-z0-9_\-]*/(?:api|service|gateway|rest|query|search|inquiry)[/\w\-./]*'
        r'|https?://[\w.\-]+/(?:api|gateway|service)[/\w\-./]*'
        r'|baseURL[:\s]*["\']([^"\']{4,120})["\'])')
    # batch1401 战训：webpack 压缩包会吐 "baseURL\"),E=i(\" 这类拼接噪声。
    # 过滤规则：剥离转义引号后必须是"看起来像 URL/路径"的串。
    def _clean_hit(h: str) -> str:
        h = h.replace('\\"', '').replace("\\'", "").strip()
        return h

    def _plausible(h: str) -> bool:
        h2 = _clean_hit(h)
        if len(h2) < 4:
            return False
        # 必须以 / 或协议开头，或含域名特征；拒绝残留代码符号的拼接噪声
        if not (h2.startswith(("/", "http://", "https://", "${"))):
            return False
        if re.search(r'[=;{}()<>]', h2):  # 代码残渣
            return False
        return True

    found: Dict[str, List[str]] = {}
    base_urls: List[str] = []  # axios/fetch baseURL 优先单列（改写 http_json 的锚点）
    frags: List[str] = []      # batch1600 战训：压缩包里散落的路径碎片（低置信，agent 自行拼接）
    signatures: List[Dict[str, Any]] = []  # 互动易考核反馈 P0：端点签名（方法+完整路径+参数键+调用片段）
    # 三个互补的调用形态：axios 实例方法 / fetch() / 配置对象 url:
    # 注意组序：形态1 的 group(1)=方法名、group(2)=路径（审查修复 P1-4：
    # 曾统一读 group(1) 导致形态一永远取到 "post" 被剪枝——签名提取整体失效）
    sig_pats = [
        (re.compile(r'\.(get|post|put|delete|request)\s*\(\s*["\']([^"\']{3,120})["\']'), "METHOD"),
        (re.compile(r'fetch\s*\(\s*["\']([^"\']{3,120})["\']'), "GET"),
        (re.compile(r'url\s*:\s*["\']([^"\']{3,120})["\']'), "CFG"),
    ]
    param_pat = re.compile(r'["\']?([A-Za-z_][A-Za-z0-9_]{1,30})["\']?\s*[:=]')
    checked = 0
    # 归档功能请求：懒加载分块追踪——SPA 业务代码常在路由级 chunk 里，首屏
    # script 标签不含目标端点。从已下载包体提取 chunk 引用，第二阶段追加扫描
    chunk_refs: list = []  # [(相对引用, 出现处的脚本URL)]
    _CHUNK_RE = re.compile(r'["\']([^"\']+\.(?:chunk|async|lazy)\.js)["\']')
    # 归档功能请求：前端路由地图——vue-router/react-router 配置对象里的
    # path:"/xxx/:id" 字面量（编译产物同样保留）。给 agent 一张"站内有哪些页面"的图
    routes: list = []
    _ROUTE_RE = re.compile(r'''path\s*:\s*["'](\/[A-Za-z0-9_\-./:]*?)["']''')

    def _extract(su: str, body: str) -> None:
        """单包扫描：候选端点 + 签名 + baseURL + 路径碎片 + 前端路由（追加进闭包容器）。"""
        for rm in _ROUTE_RE.finditer(body):
            p = rm.group(1)
            if len(routes) < 60 and p not in routes and not p.endswith((".js", ".css", ".png", ".json")):
                routes.append(p)
        raw_hits = api_pat.findall(body)
        hits = sorted({_clean_hit(m[0] or m[1] or "") for m in raw_hits})
        hits = [h for h in hits if h and _plausible(h)][:60]
        # 签名级提取（互动易考核反馈 P0）：方法+路径+参数键+调用片段。
        # 剪枝：只看含疑似路径的调用（有 / 且长度合理），cap 防超大包拖慢
        sig_this_script = 0
        for pat, default_m in sig_pats:
            for m in pat.finditer(body):
                # 形态一双组：group(1)=方法 group(2)=路径；其余单组=路径
                path = m.group(2) if pat.groups == 2 else m.group(1)
                method = m.group(1).upper() if pat.groups == 2 else default_m
                if not _plausible(path) or "." in path.rsplit("/", 1)[-1]:
                    continue  # 静态资源/文件 URL 不算端点
                snippet = body[m.start(): m.start() + 240]
                if len(signatures) < 60 and sig_this_script < 20:
                    param_keys = []
                    for pm in param_pat.finditer(snippet[len(path):]):
                        pk = pm.group(1)
                        if pk not in param_keys and pk.lower() not in (
                                "get", "post", "put", "delete", "then", "catch", "true", "false"):
                            param_keys.append(pk)
                        if len(param_keys) >= 8:
                            break
                    signatures.append({
                        "method": method,
                        "path": path[:120],
                        "params": param_keys,
                        "snippet": snippet[:180],
                        "source": su[-60:],
                    })
                    sig_this_script += 1
                    if len(signatures) >= 60:
                        break
        # axios 实例 baseURL（baseURL:"/api" 或 baseURL:"https://x"）单独收集
        for bm in re.finditer(r'baseURL\s*[:=]\s*["\']([^"\']{4,120})["\']', body):
            c = _clean_hit(bm.group(1))
            if _plausible(c):
                base_urls.append(c)
        # 路径碎片：带 ≥2 段的引号路径（拼接产物 "/a/b"），与已确认 hits 去重
        for fm in re.finditer(r'["\'](/[A-Za-z0-9_\-]+(?:/[A-Za-z0-9_\-./]+){1,4})["\']', body):
            f = _clean_hit(fm.group(1))
            if _plausible(f) and f not in hits and not f.endswith((".js", ".css", ".png", ".svg")):
                frags.append(f)
        if hits:
            found[su[:96]] = hits  # 审查修复：按完整 URL 键控（同名 main.js 曾互相覆盖丢端点）

    for su in urls:
        if checked >= max_scripts:
            break
        try:
            if cdp_bodies is not None:
                body = (cdp_bodies.get(su) or "")[:2_000_000]
                if not body:
                    continue
                checked += 1
            else:
                jr = client.get(su)
                body = jr.get("text", "")[:2_000_000]
                if not body:
                    continue
                checked += 1
            _extract(su, body)
            for cm in _CHUNK_RE.finditer(body):
                if len(chunk_refs) < 64:
                    chunk_refs.append((cm.group(1), su))
        except Exception:
            continue

    # 懒加载分块第二阶段：解析引用为 URL（脚本目录优先、页面根兜底），
    # 抓取未扫过的 chunk 追加扫描（仅 HTTP 直连路径——挑战壳站额外抓包必失败）
    lazy_scanned = 0
    _scanned_urls = set(found.keys())
    for ref, origin in chunk_refs:
        cu = urljoin(origin, ref)
        if cu not in lazy_urls:
            lazy_urls.append(cu)
    if cdp_bodies is None and lazy_urls:
        for cu in lazy_urls:
            if lazy_scanned >= 8 or checked >= max_scripts * 3:
                break
            if cu[:96] in _scanned_urls:
                continue
            try:
                jr2 = client.get(cu)
                body2 = (jr2.get("text", "") or "")[:2_000_000]
                if not body2:
                    continue
            except Exception:
                continue
            _scanned_urls.add(cu[:96])
            checked += 1
            lazy_scanned += 1
            _extract(cu, body2)
    all_hits = sorted({h for v in found.values() for h in v})
    result = {"url": url, "scripts_checked": checked, "api_candidates": all_hits,
              "base_urls": sorted(set(base_urls))[:20],
              "path_fragments": sorted(set(frags))[:40],
              "endpoint_signatures": signatures[:60],
              "lazy_chunks": lazy_urls[:40],
              "lazy_chunks_scanned": lazy_scanned,
              "frontend_routes": sorted(routes)[:60],
              "by_script": found, "hint": "候选端点需逐个探测验证（带 UA/Referer/cookie 预热）；"
                                          "endpoint_signatures 含方法/路径/参数键，按证据强度优先看；"
                                          "lazy_chunks 是包体里发现的路由级分块（懒加载），"
                                          "未扫到的可配 --max-scripts 扩量或 CDP 路径触发；"
                                          "frontend_routes 是 vue/react 路由配置里的页面路径"
                                          "（含 :param 占位），供逐页规划抓取"}
    if out:
        import os as _os
        p = Path(_os.path.expanduser(out))
        if p.is_dir() or not p.suffix:  # 目录（含尚不存在/无后缀路径）自动补 jsrecon.json
            p = p / "jsrecon.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        result["saved"] = str(p)
    return result


def save_result(result: Dict[str, Any], out: Optional[str] = None,
                as_json: bool = False) -> Path:
    """把结果写到文件（默认 .md；--json 则 .json）。返回路径。"""
    if out:
        import os as _os
        fp = Path(_os.path.expanduser(out))
    else:
        import hashlib
        ext = ".json" if as_json else ".md"
        h = hashlib.sha256(str(result.get("url", "")).encode(), usedforsecurity=False).hexdigest()[:12]
        fp = Path(f"outputs/fetch_{h}{ext}")
    fp.parent.mkdir(parents=True, exist_ok=True)
    if as_json:
        fp.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    else:
        content = result.get("markdown") or result.get("article") or result.get("selector") or result.get("text", "")
        fp.write_text(content, encoding="utf-8")
    return fp


def crawl_url(url: str, depth: int = 2, max_pages: int = 100, allow: Optional[str] = None,
              deny: Optional[str] = None, browser: bool = False, proxy: Optional[str] = None,
              concurrency: int = 4, out: Optional[str] = None,
              respect_robots: bool = False, sitemap: Optional[str] = None,
              same_domain: bool = False) -> Dict[str, Any]:
    """从 URL 递归爬站（对标 Firecrawl crawl CLI）：
    自动生成临时任务包 → v3 引擎递归抓取 → 导出 outputs/<out>.json/csv/xlsx。
    - allow/deny: 正则过滤链接（只爬 /docs/ 等）
    - browser=True: JS 渲染页面
    返回 {name, total, fetched, errors, base_name, files}
    """
    import hashlib
    import json as _json
    import re
    import shutil
    from pathlib import Path
    from urllib.parse import urlparse

    h = hashlib.sha256(url.encode(), usedforsecurity=False).hexdigest()[:10]
    work = Path("outputs/.crawl_tmp")
    # 审查八轮（MEDIUM）：tdir 曾只按 URL 哈希定死、且开跑前 rmtree——并发同 URL 的
    # crawl 会互删对方的 config 与 .running.lock（引擎互斥失效），随后共用同一
    # outputs/items/*.jsonl 与导出文件互相覆盖。改为每轮唯一目录（pid+随机后缀）。
    import os as _os
    import secrets as _secrets
    _uniq = f"{_os.getpid()}{_secrets.token_hex(2)}"
    tdir = work / f"crawl_{h}_{_uniq}"
    (tdir / "modules").mkdir(parents=True, exist_ok=True)
    host = re.sub(r"[^A-Za-z0-9_-]", "_", urlparse(url).netloc or "site")
    base = out or f"crawl_{host}"
    # R42 修复：--out 是文件名前缀（导出固定 outputs/ 下）——传入带目录的路径
    # 时剥离目录成分，否则导出崩在 outputs/<路径>/ 不存在（曾裸栈还 exit 0）
    base = re.sub(r"[\\/]+", "_", base).strip("._") or f"crawl_{host}"
    # 审查八轮（MEDIUM）：同 host 并发 crawl 会静默互相覆盖导出（后跑者赢，先跑者
    # 数据无声丢失）。默认名被另一**存活**进程占用时改用带后缀的名字并明确告知；
    # 顺序重跑（旧锁的 pid 已死）仍沿用原文件名，行为与既有文档一致。
    _baselock = Path("outputs") / f".crawl_base_{base}.lock"
    _lock_mine = False
    try:
        _baselock.parent.mkdir(parents=True, exist_ok=True)
        _busy = False
        if _baselock.exists():
            try:
                _old_pid = int((_baselock.read_text(encoding="utf-8").strip() or "0"))
            except Exception:
                _old_pid = 0
            if _old_pid and _old_pid != _os.getpid():
                try:
                    _os.kill(_old_pid, 0)
                    _busy = True
                except ProcessLookupError:
                    _busy = False
                except PermissionError:
                    _busy = True
                except Exception:
                    _busy = False
        if _busy:
            base = f"{base}_{_uniq[-4:]}"
            import sys as _sys
            print(f"⚠️ outputs/{_baselock.stem[12:]}.* 正被另一 crawl（pid 占用）——"
                  f"本次导出改用 {base}.*，避免互相覆盖", file=_sys.stderr)
        else:
            _baselock.write_text(str(_os.getpid()), encoding="utf-8")
            _lock_mine = True
    except Exception:
        pass
    cfg = {
        "name": f"crawl_{h}",
        "start_urls": [url],
        "queue": {"max_depth": depth, "max_requests": max_pages, "max_concurrency": concurrency},
        "source": {"type": "browser", "pool": True, "stealth": True, "remove_overlays": True}
        if browser else {"type": "http"},
        "rules": [{"match": "regex", "pattern": ".*", "parser": "page"}],
        "parsers": {"page": {
            "type": "html",
            "fields": {"title": {"css": "title", "limit": 300},
                       "text": {"css": "body", "limit": 100000}},
            "extract_links": {"allow": allow, "deny": deny, "same_domain": same_domain},
        }},
        "pipelines": [{"type": "filter", "field": "text", "op": "non_empty"}],
        "storage": {"type": "jsonl", "name": f"crawl_{h}_{_uniq}"},
        "output": {"dir": "outputs", "base_name": base},
        "anti_bot": {"min_interval": 0.2, "max_retries": 2, "respect_robots": respect_robots},
    }
    if sitemap:
        cfg["source"]["sitemap"] = sitemap  # 对标 Crawlee SitemapRequestLoader：sitemap 作为种子
    if proxy:
        cfg["anti_bot"]["proxy"] = proxy
    (tdir / "config.json").write_text(_json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    from .engine_v3 import run_task
    try:
        # 页数上限由 queue.max_requests 控制（--max 是页数不是条目数）
        result = run_task(tdir)
    finally:
        shutil.rmtree(tdir, ignore_errors=True)
        # 临时 jsonl 也清理（异常路径不残留；名字与本次唯一 storage.name 一致）
        try:
            (Path("outputs/items") / f"crawl_{h}_{_uniq}.jsonl").unlink(missing_ok=True)
        except Exception:
            pass
        # 释放 base 占用锁（只有本进程写入的锁才删；并发时用的是别人的锁）
        if _lock_mine:
            try:
                _baselock.unlink(missing_ok=True)
            except Exception:
                pass
    result["base_name"] = base
    # 只报真实存在的导出文件，防止"导出失败还报成功"误导
    files = {}
    for ext in ("json", "csv", "xlsx"):
        fp = Path("outputs") / f"{base}.{ext}"
        if fp.exists():
            files[ext] = str(fp)
    result["files"] = files
    return result
