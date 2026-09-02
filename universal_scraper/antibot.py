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

def solve_ddddocr(image_path: Path, det: bool = False) -> Optional[str]:
    """用 ddddocr 识别图形验证码。返回识别文本（可能为空/错误，调用方需重试）。"""
    import ddddocr
    ocr = ddddocr.DdddOcr(show_ad=False)
    img = image_path.read_bytes()
    if det:
        return ocr.classification_det(img)  # 目标检测模式（多字符）
    return ocr.classification(img)


# ---------------------------------------------------------------- Level 2: 滑块缺口

def slider_gap_x(image_path: Path, bg_path: Optional[Path] = None) -> Optional[int]:
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
    req = urllib.request.Request(host + "/in.php", data=form.encode(),
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
    """NopeCHA 图形验证码识别（简单图片，返回预测文本）。"""
    b64 = base64.b64encode(image_path.read_bytes()).decode()
    body = json.dumps({"type": "image", "image_data": [b64]}).encode()
    req = urllib.request.Request(
        service + "/solve",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.load(r)
    data = data.get("data") or {}
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
    answer_file.write_text(ans, encoding="utf-8")
    return ans


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
                result["answer"] = ans
            else:
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

    if af and result["answer"] is not None:
        af.write_text(str(result["answer"]), encoding="utf-8")
    return result


def wait_for_answer_file(answer_file: Path, timeout: int = 300) -> Optional[str]:
    """轮询等待答案文件出现（人机/外部进程）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if answer_file.exists():
            ans = answer_file.read_text(encoding="utf-8").strip()
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
    # CWAP/WZWS 滑块 WAF（期刊/政务站常见）：必须排在 verify 前，命中即判为 waf
    ("waf", re.compile(r"wzws-waf-cgi|CWAP-waf|waf_slider_verify|wzws_waf|waf-cgi|WZWS-RAY|滑动填|请完成安全验证|向右滑动|拖动滑块|拼图完成", re.I)),
    ("verify", re.compile(r"验证中心|安全验证|滑动验证|点选验证|人机验证|拼图验证|spiderindefence|访问过于频繁|异常访问|请求过于频繁|操作频繁|安全检测", re.I)),
    # 淘宝系会话标记（盒马战例）：RGV587 页 / mtop ret=TIMEOUT::——冷却+换路线，不是重试
    ("session_flagged", re.compile(r"RGV587_ERROR|RGV587|ERRCODE_NOT_LOGIN|WAIT_DIRECT|punish|TIMEOUT::|接口超时", re.I)),
    ("login", re.compile(r"请先登录|尚未登录|登录后访问|扫码登录|账号登录|立即登录|passport\.|/login\b|login\.aspx|欢迎登录", re.I)),
    ("captcha", re.compile(r"captcha|图形验证|输入验证码|请输入验证码|verify_code|turnstile|recaptcha", re.I)),
    ("rate_limit", re.compile(r"too many requests|rate limit|频率限制|访问太快|限流", re.I)),
    ("anti_bot", re.compile(r"waf|风控|反爬|该ip|您的ip|被禁止|blocked|forbidden by|403 forbidden", re.I)),
]

# 这些状态码 + 内容特征可直接判为"封禁/需要升级"
STATUS_BLOCK = {403: "403", 429: "429", 503: "503", 502: "502"}


def detect_block(status: int = 200, text: str = "", headers: Optional[Dict[str, str]] = None,
                 url: str = "") -> Dict[str, Any]:
    """识别响应是否被反爬拦截。

    返回 {"kind": "none"|"cloudflare"|"verify"|"login"|"captcha"|"rate_limit"|"anti_bot"|"403"|"429"|...,
          "detail": 命中片段, "status": int}
    kind != "none" 时调用方应：换代理/换 UA 重试，多次命中则升级浏览器模式。
    """
    h = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    status = int(status or 0)
    # 1) 状态码直接判
    if status in STATUS_BLOCK:
        return {"kind": STATUS_BLOCK[status], "detail": f"HTTP {status}", "status": status}
    if status >= 400:
        return {"kind": "http_error", "detail": f"HTTP {status}", "status": status}
    # 2) 头部特征：cf-* 头在 Cloudflare CDN 透传的正常 200（DockerHub/V2EX 等）上同样存在，
    #    只有"200 但内容极小"（真挑战页特征）才判拦截，否则误杀正常站
    if any(key in h for key in ("cf-ray", "cf-chl", "cf-cache-status")):
        if len((text or "").strip()) < 2048:
            return {"kind": "cloudflare", "detail": "header cf-* + tiny body", "status": status}
        return {"kind": "none", "detail": "cdn passthrough", "status": status}
    # 3) 正文特征（只在前 20KB 匹配，避免全文误判）
    t = (text or "")[:20000].lower()
    if not t:
        return {"kind": "none", "detail": "", "status": status}
    for kind, pat in BLOCK_PATTERNS:
        m = pat.search(t)
        if m:
            frag = t[max(0, m.start() - 12):m.end() + 12].replace("\n", " ")[:60]
            return {"kind": kind, "detail": frag, "status": status}
    return {"kind": "none", "detail": "", "status": status}


def block_summary(blocks: Dict[str, int]) -> str:
    """把多次封禁统计转成人类可读摘要（WebUI/日志用）。"""
    if not blocks:
        return "无"
    return ", ".join(f"{k}×{v}" for k, v in sorted(blocks.items(), key=lambda x: -x[1]))
