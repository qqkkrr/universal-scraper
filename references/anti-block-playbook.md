# 反爬升级手册（实战检验版）

> 每一级都要向用户播报一句进展。升级不丢人，卡住不说才丢人。

## 一、判型表（侦察后 30 秒内定级）

| 症状（看 fetch 侦察结果） | 判型 | 起手级 |
|---|---|---|
| **挑战壳引用 `/_fec_sbu/fec_wrapper.js` + `hxk_fec_*.js`（非 `$_ts`），HTTP 通道 406；真浏览器能过但**每次完整导航后 1-2 次内即出「Sorry, you have been blocked」+ Request-ID 封锁页，冷却约 10 分钟** | **FE 前端挑战 + 导航频控 WAF（wcjs.sbj.cnipa.gov.cn 实测 2026-09）** | L3 真实 Chrome 一次过；**每冷却窗口只做 1 次导航，把"点菜单→填条件→查询→capture_all"全部塞进同一会话动作链**；活页内 XHR 交互不重复触发；封禁窗口内连静态 /js/*.js 都 403 |
| 200 且内容齐全在 HTML 里 | 直接可抓 | L0 |
| 页面是 JS 应用（有界面壳、数据靠 XHR 拼，如 EUIPO eSearch） | SPA 应用 | **接口捕获（capture_all）优先**，浏览器只留交互 |
| **页面本身被拦/是壳，但站方另有公开数据接口**（排行榜/公告/搜索的 JSONP、RSS、open API，不校验 UA/签名/登录） | **公开 API 直通型** | **L0 直通：先找接口再谈反爬**——`jsrecon`／`capture_all` 找端点，或直接查站方 open API/RSS（实战例：天天基金 `data/rankhandler.aspx`、中彩网 JSONP、Crossref/OpenAlex 开放接口）。命中时**不要**升级浏览器/代理；只有接口字段不够时才回退页面路线。判定要点：**接口能用就不算"被阻断"**，别把页面拦截误判成任务障碍 |
| 返回"Just a moment"/Turnstile 挑战页；headless 也被卡；偶发 ERR_CONNECTION_CLOSED | Cloudflare 类 | 直接 L3：真实调试 Chrome 过一次校验 → 配置 cdp 附加（R16），headless 别硬试 |
| **首页/老路径 302 → `captcha.eo.qq.com`/`captcha.eo.gtimg.com` 加载 TEOCaptchaWidget（腾讯云 EdgeOne），CDP 真 Chrome 也弹验证码；`diagnose` 可能只看到 302** | **腾讯云 EdgeOne 验证码关卡（kaijiang.500.com 实测 2026-09）** | **人工关卡，L4 人机协同；自主任务无人在环时直接换源**（同任务换官方行业中彩网 jc.zhcw.com JSONP 接口完成交付），别程序绕 |
| 403 / 412 / 468 / 503，或正常 UA 也被拒 | WAF 指纹拦截 | L1 |
| **412 + 页面是 `$_ts=window['$_ts']...` 混淆 JS（响应头 Server 打码）** | **瑞数(RiverSecurity)动态防护** | **直接 L3 真实 Chrome 一次过；别在 L1/L2 浪费尝试（NMPA 实测：curl_cffi 全通道 412，CDP 真浏览器秒过）** |
| **瑞数站：HTTP 状态 202 + `$_ts` 挑战壳但 `diagnose` 误判"正常响应"；运行一段时间后 GET 详情仍通、POST 搜索开始返回 400/6B 空体，随后整站挑战循环（页面 39B 空壳反复）；换全新浏览器 profile 立刻重试无效（IP 级软封锁）** | **瑞数 POST 行为限流（epub.cnipa.gov.cn 实测 2026-09）** | L3 真实 Chrome 过一次挑战；**发现 400 空体立即停手冷却 2-10 分钟**，别重试硬闯；恢复后先走 GET 详情类轻通道，搜索 POST 限 1 次/分钟级 |
| **首页 200 正常，但搜索接口间歇 412「努力加载中」+ `CT_*` cookie + `ctct_bundle` JS；同会话连搜 2-3 次后变 200 空页；换 IP/换指纹几分钟内再次触发** | **CTCT 行为评分（gsxt 实测 2026-09）** | L3 真实 Chrome + 首页探针抓放行窗口；**每窗口只搜一次**；文字点选码 ddddocr det+cls 自动点，图标点选码留人工；详情页/翻页不吃配额，窗口内尽量多收 |
| 200 但 body 极短（<2KB），含 `document.location`、`document.write`、`stoken`、`__js_challenge`、`setTimeout(...location...)` 之类脚本壳 | JS 挑战壳 | L1 → L2 |
| **650B 壳页 `<meta id="zh-zse-ck">` + "知乎，让每一次点击都充满意义"；裸 curl 与 curl_cffi 全 403；真浏览器可过，但未登录问题页只渲染 SSR 首批 ~9-15 条回答，滚动不发任何翻页请求（前端根本不发，不是被拦）** | **知乎 zh-zse-ck 挑战 + TLS 指纹 + 未登录列表硬墙（2026-09 实测）** | L2 **有头** patchright 持久 profile 加载即过（headless 会被"安全验证"页拦）；回答列表**必须登录**（用户扫码一次，易盾滑块人工在环）；登录后 feeds 链头 cursor 由页面加载时自发一次（响应监听器须挂在 goto 之前），页内同源 fetch 重放 cursor 链免签名——动线见 R41 |
| **GraphQL 端点可达（改字段名返回正常校验错误）但请求被 `Need captcha`；或 `Unknown operation named "x"`** | **缺浏览器风控头 / operationName 是白名单** | **借浏览器上下文同源 fetch**（不逆向签名，配方 R48）；`operationName` 与 query 文本从页面 XHR 抄 |
| 返回验证码图片 / 滑块 / 点选 | 验证码 | L2 + 识别，失败 L4 |
| 提示登录 / 跳登录页 / 关键字段空且需会员 | 登录墙 | L4（唯一正路） |
| HTML 干净但没数据（列表空） | 数据走接口 | 接口捕获 |
| 前 N 条成功，之后 429/超时/空页 | 限流 | 降速 + L1 |
| 换任何方式都返回同一个短壳（如 gov.cn 1378 字节壳） | 全通道防护 | L5 人工通道 |
| **CDP 附加真浏览器里，goto 后页面活几秒到几十秒突然自己关掉（page close 事件，无崩溃无报错），重开又偶发** | **站方 JS 反自动化：检测到 CDP/自动化后调 `window.close()` 关标签** | 导航前 `context.addInitScript` 把 `window.close` 置空（不碰签名/登录，只保活标签页），采集循环里页面被关自动重建重试（小红书 2026-09 实测） |
| **wbi 签名接口纯 HTTP 报 -352 风控，加齐 buvid3/4 + 真实 Referer 后变 -412/412；请求参数里带 `dm_img_str`/`dm_cover_img_str`/`dm_img_inter` 行为指纹（WebGL 串 base64）** | **B站空间/动态系 wbi + 行为指纹双校验：指纹参数值本身参与风控评分，随机伪造必挂** | 浏览器跑一次目标页用 capture_all 录下**真实 dm_img_* 参数值** → HTTP 侧保留原值只重算 wts/w_rid 后直抓（指纹串非会话绑定，可复用）；全程浏览器只值一次（影视飓风 UP主战役 2026-09 实测：随机 dm_img_str 稳定 -352→412，真实值纯 HTTP 100/100 页通） |
| **数字/汉字显示正常但 DOM 里 charcode 乱码，CSS `::before` 注入字形；接口 JSON 正常无反爬** | **HS CSS 伪元素字体反爬 + CSS 偏移（汽车之家 2026-09 实测）** | 接口 JSON 数据不经渲染层——直接用口碑/配置 API 的 JSON 数据绕开字体反爬；必须用渲染页时用 Playwright 截图 + OCR 对照逆推映射表；`koubeiipv6.app.autohome.com.cn` 口碑 API 正常 |
| **接口 code=0 但数据全空（`items:[]`、`total:0`、`has_more:false`），真浏览器同样空** | **EMPTY_200 软封锁/匿名配额，不是"站点无数据"** | 换身份验证（登录态/另一 IP）再判 0 结果；B站动态 feed/space 匿名恒返回空集，勿当 nodata 结案 |
| **评论接口无论 pn 翻页/游标/换排序都只回 3 条，游标直接 `is_end:true`，而 `all_count` 显示几千** | **评论可见性登录墙（匿名硬配额），非风控非翻页深度问题** | L4 唯一正路：用户登录一次 → `cookies` 导出复用；勿试图用排序/分页参数绕过（B站 2026-09 实测：wbi/main、legacy、mode=2/3 全部 3 条封顶，热评头 3 条 + is_end 是设计行为） |
| **HTML 正常 200、HTTP 无任何拦截，但文本字段位置是空占位符 `<span class='hs_kw7_随机后缀'></span>`（后缀 configGd/optionOQ/nakedPrice0Jc 等每次请求随机）；页面无 .ttf/.woff 字体请求；markdown 转换后字段空白** | **汽车之家 HS 反爬：乱序 JS + CSS 伪元素注入（CSS 偏移家族现行形态；早期字体混淆已升级为此方案，无字体文件可下）** | **别逆向表达式、别 vm 执行页面脚本（安全钩子也不允许）——headless 真渲染后读计算结果**：`document.styleSheets` 里翻 JS 注入的 `.hs_kw7_后缀::before{content:"真实字符"}` 规则（部分元素走 innerHTML 回填，两者都收）建「后缀#索引→字符」映射，再对 JSON/HTML 里的 span 占位符做替换；route 屏蔽 image/media 提速但**不得屏蔽 stylesheet**；`performance.getEntriesByType("resource")` 探字体文件确认无字体路线。注意 evaluate 传字符串时表达式返回函数本身需显式 `(...)()`。配置/成交价/口碑共用此机制，一法通吃（汽车之家 2026-09 实战：83车系/337车型/7058成交价/909口碑全程无封） |
| **PC 与移动端详情页 HTTP 直取都是 11.7KB SPA 空壳（多 ID 返回同样大小）；PC 电影页评分数值明文为空 `<span class="num"></span>`，只有 `MaoYanHeiTi-DemiBold.otf` 渲染；PC 端 headless 访问被重定向到 passport 登录页（手机号+短信验证码）** | **猫眼电影：SPA 壳页 + 自定义字体反爬 + H5guard 设备指纹 + PC 登录墙（2026-09-30 实测）** | 主接口 `m.maoyan.com/apollo/apolloapi/review/v2/comments.json`（**无签名无登录**，limit≤20，ts 游标）直连；**评分绕开字体反爬走移动端 SSR**（`m.maoyan.com/movie/{id}` 直取 149KB，`sc`/`distribution` 全明文，勿上 OCR）；PC 路径 `/short-comments`、`/hot-responses` 全是壳页必须走移动端。详见 recipes R46 |

<!-- 审查八轮（渲染修复）：以下 2026-09 新判例原为"无表头的裸表格行"，紧接上一张表
     且隔了空行 → GFM 解析器当普通段落、识别不到表格（手机上看到的是管道符长句）。
     补一行表头+分隔行使其各自成表；内容一字未改（判型表只增不删）。 -->

| 症状（看 fetch 侦察结果） | 判型 | 起手级 |
|---|---|---|
| **门户整站迁移新 SPA（`/dg/website/page.html#...`，响应头 `WZWS-RAY`），旧数据接口（easyquery.htm）任何通道（含真浏览器同源 fetch）一律 403 `reason:UrlACL`；新接口 `/dg/website/publicrelease/web/external/*` 裸 HTTP 带 UA+Referer 即通、无签名无验证码** | **gov 门户改版：老接口 UrlACL 整体封死，别恋战；抓新接口** | L0：浏览器 capture_all 数据页录出真实 XHR（树/指标/数据三件套）→ 纯 HTTP 重放；注意"版别目录"机制——历史数值必须按目录 sdate/edate 推时间窗查，宽窗只回填近期数值（国家统计局 data.stats.gov.cn 实战 2026-09，配方 R38） |
| **站方/教程声称"反爬极强"，但同域存在未加密的公开 API 变体（如网易云 weapi/eapi 加密而 `/api/v1/*` 评论/用户接口明文直通）；带 UA/Referer 的裸 HTTP 直接出数据** | **"标称强反爬"≠真强：先探公开 API 变体再升级** | 起手 L0：先试同域"另一套"端点（`/api/v1`、legacy、H5/移动端前缀、open 接口）再考虑捕获/浏览器；命中即纯 HTTP 直抓（R44 网易云 1039 请求零封锁）。反例：B站 wbi 签名、小红书登录墙是真拦（R34/R40），探一次失败就按各自配方走，别反复试变体 |
| **HTML 表格渲染正常、有日期有 AQI 有质量等级，页面里还藏着多张同列名 `display:none` 干扰表；同一页面两次抓取，污染物数值列不同而日期/AQI/等级不变** | **aqistudy 式"随机值干扰表"：污染物列每次请求随机生成仅供展示，不是数据** | 别信该表数值列（排列检验都救不回来）；用官方存档替代（配方 R39）；其日期/AQI/等级列真实，可作跨源核对锚点 （www.aqistudy.cn 实战 2026-09，双抓对照实锤） |
| **查票接口返回 302 到相对路径 `queryG?…`（endpoint 动态轮换，非封锁）；旧票价接口 200 但 `queryLeftNewDTO` 为空（已下线）；过去日期/超预售期返回 200+HTML 错误页（title「铁路客户服务中心」）** | **12306 余票接口端点轮换（所谓"动态签名"实为 302 指路）+ 日期口径不可查** | L0 即可：先 GET `/otn/leftTicket/init` 预热 Cookie（JSESSIONID/BIGipServerotn/SF_cookie_2）→ 手动逐跳跟随同域 302（每跳复检白名单，禁 auto-redirect）；票价+余票数在 result 行字段[39]，**官方前端 `queryLeftTicket_end_js.js` 的 `e()` 函数就是现成解码器（每10字符=席别码1+价格×10共5位+余票数4位），勿自己猜格式**（12306 实战 2026-09） |
| **未登录访问 www.xiaohongshu.com 任何路径都渲染"安全限制 300012 IP存在风险"整页（登录页也封）；同 IP 换浏览器/profile 无效；curl 无 cookie 访问同样封** | **小红书 IP 信誉封锁（300012），先于登录态生效——不是 cookie/指纹问题** | 换出口 IP（用户本机代理/家宽→代理节点）；**Chrome `--proxy-server` 实测会被无视（指到无人监听的假端口都能上网）**——用 patchright/playwright `launchPersistentContext({proxy})` 库层代理验证出口后再开工（小红书 2026-09 实测：移动家宽 300012 封锁，香港出口正常 302 登录墙） |
| **采集中途页面突然跳转 `website-login/error?...error_code=300013&error_msg=访问频繁，请稍后再试`** | **小红书频率限流（300013）：短期高频打开笔记页/评论翻页累积触发** | 立即收手冷却 5-10 分钟，勿重试硬闯；后续会话导航间隔 ≥3-5s、评论区滚动轮次按需收敛；数据已捕获部分照常解析交付，缺的等冷却后补（小红书 2026-09 实测） |
| **采集能拿到 `window.__INITIAL_STATE__` 的站，运行时 `Object.getOwnPropertyNames(window)` 里却没有它（SSR HTML `<script>` 里有）** | **水合后删除全局状态（React/Vue SSR 消费即删）** | 别读运行时全局——直接从捕获的 SSR HTML 正文提取 `window.__INITIAL_STATE__=` 后面的字面量；注意它不是纯 JSON：`\bundefined\b`→null、`NaN`→null、`new Set(X)`/`new Map(X)`→X（配平括号替换），`</script>` 查找边界会被 JSON 内嵌 script 字符串截断，用花括号配平法取完整 blob（小红书 2026-09 实测） |
| **拼多多 H5：搜索接口 POST `/proxy/api/search` 回 `error_code 54001`+verify_auth_token（页面弹滑块/拼图）；新号继续自动化导航后升级为全站跳 `psnl_identify.html?scene=COMMON_VERIFY`（"请前往APP完成人脸认证"，**连商品详情页都拦**）；分类页滚动静默触发 `/proxy/api/api/phantom/obtain_captcha`；但**首页信息流/分类浏览/登录后的商品评论页长期正常** | **拼多多账号级行为风控：搜索面最敏感，新设备+程序化节奏从滑块逐级升到人脸墙（升级不可逆，小号即废）** | 换有历史的账号 + **用户手工暖号**（搜索→进商品页→翻评论全程真人节奏）；自动化阶段只做页内滚动与响应捕获，**不发新导航风暴**（商品页一次探测即可暴露墙）；评论采集走 `goods_comments.html?goods_id=X`（拼多多 2026-09 实战，动线见 R43） |
| **直连 403 Cloudflare 盾；换真浏览器渲染后 HTTP 200，但整页正文只剩一句 "Please login to continue / Please log in to verify you are not a bot / If you are looking to access data through our API, please visit our developer portal"；官方 API 门户（developer.stockx.com）只提供 Seller / Catalog / Order 类接口，面向卖家与企业** | **商业行情平台闭源化：CF 盾 + 登录墙双闸 + 官方 API 指向企业侧（StockX / GOAT 实测 2026-09；GOAT 连 robots.txt 都返回 CF 挑战页，商品页探测 404）** | **别升 ladder——登录墙是红线，升到 L3/L4 同样过不了，纯粹浪费用户时间**。直接换源三选一：①**已发表二手数据集**（学术首选，如 IEEE DataPort DOI `10.21227/mdj8-4y59`「StockX Sneaker Size-Day Dataset」，136,980 条 / 50 款鞋 / 2025-05~09 日频，颗粒度 鞋×尺码×天，含 lowest ask / highest bid / last sale + Google Trends score，DOI 可直接引用）②**平台官方 API 或数据合作邮件申请**（说明学术用途 + 承诺不公开原始数据、只发表聚合统计量）③商业托管数据服务（Apify / WebScrapingAPI 等，**外包爬取不转移法律风险，慎作论文主数据源**） |
| **Web 端 SPA 壳可通（200），但 `jsrecon` 扫完全部 JS 包后**零数据端点**（只剩备案 PDF 之类的静态链接）；真实数据接口在 App 端且带 native 层签名** | **App 端闭源站（得物 dewu.com 实测 2026-09）：Web 侧根本没有接口可抓，取数必须逆向 App 签名 + 伪造设备指纹 + 自动过滑块 + 解密混淆字体** | **四项全部落在合规红线内（不逆向签名 / 不伪造指纹 / 不程序过滑块），直接拒单，一次都别试**。换源或改研究设计；学术场景优先已发表数据集或官方数据合作申请 |
| **SSR 站：`fetch` 直抓只出几行壳文本（markdown 转换器扑空），但 `grep` 原始 HTML 能命中全部字段关键词；`jsrecon` 挖到 0 个端点（数据内联不走 XHR）；`--browser` 渲染能拿到完整正文** | **SvelteKit/流式 SSR 站：数据在 HTML 里但 lxml 把它解析成**两个 `<html>` 元素**——`html[0]` 含 `<head>`（canonical/title），`html[1]` 含 `<body>`（h1/价格/正文）。两者是兄弟，同一行内拿不全（KLEKT klekt.com 实测 2026-09）** | **别用 `row_xpath:"//html"` 一把梭（会命中 2 个元素 → 每页产出 2 行、其中 1 行全空，且 canonical 与 h1 分属不同行导致 url 恒空）**。正解双层：`source.row_xpath:"(//html)[1]"` **只提 url**（`css:"link[rel=canonical]"` + `attr:"href"`）→ `detail.enabled:true` + `detail.extract[{"type":"xpath_text",...}]` 提全部字段。**关键差异：`detail.extract` 的 xpath 作用于完整 HTML（head+body 都能访问），`source.fields` 的 xpath 只作用于单个 row 元素的序列化** |
| **sitemap 文件名带 `.gz`，但 `gzip.GzipFile` 解压报 "Not a gzipped file"（内容直接是 `<?xml`）** | **服务器已按 Accept-Encoding 自动解压，文件名 .gz 名不副实** | 先嗅探魔数再决定：`raw[:2] == b"\\x1f\\x8b"` 才走 GzipFile，否则当明文解码（KLEKT sitemap 实测 2026-09） |

⚠️ **接单前先确认字段在目标站是否真的存在**（得物/StockX 战训 2026-09 双踩）：
球鞋转售类平台（StockX / GOAT）**非 UGC 平台，没有买家评论系统、没有用户主页 /
粉丝数 / IP 属地**。任务书若含「评论 N 条 + 用户主页 N 个」，换任何合规源都拿不到，
**必须在开工前告知用户并砍需求**，不要抓完行情才发现两块数据为零。
另：StockX 只有聚合统计（salesLast72Hours / salesCount90Days / averagePrice90Days），
**没有逐笔成交明细**，也没有「卖家数量」「最近成交时间」的等价字段——
别把「近 30 天成交记录」当成可抓的逐笔流。

💡 **球鞋转售行情的可抓替代源：KLEKT（klekt.com，欧洲）**（2026-09 实测通关）：
`robots.txt` 明确 `Allow: /product`、`Allow: /catalog`，**HTTP 直抓即可，无任何防护**。
商品 URL 从官方 sitemap 取（`klekt.com/sitemaps/products-0001.xml.gz`、`-0002`，
合计约 1.8 万条；其中 Dunk Low 约 823 条）。
可拿字段：SKU / 配色 / 发售日期 / 成色 / 最低挂单价+尺码 / 最高出价+尺码 /
最近成交价 / 12 个月成交笔数 / 12 个月均价 / 价格波动率 / 12 个月最后成交价。
配 `source.sitemap_urls` 批量喂种子 + 上文双层 xpath 方案，40 款约 80 请求 / 192 秒 / 0 错误。
⚠️ 同样**没有买家评论与用户主页**；冷门款无成交时历史区块整个不存在（空值≠抓取失败）。

**数据型任务 API 优先（batch1700 实测：400 项中约 60% 的关键数据在 JSON API 里，
HTML 抓取反而是简单情况）**：凡目标是"数值/行情/名单/统计"类数据，侦察第一步
不是看页面结构，而是找接口——`jsrecon`（看 base_urls 锚点）→ `capture_all`
（连 POST 体一起录）→ `capture2config` 一键生成可重放 http_json 配置。
动线详见配方 R23。先写 CSS 选择器再发现数据在接口里，是最常见的返工原因。

⚠️ **capture-first 的例外：验证码把门的数据接口**（gsxt 战训 2026-09）。
如果数据接口本身被验证码/行为评分把门（请求发不出去、或发出即烧配额），
**直接走浏览器 + 人机协同，别浪费时间捕获**——接口请求本身过不了，录不到任何东西。
用 `cli captcha`（CDP 附加真 Chrome，OCR 自动点选文字码/人工在环点图标码）+
`budget --probe` 探放行窗口 + `budget --action` 给贵动作记账（配方 R30）。

## 二、升级阶梯 L0 → L5

**L0 · HTTP 直抓**（默认）：`fetch <url>` / 配置 `type: http_html`。
最快最礼貌，能用它就不升级。

**L1 · 浏览器指纹**：用 curl_cffi 伪装真实 Chrome 的 TLS/HTTP2 指纹。
配置 `type` 换 curl_cffi 通道，或直接 `adaptive` 自适应引擎（自动试到通为止）。

**L2 · 无头浏览器**：真 Chromium 渲染。`fetch --browser`，或配置里
`pagination/wait/scroll_count` 全量支持（见 spec-schema）。
适合 JS 壳、动态列表、无限滚动。

**L3 · 附加用户已登录的 Chrome（CDP）**：
`bash "${SKILL_DIR}/scripts/open-debug-chrome.sh"` 弹独立调试窗口（端口 9222，
独立配置目录，不影响用户日常 Chrome）。用户登录一次后，配置加
`"cdp": "http://127.0.0.1:9222"` 即可在他的登录态里抓。
话术：*"我弹出一个浏览器窗口，请你像平常一样登录一次，好了说一声，之后不用你再管。"*

**L4 · 人工过一次关卡 + 复用**：
- 登录：同 L3。
- 滑块/验证码：配置 `captcha`/`slider` 段自动识别（ddddocr/轨迹模拟）；
  自动识别连续 2 次失败就转人工——弹浏览器让用户滑一下，会话状态保存后复用。
- 登录态导出：`cookies` 命令把浏览器登录态导成 Cookie 串，之后走 L0/L1 轻量直抓。
- **登录不可得时的分支（商标网战训 2026-09）**：用户无法提供登录态
  （无账号/不愿给/企业实名墙）≠ 任务死刑。三步分支：
  ① **WebSearch 找同一数据实体的替代源**（R24 从"数据源未知时"前置到这里——
  政府数据几乎总有镜像/第三方聚合/官方开放数据集）；
  ② 替代源拿到数据后，**用原任务的 schema 做同构验证**（verify --expect），
  确认字段语义一致才入库；
  ③ 替代源也没有 → 回到 L5 诚实告知边界。禁止"绕登录墙"的红线不因任务
  压力放松。

**L5 · 诚实告知边界**：全通道都不通时（典型：gov.cn 对数据中心 IP 全量返回
1378 字节 JS 壳，真人浏览器才放行），明确告诉用户：
> 这个站对所有自动化访问都关了门，只放行真人浏览器。两个选择：
> ① 你在自己浏览器里打开这个页面，Cmd+S 另存到任务文件夹，我来解析；
> ② 我先把其余部分全部抓完，这几个文件留到最后人工补。

**⚠️ 先确认是真 L5 还是瑞数**：gov.cn 门户的 1378B 壳是 L5；但 412 + `$_ts`
特征是瑞数防护（NMPA 战例），走 L3 真实 Chrome 就能过——别把可自动化的站
推给人工。

绝不用"抓到了"掩盖没抓到，绝不伪造内容顶数。

## 三、限流与礼貌（预防 > 治疗）

- **小日配额预警**（epub 战训 2026-09）：政府站搜索类 POST 的配额可能是
  **个位数/天**（实测 6-10 次/日）——"站点变慢再收手"的渐变信号根本来不及
  出现，第一轮探路请求就可能烧完全天额度。**政府站搜索 POST 在证明额度
  充裕前，一律按个位数日配额假设行事：先打一发探路、确认返回形态，
  再谈放量**。若已锁死，评估详情 GET 通道是否不吃配额（→ 配方 R31 号段枚举）。
- **请求预算概念**（强验证站点通用规律）：验证类 cookie（滑块/身份核实发的）不是
  永久通行证——它有请求预算，用完（如约 140 次请求）验证重新触发。预算将尽的
  信号：连续请求开始返回验证页。此时回 L4 重新过验证，或全程改用浏览器导航
  （会话内风控更宽松）。
- **HTTP 与浏览器会话连坐**：同一 IP 上 HTTP 批量爆发会把正在正常工作的浏览器
  会话一起封。强风控站点历史采集全程浏览器单线程导航，不用 HTTP 并发。
- **同接口换参数要对比校验**（小米有品战例）：pageSize 从 20 改 100 后评论字段
  静默置零这类"参数换挡丢字段"很常见——翻页参数变更后必须抽查首末页字段完整性，
  与小样对比。
- 配置里加请求间隔（1～3 秒起），列表页翻页加随机抖动。
- 被限流后：立刻降速 ×2、降并发到 1；连续 3 轮 429 → 换时段再跑。
- 大任务建议 `proxy` 命令建免费代理池（仅用于公开数据、遵守目标站条款）。
- `crawl` 加 `--robots` 尊重站点声明。

## 四、接口捕获（数据不在 HTML 里时）

配置 `capture: true` + `record_from: capture_all`：浏览器方案会顺带录下页面
发出的所有 JSON 响应，落盘 `capture_all.json`。你（agent）读该文件，
找到目标数据的字段路径，回填到配置的 `records_path`/字段映射里再跑。
签名接口（参数带 sign/stoken/token 且你无法推导）→ 不逆向，走 L3 登录态直抓。

⚠️ **capture 只录 run 生命周期内的响应**。SPA 的数据 XHR 往往在页面加载完之后
异步触发（如 EUIPO eSearch），空等只会捕到配置/认证类响应——必须配
`"actions": [{"type": "wait", "ms": 10000}]` 这类等待让 XHR 有时间发出；
需要点击/翻页才出的数据，就把点击写进 actions 再捕获。
⚠️ **跨域 XHR 的响应体录不到**（CDP 限制，闲鱼 goofish mtop 实测：raw 全空）——
遇到跨域 mtop 接口别在 capture 路线上空转，直接走浏览器 DOM 提取或
`detail.backend: "browser"`（配方 R18）。

⚠️ **加密/密文响应（商标网战训 2026-09，红线）**：接口返回密文/二进制
（capture_all 拿到乱码）——服务端对响应做了加密（Angular 站常见）。
**禁止逆向解密**（合规红线 + 投入产出比极差）。两条正路：
① **读渲染后的 DOM**——数据总要以明文呈现给用户，`rows_css` 解析渲染结果；
② **找官方导出按钮**——很多查询系统自带 Download/Export（branddb 的
Download results 就是），点它拿官方格式的全量数据，比逆向省一个数量级。

⚠️ **SPA 框架表单驱动降级阶梯（商标网战训 2026-09）**：Angular/React 组件
`fill()` 合成事件不触发（自动补全不展开、model 不更新）、`pressSequentially`
也可能无效。按阶梯降级，每级试完再下台阶：
① `fill`（合成事件，多数站够用）→ ② `type_real` 动作（真实键盘事件：
点击聚焦 + 逐键输入，触发完整 keydown/input 链）→ ③ OS 级辅助功能驱动
（keyboard/type API 之外，如 macOS AX——工具层未封装，需 agent 现场写）→
④ **放弃 UI 驱动，直接读渲染后的 DOM**（终极兜底：URL 可构造时逐页
goto + rows_css 解析）。

## 五、实战档案（真实案例，照方抓药）

| 站点 | 症状 | 有效方案 |
|---|---|---|
| gov.cn 政策文件 | 全通道 1378B JS 壳 | L5：真人浏览器另存 + 你解析 HTML |
| 大众点评 | csec 验证码 + 接口加密 | `dianping` 专用命令：登录 Cookie 直抓搜索列表 |
| 京东搜索/详情 | 登录墙 + h5st 签名 | 红线不碰签名；用豆瓣"在哪儿买"公开聚合价替代 |
| 豆瓣读书 | 无严重反爬 | `sites` 精配直接用 |
| 小红书评论 | SPA 签名接口 | 浏览器方案 + `record_from: capture` 捕获响应 |
| 小红书 Web 全量（搜索Top池/笔记详情/评论/作者主页，2026-09 实战 500 篇） | ①未登录搜索页直接弹 `login/qrcode/create` 登录墙；②接口带 X-s/X-t 签名（红线不逆向）；③**页面 JS 反自动化 `window.close()` 关标签**；④直达笔记 URL 走 SSR（`__INITIAL_STATE__.note.noteDetailMap[id].note`，**键名驼峰** `noteId/interactInfo`，无 feed XHR）；⑤评论分页只在 `.note-scroller` 滚动容器**近底部**触发（wheel 落点/增量不足都收不到下一页）；⑥回复藏在 `.show-more`「展开N条回复」，点开才发 `comment/sub/page` | 用户调试 Chrome 扫码登录一次（R7-A）→ CDP 附加 + 拦截 `so.xiaohongshu.com/api/sns/web/v2/search/notes`（搜索卡片含点赞数+xsec_token，深翻约50屏/1000条触顶）→ 逐篇 goto `/explore/{id}?xsec_token=…`，init script 置空 window.close，feed XHR 缺失时读 SSR 驼峰键并归一成蛇形 → 评论翻页用 `el.scrollTop=el.scrollHeight` + `dispatchEvent(scroll)`，展开回复两段式（scrollIntoView→等渲染→重查节点 `el.click()`）→ 作者主页拦 `user/otherinfo`+`user_posted`。并发1、间隔≥1.3s 随机、461/-1 风控退避，500篇×(详情+100评论+6展开)约1.1万请求全程无封 |
| 小红书 IP 风控链 + 常驻浏览器采集（#大模型 话题 Top20 + 评论 + 作者主页，2026-09 实战 28 详情+3176 评论+20 主页） | ①移动家宽出口**未登录整站 300012「IP存在风险」**（连登录页都封，换 profile 无效）；②**Chrome `--proxy-server` 参数被无视**（指假端口都能上网，疑似单例/策略覆盖）；③运行时全局无 `__INITIAL_STATE__`（水合后删除），须从 SSR HTML 提取并修 `undefined`/`new Set()`/`NaN` 才能过 json.loads；④搜索 v2 接口在 `so.xiaohongshu.com`，排序藏在 body `filters:[{type:"sort_type",tags:["popularity_descending"]}]`（顶层 `sort` 字段恒 "general"，别被误导）；⑤评论 `comment/page` 顶层 10 条/页，`hasMore` 字段缺失，耗尽判定看 cursor 停止前进；⑥高频触 **300013「访问频繁」限流**（website-login/error 跳转） | patchright `launchPersistentContext({proxy:{server:'http://127.0.0.1:7897'}})` 常驻守护进程 + `page.on('response')` 全量捕获 xiaohongshu.com API/HTML 落 JSONL（登录态存 ~/.universal-scraper/xhs_profile 复用）；用户扫码登录一次后全程无人值守；详情/作者主页一律解析 SSR HTML（extract_state 配平提取），评论按 URL 参数 note_id 归属；点赞排序直接用搜索卡 `liked_count` 精确值（SSR 里是 "3.4万" 展示值，两源交叉核对一致）；采集器滚动用 `el.scrollTop=el.scrollHeight` 跳底强制触发懒加载（scrollBy 增量会被渲染增长追上导致 stable 误判提前退出） |
| magtech 期刊系统 | 常规 | `journal` 精配批量下载 PDF |
| 抖音话题视频 | 强 JS 壳 + 签名 | 仅浏览器可读部分可见数据；抓不全时如实说明并 L5 |
| EUIPO eSearch plus | 13KB JS 应用壳，直抓无数据；后端 API 基座活跃（/eSearch/api 返回 200） | 先 capture_all 捕获检索/详情接口 → JSON 直抓；浏览器只留交互（配方 R13） |
| 汽车之家 autohome（车系/配置/成交价/口碑，2026-09 实战 83车系/337车型/7058成交价/909口碑，2180请求全程无封） | ①配置/成交价字段是 HS 反爬：空 span 占位 + 乱序 JS + insertRule 注入 `::before` 内容（无字体文件，`window.hs_fuckyou` 反调试）；②Next.js 选车页 SSR 只给热门 20 个，全量筛选走 `car-web-api/car/search/searchcar`（searchtype=6 新能源、levelid 17/18/19、fueltypedetail=4 纯电，state=0 含停售）；③口碑入口已迁 k.autohome.com.cn（kiauto 域名已死），列表接口 `koubeiipv6.app.autohome.com.cn/pc/series/list` 纯 JSON 且自带成交价字段，pageSize 固定 10；④成交价页 `jiage.autohome.com.cn/price/carlist/p-{specid}-2-0-0-0-0-{页}-0` 每页 10 条 SSR+逐条内联混淆脚本，DOM 中每条记录各包一个 `.car-lists`（querySelector 只取第一个会漏 9 条，必须 querySelectorAll）；⑤配置页一页覆盖同年款全部车型列（`var config` 基本参数 + `var option` 配置项，JSON 键经序列化后全为字符串，取值要 str/int 双试） | 判型表「HS CSS 伪元素」行：headless 渲染 + 读 styleSheets 注入规则建映射还原；接口层全部纯 HTTP（searchcar/getSpecList/口碑列表/经销商报价 ashx）；范围清洗用配置项「能源类型=纯电动」（源站 series 级筛选会混入增程/插混车系） |
| NMPA 药监局数据查询（datasearch） | 全 HTTP 通道 412，412 页面是 `$_ts` 混淆 JS（瑞数防护）；出口 IP 正常也照样 412 | L3 附加真实 Chrome（CDP 9222）→ UI 操作选分类/输关键词/点放大镜 → 结果弹新标签页 → **翻页时拦截 `/data/nmpadata/search` 的 JSON 响应**（f0~f3 即批准文号/名称/单位/本位码），别解析 innerText；575/575 全量抓成 |
| 东方财富股吧 | SSR 内嵌 JSON（window.article_list）+ em_capt「身份核实」+ 验证 cookie 有请求预算（约140次/会话） | L3 过一次核实拿 wsc_checkuser_ok cookie → 浏览器导航 + embedded_json 直取（配方 R15）；忌 HTTP 批量（会连坐封浏览器会话） |
| Product Hunt | Cloudflare Turnstile 拦 headless + Next.js 客户端渲染 + 无限滚动；连接重置=风控 | 调试 Chrome 过一次校验 → cdp 附加 → row_css 卡片 + scroll_count 滚动 + detail.extract limit 抓 makers（配方 R16） |
| B站 API | 直接调接口返回 -352 风控码 | 先 GET 一次 www.bilibili.com 主页拿 buvid cookie，把 cookie **串**填进 `anti_bot.cookies`，并**固定 UA**（`"rotate_ua": false`，与预热时一致——UA 漂移会再触发 -352）；带 Referer 调 api.bilibili.com 公开接口（每周必看 `popular/series/one?number=期号`、热门 `popular` 均有现成端点） |
| B站 UP主年度全量（弹幕/评论/元数据，影视飓风 mid=946974，2026-09 实战 100 视频 29.8万弹幕/1089请求） | ①空间投稿列表 `wbi/arc/search`：随机 `dm_img_str` 稳定 -352→-412，**浏览器里同样被 -403**（未登录）；②动态 feed/space 匿名 EMPTY_200；③评论 wbi/main 与 legacy 全通道匿名只给 **3 条 + is_end:true**（all_count 几千，登录墙非风控，热浏览器复现）；④弹幕 `dm/web/seg.so` 匿名按权重强过滤（平台计数15万实给1千），`list.so` XML 较全但按时长封顶（600的倍数）；⑤合集接口 `seasons_series_list`/`seasons_archives_list` 与 `x/web-interface/view/detail`、`x/v2/reply/reply` 匿名全通 | 列表：浏览器 capture_all 一次拿**真实 dm_img_str（WebGL串）/dm_cover_img_str/dm_img_inter** → 纯 HTTP 复用原值重算 wts/w_rid 直抓（-412 退避 40s 后自愈，翻页直到 created<目标年）；弹幕：seg.so + list.so **并集**去重封顶前N条（两通道过滤规则不同互有增量，protobuf 手写 wire 解析 field1=elem、2=progress ms、7=content、8=ctime）；评论：匿名只取热评头几条+`reply/reply` 回复全量，**登录墙如实报告 L4 路径**（用户登录一次→cookies 导出→断点续跑补齐）；`diagnose` 不可用时代码 -352/-412/EMPTY_200/3条+is_end 四种症状含义完全不同，按判型表对号入座 |
| gsxt.gov.cn 国家企业信用信息公示系统 | ① 加速乐 521 JS 挑战（curl_cffi 全挂，真浏览器秒过）；② 搜索接口叠 CTCT 行为评分：`CT_*` cookie + 412「努力加载中」页 + 200 空页，**验证码失败/连搜即升级，封禁 10-25 分钟一档**；③ 搜索必过 GT4 点选码（文字「依次点击X」与图标九宫格「选N个符合右图」混发，失败自动换题）；④ 未登录只见基础登记+4个风险栏目，股东/变更/年报/抵押/出质/司法协助全部「请登录后查看更多信息」 | **首页探针抓放行窗口（10-25 分钟一个）**：探针只 `goto` 首页看 `innerText.length>100`；窗口出现立刻搜索；**每窗口限一次搜索**（验证码失败=窗口作废，再等）。文字码 `ddddocr` det 检测主图字框 + classification 认目标条与各字框 → 自动点选零延迟；图标码退人工（弹窗存活约 90s，脚本轮询 `manual_clicks.json` 留足 4 分钟）。**详情页与翻页不吃搜索配额**：会话内按「收 10-20 候选 → 逐个点详情（target=_blank）→ 关标签 → 翻页」把单窗口价值榨干。详情 GUID 是短时效令牌（约 2-3 分钟）且必须从结果页真实点击，直接 goto 报「请求异常」。代理 IP（境外）与家宽 IP 均可工作，行为评分才是主因 |
| epub.cnipa.gov.cn 中国专利公布公告（瑞数） | 首页/高级查询/详情页均 `$_ts` 瑞数（HTTP 202 挑战壳，`diagnose` 会误判 L0）；443 不通只有 80；ASP.NET 表单站：`/Dxb/AdvancedQuery` POST 查询、`/Dxb/PageQuery` POST 翻页（searchAfter 游标可过万条排序上限）、详情 `GET /patent/{公布公告号}`；单查询超 10000 条不排序；公报每周二/五出版；**突发 POST 后搜索接口 400 空体 → 整站挑战循环（39B 空页），GET 详情通道更宽容** | L3 真实 Chrome（CDP 9222）过挑战 → `form.submit()` 提交高级查询（日期字段 pd=公布(公告)日，ggr 是公报出版日，别混用）→ 结果行自带全部著录字段（列表页即数据页）→ 详情页补摘要全文+摘要附图 URL+事务数据加密ID。**搜索 POST 被限后立刻停，改走详情 GET 通道继续收**；冷却以分钟计。附图 jpg 直连 400，必须带浏览器会话下载。公报期次页有四字成语验证码 + 下载PDF，不适合批量 |
| epub.cnipa.gov.cn（续）· 搜索配额锁死日 | **搜索 POST 按 IP 日配额（实测约6-10次/日），烧尽后静默45min+换浏览器profile无效；免费代理池 0/600 可用（R19 失效——免费源过不了瑞数挑战）** | **R31 号段枚举旁路**：详情 `GET /patent/{PN}` 不吃搜索配额（封锁期照常 200），且公布号按期次连续编排——用已知锚点号（如 CN118423231A）逐号 +1 扫描，读返回页的"申请公布日"过滤目标日即可纯 GET 收数；实测命中率 100%（50/50），5-6s/号。注意同号段内技术领域相近（按 IPC 排序编 号），要领域多样性需跨号段取样 |
| wcjs.sbj.cnipa.gov.cn 中国商标网查询系统（2026-09） | HTTP 通道 406 + `/_fec_sbu/hxk_fec` 前端挑战壳；真浏览器导航 1-2 次即 IP 封锁页（Sorry, you have been blocked + Request-ID），冷却 ~10 分钟，封禁窗口内连静态 /js/*.js 都 403；SPA 一次加载成功后活页内交互不再触发；**公告查询/综合查询全通道强制 SSO 登录（跳 sso.cnipa.gov.cn/am/#/login，官方信箱答复证实）**；旧版公告系统 sbgg.sbj.cnipa.gov.cn:9080 已下线（DNS 不存在） | 只适合小样本/按需查询：L3 调试 Chrome 人工登录一次 → 单导航动作链（点公告查询→填公告日期→查询→capture_all 同会话收口），**每 10 分钟最多 1 次导航**；全量建库改走官方数据开放（ggfw.cnipa.gov.cn 商标数据开放批量下载/接口、ipdps.cnipa.gov.cn FTP），网页批量抓取路线实测不可行（请求量 ~3900 万，合规速率 3.7 年） |
| kyfw.12306.cn 余票查询（2026-09 实战：北京→十城 2026-10-01 国庆 G/D，317 可售区间/1111 席别行，22 请求/轮零拦截，全 L0 纯 HTTP） | ①`queryZ` 302→`queryG` 端点轮换（相对 Location）；②`queryTicketPrice` 已下线（200 + queryLeftNewDTO 空）；③票价+余票数内嵌在行字段[39]；④过去日期/超预售期 200+HTML 错误页；⑤城市电码（BJP）自动展开市内全部车站，同一物理车次出现多行（市内多上车站区间 + 经停多目标城市，各为独立可售商品，价格可不同：G5 北京站¥672/北京南¥667） | init 预热 Cookie → 手动跟 302（同域复检）→ 解析[39]（官方 `queryLeftTicket_end_js.js` 的 `e()` 函数即解码器：每10字符=席别码1+价格×10的5位+余票数4位；同码重复组取首现，末尾重复组是无座挂靠价）→ 解析余票数与源站 [26]/[30]/[31]/[32] 显示逐条互验（实测 818/818 一致）+ 隔数分钟重抓对票价稳定性抽验；"运行图调整暂停发售"车次时间是占位符 24:00/99:59 如实保留；并发 1、间隔 2s |
| www.cwl.gov.cn 双色球历史开奖（2026-09 实战：2003-2025 共 3397 期双源交付，1981 请求零封禁） | ①`findDrawNotice` JSON 接口带 UA/Referer 即通，但**数据窗口只到 2013 年**（issueCount 给多大都只回 2013001 起，issueStart/issueEnd/dayStart/dayEnd 参数被静默忽略）；②详情 .shtml 是 ZCMS 静态页（robots 只禁带 `?` 的查询 URL），开奖号码是 JS 填充的 `-` 占位符（非 CSS 偏移），公告表格/兑奖截止日服务端直出；③整站无字体反爬、无 CSS 偏移（新旧详情页均验证） | 表格字段走接口一次 `issueCount=3200` 全拿；公告全文+每期兑奖截止日逐期抓 `div.content-text`（2022-118 期起才有每期截止日打印，早年只有通用 60 天规则文本）；占位符号码用接口号码回填即得渲染后全文；3s 间隔 + flock 预算闸 + JSONL 断点，1958 期约 2 小时 |
| kaijiang.500.com 开奖公告（替代源侦察） | ①桌面域整站 EdgeOne：普通 404 路径直出 nginx 404，数据路径 302 → TEOCaptcha 验证码（CDP 真 Chrome 也拦，见判型表 EdgeOne 行）；②移动端 m.500.com 无验证码但**只服务端渲染最新一期**，老期号/短代码一律回落最新期页（软 404）；③历史记录"更多期次"是 JS 抽屉无静态链接 | 放弃网页路线；官方行业中彩网 jc.zhcw.com JSONP 接口（R37）可补 2003-2012 表格字段（2003-2004 奖金/奖池官方置零属数据缺失非反爬）；Wayback 对两站老公告页均无存档，搜狐年度附表只有号码+投注额 |
| data.stats.gov.cn 国家统计局"国家数据"（2026-09 实战：价格指数月/季/年三库 267 数据目录/2575 指标/35.2万观测点，563 请求零封锁，全 L0 纯 HTTP） | ①门户 2024+ 改版为 `/dg/website/page.html#...` SPA（WZWS WAF，首页 302 指路）；②旧 easyquery.htm 接口**整站 UrlACL 封死**——curl/curl_cffi/真浏览器页面内 fetch 全部 403，别再试；③新接口三件套：树 `new/queryIndexTreeAsync?pid=&code={1月,2季,3年}`、指标 `new/queryIndicatorsByCid?cid=&dt=&name=`、数据 `POST stream/esData`（JSON 体）；④**指标按时段分版别目录**（如 (2026-)/(2021-2025)/(2016-2020)/(-2015)）：esData 对超窗口查询只回填时间轴不回数值——历史数据必须按目录 sdate/edate 推 dts 窗口逐版别采集；⑤时间码格式：月 `202608MM`、季 `202603SS`（YYYY+0Q+SS）、年 `2026YY`，区间 `起-止`；⑥指标"分类指数"与总指数混排（分行业 PPI 目录里含总指数行），跨版别目录去重键=指标内部ID+时间码 | 首页 302 → `fetch --capture` 数据页（cdp 附加真 Chrome）录出树/指标/数据接口 → 裸 HTTP 重放（仅 UA+Referer，无 cookie 要求）；树从库根（`pid=""` 只回库本身）下钻一层找"价格指数"大类 → BFS 展开到 isLeaf → 每叶目录 queryIndicatorsByCid + esData(dts=按 sdate/edate 推窗，缺省 1980/1949 起兜底由服务端裁剪)；esData 名为 stream 实为整段 JSON；产出后剔"空轴行"（宽窗返回指标起报年前的时间轴，占原始行 42%）。3s 限速 563 请求零 403 |
| www.aqistudy.cn 空气质量历史数据（2026-09 实战：北京 2024 全年 8682 城市小时行 + 29.3 万站点行 + ERA5/ISD 气象，1098 文件零失败交付） | ①加密 API `apinew/aqistudyapi.php`：POST 参数名随机（形如 h1zlb1QoZ），值=Base64(JSON{appId,method,timestamp,clienttype:"WEB",object,secret=md5(appId+method+timestamp+clienttype+JSON(ObjectSort(obj)))})，响应 AES→DES→Base64 三层解密；页面全局函数 `sZIy6N0BKIVsZFNRHr(method,obj,cb)` 一体化封装（CDP evaluate 直接调，回调给明文；失败只 console.log(errcode,errmsg) 不回调——诊断要 hook 或手动复刻 ajax）；method=GETDETAIL/GETCITYWEATHER/GETCITYTIME，object={city,type:HOUR/DAY/MONTH,startTime,endTime}；②**小时接口历史深度约 1 年**：366d 内可查，400-600d 返回非密文，≥700d errcode 1011 invalid time period；clienttype=WEIXIN 拒 1006 invalid clienttype；③historydata/daydata 逐日表=随机值干扰表（仅日期/AQI/等级真实，见判型表）；④气象：open-meteo ERA5（wind_speed 默认 km/h 要 ÷3.6）、NOAA ISD（DATE 为 UTC 要 +8h，TMP/SLP/WND 字段值 ÷10） | 加密链页面运行时内调用（不抄算法不伪造）→ 二分探测发现历史深度不足 → 存档替代配方 R39：QuotSoft.net/air/（CNEMC 同源，单日全国 CSV 直链；慢站 10KB/s 用 Range 8 段并行×文件并发 3，断点续传按"本地大小=Content-Range 总长"判定）→ 官方年均锚点验证（生态环境局公布值对照 ±3% 内） |
| 知乎问题回答/评论/作者采集（诺奖AI问题 777943030，2026-09 实战 50回答+50作者+615根评论+777回复，~270请求零封锁） | ①裸 curl 650B `zh-zse-ck` 挑战壳、curl_cffi 403——**有头 patchright 持久 profile 加载即过**，headless 出"安全验证"页；②未登录：SSR 首批 9-15 条后滚动零翻页请求，`/question/{id}/answers` 子页 404 荒原页；评论 root_comments 与 members 接口**未登录可用**；③登录后页内 `fetch(url,{credentials:'include'})` 免 x-zse-96（同源+cookie 即 200，签名不碰=合规）；④feeds 链头只在页面加载时自发一次，**响应监听器必须挂在 goto 之前**；⑤cursor 消耗型：存档旧 cursor 重放返回空+is_end:true，必须现场抓；⑥页面 ~30k px 高，`scrollBy` 渐进到不了触底哨兵，须 `scrollTo(0,scrollHeight)` 跳底；⑦feeds(order=upvote) 链**不含 SSR 首屏回答**——全局最高赞只在 js-initialData，须 SSR∪链合并重排才是真 Top N；⑧root_comments 对零评论回答直接 403（合法空态）；点赞=`vote_count` 非 like_count，IP属地=`address_text`；**回复两层**：v4 内联 `child_comments` 仅首页（缺口 58%），全量走 v5 `comment_v5/comment/{cid}/child_comment`（老 `/comments/{id}/child_comments` 恒空），v5 无 address_text 用 v4 内联按 id 富化；⑨热门序两次抓取首条可不同（成员与数值稳定） | 登录器（弹窗扫码→轮询 z_c0 自动收窗）+ 采集器（链头捕获→cursor 链翻页→SSR 合并重排→作者/评论补采→v5 回复分页补齐）+ 双抓 diff 验证（评论先做成员资格复核再判差异），动线见 R41 |
| 拼多多百亿补贴商品评论（iPhone 17 Pro Max goods_id=1009522250164，2026-09 实战 130 评论+101 用户，全程 ~40 请求） | ①小号新登录：搜索 54001→自动化导航风暴升级人脸墙（商品页也拦）——判型表 PDD 行，废号换主号**人工暖号**；②百亿补贴 H5 入口=`brand_activity_subsidy.html`（首页无入口链接，从搜索"百亿补贴"进），频道内"手机数码馆"→Apple 品牌馆 `pincard_ask.html?top_goods_ids=` 有 iPhone 页签；③商品评论页首屏 20 条 SSR 直出**无任何 JSON 内嵌**（33 个 script 全查无 review_id），翻页 `POST /proxy/api/reviews/{goods_id}/list?page=N&size=10` body 带 anti_content（页面自算，勿碰）；④列表平台封顶 130 条、有空 data 页结束标记（页签"全部(659)"是同款聚合池，分散在同款链接各列表，换算需逐链接采后按 review_id 去重）；⑤默认好评 API comment=模板文案"该用户觉得商品很好，给出了5星好评"（DOM 渲染不同，按 is_default_review/模板识别）；⑥评论者头像/昵称**不可点、无用户主页接口**（H5 不暴露买家主页，主页类字段宁空勿错）；⑦**回复楼内他人昵称是原始明文**——脱敏必须建全量昵称词典（评论者+回复者+被回复者三类来源）全局替换，只掩昵称列会漏 | CDP 附加用户已登录调试 Chrome → fetch --capture 摸清接口 → 单遍采集器（重导航评论页+`page.on('response')` 监听+匀速追底滚动 4-5s/步，空 data 页即停）→ **位置映射**（展示序 1-20=SSR、21-130=API page3-13 顺序，验证 110/110 后免模糊匹配）→ 昵称词典脱敏（评论者关联键=sha256(头像URL)前12位，平台不暴露用户数字ID）→ 交付前用已知原始昵称对全部产物文件做泄漏复查 |

**两条先查表再动手的经验**（能省一个数量级的功夫）：

1. **"某天公布的全部 X"** → 先找官方公报/Gazette/Bulletin（EUIPO Trade Marks
   Bulletin 周刊、商标公告、招标公告）。公报是官方设计给人按期浏览的入口，
   别逐个实体硬查（配方 R14）。
2. **"某实体的程序/状态记录"**（商标异议、案件进展）→ 官方检索页多半是 SPA，
   第一手动作是 capture_all 找详情接口，而不是渲染 UI（配方 R13）。

| 症状（看 fetch 侦察结果） | 判型 | 起手级 |
|---|---|---|
| 巨潮 cninfo 年报面板批量（2026-09 实战：1343 家×2010-2024 共 15,638 项，两轮封禁后全量交付 13,179 真实企业-年） | ①`new/hisAnnouncement/query` POST：`stock=code,orgId` 缺 orgId 时**静默返回 0 条**（不是报错——topSearch 兜底必须保留）；②`category=category_ndbg_szsh` 对沪深两所都有效（column 参数不敏感）；③平台级封禁约 24h：换 IP 无效，探测法=直接 POST 看是否回 JSON；④报告年度≠发布年：年报 FY Y 发布在 Y+1，检索窗 Y+1~Y+2 + 标题正则 `(\d{4})年年度报告` 强校验==Y（修订版在 Y+2，取 announcementTime 最新）；⑤`static.cninfo.com.cn` 附件直链无签名可直接 GET | `cli research run`（科研批量动线）：断点续跑 progress.json + 连续 5 次 API 异常自熔落盘退出 + PDF 即采即弃（下载→pymupdf 提取→.txt.gz 文本档案→删 PDF，磁盘恒定）+ 扫描件 RapidOCR 兜底；跑完 `research panel` 出面板、`audit panel` 出口审计（唯一键/窗口/freq 公式/文本源覆盖/文档一致）；限速 2s+jitter 13-18 条/分零投诉 |

## 六、0 结果诊断报告（固定结构）

```
⚠️ 这次没抓到数据，原因和方案如下：
· 现象：<一句话，如"所有请求都返回同一段 1378 字节的保护壳页面">
· 已尝试：HTTP 直抓 → 指纹伪装 → 无头浏览器（共 3 级）
· 证据：recon.md / last_page.html 已存在 <任务目录>/，你可以亲自打开看
· 判定：<类型>
· 方案：<下一级动作，或 L5 人工通道指引>
```

要求：诊断前先把证据文件给用户留好——**失败轮的 `last_page.html`、
`capture_all.json` 会自动保留在输出目录**（v1.6.2 起 `run --config` 也持久化，
此前仅任务包保留）；禁止只说"失败了"三个字。

### 六·一、"工具失败"与"站点无数据"必须分叉（batch1700 战训，400 项实测）

0 结果的第一步不是修工具，是**判定问题类型**——batch1401~1700 四百项实测中，
failed 项里近半是"数据本身不存在于公开渠道"（当日无披露/仅内部系统/已下线），
修一个没有修复对象的问题会白烧 3 轮：

```
0 结果
 ├─ A. 数据不存在（→ 标 nodata，不修工具，直接下结论）
 │    证据要求（至少两条之一）：
 │    · WebSearch 确认该数据仅内部/付费终端/从未公开
 │    · 官方页面明示"暂无/当日无更新"，且连续多个自然日均无
 │    队列动作：batch nodata <id> --result "依据+已验证渠道"
 │
 └─ B. 数据存在但没抓到（→ 先判 blocked 还是 failed）
      B1. 需要不同网络/身份（→ 标 blocked，走用户通道，不修工具）
          · 境外 IP 封锁（页面明示"仅限中国大陆"或仅境内可达）
            → 建议：用户连境内 VPN/热点后重试
          · 验证码/会员墙/登录墙 → 建议：用户登录（cookies/cdp 复用）或放弃
      B2. 工具/路线问题（→ 标 failed，按 L0~L5 诊断修）
          证据要求：别站/历史快照/官方公报里见过同结构数据
          修 3 轮仍 0 → failed 收工，诊断报告固定结构
```

**核对环节的"时间口径"提醒（batch1800 战训）**：很多数据源只提供**当前状态**
（最新一期/最新净值/实时行情），没有"任务指定日期的历史快照"。核对时必须自问：
拿到的是当前值还是任务日期的值？若只能取当前值，交付物里显式标注
"当前值（截至 YYYY-MM-DD），非任务日期历史快照"，别让数据看起来像历史值。

判断口诀：**先证明"它存在"，再谈"我怎么拿到"**。

## 七、配额机制（进得来但只给你 N 个）—— 2026-09 科研管理战役战训

L0-L5 解决"进不来"；配额机制是"进得来但限量"。**先判型再动手，误判配额类型 = 战术全错。**

### 7.1 配额四分类（一次单变量实验定型）

| 类型 | 特征 | 有效武器 | 无效武器 |
|---|---|---|---|
| **按 IP 计** | 换 IP 立即恢复 | 代理池 + 切片分工 | 死磕单 IP |
| **按资源计** | 同一资源换 IP 仍拒；别的资源能下 | 冷却账本（窗口滚出前零请求）+ 一次命中 | 代理池、重试 |
| **全局总量** | 所有 IP 所有资源全拒 | 静默等回填 / 身份升阶 / 换通道 | 一切高频动作 |
| **账号计** | 登录态换 IP 仍拒 | 机构订阅 / 单账号节流 | 多开小号（红线禁止） |

**判别实验（战训核心）**：拿一个**从未请求过的处女资源** + 一个已烧穿的资源，
各发一次请求对照——处女资源能下 = 配额在资源维度；都拒 = 全局；处女能下但换 IP 后
同一资源拒 = 按 IP。**绝不用任务本身的头部资源做实验**（每次失败请求都计数！）。

### 7.2 探测成本铁律（本次战役最贵的教训）

- **被拒绝的请求同样消耗额度**（POST 拿到令牌即扣，无论 GET 成败）；
- "温和试探"不是免费的：每 8 分钟探一次 = 把回血窗口无限重置，**自伤**；
- 诊断一律用处女资源做对照，一次定性，不带侥幸重复；
- 一切重试先过 `quota_ledger.QuotaLedger.in_cooldown()`（窗口滚出前零请求）。

### 7.3 队首轰击与切片分工

按 IP 计配额 × 顺序队列 = 灾难：所有 worker 都从清单第一项开始，把头部资源烧穿，
后面全部没碰过（战例：30 个代理啃同一批文章，0 篇/天）。修复：**worker i ← 第 i 段**
不相交工作集（`quota_ledger.assign_chunks`），修复后 1100 篇/小时级。

### 7.4 边际余量（撞墙损耗）

每 IP 撞配额墙的瞬间，正在过手的那个资源"拿到令牌没下成"，额度白烧。
战例：71 个代理 × 每个烧 1~2 篇 = 140 篇牺牲品。规则：**每 worker 上限 =
观察墙值 × 0.75**（`quota_ledger.margin_cap`）。

### 7.5 前提失效检测（站方会中途升级）

同一套请求的成功率从 X% 骤降到 0 时，第一步是**浏览器对照重验**（指纹/头/机制
有没有被站方升级——TLS 指纹之上还会叠 Sec-Fetch 头校验），而不是换 IP 硬试。
战例：官网一夜之间给下载端点加了 Sec-Fetch 三件套校验，按"昨天的机制"排查浪费了一小时。

### 7.6 本机干扰自查（诊断第一步）

Clash 等系统代理开着时，"直连"其实走代理节点出口——烧错配额、换 IP 无效、
误诊本机额度。任何 IP/配额诊断前先跑 `cli ip`（出口 + 系统代理 + 电源三项体检）。

## 八、GraphQL 型站与本机环境坑（2026-10 快手战役补充）

### 8.1 GraphQL 站的判型与绕过

| 症状 | 真相 | 处方 |
|---|---|---|
| `curl_cffi`/requests 直连 GraphQL 报 `Need captcha`，但改字段名会返回正常的 `GRAPHQL_VALIDATION_FAILED` 校验错误 | **不是签名算法问题**，是缺浏览器风控头（kuaishou 实测：页面 JS 生成 `kww` 头 + `kwssectoken`/`kwscode` cookie） | **不逆向签名**：`page.evaluate` 里同源 `fetch('/graphql', {credentials:'include'})`，浏览器自动带风控头，滑块自动过。别在 curl 侧换指纹 |
| `operationName` 传接口名（`visionCommentList`）报 400 | operationName 是**白名单**，须用页面自己的命名（快手是 `commentListQuery`） | 从页面 XHR 捕获里抄 operationName + 完整 query 文本 |
| 同一接口有两个游标字段（如 `pcursor` / `pcursorV2`），混用后前几页正常、第 N 页起恒 0 条 | V1 游标通道已废弃，混用致游标错乱 | 只用 V2 游标单链翻页；先"逐游标推进"诊断确认每页仍有数据 |
| `__type` / `__schema` 报 introspection not allowed（Apollo 生产配置） | 正常，introspection 被禁 | **把校验错误当 schema 字典**：`Cannot query field "x" on type "T"` / `Did you mean "y"?` 会主动列出真实字段名。逐字段发请求 + 精确匹配该字段报错来枚举。**别 slice() 截断响应**，否则报错被截掉会全判为"合法"（本任务踩过，45 字段全误判） |
| 字段命名风格 | **snake_case 与驼峰混用**（`user_id` / `headurl` / `pcursorV2` / `likedCount`） | 不能按单一风格推，必须逐个验证 |

### 8.2 REST 搜索接口的限流信号

- HTTP 200 **不代表成功**：快手 `/rest/v/search/*` 限流时返回 `{"result":2,"error_msg":"操作太快了，请稍微休息一下"}`。
  **把 `result` 字段当状态码检查**，别看 HTTP 码。恢复需间隔 5s+ 且换关键词；连续 8 个关键词必被限流，
  隔开后 20 关键词 0 限流。
- 用多关键词矩阵（`X` / `X时政` / `X最新` / `X官方` …）捞全某账号全部作品，按 `author.id` 精确过滤。

### 8.3 本机（macOS）环境坑 —— 不要再试有头浏览器

| 现象 | 根因 | 处方 |
|---|---|---|
| Chrome `--remote-debugging-port=9222` 起不来，日志 `GPU process isn't usable. Goodbye.`，`--disable-gpu` 无效 | macOS seatbelt 沙箱阻止 Chrome 辅助进程 | 放弃 CDP，改 `chromium.launchPersistentContext(headless:true)` |
| `open-debug-chrome.sh` 报"端口 9222 已被其他程序占用"但 `lsof` 无输出、curl 直连 Connection refused | 脚本用 `curl` 探测，环境代理（`HTTP_PROXY`）造成假阳性 | 探测本地端口必须设 `no_proxy=127.0.0.1,localhost` 或用 `ProxyHandler({})` |
| **有头浏览器窗口活不过 3-5 分钟自动消失**（`nohup+disown`、`setsid`(macOS 无此命令)、FIFO 保活、PID 保活、异常兜底全试过，均被杀） | 有外部进程管理器清理 GUI/浏览器进程 | **不要依赖有头窗口做登录**。用户登录态拿不到就如实声明字段缺失，别反复消耗用户时间（用户已连续 6 次被关） |
| 后台浏览器/采集进程被外部杀掉（本机常态） | 同上 | 采集器必须抗杀：`uncaughtException`/`unhandledRejection` 兜底 + **每页落盘** + 同进程内重开浏览器续跑（不要依赖外部守护 shell，它也会被杀） |
| bash heredoc 里写 JS，`${...}` 模板字符串被 shell 解析报 `Bad substitution` | 未加引号的 heredoc 展开变量 | 用 Write 工具写脚本文件，别在 heredoc 里塞 JS 模板串 |
| `sed -i ''` 改 macOS 文件报 `No such file or directory` | BSD sed 与 GNU sed 参数差异 | 用 Python `pathlib` 改文件更稳 |
