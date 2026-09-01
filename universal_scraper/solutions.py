#!/usr/bin/env python3
"""🔧 失败自动诊断与解决方案引擎。

任务失败时，根据错误信息/日志自动判定失败类型，返回「可照做的解决步骤」。
WebUI 在任务失败时把 solution 展示给使用者（登录 / IP风控 / 验证码 / 选择器 / 入口失效…）。
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional


def classify_failure(error_text: str = "", messages: Optional[list] = None,
                     url: str = "") -> str:
    """把失败信息归类。返回 failure_type。"""
    txt = (error_text or "") + " " + " ".join(messages or [])[:2000] + " " + (url or "")
    t = txt.lower()

    # 0) 返回的是整页 HTML（异常页/错误页）：先看是否被 WAF/阻断
    if re.search(r"<!doctype|<html", t):
        if re.search(r"403|forbidden|被阻断|请求被|拦截|waf|_fec_sbu|fec_wrapper|#encoded#", t):
            return "ip_blocked"
        if re.search(r"404|not found|找不到|无法访问|不存在", t):
            return "entry_invalid"
    # 1) 入口/连接失效（404、连接拒绝、域名、入口）
    if re.search(r"404|not found|找不到|无法连接|connection refused|connectionerror|"
                 r"err_connection|无法访问|入口|dns|解析.*失败|getaddrinfo", txt, re.I):
        # 404 也可能是页面本身不存在；若同时提到登录/验证码则优先登录
        if not re.search(r"登录|验证码|滑块|登录墙", txt, re.I):
            return "entry_invalid"
    # 2) IP 风控 / WAF（403/412/429、请求被阻断、阿里云）
    if re.search(r"403|412|429|请求被阻断|被阻断|forbidden|waf|antibot|安全防护|"
                 r"访问过于频繁|被拦截|请求被拒绝", txt, re.I):
        if re.search(r"登录|验证码|滑块", txt, re.I):
            return "captcha_or_login"
        return "ip_blocked"
    # 3) 验证码 / 滑块（比“登录”更具体，优先）
    if re.search(r"验证码|滑块|拖动|安全验证|点选验证|captcha|扫码|完成验证", txt, re.I):
        return "captcha"
    # 4.4) 页面有响应但没有正文 / 表格 / 链接
    if re.search(r"页面无正文|无正文|没有正文|正文.*为空|无内容|页面.*空|未发现表格|未发现链接", txt, re.I):
        return "no_content"
    # 4) 登录 / 登录墙
    if re.search(r"登录|请先登录|需要登录|登录墙|session|会话已过期|账号", txt, re.I):
        return "login_required"
    # 4.5) 附件（PDF/Excel/Word）失败
    if re.search(r"pdf 下载失败|附件下载失败|不是有效 pdf|pdf 解析失败|无有效文本|扫描件|"
                 r"加密.*pdf|pdf.*密码|word 解析失败|excel 无数据|附件.*失效", txt, re.I):
        return "attachment_failed"
    # 5) 解析/选择器失败（0 条、未命中、不相关）
    if re.search(r"(?<![0-9])0\s*条|解析 0|未命中|选择器|不相关|试跑 0|条目 0|没有匹配|row_css|"
                 r"解析失败|假成功|列表 0", txt, re.I):
        return "selector_failed"
    # 6) 超时 / 卡住
    if re.search(r"超时|timeout|卡住|超过 .* 秒|timed out", txt, re.I):
        return "timeout"
    return "unknown"


# ---------------------------------------------------------------------------
# 解决方案模板库（给使用者的完整、可照做方案）
# ---------------------------------------------------------------------------
SOLUTIONS: Dict[str, Dict[str, Any]] = {
    "login_required": {
        "title": "🔐 需要登录才能抓取",
        "reason": "目标网站要求登录（登录墙 / 会话过期）。",
        "steps": [
            "双击工具目录里的「启动淘宝调试Chrome.command」，打开调试 Chrome",
            "在调试 Chrome 里手动登录目标网站（用小号，别用主账号）",
            "回到 WebUI，用「浏览器模式」重跑任务（会自动复用登录会话）",
            "如果是大众点评这类站：普通浏览器登录 → F12 → 网络 → 复制 Cookie → 粘贴到 WebUI 的 Cookie 框",
            "💡 替代数据源：若目标站登录极难（如需企业账号/实名），告诉我「要什么数据」，我帮你找不需要登录的公开替代源",
        ],
        "resources": "不需要额外付费；只需一个能登录的账号。",
    },
    "ip_blocked": {
        "title": "🚫 IP 被网站风控（403/412/429）",
        "reason": "当前出口 IP 被目标网站的 WAF（常见阿里云盾）标记/拉黑，代码无法绕过。",
        "steps": [
            "免费验证：手机开热点，重新跑一次——如果成功，就是 IP 问题",
            "免费换 IP：重启路由器/宽带重拨（换 IP）后再试",
            "付费稳定方案：买住宅代理（推荐 IPRoyal ~$2.5/GB、922 S5、Smartproxy）",
            "在 WebUI「代理」输入框粘贴代理，格式：http://用户名:密码@主机:端口",
            "还不行就把目标网址发我，我帮你判断是代理质量问题还是需要轮换代理",
            "💡 替代数据源：很多数据在官方 APP/镜像站/百度百科/其他同类公开站也能拿到——把「要什么数据」告诉我，我帮你找可自动抓取的替代源（不用死磕被风控的站）",
        ],
        "resources": "住宅代理服务商：iproyal.com / 922proxy.com / smartproxy.com（约 $1-3/GB）",
    },
    "captcha": {
        "title": "🧩 遇到验证码 / 滑块",
        "reason": "目标网站要求人工/机器识别验证码。",
        "steps": [
            "工具会弹出浏览器窗口并等你人工操作：去弹窗里滑滑块/点选/输验证码，完成自动继续（最长等 600 秒）",
            "验证码频繁时降低频率：WebUI 把并发调 1、间隔 2-5 秒",
            "想全自动：注册 2captcha.com 或 capsolver.com，把 API Key 给我，我接入自动打码",
        ],
        "resources": "2captcha / Capsolver（约 $1-3/千次）",
    },
    "captcha_or_login": {
        "title": "🧩 验证码或登录拦截（403 + 登录/验证字样）",
        "reason": "网站风控拦截，可能需要验证码、登录或换 IP。",
        "steps": [
            "先看弹出的浏览器窗口：有验证码就人工过，有登录框就登录",
            "若提示『请求被阻断』：按 IP 风控方案处理（手机热点测试 → 住宅代理）",
            "大众点评/淘宝类：Cookie 直抓或调试 Chrome 登录（见手册第三章）",
        ],
        "resources": "见《使用者解决方案手册》第三、四、五章",
    },
    "attachment_failed": {
        "title": "📎 附件（PDF/Excel/Word）解析失败",
        "reason": "目标页面/直链的附件下载或解析失败（链接失效、需要登录、扫描件、加密等）。",
        "steps": [
            "先看错误详情：下载失败 → 附件链接可能需登录或已失效，换官网栏目入口再试",
            "『无有效文本（可能是扫描件）』→ 扫描版 PDF 没有文字层，普通解析读不到：把 PDF 下载到本地，发我，我用视觉模型 OCR 成表格",
            "『不是有效 PDF』→ 附件实际是网页/图片伪装，检查链接是否带跳转或需要 Cookie",
            "加密/密码 PDF → 需要密码才能打开，先找网站是否提供明文版或公告正文",
            "Excel/Word 附件 → 工具已支持自动解析 xlsx/xls/docx；.doc 老格式或 .wps 请先另存为 .docx/.xlsx 再试",
        ],
        "resources": "扫描件 OCR 用千问视觉（已配置），不发费用；复杂附件可直接发我适配。",
    },
    "selector_failed": {
        "title": "🎯 解析失败（0 条 / 选择器不对）",
        "reason": "页面抓到了，但选择器没匹配到数据（常见：JS 渲染、AI 猜错结构）。",
        "steps": [
            "点「🤖 一键自动精配」：会自动探测、识别数据形态（列表/Vue点击/PDF/图片OCR）、生成并试跑",
            "一键精配会带真实页面样本自动修复选择器（最多 2 轮）",
            "仍失败：把网址发我，我实测后做成精配（每个都验证成功再交付）",
        ],
        "resources": "无需付费",
    },
    "entry_invalid": {
        "title": "🗺️ 入口失效（404 / 连接失败 / 站点改版）",
        "reason": "入口 URL 不存在或网站已改版/服务已停。",
        "steps": [
            "一键精配会自动探测候选入口（站点改版会自动找新地址）",
            "检查网址：登录官网找正确栏目入口；政府站常改版（如商标公告 2025-08 迁到 pub.sbj.cnipa.gov.cn）",
            "原入口是内网/临时 IP（如 106.37.208.243:8001）：确认服务是否还在，否则换公开数据源",
        ],
        "resources": "无需付费",
    },
    "timeout": {
        "title": "⏳ 任务超时 / 卡住",
        "reason": "单步耗时超过上限（常见：等待验证码、页面极慢、WAF 拖时间）。",
        "steps": [
            "检查是否有弹出浏览器窗口在等你人工操作（验证码/登录）",
            "降低并发、调大超时（WebUI 超时上限可设 240-1200 秒）",
            "页面慢：换代理或换网络；仍卡住就停止后重跑",
        ],
        "resources": "无需付费",
    },
    "no_content": {
        "title": "📄 页面没有可抓取的正文/表格/链接",
        "reason": "HTTP 请求成功，但网页里没有可提取内容（常见：需要 JS 渲染、该栏目确实暂无数据、或入口是错误页面）。",
        "steps": [
            "先勾选「浏览器渲染」，重跑一次（很多页面数据由 JavaScript 动态生成）",
            "确认入口是目标列表/内容页，不是首页导航或错误页",
            "查看网页实际内容：若是「暂无数据/查询无结果」，说明该条件确实没有数据，请按要求改时间/关键词",
            "仍不行就把页面 URL 和运行日志发我，我判断是动态接口、选择器还是入口问题",
        ],
        "resources": "无需付费",
    },
    "unknown": {
        "title": "❓ 无法自动判断原因",
        "reason": "错误类型未识别，需要人工看一下。",
        "steps": [
            "把上面红色错误信息完整复制发给我（Codex），我帮你定位",
            "同时告诉我：目标网址、是否登录过、用的什么网络",
            "常见兜底：先按《使用者解决方案手册》第七节自查表逐条排查",
        ],
        "resources": "无需付费",
    },
}


def get_solution(failure_type: str = "unknown", error_text: str = "",
                 messages: Optional[list] = None, url: str = "") -> Dict[str, Any]:
    """返回 {type, title, reason, steps[], resources, error}。"""
    ftype = failure_type or classify_failure(error_text, messages, url)
    sol = SOLUTIONS.get(ftype, SOLUTIONS["unknown"])
    out = dict(sol)
    out["type"] = ftype
    out["error"] = (error_text or "")[:500]
    return out


def attach_solution(job: Dict[str, Any], error_text: str = "") -> None:
    """给 job 附加 solution（_job_error 调用），登录/反爬类失败带"一键打开调试Chrome"动作。"""
    try:
        msgs = job.get("messages") or []
        sol = dict(get_solution("", error_text, msgs, job.get("description") or ""))
        # 一键动作：需要人工参与的场景 → 打开调试 Chrome 登录目标站（体验优化）
        _ft = classify_failure(error_text, msgs, job.get("task_dir") or "")
        if _ft in ("login_required", "captcha_or_login", "ip_blocked", "captcha"):
            _url = ""
            try:
                _td = job.get("task_dir") or ""
                if _td:
                    import json as _json
                    from pathlib import Path as _P
                    _cfg = _json.loads((_P(_td) / "config.json").read_text(encoding="utf-8"))
                    _url = (_cfg.get("start_urls") or [""])[0]
            except Exception:
                pass
            if not _url:
                # paste 任务无 task_dir：title 就是 URL
                import re as _re
                _m = _re.search("https?://[^\\s'\"]+", str(job.get("title") or ""))
                if _m:
                    _url = _m.group(0)
            sol["action"] = {"label": "🚀 打开调试 Chrome 并登录/过验证",
                             "api": "/api/chrome/start", "url": _url,
                             "tip": "点击后 Chrome 会打开目标网站，完成登录/滑块/过盾后回到这里重跑任务（工具会自动复用会话）"}
            # 第二步闭环：过完验证/登录后，一键把调试 Chrome 的会话导入工具存档，
            # 之后 HTTP/浏览器两条路线都会自动复用（"登录一次，以后全自动"）
            sol["action2"] = {"label": "🍪 登录/过验证后：一键导入会话",
                              "api": "/api/cookies/import",
                              "tip": "上一步完成后点这里导入登录会话；导入成功后回来点「↻ 重跑」即可"}
        job["solution"] = sol
    except Exception:
        pass
