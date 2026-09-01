#!/usr/bin/env python3
"""🏆 高频网站精配解析器注册表（v1）。

架构：每个网站一个「精配解析器」（match + parse），auto/WebUI 检测到命中 URL 就自动用它
替代 AI 猜测的选择器，字段干净、速度快。未命中的网站走通用 AI 流程，不受影响。

如何新增网站：复制 dianping 的写法，实现 match(url) + parse(html_or_json, ...)，
然后注册到 SITES 字典即可。
"""
from __future__ import annotations

from pathlib import Path

import bisect
import json
import re
import urllib.parse
from typing import Any, Callable, Dict, List, Optional

from .book_catalog import parse_dangdang_search as _parse_dangdang_search
from .book_catalog import parse_douban_book_buylinks as _parse_douban_book_buylinks
from .book_catalog import parse_douban_book_detail as _parse_douban_book_detail
from .book_catalog import parse_douban_book_search as _parse_douban_book_search

# ---------------------------------------------------------------------------
# 高频网站表（中文互联网高频数据源）
# ---------------------------------------------------------------------------
HIGH_FREQUENCY_SITES = [
    {"name": "大众点评", "domain": "dianping.com", "module": "dianping", "status": "✅ 已精配",
     "desc": "美食/商家列表（Cookie 直抓 SSR）", "difficulty": "高反爬·需登录 Cookie"},
    {"name": "京东", "domain": "item.jd.com", "module": "jd", "status": "✅ 聚合价方案（不绕过登录）",
     "desc": "商品详情/搜索当前会跳登录墙；价格采用豆瓣在哪儿买公开聚合价+联盟跳转",
     "difficulty": "高·强风控+需登录；不逆向h5st/不获取登录态",
     "url_tips": "商品页: https://item.jd.com/<skuID>.html；在哪儿买聚合: https://book.douban.com/subject/<id>/buylinks"},
    {"name": "当当网", "domain": "search.dangdang.com", "module": "dangdang_search", "status": "✅ 已精配",
     "desc": "图书 ISBN 搜索：实时价/划线价/商品链接（HTTP SSR）",
     "difficulty": "低·公开搜索页",
     "url_tips": "搜索: https://search.dangdang.com/?key=<ISBN>"},
    {"name": "豆瓣", "domain": "douban.com", "module": "douban", "status": "✅ 已精配",
     "desc": "电影/图书/小组", "difficulty": "中·有反爬但 SSR"},
    {"name": "豆瓣读书", "domain": "book.douban.com", "module": "douban_book", "status": "✅ 已精配",
     "desc": "书目详情/ISBN搜索/在哪儿买（京东/当当聚合价）",
     "difficulty": "中·有反爬但 SSR；需限速",
     "url_tips": "详情: /subject/<id>/；在哪儿买: /subject/<id>/buylinks；搜索: /subject_search?cat=1001&search_text=<ISBN>"},
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
    {"name": "科研管理", "domain": "kygl.net.cn", "module": "kygl", "status": "✅ 已精配",
     "desc": "期刊目录/文章/官方MAG XML全文（2026 全部期次）", "difficulty": "中·PDF需权限，XML公开",
     "url_tips": "期次: /CN/Y<年>/V<卷>/I<期>；文章: /CN/<DOI>；全文 XML: /article/2026/.../<id>/<file>.mag.xml"},
    {"name": "全国公共资源交易平台", "domain": "ggzy.gov.cn", "module": "ggzy", "status": "✅ 已精配",
     "desc": "招标/中标公告搜索（真实浏览器过WAF，历史交易dealList）", "difficulty": "中高·WAF+可能验证码",
     "url_tips": "搜索入口: https://www.ggzy.gov.cn/history/dealList.html?keyword=<关键词>&begin=YYYY-MM-DD&end=YYYY-MM-DD&stages=0001,0002（0001=招标公告, 0002=中标公告）；详情页 /deal/html/a/xxx.html 正文在 /deal/html/b/xxx.html"},
    {"name": "工信部APP通报", "domain": "miit.gov.cn", "module": "miit", "status": "✅ 已精配",
     "desc": "APP（SDK）侵害用户权益通报：通知公告列表（JS渲染）+ 详情 PDF 附件表格（App名称/版本/所涉问题）",
     "difficulty": "中·JS渲染+PDF附件",
     "url_tips": "专用栏目: https://www.miit.gov.cn/jgsj/xgj/APPqhyhqyzxzzxd/tzgg/ （不是 /zwgk/zcwj/wjfb/tz/，那是规划类通知）"},
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
             fetch: Optional[Callable] = None, desc: str = "", run: Optional[Callable] = None,
             keywords: tuple = (), seed_url: str = ""):
    SITES[name] = {"name": name, "match": matcher, "parse": parser,
                   "fetch": fetch, "desc": desc, "run": run,
                   "keywords": tuple(keywords), "seed_url": seed_url}


# 任务描述 → 精配语义路由：一句话任务时，AI 可能选错入口 URL，
# 用描述关键词直接命中精配库（Firecrawl feature-routing 思路），
# 命中即用精配种子 URL 跑，不再让 AI 乱猜入口。
_DESC_ROUTE = [
    ("zige", ("职业资格", "职业目录", "资格考试目录", "职业技能等级", "国家职业资格"), "https://www.gov.cn/zhengce/zhengceku/2021-12/03/content_5655553.htm"),
    ("tax", ("税务总局", "税收政策", "增值税", "税收法规", "税务局公告", "税法", "留抵退税", "关税"), "https://www.chinatax.gov.cn/search5/search/s?searchWord=%E5%A2%9E%E5%80%BC%E7%A8%8E&column=%E6%94%BF%E7%AD%96%E6%B3%95%E8%A7%84&strict=1"),
    ("nhsa", ("医保", "医保局", "药品目录", "医药", "医疗保障", "集中采购"), "http://www.nhsa.gov.cn/col/col104/index.html"),
    ("eq", ("地震", "震级", "震源深度", "earthquake"), "https://earthquake.usgs.gov/fdsnws/event/1/query?format=geojson&starttime=NOW24H&minlatitude=18&maxlatitude=54&minlongitude=73&maxlongitude=135&minmagnitude=0"),
    ("miit", ("工信部", "app通报", "侵害用户权益", "app（sdk）", "违规收集个人信息"), ""),
    ("std", ("国家标准", "标准平台", "标准号", "国标", "标准搜索"), ""),
    ("ggzy", ("公共资源交易", "招标", "中标", "采购公告", "ggzy"), ""),
    ("cninfo", ("巨潮", "上市公司公告", "年报", "公告查询"), ""),
    ("zszc", ("招生章程", "阳光高考", "招生简章"), ""),
    ("sytyxb", ("沈阳体育学院学报", "体育学院学报"), ""),
    ("ajcass", ("社科院期刊", "期刊系", "ajcass"), ""),
    ("weathercn", ("中国天气", "天气", "天气预报", "实时气温"), ""),
    ("github_trending", ("github trending", "github热榜", "热门仓库"), ""),
    ("stackoverflow", ("stack overflow", "stackoverflow", "stackoverflow.com", "stack exchange"), "https://api.stackexchange.com/2.3/questions?tagged={TAG}&fromdate=EPOCH24H&sort=creation&order=desc&site=stackoverflow&pagesize=20&filter=default"),
    ("arxiv", ("arxiv", "论文预印本", "每日论文"), ""),
    ("leetcode", ("leetcode", "力扣", "题库"), ""),
    ("douban", ("豆瓣", "豆瓣电影", "豆瓣读书", "图书目录", "书目", "书籍目录", "ISBN"), ""),
    ("bilibili", ("b站", "bilibili", "哔哩哔哩", "视频搜索"), ""),
    ("dianping", ("大众点评", "点评", "美食商家"), ""),
    ("douyin", ("抖音", "douyin", "短视频"), ""),
    ("kuaishou", ("快手", "kuaishou"), ""),
    ("weibo", ("微博", "weibo"), ""),
    ("zhihu", ("知乎", "zhihu"), ""),
    ("jd", ("京东", "jd.com", "京东商城"), ""),
    ("tmall_taobao", ("淘宝", "天猫", "taobao", "tmall", "店铺"), ""),
]


def match_site_by_description(desc: str) -> Optional[str]:
    """按任务描述关键词匹配精配，返回精配名；未命中 None。"""
    d = (desc or "").lower()
    for name, kws, _seed in _DESC_ROUTE:
        for kw in kws:
            if kw in d:
                return name
    return None


def seed_url_for(desc: str) -> str:
    """描述命中的精配种子 URL（TODAY 占位符替换为今天）。"""
    d = (desc or "").lower()
    for name, kws, seed in _DESC_ROUTE:
        if any(kw in d for kw in kws):
            if "NOW24H" in seed:
                from datetime import datetime, timedelta, timezone
                seed = seed.replace("NOW24H",
                                    (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S"))
            if "EPOCH24H" in seed:
                # Stack Exchange API 要 Unix 秒，不是 ISO 时间
                from datetime import datetime, timedelta, timezone as _tz
                seed = seed.replace("EPOCH24H",
                                    str(int((datetime.now(_tz.utc) - timedelta(hours=24)).timestamp())))
            elif "TODAY" in seed:
                from datetime import date
                seed = seed.replace("TODAY", date.today().strftime("%Y-%m-%dT%H:%M:%S"))
            if "{TAG}" in seed:
                # 从描述抽标签（如 标签"python" / tagged=python），抽不到就删掉 tagged 参数
                import re as _re, urllib.parse as _up
                _m = _re.search('(?:标签|tagged|tag)[:=：\\s]*["\'“”]?([A-Za-z0-9][A-Za-z0-9\\-+#.]{0,30})', d)
                _tag = _m.group(1).strip() if _m else ""
                if _tag:
                    seed = seed.replace("{TAG}", _up.quote(_tag))
                else:
                    seed = seed.replace("tagged={TAG}&", "")
            return seed
    return ""


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
               "douban": "douban", "douban_book": "douban_book_detail",
               "dangdang_search": "dangdang_search", "bilibili": "bilibili",
               "github": "github", "weather": "weather", "netease_news": "netease"}
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
        # 只报真实导出成功的文件：写入失败不伪装成功（参考 quick.py 同类做法）
        files: Dict[str, str] = {}
        _warns: List[str] = []
        try:
            fp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
            files["json"] = f"outputs/{name}.json"
        except Exception as _e:
            _warns.append(f"JSON 导出失败: {_e}")
        try:
            import csv
            with open(out_dir / f"{name}.csv", "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=[k for k in rows[0] if k != "_site"])
                w.writeheader()
                w.writerows([{k: v for k, v in r.items() if k != "_site"} for r in rows])
            files["csv"] = f"outputs/{name}.csv"
        except Exception as _e:
            _warns.append(f"CSV 导出失败: {_e}")
        try:
            from openpyxl import Workbook
            wb = Workbook(); ws = wb.active
            keys = [k for k in rows[0] if k != "_site"]
            ws.append(keys)
            for r in rows:
                ws.append([r.get(k, "") for k in keys])
            wb.save(out_dir / f"{name}.xlsx")
            files["xlsx"] = f"outputs/{name}.xlsx"
        except Exception as _e:
            _warns.append(f"XLSX 导出失败: {_e}")
        ret = {"total": len(rows), "rows": rows, "files": files, "error": "", "site": site}
        if _warns:
            ret["warning"] = "；".join(_warns)
        return ret
    fetch = s.get("fetch") or fetch_html
    try:
        res = fetch(url, cookie=cookie, proxy=proxy)
    except TypeError:
        res = fetch_html(url, cookie=cookie, proxy=proxy)
    if not res.get("ok"):
        return {"total": 0, "rows": [], "files": {},
                "error": f"抓取失败 HTTP {res.get('status')}: {res.get('error','')}"}
    rows = s["parse"](res.get("html", ""), url)
    if limit and int(limit) > 0:
        rows = rows[:int(limit)]
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
    # 只报真实导出成功的文件：写入失败不伪装成功（参考 quick.py 同类做法）
    files: Dict[str, str] = {}
    _warns: List[str] = []
    try:
        fp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        files["json"] = f"outputs/{name}.json"
    except Exception as _e:
        _warns.append(f"JSON 导出失败: {_e}")
    try:
        import csv
        with open(out_dir / f"{name}.csv", "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=[k for k in rows[0] if k != "_site"])
            w.writeheader()
            w.writerows([{k: v for k, v in r.items() if k != "_site"} for r in rows])
        files["csv"] = f"outputs/{name}.csv"
    except Exception as _e:
        _warns.append(f"CSV 导出失败: {_e}")
    try:
        from openpyxl import Workbook
        wb = Workbook(); ws = wb.active
        keys = [k for k in rows[0] if k != "_site"]
        ws.append(keys)
        for r in rows:
            ws.append([r.get(k, "") for k in keys])
        wb.save(out_dir / f"{name}.xlsx")
        files["xlsx"] = f"outputs/{name}.xlsx"
    except Exception as _e:
        _warns.append(f"XLSX 导出失败: {_e}")
    ret = {"total": len(rows), "rows": rows, "files": files, "error": "", "site": site}
    if _warns:
        ret["warning"] = "；".join(_warns)
    return ret


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
    if not html or not html.strip():
        return []
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
    if "book.douban.com" in url and any(k in url for k in ("/subject/", "/subject_search", "/buylinks")):
        return False
    return "douban.com" in url and any(k in url for k in ("movie", "book", "group", "/subject/", "search"))


register("douban", match_douban, parse_douban, desc="豆瓣：电影/图书榜单与搜索")


# ---------- 豆瓣读书（书目详情/搜索/在哪儿买） ----------
def match_douban_book_detail(url: str) -> bool:
    return "book.douban.com/subject/" in url and "/buylinks" not in url


def match_douban_book_search(url: str) -> bool:
    return "book.douban.com/subject_search" in url


def match_douban_book_buylinks(url: str) -> bool:
    return "book.douban.com/subject/" in url and "/buylinks" in url


def match_dangdang_search(url: str) -> bool:
    return "search.dangdang.com" in url and "key=" in url


register("douban_book_detail", match_douban_book_detail, _parse_douban_book_detail,
         desc="豆瓣读书：书目详情（评分/ISBN/出版社/简介/封面）")
register("douban_book_search", match_douban_book_search, _parse_douban_book_search,
         desc="豆瓣读书：ISBN/书名搜索")
register("douban_book_buylinks", match_douban_book_buylinks, _parse_douban_book_buylinks,
         desc="豆瓣读书：在哪儿买（京东/当当公开聚合价）")
register("dangdang_search", match_dangdang_search, _parse_dangdang_search,
         desc="当当：图书 ISBN 搜索（实时价/链接）")


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
    if not html or not html.strip():
        return []
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
    if not html or not html.strip():
        return []
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



def match_kygl(url: str) -> bool:
    return "kygl.net.cn" in url and ("/CN/Y20" in url or "showOldVolumn" in url or "/CN/10.19571/" in url or "/article/2026/1000-2995/" in url)


def _kygl_parse(html, url):
    from .journals import parse_issue_html
    m = re.search(r"/CN/Y(\d{4})/V(\d+)/I(\d+)", url)
    issue = {"year": m.group(1), "vol": m.group(2), "issue": m.group(3),
             "label": f"{m.group(1)}年 第{m.group(3)}期"} if m else {}
    site = {"base": "https://www.kygl.net.cn", "ctx": "/CN"}
    return parse_issue_html(html, issue, site=site)


register("kygl", match_kygl, _kygl_parse, fetch=fetch_html,
         desc="科研管理：期次/文章/全部全文（magtech 期刊系统，官方 MAG XML 公开）")


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
    """淘宝/天猫精配：优先走快引擎（店铺移动端接口 / MTOP 搜索 / CDP 并行详情），
    失败时兜底旧桥。前提：先双击「启动淘宝调试Chrome.command」并登录淘宝一次。"""
    from pathlib import Path as _P
    from urllib.parse import urlparse as _up, parse_qs as _pq
    import subprocess, os, sys
    root = _P(__file__).resolve().parent.parent
    engine = root / "scripts" / "taobao_tmall_engine.py"
    cdp = os.environ.get("US_CDP", "http://127.0.0.1:9222")
    limit = int(limit or 20)
    mode, target = None, url
    host = (_up(url).netloc or "").lower()
    if "tmall.com" in host and not host.startswith("s.") and "search" not in url:
        mode = "shop"
    elif "s.taobao.com" in host or "search" in url.lower():
        mode = "search"
        q = _pq(_up(url).query).get("q") or _pq(_up(url).query).get("keyword")
        if q:
            target = q[0]
    try:
        cmd = [sys.executable, str(engine), "--mode", mode or "shop", "--target", target,
               "--max", str(limit), "--workers", "2", "--json"]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        for line in (p.stdout or "").splitlines():
            if line.startswith("RESULT_JSON "):
                res = json.loads(line[len("RESULT_JSON "):])
                rows = res.get("rows") or []
                if rows:
                    return rows
                raise RuntimeError("淘宝/天猫精配 0 条（请确认调试 Chrome 已登录淘宝，且店铺/关键词正确）")
        raise RuntimeError((p.stderr or p.stdout or "").strip().splitlines()[-1][-200:]
                           if (p.stderr or p.stdout or "").strip() else "引擎无输出")
    except RuntimeError as e:
        # 快引擎不可用（如 CDP 未开）→ 兜底旧桥（店铺页 DOM 收集链接）
        if mode == "search" or not url or "tmall.com" not in host:
            raise RuntimeError(str(e))
        from .runtime import resolve_node, resolve_node_path
        bridge = root / "scripts" / "taobao_shop_bridge.cjs"
        node = os.environ.get("UNIVERSAL_SCRAPER_NODE", resolve_node())
        npath = os.environ.get("UNIVERSAL_SCRAPER_NODE_PATH", resolve_node_path())
        cmd2 = [node, str(bridge), "--cdp", cdp, "--shop", url, "--max", str(limit)]
        env = {**os.environ, "NODE_PATH": npath}
        try:
            p2 = subprocess.run(cmd2, capture_output=True, text=True, env=env, timeout=900)
        except subprocess.TimeoutExpired:
            raise RuntimeError("淘宝/天猫精配超时（>900s），可能卡在详情页")
        rows: List[Dict[str, Any]] = []
        meta = {}
        for line in (p2.stdout or "").splitlines():
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
            subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=120)
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


# ===========================================================================
# 高频站点补充精配（100 实战任务覆盖）
# ===========================================================================

# ---------- GitHub Trending（SSR） ----------
def parse_github_trending(html: str, url: str) -> List[Dict[str, Any]]:
    from lxml import html as lh
    try:
        doc = lh.fromstring(html)
    except Exception:
        return []
    out = []
    for art in doc.cssselect("article.Box-row"):
        a = art.cssselect("h2 a")
        if not a:
            continue
        name = re.sub(r"\s+", " ", a[0].text_content()).strip().replace(" / ", "/")
        href = a[0].get("href") or ""
        desc = _css_text(art, "p")
        stars = _css_text(art, "a[href*='stargazers']") or _css_text(art, ".Link--muted")
        lang = _css_text(art, "[itemprop='programmingLanguage']")
        out.append({"repo": name, "url": "https://github.com" + href if href.startswith("/") else href,
                    "description": desc[:200], "stars": stars.strip(), "language": lang, "_site": "github_trending"})
        if len(out) >= 30:
            break
    return out


def match_github_trending(url: str) -> bool:
    return "github.com/trending" in url


register("github_trending", match_github_trending, parse_github_trending,
         desc="GitHub Trending：每周/每日热门仓库（SSR）")


# ---------- Stack Overflow（公开 API api.stackexchange.com，免 Cloudflare） ----------
def parse_stackoverflow(html: str, url: str) -> List[Dict[str, Any]]:
    try:
        data = json.loads(html)
    except Exception:
        return []
    items = data.get("items") or []
    out = []
    for it in items[:30]:
        out.append({
            "title": (it.get("title") or "").strip(),
            "url": it.get("link") or "",
            "score": it.get("score"),
            "answer_count": it.get("answer_count"),
            "tags": ",".join(it.get("tags") or [])[:100],
            "creation_date": it.get("creation_date") or "",
            "_site": "stackoverflow",
        })
    # 24h 新问题里按分数排序（任务常要"高分问题"）
    out.sort(key=lambda r: r.get("score") if isinstance(r.get("score"), (int, float)) else -1e9, reverse=True)
    return out


def match_stackoverflow(url: str) -> bool:
    return "api.stackexchange.com" in url or "stackoverflow.com" in url


register("stackoverflow", match_stackoverflow, parse_stackoverflow,
         desc="Stack Overflow：问答（公开 API api.stackexchange.com，绕 Cloudflare）",
         keywords=("stack overflow", "stackoverflow", "stack exchange"),
         seed_url="https://api.stackexchange.com/2.3/questions?tagged={TAG}&fromdate=EPOCH24H&sort=creation&order=desc&site=stackoverflow&pagesize=20&filter=default")


# ---------- GitHub Topics（SSR，article.border 卡片结构） ----------
def parse_github_topics(html: str, url: str) -> List[Dict[str, Any]]:
    import re as _re
    from lxml import html as lh
    try:
        doc = lh.fromstring(html)
    except Exception:
        return []
    out = []
    for art in doc.cssselect("article.border"):
        h3 = art.cssselect("h3")
        if not h3:
            continue
        # h3 全文 = owner / name（两个 a + 分隔 span），text_content 一次拿全
        name = _re.sub(r"\s+", " ", h3[0].text_content()).strip().replace(" / ", "/")
        a = art.cssselect("h3 a")
        href = a[0].get("href") if a else ""
        desc = _css_text(art, "p")
        # star 数：Topics 页是按钮文本 "Star 171k"（无 stargazers 链接），先精确再兜底
        stars = _css_text(art, "a[aria-label*='stargazers']") or ""
        if not stars:
            _txt = _re.sub(r"\s+", " ", art.text_content())
            _m = _re.search(r"Star (\d+(?:\.\d+)?k?)", _txt)
            if _m:
                stars = _m.group(1)
        lang = _css_text(art, "[itemprop='programmingLanguage']")
        out.append({"repo": name, "url": "https://github.com" + href if href.startswith("/") else href,
                    "description": desc[:200], "stars": stars.strip().replace("Star ", ""), "language": lang,
                    "_site": "github_topics"})
        if len(out) >= 30:
            break
    return out


def match_github_topics(url: str) -> bool:
    return "github.com/topics/" in url


register("github_topics", match_github_topics, parse_github_topics,
         desc="GitHub Topics：按主题浏览热门仓库（SSR，article.border）")


# ---------- arXiv（Atom API / list HTML） ----------
def parse_arxiv(html: str, url: str) -> List[Dict[str, Any]]:
    import xml.etree.ElementTree as ET
    from lxml import html as lh
    out = []
    if "export.arxiv.org" in url or "arxiv.org/api" in url:
        ns = {"a": "http://www.w3.org/2005/Atom"}
        try:
            root = ET.fromstring(html)
            for e in root.findall("a:entry", ns):
                title = re.sub(r"\s+", " ", "".join((e.findtext("a:title", "", ns) or "").split()))
                link = (e.findtext("a:id", "", ns) or "").strip()
                authors = [au.findtext("a:name", "", ns) for au in e.findall("a:author", ns)]
                summary = re.sub(r"\s+", " ", (e.findtext("a:summary", "", ns) or "")).strip()
                published = (e.findtext("a:published", "", ns) or "").strip()
                out.append({"title": title, "url": link, "authors": "、".join(authors)[:200],
                            "abstract": summary[:300], "published": published, "_site": "arxiv"})
        except Exception:
            pass
        return out
    # HTML 列表（arxiv.org/list/...）
    try:
        doc = lh.fromstring(html)
        for li in doc.cssselect("dl dt")[:50]:
            a = li.cssselect("a[href*='/abs/']")
            if not a:
                continue
            out.append({"id": (a[0].get("href") or "").split("/abs/")[-1],
                        "url": "https://arxiv.org" + a[0].get("href", ""), "_site": "arxiv"})
        for dd in doc.cssselect("dl dd")[:50]:
            if len(out) > len(dd.cssselect("")):
                pass
        # 简化：只抓标题行
        if not out:
            for dd in doc.cssselect("dl dd")[:50]:
                title = _css_text(dd, ".list-title")
                if title:
                    out.append({"title": title[:200], "_site": "arxiv"})
    except Exception:
        pass
    return out


def match_arxiv(url: str) -> bool:
    return ("arxiv.org" in url and ("export.arxiv.org" in url or "/list/" in url or "/api/" in url)) or "export.arxiv.org" in url


register("arxiv", match_arxiv, parse_arxiv, desc="arXiv：每日新提交论文（Atom API / 列表）")


# ---------- LeetCode 题库（官方 JSON API） ----------
def parse_leetcode(html: str, url: str) -> List[Dict[str, Any]]:
    try:
        data = json.loads(html)
    except Exception:
        return []
    out = []
    for p in (data.get("stat_status_pairs") or [])[:100]:
        st = p.get("stat") or {}
        title = st.get("question__title") or ""
        if not title:
            continue
        total_acs = st.get("total_acs") or 0
        total_sub = st.get("total_submitted") or 0
        rate = round(total_acs * 100.0 / total_sub, 2) if total_sub else ""
        diff = {1: "简单", 2: "中等", 3: "困难"}.get((p.get("difficulty") or {}).get("level"), "")
        out.append({
            "title": title,
            "frontend_id": st.get("frontend_question_id") or st.get("question_id") or "",
            "difficulty": diff,
            "acceptance": rate,
            "url": f"https://leetcode.cn/problems/{st.get('question__title_slug') or ''}/",
            "status": "已解决" if p.get("status") == "ac" else "",
            "_site": "leetcode",
        })
    return out


def match_leetcode(url: str) -> bool:
    return "leetcode.cn/api/problems" in url or "leetcode.com/api/problems" in url


register("leetcode", match_leetcode, parse_leetcode, desc="LeetCode：题库全量（官方 JSON API）")


# ---------- B站每周必看（官方 API JSON） ----------
def parse_bilibili_weekly(html: str, url: str) -> List[Dict[str, Any]]:
    try:
        data = json.loads(html)
    except Exception:
        return []
    lst = ((data.get("data") or {}).get("list")) or []
    out = []
    for v in lst[:50]:
        st = v.get("stat") or {}
        out.append({
            "bv": v.get("bvid") or "",
            "title": v.get("title") or "",
            "up": (v.get("owner") or {}).get("name") or "",
            "url": f"https://www.bilibili.com/video/{v.get('bvid') or ''}",
            "view": st.get("view") or 0,
            "danmaku": st.get("danmaku") or 0,
            "coin": st.get("coin") or 0,
            "favorite": st.get("favorite") or 0,
            "_site": "bilibili_weekly",
        })
    return out


def match_bilibili_weekly(url: str) -> bool:
    return "api.bilibili.com/x/web-interface/popular/series/one" in url


# 停用（风控/JS渲染，交给通用浏览器流程）
# register("bilibili_weekly", match_bilibili_weekly, parse_bilibili_weekly,
#          desc="B站每周必看：指定期号视频列表（官方 API）")


# ---------- 中国天气网（JSON） ----------
def parse_weathercn(html: str, url: str) -> List[Dict[str, Any]]:
    # 旧 JSON 接口已下线（返回加载页），改为解析 weather1d/weather SSR 页面
    try:
        data = json.loads(html)
    except Exception:
        data = None
    if isinstance(data, dict) and data.get("weatherinfo"):
        wi = data["weatherinfo"]
        return [{
            "city": wi.get("city") or wi.get("cityname") or "",
            "temp": wi.get("temp") or wi.get("temp1") or "",
            "temp_min": wi.get("tempn") or wi.get("temp2") or "",
            "weather": wi.get("weather") or "",
            "wind": (wi.get("WD") or "") + (wi.get("WS") or ""),
            "humidity": wi.get("SD") or "",
            "update": wi.get("time") or wi.get("ptime") or "",
            "_site": "weathercn",
        }]
    from lxml import html as lh
    try:
        doc = lh.fromstring(html)
    except Exception:
        return []
    mt = re.search(r"([\u4e00-\u9fa5]{2,6})天气预报", html or "")
    city = mt.group(1) if mt else _css_text(doc, ".cityName, h1")
    temps = []
    for e in doc.cssselect("#today .tem i"):
        t = re.sub(r"[^0-9℃°]", "", e.text_content() or "").strip()
        if t:
            temps.append(t)
    wea = _css_text(doc, "#today .wea")
    wind = _css_text(doc, "#today .win") or _css_text(doc, "#today .win i")
    if not temps and not wea:
        return []
    return [{
        "city": city or "",
        "temp": "/".join(temps[:2]),
        "weather": wea,
        "wind": wind[:40],
        "update": "",
        "_site": "weathercn",
    }]


def match_weathercn(url: str) -> bool:
    return "weather.com.cn" in url and any(k in url for k in ("weather", "data"))


register("weathercn", match_weathercn, parse_weathercn, desc="中国天气网：城市实时/预报 JSON")


# ---------- 中国政府网·政策文件库（SSR） ----------
def parse_govcn(html: str, url: str) -> List[Dict[str, Any]]:
    from lxml import html as lh
    try:
        doc = lh.fromstring(html)
    except Exception:
        return []
    out = []
    for a in doc.cssselect("ul li a, .news_box li a, .list li a")[:40]:
        title = re.sub(r"\s+", " ", a.text_content()).strip()
        href = a.get("href") or ""
        if not title or len(title) < 6:
            continue
        if not href.startswith("http"):
            href = "https://www.gov.cn" + href if href.startswith("/") else href
        out.append({"title": title[:120], "url": href, "_site": "govcn"})
    return out


def match_govcn(url: str) -> bool:
    return "gov.cn" in url and any(k in url for k in ("zhengce", "policy", "zuixin", "最新"))


# 停用（风控/JS渲染，交给通用浏览器流程）
# register("govcn", match_govcn, parse_govcn, desc="中国政府网：政策文件库/最新文件（SSR）")


# ---------- 什么值得买·好价（SSR） ----------
def parse_smzdm(html: str, url: str) -> List[Dict[str, Any]]:
    from lxml import html as lh
    try:
        doc = lh.fromstring(html)
    except Exception:
        return []
    out = []
    for it in doc.cssselect(".feed-item, .list_item")[:40]:
        a = it.cssselect(".feed-block-title a, .itemName a, h2 a")
        if not a:
            continue
        title = re.sub(r"\s+", " ", a[0].text_content()).strip()
        if not title:
            continue
        price = _css_text(it, ".z-highlight, .red, .price, .itemName strong")
        mall = _css_text(it, ".feed-block-merchant, .mall, .itemMall")
        out.append({"title": title[:120], "url": a[0].get("href") or "",
                    "price": price, "mall": mall, "_site": "smzdm"})
    return out


def match_smzdm(url: str) -> bool:
    return "smzdm.com" in url and any(k in url for k in ("jingxuan", "fenlei", "/hot", "/hao", "youhui"))


# 停用（风控/JS渲染，交给通用浏览器流程）
# register("smzdm", match_smzdm, parse_smzdm, desc="什么值得买：好价/精选（SSR）")


# ---------- 虎扑·步行街（SSR） ----------
def parse_hupu(html: str, url: str) -> List[Dict[str, Any]]:
    from lxml import html as lh
    try:
        doc = lh.fromstring(html)
    except Exception:
        return []
    out = []
    for it in doc.cssselect(".bbs-sl-web-post-body, .list-item, li[class*='thread']")[:40]:
        a = it.cssselect("a")
        if not a:
            continue
        title = re.sub(r"\s+", " ", a[0].text_content()).strip()
        if not title or len(title) < 4:
            continue
        href = a[0].get("href") or ""
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            href = "https://bbs.hupu.com" + href
        out.append({"title": title[:120], "url": href,
                    "author": _css_text(it, ".bbs-sl-web-post-author, .author"),
                    "replies": _css_text(it, ".bbs-sl-web-post-reply, .reply, .num"),
                    "_site": "hupu"})
    return out


def match_hupu(url: str) -> bool:
    return "bbs.hupu.com" in url


register("hupu", match_hupu, parse_hupu, desc="虎扑：步行街热帖（SSR）")


# ---------- 百度贴吧（SSR） ----------
def parse_tieba(html: str, url: str) -> List[Dict[str, Any]]:
    from lxml import html as lh
    try:
        doc = lh.fromstring(html)
    except Exception:
        return []
    out = []
    for li in doc.cssselect("li.j_thread_list")[:50]:
        a = li.cssselect(".j_th_tit")
        if not a:
            continue
        title = a[0].text_content().strip()
        if not title:
            continue
        out.append({"title": title[:120],
                    "url": "https://tieba.baidu.com" + (a[0].get("href") or ""),
                    "replies": _css_text(li, ".threadlist_rep_num"),
                    "time": _css_text(li, ".threadlist_reply_date, .pull-right"),
                    "_site": "tieba"})
    return out


def match_tieba(url: str) -> bool:
    return "tieba.baidu.com/f" in url


# 停用（风控/JS渲染，交给通用浏览器流程）
# register("tieba", match_tieba, parse_tieba, desc="百度贴吧：帖子列表（SSR）")


# ---------- CSDN 热门（SSR） ----------
def parse_csdn(html: str, url: str) -> List[Dict[str, Any]]:
    from lxml import html as lh
    try:
        doc = lh.fromstring(html)
    except Exception:
        return []
    out = []
    for it in doc.cssselect(".blog-list-box, .article-item-box, .list-item, li[class*='article']")[:40]:
        a = it.cssselect("a[href*='blog.csdn.net']") or it.cssselect(".title a") or it.cssselect("a")
        if not a:
            continue
        title = re.sub(r"\s+", " ", a[0].text_content()).strip()
        if not title or len(title) < 4:
            continue
        out.append({"title": title[:120], "url": a[0].get("href") or "",
                    "author": _css_text(it, ".nickname, .name, .author"),
                    "stats": _css_text(it, ".read-num, .view, .statistics")[:60],
                    "_site": "csdn"})
    return out


def match_csdn(url: str) -> bool:
    return "csdn.net" in url and any(k in url for k in ("nav", "hot", "list", "articles"))


register("csdn", match_csdn, parse_csdn, desc="CSDN：热门/分类文章（SSR）")


# ---------- 东方财富数据中心（通用 JSON API） ----------
_EM_RENAME = {
    "SECURITY_CODE": "代码", "SECURITY_NAME_ABBR": "名称", "SECURITY_NAME": "名称",
    "TRADE_DATE": "日期", "EXPLAIN": "上榜原因", "BILLBOARD_NET_AMT": "龙虎榜净买额",
    "BILLBOARD_BUY_AMT": "买入额", "BILLBOARD_SELL_AMT": "卖出额", "CLOSE_PRICE": "收盘价",
    "CHANGE_RATE": "涨跌幅", "TURNOVER_RATE": "换手率", "DEAL_AMOUNT_RATIO": "成交占比",
    "FUND_CODE": "基金代码", "FUND_NAME": "基金名称", "FUND_MANAGER": "基金经理",
    "REWARD": "近六月收益", "DATE": "日期", "CURRENT_PRICE": "现价", "PREMIUM_RATE": "转股溢价率",
    "PURE_BOND_VALUE": "纯债价值", "YIELD": "到期税后收益", "BOND_CODE": "转债代码",
    "BOND_NAME": "转债名称", "PROJECT_NAME": "项目名称", "STATUS": "状态",
    "ACCEPTANCE_DATE": "受理日期", "LATEST_STATUS": "最新状态", "ORG_NAME": "公司名称",
}


def parse_eastmoney(html: str, url: str) -> List[Dict[str, Any]]:
    out = []
    try:
        data = json.loads(html)
    except Exception:
        data = None
    if isinstance(data, dict):
        rows = (((data.get("result") or {}).get("data")) or [])
        for r in rows[:50]:
            row = {}
            for k, v in r.items():
                if v is None or v == "":
                    continue
                row[_EM_RENAME.get(k, k)] = str(v) if not isinstance(v, (int, float)) else v
            if row:
                out.append({**row, "_site": "eastmoney"})
        if out:
            return out
    # HTML 表格兜底（data.eastmoney.com 页面）
    from lxml import html as lh
    try:
        doc = lh.fromstring(html)
    except Exception:
        return []
    for tr in doc.cssselect("table tbody tr")[:50]:
        tds = [re.sub(r"\s+", " ", td.text_content()).strip() for td in tr.cssselect("td")]
        if len(tds) < 2:
            continue
        if not any(tds):
            continue
        out.append({"row": " | ".join(t for t in tds if t), "_site": "eastmoney"})
    return out


def match_eastmoney(url: str) -> bool:
    return "eastmoney.com" in url and ("api/data/v1/get" in url or "stock/lhb" in url or "fundranking" in url)


register("eastmoney", match_eastmoney, parse_eastmoney,
         desc="东方财富数据中心：龙虎榜/基金/转债等 JSON API")


# ---------- 天天基金排行（rankhandler JSONP） ----------
def parse_fundrank(html: str, url: str) -> List[Dict[str, Any]]:
    m = re.search(r"datas:\s*\[(.*?)\]\s*,\s*allRecords", html, re.S)
    if not m:
        m = re.search(r"datas:\s*\[(.*?)\]", html, re.S)
    if not m:
        return []
    names = ["代码", "名称", "拼音", "日期", "单位净值", "累计净值", "日增长率",
             "近1周", "近1月", "近3月", "近6月", "近1年", "近2年", "近3年", "近5年",
             "今年来", "成立来", "成立日期", "自定义"]
    out = []
    for raw in re.findall(r'"([^"]+)"', m.group(1)):
        parts = raw.split(",")
        row = {}
        for i, p in enumerate(parts):
            if i < len(names) and p:
                row[names[i]] = p
        if row.get("代码"):
            out.append({**row, "_site": "fundrank"})
        if len(out) >= 100:
            break
    return out


def match_fundrank(url: str) -> bool:
    return "fund.eastmoney.com/data/rankhandler" in url


register("fundrank", match_fundrank, parse_fundrank, desc="天天基金：基金排行（rankhandler JSONP）")


# ---------- 豆瓣同城活动（SSR） ----------
def parse_douban_events(html: str, url: str) -> List[Dict[str, Any]]:
    from lxml import html as lh
    try:
        doc = lh.fromstring(html)
    except Exception:
        return []
    out = []
    for li in doc.cssselect(".events-list li")[:50]:
        txt = re.sub(r"\s+", " ", li.text_content()).strip()
        if not txt:
            continue
        a = li.cssselect("a")
        link = a[0].get("href") if a else ""
        m_time = re.search(r"时间：\s*(.+?)\s+地点：", txt)
        m_place = re.search(r"地点：\s*(.+?)\s+费用：", txt)
        m_fee = re.search(r"费用：\s*(.+?)\s+发起", txt)
        m_want = re.search(r"(\d+)人感兴趣", txt)
        out.append({
            "title": txt.split("时间：")[0].strip()[:120],
            "url": link,
            "time": (m_time.group(1) if m_time else "")[:80],
            "place": (m_place.group(1) if m_place else "")[:80],
            "fee": (m_fee.group(1) if m_fee else ""),
            "interested": m_want.group(1) if m_want else "",
            "_site": "douban_events",
        })
    return out


def match_douban_events(url: str) -> bool:
    return "douban.com/location" in url and "events" in url


register("douban_events", match_douban_events, parse_douban_events, desc="豆瓣同城：活动列表（SSR）")


# ---------- 巨潮资讯公告查询（POST 接口，GET 必 500） ----------
def _cninfo_run(url: str, cookie: str = "", proxy: Optional[str] = None,
                limit: int = 20) -> List[Dict[str, Any]]:
    """巨潮公告查询：http://www.cninfo.com.cn/new/fulltextSearch/full?searchkey=<代码>
    用全文搜索接口（hisAnnouncement/query 的 stock 过滤实测不生效），返回目标证券公告。"""
    from urllib.parse import urlparse, parse_qs
    import time as _t
    q = parse_qs(urlparse(url).query)
    stock = (q.get("searchkey") or q.get("stock") or [""])[0].strip() or "600519"
    stock = stock.strip(",")
    page_size = min(int((q.get("pageSize") or ["30"])[0]), 50)
    pages = min(int((q.get("max_pages") or ["5"])[0]), 20)
    out = []
    try:
        from curl_cffi import requests as creq
        s = creq.Session(impersonate="chrome")
        for page in range(1, pages + 1):
            r = s.get("http://www.cninfo.com.cn/new/fulltextSearch/full",
                      params={"searchkey": stock, "sdate": "", "edate": "", "isfulltext": "false",
                              "sortName": "pubdate", "sortType": "desc",
                              "pageNum": str(page), "pageSize": str(page_size)},
                      headers={"Referer": "http://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice"},
                      timeout=20)
            try:
                j = r.json()
            except Exception:
                break
            anns = j.get("announcements") or []
            for a in anns:
                # 客户端按证券代码过滤（searchkey 可能匹配标题含代码的其它公告）
                if stock and (a.get("secCode") or "") not in (stock.split(",")):
                    continue
                title = (a.get("announcementTitle") or "").replace("<em>", "").replace("</em>", "")
                out.append({
                    "标题": title,
                    "公告类型": a.get("announcementType") or "",
                    "发布时间": _t.strftime("%Y-%m-%d %H:%M", _t.localtime((a.get("announcementTime") or 0) / 1000)) if a.get("announcementTime") else "",
                    "PDF链接": ("http://static.cninfo.com.cn/" + a.get("adjunctUrl")) if a.get("adjunctUrl") else "",
                    "证券代码": a.get("secCode") or "",
                    "证券名称": a.get("secName") or "",
                })
            if not anns:
                break
            if len(out) >= int(limit or 20):
                break
            import time
            time.sleep(0.5)
    except Exception as e:
        raise RuntimeError(f"巨潮公告抓取失败：{e}")
    if not out:
        raise RuntimeError("巨潮公告 0 条（接口可能已变更，或需要验证）")
    return out


def match_cninfo(url: str) -> bool:
    return "cninfo.com.cn/new/hisAnnouncement/query" in url or "cninfo.com.cn/new/fulltextSearch" in url


register("cninfo", match_cninfo, lambda html, url: [], run=_cninfo_run,
         desc="巨潮资讯：上市公司公告查询（POST 接口）")


# ===========================================================================
# 工信部 APP 侵害用户权益通报（miit.gov.cn）
# ---------------------------------------------------------------------------
# 列表页是 JS 渲染壳（HTTP 直抓只有 5KB 导航壳）；详情页正文只有「详见附件」，
# App 名单在详情页内嵌 PDF 阅读器 iframe 的 fileurl（PDF 直链）里。
# 精配流程：浏览器桥渲染列表 → 详情页取 PDF 直链 → 下载附件 → 按表格网格解析。
# ---------------------------------------------------------------------------
def match_miit(url: str) -> bool:
    u = (url or "").lower()
    return ("miit.gov.cn" in u) and (
        "appqhyhqyzxzzxd" in u or "jgsj/xgj" in u or "zwgk/zcwj/wjfb/tz" in u)


from pdfminer.high_level import extract_pages
from pdfminer.layout import LTTextContainer, LTTextLine, LTRect, LTLine, LTChar

FIELD_ALIASES = {
    "seq": ["序号", "序"],
    "name": ["应用名称", "产品名称"],
    "developer": ["应用开发者", "生产厂商", "开发者", "企业名称"],
    "source": ["应用来源", "样品来源", "来源"],
    "version": ["应用版本", "版本"],
    "issue": ["所涉问题", "问题项"],
}
FIELDS = ("seq", "name", "developer", "source", "version", "issue")

def _map_token(t):
    t = t.replace(" ", "").replace("\u3000", "")
    for f, aliases in FIELD_ALIASES.items():
        for a in aliases:
            if t == a or t.startswith(a) or a in t:
                return f
    return None

def _header_tokens(line):
    T = line.get_text()
    chars = [c for c in line if isinstance(c, LTChar) and c.get_text()]
    toks, cur_txt, cur_xs, ti = [], "", [], 0
    for c in chars:
        ct = c.get_text().replace("\u3000", " ")
        if ct.strip() == "":
            if cur_txt:
                toks.append((sum(cur_xs) / len(cur_xs), cur_txt))
                cur_txt, cur_xs = "", []
            ti += len(c.get_text())
            continue
        while ti < len(T) and T[ti] in (" ", "\u3000"):
            if cur_txt:
                toks.append((sum(cur_xs) / len(cur_xs), cur_txt))
                cur_txt, cur_xs = "", []
            ti += 1
        cur_txt += ct.strip()
        cur_xs.append(c.x0 + (c.width or 0) / 2)
        ti += len(c.get_text())
    if cur_txt:
        toks.append((sum(cur_xs) / len(cur_xs), cur_txt))
    merged = []
    for x, t in toks:
        if merged:
            px, pt = merged[-1]
            if x - px < 32 and (_map_token(pt + t) is not None or _map_token(pt) is None):
                merged[-1] = (px, pt + t)
                continue
        merged.append((x, t))
    return merged

def _grid_lines(page):
    h_segs, v_segs = {}, {}
    for el in page:
        if isinstance(el, (LTLine, LTRect)):
            x0, x1 = sorted((el.x0, el.x1)); y0, y1 = sorted((el.y0, el.y1))
        else:
            continue
        if abs(x1 - x0) > 0.5 and abs(y1 - y0) < 1.5:
            ky = round((y0 + y1) / 2)
            h_segs.setdefault(ky, []).append((x0, x1))
        elif abs(y1 - y0) > 0.5 and abs(x1 - x0) < 1.5:
            kx = round((x0 + x1) / 2)
            v_segs.setdefault(kx, []).append((y0, y1))
    h_full, v_lines = set(), set()
    for y, segs in h_segs.items():
        segs = sorted(segs)
        cur0, cur1 = segs[0]; cov = 0
        for a, b in segs[1:]:
            if a <= cur1 + 1: cur1 = max(cur1, b)
            else: cov += cur1 - cur0; cur0, cur1 = a, b
        cov += cur1 - cur0
        if cov > 300: h_full.add(float(y))
    for x, segs in v_segs.items():
        segs = sorted(segs)
        cur0, cur1 = segs[0]; cov = 0
        for a, b in segs[1:]:
            if a <= cur1 + 1: cur1 = max(cur1, b)
            else: cov += cur1 - cur0; cur0, cur1 = a, b
        cov += cur1 - cur0
        if cov > 150: v_lines.add(float(x))
    return sorted(h_full), sorted(v_lines)

def _header_fields(page, hs):
    hs_sorted = sorted(hs, reverse=True)
    if len(hs_sorted) < 2:
        return None
    top, second = hs_sorted[0], hs_sorted[1]
    cols = []
    for el in page:
        if not isinstance(el, LTTextContainer):
            continue
        for line in el:
            if not isinstance(line, LTTextLine):
                continue
            if line.y0 <= second or line.y0 > top:
                continue
            for x, t in _header_tokens(line):
                f = _map_token(t)
                if f:
                    cols.append((x, f))
    return cols or None

def _col_fields(vxs, header_fields):
    if not vxs or not header_fields:
        return None
    fields = [None] * (len(vxs) - 1)
    for x, f in header_fields:
        ci = bisect.bisect(vxs, x) - 1
        if 0 <= ci < len(fields):
            fields[ci] = f
    return fields

def _line_field_text(line, vxs, col_fields):
    """字符级分列：返回 {field: text}（T 对齐保留词间空格）。"""
    T = line.get_text()
    chars = [c for c in line if isinstance(c, LTChar) and c.get_text()]
    cells = {}
    ti = 0
    for c in chars:
        while ti < len(T) and T[ti] in (" ", "\u3000"):
            # 空格归到“下一个字符”的列（列间隙空格会在 strip 时去掉）
            ti += 1
            # 记录空格给当前列? 简化：先只记字符，最后统一处理
            pass
        ci = bisect.bisect(vxs, c.x0) - 1
        f = col_fields[ci] if 0 <= ci < len(col_fields) else None
        ct = c.get_text()
        if f in FIELDS:
            cells[f] = cells.get(f, "") + ct
        ti += len(ct)
    # 用 T 里的空格恢复词间空格：按列边界在字符流位置插入
    # （上面已按字符拼，这里再对每列做 T 对齐重建）
    return _restore_spaces(line, vxs, col_fields)

def _restore_spaces(line, vxs, col_fields):
    """更稳做法：一次遍历，按 T 的空格插入到当前列文本末尾。"""
    T = line.get_text()
    chars = [c for c in line if isinstance(c, LTChar) and c.get_text()]
    cells = {}
    ti = 0
    for c in chars:
        # 处理当前字符前的空格（T 对齐）
        while ti < len(T) and T[ti] in (" ", "\u3000"):
            # 空格属于哪个列？它后面的第一个可见字符所在列
            # 先记 pending，等下一个字符定列后加空格
            ti += 1
        ci = bisect.bisect(vxs, c.x0) - 1
        f = col_fields[ci] if 0 <= ci < len(col_fields) else None
        if f in FIELDS:
            cells[f] = cells.get(f, "") + c.get_text().replace("\u3000", " ")
        ti += len(c.get_text())
    # 重新用 T 对齐插入空格（简化：T 中空格对应的位置由 char 流定位）
    cells2 = {}
    ti = 0
    pending_space = False
    for c in chars:
        while ti < len(T) and T[ti] in (" ", "\u3000"):
            pending_space = True
            ti += 1
        ci = bisect.bisect(vxs, c.x0) - 1
        f = col_fields[ci] if 0 <= ci < len(col_fields) else None
        if f in FIELDS:
            if pending_space and cells2.get(f):
                if not cells2[f].endswith(" "):
                    cells2[f] += " "
            cells2[f] = cells2.get(f, "") + c.get_text().replace("\u3000", " ")
        pending_space = False
        ti += len(c.get_text())
    return {k: re.sub(r"\s+", " ", v).strip() for k, v in cells2.items() if v.strip()}

def _parse_miit_pdf(path):
    out = []
    col_fields = None
    for page in extract_pages(path):
        hs, vxs = _grid_lines(page)
        if not hs:
            continue
        hf = _header_fields(page, hs)
        has_header = bool(hf and len(hf) >= 2 and any(f != "seq" for f in hf))
        if has_header:
            cf = _col_fields(vxs, hf)
            if cf and sum(1 for f in cf if f) >= 2:
                col_fields = cf
        if not col_fields:
            continue
        top, bottom = max(hs), min(hs)
        hs_sorted = sorted(hs, reverse=True)
        hdr_min = hs_sorted[1] if (len(hs_sorted) >= 2 and has_header) else None
        entries = []  # (y0, x0, field, text)
        for el in page:
            if not isinstance(el, LTTextContainer):
                continue
            for line in el:
                if not isinstance(line, LTTextLine):
                    continue
                t = line.get_text().strip()
                if not t:
                    continue
                if re.match(r"^[-–—]\s*\d+\s*[-–—]$", t):
                    continue
                if line.y0 > top or line.y0 < bottom:
                    continue
                if hdr_min is not None and line.y0 > hdr_min:
                    continue
                cells = _restore_spaces(line, vxs, col_fields)
                for f, txt in cells.items():
                    if txt:
                        entries.append((line.y0, line.x0, f, txt))
        anchors = [(y0, int(txt)) for y0, x0, f, txt in entries
                   if f == "seq" and re.fullmatch(r"\d{1,3}", txt)]
        if not anchors:
            continue
        anchors.sort(key=lambda a: a[0], reverse=True)
        # 每行：按最近序号锚点聚合
        rows_by_y = {}
        for y0, x0, f, txt in entries:
            if f == "seq":
                continue
            ay = min(anchors, key=lambda a: abs(a[0] - y0))[0]
            row = rows_by_y.setdefault(ay, {n: "" for n in FIELDS})
            if row[f] and txt:
                prev, nxt = row[f][-1], txt[0]
                if _is_cjk(prev) and _is_cjk(nxt):
                    row[f] += txt
                else:
                    row[f] += (" " if (prev.isascii() and nxt.isascii()) else "") + txt
            else:
                row[f] += txt
        for ay, a_n in anchors:
            row = rows_by_y.get(ay)
            if row is not None:
                row["seq"] = str(a_n)
        out.extend(r for r in rows_by_y.values() if r.get("seq"))
    out.sort(key=lambda r: int(r["seq"]) if r["seq"].isdigit() else 9999)
    out = [r for r in out if r["seq"].isdigit()
           and r["name"] not in ("应用名称", "产品名称", "APP（SDK）名单")]
    return out

def _is_cjk(ch):
    o = ord(ch)
    return 0x4E00 <= o <= 0x9FFF or 0x3000 <= o <= 0x303F

def _parse_miit_xlsx(path) -> List[Dict[str, Any]]:
    """解析工信部 APP 通报附件 Excel（旧通报可能用 XLS/XLSX）。"""
    from openpyxl import load_workbook
    wb = load_workbook(path, data_only=True)
    ws = wb.active
    header = [str(c.value or "").strip() for c in ws[1]]

    def find(*names):
        for i, h in enumerate(header):
            if any(n in h for n in names):
                return i
        return -1

    idx = {
        "seq": find("序号"),
        "name": find("应用名称", "App名称", "APP名称", "名称"),
        "developer": find("应用开发者", "开发者", "企业名称", "运营者"),
        "source": find("应用来源", "来源"),
        "version": find("版本"),
        "issue": find("所涉问题", "问题", "涉及问题", "违规问题"),
    }
    idx = {k: v for k, v in idx.items() if v >= 0}
    if "name" not in idx or "issue" not in idx:
        return []
    rows = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        item = {}
        for k, i in idx.items():
            v = row[i] if 0 <= i < len(row) and row[i] is not None else ""
            item[k] = str(v).strip()
        if any(item.values()):
            rows.append(item)
    return rows


def _miit_run(url: str, cookie: str = "", proxy: Optional[str] = None,
              limit: int = 20) -> List[Dict[str, Any]]:
    """工信部 APP 通报：浏览器桥渲染列表/详情 → 取 PDF 直链 → 下载解析表格。
    limit<=20（默认值）时抓全栏目（约 24 篇），否则抓前 limit 篇。"""
    from pathlib import Path as _P
    from .browser import run_bridge, BrowserBridgeError
    # AI/用户给的入口可能选错栏目（如 /zwgk/zcwj/wjfb/tz/ 规划通知）：
    # 只要目标是工信部 APP 通报，一律归一化到「APP侵害用户权益专项整治行动-通知公告」专用栏目。
    if "appqhyhqyzxzzxd" not in (url or "").lower():
        url = "https://www.miit.gov.cn/jgsj/xgj/APPqhyhqyzxzzxd/tzgg/"
    bridge = _P(__file__).resolve().parent.parent / "scripts" / "miit_bridge.cjs"
    # run_site 默认 limit=20：视为「未指定」，抓全栏目（约 24 篇）；显式小值则按指定抓。
    _lim = int(limit or 20)
    max_articles = 100 if _lim in (0, 20) else min(_lim, 100)
    articles: List[Dict[str, Any]] = []
    try:
        for obj in run_bridge(bridge, {"list_url": url,
                                       "max_articles": str(max_articles),
                                       "settle": "1000", "deadlineMs": "420000"},
                              timeout=600):
            if obj.get("type") == "article":
                articles.append(obj)
    except BrowserBridgeError as e:
        raise RuntimeError(f"工信部浏览器桥失败：{e}")
    if not articles:
        raise RuntimeError("工信部列表/详情 0 条（可能被 WAF 拦截或栏目结构变化）")
    # 下载附件并解析
    import requests as _req
    out_dir = _P("outputs/miit_attachments")
    out_dir.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": UA,
               "Referer": "https://www.miit.gov.cn/"}
    rows: List[Dict[str, Any]] = []
    for a in articles:
        pdf_url = (a.get("pdfUrl") or "").strip()
        report_url = (a.get("href") or "").strip()
        title = (a.get("title") or "").strip()
        pub_date = (a.get("date") or "").strip()
        if not pdf_url:
            rows.append({"report_title": title, "publish_date": pub_date,
                         "app_name": "", "developer": "", "source": "",
                         "version": "", "issue": "", "report_url": report_url,
                         "pdf_url": "", "_error": "详情页无附件（可能未挂 PDF）"})
            continue
        if pdf_url.startswith("/"):
            pdf_url = "https://www.miit.gov.cn" + pdf_url
        fname = re.sub(r"[^0-9A-Za-z._-]+", "_", pdf_url.rsplit("/", 1)[-1]) or "attach.bin"
        fp = out_dir / fname
        try:
            r = _req.get(pdf_url, headers=headers, timeout=40, verify=False,
                         proxies={"http": proxy, "https": proxy} if proxy else None)
            r.raise_for_status()
            fp.write_bytes(r.content)
            if fp.suffix.lower() in (".xls", ".xlsx"):
                parsed = _parse_miit_xlsx(fp)
            else:
                parsed = _parse_miit_pdf(fp)
            if not parsed:
                rows.append({"report_title": title, "publish_date": pub_date,
                             "app_name": "", "developer": "", "source": "",
                             "version": "", "issue": "", "report_url": report_url,
                             "pdf_url": pdf_url, "_error": "附件解析 0 行（格式可能变化）"})
                continue
            for item in parsed:
                rows.append({"report_title": title, "publish_date": pub_date,
                             "app_name": item.get("name", ""),
                             "developer": item.get("developer", ""),
                             "source": item.get("source", ""),
                             "version": item.get("version", ""),
                             "issue": item.get("issue", ""),
                             "report_url": report_url, "pdf_url": pdf_url})
        except Exception as e:
            rows.append({"report_title": title, "publish_date": pub_date,
                         "app_name": "", "developer": "", "source": "",
                         "version": "", "issue": "", "report_url": report_url,
                         "pdf_url": pdf_url, "_error": f"附件下载/解析失败: {type(e).__name__}: {e}"})
    if not rows:
        raise RuntimeError("工信部附件解析 0 条")
    return rows


match_miit.__doc__ = "工信部：APP 侵害用户权益通报（JS 列表 + 详情 PDF 附件表格）"
register("miit", match_miit, lambda html, url: [], run=_miit_run,
         desc="工信部：APP 侵害用户权益通报（JS 列表 + 详情 PDF 附件表格）")


# ===========================================================================
# 配置型精配（一键自动精配生成器产物）
# 生成器把方案保存为 tasks/auto_precise_<host>/config.json + configs/auto_precise_<host>.json，
# 这里负责：运行时注册 + 启动扫描持久化注册。match 按主域名命中。
# ===========================================================================
_AUTO_PRECISE: Dict[str, Dict[str, Any]] = {}


def _match_host(host: str):
    def _m(url: str) -> bool:
        u = (url or "").lower()
        return host in u
    return _m


def _register_image_precise(meta: Dict[str, Any]):
    """注册图片榜单精配：meta 含 host/entry/extra_pages/img_src_hint。"""
    host = (meta.get("host") or "").lower().strip()
    if not host:
        return
    name = f"auto_precise_{re.sub(r'[^0-9A-Za-z_.-]', '_', host).strip('_')}"

    def _run(url, cookie="", proxy=None, limit=20):
        from .precise_auto import _image_ranking_run
        m = dict(meta)
        m.setdefault("name", name)
        return _image_ranking_run(m, url or meta.get("entry", ""), limit=int(limit or 20))

    register(name, _match_host(host), lambda html, url: [], run=_run,
             desc=f"自动精配·图片榜单[{host}]（下载图片+OCR）")
    _AUTO_PRECISE[host] = {"name": name, "kind": "image_ranking", "task_dir": str(meta.get("entry", ""))}


def _register_tactic_precise(meta: Dict[str, Any]):
    """注册战术型精配：meta 含 host/tactic/params（entry/seed/item_css/url_template/fields/img_src_hint）。

    html_engine 不是 run 型战术（它应由 _register_engine_config 注册成 engine 任务包）。
    历史遗留的 tactic:html_engine 配置属于毒配置：忽略并删除，避免 run_site 误报“未支持的战术”。
    """
    host = (meta.get("host") or "").lower().strip()
    tactic = meta.get("tactic") or "html_engine"
    if not host:
        return
    if tactic not in ("cookie_click", "pdf_attach", "image_ocr"):
        try:
            _base = re.sub(r"[^0-9A-Za-z_.-]", "_", host).strip("_")
            _cfg = Path(__file__).resolve().parent.parent / "configs" / f"auto_precise_{_base}.json"
            _cfg.unlink(missing_ok=True)
        except Exception:
            pass
        return
    name = f"auto_precise_{re.sub(r'[^0-9A-Za-z_.-]', '_', host).strip('_')}"

    def _run(url, cookie="", proxy=None, limit=20):
        from .tactics import (cookie_click_run, pdf_attach_run, image_ocr_run)
        params = dict(meta.get("params") or {})
        params.setdefault("host", host)
        params.setdefault("entry", meta.get("entry") or "")
        _lim = int(limit or 0)
        if tactic == "cookie_click":
            return cookie_click_run(params, url or params.get("entry", ""), limit=_lim)
        if tactic == "pdf_attach":
            return pdf_attach_run(params, url or params.get("entry", ""), limit=_lim)
        if tactic == "image_ocr":
            return image_ocr_run(params, url or params.get("entry", ""), limit=_lim)
        raise RuntimeError(f"未支持的战术: {tactic}")

    register(name, _match_host(host), lambda html, url: [], run=_run,
             desc=f"自动精配·战术[{tactic}][{host}]")
    _AUTO_PRECISE[host] = {"name": name, "kind": f"tactic:{tactic}", "task_dir": ""}


def register_config_precise(host: str, task_dir: Any, kind: str = "engine"):
    """注册配置型精配（engine=通用引擎任务包；image_ranking=图片榜单OCR运行器）。"""
    host = (host or "").lower().strip()
    if not host:
        return
    name = f"auto_precise_{re.sub(r'[^0-9A-Za-z_.-]', '_', host).strip('_')}"

    def _run(url, cookie="", proxy=None, limit=20):
        from pathlib import Path
        if kind == "image_ranking":
            from .precise_auto import _image_ranking_run
            meta = {"host": host, "kind": "image_ranking", "name": name,
                    "entry": str(task_dir), "img_src_hint": ""}
            return _image_ranking_run(meta, url, limit=int(limit or 20))
        from .engine_v3 import run_task
        td = Path(task_dir)
        res = run_task(td, limit=int(limit or 20))
        out_name = (res or {}).get("name") or td.name
        fp = Path("outputs") / f"{out_name}.json"
        if fp.exists():
            try:
                rows = json.loads(fp.read_text(encoding="utf-8"))
            except Exception:
                rows = []
        else:
            rows = []
        return rows if isinstance(rows, list) else []

    register(name, _match_host(host), lambda html, url: [], run=_run,
             desc=f"自动精配[{host}]（kind={kind}）")
    _AUTO_PRECISE[host] = {"name": name, "kind": kind, "task_dir": str(task_dir)}


def _load_auto_precise():
    """启动时扫描 configs/auto_precise_*.json 重新注册（重启不丢）。"""
    try:
        from pathlib import Path
        cfg_dir = Path(__file__).resolve().parent.parent / "configs"
        for meta_file in sorted(cfg_dir.glob("auto_precise_*.json")):
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
                host = meta.get("host") or ""
                kind = meta.get("kind") or "engine"
                if kind == "image_ranking":
                    _register_image_precise(meta)
                elif str(kind).startswith("tactic:"):
                    _register_tactic_precise(meta)
                else:
                    task_dir = meta.get("task_dir") or ""
                    if host and task_dir:
                        register_config_precise(host, task_dir, kind=kind)
            except Exception:
                continue
    except Exception:
        pass


_load_auto_precise()


# ===========================================================================
# 阳光高考「招生章程」（gaokao.chsi.com.cn）
# ---------------------------------------------------------------------------
# 全站阿里云 WAF（HTTP/headless 直连 412），必须先访问首页种 Cookie；
# 列表是 Vue SPA，学校行 .sch-item 点击 → window.open(listZszc--schId-<orgId>.dhtml)。
# 桥：种 Cookie → 列表 → 逐个点击捕获 schId → 输出学校（名称/schId/省市/主管部门/标签）。
# ---------------------------------------------------------------------------
def match_zszc(url: str) -> bool:
    # 放宽到整个阳光高考域：AI 常给 /zsjz/、首页等无效入口，_zszc_run 会自动归一化到本科招生章程列表页
    u = (url or "").lower()
    return "gaokao.chsi.com.cn" in u


def _zszc_clean(text: str) -> str:
    return re.sub(r"[\ue000-\uf8ff]", "", text or "").replace("\u200b", "").strip()


def _zszc_run(url: str, cookie: str = "", proxy: Optional[str] = None,
              limit: int = 200) -> List[Dict[str, Any]]:
    """阳光高考招生章程：浏览器桥 → 学校列表（含 schId/双一流标签）→ 生成章程链接。"""
    from pathlib import Path as _P
    from .browser import run_bridge, BrowserBridgeError
    # AI/用户入口可能给 /zsjz/（404）等无效路径：归一化到本科招生章程列表页
    if "listVerifedZszc" not in (url or "") and "listZszc" not in (url or ""):
        url = "https://gaokao.chsi.com.cn/zsgs/zhangcheng/listVerifedZszc--method-index,lb-1.dhtml"
    bridge = _P(__file__).resolve().parent.parent / "scripts" / "zszc_bridge.cjs"
    max_n = 200 if int(limit or 200) <= 0 else min(int(limit or 200), 500)
    schools: List[Dict[str, Any]] = []
    try:
        for obj in run_bridge(bridge, {"list_url": url, "max": str(max_n), "settle": "800"},
                              timeout=600):
            if obj.get("type") == "school":
                schools.append(obj)
    except BrowserBridgeError as e:
        raise RuntimeError(f"阳光高考浏览器桥失败：{e}")
    if not schools:
        raise RuntimeError("阳光高考列表 0 条（可能被 WAF 拦截或页面结构变化）")
    rows = []
    for s in schools:
        name = _zszc_clean(s.get("name") or "")
        if not name:
            continue
        sch_id = str(s.get("schId") or "").strip()
        tags = [_zszc_clean(t) for t in (s.get("tags") or [])]
        rows.append({
            "学校名称": name,
            "省市": _zszc_clean(s.get("province") or ""),
            "主管部门": _zszc_clean(s.get("dept") or ""),
            "层次标签": "、".join(tags),
            "是否双一流": "是" if any("双一流" in t for t in tags) else "否",
            "schId": sch_id,
            "章程链接": f"https://gaokao.chsi.com.cn/zsgs/zhangcheng/listZszc--schId-{sch_id}.dhtml" if sch_id else "",
        })
    return rows


register("zszc", match_zszc, lambda html, url: [], run=_zszc_run,
         desc="阳光高考：招生章程学校列表（WAF+Vue，点击捕获 schId）")


# ===========================================================================
# 全国标准信息公共服务平台（std.samr.gov.cn）
# ---------------------------------------------------------------------------
# 搜索页 /search/std 是 JS 壳，真实接口是 GET /search/stdPage?q=<关键词>&tid=
# 结果行 .post：标准号(.en-code)/名称(a 文本)/状态(.s-status)/tid+pid 属性；
# 详情页 /gb/search/gbDetailed?id=<pid>&tid=<tid> 含实施日期。
# ---------------------------------------------------------------------------
def match_std(url: str) -> bool:
    return "std.samr.gov.cn" in (url or "").lower()


def _std_run(url: str, cookie: str = "", proxy: Optional[str] = None,
             limit: int = 20) -> List[Dict[str, Any]]:
    import requests as _req
    from urllib.parse import urlparse, parse_qs, quote
    from lxml import html as _LH
    q = parse_qs(urlparse(url).query).get("q", [""])[0].strip() or "电动自行车"
    if "stdPage" not in (url or ""):
        url = f"https://std.samr.gov.cn/search/stdPage?q={quote(q)}&tid="
    headers = {"User-Agent": UA, "Referer": f"https://std.samr.gov.cn/search/std?q={quote(q)}"}
    r = _req.get(url, headers=headers, timeout=20, verify=False,
                 proxies={"http": proxy, "https": proxy} if proxy else None)
    r.raise_for_status()
    doc = _LH.fromstring(r.text)
    rows: List[Dict[str, Any]] = []
    for post in doc.cssselect(".post"):
        a = post.cssselect("a[tid][pid]")
        if not a:
            continue
        a = a[0]
        text = (a.text_content() or "").replace("\u200b", "").strip()
        m_no = re.search(r"((?:GB|DB)[/T ]*[\d.]+[-—]?\d*)", text)
        # 任务要求“国家标准”：只保留 GB 开头的（过滤地方标准 DB）
        if not (m_no and m_no.group(1).startswith("GB")):
            continue
        name = re.sub(r"^(?:GB|DB)[/T ]*[\d.]+[-—]?\d*\s*", "", text).strip()
        status_el = post.cssselect(".s-status")
        status = status_el[0].text_content().strip() if status_el else ""
        tid = a.get("tid") or ""
        pid = a.get("pid") or ""
        rows.append({
            "标准号": m_no.group(1) if m_no else "",
            "标准名称": name[:120],
            "标准状态": status,
            "tid": tid,
            "pid": pid,
            "详情链接": f"https://std.samr.gov.cn/gb/search/gbDetailed?id={pid}&tid={tid}" if pid else "",
        })
    # 详情补抓实施日期
    _max = 20 if int(limit or 0) <= 20 else int(limit or 20)
    for row in rows[:_max]:
        if not row.get("pid"):
            continue
        try:
            dr = _req.get(row["详情链接"], headers=headers, timeout=20, verify=False)
            dtxt = re.sub(r"<[^>]+>", " ", dr.text or "")
            m = re.search(r"实施日期[：:\s]*([0-9]{4}[-/年][0-9]{1,2}[-/月][0-9]{1,2})", dtxt)
            row["实施日期"] = m.group(1).replace("/", "-").replace("年", "-").replace("月", "-") if m else ""
            m2 = re.search(r"发布日期[：:\s]*([0-9]{4}[-/年][0-9]{1,2}[-/月][0-9]{1,2})", dtxt)
            row["发布日期"] = m2.group(1).replace("/", "-").replace("年", "-").replace("月", "-") if m2 else ""
        except Exception:
            row["实施日期"] = ""
    if not rows:
        raise RuntimeError("标准搜索 0 条（接口可能变更）")
    return rows


register("std", match_std, lambda html, url: [], run=_std_run,
         desc="全国标准信息公共服务平台：标准搜索（stdPage 接口 + 详情实施日期）")


# ---------------------------------------------------------------------------
# 人社部《国家职业资格目录（2021年版）》：公告页/PDF 附件直链 → 解析表格
# 公告页: https://www.gov.cn/zhengce/zhengceku/2021-12/03/content_5655553.htm
# 附件直链: .../5655553/files/86876724c6ee4cdcb3d0be524aee036f.pdf
# 表格结构: 专业技术人员（59项：序号|职业资格名称[|具体项目]|实施部门|资格类别|设定依据）
#           技能人员（13项：同上 + 备注列）
# ---------------------------------------------------------------------------
def match_zige(url: str) -> bool:
    u = (url or "").lower()
    return ("content_5655553" in u or "86876724c6ee4cdcb3d0be524aee036f" in u
            or ("gov.cn" in u and "职业资格目录" in u))


def _parse_zige_pdf(path: str) -> List[Dict[str, Any]]:
    """pdfplumber 抽取国家职业资格目录 PDF 表格，自动归一化列宽/跨行/子行继承。"""
    import pdfplumber
    out: List[Dict[str, Any]] = []
    section = None          # None / "专业技术人员" / "技能人员"
    last_seq = ""
    ctx = {}                # 当前组的名称/部门/类别/依据/备注（供子行继承）
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                if not table:
                    continue
                for raw in table:
                    joined = "|".join((c or "") for c in raw)
                    # 技能人员小节表头（带备注列）
                    if "技能人员" in joined and "职业资格名称" in joined:
                        section = "技能人员"
                        ctx = {}
                        continue
                    # 表头行（专业技术人员）
                    if "职业资格名称" in joined and "实施部门" in joined:
                        section = "专业技术人员" if "备注" not in joined else "技能人员"
                        ctx = {}
                        continue
                    if section is None:
                        continue
                    cells = [(c or "").strip() for c in raw]
                    if not any(cells):
                        continue
                    if section == "专业技术人员":
                        # 6列=[序号,名称,具体项目,部门,类别,依据]；5列=[序号,名称,部门,类别,依据]
                        if len(cells) >= 6:
                            seq, name, sub, dept, cat, basis = (cells[0], cells[1], cells[2],
                                                                cells[3], cells[4], "".join(cells[5:]))
                        elif len(cells) == 5:
                            seq, name, sub, dept, cat, basis = cells[0], cells[1], "", cells[2], cells[3], cells[4]
                        else:
                            continue
                        remark = ""
                    else:
                        # 7列=[序号,组名,具体项目,部门,类别,依据,备注]；6列歧义：index4含"类"→无备注，否则→无具体项目
                        if len(cells) >= 7:
                            seq, name, sub, dept, cat, basis, remark = (cells[0], cells[1], cells[2],
                                                                        cells[3], cells[4], cells[5], "".join(cells[6:]))
                        elif len(cells) == 6:
                            if "类" in cells[4]:
                                seq, name, sub, dept, cat, basis = cells[0], cells[1], cells[2], cells[3], cells[4], cells[5]
                                remark = ""
                            else:
                                seq, name, sub, dept, cat, basis, remark = (cells[0], cells[1], "", cells[2],
                                                                            cells[3], cells[4], cells[5])
                        elif len(cells) == 5:
                            seq, name, sub, dept, cat, basis = cells[0], cells[1], "", cells[2], cells[3], cells[4]
                            remark = ""
                        else:
                            continue
                    # 单元格内换行归一
                    name = name.replace("\n", "")
                    sub = sub.replace("\n", "")
                    cat = re.sub(r"\s+", "", cat)
                    dept = re.sub(r"\s*\n\s*", "、", dept).strip("、")
                    # 设定依据：换行后若下一行以《开头视为新法规加"；"，否则是续行直接拼接
                    _bp = basis.split("\n")
                    basis = _bp[0] + "".join(
                        ("；" if p.strip().startswith("《") else "") + p for p in _bp[1:])
                    basis = re.sub(r"[；]+", "；", basis).strip("；")
                    remark = remark.replace("\n", "").strip("；")
                    is_new = bool(re.fullmatch(r"\d{1,3}", seq))
                    if is_new:
                        last_seq = seq
                        ctx = {"name": name, "dept": dept, "cat": cat, "basis": basis, "remark": remark}
                    elif ctx:
                        # 子行继承组信息（单元格合并后为空）
                        if not name:
                            name = ctx.get("name", "")
                        if not dept:
                            dept = ctx.get("dept", "")
                        if not cat:
                            cat = ctx.get("cat", "")
                        if not basis:
                            basis = ctx.get("basis", "")
                        if not remark:
                            remark = ctx.get("remark", "")
                    if not name and not sub:
                        continue
                    # 子行：组名 + 具体项目（避免名称与项目重复）
                    item_name = name or sub
                    item_sub = sub if sub and sub != item_name else ""
                    out.append({
                        "序号": last_seq,
                        "职业资格名称": item_name,
                        "具体职业/工种": item_sub,
                        "实施部门（单位）": dept or ctx.get("dept", ""),
                        "资格类别": cat or ctx.get("cat", ""),
                        "设定依据": basis or ctx.get("basis", ""),
                        "备注": remark or ctx.get("remark", ""),
                        "所属部分": section,
                    })
    # 去重
    seen = set()
    dedup = []
    for r in out:
        key = (r["序号"], r["职业资格名称"], r["具体职业/工种"], r["实施部门（单位）"], r["资格类别"])
        if key in seen:
            continue
        seen.add(key)
        dedup.append(r)
    return dedup


def _zige_run(url: str, cookie: str = "", proxy: Optional[str] = None,
              limit: int = 0) -> List[Dict[str, Any]]:
    """公告页/PDF 直链 → 下载附件 → 精细解析表格（复用通用下载/找附件）。"""
    from .pdf_table import download_pdf, extract_pdf_links, is_pdf_url
    pdf_url = url if is_pdf_url(url) else None
    if pdf_url is None:
        import requests as _req
        headers = {"User-Agent": UA}
        r = _req.get(url, headers=headers, timeout=40, verify=False,
                     proxies={"http": proxy, "https": proxy} if proxy else None)
        r.raise_for_status()
        pdfs = extract_pdf_links(r.text or "", url)
        if not pdfs:
            raise RuntimeError("公告页未找到 PDF 附件链接（页面结构可能变更）")
        pdf_url = pdfs[0]
    tmp = download_pdf(pdf_url, proxy=proxy, timeout=60)
    try:
        return _parse_zige_pdf(str(tmp))
    finally:
        try:
            import os
            os.unlink(str(tmp))
        except Exception:
            pass


register("zige", match_zige, lambda html, url: [], run=_zige_run,
         desc="人社部《国家职业资格目录（2021年版）》：公告页/PDF附件 → 72 项职业资格表格")


# ---------------------------------------------------------------------------
# 地震（USGS 中国区域 API）：最近24小时地震 + 震级分布图
# 官方源 news.ceic.ac.cn/ajax/google 常被阿里云盾 invisible bucket 拦截，
# 备用源 earthquake.usgs.gov 免费可靠、字段齐全（时间/震级/地点/深度/经纬度）。
# ---------------------------------------------------------------------------
def match_eq(url: str) -> bool:
    return "earthquake.usgs.gov/fdsnws/event/1/query" in (url or "")


def _eq_run(url: str, cookie: str = "", proxy: Optional[str] = None,
            limit: int = 0) -> List[Dict[str, Any]]:
    import requests as _req
    from datetime import datetime, timedelta, timezone
    headers = {"User-Agent": UA}
    r = _req.get(url, headers=headers, timeout=40, verify=False,
                 proxies={"http": proxy, "https": proxy} if proxy else None)
    r.raise_for_status()
    data = r.json()
    feats = data.get("features") or []
    if not feats:
        raise RuntimeError("USGS 返回 0 条地震（调整时间/区域参数再试）")
    bj = timezone(timedelta(hours=8))
    rows: List[Dict[str, Any]] = []
    for f in feats:
        p = f.get("properties") or {}
        g = f.get("geometry") or {}
        coords = g.get("coordinates") or [None, None, None]
        ts = p.get("time")
        tstr = ""
        if ts:
            tstr = datetime.fromtimestamp(ts / 1000, tz=bj).strftime("%Y-%m-%d %H:%M:%S")
        rows.append({
            "时间（北京时间）": tstr,
            "震级": p.get("mag", ""),
            "地点": p.get("place", ""),
            "震源深度（km）": coords[2] if len(coords) > 2 else "",
            "经度": coords[0] if len(coords) > 0 else "",
            "纬度": coords[1] if len(coords) > 1 else "",
            "详情链接": p.get("url", ""),
            "震源类型": p.get("type", ""),
        })
    # 绘制震级-经纬度分布图
    try:
        _draw_eq_map(rows, proxy=proxy)
    except Exception:
        pass
    return rows


def _draw_eq_map(rows: List[Dict[str, Any]], proxy: Optional[str] = None) -> None:
    """中国区域地震分布散点图（震级=点大小，颜色=深度）。"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    import os as _os
    import numpy as np
    # 中文字体（macOS 系统字体）
    for _fp in ("/System/Library/Fonts/PingFang.ttc",
                "/System/Library/Fonts/STHeiti Light.ttc",
                "/System/Library/Fonts/Hiragino Sans GB.ttc",
                "/System/Library/Fonts/Songti.ttc"):
        if _os.path.exists(_fp):
            try:
                font_manager.fontManager.addfont(_fp)
                plt.rcParams["font.family"] = font_manager.FontProperties(fname=_fp).get_name()
                break
            except Exception:
                continue
    plt.rcParams["axes.unicode_minus"] = False
    pts = [(r.get("经度"), r.get("纬度"), r.get("震级"), r.get("震源深度（km）"), r.get("时间（北京时间）"), r.get("地点"))
           for r in rows]
    pts = [p for p in pts if isinstance(p[0], (int, float)) and isinstance(p[1], (int, float))]
    if not pts:
        return
    lon = np.array([p[0] for p in pts], dtype=float)
    lat = np.array([p[1] for p in pts], dtype=float)
    mag = np.array([float(p[2]) if isinstance(p[2], (int, float, str)) and str(p[2]).replace(".", "", 1).isdigit() else 2.0 for p in pts])
    dep = np.array([float(p[3]) if isinstance(p[3], (int, float, str)) and str(p[3]).replace(".", "", 1).isdigit() else 10.0 for p in pts])
    fig, ax = plt.subplots(figsize=(10, 8), dpi=130)
    sc = ax.scatter(lon, lat, s=(mag * 18) ** 1.6, c=dep, cmap="YlOrRd",
                    alpha=0.75, edgecolors="k", linewidths=0.4)
    # 中国主要边界简化框（经纬度范围）
    ax.set_xlim(73, 135)
    ax.set_ylim(18, 54)
    ax.set_xlabel("经度"); ax.set_ylabel("纬度")
    ax.set_title(f"中国及周边地震分布（最近24小时，{len(pts)} 条）", fontsize=14)
    ax.grid(True, linestyle="--", alpha=0.3)
    fig.colorbar(sc, ax=ax, label="震源深度 (km)")
    # 标注 ≥5 级
    for p, m, t, place in zip(pts, mag, [x[4] for x in pts], [x[5] for x in pts]):
        if m >= 5:
            ax.annotate(f"M{m} {str(t)[11:16]}", (p[0], p[1]), fontsize=9,
                        xytext=(6, 6), textcoords="offset points")
    from pathlib import Path
    out_dir = Path("outputs"); out_dir.mkdir(exist_ok=True)
    fp = out_dir / "earthquake_distribution.png"
    fig.tight_layout()
    fig.savefig(fp)
    plt.close(fig)


register("eq", match_eq, lambda html, url: [], run=_eq_run,
         desc="地震（USGS 中国区域 API）：最近24小时地震（北京时间/震级/地点/深度）+ 分布图")


# ---------------------------------------------------------------------------
# 税务总局政策法规库（fgk.chinatax.gov.cn / 官网站内搜索接口）
# 搜索接口: POST https://www.chinatax.gov.cn/search5/search/s
#   siteCode=bm29000002&searchWord=关键词&column=政策法规&uc=1
# 返回 searchResultAll.searchTotal：title/pubDate/url/column/pubName/govDoc 等
# 注意：官方旧列表页(2021)已停更，法规库新平台在 fgk.chinatax.gov.cn
# ---------------------------------------------------------------------------
def match_tax(url: str) -> bool:
    u = (url or "").lower()
    return ("chinatax.gov.cn/search5/search" in u or "fgk.chinatax.gov.cn" in u)


def _tax_run(url: str, cookie: str = "", proxy: Optional[str] = None,
             limit: int = 0) -> List[Dict[str, Any]]:
    from urllib.parse import urlparse, parse_qs
    import requests as _req
    q = parse_qs(urlparse(url).query)
    word = (q.get("searchWord") or [""])[0].strip() or "增值税"
    column = (q.get("column") or [""])[0].strip() or "政策法规"
    strict = (q.get("strict") or ["1"])[0] != "0"  # 默认标题必须含关键词
    form = {
        "siteCode": "bm29000002",
        "searchWord": word,
        "column": column,
        "uc": 1,
        "left_right_index": "",
    }
    r = _req.post("https://www.chinatax.gov.cn/search5/search/s", data=form,
                  timeout=40, verify=False,
                  headers={"User-Agent": UA,
                           "Referer": "https://www.chinatax.gov.cn/"},
                  proxies={"http": proxy, "https": proxy} if proxy else None)
    r.raise_for_status()
    data = r.json()
    st = ((data.get("searchResultAll") or {}).get("searchTotal")) or []
    rows: List[Dict[str, Any]] = []
    for it in st:
        title = re.sub(r"<[^>]+>", "", it.get("title") or "").strip()
        if strict and word and word not in title:
            continue
        doc = it.get("govDoc") or {}
        rows.append({
            "标题": title,
            "链接": it.get("url", ""),
            "发布日期": (it.get("pubDate") or "")[:10],
            "栏目": it.get("column", ""),
            "发布单位": it.get("pubName", ""),
            "文号": (doc.get("docNo") or doc.get("docNum") or ""),
            "效力级别": it.get("xxgk_effectLevel", ""),
            "制定年份": it.get("xxgk_formulatedYear", ""),
            "内容摘要": re.sub(r"<[^>]+>", "", it.get("abstracts") or it.get("shortContent") or "")[:200],
        })
    if not rows:
        raise RuntimeError(f"税务总局搜索 0 条（关键词「{word}」/栏目「{column}」无结果或接口变更）")
    return rows


register("tax", match_tax, lambda html, url: [], run=_tax_run,
         desc="税务总局政策法规库（搜索接口）：最新增值税等税收政策（标题/文号/效力级别/日期）")


# ---------------------------------------------------------------------------
# 四川省教育考试院（sceea.cn）：首页通知列表（标题/链接/日期）
# 之前 LLM 抽取只给标题+日期、丢了链接（半成品），改为直接 HTML 解析补全链接
# ---------------------------------------------------------------------------
def match_sceea(url: str) -> bool:
    return "sceea.cn" in (url or "").lower()


def parse_sceea(html: str, url: str) -> List[Dict[str, Any]]:
    if not html or not html.strip():
        return []
    from lxml import html as _LH
    from urllib.parse import urlparse
    doc = _LH.fromstring(html or "")
    base = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
    rows: List[Dict[str, Any]] = []
    for li in doc.cssselect("#newsListDynamic li, ul.news-list li"):
        a = li.cssselect("span.title a")
        if not a:
            continue
        a = a[0]
        title = (a.get("title") or a.text_content() or "").strip()
        href = (a.get("href") or "").strip()
        if not title or not href:
            continue
        if href.startswith("/"):
            href = base + href
        elif not href.startswith("http"):
            href = base + "/" + href
        d = li.cssselect("span.date")
        rows.append({"标题": title, "链接": href,
                     "日期": (d[0].text_content().strip() if d else "")})
    return rows


register("sceea", match_sceea, parse_sceea,
         desc="四川省教育考试院：首页通知列表（标题/链接/日期）")


# ---------------------------------------------------------------------------
# 国家医保局（nhsa.gov.cn）政策法规栏目 col104：
# 列表在页面 <datastore> 的 <record> CDATA 里（服务端渲染），解析 li 四个 span
# （索引/标题+链接/发文字号/发布日期）。之前误抓导航 li 导致 28 行空壳。
# ---------------------------------------------------------------------------
def match_nhsa(url: str) -> bool:
    return "nhsa.gov.cn/col/col104" in (url or "").lower()


def _nhsa_fetch(url: str, cookie: str = "", proxy: Optional[str] = None):
    """nhsa 专用 fetch：requests + smart_decode（页面编码易误判，curl_cffi 会卡）。"""
    import requests as _req
    import warnings as _w
    _w.filterwarnings("ignore")
    from .core import smart_decode
    hdrs = {"User-Agent": UA}
    if cookie:
        hdrs["Cookie"] = cookie
    r = _req.get(url, timeout=20, verify=False, headers=hdrs,
                 proxies={"http": proxy, "https": proxy} if proxy else None)
    r.raise_for_status()
    html = smart_decode(r.content, dict(r.headers))
    return {"ok": True, "status": r.status_code, "html": html,
            "final_url": r.url, "headers": dict(r.headers)}


def parse_nhsa(html: str, url: str) -> List[Dict[str, Any]]:
    import re as _re
    from urllib.parse import urlparse
    base = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
    m = _re.search(r"<datastore>(.*?)</datastore>", html or "", _re.S)
    if not m:
        return []
    store = m.group(1)
    recs = _re.findall(r"<record><!\[CDATA\[(.*?)\]\]></record>", store, _re.S)
    rows: List[Dict[str, Any]] = []
    for rec in recs:
        # 索引
        im = _re.search(r"<span[^>]*>([^<]{2,40})</span>", rec)
        # 标题+链接
        am = _re.search(r'<a href="([^"]+)"[^>]*title="([^"]+)"', rec)
        if not am:
            am = _re.search(r'<a href="([^"]+)"[^>]*>([^<]{4,80})</a>', rec)
        # 文号 + 日期
        spans = _re.findall(r"<span[^>]*>([^<]{1,60})</span>", rec)
        title = am.group(2).strip() if am else ""
        href = am.group(1).strip() if am else ""
        if href.startswith("/"):
            href = base + href
        elif href and not href.startswith("http"):
            href = base + "/" + href
        wh = ""
        dt = ""
        for sp in spans:
            s = sp.strip()
            if _re.search(r"〔|\]号|号$", s):
                wh = s
            elif _re.fullmatch(r"20\d{2}-\d{1,2}-\d{1,2}", s):
                dt = s
        rows.append({"索引": (im.group(1).strip() if im else ""),
                     "标题": title, "链接": href,
                     "发文字号": wh, "发布日期": dt})
    return [r for r in rows if r["标题"]]


register("nhsa", match_nhsa, parse_nhsa, fetch=_nhsa_fetch,
         desc="国家医保局：政策法规栏目（col104 datastore 45条，标题/文号/日期）")
