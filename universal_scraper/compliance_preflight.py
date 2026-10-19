#!/usr/bin/env python3
"""合规预检（compliance preflight；不联网、不写业务数据）。

与 universal_scraper.compliance（旧的合规工具箱）分开，避免覆盖已有
ComplianceGate/AdaptiveThrottle 等实现。

用途：开工前把"技术能抓"与"允许抓"分开，输出风险等级与必办事项。
重要：工程检查清单，不构成法律意见；最终合规判断由用户/法务确认。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

LEVELS = ("green", "amber", "red")

_PII_HINTS = {
    "high": ("手机号", "手机", "电话", "身份证", "证件", "真实姓名", "实名", "邮箱",
             "email", "phone", "mobile", "id_card", "idcard", "realname", "real_name",
             "银行卡", "支付", "订单", "地址", "住址", "精确位置", "gps"),
    "medium": ("用户id", "user_id", "userid", "买家", "卖家id", "seller_id", "nickname",
               "昵称", "头像", "avatar", "ip属地", "ip_location", "属地", "信用",
               "芝麻", "粉丝", "关注", "留言", "评论", "comment", "reply",
               "生日", "性别", "学校", "公司"),
}
_BYPASS_HINTS = (
    "x-sign", "x_sign", "x-mini-wua", "x_mini_wua", "x5sec", "x-5sec",
    "签名算法", "逆向签名", "设备指纹", "device fingerprint",
    "fingerprint spoof", "指纹伪造", "滑块破解", "过滑块", "验证码破解",
    "captcha bypass", "风控降权", "绕过风控", "改机", "群控", "umid", "utdid",
)


def _collect_field_names(cfg: Dict[str, Any]) -> List[str]:
    names: List[str] = []

    def add_fields(d: Any) -> None:
        if isinstance(d, dict):
            names.extend(str(k) for k in d)

    src = cfg.get("source") or {}
    if isinstance(src, dict):
        add_fields(src.get("fields"))
    rec = cfg.get("record") or {}
    if isinstance(rec, dict):
        add_fields(rec.get("fields"))
    det = cfg.get("detail") or {}
    if isinstance(det, dict):
        for spec in det.get("extract") or []:
            if isinstance(spec, dict) and spec.get("name"):
                names.append(str(spec["name"]))
    parsers = cfg.get("parsers") or {}
    if isinstance(parsers, dict):
        for p in parsers.values():
            if isinstance(p, dict):
                add_fields(p.get("fields"))
    return sorted(set(names))


def assess(cfg: Dict[str, Any], purpose: str = "", legal_basis: str = "",
           authorized: bool = False, public_only: bool = True,
           scale: int = 0, retention_days: int = 0) -> Dict[str, Any]:
    """返回结构化风险评估。level: green/amber/red。"""
    text = json.dumps(cfg, ensure_ascii=False, default=str).lower()
    fields = _collect_field_names(cfg)
    hits: Dict[str, List[str]] = {"high": [], "medium": []}
    for f in fields:
        low = f.lower()
        for level in ("high", "medium"):
            if any(h.lower() in low for h in _PII_HINTS[level]) and f not in hits[level]:
                hits[level].append(f)
    bypass = sorted({h for h in _BYPASS_HINTS if h.lower() in text})

    findings: List[Dict[str, Any]] = []
    if bypass:
        findings.append({"code": "bypass_protection", "severity": "red",
                         "message": "配置/说明涉及绕过技术保护措施（签名/设备指纹/滑块/风控降权）",
                         "evidence": bypass[:8]})
    if not public_only:
        # 非公开数据与字段识别无关：没识别出 PII 也不代表可以抓
        findings.append({"code": "non_public", "severity": "red",
                         "message": "声明目标包含非公开数据（私信/订单/后台等）"})
    if hits["high"]:
        _high_ok = bool(authorized and public_only and legal_basis and retention_days > 0)
        findings.append({"code": "sensitive_personal_data",
                         "severity": "amber" if _high_ok else "red",
                         "message": "包含高风险个人信息字段（手机/证件/实名/地址/支付等）",
                         "evidence": hits["high"]})
    if hits["medium"]:
        findings.append({"code": "personal_data", "severity": "amber",
                         "message": "包含可识别到个人的字段（用户ID/昵称/头像/IP/留言/信用等）",
                         "evidence": hits["medium"]})
    if hits["high"] or hits["medium"]:
        if not authorized:
            findings.append({"code": "no_authorization", "severity": "amber",
                             "message": "未声明已获得授权/合法性基础"})
        if scale >= 1000:
            findings.append({"code": "large_scale", "severity": "amber",
                             "message": f"规模 {scale} 条，属于批量个人信息处理，需评估最小必要与安全措施"})
        if retention_days <= 0:
            findings.append({"code": "no_retention", "severity": "amber",
                             "message": "未声明保存期限；个人信息不应无限期保存"})
        if not legal_basis:
            findings.append({"code": "no_legal_basis", "severity": "amber",
                             "message": "未填写合法性基础（同意/合同必要/法定义务/公共利益等）"})

    sev = {f["severity"] for f in findings}
    level = "red" if "red" in sev else ("amber" if "amber" in sev else "green")
    actions: List[str] = []
    if level == "red":
        actions.append("停止：不要执行采集；先移除绕过手段或改为公开/授权数据路径")
    if any(f["code"] in ("personal_data", "sensitive_personal_data") for f in findings):
        actions.extend([
            "做最小必要：删除非交付必需字段（头像/信用/精确IP等）",
            "脱敏/聚合：用户ID做不可逆哈希，交付层不保留原始昵称+ID组合",
            "写明目的、合法性基础、保存期限与删除方式",
        ])
    if any(f["code"] == "large_scale" for f in findings):
        actions.append("批量处理前做个人信息保护影响评估（PIPIA），并限制访问与留存")
    if scale >= 1000:
        actions.append("交付前抽样复核，记录采集时间、来源、口径与缺口")
    return {"level": level, "findings": findings, "fields": fields,
            "personal_data": bool(hits["high"] or hits["medium"]),
            "bypass_detected": bool(bypass), "purpose": purpose,
            "legal_basis": legal_basis, "authorized": bool(authorized),
            "public_only": bool(public_only), "scale": int(scale or 0),
            "retention_days": int(retention_days or 0), "actions": actions,
            "disclaimer": "工程检查清单，不构成法律意见；最终合规判断请由用户/法务确认。"}


def render(report: Dict[str, Any]) -> str:
    icon = {"green": "✅", "amber": "⚠️", "red": "⛔"}.get(report.get("level"), "•")
    lines = [f"{icon} 合规预检：{str(report.get('level', '')).upper()}"]
    if report.get("purpose"):
        lines.append(f"   用途: {report['purpose']}")
    for f in report.get("findings", []):
        ev = ""
        if f.get("evidence"):
            ev = f"（{', '.join(map(str, f.get('evidence', [])))[:160]}）"
        lines.append(f"   - [{f.get('severity')}] {f.get('message')}{ev}")
    for a in report.get("actions", []):
        lines.append(f"   → {a}")
    if report.get("disclaimer"):
        lines.append(f"   {report['disclaimer']}")
    return "\n".join(lines)


def load_source(path: Path) -> Dict[str, Any]:
    p = Path(path)
    if p.is_dir():
        cfg = json.loads((p / "config.json").read_text(encoding="utf-8"))
    else:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(cfg, dict):
        raise ValueError("配置顶层必须是对象")
    return cfg
