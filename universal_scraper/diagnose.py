# -*- coding: utf-8 -*-
"""diagnose —— 阻断判型器：一次请求，机器可判的"症状 → 处方"。

NMPA 战训（2026-09-10）：弱模型读 markdown 判型表不可靠（曾把瑞数站误判为
"L5 人工通道"直接放弃）；执行命令比阅读判断可靠。本模块把 anti-block-playbook
的判型表变成可执行代码，并对 core.py 的预算记账提供"通道拦截 vs 配额耗尽"
的区分依据（瑞数/Cloudflare 拦截不烧预算，换通道即可）。

本模块保持零内部依赖（不 import core/fetchers），供各方反向安全引用。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# 每类防护：状态码口径 + 响应体特征（正则，宽松匹配防转义变体）+ 处方。
# 特征必须来自真实战例响应体，禁止凭印象添加（误报会让弱模型走错通道）。
BLOCK_SIGNATURES: List[Dict[str, Any]] = [
    {
        "type": "riversafe",
        "name": "瑞数(RiverSecurity)动态防护",
        "status": (412, 403, 200, 202),
        # epub 战训（2026-09）：瑞数挑战壳也以 HTTP 202 返回——曾只认 412/403/200，
        # 202+$_ts 被判"✅ 正常 L0"，弱模型拿着结论去硬抓陷入 0 结果循环。
        # $_ts=window['$_ts'] 这类 JS 赋值指纹足够独特，任何状态码下都作数。
        "any_status": True,
        "patterns": [
            r"\$_ts\s*=\s*window\[\s*['\"]?\$_ts",
            r"\$_ts\.nsd\s*=",
            # 注意：meta refresh / document.location 是通用壳特征（见 SHELL_PATTERNS），
            # 不进瑞数特征——NMPA 复盘：单凭 meta 刷新会误伤普通重定向页
        ],
        "require_any": 1,
        "evidence_hint": "响应体是 $_ts 混淆 JS（任何状态码，实测 412/200/202），需真实浏览器执行算 cookie",
        "prescription": ("走 L3 真实 Chrome：bash scripts/open-debug-chrome.sh 附加 CDP，"
                         "UI 操作触发查询后拦截 XHR JSON（配方 R26 rs_harvest.cjs）。"
                         "禁止在 HTTP 通道重试——curl_cffi 全通道必挂，白白烧请求"),
        "burns_budget": False,
    },
    {
        "type": "cloudflare",
        "name": "Cloudflare 盾",
        "status": (403, 503, 429, 200),
        "patterns": [
            r"Just a moment",
            r"cf-browser-verification",
            r"Attention Required.*Cloudflare",
        ],
        # 嵌件特征：登录页/评论框常内嵌 Turnstile（200 正常页），不能作为 200 判型依据
        "embed_patterns": [
            r"__cf_chl|cf_chl_opt|challenge-platform",
            r"turnstile",
        ],
        "require_any": 1,
        "evidence_hint": "Challenge/Turnstile 页面，headless 必卡",
        "prescription": "L3 弹独立调试 Chrome 人工过一次校验 → 配置 cdp 附加复用会话（R16）",
        "burns_budget": False,
    },
    {
        "type": "aliyun_waf",
        "name": "阿里云 WAF",
        "status": (403, 405, 429),
        "patterns": [
            r"errors\.aliyun\.com",
            r"blocked\s*by\s*WAF",
        ],
        "require_any": 1,
        "evidence_hint": "阿里云 WAF 拦截页",
        "prescription": "L1 curl_cffi(impersonate=chrome) 换指纹重试 → 不通降速/换出口",
        "burns_budget": True,
    },
]

# 通用 JS 壳特征（200 状态 + 极短 body 时才参与判定，避免误伤正常页）。
# 审查修复：拆"强挑战特征"与"弱跳转特征"——document.location 裸赋值是
# 普通跳转页的写法，判成阻断会把可抓的站推去无头浏览器（弱模型误路由）。
STRONG_SHELL_PATTERNS = [r"stoken", r"__js_challenge"]
WEAK_SHELL_PATTERNS = [
    r"document\.location(\.replace|\.href)?\s*=",
    r"window\.location\.(replace|href)\s*=",
    r"http-equiv=[\"']?refresh",
]

# SPA 应用壳特征（200 + 有 UI 壳但无数据 → 建议接口捕获）
SPA_PATTERNS = [
    r"<div[^>]+id=[\"'](?:app|root|__next)[\"']",
    r"__NEXT_DATA__",
    r"data-v-[0-9a-f]{8}",
]


def _decode_body(raw: bytes) -> str:
    if not raw:
        return ""
    try:
        return raw[:65536].decode("utf-8", errors="replace")
    except Exception:
        return ""


def classify_block(status: int, body_text: str, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """纯函数判型：HTTP 状态 + 响应体文本（前 64KB 足够）→ 命中的防护类型。

    返回 {is_block, type, name, evidence, prescription, burns_budget}。
    无命中时 is_block=False。headers 可为 None（大小写不敏感地取 server）。
    供 core._budget_auto_mark 与 cli diagnose 共用。
    """
    headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    body = body_text or ""
    server_masked = set(headers.get("server", "")) <= {"*", " "} and bool(headers.get("server"))
    status = int(status or 0)

    for sig in BLOCK_SIGNATURES:
        # epub 战训修复：any_status 签名（瑞数 $_ts）不受状态码门控——
        # 挑战壳的指纹在响应体里，202/204 等少见码同样作数
        if not sig.get("any_status") and status not in sig["status"]:
            continue
        body_evidence = []
        for pat in sig.get("patterns", []):
            m = re.search(pat, body, re.I)
            if m:
                body_evidence.append(m.group(0)[:60])
        # 嵌件类特征（Turnstile 组件等）：仅非 200 时可作证据——200 正常页内嵌
        # Turnstile 登录框是常见形态，不能据此判"被盾拦"（审查修复）
        if status != 200:
            for pat in sig.get("embed_patterns", []):
                m = re.search(pat, body, re.I)
                if m:
                    body_evidence.append(m.group(0)[:60])
        # 审查修复：Server 打码只做佐证不单独定罪——header-only 证据曾让
        # 任意打码 Server 的普通 403 被判瑞数（跳过记账+错误处方）
        if len(body_evidence) >= int(sig.get("require_any", 1)):
            evidence = list(body_evidence[:3])
            if sig["type"] == "riversafe" and server_masked:
                evidence.append("Server 头打码（佐证）")
            return {
                "is_block": True,
                "type": sig["type"],
                "name": sig["name"],
                "evidence": " | ".join(evidence),
                "prescription": sig["prescription"],
                "burns_budget": bool(sig["burns_budget"]),
            }

    # 强挑战特征不受 <4096 门槛限制（R27 实战：data.stats.gov.cn 限流挑战壳
    # 是 38KB 的混淆 JS 页）。审查修复 P1：先剥离 <noscript>/<script> 再匹配——
    # "Please enable JavaScript" 是全网最常见的 noscript 兜底文案，正常 SPA
    # 首页带着它照样能抓；只看挑战页特有的"标签外+标签内"组合才算数
    if status == 200:
        noscripts = re.findall(r"<noscript[^>]*>(.*?)</noscript>", body, re.I | re.S)
        scripts = re.findall(r"<script[^>]*>(.*?)</script>", body, re.I | re.S)
        outside = re.sub(r"<(script|noscript)[^>]*>.*?</\1>", "", body, flags=re.I | re.S)
        phrase_in_noscript = any(re.search(r"Please\s+enable\s+JavaScript", n, re.I) for n in noscripts)
        # 审查二轮（M）：混淆指纹曾缺 re.I——大写 0X 十六进制漏检
        obfusc_in_script = any(re.search(r"0x[0-9a-f]{2,}|_0x[0-9a-f]+|eval\(function", sc, re.I) for sc in scripts)
        if re.search(r"Please\s+enable\s+JavaScript", outside, re.I) or \
           (phrase_in_noscript and obfusc_in_script):
            return {
                "is_block": True,
                "type": "js_shell",
                "name": "JS 挑战壳（限流触发）",
                "evidence": "200 页命中 Please enable JavaScript"
                            + ("（noscript 提示 + 混淆 JS 脚本）" if phrase_in_noscript else "（正文直出）"),
                "prescription": "降速冷却（≥3s/请求 + 会话复用）；存量会话可用后立即降频；顽固时 L3 真实 Chrome 过一次",
                "burns_budget": True,
            }

    # 通用 JS 壳：仅 200 + 极短 body 时判（避免正常页含 location 赋值误报）
    if status == 200 and len(body) < 4096:
        for pat in STRONG_SHELL_PATTERNS:
            if re.search(pat, body, re.I):
                return {
                    "is_block": True,
                    "type": "js_shell",
                    "name": "JS 挑战壳",
                    "evidence": f"200 但 body 仅 {len(body)}B，命中 {pat}",
                    "prescription": "L1 curl_cffi 重试 → 不通升 L2 无头浏览器渲染",
                    "burns_budget": True,
                }        # 弱特征（location 赋值/meta refresh）= 普通跳转页：非阻断，只给路由提示
        for pat in WEAK_SHELL_PATTERNS:
            if re.search(pat, body, re.I):
                return {
                    "is_block": False,
                    "type": "js_redirect_hint",
                    "name": "JS/ meta 重定向跳板（非阻断）",
                    "evidence": f"短页命中 {pat}",
                    "prescription": "普通跳转：直接请求跳转目标 URL，或 fetch --browser 渲染跟随",
                    "burns_budget": False,
                }

    # SPA 壳提示（不是阻断，是路由建议）
    if status == 200:
        # 审查修复：曾只扫前 8000 字符——重 <head> 的 SPA 页拦截词落在 8000 字符
        # 之外被漏掉，WAF 拦截页误判成 spa_hint。body 已在 _decode_body 截到
        # 64KB，全量扫描成本可忽略（口径同"宁可误报拦截不可漏报"）
        _blk_words = re.search(r"访问被拒|拒绝访问|请求被拦截|Access Denied|blocked|"
                               r"安全验证|请完成验证", body or "", re.I)
        for pat in SPA_PATTERNS:
            if re.search(pat, body, re.I):
                if _blk_words:
                    # OCR R131（M）：SPA 特征曾掩盖未知 WAF 的拒绝页——拦截词
                    # 并存时按拦截优先（宁可误报拦截不可漏报）
                    return {
                        "is_block": True,
                        "type": "waf_plain",
                        "name": "WAF 拦截页（含 SPA 壳特征）",
                        "evidence": f"SPA 特征 {pat} 与拦截词共存",
                        "prescription": "换通道（browser/代理）或按站点处方冷却后重试",
                        "burns_budget": True,
                    }
                return {
                    "is_block": False,
                    "type": "spa_hint",
                    "name": "SPA 应用壳",
                    "evidence": f"命中 SPA 特征 {pat}",
                    "prescription": "数据靠 XHR 拼：先 jsrecon 侦察接口 → capture_all 捕获 → http_json 重放",
                    "burns_budget": False,
                }

    # 无特征但状态码明确是拒绝
    if status in (403, 468):
        return {
            "is_block": True,
            "type": "waf_plain",
            "name": "WAF 指纹拦截",
            "evidence": f"HTTP {status} 且响应体无已知防护特征",
            "prescription": "L1 curl_cffi(impersonate=chrome) 换 TLS 指纹重试",
            "burns_budget": True,
        }
    if status == 429:
        return {
            "is_block": True,
            "type": "rate_limit",
            "name": "限流",
            "evidence": "HTTP 429",
            "prescription": "降速（min_interval 加倍）+ 遵守 Retry-After；别换指纹硬闯",
            "burns_budget": True,
        }
    return {"is_block": False, "type": "ok" if 200 <= status < 300 else "other",
            "name": "正常响应" if 200 <= status < 300 else f"HTTP {status}",
            "evidence": "", "prescription": "", "burns_budget": False}


def looks_like_challenge(status: int, body: str, content_type: str = "") -> bool:
    """挑战壳统一判定助手（供 agent/脚本直接调用，规则与 classify_block 同源不漂移）。

    判定维度（2026-09-11 国家数据考核反馈：正常 SPA 首页仅 3KB 且无混淆脚本，
    曾被自写脚本误判为挑战壳——正常壳 ≠ 限流壳，别只看体积或关键词）：
      - HTTP 200（挑战壳都装正常返回）；
      - 正文命中挑战标记：'Please enable JavaScript'（剥 noscript 规则见
        classify_block）或 stoken/__js_challenge 等强标记；
      - content_type 若提供则须含 text/html（JSON API 被挑战时直接判型失败）。
    返回 True = 建议按配方冷却（≥60s），False = 正常响应（含正常 SPA 壳）。
    """
    if int(status or 0) != 200:
        return False
    if content_type and "html" not in content_type.lower():
        return False
    # 审查修复 P2：不截断——正文已在内存（真挑战壳 38KB，全量扫描成本可忽略）
    v = classify_block(200, body or "")
    return v.get("type") == "js_shell"


def diagnose_quick(url: str, timeout: float = 8.0, proxy: Optional[str] = None) -> Dict[str, Any]:
    """🪶 轻量探针（归档功能请求）：HEAD 预检 + 按需 GET 截断复核，8s 短超时，
    跳过系统代理检测——供代理循环/批量预检快速分诊。结果附 "quick": True。

    判型可信度口径（审查 M1 修复）：HEAD 无响应体，对 body 指纹类（瑞数 $_ts、
    JS 挑战壳）是瞎的——仅 status>=400 且非 412 时 HEAD 结论直接可信（403/429
    判型不依赖 body）；2xx / 412 / 405 / 501 一律回退一次 GET（截断 64KB）复核，
    避免"412 判非阻断 + 200 壳判 L0"的假绿灯。HEAD 网络层异常直接失败
    （审查 L3：不再二次 GET 让死站耗时翻倍）。"""
    result: Dict[str, Any] = {"url": url, "http_backend": "curl_cffi(chrome)", "status": 0,
                              "body_bytes": 0, "block": None, "system_proxy": None, "quick": True}
    kw: Dict[str, Any] = {"timeout": timeout, "impersonate": "chrome", "allow_redirects": True}
    if proxy:
        kw["proxies"] = {"http": proxy, "https": proxy}
    try:
        # 审查三轮（H）：import 曾在 try 外——未装 curl_cffi 时 ImportError 裸崩
        # 而非走下方 error 字典（轻探针在无依赖环境应优雅降级报错）
        from curl_cffi import requests as creq
        resp = creq.head(url, **kw)
        _sc = int(resp.status_code)
        if _sc >= 400 and _sc not in (405, 501, 412):
            # 403/429/468 等判型不依赖 body——HEAD 结论直接可用（省一次 GET）
            body = b""
        else:
            # 2xx（可能是 JS 挑战壳）与 412（瑞数标志码，HEAD 指纹瞎）/405/501：
            # GET 截断复核
            resp = creq.get(url, **kw)
            body = (resp.content or b"")[:65536]
        result["status"] = int(resp.status_code)
        hdrs = dict(getattr(resp, "headers", {}) or {})
        # HEAD 无响应体——体长从 Content-Length 提示；GET 用实际截断长度
        _cl = str(hdrs.get("Content-Length") or hdrs.get("content-length") or "")
        result["body_bytes"] = int(_cl) if _cl.isdigit() else len(body)
        result["block"] = classify_block(result["status"], _decode_body(body), hdrs)
    except Exception as e:
        result["error"] = f"请求失败: {type(e).__name__}: {str(e)[:160]}"
        result["block"] = {"is_block": False, "type": "net_error", "name": "网络层失败",
                           "evidence": result["error"],
                           "prescription": "轻探针失败≠站点封禁；用完整 `cli diagnose` 复核",
                           "burns_budget": False}
    return result


def diagnose(url: str, timeout: float = 25.0, proxy: Optional[str] = None) -> Dict[str, Any]:
    """实测判型：用默认最强 HTTP 通道（curl_cffi chrome 指纹）请求一次 → classify_block。
    附加本机系统代理检查（Clash 劫持会让判型失真，必须先提示）。"""
    result: Dict[str, Any] = {"url": url, "http_backend": "curl_cffi(chrome)", "status": 0,
                              "body_bytes": 0, "block": None, "system_proxy": None}
    # 系统代理检查放最前：被劫持时 412 可能是代理节点 IP 被拦，而非本机通道问题
    try:
        from .net import detect_system_proxy
        sp = detect_system_proxy()
        result["system_proxy"] = sp
    except Exception:
        result["system_proxy"] = None

    try:
        from curl_cffi import requests as creq
        kw: Dict[str, Any] = {"timeout": timeout, "impersonate": "chrome", "allow_redirects": True}
        if proxy:
            kw["proxies"] = {"http": proxy, "https": proxy}
        resp = creq.get(url, **kw)
        body = _decode_body(resp.content or b"")
        result["status"] = int(resp.status_code)
        result["body_bytes"] = len(resp.content or b"")
        result["block"] = classify_block(result["status"], body, dict(resp.headers.items()))
    except Exception as e:
        result["error"] = f"请求失败: {type(e).__name__}: {str(e)[:160]}"
        # OCR R131（L）：异常类型细分——DNS 解析失败与连接超时的处方不同
        _en = type(e).__name__
        if "Resolver" in _en or "getaddrinfo" in str(e) or "Name or service" in str(e):
            _rx = "DNS 解析失败——查域名拼写 / 本机 DNS 配置 / VPN 分流规则"
        elif "timed out" in str(e).lower() or "Timeout" in _en:
            _rx = "连接超时——目标站慢或出口被限速；试加大 timeout 或换出口"
        elif "refused" in str(e).lower():
            _rx = "连接被拒——目标端口未开或本机防火墙拦截"
        else:
            _rx = "先 `cli ip` 查真实出口 + 查系统代理（Clash 劫持常见），再重试"
        result["block"] = {"is_block": False, "type": "net_error", "name": "网络层失败",
                           "evidence": result["error"],
                           "prescription": _rx,
                           "burns_budget": False}
    return result


def format_verdict(d: Dict[str, Any]) -> str:
    """人类可读输出（与 `cli ip` 同风格）。"""
    lines: List[str] = []
    sp = d.get("system_proxy") or {}
    if sp.get("enabled"):
        lines.append(f"⚠️  系统代理已启用（{', '.join(sp.get('sources') or [])}）"
                     f"——判型结果可能失真，必要时先退出 Clash 再 diagnose")
    elif sp.get("processes"):
        # 商标网战训：代理进程在跑但 env/scutil 均未启用 ≠ 劫持——降为提示
        lines.append(f"ℹ️  本机有代理进程（{', '.join(sp.get('processes') or [])}）"
                     f"但系统代理未启用——请求大概率未被劫持")
    if d.get("error"):
        lines.append(f"❌ {d['error']}")
        blk = d.get("block") or {}
        if blk.get("prescription"):
            lines.append(f"💡 处方: {blk['prescription']}")
        return "\n".join(lines)
    lines.append(f"🎯 {d['url'][:80]}")
    lines.append(f"   状态: HTTP {d['status']}  body {d['body_bytes']}B  通道: {d['http_backend']}")
    blk = d.get("block") or {}
    name = blk.get("name", "?")
    if blk.get("type") == "ok":
        lines.append(f"   判型: ✅ {name}——可直接抓（L0）")
    elif blk.get("type") == "spa_hint":
        lines.append(f"   判型: 🧩 {name}（非阻断）")
        lines.append(f"   证据: {blk.get('evidence', '')}")
        lines.append(f"   💡 处方: {blk.get('prescription', '')}")
    else:
        lines.append(f"   判型: 🧬 {name}")
        lines.append(f"   证据: {blk.get('evidence', '')}")
        lines.append(f"   💡 处方: {blk.get('prescription', '')}")
        # 审查修复：仅"确为阻断且不烧台账"才提示换通道——js_redirect_hint/HTTP 5xx
        # 等非阻断类型曾也被打上"通道拦截"标签误导路由
        if blk.get("is_block") and blk.get("burns_budget") is False:
            lines.append("   （此类型为通道拦截：不计入域名封锁台账，换通道即可过）")
    return "\n".join(lines)
