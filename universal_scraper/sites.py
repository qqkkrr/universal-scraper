#!/usr/bin/env python3
"""🏆 高频网站精配解析器注册表（v1）。

架构：每个网站一个「精配解析器」（match + parse），auto/WebUI 检测到命中 URL 就自动用它
替代 AI 猜测的选择器，字段干净、速度快。未命中的网站走通用 AI 流程，不受影响。

如何新增网站：复制 dianping 的写法，实现 match(url) + parse(html_or_json, ...)，
然后注册到 SITES 字典即可。
"""
from __future__ import annotations

import json
import re
import urllib.parse
from typing import Any, Callable, Dict, List, Optional

# ---------------------------------------------------------------------------
# 高频网站表（中文互联网高频数据源）
# ---------------------------------------------------------------------------
HIGH_FREQUENCY_SITES = [
    {"name": "大众点评", "domain": "dianping.com", "module": "dianping", "status": "✅ 已精配",
     "desc": "美食/商家列表（Cookie 直抓 SSR）", "difficulty": "高反爬·需登录 Cookie"},
    {"name": "京东", "domain": "jd.com", "module": "jd", "status": "🔄 待精配",
     "desc": "商品搜索/价格", "difficulty": "中·SSR 可抓"},
    {"name": "豆瓣", "domain": "douban.com", "module": "douban", "status": "🔄 待精配",
     "desc": "电影/图书/小组", "difficulty": "中·有反爬但 SSR"},
    {"name": "B站", "domain": "bilibili.com", "module": "bilibili", "status": "🔄 待精配",
     "desc": "视频搜索/信息", "difficulty": "中·有风控"},
    {"name": "知乎", "domain": "zhihu.com", "module": "zhihu", "status": "🔄 待精配",
     "desc": "问题/回答/搜索", "difficulty": "中高·需 Cookie"},
    {"name": "百度百科", "domain": "baike.baidu.com", "module": "baike", "status": "🔄 待精配",
     "desc": "词条标题/摘要", "difficulty": "低·公开"},
    {"name": "微博", "domain": "weibo.com", "module": "weibo", "status": "🔄 待精配",
     "desc": "热搜/用户微博", "difficulty": "高·需登录 Cookie"},
    {"name": "GitHub", "domain": "github.com", "module": "github", "status": "🔄 待精配",
     "desc": "仓库/代码/Issue（REST API）", "difficulty": "低·API 公开"},
    {"name": "天气", "domain": "wttr.in", "module": "weather", "status": "🔄 待精配",
     "desc": "全球天气（公开 API）", "difficulty": "低·公开 API"},
    {"name": "澎湃新闻", "domain": "thepaper.cn", "module": "thepaper", "status": "🔄 待精配",
     "desc": "新闻列表/正文", "difficulty": "低·公开 SSR"},
    {"name": "网易新闻", "domain": "news.163.com", "module": "netease_news", "status": "🔄 待精配",
     "desc": "新闻列表/正文", "difficulty": "低·公开 SSR"},
    {"name": "百度搜索", "domain": "baidu.com", "module": "baidu_search", "status": "🔄 待精配",
     "desc": "搜索结果（标题/链接/摘要）", "difficulty": "中·有反爬"},
]

# ---------------------------------------------------------------------------
# 注册表：URL 匹配 → 解析器
# ---------------------------------------------------------------------------
# 每个条目: {name, match: fn(url)->bool, parse: fn(html, url)->list[dict] 或 dict,
#           fetch: fn(url, cookie, proxy)->(html, final_url) 可选覆盖默认 HTTP}
SITES: Dict[str, Dict[str, Any]] = {}


def register(name: str, matcher: Callable[[str], bool], parser: Callable,
             fetch: Optional[Callable] = None, desc: str = ""):
    SITES[name] = {"name": name, "match": matcher, "parse": parser,
                   "fetch": fetch, "desc": desc}


def _dianping_fetch(url, cookie="", proxy=None):
    from .dianping import fetch_search_page
    return fetch_search_page("美食", 2, cookie=cookie, proxy=proxy)


def _dianping_parse(html, url):
    from .dianping import parse_search_html
    return parse_search_html(html, limit=50)


def match_dianping(url: str) -> bool:
    return "dianping.com/search" in url


register("dianping", match_dianping, _dianping_parse, fetch=_dianping_fetch,
         desc="大众点评：搜索页 Cookie 直抓 SSR")


def match_site(url: str) -> Optional[str]:
    """返回命中注册表的站点名，未命中返回 None。"""
    for name, s in SITES.items():
        try:
            if s["match"](url):
                return name
        except Exception:
            continue
    return None


def list_sites() -> List[Dict[str, str]]:
    out = []
    # module 字段 → SITES 注册键
    mapping = {"baidu_search": "baidu", "dianping": "dianping",
               "douban": "douban", "bilibili": "bilibili", "github": "github",
               "weather": "weather"}
    for s in HIGH_FREQUENCY_SITES:
        key = mapping.get(s["module"], s["module"])
        done = key in SITES
        out.append({**s, "status": "✅ 已精配" if done else s["status"]})
    return out


# ---------------------------------------------------------------------------
# 通用 HTTP 抓取（带 Cookie/代理），解析器复用
# ---------------------------------------------------------------------------
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/18.6 Safari/605.1.15")


def fetch_html(url: str, cookie: str = "", proxy: Optional[str] = None,
               timeout: int = 20) -> Dict[str, Any]:
    """GET URL 返回 HTML/JSON。成功 {ok, status, html, final_url}。"""
    import urllib.request
    headers = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,application/json,*/*",
               "Accept-Language": "zh-CN,zh-Hans;q=0.9", "Accept-Encoding": "identity"}
    if cookie:
        headers["Cookie"] = cookie
    opener = urllib.request.build_opener()
    if proxy:
        opener.add_handler(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    req = urllib.request.Request(url, headers=headers)
    try:
        r = opener.open(req, timeout=timeout)
        raw = r.read(1000000).decode("utf-8", "ignore")
        return {"ok": True, "status": r.status, "html": raw, "final_url": r.geturl()}
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code, "html": "", "final_url": url, "error": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "status": 0, "html": "", "final_url": url, "error": f"{type(e).__name__}: {e}"}


def run_site(url: str, cookie: str = "", proxy: Optional[str] = None,
            limit: int = 20, out_name: Optional[str] = None) -> Dict[str, Any]:
    """通用精配执行：命中注册表 → 抓取 → 解析 → 导出。返回 {total, rows, files, error}。"""
    site = match_site(url)
    if not site:
        return {"total": 0, "rows": [], "files": {}, "error": f"未命中精配站点: {url}"}
    s = SITES[site]
    fetch = s.get("fetch") or fetch_html
    try:
        res = fetch(url, cookie=cookie, proxy=proxy)
    except TypeError:
        res = fetch_html(url, cookie=cookie, proxy=proxy)
    if not res.get("ok"):
        return {"total": 0, "rows": [], "files": {},
                "error": f"抓取失败 HTTP {res.get('status')}: {res.get('error','')}"}
    rows = s["parse"](res.get("html", ""), url)[:limit]
    if not rows:
        return {"total": 0, "rows": [], "files": {}, "error": "解析 0 条（页面结构变化或需登录/Cookie）"}
    from pathlib import Path as _P
    from urllib.parse import urlparse as _up
    host = _up(url).netloc.replace(".", "_")
    name = out_name or f"site_{site}_{host}"
    out_dir = _P("outputs"); out_dir.mkdir(exist_ok=True)
    fp = out_dir / f"{name}.json"
    fp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        import csv
        with open(out_dir / f"{name}.csv", "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=[k for k in rows[0] if k != "_site"])
            w.writeheader()
            w.writerows([{k: v for k, v in r.items() if k != "_site"} for r in rows])
    except Exception:
        pass
    try:
        from openpyxl import Workbook
        wb = Workbook(); ws = wb.active
        keys = [k for k in rows[0] if k != "_site"]
        ws.append(keys)
        for r in rows:
            ws.append([r.get(k, "") for k in keys])
        wb.save(out_dir / f"{name}.xlsx")
    except Exception:
        pass
    files = {"json": f"outputs/{name}.json", "csv": f"outputs/{name}.csv", "xlsx": f"outputs/{name}.xlsx"}
    return {"total": len(rows), "rows": rows, "files": files, "error": "", "site": site}


if __name__ == "__main__":
    print(json.dumps(list_sites(), ensure_ascii=False, indent=1))


# ===========================================================================
# 精配解析器实现
# ===========================================================================

def _css_text(el, sel, idx=0):
    try:
        els = el.cssselect(sel)
        return els[idx].text_content().strip() if len(els) > idx else ""
    except Exception:
        return ""


def _css_attr(el, sel, attr, idx=0):
    try:
        els = el.cssselect(sel)
        return (els[idx].get(attr) or "").strip() if len(els) > idx else ""
    except Exception:
        return ""


# ---------- 豆瓣（电影榜单 / 搜索） ----------
def parse_douban(html: str, url: str) -> List[Dict[str, Any]]:
    from lxml import html as lh
    doc = lh.fromstring(html)
    out = []
    # 榜单 tr.item
    for tr in doc.cssselect("tr.item"):
        name = re.sub(r"\s+", " ", _css_text(tr, ".pl2 a")).strip()
        if not name:
            continue
        link = _css_attr(tr, ".pl2 a", "href")
        rating = _css_text(tr, ".rating_nums")
        intro = _css_text(tr, ".pl") or ""
        out.append({"title": name, "url": link, "rating": rating,
                    "info": intro[:200], "_site": "douban"})
    # 搜索页 result
    for div in doc.cssselect(".result"):
        name = _css_text(div, "h2 a") or _css_text(div, ".title a")
        if not name:
            continue
        out.append({"title": name, "url": _css_attr(div, "h2 a", "href") or _css_attr(div, ".title a", "href"),
                    "rating": _css_text(div, ".rating_nums"), "info": _css_text(div, ".abstract"),
                    "_site": "douban"})
    return out


def match_douban(url: str) -> bool:
    return "douban.com" in url and any(k in url for k in ("movie", "book", "group", "/subject/", "search"))


register("douban", match_douban, parse_douban, desc="豆瓣：电影/图书榜单与搜索")


# ---------- GitHub（REST API JSON） ----------
def parse_github(html: str, url: str) -> List[Dict[str, Any]]:
    try:
        data = json.loads(html)
    except Exception:
        return []
    items = data.get("items") or data.get("results") or []
    out = []
    for it in items[:30]:
        out.append({
            "repo": it.get("full_name") or it.get("name", ""),
            "url": it.get("html_url") or "",
            "description": (it.get("description") or "")[:200],
            "stars": it.get("stargazers_count") or it.get("watchers", 0),
            "language": it.get("language") or "",
            "_site": "github",
        })
    return out


def match_github(url: str) -> bool:
    return "github.com" in url and "/search" in url


register("github", match_github, parse_github, desc="GitHub：仓库搜索（公开 API）")


# ---------- 天气（wttr.in JSON） ----------
def parse_weather(html: str, url: str) -> List[Dict[str, Any]]:
    try:
        data = json.loads(html)
    except Exception:
        return []
    cc = (data.get("current_condition") or [{}])[0]
    area = (data.get("nearest_area") or [{}])[0]
    out = [{
        "location": (area.get("areaName") or [{}])[0].get("value", "") if area.get("areaName") else "",
        "country": (area.get("country") or [{}])[0].get("value", "") if area.get("country") else "",
        "temp_c": cc.get("temp_C", ""),
        "feels_c": cc.get("FeelsLikeC", ""),
        "humidity": cc.get("humidity", ""),
        "weather": ((cc.get("weatherDesc") or [{}])[0].get("value", "") if cc.get("weatherDesc") else ""),
        "wind_kmh": cc.get("windspeedKmph", ""),
        "_site": "weather",
    }]
    return out


def match_weather(url: str) -> bool:
    return "wttr.in" in url


register("weather", match_weather, parse_weather, desc="天气：wttr.in 公开 JSON")


# ---------- 百度搜索 ----------
def parse_baidu(html: str, url: str) -> List[Dict[str, Any]]:
    from lxml import html as lh
    doc = lh.fromstring(html)
    out = []
    for div in doc.cssselect(".result, div[class*='c-container']"):
        title_el = div.cssselect("h3 a") or div.cssselect(".c-title a") or div.cssselect("h3")
        if not title_el:
            continue
        title = title_el[0].text_content().strip()
        if not title:
            continue
        link = title_el[0].get("href", "") if len(title_el[0].items()) else ""
        abstract = div.cssselect(".c-abstract") or div.cssselect(".c-span-last") or div.cssselect(".content-right_8Zs40")
        out.append({
            "title": title[:120],
            "url": link,
            "abstract": (abstract[0].text_content().strip()[:200] if abstract else ""),
            "_site": "baidu",
        })
        if len(out) >= 20:
            break
    return out


def match_baidu(url: str) -> bool:
    return "baidu.com/s" in url or "baidu.com/s?" in url


register("baidu", match_baidu, parse_baidu, desc="百度搜索：结果标题/链接/摘要")


# ---------- B站搜索（SSR 卡片） ----------
def parse_bilibili(html: str, url: str) -> List[Dict[str, Any]]:
    from lxml import html as lh
    doc = lh.fromstring(html)
    out = []
    for card in doc.cssselect(".bili-video-card")[:30]:
        title = _css_text(card, ".bili-video-card__info--tit, .bili-video-card__title, h3 a, .title")
        link = _css_attr(card, "a", "href")
        if not title and not link:
            continue
        up = _css_text(card, ".bili-video-card__info--author, .up-name, .name")
        play = _css_text(card, ".bili-video-card__info--play, .play, .bili-video-card__stats--item")
        out.append({"title": title or "", "url": "https:" + link if link.startswith("//") else link,
                    "up": up, "stats": play[:80], "_site": "bilibili"})
    return out


def match_bilibili(url: str) -> bool:
    return "search.bilibili.com" in url or "bilibili.com/video" in url or "/search?" in url and "bilibili" in url


register("bilibili", match_bilibili, parse_bilibili, desc="B站：视频搜索（SSR）")
