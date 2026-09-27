#!/usr/bin/env python3
"""反爬/验证码子系统：四级方案。

Level 1  反检测浏览器（patchright/playwright 自动探测，见 bridge）
Level 2  简单验证码自动解：ddddocr（图形 OCR）+ OpenCV（滑块缺口）
Level 3  强验证码代解：2captcha / nopecha HTTP API（付费）
Level 4  人机结合：把验证码图存下来，等你/人工输入答案（solve-file 协议）

核心是 **solve-file 协议**：浏览器桥遇到验证码时把图存成
  <dir>/captcha_<seq>.png
并输出 {"type":"captcha","imageFile":...,"token":...}，
本模块把答案写到  <dir>/captcha_<seq>.answer
桥读到答案文件后自动填码继续 —— 同一浏览器会话不丢 cookie/指纹。
"""
from __future__ import annotations
from .core import assert_http_url

import base64
import json
import re
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional


# ---------------------------------------------------------------- 策略解析

def resolve_strategy(anti_cfg: Dict[str, Any]) -> str:
    """返回最终策略：auto / dddddocr / opencv_slider / 2captcha / nopecha / human / none。"""
    cap = anti_cfg.get("captcha", {}) or {}
    s = str(cap.get("strategy", "auto")).lower()
    if s in ("auto",):
        if _has_ddddocr():
            return "ddddocr"
        return "human"
    return s


def _has_ddddocr() -> bool:
    try:
        __import__("ddddocr")  # 可用性探测：仅验证可导入
        return True
    except Exception:
        return False


def _has_cv2() -> bool:
    try:
        __import__("cv2")  # 可用性探测：仅验证可导入
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- Level 2: ddddocr

_DDDDOCR_OBJ = None
_DDDDOCR_LOCK = threading.Lock()


def _ddddocr_obj():
    """OCR R131（M）：DdddOcr 曾每次调用重新实例化——onnx 模型加载百 ms 级，
    连续多张验证码时成为瓶颈。进程级缓存，首调用加载一次。"""
    global _DDDDOCR_OBJ
    with _DDDDOCR_LOCK:
        if _DDDDOCR_OBJ is None:
            import ddddocr
            _DDDDOCR_OBJ = ddddocr.DdddOcr(show_ad=False)
        return _DDDDOCR_OBJ


def solve_ddddocr(image_path: Path, det: bool = False) -> Optional[str]:
    """用 ddddocr 识别图形验证码。返回识别文本（可能为空/错误，调用方需重试）。"""
    ocr = _ddddocr_obj()
    img = image_path.read_bytes()
    with _DDDDOCR_LOCK:  # onnx 推理非线程安全——缓存实例后必须自串行化
        if det:
            return ocr.classification_det(img)  # 目标检测模式（多字符）
        return ocr.classification(img)


# ---------------------------------------------------------------- Level 2: 滑块缺口

def slider_gap_x(image_path: Path) -> Optional[int]:
    """OpenCV 找滑块缺口 x 坐标。

    image_path 为滑块背景图（或含滑块的合成图）。
    用 Canny 边缘 + 模板/轮廓方法；返回缺口左侧 x 像素。
    """
    import cv2
    __import__("numpy")  # 预加载 numpy（cv2 依赖），不直接使用
    img = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
    if img is None:
        return None
    edges = cv2.Canny(img, 100, 200)
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best = None
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        # 缺口通常是背景图中一块明显差异的矩形区域
        if 10 <= w <= 200 and 10 <= h <= 200 and (best is None or w * h > best[0]):
            best = (w * h, x, y, w, h)
    return best[1] if best else None


# ---------------------------------------------------------------- Level 3: 2captcha / nopecha

def solve_2captcha(
    image_path: Path,
    api_key: str,
    service: str = "2captcha.com",
    timeout: int = 120,
) -> Optional[str]:
    """2captcha 图形验证码代解：上传→轮询→返回文本。"""
    b64 = base64.b64encode(image_path.read_bytes()).decode()
    host = f"https://{service}"
    form = urllib.parse.urlencode({"key": api_key, "method": "base64", "body": b64})
    # OCR R131（M）：in.php 上传曾不过 SSRF 边界（res.php 过了）——service 配置
    # 里的伪协议/私网地址可直连。与轮询同口径
    _in_url = assert_http_url(host + "/in.php")
    req = urllib.request.Request(_in_url, data=form.encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = r.read().decode()
    if not resp.startswith("OK|"):
        return None
    captcha_id = resp.split("|", 1)[1]
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(5)
        q = urllib.parse.urlencode({"key": api_key, "action": "get", "id": captcha_id})
        with urllib.request.urlopen(assert_http_url(host + "/res.php?" + q), timeout=30) as r:
            resp = r.read().decode()
        if resp.startswith("OK|"):
            return resp.split("|", 1)[1]
        if "CAPCHA_NOT_READY" in resp:
            continue
        return None
    return None


def solve_nopecha(
    image_path: Path,
    api_key: str,
    service: str = "https://api.nopecha.com",
) -> Optional[str]:
    """NopeCHA 图形验证码识别（简单图片，返回预测文本）。
    service 经 assert_http_url 过 SSRF 边界（OCR R131：与 2captcha 同口径）。"""
    b64 = base64.b64encode(image_path.read_bytes()).decode()
    body = json.dumps({"type": "image", "image_data": [b64]}).encode()
    req = urllib.request.Request(
        assert_http_url(service + "/solve"),
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.load(r)
    # 顶层响应同样可能是 list/标量——裸 .get 会 AttributeError（与内层 data 同口径）
    if not isinstance(data, dict):
        return None
    data = data.get("data") or {}
    # OCR R131（L）：NopeCHA 偶发 data 为 list/str——.get 裸炸 AttributeError
    if not isinstance(data, dict):
        return None
    return data.get("text") or data.get("solution")


# ---------------------------------------------------------------- Level 4: 人机结合

def human_solve(image_path: Path, answer_file: Path, prompt: str = "") -> str:
    """人机结合：打印提示（stderr，防污染 MCP/WebUI 的 stdout 协议），等用户输入答案（交互式）。"""
    import sys as _sys
    print("\n" + "=" * 60, file=_sys.stderr)
    print(f"[人机验证] 请打开图片查看验证码: {image_path}", file=_sys.stderr)
    if prompt:
        print(f"提示: {prompt}", file=_sys.stderr)
    print("输入验证码后回车（或输入 q 退出）: ", end="", flush=True, file=_sys.stderr)
    ans = input().strip()
    if ans.lower() == "q":
        # OCR R131（H）：'q' 是退出指令而非验证码——曾把 "q" 写进答案文件并被
        # 桥提交为验证码解答。审查二轮（H）：改写空串后 wait_for_answer_file 的
        # `if ans:` 会当"还没答"继续挂满 300s——用显式退出哨兵，wait 侧识别
        ans = _QUIT_SENTINEL
    answer_file.write_text(ans, encoding="utf-8")
    return "" if ans == _QUIT_SENTINEL else ans


# ---------------------------------------------------------------- 统一入口（solve-file 协议）

def solve_captcha_file(
    image_file: str,
    anti_cfg: Dict[str, Any],
    answer_file: Optional[str] = None,
    seq: int = 0,
) -> Dict[str, Any]:
    """按配置解一张验证码图，返回 {'answer': str|None, 'strategy': str, 'error': str}。

    - 如果给了 answer_file，先看外部是否已写好答案（人机/外部程序），
      否则自动解并写入 answer_file（供桥轮询读取）。
    """
    cap = anti_cfg.get("captcha", {}) or {}
    strategy = resolve_strategy(anti_cfg)
    img = Path(image_file)
    af = Path(answer_file) if answer_file else None
    result: Dict[str, Any] = {"strategy": strategy, "answer": None, "error": ""}

    # 配置里直接给了答案（测试/已知验证码场景）
    if cap.get("answer"):
        result["answer"] = str(cap["answer"])
        result["from"] = "config"
        if af:
            af.write_text(result["answer"], encoding="utf-8")
        return result

    # 外部答案已就绪（人工/其他进程已写好）
    if af and af.exists():
        ans = af.read_text(encoding="utf-8").strip()
        if ans:
            result["answer"] = ans
            result["from"] = "external"
            return result

    try:
        if strategy == "ddddocr":
            # 简单图形验证码：识别后清理非字母数字（保留常见字符）
            ans = solve_ddddocr(img)
            if ans:
                ans = re.sub(r"[^0-9A-Za-z]", "", ans)
            if ans:
                result["answer"] = ans
            else:
                # 审查七轮：识别串全是非字母数字（如"@@@"）时曾清洗成空串还
                # 不报错——调用方拿到 answer=""/error="" 与成功无法区分
                result["error"] = "ddddocr 识别为空"
        elif strategy == "opencv_slider":
            x = slider_gap_x(img)
            result["answer"] = str(x) if x is not None else None
            if x is None:
                result["error"] = "未检测到缺口"
        elif strategy == "2captcha":
            result["answer"] = solve_2captcha(img, cap.get("api_key", ""), cap.get("service", "2captcha.com"))
            if not result["answer"]:
                result["error"] = "2captcha 未返回答案"
        elif strategy == "nopecha":
            result["answer"] = solve_nopecha(img, cap.get("api_key", ""))
            if not result["answer"]:
                result["error"] = "nopecha 未返回答案"
        elif strategy == "human":
            import sys as _sys
            if not _sys.stdin.isatty():
                # 守护进程/WebUI 无终端：绝不能 input() 永久阻塞
                result["error"] = "非交互环境（stdin 非 tty），无法人工输入验证码"
            else:
                result["answer"] = human_solve(img, af or Path(str(img) + ".answer"), cap.get("prompt", ""))
        else:
            result["error"] = f"未知策略: {strategy}"
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"

    # 审查三轮（H）+ 四轮复核：human_solve 对 q 返回空串，truthy 检查才不会把
    # 已写入 answer_file 的退出哨兵覆盖成空串（上轮 is not None 判定仍放行空串，
    # 覆盖依旧发生）。空/None 一律不落盘——wait 侧空文件=未答语义不受影响
    if af and result["answer"]:
        af.write_text(str(result["answer"]), encoding="utf-8")
    return result


_QUIT_SENTINEL = "__quit__"  # 人工应答的退出指令哨兵（空串与"尚未应答"无法区分）


def wait_for_answer_file(answer_file: Path, timeout: int = 300) -> Optional[str]:
    """轮询等待答案文件出现（人机/外部进程）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        # OCR R131（M）：exists→read 的 TOCTOU——文件在两步间被外部进程替换/删除
        # 时 read 抛异常曾炸掉等待循环。读取失败按"还没写好"继续轮询
        try:
            ans = answer_file.read_text(encoding="utf-8").strip()
        except OSError:
            ans = ""
        if ans == _QUIT_SENTINEL:
            return ""  # 用户主动放弃：立即返回空答案，不挂满超时
        if ans:
            return ans
        time.sleep(2)
    return None


# ---------------------------------------------------------------- 封禁检测（对标 Crawlee block-detection / cloudscraper）
# 统一识别"HTTP 200 但实际被风控"的页面：Cloudflare 挑战、安全验证、登录墙、验证码、限流等，
# 供 HttpClient/引擎在拿到响应后判断是否要 换代理 / 换 UA / 升级浏览器 / 重试。

BLOCK_PATTERNS = [
    # 注意：不能用裸词 cloudflare 判拦截——大量站点内容页正常提及该词（技术博客/云厂商文档）
    ("cloudflare", re.compile(r"cf-challenge|cf_clearance|just a moment|__cf_chl|challenges\.cloudflare\.com|checking your browser", re.I)),
    # login 提前于 waf/verify（R10 复查修正：waf 词表含"向右滑动/拖动滑块"等
    # 滑块短语，登录墙普遍内嵌滑块组件——waf 在前会让登录墙被判成封禁）
    ("login", re.compile(r"请先登录|尚未登录|登录后访问|扫码登录|账号登录|立即登录|passport\.|/login\b|login\.aspx|欢迎登录", re.I)),
    # CWAP/WZWS 滑块 WAF（期刊/政务站常见）：命中即判为 waf
    ("waf", re.compile(r"wzws-waf-cgi|CWAP-waf|waf_slider_verify|wzws_waf|waf-cgi|WZWS-RAY|滑动填|请完成安全验证|向右滑动|拖动滑块|拼图完成", re.I)),
    # 淘宝系会话标记（盒马战例）：RGV587 页 / mtop ret=TIMEOUT::——冷却+换路线，不是重试。
    # 这组是 JSON 业务令牌（有意不门控）
    ("session_flagged", re.compile(r"RGV587_ERROR|RGV587|ERRCODE_NOT_LOGIN|WAIT_DIRECT|punish|TIMEOUT::|接口超时", re.I)),
    # captcha：中文动作短语保留长页判定；英文泛词下沉短页表
    ("captcha", re.compile(r"图形验证|输入验证码|请输入验证码|verify_code", re.I)),
]

# 通用词家族（rate_limit/verify/anti_bot）：会出现在正常文章/页脚里
# （"429 Too Many Requests"教程、"您的IP"页脚、"403 Forbidden"文档）——
# 只对 <10KB 页面作数（审查 P1：曾全尺寸匹配，硬停了正常内容任务）
BLOCK_PATTERNS_GATED = [
    ("verify", re.compile(r"验证中心|安全验证|滑动验证|点选验证|人机验证|拼图验证|spiderindefence|访问过于频繁|异常访问|请求过于频繁|操作频繁|安全检测", re.I)),
    ("rate_limit", re.compile(r"too many requests|rate limit|频率限制|访问太快|限流", re.I)),
    ("anti_bot", re.compile(r"该ip|您的ip|403 forbidden|forbidden by", re.I)),
]
_GATE_SIZE = 10000

# 短页/泛词封禁指纹：只在短页（<2KB）上作数——长文档里这些词是正常内容
SHORT_BODY_BLOCK_PATTERNS = [
    ("banned", re.compile(r"封禁|违规行为|违规访问|禁止访问|访问异常|身份异常|存在异常|已被限制|已被封|账户异常", re.I)),
    ("anti_bot", re.compile(r"waf|风控|反爬|被禁止|blocked", re.I)),
    ("captcha", re.compile(r"captcha|turnstile|recaptcha", re.I)),
]

# Tier1 厂商结构指纹（Crawl4AI antibot_detector 哲学，2026-09 精读采纳）：
# 这些是封禁页【独有结构标记】，绝不会出现在正常内容/JSON 里——因此不限页面
# 大小都查。检测取向：误报有兜底（换会话重试/升级浏览器可救），漏报致命
# （小白拿到垃圾数据还以为成功）。
STRUCTURAL_BLOCK_PATTERNS = [
    ("waf", re.compile(r"reference\s*#\s*\d+\.[0-9a-f]+\.\d+\.[0-9a-f]+", re.I)),                # Akamai Reference #
    ("cloudflare", re.compile(r"challenge-form[\s\S]{0,400}?__cf_chl_f_tk=", re.I)),             # CF 挑战表单
    ("cloudflare", re.compile(r'<span\s+class="cf-error-code">\d{4}</span>', re.I)),             # CF 1020/1015 等
    ("cloudflare", re.compile(r"/cdn-cgi/challenge-platform/\S+orchestrate", re.I)),             # CF JS 挑战
    ("waf", re.compile(r"window\._pxappid\s*=|captcha\.px-cdn\.net", re.I)),                     # PerimeterX/HUMAN
    ("captcha", re.compile(r"captcha-delivery\.com", re.I)),                                      # DataDome
    ("waf", re.compile(r"_incapsula_resource|incapsula\s+incident\s+id", re.I)),                 # Imperva/Incapsula
    ("waf", re.compile(r"sucuri\s+website\s+firewall", re.I)),                                    # Sucuri
    ("waf", re.compile(r"kpsdk\.scriptstart\s*=\s*kpsdk\.now\(\)", re.I)),                       # Kasada
]

# Tier2 散文式封禁短语：语义上像正常句子，可能出现在转载文章里——只对
# 短页（<10KB，Crawl4AI Tier2 口径）作数。v1 路径误报会硬停任务，必须保守。
TIER2_PROSE_BLOCK_PATTERNS = [
    ("waf", re.compile(r"pardon\s+our\s+interruption", re.I)),                                    # Akamai 挑战页
    ("anti_bot", re.compile(r"access\s+to\s+this\s+page\s+has\s+been\s+blocked", re.I)),         # PX 封禁页标题
    ("anti_bot", re.compile(r"blocked\s+by\s+network\s+security", re.I)),                        # Reddit 等大 SPA 壳内嵌文案
]
_TIER2_PROSE_MAX_SIZE = 10000

# ---- Tier3 结构完整性（渲染后空壳检测）----
_STRUCTURAL_MAX_SIZE = 50000
_CONTENT_ELEMENTS_RE = re.compile(r"<(?:p|h[1-6]|article|section|li|td|a|pre)\b", re.I)
_SCRIPT_TAG_RE = re.compile(r"<script\b", re.I)
_STYLE_BLOCK_RE = re.compile(r"<style\b[\s\S]*?</style>", re.I)
_SCRIPT_BLOCK_RE = re.compile(r"<script\b[\s\S]*?</script>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_BODY_RE = re.compile(r"<body\b", re.I)
_DATA_PRE_RE = re.compile(r"<body[^>]*>\s*<pre[^>]*>\s*[{\[]", re.I)


def _looks_like_data(text: str) -> bool:
    """JSON/XML API 响应（含浏览器 <pre> 包裹的渲染 JSON）——不是封禁页。"""
    s = (text or "").lstrip()
    if not s:
        return False
    if s[0] in ("{", "["):
        return True
    head = s[:10].lower()
    if head.startswith("<html") or head.startswith("<!"):
        return bool(_DATA_PRE_RE.search(s[:500]))
    return False


def structural_integrity(html: str) -> list:
    """Tier3 结构完整性信号（对 <50KB 的非数据 HTML）：
    minimal_text / no_content_elements / script_heavy_shell / no_body。
    HTTP 直抓的 SPA 壳是【合法形态】（另有 jsrecon/capture 处方），
    本函数只应配合 rendered=True 用于"渲染后仍空壳"的判定。"""
    html = html or ""
    if not html or len(html) > _STRUCTURAL_MAX_SIZE or _looks_like_data(html):
        return []
    if not _BODY_RE.search(html):
        return ["no_body"]
    m = re.search(r"<body\b[^>]*>([\s\S]*)</body>", html, re.I)
    body = m.group(1) if m else html
    stripped = _STYLE_BLOCK_RE.sub("", _SCRIPT_BLOCK_RE.sub("", body))
    visible = len(_TAG_RE.sub("", stripped).strip())
    signals = []
    if visible < 50:
        signals.append("minimal_text")
    content_elements = len(_CONTENT_ELEMENTS_RE.findall(html))
    if content_elements == 0:
        signals.append("no_content_elements")
    if _SCRIPT_TAG_RE.findall(html) and content_elements == 0 and visible < 100:
        signals.append("script_heavy_shell")
    return signals


def looks_like_empty_shell(html: str) -> bool:
    """渲染后空壳判定（Crawl4AI Tier3 口径）：≥2 个结构信号，或 1 信号且页很小。"""
    sig = structural_integrity(html)
    if len(sig) >= 2:
        return True
    return len(sig) == 1 and len(html or "") < 5000

# 这些状态码 + 内容特征可直接判为"封禁/需要升级"
STATUS_BLOCK = {403: "403", 429: "429", 503: "503", 502: "502"}


def detect_block(status: int = 200, text: str = "", headers: Optional[Dict[str, str]] = None,
                 url: str = "", rendered: bool = False) -> Dict[str, Any]:
    """识别响应是否被反爬拦截。

    返回 {"kind": "none"|"cloudflare"|"verify"|"login"|"captcha"|"rate_limit"|"anti_bot"|"403"|"429"|...,
          "detail": 命中片段, "status": int}
    kind != "none" 时调用方应：换代理/换 UA 重试，多次命中则升级浏览器模式。

    rendered=True（浏览器渲染后调用）时追加 Tier3 结构完整性检测：
    渲染完仍是空壳 = 软封锁（kind=empty_shell）。HTTP 直抓的 SPA 壳是合法
    形态（另有 jsrecon/capture 处方），绝不传 rendered=True 误判。"""
    h = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    status = int(status or 0)
    _ct = h.get("content-type", "")
    _is_json = "json" in _ct.lower()
    # 1) 状态码直接判
    if status in STATUS_BLOCK:
        return {"kind": STATUS_BLOCK[status], "detail": f"HTTP {status}", "status": status}
    if status >= 400:
        return {"kind": "http_error", "detail": f"HTTP {status}", "status": status}
    # 2) 头部特征：cf-* 头在 Cloudflare CDN 透传的正常 200（DockerHub/V2EX 等）上同样存在。
    #    审查修复（误伤复盘）：只有"200 且 content-type 是 HTML 且内容极小"才判拦截——
    #    CF 前置的 JSON API 小响应（{"code":0,"msg":"ok"}）曾被误杀整站
    _cf_cdn = False   # 见过 cf-* 头（CDN 前置）；仅用于末尾 detail 文案
    if any(key in h for key in ("cf-ray", "cf-chl", "cf-cache-status")):
        # Tier1 结构指纹优先于 CDN 透传判定：CF 前置站送来 ≥2KB 挑战页时
        # 结构标记仍要抓（审查修复：原逻辑提前 return none 漏掉这类封锁）
        _t_cf = (text or "")[:20000].lower()
        if _t_cf and not _looks_like_data(text or ""):
            for kind, pat in STRUCTURAL_BLOCK_PATTERNS:
                m = pat.search(_t_cf)
                if m:
                    frag = _t_cf[max(0, m.start() - 12):m.end() + 12].replace("\n", " ")[:60]
                    return {"kind": kind, "detail": frag, "status": status}
        # 审查八轮（HIGH）修复：此处在"tiny html 但无挑战信号"与"大页"两条路径上
        # 曾直接 `return none/cdn passthrough` —— 只要响应带 cf-ray，下文全部检测
        # （BLOCK_PATTERNS 散文指纹、BLOCK_PATTERNS_GATED、SHORT_BODY_BLOCK_PATTERNS、
        # rendered 空壳判定）被整体跳过：实测同一段"账号异常/访问过于频繁"正文，
        # 无 cf 头判 waf/banned，带 cf-ray 判 none —— 拦截页被当正常内容入库
        # （fetchers 的"封禁页绝不入库"硬闸因此失效）。改为不 return，落入下方
        # 通用检测；只有真正无任何拦截信号时才在末尾返回 none（detail 保留 cdn 说明）。
        _cf_cdn = True
        if ("html" in _ct.lower() or not _ct) and len((text or "").strip()) < 2048:
            # R37 修复（P0）：裸 cf-* 透传头 + 小 HTML 曾误杀 CDN 前置的正常小页
            # （example.com 就架在 Cloudflare 后面）——需要实际挑战信号才判拦截：
            # cf-mitigated: challenge 头，或正文里的挑战关键词
            _cf_mitigated = str(h.get("cf-mitigated", "")).lower() == "challenge"
            _challenge_kw = any(k in _t_cf for k in
                                ("just a moment", "checking your browser", "challenge-platform",
                                 "cf-chl-bypass", "attention required", "安全验证", "人机验证"))
            if _cf_mitigated or _challenge_kw:
                return {"kind": "cloudflare", "detail": "cf 挑战信号 + tiny html body", "status": status}
    # 3) 正文特征（只在前 20KB 匹配，避免全文误判）
    t = (text or "")[:20000].lower()
    if not t:
        # 渲染后连正文都没有（status=200 空响应）——直接空壳
        if rendered and not (text or "").strip():
            return {"kind": "empty_shell", "detail": "结构完整性: 空响应", "status": status}
        return {"kind": "none", "detail": "", "status": status}
    # 3.5) Tier1 厂商结构指纹：不限大小（数据响应排除——JSON 里这些标记
    #      只可能来自被抓取的内容本身）
    if not _looks_like_data(text or ""):
        for kind, pat in STRUCTURAL_BLOCK_PATTERNS:
            m = pat.search(t)
            if m:
                frag = t[max(0, m.start() - 12):m.end() + 12].replace("\n", " ")[:60]
                return {"kind": kind, "detail": frag, "status": status}
        # Tier2 散文短语：只对短页作数（长文章里这些句子可能是正常内容——
        # v1 路径误报会硬停任务，必须保守）
        if len((text or "").strip()) < _TIER2_PROSE_MAX_SIZE:
            for kind, pat in TIER2_PROSE_BLOCK_PATTERNS:
                m = pat.search(t)
                if m:
                    frag = t[max(0, m.start() - 12):m.end() + 12].replace("\n", " ")[:60]
                    return {"kind": kind, "detail": frag, "status": status}
        # 大页深扫：剥掉脚本/样式再查【结构指纹】（封禁文案常埋在 100KB+ 的
        # CSS/JS 下面）；头尾各扫 30KB。散文短语不进深扫——大页误报代价过高
        if len(text or "") > 20000:
            _stripped = _STYLE_BLOCK_RE.sub("", _SCRIPT_BLOCK_RE.sub("", (text or "")[:500000]))
            # 头尾拼接在 _stripped ≤60KB 时会把内容自拼接出人为接缝（结构指纹可能
            # 跨缝假命中）——整段能一次扫完时直接扫原文，超大页才用头尾窗口
            _deep = _stripped if len(_stripped) <= 60000 else _stripped[:30000] + _stripped[-30000:]
            for kind, pat in STRUCTURAL_BLOCK_PATTERNS:
                m = pat.search(_deep.lower())
                if m:
                    frag = _deep[max(0, m.start() - 12):m.end() + 12].replace("\n", " ")[:60]
                    return {"kind": kind, "detail": "大页深扫: " + frag, "status": status}
    for kind, pat in BLOCK_PATTERNS:
        m = pat.search(t)
        if m:
            frag = t[max(0, m.start() - 12):m.end() + 12].replace("\n", " ")[:60]
            return {"kind": kind, "detail": frag, "status": status}
    # 3.6) 通用词家族 + Tier2 散文短语：只对 <10KB 页面作数（长文章/页脚里
    #      这些词句可能是正常内容——硬停任务的误报必须保守）
    if len((text or "").strip()) < _GATE_SIZE:
        for kind, pat in BLOCK_PATTERNS_GATED:
            m = pat.search(t)
            if m:
                frag = t[max(0, m.start() - 12):m.end() + 12].replace("\n", " ")[:60]
                return {"kind": kind, "detail": frag, "status": status}
    # 4) 短页专属封禁指纹（裁判文书网战训：93B 封禁页被判"页面太短→已下架"继续
    #    抓了 1.3 万条）。封禁/违规词只在短页作数——长文档里这些词是正常内容；
    #    JSON 错误封套（{"msg":"访问异常"}）是 API 的业务拒绝不是页面封禁，排除
    if len((text or "").strip()) < 2048 and not _is_json:
        for kind, pat in SHORT_BODY_BLOCK_PATTERNS:
            m = pat.search(t)
            if m:
                frag = t[max(0, m.start() - 12):m.end() + 12].replace("\n", " ")[:60]
                return {"kind": kind, "detail": frag, "status": status}
    # 5) Tier3 结构完整性（仅渲染后）：HTTP 直抓的 SPA 壳合法，渲染后空壳 = 软封锁
    if rendered:
        sig = structural_integrity(text or "")
        if len(sig) >= 2 or (len(sig) == 1 and len(text or "") < 5000):
            return {"kind": "empty_shell", "detail": f"结构完整性: {', '.join(sig)}",
                    "status": status}
    return {"kind": "none", "detail": "cdn passthrough" if _cf_cdn else "", "status": status}


# 短页连击阈值：连续 N 页 body 都这么短且长度一致 → 判异常（封禁页特征：每次返回同一文案）
SHORT_PAGE_STREAK_LIMIT = 3
SHORT_PAGE_MAX_BYTES = 512


class ShortPageStreak:
    """连续同长短页启发式（裁判文书网战训）：封禁/拦截页每次返回几乎相同的
    小响应。正常列表页每页长度各异；连续 N 页都 ≤max_bytes 且长度完全一致 → 异常。"""

    def __init__(self, limit: int = SHORT_PAGE_STREAK_LIMIT,
                 max_bytes: int = SHORT_PAGE_MAX_BYTES):
        self.limit = limit
        self.max_bytes = max_bytes
        self._streak = 0
        self._last_len = -1

    def feed(self, body_len: int) -> bool:
        """喂入本页字节数，返回是否已构成异常连击（是则调用方应硬停机）。"""
        if 0 < body_len <= self.max_bytes:
            self._streak = self._streak + 1 if self._last_len == body_len else 1
            self._last_len = body_len
        else:
            self._streak = 0
            self._last_len = -1
        return self._streak >= self.limit

    def reset(self) -> None:
        """正常页调用：连击清零（审查修复：此前只喂坏页不重置，"连续"退化成
        "累计"，合法运行中零星坏页会累积触发假停机）。"""
        self._streak = 0
        self._last_len = -1


def block_summary(blocks: Dict[str, int]) -> str:
    """把多次封禁统计转成人类可读摘要（WebUI/日志用）。"""
    if not blocks:
        return "无"
    return ", ".join(f"{k}×{v}" for k, v in sorted(blocks.items(), key=lambda x: -x[1]))
