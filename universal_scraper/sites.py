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
    {"name": "全国公共资源交易平台", "domain": "ggzy.gov.cn", "module": "ggzy", "status": "✅ 已精配",
     "desc": "招标/中标公告搜索（真实浏览器过WAF，历史交易dealList）", "difficulty": "中高·WAF+可能验证码",
     "url_tips": "搜索入口: https://www.ggzy.gov.cn/history/dealList.html?keyword=<关键词>&begin=YYYY-MM-DD&end=YYYY-MM-DD&stages=0001,0002（0001=招标公告, 0002=中标公告）；详情页 /deal/html/a/xxx.html 正文在 /deal/html/b/xxx.html"},
    {"name": "社科院期刊系（ajcass）", "domain": "*.ajcass.com", "module": "ajcass", "status": "✅ 已精配",
     "desc": "中国社科院各刊官网（中国工业经济/经济研究/金融研究等同一套系统）：期次列表=GetIssueContentList，含[摘要]/作者/全文PDF/浏览人次；期次页有 WAF 滑块",
     "difficulty": "中·期次页有 WAF 滑块（浏览器模式滑一次）",
     "url_tips": "期次列表: /Magazine/GetIssueContentList?Year=2026&Issue=1；文章: /Magazine/Show?id=.. 或 /Magazine/show/?id=.."},
]

# ---------------------------------------------------------------------------
# 注册表：URL 匹配 → 解析器
# ---------------------------------------------------------------------------
# 每个条目: {name, match: fn(url)->bool, parse: fn(html, url)->list[dict] 或 dict,
#           fetch: fn(url, cookie, proxy)->(html, final_url) 可选覆盖默认 HTTP}
SITES: Dict[str, Dict[str, Any]] = {}


def register(name: str, matcher: Callable[[str], bool], parser: Callable,
             fetch: Optional[Callable] = None, desc: str = "", run: Optional[Callable] = None):
    SITES[name] = {"name": name, "match": matcher, "parse": parser,
                   "fetch": fetch, "desc": desc, "run": run}


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


def parse_site_html(url: str, html: str) -> List[Dict[str, Any]]:
    """注册表精配解析（引擎兜底用）：命中站点直接解析 HTML，未命中/空返回 []。"""
    site = match_site(url)
    if not site or not html:
        return []
    s = SITES[site]
    try:
        rows = s["parse"](html, url)
    except Exception:
        return []
    if not isinstance(rows, list):
        return []
    # 空壳行防线
    rows = [r for r in rows if any(str(v or "").strip() for k, v in r.items() if k != "_site")]
    for r in rows:
        r.setdefault("_site", site)
    return rows


def run_site(url: str, cookie: str = "", proxy: Optional[str] = None,
            limit: int = 20, out_name: Optional[str] = None) -> Dict[str, Any]:
    """通用精配执行：命中注册表 → 抓取 → 解析 → 导出。返回 {total, rows, files, error}。"""
    site = match_site(url)
    if not site:
        return {"total": 0, "rows": [], "files": {}, "error": f"未命中精配站点: {url}"}
    s = SITES[site]
    if s.get("run"):
        # 直达型精配（如 ggzy 真浏览器桥）：直接产出记录，不走 fetch+parse
        try:
            rows = s["run"](url, cookie=cookie, proxy=proxy, limit=limit) or []
        except Exception as e:
            return {"total": 0, "rows": [], "files": {}, "error": f"精配运行失败: {type(e).__name__}: {e}"}
        rows = [r for r in rows if any(str(v or "").strip() for k, v in r.items() if k != "_site")]
        if not rows:
            return {"total": 0, "rows": [], "files": {}, "error": "解析 0 条（可能触发验证码/WAF/无结果）"}
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


# ---------------------------------------------------------------------------
# 社科院期刊系（*.ajcass.com）：中国工业经济/经济研究/金融研究等同一套系统
# 期次列表页结构（div.neirong > div）：
#   <a href="/Magazine/show/?id=.." style="...bold;">标题</a>
#   [摘要]摘要… ｜ 作者：xxx ｜ 全文：[PDF xx KB] 2026.43(1) 共有<b> N </b>人次浏览
# ---------------------------------------------------------------------------
def match_ajcass(url: str) -> bool:
    try:
        host = urllib.parse.urlparse(url).hostname or ""
        return host.endswith(".ajcass.com") or host == "ajcass.com"
    except Exception:
        return False


def _ajcass_block(row_html: str, field: str, default: str = "") -> str:
    """按字段名从文章块提取干净文本（标题/作者/期号/浏览人次/摘要/PDF大小）。"""
    try:
        from lxml import html as _lh
        doc = _lh.fromstring(row_html)
        txt = " ".join(doc.text_content().split())
    except Exception:
        txt = re.sub(r"<[^>]+>", " ", row_html)
        txt = " ".join(txt.split())
    if field == "title":
        m = re.search(r'<a[^>]+href="([^"]*(?:Magazine/Show|Magazine/show)[^"]*)"[^>]*>(.*?)</a>', row_html, re.S | re.I)
        if m:
            return " ".join(re.sub(r"<[^>]+>", " ", m.group(2)).split())
        return default
    if field == "link":
        m = re.search(r'href="([^"]*(?:Magazine/Show|Magazine/show)[^"]*)"', row_html, re.I)
        return m.group(1) if m else default
    if field == "authors":
        m = re.search(r"作者[：:]\s*([^。；;\n]{1,120}?)", txt)
        return m.group(1).strip().rstrip("】") if m else default
    if field == "issue":
        m = re.search(r"(\d{4})\s*\.\s*(\d+)\s*\((\d+)\)", txt)
        if m:
            return f"{m.group(1)}年第{m.group(3)}期(卷{m.group(2)})"
        return default
    if field == "views":
        m = re.search(r"共有\s*([\d,]+)\s*人次浏览", txt)
        return m.group(1).replace(",", "") if m else default
    if field == "summary":
        m = re.search(r"\[摘要\](.*?)(?:作者[：:]|$)", txt)
        return (m.group(1).strip() or default) if m else default
    if field == "pdf_size":
        m = re.search(r"([\d.]+)\s*KB", txt)
        return m.group(1) if m else default
    return default


def parse_ajcass(html: str, url: str = "") -> List[Dict[str, Any]]:
    """解析 ajcass 期刊期次列表页 → 文章记录（含中英文双字段名，兼容 AI 配置 pipeline）。"""
    if not html:
        return []
    rows: List[Dict[str, Any]] = []
    _lh = None
    try:
        from lxml import html as _lh_mod
        _lh = _lh_mod
        doc = _lh.fromstring(html)
        blocks = doc.cssselect("div.neirong > div") or doc.cssselect("div.neirong")
    except Exception:
        blocks = []
    if not blocks:
        for m in re.finditer(r"<div[^>]*neirong[^>]*>(.*?)</div>", html, re.S | re.I):
            blocks.append(m.group(1))
    for b in blocks:
        if not isinstance(b, str) and _lh is None:
            continue  # 无 lxml 时只处理字符串块
        b_html = b if isinstance(b, str) else _lh.tostring(b, encoding="unicode")
        title = _ajcass_block(b_html, "title")
        link = _ajcass_block(b_html, "link")
        if not title and not link:
            continue
        authors = _ajcass_block(b_html, "authors")
        issue = _ajcass_block(b_html, "issue")
        views = _ajcass_block(b_html, "views")
        summary = _ajcass_block(b_html, "summary")
        pdf_size = _ajcass_block(b_html, "pdf_size")
        if not views and not issue and not title:
            continue
        rows.append({
            "标题": title, "title": title,
            "链接": link, "link": link,
            "作者": authors, "authors": authors,
            "刊期": issue, "issue": issue,
            "浏览人次": views, "views": views, "download_raw": views,
            "摘要": summary, "summary": summary,
            "pdf_size": pdf_size,
        })
    return rows


def _ajcass_fetch(url, cookie="", proxy=None):
    # 期次列表页有 CWAP-WAF 滑块：HTTP 抓不到就返回错误，由 auto 层自动升级浏览器
    res = fetch_html(url, cookie=cookie, proxy=proxy)
    if res.get("ok"):
        try:
            from .antibot import detect_block
            bd = detect_block(res.get("status", 200), res.get("html", ""),
                              res.get("headers") or {}, url)
            if bd["kind"] == "waf":
                res["ok"] = False
                res["error"] = "WAF滑块拦截（wzws/CWAP waf_slider_verify）——请用浏览器模式滑一次"
        except Exception:
            pass
    return res


register("ajcass", match_ajcass, parse_ajcass, fetch=_ajcass_fetch,
         desc="社科院期刊系（*.ajcass.com）：期次列表/文章/浏览人次，期次页有WAF滑块")


# ---------------------------------------------------------------------------
# 全国公共资源交易平台（ggzy.gov.cn）：招标/中标公告搜索
# 走专用真浏览器桥（过 WAF/可能验证码），URL 用 query 传参：
#   /history/dealList.html?keyword=数据中心&begin=2025-04-10&end=2025-04-10&stages=0001,0002
# 0001=招标公告(交易公告), 0002=中标公告(成交公示)
# ---------------------------------------------------------------------------
def match_ggzy(url: str) -> bool:
    return "ggzy.gov.cn" in (url or "")


def _ggzy_run(url: str, cookie: str = "", proxy: Optional[str] = None,
              limit: int = 20) -> List[Dict[str, Any]]:
    from urllib.parse import urlparse, parse_qs
    q = parse_qs(urlparse(url).query)
    keyword = (q.get("keyword") or q.get("keywords") or [""])[0].strip()
    begin = (q.get("begin") or q.get("beginDate") or [""])[0].strip()
    end = (q.get("end") or q.get("endDate") or [""])[0].strip()
    stages = (q.get("stages") or q.get("stage") or ["0001,0002"])[0].split(",")
    max_pages = int((q.get("max_pages") or ["50"])[0])
    if not keyword:
        return []
    from pathlib import Path as _P
    from .browser import crawl_ggzy_list, CaptchaError, BrowserBridgeError
    bridge = _P(__file__).resolve().parent.parent / "scripts" / "ggzy_bridge.cjs"
    stage_labels = {"0001": "招标公告", "0002": "中标公告", "0003": "变更公告"}
    out: List[Dict[str, Any]] = []
    import time as _time
    for st in stages:
        st = st.strip()
        recs = None
        last_err = ""
        # WAF/限流可能是瞬时的：最多重试 3 次，退避逐步拉长（15/30/60s），不硬刚；
        # 桥内部还有 6 次页级重试 + 6 分钟整体时限，绝不无限拖
        for _attempt in range(1, 4):
            try:
                recs = crawl_ggzy_list(bridge, keyword, begin, end, st,
                                       max_pages=max_pages, settle=2500, deadline_ms=360000)
                break
            except CaptchaError as e:
                raise RuntimeError(f"ggzy 触发验证码：{e}（可稍后重试/换网络，或人工打开网页过验证）")
            except BrowserBridgeError as e:
                last_err = str(e)
                _time.sleep(15 * _attempt)
        if recs is None:
            raise RuntimeError(f"ggzy 桥错误（重试3次后）：{last_err}")
        for r in recs or []:
            if not isinstance(r, dict):
                continue
            title = str(r.get("title") or r.get("noticeTitle") or r.get("projectName") or "")
            link = str(r.get("url") or r.get("link") or r.get("noticeUrl") or "")
            date = str(r.get("publishTime") or r.get("publishDate") or r.get("date") or r.get("pubDate") or "")
            region = str(r.get("provinceText") or r.get("cityText") or r.get("region") or r.get("area") or "")
            row = {
                "标题": title, "title": title,
                "链接": link, "link": link,
                "日期": date, "publish_date": date,
                "地区": region, "region": region,
                "类型": stage_labels.get(st, st), "stage": st,
            }
            for k, v in r.items():
                if k not in row:
                    row[k] = v
            out.append(row)
            if len(out) >= limit:
                break
    return out


register("ggzy", match_ggzy, lambda html, url: [], run=_ggzy_run,
         desc="全国公共资源交易平台：招标/中标公告（真浏览器过WAF，URL带keyword/begin/end/stages）")


# ===========================================================================
# 淘宝/天猫旗舰店精配（CDP 附着用户已登录 Chrome——绕开 AI 猜配置）
# ===========================================================================

def match_taobao(url: str) -> bool:
    return "tmall.com" in (url or "") or "taobao.com" in (url or "")


def _taobao_run(url: str, cookie: str = "", proxy: Optional[str] = None,
                limit: int = 20) -> List[Dict[str, Any]]:
    """附着 CDP 9222（用户已登录的 Chrome）抓店铺商品 + 详情产品参数。
    前提：先双击「启动淘宝调试Chrome.command」并登录淘宝一次。"""
    from pathlib import Path as _P
    from .runtime import resolve_node, resolve_node_path
    import subprocess, os
    bridge = _P(__file__).resolve().parent.parent / "scripts/taobao_shop_bridge.cjs"
    node = os.environ.get("UNIVERSAL_SCRAPER_NODE", resolve_node())
    npath = os.environ.get("UNIVERSAL_SCRAPER_NODE_PATH", resolve_node_path())
    cdp = os.environ.get("US_CDP", "http://127.0.0.1:9222")
    cmd = [node, str(bridge), "--cdp", cdp, "--shop", url, "--max", str(int(limit or 20))]
    env = {**os.environ, "NODE_PATH": npath}
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=900)
    except subprocess.TimeoutExpired:
        raise RuntimeError("淘宝/天猫精配超时（>900s），可能卡在详情页")
    rows: List[Dict[str, Any]] = []
    meta = {}
    for line in (p.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        t = obj.get("type")
        if t == "meta":
            meta = {**meta, **{k: v for k, v in obj.items() if k != "type"}}
        elif t == "item":
            rows.append({
                "标题": str(obj.get("title") or obj.get("list_title") or "").strip(),
                "价格": str(obj.get("price") or "").strip(),
                "店铺": str(obj.get("shop") or "").strip(),
                "链接": str(obj.get("url") or "").strip(),
                "产品参数": "；".join([str(x) for x in (obj.get("params") or []) if str(x).strip()]),
            })
        elif t == "login":
            raise RuntimeError(f"淘宝/天猫需要人工验证：{obj.get('message')}（请在调试 Chrome 里登录/拖滑块）")
        elif t == "error":
            raise RuntimeError(f"淘宝/天猫精配失败：{obj.get('message')}")
    if not rows:
        err = meta.get("items_found", 0) if meta else 0
        raise RuntimeError(f"淘宝/天猫精配 0 条（页面商品链接 {err} 个；请确认已登录淘宝且店铺 URL 正确）")
    return rows


register("tmall_taobao", match_taobao, lambda html, url: [], run=_taobao_run,
         desc="淘宝/天猫店铺：CDP 附着已登录 Chrome 抓商品列表+详情产品参数（需先开调试 Chrome 并登录一次）")


# ===========================================================================
# 浏览器渲染精配（京东/知乎/微博/抖音/快手：SSR 无数据，需要真实浏览器）
# ===========================================================================

def browser_fetch(url, cookie="", proxy=None):
    """用真实浏览器渲染页面后返回 HTML（无头；需登录的站会走登录档案）。"""
    import subprocess, tempfile, os
    from pathlib import Path as _P
    root = _P(__file__).resolve().parent.parent
    from .runtime import resolve_node, resolve_node_path
    node = os.environ.get("UNIVERSAL_SCRAPER_NODE", resolve_node())
    npath = os.environ.get("UNIVERSAL_SCRAPER_NODE_PATH", resolve_node_path())
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
