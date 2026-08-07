#!/usr/bin/env python3
"""🌐 出口 IP 检测（换 IP 后确认用）。"""
from __future__ import annotations
import json
import urllib.request
from typing import Dict, Any


def detect_ip(timeout: int = 15) -> Dict[str, Any]:
    """返回 {ip, isp, city, region, org}；失败返回 {error}。"""
    try:
        req = urllib.request.Request("http://ip-api.com/json/?lang=zh-CN",
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


if __name__ == "__main__":
    print(json.dumps(detect_ip(), ensure_ascii=False, indent=2))
