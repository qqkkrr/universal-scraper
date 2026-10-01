#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""万能自适应引擎：自动探测目标站特征 → 选择最优取数策略 → 自修复闭环。

升级链: HTTP直抓 → TLS指纹伪装 → 无头浏览器 → 有头浏览器(登录提示)
每次失败自动升级；成功后缓存策略到站点注册表，下次跳过试错。

用法:
  python3 -m universal_scraper.adaptive "https://target.com/list"
 或在 Python: from universal_scraper.adaptive import adaptive_fetch
"""
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from curl_cffi import requests as cffi_requests

STRATEGY_FILE = Path(__file__).resolve().parent.parent / "configs" / "_adaptive_strategies.json"
ESCALATION = ["http", "curl_cffi", "browser_headless", "browser_visible"]
LABELS = {"http": "HTTP 直抓", "curl_cffi": "TLS 指纹伪装",
          "browser_headless": "无头浏览器", "browser_visible": "有头浏览器"}
# 审查八轮（MEDIUM）：save_strategy 是"读-改-写"（tmp+replace 只防半截文件，不防
# 丢更新）——实测两线程各存一个域名后缓存只剩后写者，先写者的策略被静默丢弃。
# 进程内用线程锁互斥整个读改写（跨进程并发仍需外层互斥，属既有前提）。
_SAVE_LOCK = __import__("threading").Lock()
# 审查八轮（H）：缓存写失败的一次性告警 flag（只读技能目录下每次都告警会刷屏）
_SAVE_WARNED = False


def _load_strategies() -> dict:
    if STRATEGY_FILE.exists():
        try:
            data = json.loads(STRATEGY_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                # 审查八轮（M）：域值非 dict（旧版格式/手编 "domain": "browser_headless"）
                # 曾在 get_cached_strategy/save_strategy 的 .get/.setdefault 上
                # AttributeError——载入期剔除（与 PoolState/QuotaLedger 的载入
                # 校验同款式）
                bad = [k for k, v in data.items() if not isinstance(v, dict)]
                if bad:
                    print(f"⚠️ 策略缓存条目非 dict，剔除: {bad[:3]}", file=sys.stderr)
                    for k in bad:
                        del data[k]
                return data
            print(f"⚠️ 策略缓存顶层非 dict（{type(data).__name__}），本轮视为空缓存", file=sys.stderr)
        except Exception as e:
            # OCR R131（M）：缓存损坏曾静默当空——每次都全链路重试且无任何线索
            print(f"⚠️ 策略缓存损坏（{type(e).__name__}），本轮视为空缓存", file=sys.stderr)
    return {}


def _save_strategies(data: dict):
    # OCR R131（H）：并发/中断下的非原子写曾留下半截 JSON（下轮全链路重试）。
    # 临时文件 + 原子替换
    STRATEGY_FILE.parent.mkdir(parents=True, exist_ok=True)
    import tempfile as _tf, os as _os
    _fd, _tmp = _tf.mkstemp(dir=str(STRATEGY_FILE.parent), suffix=".tmp")
    try:
        with _os.fdopen(_fd, "w", encoding="utf-8") as _f:
            _f.write(json.dumps(data, ensure_ascii=False, indent=1))
        _os.replace(_tmp, STRATEGY_FILE)
    except Exception:
        try:
            _os.unlink(_tmp)
        except OSError:
            pass
        raise


def _save_strategies_quiet(data: dict) -> bool:
    """审查八轮（H）：只读技能目录（site-packages/非属主用户/chmod）下
    mkdir/mkstemp 的 PermissionError 曾从 save_strategy 原样炸出——adaptive_fetch
    成功拿到 html 后在 save_strategy(url, strategy) 上崩溃，成功结果整体丢失；
    失败路径同理把"所有策略均失败"的正常返回替换成 traceback。缓存写失败只
    降级为一次性告警，不影响主流程返回契约（永不抛）。"""
    global _SAVE_WARNED
    try:
        _save_strategies(data)
        return True
    except Exception as e:
        if not _SAVE_WARNED:
            _SAVE_WARNED = True
            print(f"⚠️ 策略缓存写入失败（{type(e).__name__}: {str(e)[:60]}），本次不落缓存: {STRATEGY_FILE}",
                  file=sys.stderr)
        return False


def get_cached_strategy(url: str):
    domain = urlsplit(url).hostname or ""
    return _load_strategies().get(domain, {}).get("best_strategy")


def save_strategy(url: str, strategy: str):
    domain = urlsplit(url).hostname or ""
    if not domain:
        return
    # OCR R131（M）：瞬时失败（网络抖动/出口切换）曾把此前验证过的好策略覆盖成
    # failed——下次明明可用的策略被跳过。failed 只在新站点（尚无有效策略）时记录
    # OCR R131 终审（M）：prev 曾是独立 _load_strategies() 调用——与下方 data 的
    # 二次读取构成 TOCTOU 双读（并发 save 时 prev 可能过时）。合并为一次读
    # 审查八轮：读改写整体进锁（并发 save 曾互相覆盖：两线程各存一个域名，
    # 缓存只剩后写者）
    with _SAVE_LOCK:
        data = _load_strategies()
        prev = data.get(domain, {}).get("best_strategy")
        if strategy == "failed" and prev not in (None, "", "failed"):
            return
        data.setdefault(domain, {})["best_strategy"] = strategy
        data.setdefault(domain, {})["updated"] = time.strftime("%Y-%m-%d %H:%M")
        # OCR R131（M）：TOCTOU 修复——prev 和 data 曾是两次独立 _load_strategies()，
        # 并发 save 时后写覆盖先写。合并为一次读（prev 直接取自 data）
        _save_strategies_quiet(data)


def _detect_block(text: str, status: int) -> str:
    t = (text or "")[:5000].lower()
    if status in (403, 406, 412):
        return "captcha" if ("验证" in t or "captcha" in t) else "waf"
    if status == 429:
        return "rate_limit"
    if "百度安全验证" in t:
        return "captcha"
    if "wappass" in t or "verify.meituan" in t:
        return "login"
    # JS challenge 壳页特征：短页面 + 含跳转/挑战代码（不能仅凭短就判定——短页面可能合法）
    if len(t) < 300 and re.search(r"document\.(?:location|write)|setTimeout.*location|__js_challenge|stoken", t):
        return "js_challenge"
    # OCR R7：其余 4xx/5xx 无特征也按拦截处理——否则错误页（404/500）会被当成功返回
    if status >= 400:
        return "waf"
    return "none"


# ── 策略实现 ──

def _fetch_http(url: str, timeout: int = 20):
    import urllib.request
    import urllib.error
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as e:
        # OCR R7（H）：4xx/5xx 曾直接抛 HTTPError——状态码和拦截页到不了
        # _detect_block，验证码/WAF/限流分类对 HTTP 策略形同虚设。如实返回让上层分类
        try:
            raw = e.read()
        except Exception:
            raw = b""
        status = e.code
    # OCR R131（M）：非 UTF-8 站点（gb2312/gbk）曾按 ignore 解码成乱码，
    # 登录探测关键词全部失配——先试 utf-8 再回退 gb18030
    try:
        return status, raw.decode("utf-8")
    except UnicodeDecodeError:
        return status, raw.decode("gb18030", "ignore")


def _fetch_curl_cffi(url: str, timeout: int = 25):
    r = cffi_requests.get(url, timeout=timeout, impersonate="chrome")
    return r.status_code, r.text


def _adaptive_session_dir() -> str:
    """OCR R131（H）：固定 /tmp/us_adaptive 可被本地其他用户预置符号链接劫持
    （浏览器会话写往攻击者指定目录）。改按用户名分目录 + 0700 + 符号链接拒绝。"""
    import getpass, tempfile as _tf  # pyflakes：os 未用（mkdir 用 Path 方法）
    base = Path(_tf.gettempdir()) / f"us_adaptive_{getpass.getuser()}"
    try:
        if base.is_symlink():
            raise RuntimeError("session dir 是符号链接，拒绝使用")
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
        return str(base)
    except RuntimeError:
        raise  # 硬化检查（符号链接劫持拒绝）不许被下面的兜底吞掉
    except Exception:
        return _tf.mkdtemp(prefix="us_adaptive_")


def _fetch_browser(url: str, headless: bool):
    """用已有的 BrowserFetcher 类（内部安全处理子进程）。"""
    import importlib
    fm = importlib.import_module(".modules.fetchers", package="universal_scraper")
    from .protocols import Request as Req
    source = {"type": "browser", "url": url, "pool": False,
              "headless": headless, "scroll_count": 2,
              "scroll_wait_ms": 2000, "pagination": {"type": "none"}}
    fetcher = fm.BrowserFetcher(source, {}, {"session_dir": _adaptive_session_dir()})
    resp = fetcher.fetch(Req(url=url))
    # OCR R131（H）：`or 200` 曾把失败的 status=0 伪装成成功 200——_detect_block
    # 拿到伪造状态误判放行。失败就如实返回 0
    return resp.status if resp.status else 0, resp.text or ""


# ── 核心入口 ──

def adaptive_fetch(url: str, keyword: str = "", log=None) -> dict:
    """自适应取数：自动升级策略直到成功。返回 {strategy, status, html, error}。"""
    _log = log or (lambda m: print(m))

    domain = urlsplit(url).hostname or ""
    cached = get_cached_strategy(url)
    start_idx = ESCALATION.index(cached) if cached in ESCALATION else 0

    last_err, html, status = "", "", 0

    for i, strategy in enumerate(ESCALATION[start_idx:], start_idx):
        label = LABELS.get(strategy, strategy)
        _log(f"🔄 [{domain}] 策略 {i+1}/{len(ESCALATION)}: {label}…")
        try:
            if strategy == "http":
                status, html = _fetch_http(url)
            elif strategy == "curl_cffi":
                status, html = _fetch_curl_cffi(url)
            elif strategy in ("browser_headless", "browser_visible"):
                headless = strategy == "browser_headless"
                status, html = _fetch_browser(url, headless)
        except Exception as e:
            last_err = f"{type(e).__name__}: {str(e)[:80]}"
            _log(f"  ⚠️ {label} 异常: {last_err}")
            continue

        block = _detect_block(html, status)
        if block == "none" and html.strip():
            _log(f"  ✅ {label} 成功（{len(html)} 字符）")
            save_strategy(url, strategy)
            return {"strategy": strategy, "status": status, "html": html,
                    "items": [], "error": ""}

        _log(f"  ⚠️ {label} 被拦截[{block}]: {len(html)} 字符")
        last_err = f"拦截类型 {block}"

        if block == "login":
            # OCR R131（M）：登录提示时立即存 browser_visible 曾把未验证的策略
            # 固化成"最优"——只提示，不落缓存
            _log("  💡 请在浏览器中登录该网站，然后点「🍪 导入会话」→「重跑」")

    save_strategy(url, "failed")
    _log(f"❌ 所有策略均失败: {last_err}")
    return {"strategy": "failed", "status": 0, "html": "", "items": [],
            "error": f"所有策略均失败: {last_err}"}


# ── 独立运行 ──

if __name__ == "__main__":
    # sys 已在模块级导入（pyflakes：局部重导入曾遮蔽警告）
    url = sys.argv[1] if len(sys.argv) > 1 else "https://example.com"
    result = adaptive_fetch(url, log=print)
    print(json.dumps({"strategy": result["strategy"], "status": result["status"],
                      "html_len": len(result["html"]), "error": result["error"]},
                     ensure_ascii=False))
