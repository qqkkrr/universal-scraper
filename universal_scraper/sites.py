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
    {"name": "京东", "domain": "jd.com", "module": "jd", "status": "🔧 浏览器模式·需登录调优",
     "desc": "商品/店铺/评论（浏览器+扫码登录，须有 pt_key/pt_pin）",
     "difficulty": "高·强风控+需登录",
     "url_tips": "店铺页: https://mall.jd.com/index-<店铺ID>.html；商品页: https://item.jd.com/<skuID>.html；不要用 <店铺名>sp.jd.com"},
    {"name": "豆瓣", "domain": "douban.com", "module": "douban", "status": "🔄 待精配",
     "desc": "电影/图书/小组", "difficulty": "中·有反爬但 SSR"},
    {"name": "B站", "domain": "bilibili.com", "module": "bilibili", "status": "🔄 待精配",
     "desc": "视频搜索/信息", "difficulty": "中·有风控"},
    {"name": "知乎", "domain": "zhihu.com", "module": "zhihu", "status": "🔧 浏览器模式·需登录调优",
     "desc": "问题/回答/搜索", "difficulty": "中高·需登录"},
    {"name": "微博", "domain": "weibo.com", "module": "weibo", "status": "🔧 浏览器模式·需登录调优",
     "desc": "热搜/用户微博", "difficulty": "高·需登录"},
    {"name": "GitHub", "domain": "api.github.com", "module": "github", "status": "✅ 已精配（HTTP/API）",
     "desc": "仓库搜索 API（api.github.com/search）", "difficulty": "低·API 公开"},
    {"name": "天气", "domain": "wttr.in", "module": "weather", "status": "🔄 待精配",
     "desc": "全球天气（公开 API）", "difficulty": "低·公开 API"},
    {"name": "百度百科", "domain": "baike.baidu.com", "module": "baike", "status": "✅ 已精配",
     "desc": "词条卡片（公开 API）", "difficulty": "低·openapi 可用"},
    {"name": "澎湃新闻", "domain": "thepaper.cn", "module": "thepaper", "status": "✅ 已精配",
     "desc": "新闻列表（公开 API）", "difficulty": "低·API 公开"},
    {"name": "网易新闻", "domain": "news.163.com", "module": "netease_news", "status": "✅ 已精配",
     "desc": "头条列表（JSONP）", "difficulty": "低·API 公开"},
    {"name": "百度搜索", "domain": "baidu.com", "module": "baidu_search", "status": "✅ 已精配",
     "desc": "搜索结果（标题/链接/摘要）", "difficulty": "中·有反爬"},
    {"name": "抖音", "domain": "douyin.com", "module": "douyin", "status": "🔧 浏览器模式·需登录调优",
     "desc": "视频列表", "difficulty": "高·需登录"},
    {"name": "快手", "domain": "kuaishou.com", "module": "kuaishou", "status": "🔧 浏览器模式·需登录调优",
     "desc": "视频列表", "difficulty": "高·需登录"},
    {"name": "沈阳体育学院学报", "domain": "stxb.magtech.com.cn", "module": "sytyxb", "status": "✅ 已精配",
     "desc": "期刊全文/PDF（magtech 系统，2024 起免费）", "difficulty": "低·公开全文",
     "url_tips": "期次: /CN/Y<年>/V<卷>/I<期>；文章: /CN/<DOI>；PDF 由 showArticleFile.do 换取直链"},
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
        reg = SITES.get(key)
        if reg:
            if "浏览器渲染" in reg.get("desc", ""):
                out.append({**s, "status": "🔧 浏览器模式·需登录调优"})
            else:
                out.append({**s, "status": "✅ 已精配（HTTP/API）"})
        else:
            out.append({**s, "status": s["status"]})
    return out


# ---------------------------------------------------------------------------
# 通用 HTTP 抓取（带 Cookie/代理），解析器复用
# ---------------------------------------------------------------------------
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/18.6 Safari/605.1.15")


def fetch_html(url: str, cookie: str = "", proxy: Optional[str] = None,
               timeout: int = 20, allow_html_404: bool = True) -> Dict[str, Any]:
    """GET URL 返回 HTML/JSON（curl_cffi TLS 指纹伪装优先，回退 urllib）。
    成功 {ok, status, html, final_url, headers}。"""
    from .core import smart_decode
    headers = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,application/json,*/*",
               "Accept-Language": "zh-CN,zh-Hans;q=0.9", "Accept-Encoding": "gzip, deflate"}
    if cookie:
        headers["Cookie"] = cookie
    # 1) curl_cffi：伪装 TLS/JA3/HTTP2 指纹（反 403）。
    #    注意：curl_cffi 没有 "auto" 目标（会抛 ImpersonateError），这里 Python 侧随机选合法目标，
    #    且不传 UA（让 curl_cffi 按目标浏览器生成配套 UA，避免 "Safari UA + Chrome TLS" 错配）。
    try:
        import random as _random
        import curl_cffi.requests as cffi
        _TARGETS = ["chrome", "chrome131", "chrome124", "chrome123", "edge101", "safari17_0", "firefox133"]
        hdrs2 = dict(headers)
        hdrs2.pop("User-Agent", None)
        kw = {"headers": hdrs2, "timeout": timeout, "impersonate": _random.choice(_TARGETS)}
        if proxy:
            kw["proxies"] = {"http": proxy, "https": proxy}
        resp = cffi.get(url, **kw)
        raw = resp.content or b""
        import gzip
        enc = (resp.headers.get("Content-Encoding") or "").lower()
        if enc == "gzip":
            try:
                raw = gzip.decompress(raw)
            except Exception:
                pass
        text = smart_decode(raw, dict(resp.headers))
        ok = resp.status_code < 400 or (allow_html_404 and resp.status_code == 404 and len(text) > 200)
        return {"ok": ok, "status": resp.status_code, "html": text,
                "final_url": str(resp.url), "headers": {k.lower(): v for k, v in resp.headers.items()}}
    except Exception:
        pass
    # 2) urllib 回退
    import urllib.request
    import gzip
    hdrs = dict(headers)
    hdrs.pop("Accept-Encoding", None)
    hdrs["Accept-Encoding"] = "identity"
    opener = urllib.request.build_opener()
    if proxy:
        opener.add_handler(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    req = urllib.request.Request(url, headers=hdrs)
    try:
        with opener.open(req, timeout=timeout) as r:
            raw = r.read(1000000)
            enc = r.headers.get("Content-Encoding", "").lower()
            if enc == "gzip":
                try:
                    raw = gzip.decompress(raw)
                except Exception:
                    pass
            text = smart_decode(raw, {k.lower(): v for k, v in r.headers.items()})
            return {"ok": True, "status": getattr(r, "status", 200), "html": text,
                    "final_url": r.geturl(), "headers": {k.lower(): v for k, v in r.headers.items()}}
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
    # 空壳行防线：全字段为空的"成功"行按失败处理（防精配假成功）
    rows = [r for r in rows if any(str(v or "").strip() for k, v in r.items() if k != "_site")]
    if not rows:
        return {"total": 0, "rows": [], "files": {}, "error": "解析 0 条（页面结构变化、接口失效或需登录/Cookie）"}
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
    # 精配只服务 REST API（api.github.com/search/*）；HTML 搜索页是 JS 渲染，交给通用/AI 流程
    return "api.github.com" in url and "/search" in url


register("github", match_github, parse_github, desc="GitHub：仓库搜索 API（api.github.com/search，公开）")


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


# ---------- 百度百科（openapi JSON） ----------
def fetch_baike(url, cookie="", proxy=None):
    import urllib.parse as _up
    m = re.search(r"/item/([^/?#]+)", url)
    key = _up.unquote(m.group(1)) if m else ""
    api = ("https://baike.baidu.com/api/openapi/BaikeLemmaCardApi?"
           "scope=103&format=json&appid=379020&bk_key=" + urllib.parse.quote(key))
    r = fetch_html(api, cookie=cookie, proxy=proxy)
    return r


def parse_baike(html, url):
    try:
        d = json.loads(html)
    except Exception:
        return []
    if d.get("errno") is not None and d.get("errno") != 0:
        return []  # 接口失效/风控（如 {"errno":2}），不再返回空壳行
    if not (d.get("title") or d.get("desc") or d.get("lemmaUrl")):
        return []
    card = []
    for c in d.get("card", [])[:8]:
        v = c.get("value") or []
        if isinstance(v, list):
            v = "；".join(str(x) for x in v)
        card.append(f"{c.get('name','')}: {v}")
    return [{"title": d.get("title", ""), "desc": d.get("desc", ""),
             "card": " | ".join(card)[:400], "url": d.get("lemmaUrl", ""),
             "_site": "baike"}]


def match_baike(url):
    return "baike.baidu.com/item" in url


register("baike", match_baike, parse_baike, fetch=fetch_baike, desc="百度百科词条卡片")


# ---------- 网易新闻（头条 JSONP） ----------
def fetch_netease(url, cookie="", proxy=None):
    u = ("https://temp.163.com/special/00804KVA/cm_yaowen20200213.js?callback=data_callback")
    import urllib.request as _ur
    headers = {"User-Agent": UA, "Referer": "https://news.163.com/", "Accept-Encoding": "identity"}
    if cookie:
        headers["Cookie"] = cookie
    try:
        r = _ur.urlopen(_ur.Request(u, headers=headers), timeout=20)
        raw = r.read().decode(r.headers.get_content_charset() or "gbk", "ignore")
        return {"ok": True, "status": r.status, "html": raw, "final_url": u}
    except Exception as e:
        return {"ok": False, "status": 0, "html": "", "final_url": u, "error": str(e)}


def parse_netease(html, url):
    m = re.search(r"\((\[.*\])\)", html, re.S) or re.search(r"data_callback\((.*)\)\s*$", html, re.S)
    try:
        data = json.loads(m.group(1)) if m else []
    except Exception:
        return []
    out = []
    for it in (data or [])[:30]:
        t = it.get("title") or ""
        if t and "\\u" in t:
            try:
                t = t.encode("latin1").decode("unicode_escape")
            except Exception:
                pass
        out.append({"title": re.sub(r"<[^>]+>", "", t).strip()[:120],
                    "url": it.get("docurl", ""), "digest": (it.get("digest") or "")[:120],
                    "_site": "netease"})
    return out


def match_netease(url):
    return "163.com" in url


register("netease", match_netease, parse_netease, fetch=fetch_netease, desc="网易新闻头条")


# ---------- 澎湃新闻（API JSON） ----------
def fetch_thepaper(url, cookie="", proxy=None):
    u = "https://api.thepaper.cn/contentapi/wwwIndex/rightSidebar"
    return fetch_html(u, cookie=cookie, proxy=proxy)


def parse_thepaper(html, url):
    try:
        d = json.loads(html)
    except Exception:
        return []
    data = d.get("data", {})
    out = []
    seen = set()
    for key in ("hotNews", "financialInformationNews", "editorHandpicked", "morningEveningNews"):
        for it in data.get(key, []) or []:
            name = it.get("name") or it.get("title") or ""
            if not name or name in seen:
                continue
            seen.add(name)
            cid = it.get("contId") or ""
            out.append({"title": name.strip()[:120],
                        "url": f"https://www.thepaper.cn/newsDetail_forward_{cid}" if cid else "",
                        "interaction": it.get("interactionNum") or "",
                        "_site": "thepaper"})
    return out


def match_thepaper(url):
    return "thepaper.cn" in url


register("thepaper", match_thepaper, parse_thepaper, fetch=fetch_thepaper, desc="澎湃新闻")


# ===========================================================================
# 期刊系统精配（magtech：沈阳体育学院学报等——期次/文章/PDF，公开全文）
# ===========================================================================
def match_sytyxb(url: str) -> bool:
    return "stxb.magtech.com.cn" in url and ("/Y20" in url or "showOldVolumn" in url or "/10.12163/" in url)


def _sytyxb_parse(html, url):
    from .journals import parse_issue_html
    m = re.search(r"/Y(\d{4})/V(\d+)/I(\d+)", url)
    issue = {"year": m.group(1), "vol": m.group(2), "issue": m.group(3),
             "label": f"{m.group(1)}年 第{m.group(3)}期"} if m else {}
    return parse_issue_html(html, issue)


register("sytyxb", match_sytyxb, _sytyxb_parse, fetch=fetch_html,
         desc="沈阳体育学院学报：期次/文章/PDF（magtech 期刊系统）")


# ===========================================================================
# 浏览器渲染精配（京东/知乎/微博/抖音/快手：SSR 无数据，需要真实浏览器）
# ===========================================================================

def browser_fetch(url, cookie="", proxy=None):
    """用真实浏览器渲染页面后返回 HTML（无头；需登录的站会走登录档案）。"""
    import subprocess, tempfile, os
    from pathlib import Path as _P
    root = _P(__file__).resolve().parent.parent
    node = os.environ.get("UNIVERSAL_SCRAPER_NODE",
                          "/Users/kairanqin/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node")
    npath = os.environ.get("UNIVERSAL_SCRAPER_NODE_PATH",
                           "/Users/kairanqin/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules")
    spec = {"url": url, "wait": {"selector": "body", "timeout": 12000},
            "scrollCount": 1, "scrollWait": 1000}
    with tempfile.TemporaryDirectory() as tmp:
        sp = _P(tmp) / "spec.json"; out = _P(tmp) / "out"; out.mkdir()
        sp.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        cmd = [node, str(root / "scripts/browser_generic.cjs"),
               "--spec", str(sp), "--out", str(out), "--headless", "1",
               "--profile", str(root / "outputs" / ".browser_profile"),
               "--storageState", str(root / "outputs" / ".session" / "session.json")]
        env = {**os.environ, "NODE_PATH": npath}
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=120)
        except Exception as e:
            return {"ok": False, "status": 0, "html": "", "final_url": url, "error": str(e)}
        files = sorted(out.glob("*.html"))
        if files:
            return {"ok": True, "status": 200, "html": files[0].read_text(encoding="utf-8", errors="replace"),
                    "final_url": url}
        return {"ok": False, "status": 0, "html": "", "final_url": url, "error": "渲染无输出"}


def parse_browser_generic(html, url, site):
    from lxml import html as lh
    try:
        doc = lh.fromstring(html)
    except Exception:
        return []
    cfg = {
        "jd": {"row": ".gl-item, .gl-warp .gl-item, li[data-sku], .search-product-list .product-item",
               "title": ".p-name a, .p-name em, .title a", "link": ".p-name a, .title a",
               "price": ".p-price i, .price i, .p-price"},
        "weibo": {"row": ".card-wrap, .card, .m-item-list li",
                  "title": ".txt, h2, .title, a[href*='/weibo?']", "link": "a[href*='weibo.com/']"},
        "zhihu": {"row": ".SearchResult-Card, .List-item, .search-result-card",
                  "title": ".ContentItem-title, h2, .title", "link": "a[href*='zhihu.com/']"},
        "douyin": {"row": "[data-e2e='search-card'], .xgplayer, .video-card, .search-result-card",
                   "title": "[data-e2e='search-card-title'], .title, h3", "link": "a[href*='douyin.com/video/']"},
        "kuaishou": {"row": ".video-card, .search-result-card, .card",
                     "title": ".title, h3, .video-title", "link": "a[href*='kuaishou.com/']"},
    }.get(site, {})
    out = []
    for el in doc.cssselect(cfg.get("row", "body > *"))[:30]:
        title = ""
        for sel in (cfg.get("title") or "").split(","):
            sel = sel.strip()
            if not sel:
                continue
            els = el.cssselect(sel)
            if els:
                title = els[0].text_content().strip()
                break
        if not title:
            continue
        link = ""
        for sel in (cfg.get("link") or "").split(","):
            sel = sel.strip()
            if not sel:
                continue
            els = el.cssselect(sel)
            if els and els[0].get("href"):
                link = els[0].get("href")
                if link.startswith("//"):
                    link = "https:" + link
                break
        out.append({"title": title[:120], "url": link, "_site": site})
    return out


SITE_DOMAINS = {"jd": "jd.com", "weibo": "weibo.com", "zhihu": "zhihu.com",
                "douyin": "douyin.com", "kuaishou": "kuaishou.com"}
for _name, _dom in SITE_DOMAINS.items():
    def _mk(_n, _d):
        def matcher(url):
            return _d in url
        def parse(html, url):
            return parse_browser_generic(html, url, _n)
        return matcher, parse
    _m, _p = _mk(_name, _dom)
    register(_name, _m, _p, fetch=browser_fetch, desc=f"{_name}（浏览器渲染）")
