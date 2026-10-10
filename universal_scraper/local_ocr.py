#!/usr/bin/env python3
"""🖼 本地 OCR：macOS Vision 框架取字（离线、免费、零安装）。

为什么要有它：云端 VLM（`llm.LLMClient.vision`）质量好但花钱、要联网、有配额。
本机 Vision 是**独立第二引擎**，可对同一张图交叉核验——实测抓到过云端模型的
"顺句补字"偏置：表格里明明是"苏州云网通信息科技"，模型写成"苏州云网通信信息科技"。

实测要点（macOS 26.x；本机只需 pyobjc-Cocoa，无需 pyobjc-framework-Vision）：
- 用 `objc.loadBundle` 手动加载 Vision.framework；类名注入到传入的 dict（不污染 globals）
- **recognitionLevel 必须用 0（fast）**：1（accurate）档对中文小字返回乱码（实测整图 13 段全乱）
- `usesLanguageCorrection=True` + `["zh-Hans","en-US"]` 组合最稳
- boundingBox 是归一化坐标且**原点在左下**（y 越大越靠上）；按阅读顺序排序要 `-y`

公开 API：
  available() -> bool                 # 能否跑
  unavailable_reason() -> str         # 不能跑的具体原因（空串=可用）
  ocr_image(src) -> [{text,x,y,w,h}]
  ocr_text(src) -> str                # 阅读顺序拼接
  row_bands(path, x_frac) -> [(y0,y1)]  # 按指定列内的横线切逻辑行
  cross_check_verdict(primary, alt) -> "agree|differ|alt_wins|no_alt"

不可用时一律返回空容器，不抛异常——调用方必须自己判断"空结果 ≠ 成功"（用
`unavailable_reason()` 区分"引擎没跑"与"图上没字"）。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

_VISION_BUNDLE = "/System/Library/Frameworks/Vision.framework"
_NS: Optional[Dict[str, Any]] = None
_LOAD_TRIED = False

ImgSrc = Union[str, Path, bytes]

# 中文字符判断（用于无空格拼接）
def _is_cjk(ch: str) -> bool:
    return "\u4e00" <= ch <= "\u9fff" or "\u3000" <= ch <= "\u303f" or "\uff00" <= ch <= "\uffef"


def _load() -> Optional[Dict[str, Any]]:
    """加载 Vision 框架类（进程内缓存；失败缓存为 None，不反复重试）。"""
    global _NS, _LOAD_TRIED
    if _LOAD_TRIED:
        return _NS
    _LOAD_TRIED = True
    if sys.platform != "darwin" or not Path(_VISION_BUNDLE).exists():
        return None
    try:
        import Foundation
        import objc  # pyobjc-core
    except Exception:
        return None
    if not hasattr(Foundation, "NSData") or not hasattr(Foundation, "NSURL"):
        return None
    ns: Dict[str, Any] = {}
    try:
        objc.loadBundle("Vision", ns, bundle_path=_VISION_BUNDLE)
    except Exception:
        return None
    if "VNImageRequestHandler" not in ns or "VNRecognizeTextRequest" not in ns:
        return None
    _NS = ns
    return _NS


def unavailable_reason() -> str:
    """本机不能跑本地 OCR 的**具体**原因（空串 = 可用）。

    为什么要有它：不可用的 5 种成因（非 darwin / 框架缺失 / pyobjc 缺失 /
    loadBundle 失败 / 类名缺失）若都塌缩成 `available()==False`，排障时无法回答
    "为什么没跑 Vision"，调用方也分不清"引擎没跑"与"图上没字"。"""
    if _load() is not None:
        return ""
    if sys.platform != "darwin":
        return f"非 macOS（{sys.platform}）"
    if not Path(_VISION_BUNDLE).exists():
        return f"缺少 {_VISION_BUNDLE}"
    try:
        import Foundation
        import objc
    except Exception as e:
        return f"pyobjc/Foundation 不可用：{type(e).__name__}: {e}"
    if not hasattr(objc, "loadBundle"):
        return "pyobjc-core 缺 loadBundle（安装不完整）"
    if not hasattr(Foundation, "NSData"):
        return "pyobjc-Foundation 缺 NSData/NSURL（pyobjc 安装不完整）"
    return "Vision 框架加载失败或缺少 VNImageRequestHandler/VNRecognizeTextRequest"


def available() -> bool:
    """本机能否跑本地 Vision OCR。"""
    return _load() is not None


def _handler_for(src: ImgSrc):
    """构造 VNImageRequestHandler：路径走 URL，字节走 NSData。"""
    ns = _load()
    if ns is None:
        return None
    from Foundation import NSData, NSURL
    if isinstance(src, (str, Path)):
        url = NSURL.fileURLWithPath_(str(src))
        return ns["VNImageRequestHandler"].alloc().initWithURL_options_(url, None)
    data = NSData.dataWithBytes_length_(bytes(src), len(bytes(src)))
    return ns["VNImageRequestHandler"].alloc().initWithData_options_(data, None)


def ocr_image(src: ImgSrc, languages: Tuple[str, ...] = ("zh-Hans", "en-US")) -> List[Dict[str, Any]]:
    """识别整图 → [{"text","x","y","w","h"}]（归一化坐标，原点左下）。

    空列表 = 不可用或无文本（调用方自行区分：先 available() 再判断结果）。
    """
    ns = _load()
    handler = _handler_for(src)
    if ns is None or handler is None:
        return []
    req = ns["VNRecognizeTextRequest"].alloc().init()
    req.setRecognitionLevel_(0)                 # fast：accurate 档中文乱码（实测）
    try:
        req.setRecognitionLanguages_(list(languages))
    except Exception:
        pass
    req.setUsesLanguageCorrection_(True)
    try:
        handler.performRequests_error_([req], None)
    except Exception:
        return []
    out: List[Dict[str, Any]] = []
    for obs in (req.results() or []):
        cands = obs.topCandidates_(1)
        if not cands:
            continue
        bb = obs.boundingBox()
        out.append({"text": cands[0].string(),
                    "x": float(bb.origin.x), "y": float(bb.origin.y),
                    "w": float(bb.size.width), "h": float(bb.size.height)})
    return out


def _join_segments(segs: List[Dict[str, Any]]) -> str:
    """同一行内按 x 拼接：两侧都是中日韩字符时不加空格。"""
    segs = sorted(segs, key=lambda s: s["x"])
    buf = ""
    for s in segs:
        t = s["text"]
        if buf and not (_is_cjk(buf[-1]) and _is_cjk(t[:1])):
            buf += " "
        buf += t
    return buf


def ocr_text(src: ImgSrc, line_tol: float = 0.02) -> str:
    """按阅读顺序（先上后下、先左后右）拼成多行文本。

    y 差 ≤ line_tol 的片段视为同一行（表格单元格内换行会被拼成一行）。"""
    segs = ocr_image(src)
    if not segs:
        return ""
    lines: List[List[Dict[str, Any]]] = []
    for s in sorted(segs, key=lambda d: -d["y"]):
        for ln in lines:
            if abs(ln[0]["y"] - s["y"]) <= line_tol:
                ln.append(s)
                break
        else:
            lines.append([s])
    return "\n".join(_join_segments(ln) for ln in lines)


def row_bands(path: Union[str, Path], x_frac: Tuple[float, float] = (0.0, 0.10),
              min_h: int = 8, dark_thr: int = 140) -> List[Tuple[int, int]]:
    """按指定列内的**横线**切逻辑行，返回 [(y0, y1), ...]（像素坐标）。

    用途：扫描件/截图表格的逐行裁剪。为什么锚在窄列：整宽横线统计会把
    "一格多问题"的内部细线也算进来，切出的带不对齐逻辑行（实测把下一行的
    公司名并进上一行）。**序号列每个逻辑行恰好一格**，其上下边界即行界。
    无框线（或 Pillow 缺失）返回 [] —— 调用方需走整图兜底。"""
    try:
        from PIL import Image
        img = Image.open(str(path)).convert("L")
    except Exception:
        return []                                   # 文件不存在/不是图片：按"不可用"处理，不抛
    W, H = img.size
    x0 = max(0, int(W * x_frac[0]))
    x1 = max(x0 + 1, int(W * x_frac[1]))
    px = img.load()
    thr = (x1 - x0) * 0.5
    lines: List[int] = []
    run: List[int] = []
    for y in range(H):
        dark = 0
        for x in range(x0, x1):
            if px[x, y] < dark_thr:
                dark += 1
        if dark > thr:
            run.append(y)
        elif run:
            lines.append(int(sum(run) / len(run)))
            run = []
    if run:
        lines.append(int(sum(run) / len(run)))
    if len(lines) < 3:
        return []
    return [(lines[i] + 1, lines[i + 1] - 1) for i in range(len(lines) - 1)
            if lines[i + 1] - lines[i] > min_h]


def cross_check_verdict(primary: str, alt: str) -> str:
    """两引擎读同一字段的裁决：agree / differ / alt_wins / no_alt。

    `no_alt` 专指"副引擎没读到"（不可用、或该行带内没有该列文本）：必须与
    `differ`（两读都有值但不同）区分开——否则每一行都会被标成"引擎分歧"，
    交付数据里的 `ocr_check` 就是对现实的虚假陈述（实测本地引擎不可用时全表 differ）。

    alt_wins 判据：主读比副读**恰好多一个字**（LLM 顺句补字偏置），且副读是
    完整字段值（以公司/机构后缀收尾、够长、不混入商店词）。
    实测：qwen-vl "苏州云网通信信息科技有限公司" vs Vision "苏州云网通信息科技有限公司"
    —— 前者多插一个"信"，真值以后者为准。
    """
    import re as _re

    def _norm(s: str) -> str:
        return _re.sub(r"[\s（）()·,，.。]", "", s or "")

    a, b = _norm(primary), _norm(alt)
    if not b:
        return "no_alt"
    if a == b:
        return "agree"
    clean = (bool(_re.search(r"(公司|中心|研究院|研究所|学院|银行|医院|局|所|厂|店)$", b))
             and len(b) >= 8
             and not _re.search(r"(官网|商店|市场|助手|Store|软件园|应用宝|豌豆荚|下载)", b)
             and not _re.search(r"(.)\1", b))
    if clean and any(a[:i] + a[i + 1:] == b for i in range(len(a))):
        return "alt_wins"
    return "differ"
