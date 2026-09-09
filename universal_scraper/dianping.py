#!/usr/bin/env python3
"""🌶️ 大众点评专用抓取器（Cookie 直抓 SSR，绕开 csec 页面 JS）。

原理（来自 dianping_spider 验证 + 本机实测）：
  大众点评搜索页是 SSR 渲染——用「真实登录 Cookie + 住宅/移动 IP」直接 GET 搜索页 HTML，
  即可拿到 .shop-list 完整列表（店名/人均/点评数/区域/shopId），无需过 csec/验证码。

用法:
  python3 -m universal_scraper.cli dianping --cookie "<完整Cookie>" --keyword 美食 --city 2 --limit 10

说明:
  - Cookie：在你已登录大众点评的浏览器按 F12 → Network → 复制请求头 Cookie 整串
  - IP：家庭宽带/手机热点（被风控的 IP 会 302 到验证中心，换网络即可）

batch2400 美团战训补充（GLM 实测踩坑记录）:
  - **星级解析**：HTML 中 star_class 形如 "star_40"=4.0 星、"star_45"=4.5 星、
    "star_0"=无评分（新店/无 enough 评价）。解析正则：/star_(\\d+)/ → int/10。
  - **人均/月售的字段分布**：人均（¥XX/人）只在关键词搜索卡片和详情页有；
    月售是 App 专有字段（网页版不显示）——需要月售请走 App 抓包或 CDP。
  - **商圈筛选**：URL 格式 /ch10/g{品类id}r{商圈id}，品类和商圈 ID 需先从
    分类页 HTML 提取（ch10=美食，g{X}=品类，r{Y}=商圈）。商圈代码发现流程：
    打开目标城市首页 → 定位到商圈筛选栏 → 提取 r{数字} 链接。
  - **分类浏览模式**：URL /ch10/g113r802（火锅+某商圈）比关键词搜索更适合
    "抓取某商圈全部奶茶店"类任务——结果更全、不会被关键词过滤掉同名店。
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/18.6 Safari/605.1.15")


def fetch_search_page(keyword: str, city: int = 2, cookie: str = "",
                      proxy: Optional[str] = None, timeout: int = 20) -> Dict[str, Any]:
    """GET 搜索页 SSR HTML。返回 {ok, status, final_url, html}。"""
    kw = urllib.parse.quote(keyword)
    url = f"https://www.dianping.com/search/keyword/{city}/0_{kw}"
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-CN,zh-Hans;q=0.9",
        "Referer": "https://www.dianping.com/",
        "Accept-Encoding": "identity",
    }
    if cookie:
        headers["Cookie"] = cookie
    opener = urllib.request.build_opener()
    if proxy:
        opener.add_handler(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    req = urllib.request.Request(url, headers=headers)
    try:
        from .core import smart_decode
        r = opener.open(req, timeout=timeout)
        html = smart_decode(r.read(500000), {k.lower(): v for k, v in r.headers.items()})
        final = r.geturl()
        return {"ok": True, "status": r.status, "final_url": final, "html": html}
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code, "final_url": url, "html": "", "error": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "status": 0, "final_url": url, "html": "", "error": f"{type(e).__name__}: {e}"}


def parse_search_html(html: str, limit: int = 10) -> List[Dict[str, Any]]:
    """解析 .shop-list li → 前 limit 家商家。"""
    if not html or not html.strip():
        return []
    from lxml import html as lhtml
    doc = lhtml.fromstring(html)
    lis = doc.cssselect(".shop-list li")
    out = []
    for li in lis[:limit]:
        try:
            shop_id = li.cssselect("[data-shopid]")[0].get("data-shopid") or ""
        except Exception:
            shop_id = ""
        try:
            name = li.cssselect(".tit h4")[0].text_content().strip()
        except Exception:
            name = ""
        try:
            review = re.sub(r"\D", "", li.cssselect(".review-num b")[0].text_content() or "")
        except Exception:
            review = ""
        try:
            price = re.sub(r"\D", "", li.cssselect(".mean-price b")[0].text_content() or "")
        except Exception:
            price = ""
        try:
            tags = li.cssselect(".tag-addr .tag")
            region = " / ".join(t.text_content().strip() for t in tags[:2])
        except Exception:
            region = ""
        if not name and not shop_id:
            continue
        out.append({
            "shopId": shop_id,
            "shopName": name,
            "avgPrice": price,
            "reviewCount": review,
            "address": region,
            "url": f"https://www.dianping.com/shop/{shop_id}" if shop_id else "",
        })
    return out


def run(keyword: str, city: int = 2, cookie: str = "", limit: int = 10,
        proxy: Optional[str] = None, out_name: Optional[str] = None) -> Dict[str, Any]:
    """完整流程：抓搜索页 → 解析 → 导出。返回 {total, rows, files}。"""
    res = fetch_search_page(keyword, city, cookie, proxy)
    if not res.get("ok"):
        hint = ""
        if res.get("status") == 200 and "verify.meituan.com" in res.get("final_url", ""):
            hint = "（被重定向到美团验证中心：Cookie 失效或当前 IP 被风控，请换网络/重新复制 Cookie）"
        elif res.get("status") in (403, 302):
            hint = "（HTTP 403/302：Cookie 失效或 IP 被风控，请换网络/重新复制 Cookie）"
        return {"total": 0, "rows": [], "files": {}, "error": f"抓取失败 {res.get('status')}{hint}"}
    rows = parse_search_html(res.get("html", ""), limit)
    if not rows:
        if "shop-list" not in res.get("html", ""):
            return {"total": 0, "rows": [], "files": {},
                    "error": "页面未包含商家列表（可能被重定向到验证/登录页），请检查 Cookie 与网络"}
        return {"total": 0, "rows": [], "files": {}, "error": "解析到 0 家（页面结构可能变化）"}
    name = out_name or f"dianping_{keyword}_{city}"
    out_dir = Path("outputs")
    out_dir.mkdir(exist_ok=True)
    fp = out_dir / f"{name}.json"
    fp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.append(list(rows[0].keys()))
        for r in rows:
            ws.append([r[k] for k in rows[0]])
        wb.save(out_dir / f"{name}.xlsx")
    except Exception:
        pass
    try:
        import csv
        with open(out_dir / f"{name}.csv", "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    except Exception:
        pass
    files = {"json": f"outputs/{name}.json", "csv": f"outputs/{name}.csv", "xlsx": f"outputs/{name}.xlsx"}
    return {"total": len(rows), "rows": rows, "files": files, "error": ""}


if __name__ == "__main__":
    import sys
    sys.exit(0)
