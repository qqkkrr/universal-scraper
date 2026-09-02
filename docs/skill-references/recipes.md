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

## 交付前必做

```bash
PYTHONPATH="${SKILL_DIR}" python3 -m universal_scraper.cli verify --file "<任务目录>/数据.json" --network
PYTHONPATH="${SKILL_DIR}" python3 -m universal_scraper.cli report "<任务目录>/数据.csv" --out "<任务目录>/report.html"
```

`verify` 吃 JSON 结果文件做字段完整率/去重，`--network` 联网抽样重抓对比；
`report` 吃 CSV 出可视化 HTML（确切参数随时 `--help` 确认）。
