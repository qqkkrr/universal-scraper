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


def detect_ip(timeout: int = 15) -> Dict[str, Any]:
    """返回 {ip, isp, city, region, org}；失败返回 {error}。"""
    try:
        req = urllib.request.Request(_guard_echo_url("http://ip-api.com/json/?lang=zh-CN"),
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
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
            p = re.search(r"HTTPProxy\s*:\s*(\S+)", txt)
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
            ps = subprocess.run(["ps", "aux"], capture_output=True, text=True, timeout=5).stdout
            for name in ("clash", "mihomo", "verge", "v2ray", "sing-box", "surge"):
                if re.search(name, ps, re.I):
                    out["processes"].append(name)
        except Exception as e:
            out["sources"].append(f"进程检测失败({type(e).__name__})，结果不可信")
    if out["enabled"] or out["processes"]:
        out["warning"] = ("检测到系统代理/本机代理进程：'直连'请求可能被劫持到代理节点出口。"
                          "诊断配额/IP 问题前先确认真实出口（detect_ip），必要时关闭系统代理或用 no_proxy 锁定直连。")
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
