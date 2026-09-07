# 任务配方手册（照抄即用的模板）

> 用法：判型（见反爬手册）→ 选配方 → 替换 `<尖括号>` 内容 → validate → --limit 5 样本。
> 所有配置里 `output.dir` 一律写用户任务目录的绝对路径。

## R1 · 普通列表翻页（最常见）

**A. 页码在查询参数**（`?page=2` 型）——URL 写干净，页码由策略追加：

```json
{
  "name": "简单列表",
  "source": {
    "type": "http_html",
    "url": "https://example.com/list",
    "row_css": "div.item",
    "fields": {
      "标题": {"css": "h3 a"},
      "链接": {"css": "h3 a", "attr": "href"}
    }
  },
  "pagination": {"strategy": "page_param", "page_param": "page", "start": 1, "max_pages": 50},
  "record": {"fields": {"标题": {"from": "标题"}, "链接": {"from": "链接"}}},
  "output": {"dir": "<任务目录>", "base_name": "list", "formats": ["json", "csv", "xlsx"]},
  "anti_bot": {"min_interval": 1.0, "max_retries": 3, "http_backend": "requests"}
}
```

**B. 页码在路径里**（`/page/2/` 型）——改用"跟随下一页链接"：

```json
"pagination": {"strategy": "next_url", "next_selector": "li.next a", "max_pages": 50}
```

## R2 · 点"下一页"按钮的列表

`source.type` 用 `browser`，翻页写进 source（完整配置示例）：

```json
{
  "name": "点击翻页列表",
  "source": {
    "type": "browser",
    "url": "https://example.com/list",
    "wait": {"selector": ".item", "timeout": 20000},
    "row_css": ".item",
    "fields": {"标题": {"css": ".t"}},
    "pagination": {"type": "click", "selector": "a.next", "wait_ms": 1500}
  },
  "pagination": {"strategy": "none", "max_pages": 30},
  "record": {"fields": {"标题": {"from": "标题"}}},
  "output": {"dir": "<任务目录>", "base_name": "list", "formats": ["csv", "xlsx"]},
  "anti_bot": {"min_interval": 1.0, "max_retries": 3}
}
```

## R3 · 无限滚动列表

browser 型 + 滚动参数，不加翻页：

```json
"source": {
  "type": "browser",
  "url": "<URL>",
  "row_css": "<行选择器>",
  "fields": {"<字段>": {"css": "<选择器>"}},
  "scroll_count": 10,
  "scroll_wait_ms": 2000
},
"pagination": {"strategy": "none"}
```

## R4 · 列表 → 详情两级采集

R1 基础上开 detail（相对链接记得补前缀）：

```json
"detail": {
  "enabled": true,
  "url_field": "链接",
  "url_transform": [{"type": "prefix", "value": "https://example.com"}],
  "extract": [{"name": "正文", "css": "div.article"}],
  "concurrency": 2,
  "interval": 0.5
}
```

## R5 · 单页正文 / 表格提取（不写配置）

```bash
fetch "<URL>" --article --out "<任务目录>/正文.md"
fetch "<URL>" --table --json --out "<任务目录>/表格.json"
fetch "<URL>" --browser --screenshot "<任务目录>/截图.png"
```

## R6 · 整站文档收集

```bash
crawl "<入口URL>" --allow "/docs/|\\.pdf$" --depth 3 --max 500 --robots --out "<任务目录>/crawl"
```

## R7 · 需要登录的站（两条正路）

**路 A · CDP 附加**（交互多、页面复杂时）：
1. `bash "${SKILL_DIR}/scripts/open-debug-chrome.sh"`，让用户在弹窗里登录。
2. 配置 browser 型 + `"cdp": "http://127.0.0.1:9222"`。

**路 B · Cookie 直抓**（页面本身简单时）：
1. 同样让用户登录调试 Chrome（或任意浏览器导出 cookie）。
2. `cookies` 命令导出 Cookie 串 → 填进 `anti_bot.cookies` + `cookie_domain`。
3. 走 http_html 轻量抓取。

## R8 · 数据藏在接口里（SPA）

1. browser 型 + `"capture": true`，先小跑一页。
2. 读任务目录 `capture_all.json`，定位含目标数据的响应与字段路径。
3. 改配置：`"record_from": "capture"` + capture 模式声明
   `{"name": "x", "url_pattern": "<接口URL特征>", "records_path": "data.list"}`，
   字段从 JSON 键映射。接口带签名 → 不逆向，保持 capture_all 模式由你事后挑字段。

## R9 · 已有精配的站（零配置）

```bash
sites                                      # 看精配列表
sites --run "<目标URL>"                    # 直接跑对应精配
```

覆盖：豆瓣图书（详情/搜索/在哪儿买）、当当搜索、magtech 期刊、大众点评等。

## R10 · 论文 PDF 批量下载 / 图书目

```bash
journal --journal "<期刊标识>" --years 2020-2024 --out "<任务目录>/PDF"
books --spec <books.json> --out "<任务目录>/书目" --no-covers
```

## R11 · 定时任务与监控

```bash
schedule --task <任务包目录> --every 3600 --resume
monitor  --task <任务包目录> --every 300 --key url
```

## R12 · 强防护站（升级链打包）

写好 R1/R2 配置后把 `anti_bot.http_backend` 换 `curl_cffi` +
`"impersonate": "chrome"`（L1）；仍不通转 browser 型（L2）；
再不通走 CDP（L3）与人工关卡（L4）；全灭 → L5 人工通道。
每一级都只改配置、不换工具，用户无感知，只听你播报。

## R13 · 知识产权程序记录（EUIPO / WIPO / 国知局等，商标·专利·外观通用）

典型任务："抓某商标的异议（opposition）、无效（invalidity）程序记录"。
官方检索入口几乎都是 JS 应用（EUIPO eSearch plus 实测：页面是 13KB 的 JS 壳，
直抓无数据，但后端 API 基座是活的）。打法按顺序：

1. **第一手永远是接口捕获，不是浏览器啃 UI**：
   browser 型 + `"capture": true` 小跑一页搜索 → 读 `capture_all.json`
   → 找出检索接口和详情接口的 URL、参数、返回结构。
   ⚠️ EUIPO 实测：**capture 只录 run 生命周期内的响应，SPA 的数据 XHR 常在加载后
   异步触发**——必须配 actions 等待才有货，否则只捕到配置/认证类响应：
   ```json
   "actions": [{"type": "wait", "ms": 10000}]
   ```
2. **接口能直连** → 改写 http_json 配置：`records_path` 指向结果列表，
   字段从 JSON 键映射；程序记录（Legal events / Opposition / Cancellation /
   Invalidity）通常在详情接口的独立数组里，抓详情接口即可。
3. **接口带签名/加密参数** → 不逆向（红线），保持 capture 模式用浏览器取数，
   或走 CDP 登录态（L3）。
4. **实体定位**：问用户要任一标识——EUTM 注册号（018xxxxxx 格式）/ 商标名称 /
   申请人；有注册号最稳。
5. 浏览器兜底时的搜索 → 详情 drill-down：`actions` 填搜索框 + 提交 +
   `wait` 等结果 + `row_css` 取列表，详情抽取用 detail 段（同 R4）。

## R14 · 官方公报 / Gazette 按日期路线

适用："某天/某期公布的全部 X"（新商标公告、异议无效决定、企业处罚、招标公告）。
**先找官方公报再动手**——公报是官方设计出来按期浏览的入口，比逐个实体查快一个数量级：

1. 找公报归档页（如 EUIPO 的 Trade Marks Bulletin 周刊、中国商标公告、
   各级政府采购公告），通常按"年 → 期次/日期"两级列表。
2. 用 R1/R2 抓期次索引 → 得到每期 PDF/HTML 链接。
3. PDF 批量下载后用内建抽取（pdfminer/pdfplumber）转文本，按关键词/日期过滤。
4. EUIPO 提示：Trade Marks Bulletin 每周一期，异议/无效决定都在对应期次里，
   覆盖"2025-09-20 当天公布"这类需求就靠它，而不是逐个商标查。

**gov 类"通道穷举清单"**（版权中心战例：官方入口整体迁移时按序排查，逐条留证据）：
旧入口（301/存档）→ 新查询系统（登录墙/验证码如实记录）→ 前端 JS 包
（`jsrecon` 提接口）→ 公开 API 探测 → 移动端/H5/微信版 → 官方公报期次
（R14 主路线）→ Wayback/档案馆快照 → 上级部委/姊妹站镜像 → 商业数据库
（天眼查/企查查等，注明替代口径）。全部不通 → 0 结果诊断报告 + 证据文件
+ 最近可行替代，绝不假成功。

## R15 · 东方财富股吧（历史日期采集 SOP，强风控站点通用打法）

典型任务："抓某股吧某天的发帖标题、阅读量、评论数"。股吧是 SSR：数据就嵌在
页面 `<script>` 的 `window.article_list` 里，且历史翻页直接是 URL 页码参数。
按顺序走：

1. **侦察判型**：`fetch <吧URL>` → HTML 里有 `article_list` → 判为内嵌 JSON 页。
2. **身份核实墙**（em_capt）：HTTP 批量抓会触发"身份核实"并下发
   `wsc_checkuser_ok`/`st_psi` cookie。正路过法：`open-debug-chrome.sh` 弹调试
   Chrome → 用户完成一次核实 → CDP 附加（L3）。
3. **取数**：browser 型 + `"embedded_json": "window.article_list"`，
   `record.fields` 从记录键映射（title/read/comment 等）。
4. **历史回溯**：URL 直接指向目标页码（深链），配
   `"pagination": {"strategy": "none", "start": <目标页>, "max_pages": <目标页>}`，
   逐页点击"下一页"或逐个深链导航，**不要 HTTP 批量并发**（见下）。
5. **日期口径**：pipeline 里 regex 前缀过滤当天：
   `{"type": "filter", "field": "post_publish_time", "op": "regex", "pattern": "^2025-09-01"}`。
6. **断点续传**：需要可续跑的长回溯用 v3 任务包（`run --task --resume`，增量去重
   天然防重复）；v2 config 的 `--resume` 只覆盖详情阶段。

⚠️ **请求预算红线**：股吧类站点验证 cookie 有请求预算（实测约 140 次/会话），
HTTP 批量爆发不仅自身被封，还会**反噬正在工作的浏览器会话**（IP 连坐）。
历史采集全程用浏览器导航，克制、单线程、必要时分时段。

## R16 · Cloudflare / Next.js SPA（Product Hunt 类，headless 必被卡）

特征：`fetch` 直抓返回"Just a moment"/挑战页；`fetch --browser` headless 也被卡；
偶发 `ERR_CONNECTION_CLOSED`（连接重置也是风控表现，别反复硬试同一通道）。
正确打法：

1. **别跟 headless 较劲**——Cloudflare 指纹检测能识别自动化浏览器，升级阶梯
   直接跳到 L3：`bash "${SKILL_DIR}/scripts/open-debug-chrome.sh" "<目标URL>"`
   （真实 Chrome 人工过一次校验，端口 9222 保持开着，跨任务可复用）。
2. **配置 browser 型 + `"cdp": "http://127.0.0.1:9222"`** 附加该实例——之后的
   row_css 卡片提取、`scroll_count` 滚动加载、`pagination.type=click` 全部照常
   配置化（无限滚动用 R3，DOM 卡片选择器从"检查元素"里抄）。
3. **详情页列表字段**（如 PH 的 makers）：`detail.extract` 用
   `{"name": "makers", "type": "css_attr", "selector": "a[href^='/@']", "attr": "href"}`
   ——`limit` 缺省抓全部，多条换行连接；注意选择器口径，别把 upvoter 混进来。
4. **数据若在接口里**：附加成功后先跑一次 `capture: true` + actions 等待，
   Next.js 的 `__next_f` flight 数据有时能从接口/脚本里直接拿到，能直抓就不爬 UI。

## R17 · 淘宝系电商店铺（盒马/淘宝/天猫，mtop + 登录墙 + OCR）

目标特征：mtop/jsonp 接口、RGV587 会话标记、滑块=人工关卡、列表价格是
`priceEncoded` 不能直接用、配料/规格在详情 desc **图片**里。标准路线：

1. **L3 登录关卡**：`open-debug-chrome.sh` → 用户登录一次（淘宝系无登录态寸步难行）。
2. **找接口**：进店后先 capture_all（现在会**同时记录请求体**、自动剥 JSONP 壳）；
   或直接用页面自带的 mtop 库（`window.lib.mtop.H5Request`）——借页面自己的库，
   不逆向 sign，这是合规红线内唯一捷径。
3. **翻页**：接口参数在 body 里 → `pagination.strategy: "template"` +
   `json_body: {"pageIdx": "{{page}}"}`。
4. **价格红线**：列表接口的 `priceEncoded` 是加价后假象——价格必须进详情页 DOM 取。
5. **RGV587 = 会话已被标记**：引擎现在能识别（session_flagged）。正确动作是
   冷却几分钟 + 换路线，**不是重试**。兜底路线：主站搜索关键词 → 结果里按店铺名过滤
   （盒马战例靠它完成交付）。
6. **配料/规格 OCR**：食品/化妆品的配料表普遍在详情末尾的标签图里。CDP 截图 desc
   区域 → rapidocr 识别（doctor 会检查 `rapidocr_onnxruntime`）：
   ```python
   from rapidocr_onnxruntime import RapidOCR
   text = "\n".join(line[1] for line in RapidOCR()(img_path)[0])
   ```
7. **对比校验**：换 pageSize/翻页参数后抽查首末页字段完整性——参数换挡丢字段是
   该系接口的常见暗坑。

## R18 · 闲鱼/二手平台（登录墙 + JS 站 + 跨域 mtop）

目标特征：扫码登录强制、数据靠 JS 渲染、接口是跨域 mtop（**capture 录不到响应体**——
跨域 XHR 的 body 拿不到是 CDP 限制，别在 capture 路线上空转）。标准路线：

1. **L3 登录关卡**：`open-debug-chrome.sh` → 用户扫一次码；`cdp --login-state goofish.com`
   确认登录态（v1.9 修复了输出 bug）。
2. **列表**：browser 型 + `cdp` 附加 + **`actions` 排序点击**（v1.9 起透传到桥：
   `[{"type":"click","selector":"最新排序"}]`）+ `pagination.type: "js"` 点"下一页"。
3. **价格等无缝拼接字段**：用**结构化子字段**分开取，防 `¥5923人想要` 不可逆拼接：
   ```json
   "价格区": {"subs": {"价格": "span.price", "想要": "span.want"}}
   ```
4. **详情**：`detail.backend: "browser"`（v1.9 新增）——单次桥进程顺序导航全部详情 URL，
   复用同一 CDP 连接，不逐页起浏览器。`backend: "http"` 对登录+JS 站只会拿回空壳。
5. **文本派生字段**（使用次数/划痕）：pipeline `regex_extract`：
   `{"type":"regex_extract","field":"描述","pattern":"使用(\\d+)次","to":"使用次数"}`。
6. **iterate 多轮注意**：桥每轮会重连 CDP（已加重试），标签页已改为用完即关——
   不再积压拖垮 Chrome；仍建议单轮 ≤ 几百页。

## 交付前必做

```bash
PYTHONPATH="${SKILL_DIR}" python3 -m universal_scraper.cli verify --file "<任务目录>/数据.json" --network
PYTHONPATH="${SKILL_DIR}" python3 -m universal_scraper.cli report "<任务目录>/数据.csv" --out "<任务目录>/report.html"
```

`verify` 吃 JSON 结果文件做字段完整率/去重，`--network` 联网抽样重抓对比；
`report` 吃 CSV 出可视化 HTML（确切参数随时 `--help` 确认）。

## R19 · 配额收割（按 IP 计配额的批量下载站）

目标特征：能浏览、能拿列表，但下载/详情按 **IP 每日限量**（学术期刊 PDF、
报告站、图库原图）。判型见 playbook 第七章（先确认是按 IP 计，别对全局/按资源
配额误用本配方）。标准动线：

1. **建池（目标站校验）**：
   ```bash
   PYTHONPATH="${SKILL_DIR}" python3 -m universal_scraper.cli proxy --refresh \
     --target-url "https://<目标站>/某重页面" --marker "<站名唯一文案>"
   ```
   战训：通用靶（example.com）37.5% 可用 ≈ 目标站 0% 可用；**校验必须打目标站**
   （重页面 + 内容标记），可用率 10~20% 才是真实数字。
2. **三态账本**：`outputs/proxies.pool.json` 记 fresh/alive/dead/burned；
   burned（当日配额烧尽）拉黑到次日，dead 30 分钟可复活。
   `cli proxy --status` 查看分布。
3. **切片分工**：worker i ← 清单第 i 段（`quota_ledger.worker_chunk`），
   每代理上限 = 观察墙 × 0.75（墙 20 → 设 15）。
4. **指纹绑定**：代理只换网络身份；请求仍须 `curl_cffi impersonate="chrome"` +
   完整浏览器头（含 `Sec-Fetch-Dest/Mode/Site`——2026-09 起常见校验项）。
5. **失败分类路由**（`quota_ledger.route_failure`）：超时=换 worker 不动账本；
   被拒=记账冷却；连续多 IP 全拒=全局熔断，全队静默等窗口；磁盘错误=暂停且
   **不烧代理**。
6. **半衰期补给**：免费代理 ~40 分钟衰减一半，池子边用边补（每轮抽样 600~800）。

## R20 · 机构通道（学术资源终结者：VPN + 知网/万方）

适用：目标是被数据库收录的文献/报告，且用户有高校/机构身份。官网通道被配额
锁死时的最快正路（战例：官网 3 天 1250 篇，知网通道 40 分钟 139 篇）。动线：

1. **用户连机构 VPN**（这一步必须用户本人；`cli ip` 确认出口变为教育网）。
2. 调试 Chrome 打开 `navi.cnki.net/knavi/journals/<刊名代码>/detail`
   （滑块验证交给用户点一次）。
3. 左侧年份 `dt` → 期次 `a`（`JournalDetail.BindIssueClick`）→ 列表页
   `a[href*=kcms2/article/abstract]` 取详情链接，按标题归一化匹配目标清单
   （去标点/空格后前缀比对）。
4. 详情页 `#pdfDown.href`（bar.cnki.net 下载订单链接）→ CDP
   `Browser.setDownloadBehavior` 指定下载目录 → 导航触发下载 → 轮询
   `.crdownload`→`.pdf` 完成 → `%PDF` 头校验 → 按任务清单标准名归档。
5. 标签页管理：详情标签导航到下载 URL 后域名会变，**按排除法**（非 navi 的页面）
   找回标签复用。
6. 断点续传：已归档清单驱动跳过；超时文章换轮重试（知网下载服务偶发 1~3 分钟挂起，
   放宽等待比判死刑好）。

## R21 · 通道组合与切换时机（成本决策）

单一通道打不动时，按**边际成本/条**决策切换。战例全景：

| 通道 | 配额制度 | 成本/篇 | 启用时机 |
|---|---|---|---|
| 官网直连 | 每 IP 日 ~20 | 低（额度内） | 起手默认 |
| 官网+代理池 | 同上×N | 中（池维护） | 需要放量时（R19） |
| 官方镜像（如 CAST 集群） | 常免登录直链 | 极低 | 一开始就探（本期次是否有货） |
| 官网登录态 | 匿名全局预算→登录按 IP | 中 | 匿名预算烧尽时升阶 |
| 数据库（知网/万方） | 机构订阅 | 极低 | 机构身份可用时优先 |
| Wayback/公报 | 无配额 | 看覆盖 | 历史版本/防删 |

铁律：**成本/篇暴涨 10 倍以上（如全局预算锁死）立即评估切换**，别在锁死通道上
消耗时间——战例里我们多熬了两天才切知网，多付的两天就是决策学费。

## R22 · 无人值守长跑（过夜挂机自检清单）

批量任务要过夜时，启动前逐项自检（全部来自真实翻车）：

- [ ] **电源**：接电（电池模式合盖即睡，caffeinate 全无效；`cli ip` 可查）；
- [ ] **防睡眠**：`caffeinate -s`（系统级；`-i` 只防闲置不防合盖）；
- [ ] **锁死直连**：进程内 `os.environ["no_proxy"]="*"`，防系统代理中途劫持换出口；
- [ ] **原子状态**：所有 state 写入走 tmp+replace；读侧容忍损坏（备份 .corrupt 重建）；
- [ ] **分级看门狗**：单请求 25s / 预热 25s / 批次 = 上限×每篇预估+120s（SIGALRM）；
- [ ] **胜利退出**：清单清空立即退出（防空转刷池）；熔断期拒绝不计入"池耗尽"；
- [ ] **磁盘探测**：每轮写探针文件，外接盘休眠/掉线时暂停且**不烧代理**；
- [ ] **日志**：`print(flush=True)` 直写文件，别过管道（管道缓冲会吞日志误导排障）；
- [ ] **有产出即重置耐心**：池耗尽自动刷新重试，但连续 N 轮零产出要退出汇报。

## R23 · 数据型任务 API 优先动线（batch1700 战训：400 项实测约 60% 数据在 API）

适用：目标是数值/行情/名单/统计类数据（汇率、指数、成交排名、登记名单、月度宏观数据）。
这类站几乎清一色"JS 壳 + POST JSON 接口"（chinamoney/xkz/NAFMII/AMAC 全是），
HTML+选择器是最后手段。标准动线：

1. **判型侦察**：`fetch <url>` 看是壳还是数据页；壳页直接进 2。
2. **jsrecon**：拿 `base_urls` 锚点与 `path_fragments`（v1.12 起）——axios 实例的
   baseURL 是改写配置的锚。
3. **capture_all**（browser+cdp 配置）：页面自然操作一轮，所有 JSON 响应连同
   **POST 体/方法/Content-Type** 落盘（v1.12 起主路径全透传）。
4. **capture2config capture_all.json**：一键生成 http_json 配置草案——
   翻页参数自动模板化 `{{page}}`，records_path 已猜好。
5. **小样 → 全量**：`run --limit 2` 验证字段映射与翻页，再放 max_pages。
6. **翻页上限未知 → 倍增探测**：max_pages 从 2→4→8→16 翻倍直到空页/重复页
   （比逐页试探快一个数量级；比二分实现简单）。总页数 = 最后一个非空页。
7. **0 结果分叉**：先按 playbook 六·一判定"数据不存在(nodata)"还是"没抓到(failed)"——
   400 项实测里 failed 近半其实是 nodata，别修一个没有修复对象的问题。

禁忌：不要在没做 2/3 之前就写 CSS 选择器解析 HTML 表格——那是对 API 站最贵的误解。

## R24 · WebSearch 数据源发现通道（batch2200 战训：第二大取数来源，~30% 任务）

WebSearch 不是抓取工具，是**通道发现与核验工具**——在"该数据是否存在/在哪存在"
这个问题上性价比最高。使用纪律：

1. **什么时候用**：任务目标陌生（没建过管道）→ 先 WebSearch 确认数据是否公开、
   谁发布、什么格式——再决定走 R23（API 优先）/ R19（配额收割）还是直接判 nodata。
2. **怎么用**：查三类问题——①"XX 数据 是否公开 / 官网"（找官方发布页）；
   ②"XX 指数 历史数据 下载"（找静态文件入口）；③某站报错时"XX API 文档/限流"
   （确认是自己的问题还是站方规则）。
3. **交叉核验义务**：WebSearch 找到的二手聚合站只作**核对**，交付数据必须来自
   官方源或标注来源分级（官方 > 聚合器 > 新闻转述）。
4. **历史回溯窗口**：很多接口只留近 3~6 个月数据（如新浪全球期货
   getGlobalFuturesDailyKLine）——历史日期取空≠接口坏，先查窗口再判 nodata
   （playbook 六·一）。

## 四种取数模式速查（选型表）

| 模式 | 适用特征 | 配方/工具 |
|---|---|---|
| **JSON API 直抓** | 数据在接口里（60% 的数据型任务） | R23：jsrecon→capture→capture2config→小样→全量 |
| **静态文件下载** | 官方统计页挂 XLSX/CSV/PDF | `fetch` + `pdf --download`（断点/校验）+ `pdf --tables`（表格抽取） |
| **HTML 静态页** | 服务端渲染表格 | `fetch --json` / `run --config`（http_html + 选择器） |
| **浏览器渲染** | JS 壳/登录态/强风控 | R13/R17/R20：browser+cdp capture → 接口或 DOM |

选型顺序就是表格顺序：**能 API 不静态，能静态不渲染**（取数成本差十倍，见 playbook 7.5 前提失效检测的对照法）。
