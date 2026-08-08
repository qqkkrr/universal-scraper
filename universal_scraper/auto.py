#!/usr/bin/env python3
"""🤖 自动层：一句话任务 → AI 自动生成配置 → 自动运行 → 失败自修复。

原理：
  1. 用大模型（默认千问 Qwen）把用户的中文任务转成 v3 任务包 config.json
  2. 引擎自动运行
  3. 若 0 条或报错：把日志喂回大模型，自动修正配置再跑（最多 rounds 轮）

用法:
  CLI:  python3 -m universal_scraper.cli auto "抓取 https://quotes.toscrape.com 的名言、作者和标签，翻 2 页"
  API:  POST /api/auto  {"description": "..."}
"""
from __future__ import annotations
import os

import hashlib
import json
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent

SYSTEM_PROMPT = """你是万能爬虫工具的"任务配置生成器"。根据用户的中文任务，输出**一个完整的 v3 任务包 config.json**（严格 JSON，不要任何解释、不要 markdown 代码块）。

v3 任务包 config.json 结构（字段含义）：
{
  "name": "任务名(英文小写)",
  "intent": {   // 必填：AI 对任务意图的自检，供用户确认时一眼看出入口对不对
    "target_type": "资源类型，如 mod/软件/文档/商品/新闻/帖子/评论/视频...",
    "entry_rationale": "为什么这个入口对应目标类型（如：MOD发布区 forum-53 是 mod 资源列表页）",
    "expected_fields": ["标题", "作者", "发布时间", "下载链接"]
  },
  "vars": {"可选的变量": "值"},
  "start_urls": ["入口网址(必须 http/https)"],
  "queue": {"max_depth": 3, "max_requests": 100, "max_concurrency": 2},
  "source": {
    "type": "http",              // http=普通请求；browser=浏览器渲染(JS页面/需要登录后DOM)
    "query": {"参数": "值"},       // 可选固定 URL 参数
    "headers": {"可选": "自定义请求头"},
    "actions": [{"type":"click","selector":"#more"}]   // 仅 browser：点击/输入/滚动等
  },
  "rules": [{"match":"contains","pattern":"/list","parser":"list"}],
  "parsers": {
    "list": {
      "type": "html",            // html=网页；json=JSON接口；json_paged=JSON分页；table=表格；article=正文
      "row_css": "列表行选择器",   // html 列表必填
      "fields": {"字段名": {"css":"选择器"} 或 {"from":"JSON路径"}},
      "extract_links": {"allow":"下一页/详情链接正则", "same_domain":true}   // 需要翻页时必填
    }
  },
  "pipelines": [
    {"type":"filter","field":"字段","op":"non_empty"},
    {"type":"filter","field":"时间戳字段","op":"between","min":<unix秒>,"max":<unix秒>},  // 用户提到日期范围时用
    {"type":"dedup","key":"主键"}
  ],
  "detail": {                       // 列表页缺字段时必配（如发布日期/正文/价格在详情页）
    "enabled": true,
    "url_field": "link",            // 列表记录里详情 URL 的字段名
    "url_transform": [{"prefix": "https://www.zhipin.com"}],  // 相对路径补全域名（必须写对）
    "extract": [{"name": "publish_time", "type": "css_text", "selector": ".job-banner .time::text"}],
    "filters": [                    // 详情合并后再过滤（如按发布日期）
      {"type": "parse_date", "field": "publish_time", "out": "ts"},
      {"type": "filter", "field": "ts", "op": "between", "min": <unix秒>, "max": <unix秒>}
    ],
    "concurrency": 2,
    "interval": 0.5
  },
  "storage": {"type":"jsonl","name":"任务名"},
  "output": {"dir":"outputs","base_name":"任务名"},
  "anti_bot": {"min_interval":0.5,"max_retries":2}
}

规则：
- **意图自检（最高优先级，必须遵守）**：先判断用户要的实体类型（资源/帖子/商品/文档/新闻/评论等）。**资源类任务（mod/软件/插件/模板/文档/视频/素材）必须进入该类型专属的资源/下载/发布列表页**，禁止用全站最新发表/全站搜索当入口（会把求助帖/闲聊当结果）。入口 URL 未知时，从已知站点结构推断（论坛常有「MOD发布区」「资源下载区」），并把判断写进 intent.entry_rationale 供用户确认；不确定就 intent.entry_unknown=true。
- **intent 必填**：每个配置都要输出 intent，用户确认计划时靠它判断入口对不对。
- 日期范围：把用户说的日期转成 **北京时间** 的 Unix 秒，用 between 过滤（min=当天 00:00 CST，max=次日 00:00 CST，左闭右开）。
- **日期硬规则（必须遵守）**：当前真实日期以任务消息里【当前真实日期】为准（每次都会注入，含今天/明天的 Unix 秒，**直接抄用，禁止用示例里的旧日期**）。用户说"今天/昨天/前天/今天8点/N天前"等时，一律按注入的真实日期换算成北京时间 Unix 秒（between 用当天 00:00 ~ 次日 00:00，左闭右开）。
- 翻页：HTML 用 extract_links，allow 建议写**锚定路径正则**（如 "^/page/\\d+/$"），引擎会按 URL 路径匹配，防止误吃 /tag/xxx/page/1/ 这类同构 URL；JSON 分页用 type=json_paged（records_path/strategy=page_param/page_param/page_size/max_pages/fields）。
- 选择器要根据网站常见结构推断（.item/.list/table tr 等），宁可宽一点；字段 CSS 可直接写 ".text" 或 ".text::text"，两种都支持。
- **字段提取规范（必须遵守）**：标题/名称优先选行内第一个带语义的节点（span/heading/首个链接），**禁止**直接用裸 `a::text` 或裸 `a::attr(href)`——列表行常有多个链接（详情+附件），裸 a 会把它们拼成 "d\nf"、"/a /b"。取链接时用首个详情链接：`a:first-of-type::text`、`a[href*='/detail']::text`；href 用 `.u::attr(href)` 或 `a:first-of-type::attr(href)`。只有用户明确要"所有链接"时才用 multiple。
- **列表缺字段用 detail（必须遵守）**：如果用户要的字段（发布日期/时间/正文/价格/评分等）在列表页没有、只在详情页有（如招聘/电商/资讯站的发布时间、商品详情），必须配 `detail`：`url_field` 用列表记录里存详情 URL 的字段（如 link），`url_transform.prefix` 把相对路径补成完整域名，`extract` 用 css_text/css_attr 抓详情页字段，`filters` 做合并后过滤（日期类先用 parse_date 转 unix 秒再 between）。引擎会自动逐个抓详情页并合并回列表记录。
- **论坛类站点（Discuz 等，如 bbs.mountblade.com.cn）硬知识（必须遵守）**：
  "最新发表/最新回复" guide 列表页（forum.php?mod=guide）通常**没有板块列**——只有 标题/作者/回复数/查看数/最后回复时间，**不要把板块名配成列表字段**（td.common 在 guide 页不存在，会得到空字段）。
  正确做法：板块名从详情页面包屑取倒数第二个链接 `div#pt a:nth-last-of-type(2)::text`（Discuz 面包屑是 首页>板块>帖子标题，last-of-type 是标题，别抓错；字段名用 forum_name 或 forum 均可）。
  发帖时间（精确）：
  - 列表页发帖日期（发帖者列，作者链接含 uid=）：`xpath:.//td[contains(@class,'by') and cite/a[contains(@href,'uid')]]//em//span/@title`（如 2026-8-8；**不要用** `td.by em span[title]`，那会同时抓到"最后回复时间"）
  - 详情页发帖时间（精确到秒）：`em[id^='authorposton']::text`（如「发表于 2025-8-22 09:47:13」/「发表于 昨天 22:47」，parse_date 都能转）；详情页易触发验证码，所以列表日期留作兜底。
  **Discuz 排序硬知识**：板块列表默认按【最后回复】排序；`orderby=dateline`/`filter=dateline` 参数在很多站无效（实测 mountblade 无效）。用户要"今天新发布"时，正确做法：抓列表（按默认排序翻前几页）→ 每条进详情页取发帖时间 em[id^='authorposton'] → detail.filters 用 parse_date + between 只保留发帖时间在今天的；列表页发帖日期（uid 列）作为详情页失败时的兜底。
- 需要登录的网站（大众点评/小红书/微博/淘宝/京东/知乎等）：source.type 用 browser，并加 "headless": false（弹出真实浏览器供人工登录）与 "login":{"enabled":true,"url":"入口页","wait_selector":"登录成功后页面上才会出现的元素选择器（如 .user-info、.avatar、用户名节点）"}。工具会在首次运行时弹出浏览器让用户登录一次，自动保存登录态，之后自动复用。
-- **高频站点入口速查表（必须优先按此给 start_urls；禁止凭空猜不存在的路径）**：
  - 淘宝搜索: https://s.taobao.com/search?q=<关键词>（JS 页面，browser 模式；或已登录 CDP 精配）
  - 京东搜索: https://search.jd.com/Search?keyword=<关键词>（browser+登录）
  - 拼多多: https://mobile.yangkeduo.com/ads.html（百亿补贴频道，browser+登录）
  - 1688搜索: https://s.1688.com/selloffer/offer_search.htm?keywords=<关键词>
  - 闲鱼: https://www.goofish.com/（browser+登录）
  - 得物: https://www.dewu.com/（browser+登录）
  - 网易考拉: https://www.kaola.com/（browser+登录，跨境海淘；注意别用 www.163.com 门户）
  - 什么值得买好价: https://www.smzdm.com/jingxuan/（JS 挑战，browser）
  - 链家二手房: https://sh.lianjia.com/ershoufang/pudong/（城市拼音+ershoufang+区域拼音）
  - 贝壳: https://sz.ke.com/zufang/（城市拼音+zufang 整租）
  - 安居客: https://chengdu.anjuke.com/loupan/（城市拼音+loupan 新盘）
  - 自如: https://www.ziroom.com/z/nl/z1.html（整租一居）
  - 58二手房: https://hz.58.com/ershoufang/（城市拼音+ershoufang）
  - BOSS直聘: https://www.zhipin.com/web/geek/job?query=<关键词>&city=100010000（browser+登录）
  - 拉勾: https://www.lagou.com/wn/zhaopin?kd=<关键词>（browser）
  - 猎聘: https://www.liepin.com/zhaopin/?key=<关键词>
  - 智联: https://www.zhaopin.com/sou/jl<城市编号>?kw=<关键词>
  - 微博热搜: https://s.weibo.com/top/summary（browser+登录）
  - 知乎搜索: https://www.zhihu.com/search?type=content&q=<关键词>（browser+登录）
  - 小红书: https://www.xiaohongshu.com/search_result?keyword=<关键词>（browser+登录）
  - 抖音: https://www.douyin.com/search/<关键词>（browser+登录）
  - B站搜索: https://search.bilibili.com/all?keyword=<关键词>（SSR 可 http）
  - 虎扑: https://bbs.hupu.com/（SSR 可 http）
  - 百度贴吧: https://tieba.baidu.com/f?kw=<吧名>（403 风控，browser）
  - CSDN热门: https://www.csdn.net/nav/ai（SSR 可 http）
  - 东方财富龙虎榜: https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_DAILYBILLBOARD_DETAILSNEW&columns=ALL&pageNumber=1&pageSize=50&sortColumns=TRADE_DATE,SECURITY_CODE&sortTypes=-1,-1（公开 JSON）
  - 天天基金排行: https://fund.eastmoney.com/data/rankhandler.aspx?op=ph&dt=kf&ft=all&sc=6yzf&st=desc&pi=1&pn=50&dx=1（公开 JSONP）
  - 集思录: https://www.jisilu.cn/data/cbnew/（browser）
  - 上交所科创板项目动态: https://kcb.sse.com.cn/renewal/（browser；注意上交所=sse.com.cn，不是上海交大 sjtu）
  - 巨潮公告: http://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice（browser）
  - 携程机票: https://flights.ctrip.com/online/list/oneway-changsha-chongqing?depdate=<日期>（browser）
  - 大众点评: https://www.dianping.com/search/keyword/<城市ID>/0_<关键词>（browser+登录）
  - 美团: https://www.meituan.com/s/<关键词>/（browser+登录）
  - 马蜂窝: https://www.mafengwo.cn/travel-scenic-spot/mafengwo/<id>.html（browser）
  - 12306: https://kyfw.12306.cn/otn/leftTicket/init（browser+登录）
  - 途家: https://www.tujia.com/（browser）
  - 知网: https://kns.cnki.net/kns8s/defaultresult/index?kw=<关键词>（browser+登录）
  - GitHub Trending: https://github.com/trending（SSR 可 http）
  - GitHub 搜索: https://api.github.com/search/repositories?q=<关键词>&sort=stars（公开 JSON）
  - LeetCode 题库: https://leetcode.cn/api/problems/all/（公开 JSON）
  - arXiv: http://export.arxiv.org/api/query?search_query=cat:cs.CL&sortBy=submittedDate&sortOrder=descending（公开 Atom）
  - 中国天气网: https://www.weather.com.cn/weather1d/<城市编码>.shtml（SSR 可 http，武汉=101200101）
  - 猫眼票房: https://piaofang.maoyan.com/dashboard（browser）
  - 网易云音乐: https://music.163.com/#/discover/toplist（browser）
  - QQ音乐: https://y.qq.com/n/ryqq/toplist（browser）
  - TapTap: https://www.taptap.cn/top/board（browser）
  - 汽车之家口碑: https://k.autohome.com.cn/<车系ID>/（browser）
  - 懂车帝: https://www.dongchedi.com/sales（browser）
  - 瓜子二手车: https://www.guazi.com/tesla/（browser）
  - 企查查: https://www.qcc.com/（browser+登录）
  - 国家统计局: https://www.stats.gov.cn/sj/zxfb/（browser）
  - 中国政府网政策: https://www.gov.cn/zhengce/zuixin/（JS 加载，browser）
  - 中国政府采购网: https://www.ccgp.gov.cn/cggg/zygg/（browser）
  - 豆瓣同城: https://www.douban.com/location/beijing/events/（SSR 可 http）
  - 丁香园用药: https://www.drugs.dxy.cn/（browser）
  - 爱企查: https://aiqicha.baidu.com/（browser+登录）
  - Google Play: https://play.google.com/store/apps/category/GAME/collection/topselling_free（browser）
  - 币安公告: https://www.binance.com/zh-CN/support/announcement（browser）
  - 中国裁判文书网: https://wenshu.court.gov.cn/（browser+登录）
  - 中国执行信息: http://zxgk.court.gov.cn/（browser）
  - 中国专利: https://epub.cnipa.gov.cn/（browser）
  - 七麦数据: https://www.qimai.cn/rank（browser+登录）
  - 国家药监局: https://www.nmpa.gov.cn/（browser）
  - PyPI: https://pypi.org/project/<包名>/（公开 JSON 详情；趋势可 https://pypistats.org/top）
  - Stack Overflow: https://stackoverflow.com/questions/tagged/python?sort=votes（browser）
  - NPM: https://www.npmjs.com/browse/depended（browser）
  - Docker Hub: https://hub.docker.com/_/python（browser）
  - Wappalyzer: https://www.wappalyzer.com/technologies/（browser）
  - 虎课网: https://www.huke88.com/（browser）
  - 强登录站（得物/闲鱼/抖音/快手/小红书/微博/知乎/淘宝/京东/拼多多）：一律 browser + login/verify，注明需要人工登录一次。

- **京东 URL 硬知识（必须遵守）**：京东店铺页真实格式是 https://mall.jd.com/index-<店铺数字ID>.html；商品页是 https://item.jd.com/<sku数字ID>.html；**绝对不要**把店铺名猜成 "<店铺名>sp.jd.com/list.html"（那是假地址，会 404）。用户只给店铺名没给链接时，start_urls 可以先用京东搜索或直接用已知商品链接，并在任务说明里注明"需先找到店铺/商品真实 URL"。京东搜索页(www.jd.com)、商品页、评论接口(club.jd.com)全都被强风控：公开 HTTP 接口已失效，必须 source.type=browser + 真实扫码登录。京东登录硬校验："login":{"enabled":true,"url":"https://www.jd.com/","wait_selector":".nickname","require_cookie":"pt_key|pt_pin"}——工具会检查登录 cookie 是否真的出现，没有 pt_key/pt_pin 就不会放行，避免"假登录通过"。
- 验证码/整页验证（大众点评/美团等会跳到验证中心）：source.type=browser 并加 "headless": false 与
  "verify":{"enabled":true,"markers":["verify.meituan.com","验证中心","spiderindefence","安全验证","滑动验证","访问过于频繁"],
  "success_selector":"<列表行选择器，如 div[data-shop-id]>","max_wait_ms":600000}；
  工具会弹出真实浏览器，用户手动过滑块/点选后自动继续并保存会话，之后复用。
- 大众点评/美团这类：过完滑块**还会强制登录**（扫码/账号）。所以 verify 和 login 两个都要配：
  "verify":{...上面...} + "login":{"enabled":true,"url":"https://www.dianping.com/","wait_selector":"#J-userinfo a, .user-info a, a[href*='/member/'], .nav-user"}；
  用户在弹窗里：先过滑块，再扫码/账号登录；工具自动继续并保存会话。
- **大众点评列表页如被风控需要住宅代理**：只有用户明确提供了真实代理时才在 anti_bot.proxy 里填写；**不要写 "host:port" 之类的示例占位符**（会导致请求报错）。用户没给代理就不配置 proxy。
- 图片验证码：anti_bot 加 "captcha":{"strategy":"auto"}（自动识别，失败则人工兜底）。
- **CDP 直连真实浏览器（强风控站的王炸）**：如果用户说"已登录/用我自己的浏览器"，source.type 用 browser 并加 "cdp":"http://127.0.0.1:9222"（用户需先开 Chrome：退出 Chrome 后执行 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome --remote-debugging-port=9222 并登录目标站）。CDP 模式附着真实浏览器=真实指纹+真实登录态，京东/知乎/微博/小红书/抖音这类站最稳；不要配 login（用户浏览器已登录）。
- Cloudflare/Turnstile 挑战：工具会自动尝试点击"我不是机器人"复选框并等待 cf_clearance，仍失败才转人工；无需额外配置。
- **强风控 SPA（小红书/抖音/知乎/微博等，数据全靠加密接口）**：source.type 用 browser + 登录/CDP，并加 "record_from":"capture_all" 与 "capture_all":true——工具会**自动捕获页面上所有 JSON 接口响应**（不用预先知道接口名），任务结束后记录在 {_api_url, data}，再由 LLM/解析器挑字段。比硬逆向签名省事得多。
- 如果用户已提供登录 Cookie：source.type 用 http，source.headers 加 "Cookie": "<用户提供的Cookie>"，
  并加 rules/parsers 解析 SSR 页面（如大众点评搜索页 .shop-list li），不要用 browser（Cookie 直抓更快更稳）。
- **全国公共资源交易平台（ggzy.gov.cn）URL 硬知识（必须遵守）**：旧搜索页 searchtj.jsp 已下线（404）。正确入口是历史交易列表页，并在 URL query 里带参数：`https://www.ggzy.gov.cn/history/dealList.html?keyword=<关键词>&begin=YYYY-MM-DD&end=YYYY-MM-DD&stages=0001,0002`（0001=招标公告/交易公告，0002=中标公告/成交公示；可只写一个）。start_urls 直接写这个带参数的 URL；source.type 用 browser（站点 WAF 对非浏览器返回 404）。工具命中 ggzy 精配后会自己驱动真实浏览器搜索。
- **入口网址必须真实（硬性规则）**：只能写你知道真实存在的网址；不知道官方域名时**不要编造**（常见错误：把期刊/公司官网猜成 www.xxx.org.cn，DNS 解析直接失败）。工具会自动校验域名：无法解析的域名会被丢弃并自动搜索官方域名替换。
- **CWAP/WZWS 滑块 WAF**（部分期刊/政务/学校站会 302 到 waf_slider_verify.html）：不需要你手写特殊配置，工具检测到 WAF 拦截会自动升级为 browser + 人工滑块模式（会弹真实浏览器）。
- 只输出 JSON 对象本身。"""


def _extract_json(raw: str) -> dict:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise ValueError("AI 未返回 JSON: " + raw[:200])
    return json.loads(m.group(0))


def _llm_chat(messages: List[Dict[str, str]], timeout: int = 200) -> str:
    """LLM 调用带硬超时 + 空响应重试（模型偶尔返回空/纯空白，提高温度再补一次）。"""
    from .llm import LLMClient
    import time as _t

    def _call(temp: float) -> str:
        box: Dict[str, Any] = {}

        def _t0():
            try:
                box["r"] = LLMClient().chat(messages, temperature=temp)
            except Exception as e:
                box["e"] = e

        th = threading.Thread(target=_t0, daemon=True)
        th.start()
        th.join(timeout)
        if th.is_alive():
            raise RuntimeError(f"LLM 响应超时（>{timeout}s），请检查模型接口/网络/Key")
        if "e" in box:
            raise box["e"]
        return box["r"]

    out = _call(0.2)
    if out is None or not str(out).strip():
        _t.sleep(2)
        out = _call(0.7)   # 空响应：提高温度重试一次
    return str(out or "")


def _validate_and_fix(cfg: dict, description: str = "", log=None) -> dict:
    """规范化 AI 输出：把不认识的写法映射为引擎支持的写法，再校验。"""
    from .config import validate_task
    cfg.setdefault("name", "auto_task")
    cfg.setdefault("start_urls", [])
    # LLM 可能一次性生成几十个入口：只保留前 10 个（防队列爆炸）
    if isinstance(cfg.get("start_urls"), list) and len(cfg["start_urls"]) > 10:
        cfg["start_urls"] = cfg["start_urls"][:10]
    cfg.setdefault("queue", {"max_depth": 3, "max_requests": 100, "max_concurrency": 2})
    cfg.setdefault("source", {"type": "http"})
    cfg.setdefault("storage", {"type": "jsonl", "name": cfg["name"]})
    cfg.setdefault("output", {"dir": "outputs", "base_name": cfg["name"]})
    cfg.setdefault("anti_bot", {"min_interval": 0.5, "max_retries": 2})
    cfg.setdefault("pipelines", [])
    if not cfg.get("rules"):
        cfg["rules"] = [{"match": "contains", "pattern": "/", "parser": "default"}]
    # 浏览器任务默认滚动（懒加载站不滚动=0条）：未显式配置时给 4 次
    if (cfg.get("source") or {}).get("type") == "browser":
        _src = cfg["source"]
        _src.setdefault("scroll_count", 4)
        _src.setdefault("scroll_wait_ms", 800)
    cfg.setdefault("parsers", {})
    # source.type 规范化
    st = cfg["source"].get("type", "http")
    if st not in ("http", "browser", "bridge"):
        cfg["source"]["type"] = "http"
    # storage.type 规范化
    st2 = cfg["storage"].get("type", "jsonl")
    if st2 not in ("jsonl", "csv", "sqlite", "multi"):
        cfg["storage"]["type"] = "jsonl"
    # 规则规范化：match 只支持 regex/contains/startswith
    for r in cfg["rules"]:
        if r.get("match") not in ("regex", "contains", "startswith"):
            r["match"] = "contains"
        if not r.get("pattern"):
            r["pattern"] = "/"
        if not r.get("parser"):
            r["parser"] = "default"
    # 分页/列表 allow 自动锚定：非锚定的 page 类正则改为 ^/...$（按路径匹配），
    # 避免 /page/\\d+/ 把 /tag/xxx/page/1/ 这类同构 URL 全部入队导致队列爆炸
    for pcfg in cfg.get("parsers", {}).values():
        if not isinstance(pcfg, dict):
            continue
        lk = pcfg.get("extract_links") or {}
        allow = lk.get("allow")
        if isinstance(allow, str) and re.search(r"(page|p=)", allow, re.I) \
                and re.search(r"\\d|\[0-9\]", allow):
            allow = allow.strip()
            if not allow.startswith("^"):
                # 非锚定 → 锚定为路径模式（^/...），防止 /tag/xxx/page/1/ 同构 URL 入队
                if allow.startswith("/"):
                    lk["allow"] = f"^{allow.rstrip('/')}/?$"
                else:
                    lk["allow"] = f"^/{allow.lstrip('/')}"
            elif not allow.startswith(("^/", "^http", "^https", r"^\w+://")):
                # 已锚定但缺路径开头 /（如 ^catalogue/page-\\d+.html$ → ^/catalogue/...）
                lk["allow"] = "^/" + allow[1:]
            pcfg["extract_links"] = lk
    # 默认 parser
    cfg["parsers"].setdefault("default", {"type": "html", "fields": {}})
    # records_path 规范化：JSONPath 的 $ 根 / $.a.b → 引擎支持的 a.b / 空（顶层数组自动识别）
    for pcfg in cfg["parsers"].values():
        if not isinstance(pcfg, dict):
            continue
        rp = pcfg.get("records_path")
        if isinstance(rp, str):
            rp = rp.strip()
            if rp in ("$", "$[]", "$."):
                pcfg["records_path"] = ""
            elif rp.startswith("$."):
                pcfg["records_path"] = rp[2:]
            elif rp.startswith("$["):
                pcfg["records_path"] = ""
    # start_urls 过滤非 http(s)
    cfg["start_urls"] = [u for u in cfg.get("start_urls", []) if str(u).startswith(("http://", "https://"))]
    if not cfg["start_urls"]:
        raise ValueError("AI 未给出有效入口网址")

    # 域名校验 + 死域名搜索替换（AI 猜错域名时自动救回，如 ciejournal.org.cn → ciejournal.ajcass.com）
    try:
        cfg = _fix_dead_domains(cfg, description, log)
    except Exception:
        pass

    # source.actions 规范化：AI 偶发把 action.type 写错/写空，直接丢弃非法动作，避免 ConfigError 卡死整单
    _actions = (cfg.get("source") or {}).get("actions")
    if isinstance(_actions, list) and _actions:
        _valid_actions = {"click", "type", "fill", "press", "select", "wait", "wait_time",
                          "wait_for_selector", "waitfor", "scroll", "exec", "js", "execute_javascript",
                          "screenshot", "noop"}
        _kept = []
        for _a in _actions:
            if not isinstance(_a, dict):
                continue
            _t = str(_a.get("type") or _a.get("action") or "").lower()
            if _t not in _valid_actions:
                continue
            if _t in ("wait", "wait_time") and not _a.get("ms") and not _a.get("milliseconds"):
                _a["ms"] = 1000
            if _t == "click" and not _a.get("selector") and not _a.get("xpath"):
                continue
            _kept.append(_a)
        cfg.setdefault("source", {})["actions"] = _kept

    # 管道过滤字段校验：AI 常把过滤字段写成解析器里不存在的名字（如 published/时间），
    # 不存在的字段过滤会把整单滤成 0 条——直接丢弃这类过滤（ts 是 detail 合并后的日期字段，保留）
    _known = set()
    for _p in (cfg.get("parsers") or {}).values():
        if isinstance(_p, dict):
            _known.update((_p.get("fields") or {}).keys())
    _det = cfg.get("detail") or {}
    for _ex in (_det.get("extract") or []):
        if isinstance(_ex, dict) and _ex.get("name"):
            _known.add(_ex["name"])
    _kept_pl = []
    for _pl in (cfg.get("pipelines") or []):
        if not isinstance(_pl, dict):
            continue
        if _pl.get("type") == "filter" and _pl.get("field") and _pl.get("field") not in _known \
                and _pl.get("field") != "ts":
            continue  # 字段不存在 → 丢弃
        if _pl.get("type") == "parse_date" and _pl.get("field") and _pl.get("field") not in _known \
                and _pl.get("field") != "ts":
            continue
        _kept_pl.append(_pl)
    cfg["pipelines"] = _kept_pl
    if _det.get("filters"):
        _kept_df = []
        for _pl in _det.get("filters") or []:
            if not isinstance(_pl, dict):
                continue
            if _pl.get("type") == "filter" and _pl.get("field") and _pl.get("field") not in _known \
                    and _pl.get("field") != "ts":
                continue
            if _pl.get("type") == "parse_date" and _pl.get("field") and _pl.get("field") not in _known \
                    and _pl.get("field") != "ts":
                continue
            _kept_df.append(_pl)
        _det["filters"] = _kept_df

    # 变量缺失检测：AI 常留 {company_name} 等占位符却没给 vars 值。
    # 收集所有未赋值变量 → cfg["_needs_input"]，plan 时给用户醒目提示，避免空转
    _need = []
    _vars = cfg.get("vars") or {}
    _scan_txt = " ".join(str(x) for x in (cfg.get("start_urls") or [])) + " " + json.dumps((cfg.get("source") or {}).get("query") or {}, ensure_ascii=False)
    for _m in re.finditer(r"\{\{?(\w+)\}?\}", _scan_txt):
        _name = _m.group(1)
        if _name and _name not in _vars and _name not in _need:
            _need.append(_name)
    if _need:
        cfg["_needs_input"] = _need
        _int = cfg.setdefault("intent", {})
        _int["entry_unknown"] = True

    _su0 = (cfg.get("start_urls") or [""])[0]
    # 已知「HTML 壳页 → 公开 API」改写：这些页面本体是 JS 壳，静态抓 0 条，
    # 直接换成可直抓的 JSON/JSONP 接口（比让 AI 猜更稳）
    _API_REWRITE = {
        "fund.eastmoney.com/data/fundranking.html":
            "https://fund.eastmoney.com/data/rankhandler.aspx?op=ph&dt=kf&ft=all&sc=6yzf&st=desc&pi=1&pn=50&dx=1",
        "pypi.org/trending":
            "https://pypistats.org/top",
        "api.fund.eastmoney.com/FundRank/GetFundRankList":
            "https://fund.eastmoney.com/data/rankhandler.aspx?op=ph&dt=kf&ft=all&sc=6yzf&st=desc&pi=1&pn=50&dx=1",
        "leetcode.cn/api/problems/lcof2/": "https://leetcode.cn/api/problems/all/",
        "leetcode.cn/api/problems/lcof/": "https://leetcode.cn/api/problems/all/",
        "fgj.hangzhou.gov.cn/col/col1229066785": "https://fgj.hangzhou.gov.cn/col/col1229404539/index.html",
    }
    for _k, _v in _API_REWRITE.items():
        if _k in _su0:
            cfg["start_urls"] = [_v]
            cfg["source"] = {"type": "http"}
            _su0 = _v
            break

    # 上交所 vs 上海交大：AI 常混淆（sjtu.cn 是上海交大）。任务含"上交所/科创板"就强制换入口
    # 注意 kcb.sse.com.cn 域名在本网络 NXDOMAIN，用 www.sse.com.cn/listing/renewal/ipo/
    if re.search(r"上交所|科创板", description or "") and ("sjtu" in _su0 or "kcb.sse.com.cn" in _su0):
        cfg["start_urls"] = ["https://www.sse.com.cn/listing/renewal/ipo/"]
        _su0 = cfg["start_urls"][0]
        cfg["source"] = {"type": "browser", "headless": False, "scroll_count": 4, "scroll_wait_ms": 800}
        _src = cfg["source"]

    # 已知强登录/反爬站点：AI 若配成 http 直抓，自动升为 browser+登录（否则必 0 条）
    _login_domains = ("xiaohongshu.com", "zhihu.com", "weibo.com", "douyin.com", "kuaishou.com",
                      "taobao.com", "tmall.com", "jd.com", "yangkeduo.com", "pinduoduo.com",
                      "dianping.com", "meituan.com", "goofish.com", "dewu.com", "zhipin.com",
                      "qcc.com", "tianyancha.com", "aiqicha.baidu.com", "qimai.cn",
                      "wenshu.court.gov.cn", "zxgk.court.gov.cn", "kns.cnki.net", "bilibili.com")
    _src = cfg.get("source") or {}
    _st = (_src.get("type") or "http").lower()
    _host = (_su0.split("//")[-1].split("/")[0] if "//" in _su0 else _su0).lower()

    # 已知强反爬站：即使 AI 配了 browser 但没配 verify/login，也自动补 verify
    # （否则浏览器打开看到验证页/登录墙就直接 0 条，用户也不知道要人工操作）
    _ANTIBOT_VERIFY = {
        "58.com": ["58.com", "antibot", "安全验证", "验证码", "拖动"],
        "ke.com": ["captcha", "验证", "安全认证"],
        "zu.ke.com": ["captcha", "验证", "安全认证"],
        "anjuke.com": ["antibot", "验证码", "安全验证"],
        "5i5j.com": ["请点击", "验证", "authentication"],
        "liepin.com": ["验证", "captcha", "登录"],
        "youzan.com": ["验证", "登录", "captcha"],
        "fgj.hangzhou.gov.cn": ["预售", "公示"],
        "tujia.com": ["验证", "captcha"],
        "guazi.com": ["验证", "captcha", "登录"],
        "dongchedi.com": ["验证", "captcha"],
    }
    for _dom, _mk in _ANTIBOT_VERIFY.items():
        if _dom in _host and _src.get("type") == "browser" \
                and not (_src.get("verify") or {}).get("enabled"):
            cfg["source"].setdefault("verify", {
                "enabled": True, "markers": _mk, "max_wait_ms": 600000,
                "success_selector": "body",
            })
            break
    # 杭州房管局预售公示页是 JS 壳：http 拿不到列表，强制 browser
    if "fgj.hangzhou.gov.cn" in _su0 and _src.get("type") == "http":
        cfg["source"] = {"type": "browser", "headless": False, "scroll_count": 4, "scroll_wait_ms": 800}
        _src = cfg["source"]
    if _st == "http" and not (_src.get("headers") or {}).get("Cookie") \
            and any(_host.endswith(d) for d in _login_domains):
        cfg["source"] = {
            "type": "browser", "headless": False,
            "login": {"enabled": True, "url": _su0,
                      "wait_selector": ".user-info, .avatar, .nickname, a[href*='/member/'], .nav-user"},
            "verify": {"enabled": True, "max_wait_ms": 600000,
                       "markers": ["验证", "安全", "滑块", "登录", "captcha"],
                       "success_selector": "body"},
        }
        _src = cfg["source"]
    # SPA/JS 单页应用入口：AI 常把这类配成 http 直抓（拿到的是空壳 HTML，0 条）。
    # 识别常见 SPA 入口 → 强制浏览器渲染
    _spa = any(k in _su0 for k in ("/problem-list/", "leetcode.cn", "/explore/", "xiaohongshu.com/explore",
                                   "api.zhihu.com") if k != "leetcode.cn/api")
    if _st == "http" and _spa and "leetcode.cn/api" not in _su0:
        cfg["source"] = {
            "type": "browser", "headless": False,
            "scroll_count": 4, "scroll_wait_ms": 800,
            "record_from": "capture_all", "capture_all": True,
            "actions": [{"type": "scroll", "direction": "down", "amount": 1200, "ms": 600}],
        }
        _src = cfg["source"]

    # 公开 JSON/Atom 精配站点：确保 source=http 且不改浏览器
    # 只对「纯公开 API」入口强制 http：leetcode.cn 的 /problem-list/ 是 SPA，必须 browser；
    # 且强制 http 不能覆盖强登录站点（zhihu/api 也要登录，http 直抓必 401）
    _http_ok_domains = ("api.github.com", "export.arxiv.org", "leetcode.com",
                        "wttr.in", "weather.com.cn", "api.bilibili.com")
    _is_login_host = any(_host.endswith(d) for d in _login_domains)
    if (any(_host.endswith(d) for d in _http_ok_domains) \
            or ("/api/" in _su0 and not _is_login_host)
            or ("leetcode.cn" in _host and "/api/" in _su0)) and not _is_login_host:
        cfg["source"] = {"type": "http"}
    # 强制 JSON 精配站点用 http + 不弹登录
    cfg.setdefault("anti_bot", {})

    # 路由修正：rules 若指向空解析器，自动改指"最有内容"的解析器（避免抓到一堆空壳）
    def parser_richness(pc):
        if not isinstance(pc, dict):
            return 0
        return len(pc.get("fields") or {}) + (1 if pc.get("row_css") or pc.get("row_xpath") else 0) \
               + (1 if pc.get("type") in ("json", "json_paged", "table", "article") else 0)

    non_empty = [n for n, pc in cfg.get("parsers", {}).items() if parser_richness(pc) > 0]
    if non_empty:
        best = max(non_empty, key=lambda n: parser_richness(cfg["parsers"][n]))
        for r in cfg["rules"]:
            pn = r.get("parser") or "default"
            pc = cfg.get("parsers", {}).get(pn, {})
            if parser_richness(pc) == 0 and pn in ("default", "main", "list") and best:
                r["parser"] = best
        # 默认解析器为空且有内容解析器时：让默认解析器也指向 best，未匹配 URL 也能解析
        dp = cfg.get("parsers", {}).get("default", {})
        if parser_richness(dp) == 0 and best:
            cfg["parsers"]["default"] = dict(cfg["parsers"][best])  # 保留 extract_links，未匹配 URL 也能翻页

    # 兜底路由：若入口 URL 匹配不到任何规则，追加 catch-all（放在最后，特定规则优先）
    if non_empty:
        def _rule_hits(r, u):
            m = r.get("match", "contains")
            pat = r.get("pattern", "/")
            if m == "regex":
                try:
                    return re.search(pat, u) is not None
                except re.error:
                    return False
            if m == "startswith":
                return u.startswith(pat)
            return pat in u

        if not any(_rule_hits(r, u) for r in cfg["rules"] for u in cfg["start_urls"]):
            if not any(r.get("pattern", "/") == "/" and r.get("match") == "contains" for r in cfg["rules"]):
                cfg["rules"].append({"match": "contains", "pattern": "/", "parser": best})

    validate_task(cfg)
    return cfg


_PROBE_LOCK = threading.Lock()  # probe 缓存读写锁（多任务并发写同一缓存文件）


_AUTH_HINTS = ("验证码", "滑动验证", "安全验证", "人机验证", "访问过于频繁",
              "验证中心", "spiderindefence", "verify.meituan.com",
              "请登录", "登录后", "请输入手机号", "微信扫码登录", "app 扫码登录")


def dom_class_stats(html: str, top: int = 12, min_n: int = 3) -> list:
    """统计页面里出现次数最多的 class（真实 DOM 高频类名），喂给 LLM 写对选择器。"""
    from collections import Counter
    try:
        from lxml import html as _lh
        doc = _lh.fromstring(html)
    except Exception:
        return []
    cnt = Counter()
    for el in doc.iter():
        cls = (el.get("class") or "").strip()
        if cls:
            for c in cls.split():
                cnt[c] += 1
    return [f"{c}×{n}" for c, n in cnt.most_common(top) if n >= min_n]


def _class_stats_text(html: str) -> str:
    st = dom_class_stats(html)
    if st:
        return "\n页面实际高频 class（写选择器时直接用这些）：" + ", ".join(st)
    return ""


def _probe_summary(url: str, max_chars: int = 2500) -> str:
    """探测入口页结构摘要（8s 短超时 + 磁盘缓存 1 小时，大幅提速重复任务）。"""
    import os as _os
    import time as _t
    import urllib.request
    now = _t.time()
    cache_file = ROOT / "outputs" / ".probe_cache.json"
    data = {}
    try:
        with _PROBE_LOCK:
            if cache_file.exists():
                data = json.loads(cache_file.read_text(encoding="utf-8"))
                if data.get(url) and now - data[url][1] < 3600:
                    return data[url][0]
    except Exception:
        data = {}
    summary = ""
    timeout = int(_os.environ.get("US_PROBE_TIMEOUT", "8"))
    try:
        from .structure import build_structure_summary
        summary = build_structure_summary(url) or ""
    except Exception:
        summary = ""
    if not summary:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            # 显式直连（绕过 Clash 系统代理，避免探测被挂起）
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            raw = opener.open(req, timeout=timeout).read(300000).decode("utf-8", "ignore")
            from .extractors import html_to_markdown, extract_links_markdown
            md = html_to_markdown(raw, base_url=url, max_chars=max_chars)
            links = extract_links_markdown(raw, base_url=url, max_links=30)
            summary = md
            if links:
                summary += "\n\n页面链接：\n" + "\n".join(links)
            _cst = _class_stats_text(raw.decode("utf-8", "ignore"))
            if _cst:
                summary += _cst
        except Exception:
            summary = ""
    summary = (summary or "").strip()[:max_chars]
    if summary:
        try:
            with _PROBE_LOCK:
                data[url] = [summary, now]
                if len(data) > 200:
                    data = dict(list(data.items())[-100:])
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    return summary


def _font_obfuscation_hint(sample) -> str:
    """检测字体反爬：字段值含私有区字符(\ue000-\uf8ff)说明数字/文字被自定义字体混淆。"""
    if not sample:
        return ""
    hits = []
    for it in sample:
        for k, v in (it or {}).items():
            if k.startswith("_"):
                continue
            if any("\ue000" <= ch <= "\uf8ff" for ch in str(v or "")):
                if k not in hits:
                    hits.append(k)
    if hits:
        return "｜⚠️ 字段 " + "、".join(hits) + " 疑似字体反爬加密（数字/文字被自定义字体混淆，需解码字体映射才能还原）"
    return ""


def _rendered_class_hint(task_dir) -> str:
    """自修复时把引擎保存的渲染页高频 class 喂给 LLM（登录/JS 页裸抓拿不到真实 DOM）。"""
    try:
        lp = Path(task_dir) / "last_page.html"
        if lp.exists() and lp.stat().st_size > 2000:
            return _class_stats_text(lp.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        pass
    return ""


def _annotated_dom_hint(task_dir, url: str = "") -> str:
    """自修复时把引擎保存的渲染页转成「带 CSS 选择器标注」的 DOM 摘要（scrapedown 思路）。
    比裸 class 统计强：每行给出完整 CSS 路径 + 字段相对选择器 + 真实内容，LLM 一次修对。"""
    try:
        lp = Path(task_dir) / "last_page.html"
        if lp.exists() and lp.stat().st_size > 300:
            from .structure import annotate_dom
            return annotate_dom(lp.read_text(encoding="utf-8", errors="replace"), url or "")
    except Exception:
        pass
    return ""


def _task_evidence_summary(task_dir, log=None) -> Dict[str, Any]:
    """收集任务目录里已有的「真实证据」：渲染页 / 捕获接口 / 页面文件，供 LLM 抽取用。"""
    ev: Dict[str, Any] = {"html": "", "capture": [], "url": ""}
    try:
        lp = Path(task_dir) / "last_page.html"
        if lp.exists() and lp.stat().st_size > 300:
            ev["html"] = lp.read_text(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        cap = Path(task_dir) / "capture_all.json"
        if cap.exists() and cap.stat().st_size > 100:
            data = json.loads(cap.read_text(encoding="utf-8", errors="replace"))
            if isinstance(data, list):
                ev["capture"] = [it for it in data if isinstance(it, dict)][:30]
    except Exception:
        pass
    return ev


def _llm_extract_from_evidence(description: str, cfg: dict, task_dir, log,
                               ev: Optional[Dict[str, Any]] = None) -> dict:
    """改造3：失败轮内先用「真实页面证据」做 LLM 结构化抽取（crawl4ai LLMExtractionStrategy 路线）。

    优先级：capture_all.json（SPA 加密接口）> last_page.html（渲染页）。
    成功即返回条目，避免空转引擎重跑。
    """
    ev = ev if ev is not None else _task_evidence_summary(task_dir, log)
    url = (cfg.get("start_urls") or [""])[0]

    # 1) 捕获接口 JSON：SPA 站的数据全在接口里，让 LLM 从 JSON 挑字段
    if ev.get("capture"):
        from .structure import summarize_json
        chunks = []
        total_chars = 0
        for it in ev["capture"]:
            api = it.get("url", "")
            data = it.get("json", it.get("data"))
            s = f"接口: {api}\n" + summarize_json(data)
            chunks.append(s)
            total_chars += len(s)
            if total_chars > 6000:
                break
        log(f"📡 捕获到 {len(ev['capture'])} 个接口响应，让 LLM 从 JSON 中抽取...")
        prompt = (
            f"任务：{description}\n"
            f"以下是浏览器捕获的接口 JSON 结构（含真实样例值）。请从中提取与任务相关的所有条目，"
            f"输出 JSON 数组，每个对象用贴合任务的中文字段名，链接保持原始 URL。"
            f"接口里没有相关数据就输出 []。只输出 JSON 数组。\n\n"
            + "\n\n".join(chunks)
        )
        try:
            raw_out = _llm_chat([
                {"role": "system", "content": "你是数据抽取引擎，只输出合法 JSON 数组。"},
                {"role": "user", "content": prompt},
            ])
            raw_out = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_out.strip())
            try:
                arr = json.loads(raw_out)
            except json.JSONDecodeError:
                m = re.search(r"\[.*\]", raw_out, re.S)
                arr = json.loads(m.group(0)) if m else []
            if isinstance(arr, list) and arr:
                items = [it for it in arr if isinstance(it, dict)
                         and any(str(v or "").strip() for v in it.values())]
                if items:
                    for it in items:
                        it.setdefault("_url", url)
                        it.setdefault("_parser", "llm_capture")
                    return _save_llm_items(description, items, log)
        except Exception as e:
            log(f"⚠️ 接口 JSON 抽取失败: {e}")

    # 2) 渲染页 HTML：常规 CSS 没抓到时，LLM 直接看 Markdown 挑条目
    if ev.get("html"):
        fb = _llm_fallback_extract(description, cfg, log,
                                   rendered_html=ev["html"], rendered_url=url)
        if fb.get("items"):
            return fb
    return {"items": [], "total": 0, "files": {}}


def _save_llm_items(description: str, items: list, log) -> dict:
    """把 LLM 抽出的条目落盘 outputs/（与 auto 任务同名）。"""
    import hashlib
    name = f"auto_{hashlib.md5(description.encode()).hexdigest()[:10]}"
    items_dir = ROOT / "outputs" / "items"
    items_dir.mkdir(parents=True, exist_ok=True)
    (items_dir / f"{name}.jsonl").write_text(
        "".join(json.dumps(it, ensure_ascii=False) + "\n" for it in items), encoding="utf-8")
    fp = ROOT / "outputs" / f"{name}.json"
    fp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"✅ LLM 直接抽取成功：{len(items)} 条")
    return {"items": items, "total": len(items), "files": {"json": f"outputs/{name}.json"}}


def _try_precise_first(description: str, cfg: dict, limit, log, out_name: str = "",
                       timeout: Optional[int] = None) -> Optional[dict]:
    """改造2：命中「直达型精配」（run 型，如 ggzy）时，首轮前先跑精配，成功直接返回。
    避免空转 N 轮通用引擎。带超时保护（WAF 卡住时不阻塞整个任务）。返回 run_site 结果；未命中返回 None。"""
    su = (cfg.get("start_urls") or [""])[0]
    if not su:
        return None
    try:
        from .sites import match_site, run_site, SITES
        site = match_site(su)
        if not site or not (SITES.get(site) or {}).get("run"):
            return None
        _ch = ((cfg.get("source") or {}).get("headers") or {}).get("Cookie") or ""
        _px = (cfg.get("anti_bot") or {}).get("proxy") or ""
        log(f"🏆 命中直达精配[{site}]：优先用精配（不空转通用引擎）...")
        if timeout is None:
            import os as _os
            timeout = int(_os.environ.get("US_PRECISE_TIMEOUT", "300"))
        box: Dict[str, Any] = {}

        def _run():
            try:
                box["r"] = run_site(su, cookie=_ch, proxy=_px or None,
                                    limit=int(limit or 20), out_name=out_name or None)
            except Exception as e:
                box["e"] = e

        th = threading.Thread(target=_run, daemon=True)
        th.start()
        th.join(timeout)
        if th.is_alive():
            log(f"⏱️ 直达精配[{site}]超过 {timeout}s 未完成（WAF 限流/卡住），放弃精配改用通用引擎")
            return {"total": 0, "rows": [], "files": {},
                    "error": f"直达精配超时（>{timeout}s）", "site": site}
        if "e" in box:
            raise box["e"]
        dr = box.get("r") or {}
        if dr.get("rows"):
            log(f"🏆 直达精配[{site}]成功：{dr['total']} 条")
        elif dr.get("error"):
            log(f"⚠️ 直达精配[{site}]失败：{dr['error']}（改用通用引擎兜底）")
        return dr
    except Exception as e:
        log(f"⚠️ 直达精配未启用：{e}")
        return None


_KEY_DATE_WORDS = ("日期", "时间", "发布", "更新", "date", "time", "publish", "上线", "创建")


def _missing_key_field(description: str, sample: list) -> str:
    """检测用户任务里的关键字段是否缺失（日期/时间、下载量/阅读量等）：
    缺失时返回字段说明，用于触发自修复（避免把没抓到关键字段的结果当成功）。"""
    if not description or not sample:
        return ""
    low = description.lower()
    missing = []
    # 日期/时间类
    if any(w in low for w in _KEY_DATE_WORDS):
        time_fields = [k for k in set(k for it in sample for k in it)
                       if any(w in k.lower() for w in ("date", "time", "publish", "pub", "时间", "日期", "更新"))]
        if not time_fields:
            missing.append("发布日期/时间")
        elif all(not str(it.get(k) or "").strip() for it in sample for k in time_fields):
            missing.append("、".join(sorted(time_fields)[:3]))
    # 下载量/阅读量/被引类
    if any(w in low for w in ("下载", "download", "阅读量", "被引", "引用", "热度", "浏览")):
        dl_fields = [k for k in set(k for it in sample for k in it)
                     if any(w in k.lower() for w in ("download", "下载", "down", "read", "view", "浏览", "阅读", "人次", "热度", "次数", "popular"))]
        if not dl_fields or all(not str(it.get(k) or "").strip() for it in sample for k in dl_fields):
            missing.append("下载量/阅读量")
    # 期刊期数语境检查：任务指明"2026年第1期/2026年1月"，但数据不含该期 → 视为失败去自修复
    if re.search(r"期刊|学报|杂志|论文|文章|《", description):
        m = re.search(r"(20\d{2})年(?:第)?(\d{1,2})[期月]", description)
        if m:
            _y, _n = m.group(1), m.group(2)
            hay = " ".join(str(v) for it in sample for k, v in it.items() if not str(k).startswith("_"))
            # 兼容常见期号写法：2026年第1期 / 2026年,第1期 / 2026年1月 / 2026.43(1)（ajcass）
            if not re.search(rf"{_y}年(?:,)?第{_n}期|{_y}年{_n}月|{_y}\.\d+\s*\(\s*{_n}\s*\)", hay):
                missing.append(f"{_y}年第{_n}期")
    return "、".join(missing)


def _intent_check(description: str, sample: list, log) -> str:
    """意图验收（GitHub 大佬 agent 闭环的 verify 环节）：
    用 LLM 判断抓到的样本是否符合用户要的实体类型。
    返回 "" = 匹配；否则返回不匹配原因（触发自修复换入口，而不是修选择器）。"""
    if not description or not sample:
        return ""
    items = []
    for it in sample[:4]:
        if isinstance(it, dict):
            items.append({k: str(v)[:80] for k, v in it.items() if not str(k).startswith("_")})
    if not items:
        return ""
    try:
        raw_out = _llm_chat([
            {"role": "system", "content": "你是任务验收员：判断抓取结果是否符合用户意图。只输出 JSON {'match': true/false, 'reason': '一句话原因'}。"},
            {"role": "user", "content": (
                f"任务：{description}\n\n"
                f"抓取结果样本：{json.dumps(items, ensure_ascii=False)[:2500]}\n\n"
                f"这些结果属于用户要的实体类型吗？例如用户要 mod/资源，但结果是论坛求助帖/闲聊帖 → match=false。"
                f"只输出 JSON。"
            )},
        ], timeout=120)
        m = re.search(r"\{.*\}", raw_out, re.S)
        if m:
            d = json.loads(m.group(0))
            if d.get("match") is False:
                return str(d.get("reason") or "抓取结果与用户意图不符")
    except Exception:
        pass
    return ""


def _diagnose_failure(urls, result, log) -> str:
    """失败原因诊断（说人话、快）：404 / 登录验证码 / 字体反爬 / 选择器不匹配。"""
    import urllib.request
    reasons = []
    _blk = (result or {}).get("block_stats") or {}
    if _blk.get("waf"):
        reasons.append("网站返回 WAF 滑块验证（CWAP/wzws-waf，跳 waf_slider_verify.html）——HTTP 模式无法直抓，工具已自动切换浏览器模式，请在弹出的浏览器中完成滑块拼图")
    if result.get("errors", 0) > 0:
        reasons.append(f"请求错误 {result['errors']} 次")
    if result.get("error"):
        reasons.append(str(result["error"])[:120])
    for u in (urls or [])[:1]:
        if not str(u).startswith("http"):
            continue
        st, raw = 0, ""
        try:
            req = urllib.request.Request(str(u), headers={"User-Agent": "Mozilla/5.0"})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                resp = opener.open(req, timeout=8)
                st = resp.status or 0
                raw = resp.read(6000).decode("utf-8", "ignore")
            except urllib.error.HTTPError as e:
                st = e.code or 0
        except Exception:
            pass
        if st == 404:
            reasons.append("入口网址不存在（HTTP 404，网站可能改版或网址是 AI 猜的）")
        elif st >= 400:
            reasons.append(f"入口网址被拦截（HTTP {st}，多为反爬拒绝或需要登录）")
        head = raw[:3000]
        hit = next((k for k in _AUTH_HINTS if k in head), None)
        if hit:
            reasons.append(
                f"页面出现「{hit}」→ 网站要求登录/人工验证（滑块/点选）。"
                "解决：重跑时工具会弹出浏览器，你手动登录/过验证一次，会话会自动保存并复用（无需写代码）")
        if "@font-face" in raw or "woff" in raw.lower():
            reasons.append("页面疑似使用字体反爬（数字/文字被自定义字体混淆，需解码字体映射）")
    if not reasons:
        reasons.append("选择器未匹配到内容（可能页面结构变化，或需要浏览器渲染/登录）")
    return "；".join(dict.fromkeys(reasons))


def _page_context(url: str, max_chars: int = 2500) -> str:
    """抓入口页 → 结构摘要/Markdown 摘要（省 token、带缓存，对标 scrape-mcp/cortex-scout）。"""
    s = _probe_summary(url, max_chars=max_chars)
    if s:
        return f"入口页结构摘要（来自真实抓取）：\n{s}"
    return ""


def _llm_fallback_extract(description: str, cfg: dict, log,
                         rendered_html: Optional[str] = None,
                         rendered_url: str = "") -> dict:
    """CSS/规则解析失败时，用 LLM 直接从页面 Markdown 抽取与任务匹配的条目（ScrapeGraphAI 路线）。
    rendered_html：引擎在浏览器抓取后保存的"渲染完成"页面（登录/JS 页必须用它，裸 HTML 会拿到验证页）。"""
    import hashlib
    urls = cfg.get("start_urls") or []
    if not urls:
        return {"items": [], "total": 0, "files": {}}
    url = urls[0]
    raw = ""
    if rendered_html:
        raw = rendered_html
    else:
        import urllib.request
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            raw = urllib.request.urlopen(req, timeout=8).read(200000).decode("utf-8", "ignore")
        except Exception:
            return {"items": [], "total": 0, "files": {}}
    head = raw[:3000]
    if any(k in head for k in _AUTH_HINTS):
        log("⛔ 页面被登录/验证码拦截，LLM 兜底跳过（先解决登录）")
        return {"items": [], "total": 0, "files": {}}
    from .extractors import html_to_markdown
    md = html_to_markdown(raw, base_url=url, max_chars=8000)
    if len(md.strip()) < 80:
        return {"items": [], "total": 0, "files": {}}
    log(f"📄 页面有内容（{len(md)} 字符），正在让 LLM 直接抽取...")
    prompt = (
        f"任务：{description}\n"
        f"以下是抓取的页面内容（Markdown）。请提取与任务相关的所有条目，输出 JSON 数组，"
        f"每个对象用贴合任务的中文字段名（如 标题/链接/价格/作者/时间/正文 等），链接保持原始 URL。"
        f"如果页面上没有任何与任务相关的条目，输出 []。只输出 JSON 数组。\n\n内容：\n{md}"
    )
    try:
        raw_out = _llm_chat([
            {"role": "system", "content": "你是数据抽取引擎，只输出合法 JSON 数组。"},
            {"role": "user", "content": prompt},
        ])
        raw_out = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_out.strip())
        try:
            arr = json.loads(raw_out)
        except json.JSONDecodeError:
            m = re.search(r"\[.*\]", raw_out, re.S)
            arr = json.loads(m.group(0)) if m else []
        if not isinstance(arr, list):
            arr = []
    except Exception as e:
        log(f"❌ LLM 兜底抽取失败: {e}")
        return {"items": [], "total": 0, "files": {}}
    items = [it for it in arr if isinstance(it, dict)
             and any(str(v or "").strip() for v in it.values())]
    if not items:
        return {"items": [], "total": 0, "files": {}}
    for it in items:
        it.setdefault("_url", url)
        it.setdefault("_parser", "llm_fallback")
    name = f"auto_{hashlib.md5(description.encode()).hexdigest()[:10]}"
    items_dir = ROOT / "outputs" / "items"
    items_dir.mkdir(parents=True, exist_ok=True)
    (items_dir / f"{name}.jsonl").write_text(
        "".join(json.dumps(it, ensure_ascii=False) + "\n" for it in items), encoding="utf-8")
    fp = ROOT / "outputs" / f"{name}.json"
    fp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"✅ LLM 兜底抽取成功：{len(items)} 条")
    return {"items": items, "total": len(items), "files": {"json": f"outputs/{name}.json"}}


def _valid_proxy(p: Optional[str]) -> bool:
    """代理格式校验：http(s)://[user:pass@]ip:port，host 不能是占位符，port 必须是数字。"""
    if not p:
        return False
    import re as _re
    m = _re.match(r"^(https?)://([^/@:]+(:[^/@:]+)?@)?([^/:]+):(\d+)$", p)
    if not m:
        return False
    host = m.group(4)
    if host in ("host", "localhost", "example.com", "proxy"):
        return False
    return True


# ---------------------------------------------------------------- 域名自愈
_DNS_FIX_CACHE: Dict[str, List[str]] = {}
_DNS_FIX_LOCK = threading.Lock()
_DNS_FIX_CACHE_FILE = ROOT / "outputs" / ".dns_fix_cache.json"


def _dns_cache_load() -> Dict[str, List[str]]:
    try:
        if _DNS_FIX_CACHE_FILE.exists():
            return json.loads(_DNS_FIX_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _dns_cache_save() -> None:
    global _DNS_FIX_CACHE
    try:
        _DNS_FIX_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        # 上限 500 条，防止长期使用缓存无限膨胀
        if len(_DNS_FIX_CACHE) > 500:
            _DNS_FIX_CACHE = dict(list(_DNS_FIX_CACHE.items())[-500:])
        _DNS_FIX_CACHE_FILE.write_text(json.dumps(_DNS_FIX_CACHE, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
_DNS_SEARCH_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
                  "(KHTML, like Gecko) Version/18.6 Safari/605.1.15")


def _host_status(host: str, timeout: float = 3.0) -> str:
    """域名解析状态：ok=可解析；dead=明确不存在；unknown=超时/网络异常。"""
    import socket
    host = (host or "").strip().lower().rstrip(".")
    if not host or host == "localhost":
        return "dead"
    try:
        import ipaddress
        ipaddress.ip_address(host)
        return "ok"
    except Exception:
        pass
    out: Dict[str, str] = {}

    def _t():
        try:
            socket.getaddrinfo(host, None)
            out["st"] = "ok"
        except socket.gaierror:
            out["st"] = "dead"
        except Exception:
            out["st"] = "unknown"

    th = threading.Thread(target=_t, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        out["st"] = "unknown"
    return out.get("st", "unknown")


def _search_official_links(query: str, limit: int = 6) -> List[str]:
    """搜索引擎找官方域名：DuckDuckGo HTML + Bing，返回候选 URL（按 host 去重）。"""
    import urllib.parse
    import urllib.request
    links: List[str] = []
    seen_hosts = set()
    patterns = [
        # DDG：<a class="result__a" href="//duckduckgo.com/l/?uddg=...">
        re.compile(r'<a[^>]+href="([^"]+)"[^>]*class="[^"]*result__a[^"]*"', re.I),
        re.compile(r'<a[^>]+class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"', re.I),
        # Bing：<h2><a href="...">
        re.compile(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"', re.I),
    ]
    for base in ("https://html.duckduckgo.com/html/?q=", "https://www.bing.com/search?q="):
        if len(links) >= limit:
            break
        url = base + urllib.parse.quote(query)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _DNS_SEARCH_UA})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            raw = opener.open(req, timeout=8).read(300000).decode("utf-8", "ignore")
        except Exception:
            continue
        for pat in patterns:
            for m in pat.finditer(raw):
                href = m.group(1).replace("&amp;", "&")
                if href.startswith("//"):
                    href = "https:" + href
                if not href.startswith(("http://", "https://")):
                    continue
                if "uddg=" in href:  # DDG 跳转包装
                    try:
                        _q = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
                        if _q.get("uddg"):
                            href = _q["uddg"][0]
                    except Exception:
                        pass
                try:
                    host = urllib.parse.urlparse(href).hostname or ""
                except Exception:
                    continue
                if not host or host in seen_hosts:
                    continue
                if host.endswith((".bing.com", ".duckduckgo.com", ".microsoft.com", ".google.com", ".baidu.com")):
                    continue
                seen_hosts.add(host)
                links.append(href)
                if len(links) >= limit:
                    break
            if len(links) >= limit:
                break
    return links[:limit]


def _score_candidate(url: str, keywords: List[str], timeout: float = 4.0) -> int:
    """抓候选站点首页打分：能访问 +1，命中任务关键词每个 +2；0=不可用。"""
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _DNS_SEARCH_UA})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        resp = opener.open(req, timeout=timeout)
        raw = resp.read(80000).decode("utf-8", "ignore")
    except Exception:
        return 0
    if len(raw) < 300:
        return 0
    score = 1
    head = re.sub(r"<script.*?</script>|<style.*?</style>", " ", raw, flags=re.S | re.I)
    head = re.sub(r"<[^>]+>", " ", head)[:8000].lower()
    for kw in keywords:
        if kw and kw.lower() in head:
            score += 2
    # 官方期刊托管平台确定性加分（中科院 ajcass / 知网采编 cbpt / 万方 / 维普官方页）
    try:
        from urllib.parse import urlparse
        host = urlparse(url).hostname or ""
    except Exception:
        host = ""
    if any(t in host for t in ("ajcass", "cbpt.cnki.net", "wanfangdata", "cqvip")):
        score += 3
    # 期刊站特征加分：有"当期目录/过刊浏览/投稿指南/期刊简介"更像期刊官网而非行业组织
    for feat in ("当期目录", "过刊浏览", "投稿指南", "期刊简介", "编辑部"):
        if feat in head:
            score += 1
            break
    return score


def _fix_detail_prefixes(cfg: dict, base: str, log=None) -> None:
    """detail.url_transform.prefix 常被 AI 写成猜的死域名：用已确认的官方站点前缀修正。"""
    try:
        from urllib.parse import urlparse
        for t in (cfg.get("detail") or {}).get("url_transform", []):
            pre = (t.get("prefix") or "").strip()
            if not pre:
                continue
            ph = urlparse(pre).hostname if pre.startswith(("http://", "https://")) else ""
            if ph and _host_status(ph, timeout=2) == "dead":
                t["prefix"] = base
                if log:
                    log(f"🔧 详情页前缀 {pre} 无法解析，已修正为 {base}")
    except Exception:
        pass


def _fix_dead_domains(cfg: dict, description: str = "", log=None) -> dict:
    """域名校验 + 死域名搜索替换：
    - 有可解析域名：只保留可解析的（丢弃 AI 猜的死域名）
    - 全部死域名：用任务关键词搜官方域名，替换为能访问的候选（结果缓存，不重复搜）
    """
    import os as _os
    if _os.environ.get("US_DISABLE_DNS_FIX"):
        return cfg
    urls = [u for u in (cfg.get("start_urls") or []) if str(u).startswith(("http://", "https://"))]
    if not urls:
        return cfg
    from urllib.parse import urlparse
    hosts = [urlparse(u).hostname or "" for u in urls]
    statuses = [_host_status(h) for h in hosts]
    if any(st == "ok" for st in statuses):
        good = [u for u, st in zip(urls, statuses) if st == "ok"]
        if len(good) != len(urls):
            cfg["start_urls"] = good[:10]
            if log:
                log("🔍 已过滤 AI 猜的死域名（DNS 无法解析），保留可访问入口")
        try:
            from urllib.parse import urlparse as _up
            _b = _up(good[0])
            _fix_detail_prefixes(cfg, f"{_b.scheme}://{_b.netloc}", log)
        except Exception:
            pass
        return cfg
    if not all(st == "dead" for st in statuses):
        return cfg  # 有未知状态（超时等），不动，避免误杀
    # 全部死域名 → 搜索官方域名
    cache_key = hashlib.md5((description or " ".join(hosts)).encode("utf-8")).hexdigest()
    with _DNS_FIX_LOCK:
        if not _DNS_FIX_CACHE:
            _DNS_FIX_CACHE.update(_dns_cache_load())
        candidates = _DNS_FIX_CACHE.get(cache_key)
    if candidates is None:
        m = re.search(r"[《「『]([^》」』]{2,40})[》」』]", description or "")
        if m:
            query = m.group(1) + " 官网"
        else:
            query = (description or " ".join(h for h in hosts if h))[:80]
        if log:
            log(f"🔍 AI 给的域名全部无法解析，正在搜索官方域名（关键词：{query}）...")
        links = _search_official_links(query, limit=6)
        kws: List[str] = []
        if m and m.group(1) not in kws:
            kws.append(m.group(1))
        for kw in re.findall(r"[\u4e00-\u9fff]{2,6}", (description or "")[:60]):
            if kw not in kws:
                kws.append(kw)
        scored = []
        for u in links:
            sc = _score_candidate(u, kws)
            if sc > 0:
                scored.append((sc, u))
        # 根路径优先（避免搜到深链文章页/第三方聚合页混入入口）
        def _path_pref(u):
            try:
                from urllib.parse import urlparse
                return len(urlparse(u).path.rstrip("/").split("/"))
            except Exception:
                return 99

        scored.sort(key=lambda x: (-x[0], _path_pref(x[1]), len(x[1])))
        candidates = [u for _, u in scored[:1]]
        with _DNS_FIX_LOCK:
            _DNS_FIX_CACHE[cache_key] = candidates
            _dns_cache_save()
    if candidates:
        cfg["start_urls"] = candidates
        if log:
            log(f"✅ 已用官方域名替换死入口：{candidates[0]}")
        try:
            from urllib.parse import urlparse as _up
            _b = _up(candidates[0])
            _fix_detail_prefixes(cfg, f"{_b.scheme}://{_b.netloc}", log)
        except Exception:
            pass
    else:
        if log:
            log("⚠️ 搜索未找到可用官方域名，保留原入口（任务将报错，供诊断）")
    return cfg


def _force_browser_waf(cfg: dict) -> dict:
    """把 http 配置升级为 browser + WAF 滑块人工验证（CWAP/wzws 等）。"""
    old = cfg.get("source") or {}
    src = {
        "type": "browser",
        "headless": False,
        "scroll_count": int(old.get("scroll_count", 4)),
        "scroll_wait_ms": int(old.get("scroll_wait_ms", 800)),
        "verify": {
            "enabled": True,
            "markers": ["waf_slider_verify", "wzws-waf-cgi", "wzws_waf", "CWAP-waf", "请完成安全验证", "滑动填"],
            "max_wait_ms": 600000,
        },
    }
    for k in ("headers", "cdp", "record_from", "capture_all"):
        if old.get(k):
            src[k] = old[k]
    if (old.get("login") or {}).get("enabled"):
        src["login"] = old["login"]
    cfg["source"] = src
    if not (src.get("login") or {}).get("enabled"):
        cfg.pop("login", None)
    cfg.pop("verify", None)
    return cfg


def _build_config(description: str, proxy: Optional[str] = None,
                    cookie: Optional[str] = None, log=None) -> tuple:
    """生成任务配置（探测 + LLM + 校验 + 注入），返回 (cfg, name, task_dir)。不运行。"""
    if log is None:
        log = lambda m: None
    h = hashlib.md5(description.encode()).hexdigest()[:10]
    name = f"auto_{h}"
    task_dir = ROOT / "tasks" / name
    (task_dir / "modules").mkdir(parents=True, exist_ok=True)

    # 生成配置（先做入口页结构探测，把结构摘要喂给 AI）
    from .structure import build_structure_summary, extract_urls
    summary = None
    urls = extract_urls(description)
    if urls:
        log(f"🔍 正在探测入口页结构: {urls[0]}")
        summary = build_structure_summary(urls[0])
        if summary:
            log("✅ 结构摘要已生成（省 token、选择器更准）")
        else:
            summary = _page_context(urls[0])
            if summary:
                log("✅ 已抓取入口页 Markdown 摘要（省 token、选择器更准）")
    # 注入当前真实日期（北京时间）：让 AI 按“今天/昨天”正确换算，杜绝抄示例旧日期
    import datetime as _dt
    _tz8 = _dt.timezone(_dt.timedelta(hours=8))
    _now = _dt.datetime.now(_tz8)
    _today0 = _now.replace(hour=0, minute=0, second=0, microsecond=0)
    _tomorrow0 = _today0 + _dt.timedelta(days=1)
    prompt = f"任务：{description}\n请输出完整 config.json。"
    prompt += (
        f"\n\n【当前真实日期】今天是 {_now.strftime('%Y-%m-%d')}（北京时间）。"
        f"今天 00:00 的 Unix 秒 = {int(_today0.timestamp())}，明天 00:00 = {int(_tomorrow0.timestamp())}。"
        f"用户说“今天/昨天/前天/N天前”时按此换算，禁止使用示例里的旧日期。"
    )
    if summary:
        prompt += f"\n\n入口页结构摘要（来自真实抓取，据此推断 row_css/fields/extract_links 会更准）：\n{summary}"
    if re.search(r"翻|分页|page|更多页", description, re.I):
        prompt += "\n\n【硬性要求】任务明确要求翻页：HTML 解析器必须带 extract_links（allow 写锚定正则，如 ^/page/\\d+/$），JSON 必须用 json_paged，否则配置不合格。"
    prompt += "\n\n只输出完整 config.json。"
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    log("🤖 AI 正在理解任务并生成配置...")
    cfg = _extract_json(_llm_chat(messages))
    cfg = _validate_and_fix(cfg, description, log)
    if proxy:
        if _valid_proxy(proxy):
            cfg.setdefault("anti_bot", {})["proxy"] = proxy
            log(f"🛰️ 已注入代理：{proxy}")
        else:
            log(f"⚠️ 代理格式无效已忽略：{proxy}（正确格式 http://user:pass@ip:port，host:port 是占位符不能直接用）")
    if cookie:
        cfg.setdefault("source", {})["type"] = "http"
        cfg["source"].setdefault("headers", {})["Cookie"] = cookie
        cfg.get("source", {}).pop("login", None)
        cfg.get("source", {}).pop("verify", None)
        cfg.pop("login", None); cfg.pop("verify", None)
        log("🍪 已注入登录 Cookie（HTTP 直抓模式，跳过验证/登录弹窗）")
    _ab = cfg.get("anti_bot") or {}
    if _ab.get("proxy") and not _valid_proxy(_ab["proxy"]):
        _ab.pop("proxy", None)
        log("⚠️ 已清理配置中的非法代理占位符（host:port 不能直接用）")
    _src = cfg.get("source", {}) or {}
    _verify = _src.get("verify") or {}
    _login = _src.get("login") or {}
    if _verify.get("enabled") or _login.get("enabled"):
        wait_s = int(_verify.get("max_wait_ms", 300000)) // 1000
        log(f"⚠️ 网站需要人工验证/登录：即将弹出真实浏览器，请在弹出的窗口中完成滑块/点选/登录"
            f"（最长等待 {wait_s} 秒），完成后自动继续并保存会话")
    log(f"✅ 配置已生成（source={cfg.get('source', {}).get('type')}）")
    return cfg, name, task_dir


def describe_route(cfg: dict, description: str = "") -> Dict[str, str]:
    """生成技术路线说明（给用户确认用）。"""
    from .sites import match_site
    su = (cfg.get("start_urls") or [""])[0]
    src = cfg.get("source", {}) or {}
    parts = []
    _int = cfg.get("intent") or {}
    if _int:
        _t = str(_int.get("target_type") or "").strip()
        _e = str(_int.get("entry_rationale") or "").strip()
        if _t:
            parts.append(f"🎯 AI 理解的实体类型：{_t}")
        if _e:
            parts.append(f"📌 入口依据：{_e}")
        if _int.get("entry_unknown"):
            parts.append("⚠️ AI 不确定入口，请人工确认/提供正确入口 URL")
    else:
        parts.append("⚠️ AI 未输出意图自检（intent），请重点核对入口是否对应你要的资源类型")
    site = match_site(su) if su else None
    if site:
        parts.append(f"🏆 命中精配站点[{site}]：用专用解析器，字段干净")
    st = src.get("type", "http")
    cookie = bool((src.get("headers") or {}).get("Cookie"))
    if st == "http":
        if cookie:
            parts.append("🍪 Cookie 直抓（HTTP+登录态，无弹窗/无验证）")
        elif (cfg.get("anti_bot") or {}).get("proxy"):
            parts.append("🛰️ 代理直抓（HTTP+代理）")
        else:
            parts.append("🌐 HTTP 直连（公开页面）")
    elif st == "browser":
        if (src.get("verify") or {}).get("enabled"):
            parts.append("🖥️ 浏览器 + 人工验证（会弹窗，需拖滑块/点选）")
        if (src.get("login") or {}).get("enabled"):
            parts.append("🔑 浏览器 + 登录（会弹窗，需扫码/账号）")
        if not (src.get("verify") or {}).get("enabled") and not (src.get("login") or {}).get("enabled"):
            parts.append("🖥️ 浏览器渲染（JS 页面，无人工步骤）")
    elif st == "bridge":
        parts.append("🌉 桥取数（Node 桥过 WAF）")
    fields = []
    for pcfg in (cfg.get("parsers") or {}).values():
        if isinstance(pcfg, dict):
            fields.extend(list((pcfg.get("fields") or {}).keys()))
    fields = list(dict.fromkeys(fields))[:8]
    q = cfg.get("queue", {})
    summary_parts = [f"入口: {su[:70]}"]
    if fields:
        summary_parts.append("字段: " + "、".join(fields))
    summary_parts.append(f"上限: {q.get('max_requests', '默认')} 请求 / 深度 {q.get('max_depth', '默认')}")
    for pl in (cfg.get("pipelines") or [])[:3]:
        if pl.get("type") == "filter" and pl.get("op") == "between":
            import datetime
            try:
                t0 = datetime.datetime.fromtimestamp(int(pl["min"])).strftime("%Y-%m-%d")
                t1 = datetime.datetime.fromtimestamp(int(pl["max"])).strftime("%Y-%m-%d")
                summary_parts.append(f"日期过滤: {t0} ~ {t1}")
            except Exception:
                pass
    # 配置风险提示：帮用户在确认前一眼发现 AI 配置问题
    warnings = []
    for _vn in (cfg.get("_needs_input") or []):
        warnings.append(f"⛔ 任务缺少变量【{_vn}】（如企业名称/关键词/城市），请在上方配置 vars 里填写具体值，否则会空抓")

    _login_need = ("xiaohongshu.com", "zhihu.com", "weibo.com", "douyin.com", "kuaishou.com",
                   "taobao.com", "tmall.com", "jd.com", "yangkeduo.com", "pinduoduo.com",
                   "dianping.com", "meituan.com", "goofish.com", "dewu.com", "zhipin.com",
                   "qcc.com", "tianyancha.com", "qimai.cn", "wenshu.court.gov.cn",
                   "zxgk.court.gov.cn", "kns.cnki.net")
    _host = ((su or "").split("//")[-1].split("/")[0] if "//" in (su or "") else (su or "")).lower()
    if st == "http" and not cookie and any(_host.endswith(d) for d in _login_need):
        warnings.append("⚠️ 该站点通常需要登录，AI 却配成了 HTTP 直抓——大概率 0 条，建议改为浏览器+登录")
    if st == "browser" and not (src.get("login") or {}).get("enabled") \
            and not (src.get("verify") or {}).get("enabled"):
        warnings.append("ℹ️ 浏览器模式但没配 login/verify：若页面有登录墙会失败")
    row_css_ok = any(isinstance(pcfg, dict) and (pcfg.get("row_css") or pcfg.get("row_xpath") or pcfg.get("records_path") is not None)
                     for pcfg in (cfg.get("parsers") or {}).values())
    if not row_css_ok and st in ("http", "browser"):
        warnings.append("⚠️ 解析器没写 row_css/records_path，列表可能 0 条")
    if len(fields) < 3:
        warnings.append("⚠️ 字段少于 3 个，可能漏抓用户要的信息")
    if (cfg.get("detail") or {}).get("enabled") and not any(
            isinstance(pl, dict) and pl.get("type") in ("parse_date", "filter") and pl.get("field") == "ts"
            for pl in (cfg.get("detail") or {}).get("filters") or []):
        if re.search(r"日期|时间|今天|昨天|发布", description or ""):
            warnings.append("⚠️ 任务含时间要求，但 detail 没配 parse_date 日期过滤，可能把旧内容也收进来")
    return {"route": "；".join(parts) if parts else "通用 AI 流程",
            "summary": "；".join(summary_parts),
            "warnings": warnings}


def plan_task(description: str, limit: Optional[int] = None, proxy: Optional[str] = None,
              cookie: Optional[str] = None, log_cb=None) -> Dict[str, Any]:
    """🔍 生成「执行计划」但不运行：任务理解 + 技术路线 + 完整配置。"""
    lines = []
    def log(msg):
        lines.append(msg)
        if log_cb:
            log_cb(msg)
    try:
        cfg, name, task_dir = _build_config(description, proxy=proxy, cookie=cookie, log=log)
        # 持久化配置：确认后执行可复用；排查/批量测试也读同一份，避免每次重新生成
        try:
            from pathlib import Path as _PP
            (task_dir / "config.json").write_text(
                json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
            (task_dir / "description.txt").write_text(description, encoding="utf-8")
        except Exception:
            pass
        plan = describe_route(cfg, description)
        return {"ok": True, "name": name, "task_dir": str(task_dir),
                "config": cfg, "route": plan["route"], "summary": plan["summary"],
                "warnings": plan.get("warnings") or [],
                "needs_input": cfg.get("_needs_input") or [],
                "messages": lines, "description": description}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "messages": lines}


def auto_task(description: str, limit: Optional[int] = None, rounds: int = 2,
              log_cb=None, round_timeout: Optional[int] = None,
              proxy: Optional[str] = None,
              cookie: Optional[str] = None,
              agent_fallback: Optional[bool] = None) -> Dict[str, Any]:
    """执行一次自动任务。返回 {config, result, log, sample, files}。
    agent_fallback: 常规解析/LLM 抽取都没结果时，是否启用 LLM 浏览器代理兜底
    （None=自动：无人工验证/登录的任务默认启用，有 verify/login 的不启用避免重复弹窗）。"""
    if limit is not None:
        limit = int(limit) or None

    def log(msg):
        if log_cb:
            log_cb(msg)

    # 批量回归模式：US_REUSE_CONFIG=1 且任务已有落盘配置 → 直接复用（省 LLM、可反复修引擎）
    _h = hashlib.md5(description.encode()).hexdigest()[:10]
    _task_dir0 = ROOT / "tasks" / f"auto_{_h}"
    _reused_cfg = None
    if os.environ.get("US_REUSE_CONFIG") == "1":
        _cfg_p = _task_dir0 / "config.json"
        if _cfg_p.exists():
            try:
                _saved = json.loads(_cfg_p.read_text(encoding="utf-8"))
                if _saved.get("start_urls"):
                    _reused_cfg = _saved
            except Exception:
                pass
    if _reused_cfg is not None:
        cfg = _reused_cfg
        name = f"auto_{_h}"
        task_dir = _task_dir0
        log("♻️ 复用已落盘配置（US_REUSE_CONFIG=1），并重新过校验规则（新修复自动套用旧配置）")
        try:
            cfg = _validate_and_fix(cfg, description, log)
        except Exception as _e:
            log(f"⚠️ 复用配置校验失败，退回原配置：{_e}")
        (task_dir / "modules").mkdir(parents=True, exist_ok=True)
    else:
        cfg, name, task_dir = _build_config(description, proxy=proxy, cookie=cookie, log=log)
        try:
            (task_dir / "config.json").write_text(
                json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
            (task_dir / "description.txt").write_text(description, encoding="utf-8")
        except Exception:
            pass
    # 缺变量（如 {company_name}）→ 直接拒绝执行，给出明确提示，避免空转/卡死
    _need = cfg.get("_needs_input") or []
    if _need:
        raise RuntimeError(
            "任务缺少变量【" + "、".join(_need) + "】（如企业名称/关键词/城市）。"
            "请在「生成执行计划」后的配置里 vars 填写具体值，或把具体值直接写进任务描述再试。")
    # 统一命名：storage/output 一律用 auto 任务名（否则引擎写 items/xxx.jsonl 与
    # auto 读 sample 的 items/auto_xxx.jsonl 不一致，复核/抽样会被旧数据污染）
    cfg["name"] = name
    cfg.setdefault("storage", {})["name"] = name
    cfg.setdefault("output", {})["base_name"] = name

    last_result = {}
    last_log = ""
    sample: List[Dict[str, Any]] = []
    # 🏆 改造2：命中「直达型精配」（run 型，如 ggzy）时，首轮前先跑精配，成功直接返回
    _precise = _try_precise_first(description, cfg, limit, log, out_name=name)
    _precise_done = bool(_precise and _precise.get("rows"))
    _precise_attempted = _precise is not None   # run 型已试过：末尾不再重复打站
    if _precise_done:
        sample = _precise["rows"][:5]
        files = _precise["files"]
        last_result = {"total": _precise["total"], "fetched": _precise["total"], "errors": 0,
                       "precise": _precise.get("site")}
    _loop_rounds = 0 if _precise_done else rounds
    _llm_ev_tried = False
    for round_i in range(1, _loop_rounds + 1):
        # 写任务包
        (task_dir / "modules").mkdir(parents=True, exist_ok=True)
        (task_dir / "config.json").write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        # 每轮运行前清空旧 items（同名任务多次跑会 append 累积，污染抽样/复核）
        _items_dir = ROOT / "outputs" / "items"
        _items_dir.mkdir(parents=True, exist_ok=True)
        try:
            (_items_dir / f"{name}.jsonl").unlink(missing_ok=True)
        except Exception:
            pass

        # 运行（带超时保护：单轮最多 round_timeout 秒，超时强制终止并停止重试，绝不无限转圈）
        import os as _os
        import threading as _th
        if round_timeout is None:
            round_timeout = int(_os.environ.get("US_AUTO_ROUND_TIMEOUT", "240"))
        _src0 = cfg.get("source", {}) or {}
        if os.environ.get("US_BATCH") != "1":
            if (_src0.get("verify") or {}).get("enabled") or (_src0.get("login") or {}).get("enabled"):
                round_timeout = max(round_timeout, 600)  # 人工验证要等人，放宽到 10 分钟
            if (cfg.get("detail") or {}).get("enabled"):
                round_timeout = max(round_timeout, 1200)  # 详情补抓(几十页)要时间，放宽到 20 分钟
        log(f"▶️ 第 {round_i} 轮运行（超时上限 {round_timeout}s）...")
        from .engine_v3 import run_task
        from .log import Logger
        log_file = ROOT / "outputs" / f".run_{name}.log"
        _box = {}
        def _run_round():
            _box["r"] = run_task(task_dir, limit=limit, log_file=log_file, log_cb=log_cb)
        _t = _th.Thread(target=_run_round, daemon=True)
        _t.start()
        _t.join(timeout=round_timeout)
        if _t.is_alive():
            log(f"⏱️ 本轮超过 {round_timeout}s 未结束，正在发送停止信号并回收后台线程...")
            try:
                (Path(task_dir) / ".stop").write_text("1", encoding="utf-8")
            except Exception:
                pass
            _t.join(timeout=30)
            if _t.is_alive():
                log("⚠️ 后台线程 30s 后仍未退出（可能卡在子进程），本轮结果作废，不再重试")
            result = {"total": 0, "fetched": 0, "errors": -2, "error": f"运行超时（>{round_timeout}s）"}
            last_result = result
            last_log = log_file.read_text(encoding="utf-8", errors="replace") if log_file.exists() else ""
            break
        try:
            result = _box.get("r") or {}   # 线程内 run_task 抛错时兜底，避免二次崩溃
            last_result = result
            last_log = log_file.read_text(encoding="utf-8", errors="replace") if log_file.exists() else ""
        except Exception as e:
            result = {"total": 0, "fetched": 0, "errors": -1, "error": str(e)}
            last_result = result
            last_log = log_file.read_text(encoding="utf-8", errors="replace") if log_file.exists() else str(e)

        items_path = ROOT / "outputs" / "items" / f"{name}.jsonl"
        sample = []
        if items_path.exists():
            # 从文件末尾读（最新写入在末尾），最多回看 500 行取最新 5 条
            _lines = items_path.read_text(encoding="utf-8", errors="replace").splitlines()[-500:]
            for line in reversed(_lines):
                if line.strip():
                    try:
                        sample.append(json.loads(line))
                    except Exception:
                        pass
                if len(sample) >= 5:
                    break
            sample.reverse()

        # 成功判定：必须有"真实字段"（至少一个非 _url/_parser/_ts 的字段非空），
        # 防止 AI 生成空 fields 导致 total>0 但全是空壳
        META = ("_url", "_parser", "_ts", "_id")
        real = [it for it in sample if any(
            str(it.get(k) or "").strip() for k in it if k not in META)]
        _miss = _missing_key_field(description, sample)
        _intent_bad = ""
        if result.get("total", 0) > 0 and real and not _miss:
            _intent_bad = _intent_check(description, sample, log)
            if not _intent_bad:
                log(f"✅ 第 {round_i} 轮成功：{result.get('total')} 条（抽样 {len(real)} 条有真实字段）")
                break
            log(f"⚠️ 意图校验未通过：{_intent_bad}（抓到的不是用户要的内容，进入自修复换入口）")
        if _miss:
            log(f"⚠️ 第 {round_i} 轮 total={result.get('total')} 但用户关键字段【{_miss}】为空，视为失败，进入自修复（需要详情页补抓）...")
        elif result.get("total", 0) > 0 and not real:
            log(f"⚠️ 第 {round_i} 轮 total={result.get('total')} 但全是空壳字段，视为失败，进入自修复...")

        # 🛡️ 反爬/验证拦截 → 确定性自动升级浏览器模式（不靠 LLM 猜）
        # 覆盖 waf/cloudflare/verify/captcha/anti_bot/rate_limit 等：http 硬刚只会一直 0 条
        _blk = (result or {}).get("block_stats") or {}
        _BLOCK_UPGRADE_KINDS = ("waf", "cloudflare", "verify", "captcha", "anti_bot", "rate_limit")
        _any_block = any(_blk.get(k) for k in _BLOCK_UPGRADE_KINDS)
        if _any_block and ((cfg.get("source") or {}).get("type") == "http"):
            log("🛡️ 检测到反爬拦截（" + "、".join(f"{k}×{_blk[k]}" for k in _BLOCK_UPGRADE_KINDS if _blk.get(k))
                + "），自动升级为浏览器模式——请在弹出的浏览器窗口中完成验证/登录，完成后自动继续...")
            cfg = _force_browser_waf(cfg)
            continue

        # 🧠 改造3：失败轮内先对真实页面证据（渲染页/捕获接口）做 LLM 结构化抽取，
        # 成功即收——避免「空转重跑 → 再失败 → 再自修复」的漫长循环（crawl4ai 路线）
        if not _llm_ev_tried:
            _llm_ev_tried = True
            _ev = _task_evidence_summary(task_dir)
            if _ev.get("html") or _ev.get("capture"):
                log("🧠 常规解析未命中，先对真实页面做 LLM 直接抽取（成功即收，不空转重跑）...")
                fb = _llm_extract_from_evidence(description, cfg, task_dir, log, ev=_ev)
                if fb.get("items"):
                    sample = fb["items"][:5]
                    files = fb["files"]
                    last_result = dict(last_result or {})
                    last_result["total"] = fb["total"]
                    last_result["llm_extract"] = True
                    log(f"✅ 第 {round_i} 轮 LLM 直接抽取成功：{fb['total']} 条")
                    break

        if round_i < rounds:
            log(f"⚠️ 第 {round_i} 轮 0 条/报错，AI 正在自修复...")
            _blocks = (result or {}).get("block_stats") or {}
            _bhint = ""
            if _blocks:
                from .antibot import block_summary
                _bhint = (f"\n反爬拦截统计: {block_summary(_blocks)}。"
                          f"若含 cloudflare/verify/captcha/429/403：必须换路线——"
                          f"①source.type 改 browser + login/verify 弹窗人工过验证；"
                          f"②或建议用户用 CDP 直连已登录浏览器(加 \"cdp\":\"http://127.0.0.1:9222\")；"
                          f"③有真实代理时用 anti_bot.proxy。不要继续用 http 硬刚。")
            fix_messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": (
                    f"任务：{description}\n"
                    f"上次配置：{json.dumps(cfg, ensure_ascii=False)}\n"
                    f"运行结果：{json.dumps(result, ensure_ascii=False)}\n"
                    + (f"意图校验未通过：{_intent_bad}\n**这是入口选错，不是选择器问题**：必须换到目标资源类型对应的列表页（如论坛的 MOD发布区/资源下载区/下载频道），不要只改选择器；用户没给入口就向用户要正确入口 URL。\n" if _intent_bad else "")
                    + f"{_bhint}\n"
                    f"{_page_context(cfg.get('start_urls', [''])[0]) if cfg.get('start_urls') else ''}\n"
                    f"{_annotated_dom_hint(task_dir, (cfg.get('start_urls') or [''])[0])}\n"
                    f"{_rendered_class_hint(task_dir)}\n"
                    f"运行日志（末尾）：\n{last_log[-2000:]}\n\n"
                    f"【选择器修正要点】上面【重复块候选】是真实渲染页里带 CSS 选择器标注的 DOM："
                    f"完整CSS 就是 row_css，实例里的相对选择器就是 fields 的 css。"
                    f"请严格按它修正 row_css 和 fields，不要凭空猜选择器。"
                    f"若日志提示【用户关键字段缺失】，说明列表页没有该字段，"
                    f"必须在 config 里加 detail 配置（url_field 指向列表记录中的详情 URL 字段，"
                    f"url_transform.prefix 补全域名，extract 抓详情页字段，filters 做日期过滤）。\n"
                    f"请修正配置（选择器/网址/解析方式/是否升级浏览器/加 detail 等），只输出修正后的完整 config.json。"
                )},
            ]
            try:
                cfg = _extract_json(_llm_chat(fix_messages))
                cfg = _validate_and_fix(cfg, description, log)
                cfg["name"] = name
                cfg["storage"]["name"] = name
                cfg["output"]["base_name"] = name
            except Exception as e:
                log(f"❌ 自修复生成失败: {e}")
                break

    # 导出文件清单
    files = {}
    for ext in ("json", "csv", "xlsx"):
        fp = ROOT / "outputs" / f"{name}.{ext}"
        if fp.exists():
            files[ext] = str(fp.relative_to(ROOT))
    # 最终总结：说人话，不再让用户以为卡死
    META = ("_url", "_parser", "_ts", "_id")
    total = (last_result or {}).get("total", 0)
    real = [it for it in sample if any(str(it.get(k) or "").strip() for k in it if k not in META)]

    # 常规解析未命中但页面有内容 → LLM 直接抽取兜底（扩能力边界）
    if not (total > 0 and real):
        log("🧠 常规解析未命中，尝试 LLM 直接抽取（兜底）...")
        _rendered = ""
        _rurl = ""
        try:
            _lp = Path(task_dir) / "last_page.html"
            if _lp.exists() and _lp.stat().st_size > 2000:
                _rendered = _lp.read_text(encoding="utf-8", errors="replace")
                _rurl = (cfg.get("start_urls") or [""])[0]
        except Exception:
            pass
        fb = _llm_fallback_extract(description, cfg, log,
                                   rendered_html=_rendered or None, rendered_url=_rurl)
        if fb.get("items"):
            sample = fb["items"][:5]
            files = fb["files"]
            last_result = dict(last_result or {})
            last_result["total"] = fb["total"]
            last_result["llm_fallback"] = True
            total = fb["total"]
            real = sample
            log("✅ LLM 兜底成功，任务视为完成")

    # 🕹️ LLM 浏览器代理兜底（browser-use 路线）：常规解析+LLM抽取都没结果、
    # 且任务不需要人工验证/登录时，让 LLM 看真实页面自己点/翻/抽（对没精配/结构怪的站自适应）
    if not (total > 0 and real) and agent_fallback is not False:
        _src_now = cfg.get("source", {}) or {}
        _need_human = bool((_src_now.get("verify") or {}).get("enabled")
                           or (_src_now.get("login") or {}).get("enabled")
                           or _src_now.get("headless") is False)
        if not _need_human:
            log("🕹️ 常规解析未命中，尝试 LLM 浏览器代理模式（AI 看页面自己点/翻/抽）...")
            try:
                from .agent import agent_task
                _u0 = (cfg.get("start_urls") or [""])[0]
                _cdp0 = str(_src_now.get("cdp") or "")
                ag = agent_task(description, start_url=_u0, max_steps=10,
                                cdp=_cdp0, limit=limit, log_cb=log)
                if ag.get("items"):
                    sample = ag["items"][:5]
                    total = ag["total"]
                    real = sample
                    last_result = dict(last_result or {})
                    last_result["total"] = ag["total"]
                    last_result["agent_mode"] = True
                    # 落盘
                    try:
                        import hashlib as _hl
                        _n = f"auto_{_hl.md5(description.encode()).hexdigest()[:10]}"
                        _fp = ROOT / "outputs" / f"{_n}.json"
                        _fp.write_text(json.dumps(ag["items"], ensure_ascii=False, indent=2), encoding="utf-8")
                        files = {"json": f"outputs/{_n}.json"}
                    except Exception:
                        pass
                    log(f"✅ LLM 浏览器代理成功：{ag['total']} 条")
                else:
                    log(f"⚠️ LLM 浏览器代理未收集到条目：{ag.get('error','')[:120]}")
            except Exception as _e:
                log(f"⚠️ LLM 浏览器代理不可用：{_e}")

    # 🏆 精配解析器覆盖（高频网站注册表）：检测到命中即用精配解析器，字段干净
    # run 型直达精配已在首轮前跑过（_precise_attempted），末尾只兜底 fetch+parse 型精配
    if not _precise_done and not _precise_attempted:
        try:
            su = (cfg.get("start_urls") or [""])[0]
            from .sites import match_site, run_site
            _site = match_site(su)
            if _site:
                _ch = ((cfg.get("source") or {}).get("headers") or {}).get("Cookie") or ""
                _px = (cfg.get("anti_bot") or {}).get("proxy") or ""
                _dr = run_site(su, cookie=_ch, proxy=_px or None, limit=int(limit or 20),
                               out_name=name)
                if _dr.get("rows"):
                    _lim = int(limit or 20)
                    _rows = _dr["rows"][:_lim]
                    sample = _rows[:5]
                    files = _dr["files"]
                    total = min(int(_dr["total"] or 0), _lim)
                    real = sample
                    log(f"🏆 精配解析[{_site}]覆盖：{total} 条（字段干净）")
                elif _dr.get("error"):
                    log(f"⚠️ 精配解析[{_site}]失败：{_dr['error']}")
        except Exception as _e:
            log(f"⚠️ 精配解析未启用：{_e}")

    # 自动复核（字段完整率/去重/数量，不联网，秒级完成）
    verify = None
    try:
        from .verify import verify_rows
        verify = verify_rows(sample, cfg, sample_n=0, network=False,
                             declared=total or len(sample))
    except Exception:
        verify = None

    if total > 0 and real:
        vtxt = ""
        if verify and verify.get("checks"):
            bad = [c["name"] for c in verify["checks"] if not c.get("pass", True)]
            vtxt = ("｜复核 ✅ 通过" if not bad else "｜复核 ⚠️ " + "；".join(bad))
        summary = f"✅ 任务结束：成功 {total} 条（抽样 {len(real)} 条有真实字段）{vtxt}{_font_obfuscation_hint(real)}，导出 {list(files)}"
    else:
        reason = _diagnose_failure(cfg.get("start_urls"), last_result, log)
        summary = f"⚠️ 任务结束：0 条。原因诊断：{reason}"
        log(summary)
    return {
        "name": name,
        "config": cfg,
        "result": last_result,
        "log": last_log[-3000:],
        "sample": sample,
        "files": files,
        "summary": summary,
        "verify": verify,
        "done": True,
    }


def run_with_config(config: dict, name: str, task_dir, description: str = "",
                   limit: Optional[int] = None, rounds: int = 2,
                   round_timeout: Optional[int] = None,
                   log_cb=None) -> Dict[str, Any]:
    """确认后的执行：用已确认的 config 直接运行（跳过 AI 生成）。"""
    import json as _json
    from pathlib import Path as _P
    if limit is not None:
        limit = int(limit) or None

    def log(msg):
        if log_cb:
            log_cb(msg)

    td = _P(task_dir)
    (td / "modules").mkdir(parents=True, exist_ok=True)
    # 统一命名：storage/output 与运行器 name 一致（避免 items 文件名错位污染抽样/复核）
    config["name"] = name
    config.setdefault("storage", {})["name"] = name
    config.setdefault("output", {})["base_name"] = name
    (td / "config.json").write_text(_json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    _items_dir = ROOT / "outputs" / "items"
    _items_dir.mkdir(parents=True, exist_ok=True)
    try:
        (_items_dir / f"{name}.jsonl").unlink(missing_ok=True)
    except Exception:
        pass

    last_result = {}
    last_log = ""
    sample: List[Dict[str, Any]] = []
    # 🏆 改造2：命中「直达型精配」（run 型，如 ggzy）时，首轮前先跑精配，成功直接返回
    _precise = _try_precise_first(description or "", config, limit, log, out_name=name)
    _precise_done = bool(_precise and _precise.get("rows"))
    _precise_attempted = _precise is not None   # run 型已试过：末尾不再重复打站
    if _precise_done:
        sample = _precise["rows"][:5]
        files = _precise["files"]
        last_result = {"total": _precise["total"], "fetched": _precise["total"], "errors": 0,
                       "precise": _precise.get("site")}
    _loop_rounds = 0 if _precise_done else rounds
    _llm_ev_tried = False
    import os as _os
    import threading as _th
    if round_timeout is None:
        round_timeout = int(_os.environ.get("US_AUTO_ROUND_TIMEOUT", "240"))
    _src0 = config.get("source", {}) or {}
    if os.environ.get("US_BATCH") != "1":
        if (_src0.get("verify") or {}).get("enabled") or (_src0.get("login") or {}).get("enabled"):
            round_timeout = max(round_timeout, 600)
    for round_i in range(1, _loop_rounds + 1):
        # 每轮按当前配置重算超时（WAF 自动升级浏览器后要等人工滑块，需放宽到 10 分钟）
        _src0 = config.get("source", {}) or {}
        if os.environ.get("US_BATCH") != "1":
            if (_src0.get("verify") or {}).get("enabled") or (_src0.get("login") or {}).get("enabled"):
                round_timeout = max(round_timeout, 600)
            if (config.get("detail") or {}).get("enabled"):
                round_timeout = max(round_timeout, 1200)
        log(f"▶️ 第 {round_i} 轮运行（超时上限 {round_timeout}s）...")
        from .engine_v3 import run_task
        log_file = ROOT / "outputs" / f".run_{name}.log"
        _box = {}
        def _run_round():
            _box["r"] = run_task(td, limit=limit, log_file=log_file, log_cb=log_cb)
        _t = _th.Thread(target=_run_round, daemon=True)
        _t.start()
        _t.join(timeout=round_timeout)
        if _t.is_alive():
            log(f"⏱️ 本轮超过 {round_timeout}s 未结束，正在发送停止信号并回收后台线程...")
            try:
                (Path(task_dir) / ".stop").write_text("1", encoding="utf-8")
            except Exception:
                pass
            _t.join(timeout=30)
            if _t.is_alive():
                log("⚠️ 后台线程 30s 后仍未退出（可能卡在子进程），本轮结果作废，不再重试")
            result = {"total": 0, "fetched": 0, "errors": -2, "error": f"运行超时（>{round_timeout}s）"}
            last_result = result
            last_log = log_file.read_text(encoding="utf-8", errors="replace") if log_file.exists() else ""
            break
        try:
            result = _box.get("r") or {}   # 线程内 run_task 抛错时兜底，避免二次崩溃
            last_result = result
            last_log = log_file.read_text(encoding="utf-8", errors="replace") if log_file.exists() else ""
        except Exception as e:
            result = {"total": 0, "fetched": 0, "errors": -1, "error": str(e)}
            last_result = result
            last_log = log_file.read_text(encoding="utf-8", errors="replace") if log_file.exists() else str(e)
        items_path = ROOT / "outputs" / "items" / f"{name}.jsonl"
        sample = []
        if items_path.exists():
            # 从文件末尾读（最新写入在末尾），最多回看 500 行取最新 5 条
            _lines = items_path.read_text(encoding="utf-8", errors="replace").splitlines()[-500:]
            for line in reversed(_lines):
                if line.strip():
                    try:
                        sample.append(_json.loads(line))
                    except Exception:
                        pass
                    if len(sample) >= 5:
                        break
            sample.reverse()
        META = ("_url", "_parser", "_ts", "_id")
        real = [it for it in sample if any(str(it.get(k) or "").strip() for k in it if k not in META)]
        _miss = _missing_key_field(description, sample)
        _intent_bad = ""
        if result.get("total", 0) > 0 and real and not _miss:
            _intent_bad = _intent_check(description, sample, log)
            if not _intent_bad:
                log(f"✅ 第 {round_i} 轮成功：{result.get('total')} 条（抽样 {len(real)} 条有真实字段）")
                break
            log(f"⚠️ 意图校验未通过：{_intent_bad}（抓到的不是用户要的内容，进入自修复换入口）")
        if _miss:
            log(f"⚠️ 第 {round_i} 轮 total={result.get('total')} 但用户关键字段【{_miss}】为空，视为失败，进入自修复（需要详情页补抓）...")
        elif result.get("total", 0) > 0 and not real:
            log(f"⚠️ 第 {round_i} 轮 total={result.get('total')} 但全是空壳字段，视为失败，进入自修复...")

        # 🛡️ WAF 滑块拦截 → 确定性自动升级浏览器模式（不靠 LLM 猜）
        # 注意：即使本轮有真实行，只要 WAF 拦截导致关键字段缺失/详情失败，也必须升级
        _blk = (result or {}).get("block_stats") or {}
        if _blk.get("waf") and ((config.get("source") or {}).get("type") == "http"):
            log("🛡️ 检测到 WAF 滑块验证（CWAP/wzws），自动升级为浏览器模式——请在弹出的浏览器窗口中完成滑块拼图，完成后自动继续...")
            config = _force_browser_waf(config)
            continue

        # 🧠 改造3：失败轮内先对真实页面证据（渲染页/捕获接口）做 LLM 结构化抽取，成功即收
        if not _llm_ev_tried:
            _llm_ev_tried = True
            _ev = _task_evidence_summary(td)
            if _ev.get("html") or _ev.get("capture"):
                log("🧠 常规解析未命中，先对真实页面做 LLM 直接抽取（成功即收，不空转重跑）...")
                fb = _llm_extract_from_evidence(description or "", config, td, log, ev=_ev)
                if fb.get("items"):
                    sample = fb["items"][:5]
                    files = fb["files"]
                    last_result = dict(last_result or {})
                    last_result["total"] = fb["total"]
                    last_result["llm_extract"] = True
                    log(f"✅ 第 {round_i} 轮 LLM 直接抽取成功：{fb['total']} 条")
                    break

        if round_i < rounds:
            log(f"⚠️ 第 {round_i} 轮 0 条/报错，AI 正在自修复...")
            fix_messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": (
                    f"任务：{description}\n"
                    f"上次配置：{_json.dumps(config, ensure_ascii=False)}\n"
                    f"运行结果：{_json.dumps(result, ensure_ascii=False)}\n"
                    + (f"意图校验未通过：{_intent_bad}\n**这是入口选错，不是选择器问题**：必须换到目标资源类型对应的列表页（如论坛的 MOD发布区/资源下载区/下载频道），不要只改选择器；用户没给入口就向用户要正确入口 URL。\n" if _intent_bad else "")
                    + f"{_page_context((config.get('start_urls') or [''])[0]) if config.get('start_urls') else ''}\n"
                    f"{_annotated_dom_hint(task_dir, (config.get('start_urls') or [''])[0])}\n"
                    f"运行日志（末尾）：\n{last_log[-2000:]}\n\n"
                    f"{_rendered_class_hint(task_dir)}\n"
                    f"【选择器修正要点】上面【重复块候选】是真实渲染页里带 CSS 选择器标注的 DOM："
                    f"完整CSS 就是 row_css，实例里的相对选择器就是 fields 的 css。"
                    f"请严格按它修正 row_css 和 fields，不要凭空猜选择器。"
                    f"注意：若日志提示【用户关键字段缺失】，说明列表页没有该字段，"
                    f"必须在 config 里加 detail 配置（url_field 指向列表记录中的详情 URL 字段，"
                    f"url_transform.prefix 补全域名，extract 抓详情页字段，filters 做日期过滤）。\n"
                    f"请修正配置（选择器/网址/解析方式/加 detail 等），只输出修正后的完整 config.json。"
                )},
            ]
            try:
                config = _extract_json(_llm_chat(fix_messages))
                config = _validate_and_fix(config, description, log)
                config["name"] = name
                config["storage"]["name"] = name
                config["output"]["base_name"] = name
                (td / "config.json").write_text(_json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception as e:
                log(f"❌ 自修复生成失败: {e}")
                break

    files = {}
    for ext in ("json", "csv", "xlsx"):
        fp = ROOT / "outputs" / f"{name}.{ext}"
        if fp.exists():
            files[ext] = str(fp.relative_to(ROOT))
    META = ("_url", "_parser", "_ts", "_id")
    total = (last_result or {}).get("total", 0)
    real = [it for it in sample if any(str(it.get(k) or "").strip() for k in it if k not in META)]

    if not _precise_done and not _precise_attempted:
        try:
            su = (config.get("start_urls") or [""])[0]
            from .sites import match_site, run_site
            _site = match_site(su)
            if _site:
                _ch = ((config.get("source") or {}).get("headers") or {}).get("Cookie") or ""
                _px = (config.get("anti_bot") or {}).get("proxy") or ""
                _dr = run_site(su, cookie=_ch, proxy=_px or None, limit=int(limit or 20),
                               out_name=name)
                if _dr.get("rows"):
                    _lim = int(limit or 20)
                    _rows = _dr["rows"][:_lim]
                    sample = _rows[:5]
                    files = _dr["files"]
                    total = min(int(_dr["total"] or 0), _lim)
                    real = sample
                    log(f"🏆 精配解析[{_site}]覆盖：{total} 条（字段干净）")
                elif _dr.get("error"):
                    log(f"⚠️ 精配解析[{_site}]失败：{_dr['error']}")
        except Exception as _e:
            log(f"⚠️ 精配解析未启用：{_e}")

    if not (total > 0 and real):
        log("🧠 常规解析未命中，尝试 LLM 直接抽取（兜底）...")
        _rendered = ""
        _rurl = ""
        try:
            _lp = Path(task_dir) / "last_page.html"
            if _lp.exists() and _lp.stat().st_size > 2000:
                _rendered = _lp.read_text(encoding="utf-8", errors="replace")
                _rurl = (config.get("start_urls") or [""])[0]
        except Exception:
            pass
        fb = _llm_fallback_extract(description, config, log,
                                   rendered_html=_rendered or None, rendered_url=_rurl)
        if fb.get("items"):
            sample = fb["items"][:5]
            files = fb["files"]
            last_result = dict(last_result or {})
            last_result["total"] = fb["total"]
            last_result["llm_fallback"] = True
            total = fb["total"]
            real = sample
            log("✅ LLM 兜底成功，任务视为完成")

    verify = None
    try:
        from .verify import verify_rows
        verify = verify_rows(sample, config, sample_n=0, network=False,
                             declared=total or len(sample))
    except Exception:
        verify = None

    if total > 0 and real:
        vtxt = ""
        if verify and verify.get("checks"):
            bad = [c["name"] for c in verify["checks"] if not c.get("pass", True)]
            vtxt = ("｜复核 ✅ 通过" if not bad else "｜复核 ⚠️ " + "；".join(bad))
        summary = f"✅ 任务结束：成功 {total} 条（抽样 {len(real)} 条有真实字段）{vtxt}{_font_obfuscation_hint(real)}，导出 {list(files)}"
    else:
        reason = _diagnose_failure(config.get("start_urls"), last_result, log)
        summary = f"⚠️ 任务结束：0 条。原因诊断：{reason}"
        log(summary)
    return {
        "name": name,
        "config": config,
        "result": last_result,
        "log": last_log[-3000:],
        "sample": sample,
        "files": files,
        "summary": summary,
        "verify": verify,
        "done": True,
    }


def run_auto_cli(description: str, limit: Optional[int] = None) -> Dict[str, Any]:
    lines = []

    def log(msg):
        lines.append(msg)
        print(msg, flush=True)

    try:
        out = auto_task(description, limit=limit, log_cb=log)
        out["messages"] = lines
        return out
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "messages": lines}
