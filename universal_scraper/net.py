#!/usr/bin/env python3
"""🌐 出口 IP / 网络链路体检（换 IP 后确认 + 本机干扰排查）。

detect_system_proxy / power_source 沉淀自《科研管理》战役（2026-09）：
- Clash 等系统代理会劫持所有"直连"请求（出口其实是代理节点），
  导致烧错配额、换 IP 无效、误诊本机配额——任何诊断第一步先查这个；
- 无人值守长跑必须 caffeinate，且电池模式下 -s/-i 均无效，先看电源。
"""
from __future__ import annotations
import json
import os
import re
import shutil
import subprocess
import urllib.request
from typing import Dict, Any
from urllib.parse import urlparse

# 出口 IP 查询服务白名单（ip-api 免费版仅 http）
_IP_ECHO_HOSTS = {"ip-api.com"}


def _guard_echo_url(url: str) -> str:
    u = urlparse(url)
    if u.scheme not in ("http", "https") or u.hostname not in _IP_ECHO_HOSTS:
        raise ValueError(f"IP 查询服务不在白名单: {url}")
    return url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """R129 修复（P1）：白名单服务的 302 一律不跟——ip-api 免费版是明文 HTTP
    （可被中间人/其前置劫持），重定向会把出口 IP 探测变成对任意内网地址的
    盲 SSRF。返回 None = 不处理 → urlopen 抛 HTTPError，被 detect_ip 捕获报错。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def detect_ip(timeout: int = 15, proxy: str = "") -> Dict[str, Any]:
    """返回 {ip, isp, city, region, org}；失败返回 {error}。
    审查六轮（M5）：proxy 非空时经该代理探测——真实出口与代理出口是两回事，
    "扫码前确认出口未被封"预检必须看采集实际要走的出口。"""
    try:
        req = urllib.request.Request(_guard_echo_url("http://ip-api.com/json/?lang=zh-CN"),
                                     headers={"User-Agent": "Mozilla/5.0"})
        _opener = urllib.request.build_opener(_NoRedirect())
        if proxy:
            _opener.add_handler(urllib.request.ProxyHandler(
                {"http": proxy, "https": proxy}))
        with _opener.open(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8", "ignore"))
        if d.get("status") == "success":
            return {
                "ip": d.get("query", ""),
                "isp": d.get("isp", ""),
                "city": d.get("city", ""),
                "region": d.get("regionName", ""),
                "org": d.get("as", ""),
            }
        return {"error": d.get("message", "查询失败")}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def detect_system_proxy() -> Dict[str, Any]:
    """检测系统代理与本机代理进程（darwin 优先 scutil，跨平台回退环境变量）。

    返回 {enabled, http_proxy, port, sources:[...], processes:[...], warning}
    """
    out: Dict[str, Any] = {"enabled": False, "http_proxy": "", "port": 0,
                           "sources": [], "processes": [], "warning": ""}
    # 1) 环境变量
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy"):
        if os.environ.get(k):
            out["enabled"] = True
            out["sources"].append(f"env:{k}")
    # 2) macOS 系统代理
    if shutil.which("scutil"):
        try:
            txt = subprocess.run(["scutil", "--proxy"], capture_output=True,
                                 text=True, timeout=5).stdout
            m = re.search(r"HTTPEnable\s*:\s*1", txt)
            # OCR R131（L）：\S+ 贪婪吞尾随标点/换行残片——收敛为合法主机字符
            p = re.search(r"HTTPProxy\s*:\s*([A-Za-z0-9._:-]+)", txt)
            port = re.search(r"HTTPPort\s*:\s*(\d+)", txt)
            if m:
                out["enabled"] = True
                gp = p.group(1) if p else ""
                # 边界复现：scutil 输出 "(null)" 曾被误当代理主机名
                out["http_proxy"] = "" if gp in ("(null)", "-", "") else gp
                out["port"] = int(port.group(1)) if port else 0
                out["sources"].append("scutil(macOS系统代理)")
        except Exception as e:
            # 审查修复：诊断器自身失败绝不能伪装成"没开代理"的确定答案
            out["sources"].append(f"scutil检测失败({type(e).__name__})，结果不可信")
    # 3) 常见本机代理进程（Clash/V2Ray/sing-box 等）
    if shutil.which("ps"):
        try:
            ps = subprocess.run(["ps", "-Axo", "comm="], capture_output=True, text=True, timeout=5).stdout
            # 审查八轮（LOW）：曾扫 `ps aux` 全命令行——`grep clash`、`vim clash.md`、
            # `python3 crawl.py --proxy v2ray` 这类"提到名字"的进程全都误报（实测六个
            # 名字全中）。改扫 **可执行文件名**（comm 列取 basename）：只有真的是该
            # 程序在跑才命中（ClashX/clash-verge 这类带后缀的仍能匹配）。
            _names = "\n".join(os.path.basename(_l.strip()) for _l in ps.splitlines() if _l.strip())
            for name in ("clash", "mihomo", "verge", "v2ray", "sing-box", "surge"):
                # OCR R131（M）：子串匹配曾误报（如 "converge"/"purge" 命中 verge/
                # surge）——词边界锚定。尾部放行大写/数字续接（ClashX 是客户端名），
                # 仅拒绝小写字母续接（verges/surges 复数形）。审查二轮（M）：(?-i:)
                # 内联关大小写——re.I 会让 [A-Z0-9] 连小写也放行，防复数形失效
                if re.search(rf"\b{re.escape(name)}(?:\b|(?-i:[A-Z0-9]))", _names, re.I):
                    out["processes"].append(name)
        except Exception as e:
            out["sources"].append(f"进程检测失败({type(e).__name__})，结果不可信")
    if out["enabled"]:
        out["warning"] = ("检测到系统代理已启用（" + ", ".join(out["sources"]) +
                          "）：'直连'请求可能被劫持到代理节点出口。"
                          "诊断配额/IP 问题前先确认真实出口（detect_ip），必要时关闭系统代理或用 no_proxy 锁定直连。")
    elif out["processes"]:
        # 商标网战训（2026-09）：Clash 等进程 merely 在跑 ≠ 接管请求——
        # env/scutil 均未启用时曾报"系统代理开启"误导排查方向。降级为提示。
        out["warning"] = (f"本机有代理进程在运行（{', '.join(out['processes'])}）但 "
                          "env/scutil 均未启用系统代理——当前请求大概率未被劫持；"
                          "若诊断异常可再查真实出口（detect_ip）。")
    return out


def power_source() -> Dict[str, Any]:
    """电源模式检测（darwin）。电池模式下 caffeinate 防睡眠不可靠。"""
    out: Dict[str, Any] = {"source": "unknown", "caffeinate_hint": ""}
    if shutil.which("pmset"):
        try:
            txt = subprocess.run(["pmset", "-g", "batt"], capture_output=True,
                                 text=True, timeout=5).stdout
            if "AC Power" in txt:
                out["source"] = "AC"
                out["caffeinate_hint"] = "接电状态：caffeinate -s 可防系统级睡眠"
            elif "Battery" in txt or "BATT" in txt:
                out["source"] = "BATT"
                out["caffeinate_hint"] = ("电池模式：合盖即睡且 caffeinate 无效，"
                                          "无人值守任务请接电源")
        except Exception as e:
            out["source"] = f"unknown（pmset 失败: {type(e).__name__}）"
    return out


if __name__ == "__main__":
    print(json.dumps({"ip": detect_ip(), "proxy": detect_system_proxy(),
                      "power": power_source()}, ensure_ascii=False, indent=2))
