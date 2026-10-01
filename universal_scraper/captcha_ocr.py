#!/usr/bin/env python3
"""文字点选验证码求解（gsxt 战训沉淀，2026-09）：ddddocr det 检测字框 +
cls 识别每框内容 → 按题面顺序输出点击坐标中心。

gsxt/GT4 文字点选码形态："请依次点击【国】【家】【税】"——主图上散布字符，
按题面顺序逐个点击。ddddocr 的 det（检测）+ classification（单字识别）组合
已实测可零延迟自动点选（doctor 有体检但此前无任何消费方——本模块补上）。

图标九宫格点选（"选 N 个符合右图"）语义匹配超出 OCR 能力，**保留人工在环**
（captcha_bridge.cjs 附加真 Chrome，用户直接在窗口里点）——这是战训口径，
不要试图用 OCR 硬解图标题。
"""
from __future__ import annotations

import io
import threading
from typing import Any, Dict, List, Optional

_OCR_DET = None
_OCR_CLS = None
# 审查修复（R129）：并发 worker 首用时的 check-then-act 竞态会建两个 DdddOcr 实例
# （onnx 会话持 native 资源，被覆盖的那个靠 GC 兜底）——双检锁串行化首初始化
_OCR_INIT_LOCK = threading.Lock()
_OCR_INFER_LOCK = threading.Lock()  # OCR R131（M）：onnx 推理非线程安全——串行化调用
_OCR_WARNED: set = set()


def _ocr_warn_once(msg: str) -> None:
    """OCR 内部告警限频（同类消息只打一次，防每框刷屏）。"""
    if msg in _OCR_WARNED:
        return
    _OCR_WARNED.add(msg)
    try:
        import sys
        print(f"⚠️ captcha_ocr: {msg}", file=sys.stderr)
    except Exception:
        pass


def has_ddddocr() -> bool:
    try:
        __import__("ddddocr")
        return True
    except Exception:
        return False


def _det():
    global _OCR_DET
    if _OCR_DET is None:
        with _OCR_INIT_LOCK:
            if _OCR_DET is None:
                import ddddocr
                _OCR_DET = ddddocr.DdddOcr(det=True, show_ad=False)
    return _OCR_DET


def _cls():
    global _OCR_CLS
    if _OCR_CLS is None:
        with _OCR_INIT_LOCK:
            if _OCR_CLS is None:
                import ddddocr
                _OCR_CLS = ddddocr.DdddOcr(show_ad=False)
    return _OCR_CLS


def detect_boxes(image_bytes: bytes) -> List[Dict[str, Any]]:
    """检测图中文字框：[{x1,y1,x2,y2,cx,cy,text}]。det 定位 + cls 识别。"""
    det, cls = _det(), _cls()
    # OCR R131（M）：onnxruntime 会话并发推理非线程安全——engine_v3 多 worker
    # 并发 detect_boxes 曾可能互踩内部状态；串行化（OCR 本身毫秒级，锁开销可忽略）
    with _OCR_INFER_LOCK:
        boxes = det.detection(image_bytes) or []
    out: List[Dict[str, Any]] = []
    if not boxes:
        return out
    # 审查修复：整图只解码一次（此前每框 Image.open 重新解码全图，N 框 = N 次全图解码）。
    # PIL 是 ddddocr 的依赖，随其必装；解码失败仅放弃裁剪（逐框兜底）
    pil_img = None
    try:
        from PIL import Image
        pil_img = Image.open(io.BytesIO(image_bytes))
    except Exception:
        pil_img = None
    for b in boxes:
        try:
            x1, y1, x2, y2 = [int(v) for v in b]
        except (TypeError, ValueError):
            continue
        crop = None
        if pil_img is not None:
            try:
                buf = io.BytesIO()
                pil_img.crop((x1, y1, x2, y2)).save(buf, format="PNG")
                crop = buf.getvalue()
            except Exception as e:
                # OCR R131（L）：静默吞错排障无门——限频告警一次
                _ocr_warn_once(f"验证码裁剪失败（{type(e).__name__}: {str(e)[:60]}）")
                crop = None
        text = ""
        if crop is not None:
            try:
                with _OCR_INFER_LOCK:
                    text = (cls.classification(crop) or "").strip()
            except Exception as e:
                _ocr_warn_once(f"验证码识别失败（{type(e).__name__}: {str(e)[:60]}）")
                text = ""
        # 裁剪失败不回退整图识别（审查 HIGH）：整图分类出的多字文本会污染
        # solve_text_clicks 的匹配（假匹配 → 坐标必错）。text 置空走 unmatched，
        # 调用方按设计退人工在环。
        out.append({"x1": x1, "y1": y1, "x2": x2, "y2": y2,
                    "cx": (x1 + x2) // 2, "cy": (y1 + y2) // 2, "text": text})
    return out


def _prompt_targets(prompt: str) -> List[str]:
    """从题面提取目标字符（OCR R131 H）："依次点击：国 家 税" 曾把指令词
    "依次点击" 也当目标——每轮多 4 个必然 unmatched 的假目标。
    优先级：【】内字符 > 最后一个冒号后段 > 去标点全文（旧行为兜底）。
    审查八轮（H）：指令词剔除曾用全局 re.sub 在拼接后无分隔的串上替换——
    目标字本身是"点/击/请"（点选验证码防脚本的真实设计）时被吞掉，
    cli 守卫（pts and not unmatched）放行 → 自动点下错误坐标并提交。
    修法：冒号后段是明确目标，不再剔除；指令剔除只用于无冒号兜底分支，
    且只锚定段首（剥不干净的宁可多目标走 unmatched 退人工，不吞真目标）。"""
    import re
    m = re.findall(r"【(.)】", prompt or "")
    if m:
        return list(m)
    parts = re.split(r"[：:]", prompt or "")
    seg = parts[-1] if len(parts) > 1 else (prompt or "")
    seg = "".join(seg.split())
    out = [ch for ch in seg if ch not in "，,。.（）()【】[]“”\"'；;、"]
    if len(parts) > 1:
        return out
    # 无冒号兜底：只剥段首的连续指令前缀
    tail = "".join(out)
    tail = re.sub(r"^(?:依次点击|请点击|依序点击|按顺序点击|按顺序|依次|请|点击)+", "", tail)
    return list(tail) if tail else out


def solve_text_clicks(image_bytes: bytes, prompt: str) -> Dict[str, Any]:
    """按题面 prompt（如 "依次点击：国 家 税"）输出点击坐标序列。

    匹配策略（审查修复 P2，两段式）：先全库精确匹配（bt == t），再包含匹配
    （t in bt）——多字框命中时按字符在框内的位置计算点击中心，绝不点框中心
    （框中心在两字之间=必错点）。某字无命中时该位返回 None 并在 unmatched
    里说明——调用方应退人工而不是瞎点。
    返回 {"points": [[x,y],...], "matched": [...], "unmatched": [...],
          "boxes": [...]}"""
    targets = _prompt_targets(prompt)
    boxes = detect_boxes(image_bytes)
    points: List[Optional[List[int]]] = []
    matched: List[str] = []
    unmatched: List[str] = []
    used = set()

    def _pick(pred):
        for i, b in enumerate(boxes):
            if i in used:
                continue
            bt = (b.get("text") or "").strip()
            if bt and pred(bt):
                return i
        return None

    def _point(i: int, t: str) -> List[int]:
        b = boxes[i]
        bt = (b.get("text") or "").strip()
        if bt == t or len(bt) <= 1:
            return [b["cx"], b["cy"]]
        # 多字框：按题字在识别串中的位置算字心（框中心=两字之间，必错）
        try:
            pos = bt.index(t)
        except ValueError:
            pos = 0
        x1, x2 = int(b["x1"]), int(b["x2"])
        cx = int(x1 + (x2 - x1) * (pos + 0.5) / max(1, len(bt)))
        return [cx, int(b["cy"])]

    for t in targets:
        # 两段式：先精确匹配（单字框优先），再包含匹配（多字框按字心偏移）
        i = _pick(lambda bt, _t=t: bt == _t)
        if i is None:
            i = _pick(lambda bt, _t=t: _t in bt)
        if i is not None:
            used.add(i)
            points.append(_point(i, t))
            matched.append(t)
        else:
            points.append(None)
            unmatched.append(t)
    return {"points": points, "matched": matched, "unmatched": unmatched,
            "boxes": boxes, "prompt_targets": targets}


def simple_ocr(image_bytes: bytes) -> str:
    """整图识别（图形验证码字符串类）。未装 ddddocr 抛 RuntimeError。"""
    with _OCR_INFER_LOCK:  # OCR R131（M）：onnx 推理串行化（同 detect_boxes）
        return (_cls().classification(image_bytes) or "").strip()
