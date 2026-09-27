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
  "record": {"fields": {"标题": {"from": "标题"}, "链接": {"from": "链接"},
             "url": {"from": "链接"}}},
  "output": {"dir": "<任务目录>", "base_name": "list", "formats": ["json", "csv", "xlsx"]},
  "anti_bot": {"min_interval": 1.0, "max_retries": 3, "http_backend": "requests"}
}
```

> 交付前 `verify` 会把全空 `url` 列判死列（抽取链路断裂）——record.fields 里
> 把 `url` 映射到链接字段可避免误报。

**B. 页码在路径里**（`/page/2/` 型）——改用"跟随下一页链接"：

```json
"pagination": {"next_selector": "li.next a", "max_pages": 50}
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
  "extract": [{"name": "正文", "type": "css_text", "selector": "div.article"}],
  "concurrency": 2,
  "interval": 0.5
}
```

> extract 每项必须带 `type`（css_text/css_attr/xpath_text…，见 spec-schema.md），
> 选择器放 `selector` 键。

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
journal --site sytyxb --since 2020 --out "<任务目录>/PDF"   # 期刊标识见 KNOWN_JOURNALS
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
- [ ] **钉死 python 解释器**（2026-09-10 NBS 夜测事故）：launchd/cron 的最小 PATH 下
  `python3` 可能是系统自带的——没有 curl_cffi/requests，插件会**静默降级 urllib**
  （WARN 只在 stderr，stdout 照常出结果，极难察觉），整晚测的是降级通道。
  定时任务一律写绝对路径 `/opt/anaconda3/bin/python3`；启动后 90 秒内抽验 1 项
  真实判型（diagnose 应返回站点真实类型而非 net_error），不对就停下修环境，
  别让废数据跑一宿。

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

## R25 · 批次编排（多代理/并行批次模式，batch2400 战训：21 并行子代理 × 200 项）

适用：任务量大到需要**多个并行子代理**分工时。skill 单任务导向，本配方补上
调度层的标准件（GLM 手搓 AGENT_GUIDE.md + summary.json 契约 + 续跑逻辑的内置化）。

1. **建队列**：任务清单 → tasks.json（含 priority、每任务的输出目录约定）。
2. **生成规范**：`cli guide --out AGENT_GUIDE.md` 发给每个子代理——
   铁律/目录契约/证据 schema/续跑约定在并发下不变形的关键。
3. **领取任务**：子代理用 `batch --queue tasks.json claim`（原子领取，标 running；
   崩溃任务 30 分钟过期自动回收回 pending，不会丢）。
4. **并发上限（重要，全靠踩坑换来的数字）**：
   - HTTP 请求并发 ≤6、同域间隔 ≥1s；
   - **并行 LLM 子代理 ≤8**——batch2400 实测 19 并发吃了 8 批共 1302 次
     LLM API 限速失败；遇 429 指数退避，连续 3 批限速 → 并发减半；
   - 同一域名禁止两个子代理同时猛打（按域名分片）。
5. **断点续跑**：每任务一个目录（outputs/task_<编号>/，布局见
   references/evidence-schema.md）；"已有 done/summary 则跳过"；
   崩溃任务由 `claim` 的 stale 回收归还。
6. **收尾审计**：每任务 `verify --dir`（verdict=ok 才交付）；
   批次收尾 `batch status` 汇总 done/failed/nodata/blocked/pending。

## 数据型任务的替代验证标准（playbook 六·一的姊妹条）

"样本先行"为列表页交互采集设计；**数据型任务**（官方 API/统计页/月报）在
自主模式下逐个等确认不现实，替代验证三件套（缺一不可）：

1. **字段完整率**：`verify --dir` 输出 ≥0.9（关键字段无空值）；
2. **数值合理性**：数值在常识范围（增长率 -20%~+20%、指数 0~10000、价格 >0），
   报告期与任务要求一致；
3. **交叉源比对**：与第二个独立来源（官方另一页/聚合器/历史已验证数据）比对一致。

三件套通过的交付物标注"已按替代验证标准核验"；达不到 → 按六·一分叉处置。

## R26 · 瑞数(RiverSecurity)强防护站（NMPA 战训：$_ts 系 gov 站通用）

**适用症状**：HTTP 全通道（含 curl_cffi chrome 指纹）412/403，响应体是
`$_ts=window['$_ts']...` 混淆 JS；`cli diagnose <url>` 直接判 riversafe。
典型：国家药监局 datasearch、大量采用瑞数的部委/省局查询站。

**核心认知**：这不是配额问题也不是 IP 问题（家宽直连照样 412），是 JS 动态
challenge——必须真实浏览器执行。**判型确认后禁止在 HTTP 通道做任何重试**
（每次都在烧请求且必失败）；预算台账也不会因此记账（通道拦截 ≠ 配额耗尽）。

**动线（一条命令，配置驱动）**：
```bash
node "${SKILL_DIR}/scripts/rs_harvest.cjs" --config harvest.json
```
harvest.json 骨架（NMPA 实测可用，换站只改三处：goto、steps 选择器、intercept）：
```json
{
  "goto": "https://www.nmpa.gov.cn/datasearch/home-index.html",
  "gotoWait": 5000,
  "steps": [
    {"click": "input.el-input__inner", "wait": 1500},
    {"click": ".el-select-dropdown__item:has-text(\"境内生产药品\")", "wait": 2000},
    {"fill": {"selector": "input[placeholder*=\"批准文号\"]", "text": "阿莫西林"}, "wait": 800},
    {"click": ".search-input .el-icon-search", "popup": true, "wait": 6000}
  ],
  "intercept": {"urlPattern": "/data/nmpadata/search",
                "listPath": "data.list", "totalPath": "data.total"},
  "fields": {"批准文号": "f0", "产品名称": "f1", "生产单位": "f2", "药品本位码": "f3"},
  "paginate": {"mode": "jumper", "from": 2},
  "outJson": "out/rows.json", "outCsv": "out/rows.csv"
}
```

**三个必守细节（都踩过坑）**：
1. 结果常弹**新标签页**：查询按钮步骤必须 `popup: true`，拦截器会自动跟到新页；
2. 翻页数据**拦 XHR JSON**（`intercept`），不要解析 DOM innerText——Element UI
   文本流解析出来的是错的；
3. 复用已过挑战的标签页（`reuseTabPattern`）可跳过 UI 流程直接翻页续抓；
   反复新开页面会加剧瑞数升级挑战、标签页堆积。

**核验口径**：报告必须对齐接口 `total`（如 575/575），唯一键去重数 = 抓取数。

## R27 · 官方统计数据库（data.stats.gov.cn 国家数据，2026-09-11 实战验证）

**适用**：国家统计局国家数据平台及同构的"官方数据库 SPA + 公开 JSON API"站。

**⚠️ 先记住这条死路（2026-09-11 实测，别再试）**：旧接口 `easyquery.htm`
（m=getTree/getData 那套）已被 **UrlACL 403 全量封禁——真实 Chrome 也 403**，
与指纹/cookie 无关。网上旧教程全教这条，照抄必死。

**有效链路（三步，全部 curl_cffi 可直调，已逐环验证）**：

1. 建会话拿 cookie：`GET https://data.stats.gov.cn/`（拿 JSESSIONID）；
2. 指标树：`GET /dg/website/publicrelease/web/external/new/queryIndexTreeAsync?pid=&code=1`
   ——顶层是"月度数据"等；子节点传 `pid=<父节点._id>` 递归（注意用 `_id` 字段，
   不是 treeinfo_globalid）；`isLeaf=true` 即到表/指标层；
3. 指标与数值：
   `GET /dg/website/publicrelease/web/external/new/queryIndicatorsByCid?cid=<节点id>`
   → 指标清单（含指标 id）；再
   `POST /dg/website/publicrelease/web/external/stream/esData`，
   **Content-Type: application/json，请求体必须含全部 7 键**（2026-09-11 考核
   复盘修正：只发 `{cid, indicatorIds}` 会 **HTTP 500**——缺 `das`/`dts`/`rootId`
   服务端直接空指针，4 种补头形态都救不回来）：
   ```json
   {"cid": "<节点id>",
    "indicatorIds": ["<指标id>", ...],
    "daCatalogId": "",
    "das": [{"text": "全国", "value": "000000000000"}],
    "showType": "1",
    "dts": ["202508MM-202608MM"],
    "rootId": "<月度数据树根的 _id，即 queryIndexTreeAsync?pid=&code=1 顶层节点的 _id>"}
   ```
   → 按期数值（`data[].code="202608MM"` + `values[]`）。`dts` 是闭区间
   YYYYMM+`MM` 后缀；要最近 13 个月就写 `<去年同月>-<本月>`。
   完整契约抓取法：浏览器开月度数据页默认会自动发一次 esData，抓全量
   postData 即可（抓 XHR 时 postData 不要截断——本配方第一版就栽在这）。

**铁律——限流挑战（实测触发过）**：突发连续约 40 次请求后，站点返回 200 的
**混淆 JS 挑战壳**（"Please enable JavaScript"，38KB，or obfuscator 风格），
非 JSON。`cli diagnose` 已能识别（js_shell·限流触发）。纪律：
- 同会话复用 + 每请求间隔 **≥3 秒**；一次任务只要一个 Session；
- 见到挑战壳立即冷却 ≥60s 再继续，**别换指纹硬闯**（挑战与 IP 绑定）；
- 树遍历用 BFS 剪枝（按名称匹配目标目录），不要全树扫描。

**判型口径**：`cli diagnose https://data.stats.gov.cn/` → 200 正常（SPA 壳）。
403+`reason:UrlACL` = 访问旧接口，换新 API 基座；200+38KB 挑战壳 = 限流，降速。

**核验**：`verify --file rows.json --expect "产量,!价格"`（语义校验：
2026-09-10 夜测曾把"市场价格"表当"产量"数据判成功——完整率查不出语义错位）。

## R28 · 巨潮资讯 cninfo（上市公司公告，2026-09 双版本考核实战验证）

**适用**：上市公司公告列表/年报/摘要 PDF 下载（沪深两市）。

**列表接口（POST 表单，公开无需登录）**：
```
POST http://www.cninfo.com.cn/new/hisAnnouncement/query
Content-Type: application/x-www-form-urlencoded
X-Requested-With: XMLHttpRequest + Referer: http://www.cninfo.com.cn/new/...
pageNum=1&pageSize=30&column=szse&tabName=fulltext
&stock=000001,gssz0000001&seDate=2025-03-01~2025-03-31&isHLtitle=true
```
- `stock` = `代码,orgId`：orgId 规则 **沪市 `gssh06xxxxxx`、深市 `gssz0xxxxxx`**；
  用 `topSearch/query?keyWord=招商银行` 查 orgId（第二来源交叉）；
- 沪市 `column=sse`，深市 `column=szse`；`category=category_ndbg_szsh` 过滤年报；
- 响应 `announcements[].adjunctUrl`（如 `finalpage/2025-03-15/1222806509.PDF`）
  → 下载拼 `http://static.cninfo.com.cn/` + adjunctUrl；
- **陷阱**：`searchkey` 命中的标题带 `<em>` 高亮标签，落库前须 strip；
- 下载校验：`%PDF` 魔数 + Content-Length；`adjunctSize`（KB）可与实际字节数核对。

**已知真值（2026-09-11 核）**：招商银行 000001 全年 2024 公告 77 篇；
《2024年年度报告摘要》= `finalpage/2025-03-15/1222806509.PDF` = 921,328 字节。

**可直接照抄的完整配置（2026-09-15 凌晨实测 150 条 ✅）**——不带 Referer 头
会被 403（实证踩坑），`anti_bot.headers` 三件套是必须项：

```json
{
  "name": "cninfo_szse_ann",
  "start_urls": ["http://www.cninfo.com.cn/new/hisAnnouncement/query"],
  "source": {
    "type": "http_json",
    "method": "POST",
    "url": "http://www.cninfo.com.cn/new/hisAnnouncement/query",
    "body": "pageNum={{page}}&pageSize=30&column=szse&tabName=fulltext&plate=&stock=&searchkey=&secid=&category=&trade=&seDate=2026-09-01~2026-09-14",
    "records_path": "announcements"
  },
  "pagination": {"strategy": "template", "max_pages": 5, "records_path": "announcements"},
  "record": {"fields": {"标题": {"from": "announcementTitle"},
                         "附件路径": {"from": "adjunctUrl"},
                         "时间戳": {"from": "announcementTime"}}},
  "output": {"dir": "<任务目录>", "base_name": "cninfo_szse", "formats": ["json", "csv", "xlsx"]},
  "anti_bot": {
    "min_interval": 2.0, "max_retries": 2,
    "headers": {
      "Referer": "http://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/search",
      "X-Requested-With": "XMLHttpRequest",
      "Origin": "http://www.cninfo.com.cn"
    }
  }
}
```
（附件下载 = `http://static.cninfo.com.cn/` + 附件路径；`seDate` 按需改窗口。）

**请求预算纪律**：这类任务请用 `run --max-requests N` 硬闸（列表 1 页 + PDF 2 次 ≈ 4 次真实请求；
重试也计数）。预算紧张时的验证协议见 Full 版 `SKILL.md` 的「数据型任务的替代验证」小节
（Lite 手册对应"铁律 2 的数据型任务三件套"）。

## 紧预算验证协议（2026-09 双版本考核战训，替代验证的降档规则）

任务带 `--max-requests` 硬预算、剩余额度不足以"重抓 ≥10 条比对"时，
验证三件套降级为——① **原始响应对账**：留存的 capture/响应体直接离线重解析比对
（0 请求）；② **第二来源单点交叉**：只对关键字段做 1-2 次独立来源核对；
③ **原文回读**：PDF/长文抽 3-5 个单元格回读原文核对（0 请求）。
预算不足不是跳过验证的理由，是换更便宜的验证方式；报告里注明用了哪档。

**R27 三条易错细节（2026-09-11 重考实战补）**：
1. `indicatorIds` 用指标清单行的 32 位 **`_id`**——不是 `kj1`/`ek`/`ek_dp`（那是展示用复合键）；
2. 同一指标按**年代切片**成多个叶表（"(2026-)"、"(2021-2025)"…）：要最近 6 个月就选
   含目标期间的叶表（(2026-)），选错年代 = 空结果，不是报错；
3. **未发布期以"占位期行 + 空 value"返回**（如 9 月 CPI 在 9 月 11 日查询时：
   行在、值为 ""）——不是缺行。判定规则：`value==""` 或无 `data` 键 = 该期未发布，
   如实报 nodata，不得当 0 或跳过。

**R27 参考成本单（重考实测）**：判型 1 + 会话首页 1 + 树/指标清单 6 + esData 3
≈ **11 次**（留 2–4 次给二源复核）。20 次预算绰绰有余；树遍历务必按名剪枝，
不剪枝的全树扫描（40+ 节点）本身就是挑战壳诱因。

**平台口径备注**：CPI 的 `du_name=%`、PPI 的 `du_name=无`（同型指标单位元数据
不一致，别按单位名硬校验）；地区名 `da_name` 在"国家/全国"间跳变；历史数据
可能修订，跨天核对允许 ±0.1 级差异。

**R27 路线图（考核反馈 P1，待实现）**：多步自适应 API 链值得一个一等公民命令
（如 `cli session --budget 20 --gap 3.2`）：内置 cookie jar、逐次计数、挑战壳
冷却钩子、逐请求 JSONL 证据、raw 落盘限额。当前替代：按本配方自写执行器
（考核重考实现约 200 行：curl_cffi 单会话 + 节流 + 硬预算 + JSONL 证据），
注意用 `looks_like_challenge()`（diagnose 模块导出）统一判定挑战壳，
正常 SPA 首页（几 KB、无混淆脚本）不是挑战壳。

## R29 · 结果集上限分片法（裁判文书网战训 2026-09 DeepSeek 考核；所有"单查询只出前 N 条"的站通用）

**适用症状**：单次查询固定只返回前 600 条（或 500/1000），目标总量远超上限。

**分片维度优先级**：时间（最稳、天然有序）→ 地域 → 案由 → 审级。
- 先按月/周切时间片，把每片压到上限以内；
- 单片仍超限 → 对该片按地域（省→市）或案由子树二分，递归降维；
- **K 值自适应**：首轮抽样测出单片典型量 Q，分片粒度 = 上限 × 0.6 / Q
  （留 40% 余量防尾部堆积）。

**自校验铁律：子片计数之和必须等于父片计数**（±1 页容差）。不等 = 有片
丢条件或重复覆盖——**宁可停机排查，不许带着缺口继续**。分片边界写反
（如 `start <= t < end` 写成 `<=`）曾导致死循环烧穿预算。

**结果反证条件（强制规范）**：该站实测三个静默陷阱——①改"每页条数"会**悄悄
丢掉**日期筛选（页面 chips 还显示着）；②日期区间每次页面加载只能设一次；
③查询状态在服务端 session。规范：**每次查询后必须用结果反证请求条件**
（如校验首条记录日期落在请求区间内），反证失败 = 本页数据作废，不是
"看起来对就行"。

**站点档案（wenshu.court.gov.cn）**：
- 单查询 600 条上限；**超长文书网页端不渲染正文**；
- 下载全文需实名且须为案件当事人（自动化拿不到正文属正常，如实交付元数据）；
- **高频抓取直接封号**（实测教训）；政府/司法站总请求 >300 必须用户确认，
  间隔 ≥3s，见变慢即收手。

**阻断页识别（已内置）**：短页（<2KB）含"封禁/违规/访问异常"等指纹或连续
3 页同长短页 → 插件自动硬停机（BlockDetectedError，`run` exit 5 /
`session` exit 5，blocked.json 落证据）——**别自己在下游把封禁页当空数据写盘**。

**R22 追加（裁判文书网战训）**：macOS launchd/cron 拉起的 bash 读 `~/Documents`、
`~/Desktop` 会被 TCC 拦（`Operation not permitted`）——脚本和数据目录放
`~/.universal-scraper/` 或任务自建目录，或改用 node 直接启动。

## R30 · 验证码把门 + 行为评分站（gsxt 国家企业公示系统，2026-09 实战验证）

**适用形态**：搜索/查询接口被验证码（GT4 文字点选/图标九宫格）+ 行为评分把门；
封禁按 10-25 分钟一档升级；**HTTP 200 空页 = 封锁态**（软封锁，账面上无痕）。

**核心认知**：这类站的配额单位不是"请求数"，是"每小时 N 次贵动作（搜索），
**失败也扣额度**"。详情页/翻页是便宜动作。全部工具已内置（cli captcha /
budget --probe / budget --action），不要再现场手写 CDP 守护和探针脚本。

**动线（照抄）**：
1. **判型**：`diagnose <url>`——加速乐 521 → 直接 L3 真 Chrome（HTTP 伪装全挂）。
2. **人工在环前置检查**：调试 Chrome 必须开着（`cdp --list-tabs`）；
   登录墙字段（股东/年报等）由用户本人登录，红线不绕。
3. **开桥**：`cli captcha --start --dir /tmp/gsxt_bridge --url <入口>`——
   CDP 附加真 Chrome，真指纹+真登录态，用户随时可以在窗口里人工点码。
4. **探放行窗口**：`cli budget --probe <入口URL> --expect <站名标记> --every 60`
   → OPEN 才继续（exit 0=放行，2=仍封）。
5. **贵动作记账**：`cli budget --action gsxt-search --action-limit 3 --action-window 1800`
   → exit 0 扣额成功才发搜索（exit 2=超额，转做详情/翻页或等 reset_in 秒）。
6. **验证码**：文字点选（"依次点击：国 家 税"）→
   `cli captcha --solve --prompt "依次点击：国 家 税" --captcha-selector <主图选择器>
   --wait-selector <通过特征>`（ddddocr det+cls 自动点）；
   图标九宫格 → 不给 --prompt，人工在环（用户在窗口里点，脚本轮询
   `--wait-selector`）。**验证码失败=窗口作废，别硬闯重搜**（封禁升级）。
7. **窗口内榨干便宜动作**：收 10-20 候选 → 逐个详情 → 翻页；详情 GUID 是
   短时效令牌（约 2-3 分钟），必须从结果页真实点击获得，直接 goto 会报错。
8. **交付**：字段受登录墙限制只拿到部分 → 如实说明（0 结果诊断报告结构）。

**处方速查**：200 空页 = 软封锁（探针判 EMPTY_200）→ 等 10-25 分钟再探；
连搜 2 次 412 → 行为评分升级 → 停手等窗口；验证码图标码 → 人工在环不硬解。

## R31 · 号段枚举旁路（epub.cnipa.gov.cn 专利公布公告，2026-09 实战验证）

**适用形态**：搜索/查询接口按 IP 施加**个位数日配额**（实测 6-10 次/日），
但**详情 GET 通道不吃配额**（封锁期照常 200），且详情标识符（公布号/编号）
按期次连续编排——可以纯 GET 逐号扫描收数。

**核心认知**：配额锁死的是"搜索"这个动作，不是"数据"。号段连续性 =
搜索的免费替代品。配方可迁移到一切"ID 连续 + 搜索限配额"的站点
（政府公告、工商登记、标准号、裁判文书同类）。

**动线（照抄）**：
1. **判型**：`cli diagnose <url>`——瑞数挑战壳**任何状态码**（412/200/202），
   响应体含 `$_ts=window['$_ts']` 即判 L3（epub 实测 HTTP 202 曾被误判 L0）。
2. **过挑战**：`bash scripts/open-debug-chrome.sh` 起调试 Chrome → 手动访问
   一次目标站过瑞数挑战（CDP 9222 后续复用会话）。
3. **找锚点号**：人工查一个目标日期范围内的已知公布号（如 CN118423231A）
   ——这是枚举起点。
4. **逐号扫描**：`GET /patent/{公布号}` 逐号 +1，读返回页的"申请公布日"
   过滤目标日。命中则收著录字段；出界则跳号继续。5-6s/号，命中率实测 100%。
5. **会话下载附图**：附图 jpg 直连 400——必须带浏览器会话（CDP cookies）下载。

**实测数据**（epub.cnipa.gov.cn，2024-08-01 目标日）：
- 搜索 POST 配额：6-10 次/日（烧尽后静默 45min+，换 profile 无效=IP 级）
- 免费代理池：0/600 可用（瑞数挑战过不了，R19 判死刑）
- 号段枚举：50/50 命中（100%），5-6s/号，封锁期照常工作

**注意事项**：
- 同号段内技术领域相近（按 IPC 排序编号）——要领域多样性需跨号段取样
- 单查询超 10000 条不排序（服务端限制），号段枚举天然绕过
- 公报期次页有验证码+PDF 下载，不适合批量

**R22 追加（epub 战训）**：macOS 后台 shell `sleep` 会被 App Nap 节流
（实测循环落后计划 1.5 小时）——重试计时用**进程内定时器**（Python
`time.sleep` / Node `setTimeout`）+ `caffeinate -is`，别用 shell `sleep`。

## R32 · 强防护 + 登录墙 + SPA 框架站的三重降级（商标网 sbj.cnipa.gov.cn，2026-09 实战验证）

**适用形态**：站点同时具备 ① FE 前端挑战壳（`/_fec_sbu` 非 `$_ts`）+
导航频控 WAF（真浏览器导航 1-2 次即封）② 查询系统强制 SSO 登录
③ Angular 组件 fill() 合成事件失效 + 接口返回密文。三重叠加时逐级降级。

**核心认知**：
1. **导航频控下的动作链设计**——"每次完整导航值一次封锁窗口"，所以把
   点菜单→填条件→查询→capture 全部塞进**同一次导航的动作链**，
   活页内 XHR 交互不再触发封锁。
2. **Angular 表单降级阶梯**：fill → `type_real`（真实键盘）→ 读渲染 DOM
   （playbook 第四章有完整阶梯）。
3. **密文响应 = 换路不破解**：读渲染 DOM 或找官方 Download 按钮（红线）。
4. **登录墙不可得 → R24 替代源**（WebSearch 同实体数据源 → 同 schema 验证）：
   商标全量建库的正确答案是 ggfw.cnipa.gov.cn 官方数据开放批量下载，
   网页抓取路线实测 3900 万请求 = 3.7 年，不可行。
5. **全量任务先算请求量再选路**：数千万条 × 每条多次请求 → 网页路线
   数学上不可行时，官方开放数据/FTP/批量接口是唯一正路。

**动线（照抄）**：
1. `diagnose` 判型 → FE 挑战壳 + 登录墙确认。
2. L3 调试 Chrome + 人工登录一次 → 单导航动作链 + capture_all。
3. 全量需求 → 立即评估官方数据开放（WebSearch "site:gov.cn 商标 数据
   开放/批量下载"）→ R24 验证替代源 schema → 官方渠道收数。
4. 网页路线仅用于小样本/按需查询（每 10 分钟最多 1 次导航）。

**jsrecon 降级**（本战训触发的新能力）：挑战壳站 jsrecon HTTP 拉 JS 会
全灭——现已自动降级：检测到挑战壳且调试 Chrome 在线时，改从 CDP 网络
流量收 JS 包体再提取端点（无需手动切换）。

## R33 · 强签名社交平台全量采集（小红书动线，2026-09 实战验证）

**适用**：接口带签名（X-s/X-t 等）不逆向、需登录、SPA + SSR 混合的社交平台
（小红书已验证；同构站可套）。核心思想：**让页面自己的 JS 发签名请求，只拦截响应**。

**动线（照抄）**：
1. `diagnose` 判型 + 无登录侦察：确认登录墙（未登录时搜索页直发
   `login/qrcode/create`，接口零数据）。
2. `open-debug-chrome.sh` → 用户扫码登录一次 → 轮询 `web_session` cookie 确认。
3. **搜索建池**：CDP 附加，goto `search_result?keyword=…`，滚动加载，拦截
   `so.xiaohongshu.com/api/sns/web/v2/search/notes`——卡片即含点赞数/作者/
   **xsec_token**（打开笔记详情的必备令牌，离开搜索上下文就失效，必须落盘）。
   实测深度：约 50 屏 / 1000 条后连续无新增（平台上限，按"最新"回溯历史到不了）。
4. **逐篇详情+评论**：goto `/explore/{note_id}?xsec_token=…&xsec_source=pc_search`：
   - feed XHR 常缺省 → 读 SSR：`__INITIAL_STATE__.note.noteDetailMap[id].note`
     （**键名驼峰** `noteId/interactInfo/imageList`，递归归一为蛇形再入库）；
   - 评论翻页：唯一滚动容器 `.note-scroller`，**必须跳到最底部**才触发
     `comment/page`（每页10条）；滚轮落点错容器/增量不足都收不到；
   - 回复：`.show-more`「展开N条回复」，两段式点击（scrollIntoView→等450ms→
     **重查节点再 `el.click()`**，防 DOM 替换空点），拦 `comment/sub/page`；
   - 页面被站方 `window.close()` 反自动化关标签 → `addInitScript` 置空
     `window.close` + 关闭后重建标签页重试（判型表新增症状行）。
5. **作者主页**：goto `/user/profile/{id}`，拦 `user/otherinfo`（粉丝/关注/
   获赞与收藏/笔记数/小红书号/简介）+ `user_posted`（作品样本→算平均互动）。
6. **纪律**：并发=1、间隔≥1.3s 随机、461/-1 风控退避三连收手、逐请求 JSONL
   台账、逐实体落盘断点续跑。实测 500 篇×(详情+100评论+6展开)≈1.1 万请求零封禁。
7. **媒体**：图片/视频 CDN 直连无需签名，带 Referer 限速下载即可（原图取
   `image_list[].info_list[WB_DFT].url`，无水印视频取 `video.media.stream.h264`
   中分辨率最高一路 `master_url`）。

**桥脚本**：实战版见任务目录 `xhs_harvest.cjs`（search/notes/profiles 三模式，
可沉淀为通用桥）。

---

## R34 · B站 UP主年度全量采集（元数据+弹幕+评论，影视飓风战役 2026-09 代码化）

**通道架构**（四条，全纯 HTTP，浏览器只在拿行为指纹时值一次）：
1. 元数据 `x/web-interface/view?bvid=`（无 wbi）
2. 弹幕 `x/v1/dm/list.so?oid=<cid>`（deflate XML，无 wbi；`p` 前 8 段=时间/类型/…/颜色/…/真实时间/UID哈希）
3. 评论 `x/v2/reply/wbi/main`（**需 wbi 签名**；匿名 v2/reply 已返回空列表；匿名翻几页后 is_end 登录墙，正路=L4 登录复用；
   楼中楼只读内联首页 ≤3 条——更深层需 reply/reply 分页，`回复数` 字段核对缺口）
4. UP主列表 `x/space/wbi/arc/search`（wbi + **真实 dm_img_\*** 行为指纹）

**前置三件套**（-352 解法）：GET 主页预热 buvid + 固定 UA（`rotate_ua:false`）+ 全程 Referer bilibili.com。
**dm_img_\* 获取**：浏览器 capture 一次 UP主空间页 → `capture_all.json` 取真实
`dm_img_str/dm_cover_img_str/dm_img_inter` 原值重放（指纹非会话绑定可复用；随机伪造 -352→-412）。
**pagination_str 陷阱**：必须无空格紧凑 JSON `{"offset":""}`——带空格直接 -403。

**一键命令**（代码化沉淀，别再手写流水线）：
```bash
us bili --video BV1abc                                    # 单视频三通道
us bili --mid <UID> --year 2024 --dm-img-str '<capture原值>'   # UP主年度全量
```
输出 `outputs/bili_*/`：meta_<bvid>.json + 弹幕/评论 csv+json+xlsx（export_rows 统一出口，
`verify --dir` 可直接审计——JSONL 自定义产物也已支持）。

---

## R35 · 汽车之家纯电SUV全量采集（配置参数+成交价+口碑，2026-09 实战验证）

**站点形态**：多步 API 链 + 每页随机化反爬（HS CSS 伪元素字体反爬 + CSS 偏移），
配置引擎装不下——需「模式 C 自写采集器」（`scripts/safe_http_template.py` 复制即用）。

**四条数据通道**（全部 capture 捕获→HTTP 直抓）：
1. 车系列表 `car.api.autohome.com.cn`（JSON，无反爬）
2. 车型配置 `cfg.api.autohome.com.cn`（JSON，注意 JSON 键序列化为字符串后取值）
3. 口碑列表 `koubeiipv6.app.autohome.com.cn`（JSON，`k.autohome.com.cn` 域名已废弃）
4. 成交价（浏览器渲染页，`window.*` 内嵌状态 + HS 字体反爬——需 Playwright 渲染）

**反爬形态**：HS CSS 伪元素字体反爬——数字/汉字映射到自定义字体 codepoint，
视觉正常但 DOM 里的 charcode 是乱码。解法：用映射表（从页面字体文件逆向或
Playwright 渲染截图后 OCR 对照），或直接用口碑 API 的 JSON 数据（不经渲染层）。

**礼貌纪律**：单并发 1.2s 间隔 + 预算闸 4500 + 封禁退避（403 翻倍等待）。
全程 2180 请求零封禁（汽车之家战报实测）。

**脚本模板**：`scripts/safe_http_template.py`（Mimosa 预适配 + 预算闸 + JSONL 追加 +
verify 对接——复制后修改 `_ALLOWED_HOSTS` 和 API 端点即可）。

**verify 注意**：配置参数宽表 428 列大量选装项本来就该为空——`verify --dir`
的完整率读数偏低是正常现象，应区分「核心字段完整率」和「稀疏矩阵」（R102 汽车之家
战报落地的双口径输出，见 `universal_scraper/verify.py` 注释）。

## R36 · 12306 余票+票价一次直抓（2026-09 实战验证：北京→十城国庆 G/D，317 区间/1111 席别行，22 请求/轮）

**适用**：车次/票价/余票类查询。全程 L0 纯 HTTP（curl_cffi `impersonate="chrome"`），
无需浏览器、无需登录、无验证码——公开查询接口。

**动线**：
1. `GET https://kyfw.12306.cn/otn/leftTicket/init?linktypeid=dc` 预热 Cookie
   （JSESSIONID / BIGipServerotn / SF_cookie_2）；
2. 站名电码表 `GET /otn/resources/js/framework/station_name.js`
   （`@缩写|站名|电码|拼音|…|城市`；城市主码：北京=BJP 上海=SHH 广州=GZQ 深圳=SZQ
   成都=CDW 重庆=CQW 杭州=HZH 武汉=WHN 西安=XAY 南京=NJH 长沙=CSQ；
   **城市码查询自动展开市内全部车站**——北京南/北京西/北京朝阳/北京丰台/清河…一次全收）；
3. 查询 `GET /otn/leftTicket/queryZ?leftTicketDTO.train_date=YYYY-MM-DD&leftTicketDTO.from_station=BJP&leftTicketDTO.to_station=SHH&purpose_codes=ADULT`
   —— **queryZ 会 302 到当前有效端点（如 queryG，相对 Location，即所谓"动态签名"的真身）**：
   手动逐跳跟随（每跳复检白名单域），不要 auto-redirect；带 Referer=init 页；
4. 解析 `data.result[]` 竖线行（58 字段，2026-09 实测）：[1]预订状态（预订/候补/列车运行图调整,暂停发售）
   [2]train_no [3]车次号 [4/5]始发/终到电码 [6/7]出发/到达电码 [8]发车 [9]到达 [10]历时
   [16/17]站序 [26]无座 [30]二等 [31]一等 [32]商务 [34]折扣标识 [35]席别代码 [39]票价余票串；
5. **[39] 编码——官方前端 `queryLeftTicket_end_js.js` 的 `e()` 函数就是现成解码器，别自己猜**：
   每 10 字符一组 = 席别码(1) + 价格×10(5位,÷10=元,支持一位小数) + 余票数(4位)；
   同码重复组取首现（真实席别），末尾重复组是无座挂靠价（与挂靠席别同价，数量位 3000 是常数无意义）；
   余票显示规则：0→无，1-20→数字，≥21→有；解析数与 [30]/[31]/[32] 源站显示逐条互验
   （实测 818/818 一致——免费的强交叉验证）；
6. 旧 `queryTicketPrice` **已下线**（200 但 queryLeftNewDTO 空）——别再调；
7. 日期必须在 **15 天预售窗**内：过去日期/超预售期返回 200+HTML 错误页（title 铁路客户服务中心）——
   任务给的日期已过去时，逐线路取证留痕，改采同口径可售日期并在交付报告显式说明；
8. 席别码表（官方 JS seatTypeForHB + e()）：9商务 P特等 M一等 O二等 D优选一等 6高级软卧
   4软卧 I一等卧 F动卧 3硬卧 2软座 1硬座 A高级动卧 S二等包座 J高级动卧/高级软卧。

**坑**：
- 城市码查询下同一物理车次出现多行：①市内多上车站区间（G5 北京站¥672/北京南¥667）；
  ②经停多目标城市（G1 同时命中北京→上海与北京→南京）。均为独立可售商品，
  按 `(train_no,出发站,到达站)` 唯一化，**别按车次号去重**；物理车次数按 train_no 去重另计；
- "列车运行图调整,暂停发售"车次时间为占位符 24:00/99:59，如实保留、剔除出时间互洽校验；
- 票价是浮动折扣价（如京沪二等 598-694 元区间），与公布价不同属正常；
- 夜间 D 动卧（京广/陇海既有线，历时 11h+）二等座仅 192-238 元，是真实票价不是脏数据。

**礼貌纪律**：并发 1、间隔 2s、预算硬闸（10 线路全 G/D = 22 请求/轮）；
交付前做"隔数分钟重抓同线路，票价应 100% 稳定、余票数自然波动"的稳定性抽验。


## R37 · 官方数据双源深历史采集（福彩双色球 2003-2025，3397 期双源零封禁 2026-09 实战）

**何时用**：目标"官方站全量历史数据"，但主站结构化接口有**数据深度截止**（如 cwl.gov.cn 只到 2013 年），
老数据散落在官方行业站/静态页/已下架。核心动作线：**接口拿新、静态页拿全文、行业站补老、全量对账**。

1. **先摸清接口数据窗口再定架构**：`issueCount` 拉满（如 3200）看返回的最老记录——
   `issueStart/issueEnd/dayStart/dayEnd` 被**静默忽略**（不报错、仍回最新 N 条）的接口，
   深翻页参数就是摆设，别在参数组合上耗时间；
2. **表格字段与全文分离采集**：接口字段一次全拿（1 请求）；"公告全文/附件"逐期抓静态详情页
   （robots 允许的 .shtml），页面 JS 占位符（如号码显示 `-`）用接口值回填即得渲染后文本；
3. **老数据补源优先级**：官方行业站接口 > 官方静态栏目页 > Wayback；商业站（500彩票网 EdgeOne
   验证码墙）与论坛数据集只能当参考，不能当主源；补不到的字段**留空 + 数据说明标注**，不造数；
4. **中彩网接口**（福彩官方行业站，双色球 `lotteryId=1`）：
   `GET https://jc.zhcw.com/port/client_json.php?transactionType=10001001&lotteryId=1&type=2
   &pageNum=1&pageSize=200&startDate=YYYY-01-01&endDate=YYYY-12-31&tt=<random>&callback=cb`
   （JSONP 壳自己剥；pageSize 实测可 200，一年 1 请求；type=1 按期号区间 startIssue=较新 endIssue=较老；
   列表记录自带 winnerDetails 各奖级+prizePoolMoney 奖池，无需逐期详情）；
   **接口域名是 jc.zhcw.com**（www.zhcw.com/port/ 会 302 到 404 页），参数名从 kjsj.min.js 里抄；
5. **全量双源对账**：两源重叠年份逐期逐字段对账（本次 19580 次比对 0 不一致），
   交付报告给出完整率表 + 缺口清单 + 对账次数，这是"数据型任务替代验证三件套"的最强实现；
6. **礼貌纪律**：政府站 3s 间隔 + flock 预算硬闸（本次 2200 闸用 1981）+ JSONL 断点续跑 +
   连续 5 败熔断写 blocked.json；2000 请求级任务全程后台跑、周期性播报进度。

**坑**：
- "字体反爬/CSS 偏移"先实证再动手：cwl.gov.cn 新旧详情页均无自定义字体、无 CSS 偏移
  （号码是 JS 占位符），zhcw 的 `seqFrontWinningNum` 乱序字段是它的防复制痕迹但接口同时给真实顺序；
- 老详情页可能没有每期字段（如 2022-118 期前无每期兑奖截止日，只有 60 天规则文本），正则要容错；
- zhcw 老数据（2003-2004）奖金/奖池整体置零是**数据缺失不是真实空缺**（与奖池非零的 2005-2012
  中真实"一等奖空缺 0 注"区分），整段置 NULL 并标注，别把 0 当真值交付。

## R38 · 国家统计局"国家数据"全指标时间序列（data.stats.gov.cn 新版门户，2026-09 实战：价格指数三库 267 目录/2575 指标/35.2万观测点，563 请求零封锁）

**何时用**：从 data.stats.gov.cn 批量采集统计指标时间序列（任意分类、任意频率）。
新版门户（2024+ 改版）数据在 `/dg/website/publicrelease/web/external/*` 明文接口里，
**无签名、无加密参数、无验证码**（WZWS WAF 只拦旧 easyquery.htm 路径）——真正的难关是"版别目录"机制。

**三件套接口**（基址 `https://data.stats.gov.cn/dg/website/publicrelease/web/external`，
仅需 UA + Referer `https://data.stats.gov.cn/dg/website/page.html`，JSON/JSONP 免疫）：
1. 树：`GET /new/queryIndexTreeAsync?pid=&code={1|2|3}`（1=月度 hgyd、2=季度、3=年度库）。
   `pid=""` 只回库根（月度数据/季度数据/年度数据一个节点）；**大类目录在库根下一层**
   （再查 `pid=<库根id>`，取 name=="价格指数" 等目标大类）；目录节点带
   `isLeaf / sdate / edate / explain`（explain=指标解释，部分目录有）。
2. 指标：`GET /new/queryIndicatorsByCid?cid=<目录id>&dt=&name=` →
   `data.list[]` 含 `i_showname`（指标全名）、`ek`（指标编码）、`kj1_name`（口径单位，
   如"上年同月=100"）、`du_name`（量纲 %）、`i_annotation`（备注）。
3. 数据：`POST /stream/esData`（Content-Type: application/json；名为 stream 实为整段 JSON）：
   `{"cid":目录id, "indicatorIds":[内部ID...], "daCatalogId":"", "das":[{"text":"全国","value":"000000000000"}], "showType":"1", "dts":["起-止"], "rootId":<库根id>}`
   → `data[]{code时间码, name时间名, values[]{_id, i_showname, value, du_name, kj1_name...}}`。

**版别目录机制（本配方核心，踩错就拿不到历史数据）**：
- 指标按时段拆成多个兄弟目录：如 `全国居民消费价格分类指数(上年同月=100)` 下有
  `(2026-)`、`(2021-2025)`、`(2016-2020)`、`(-2015)` 四个版别目录，各自装着对应时段的指标；
- esData 的 dts 窗口与目录版别必须匹配：给 `(-2015)` 版别查 `198301MM-201512MM` 返回 396 行全有值；
  给当前版目录查宽窗 `198001MM-202612MM` 则 1983-2025 全是空时间轴、只有 2026 各月有值；
- **dts 推窗规则**：起=目录 sdate（None 则月/季 1980、年 1949 兜底），止=目录 edate（None 则当前年）；
  月 `YYYY01MM-YYYY12MM`、季 `YYYY01SS-YYYY04SS`、年 `YYYYYY-YYYYYY`，服务端自动裁剪到实际数据；
- 时间码格式：月 `202608MM`、季 `202603SS`（YYYY+0Q，Q∈1-4）、年 `2026YY`（从前端
  `.format("Y[0]Q")+"SS"` 构造代码里抄，别猜）。

**动线**：浏览器 capture 数据页一次录出三件套（`fetch --capture` + cdp）→ 纯 HTTP 重放 →
树 BFS 到 isLeaf → 每叶目录 queryIndicatorsByCid + esData → JSONL 增量落盘断点续跑 →
**剔除空轴行**（宽窗返回指标起报年前的纯时间轴，本次占原始行 42%）→
(指标内部ID+时间码) 去重（同一指标出现在多个重叠版别目录）。

**坑**：
- 旧 easyquery.htm（dbcode=hgyd/m=QueryData/A 码体系）已整站 UrlACL 403——curl、curl_cffi、
  真浏览器页面内同源 fetch 全部拦，教程里的老打法全部作废，别浪费时间试；
- `queryIndicatorsByCid` 的 `dt` 参数（"起年-止年"）是前端切时间时重拉指标列表用的，
  采集时留空即可拿到全量指标清单；
- 指标没有老 A 码（A01030101 类），代码字段用 `ek`（如 `6021702000021|b504...`），
  `指标内部ID`（GUID）是唯一键；
- 指标起报年前的时间轴行数值为空串（非 null），组装时按空轴剔除，
  但系列中断的真实缺月保留原样（源数据本来就没有）；
- 政府站纪律：3s 间隔、并发 1、flock 预算硬闸（本次 700 闸用 566）、JSONL 断点续跑；
  563 请求全程零 403、零验证码。

---

## R39 · 环境数据历史存档替代（目标站历史深度不足/数据是干扰值时同源改道，2026-09 aqistudy 实战）

**场景**：目标站接口能通、加密已解，但服务端限制历史窗口（aqistudy 小时数据只开约 1 年，2024 全年取不到），或页面数据列是随机干扰值（aqistudy 逐日表污染物列）。
**原则**：任务要的是**数据**不是**那个站**——找同源官方上游/权威存档，交付时注明口径差异。这不算"换需求"：aqistudy 的数据本来就来自 CNEMC，存档就是同源改道。

### 动线
1. **历史深度二分探测**：对同一 method 用 366d/400d/500d/700d/960d 单点探（间隔≥1s）。errcode 1011=窗口拒绝；返回无法解密=窗口外另一种行为；都说明"深度不足"而非"参数错"。
2. **WebSearch 找存档**：`"中国空气质量历史数据"` → QuotSoft.net/air/（2014/05 起，CNEMC 同源）：
   - `china_cities_YYYYMMDD.csv` 城市级（AQI+6参数+O3_8h+各24h滑动，列=城市名）
   - `china_sites_YYYYMMDD.csv` 全国站点级；`beijing_all/extra_YYYYMMDD.csv` 北京专用小文件（站点名即列头）
   - 官方页宣传"逐小时历史数据"的站≠历史都能给——存档站反而诚实。
3. **慢站提速**（单流 ~10KB/s）：HTTP Range 每文件 8 段并行 × 文件级并发 3（400KB 36s→12s，全年 1098 文件 ~35min）；断点续传判定=本地文件大小 == Range 探测（`bytes=0-0` 的 Content-Range 总长）。
4. **气象配源**：open-meteo ERA5 archive 一次 GET 全年逐时（`temperature_2m,relative_humidity_2m,wind_speed_10m,wind_direction_10m,surface_pressure,precipitation`；**wind_speed 默认 km/h，÷3.6 转 m/s**）；NOAA ISD 实测对照 `ncei.noaa.gov/data/global-hourly/access/2024/54511099999.csv`（**DATE 是 UTC，+8h 转北京时间**；TMP/DEW/SLP/WND 数值字段 ÷10，9999/99999 为缺测码）。
5. **官方锚点验证**：生态环境局/部委公布年月均值对照（北京 2024：PM2.5=30.5、PM10=54、SO2=3、NO2=24 μg/m³，O3=171 为"日max-8h 的 p90"评价口径≠小时均值 65）。PM10 偏高 ~9% 属沙尘过程+审核修正，SO2/NO2 ±0.3% 内。
6. **AQI 自洽反算**：按 HJ 633-2012 IAQI 分段从浓度反算（小时口径：PM 用 24h 限值、气体用 1h 限值、O3 取 1h/8h 大者）vs 发布值，r>0.9 即内部自洽。
7. **aqistudy 加密链备忘**（若只需近期窗口）：页面全局函数 `sZIy6N0BKIVsZFNRHr(method, obj, callback)` 直接 CDP evaluate 调用，回调给明文；诊断失败用页面的 `pvkWB5TRrM9kT5`+`d6o2KJrVnYKBvdlc6mTrx4` 手动复刻 ajax 看服务端 errcode。

### 交付纪律
- 主表/站点明细/气象分文件 + 交付说明（口径注记：日均 AQI vs 日评价 AQI、ERA5 是再分析值、ISD 机场站≠城区、缺测小时不插值）+ 验证报告。
- 慢站下载全程零封锁（1098 文件 5300+ Range 请求），礼貌参数：文件间隔自然由并发上限保证，无需额外加压。

---

## R40 · 小红书 Web 话题/搜索 Top 池采集（笔记+评论+作者主页，2026-09 实战：#大模型 Top20+28详情+3176评论+20主页）

**场景**：小红书强反爬（IP 信誉封锁 300012 / X-s 签名 / 频率限流 300013），网页端"话题"= 关键词搜索结果页（无独立话题 API），点赞 Top N + 评论 + 作者画像类任务。

### 前置合规
- 签名不逆向（红线）：全程真实登录态浏览器渲染，让页面自身发请求；验证码人工在环。
- 登录正路：patchright 有头窗口用户扫码一次，profile（`~/.universal-scraper/xhs_profile`）复用。

### 动线
1. **出口预检**：未登录 GET `www.xiaohongshu.com` 出现"安全限制 300012"= IP 被封，先换出口。**Chrome `--proxy-server` 实测被无视**（指假端口都能上网）——用 `patchright launchPersistentContext({proxy})` 库层代理，`ifconfig.me` 验证出口后再开工。
2. **常驻守护进程**：patchright persistent context + `page.on('response')` 把所有 xiaohongshu.com 的 API/SSR HTML 追加落 JSONL（对账和解析的唯一数据源）；一次性客户端脚本用 `connectOverCDP` 附着驱动，不重复开浏览器。
3. **搜索 Top 池**：goto `/search_result?keyword=%23话题词%23` → 点"筛选"→"最多点赞"→ 滚动加载 5-7 页。接口 `POST so.xiaohongshu.com/api/sns/web/v2/search/notes`：**排序在 body `filters:[{type:"sort_type",tags:["popularity_descending"]}]`（顶层 `sort` 恒 "general"）**；卡片 `note_card.interact_info.liked_count` 是精确值（SSR 详情里反而是"3.4万"展示值），直接按它排序锁定 Top N，检查 feed 顺序单调性兜底。
4. **逐篇详情+评论**：goto `/explore/{id}?xsec_token={卡片token}&xsec_source=pc_search`（token 从搜索卡带过来，不传给不出数据）。详情读 SSR HTML（运行时全局已删 `__INITIAL_STATE__`）：花括号配平提取 + `\bundefined\b`/`NaN`→null + `new Set(X)`/`new Map(X)`→X 才能 json.loads。评论：`.note-scroller` **跳底** `el.scrollTop=el.scrollHeight` 触发 `comment/page`（顶层 10 条/页，`hasMore` 缺失，cursor 停走即耗尽）；「展开N条回复」点掉触发 `comment/sub/page`；评论归属按 URL 参数 `note_id`。
5. **作者主页**：goto `/user/profile/{user_id}`，SSR HTML `user.userPageData`：basicInfo(nickname/redId/desc/ip/avatar) + interactions(follows/fans/interaction 获赞与收藏) + posted 作品数；**认证类型/商品橱窗 Web 端不暴露**（APP 字段），如实标注。
6. **限流纪律**：导航间隔 ≥2s、滚动轮询 1.5s+0.9s；出现 `300013 访问频繁`（website-login/error 跳转）立即收手冷却 5-10 分钟，已捕获数据照常解析交付。
7. **验收三件套**：字段完整率、comment_id 去重、搜索卡精确值 vs SSR 展示值交叉核对（万位取整 15% 容差）。

### 坑位速查
- `printf "n%02d"` 在 bash 里把 08/09 当八进制 → 批量前缀改用 `awk` 或 `printf "n%s" 0$rank` 前先验证。
- 守护进程捕获全站响应含遥测（t2/apm-fe 每 页面 100+ 条），解析端按 URL 过滤 `/api/` 与目标路径。
- 图片原图 = URL 去掉 `!nd_dft_wlteh_webp_3` 等后缀的裸 fileid；视频无水印 = `media.stream.EF4/EF5[].masterUrl`（取最高码率）。

---

## R41 · 知乎问题回答/评论/作者三件套采集（2026-09 实战：诺奖AI问题 Top50回答+50作者主页+615根评论+317回复，~160请求零封锁）

**场景**：知乎强反爬（TLS 指纹 + `zh-zse-ck` JS 挑战 + 回答列表登录墙），"点赞前 N 回答 + 作者主页 + 每回答前 20 条评论及回复"类任务。

### 前置合规
- x-zse-96 不逆向（红线）：全程页面在场——页内同源 fetch 自带 cookie 即可，签名由知乎前端自身逻辑完成。
- 登录正路：patchright 有头窗口用户扫码一次（易盾滑块人工在环），profile（`~/.universal-scraper/zhihu_profile`）复用。

### 动线
1. **登录**：`launchPersistentContext(profile, {headless:false})`（有头是过 WAF 关键）→ goto 登录页弹窗 → 轮询 `ctx.cookies()` 里 `z_c0` 出现即登录成功，自动收窗；cookie 四件套 `_xsrf/__zse_ck/d_c0/z_c0`。
2. **回答列表**：goto 问题页**之前**挂 `page.on('response')` 监听 `/api/v4/questions/{qid}/feeds`——页面加载时自发一次（链头 cursor，错过没有第二次）；拿 `paging.next` 后用页内 `fetch(url,{credentials:'include'})` 逐页重放（5 条/页，直到 is_end）。触发不了链头就 `scrollTo(0, document.documentElement.scrollHeight)` 跳底（页面 ~30k px，渐进 scrollBy 永远到不了哨兵）。
3. **Top N 口径**：feeds 链**不含 SSR 首屏回答**（全局最高赞往往只在 `js-initialData` 的 `initialState.entities.answers` 里）——SSR 批 ∪ 链上回答合并后按 `voteup_count` 重排取前 N。注意链顺序非严格降序（推荐加权混排），本地排序兜底。
4. **评论**：根评论 `/api/v4/answers/{aid}/root_comments?limit=20&offset=0&order=normal&status=open`（未登录可用；点赞=`vote_count`（非 like_count）、IP属地=`address_text`；`comment_count=0` 的回答调此接口 403=合法空态）。**回复分两层**：v4 根评论内联 `child_comments` 只有首页（实战缺口 58%）；**全量回复走 v5 家族 `/api/v4/comment_v5/comment/{cid}/child_comment?order_by=score&limit=20&offset=`**（单数 child_comment；v4 老端点 `/comments/{id}/child_comments` 已死、恒空 200）。v5 条目用 `like_count`、**无顶层 address_text**（拿 v4 内联按 id 富化）、回复关系=`reply_comment_id`（id，线程内自查人名）、`counts.total_counts`=线程总数；v5 根评论端点 `comment_v5/answers/{aid}/root_comment?order_by=score` 是线上真实热门序（每页上限 10），可与 v4 互相校验。
5. **作者**：`/api/v4/members/{url_token}?include=follower_count,voteup_count,thanked_count,answer_count,articles_count,description,badge[].topics,locations`（未登录可用）；徽章数组（"优秀回答者"等）map title。
6. **纪律**：请求间隔 ≥2.2s+抖动；403/429 退避 30-90s；原始 JSON 全落盘 raw/ 作证据；交付前双抓 diff 验证。

### 坑位速查
- 未登录问题页 SSR 首批之后滚动**零请求**（前端根本不发）——别浪费时间诊断滚动或换指纹。
- `/question/{qid}/answers` 子页桌面版已下线（404 "荒原"页），别走。
- 存档的 cursor 不可复用（重放返回空+is_end:true），每次会话现场抓链头。
- 知乎"热门序"两次抓取首条可不同（成员与数值稳定）——双抓 diff 标红时先做成员资格复核再下结论。
- 导出：JSON 对象键会字符串化，`replies[comment_id]` 查找要 `str()`；裸 curl/curl_cffi 过不了 zh-zse-ck，别在 HTTP 侧浪费时间。

## R42 · 第三方每日存档仓库定位实体（实战反馈六，知乎诺奖问题 2026-09）
**场景**：需要定位"某天的热榜实体/问题 ID"，但站内搜索要登录、搜索引擎全被墙
（百度/必应/Google 对该实体的收录为空或过期）——离线存档是定位旧热点的最短路径。

**打法**：
1. GitHub 搜 `热榜 每日 存档`/`trending archive`——justjavac 系仓库有知乎/微博/V2EX
   等站的每日 JSON 存档（按日期目录组织）；
2. `raw.githubusercontent.com/<repo>/main/data/<日期>.json` 直取当日存档，
   从中拿问题 ID/标题/热度；
3. 拿到 ID 后回到目标站走正常深链采集（xsec_token/xsec_source 类深链参数同理）。

**边界**：存档只解决"实体定位"，不替代正主数据源——正主数据仍从目标站采
（保证时效与字段全）；存档社区的 JSON 结构无契约，解析要防御性。
**同族思路**：统计公报（CNEMC/统计局）、Wayback Machine、官方数据网盘——
"第三方存档生态"是数据型任务的常规替代源（Full 版 `SKILL.md` 的"数据型任务开局三问②"；
Lite 手册对应铁律 2 的三件套表述）。

## R43 · 拼多多百亿补贴商品评论在场采集（2026-09 实战：iPhone 17 Pro Max goods_id=1009522250164，130 评论+101 用户，全程 ~40 请求零新增风控）

**场景**：拼多多 H5（mobile.yangkeduo.com）百亿补贴商品的评论列表 + 评论者公开信息。强账号级风控（搜索面 54001 弹验证码 → 自动化导航风暴升级人脸墙），评论接口 anti_content 页面自算（红线：不逆向不伪造）。

**前置合规**：登录态真 Chrome（用户人工暖号一次：搜索→进商品页→翻评论，全程真人节奏）；CDP 附加（R7/R16 同款）；不新开导航风暴。

**动线**：
1. **找商品**（无 goods_id 时）：搜索页/首页搜"百亿补贴"→ `brand_activity_subsidy.html`（H5 唯一百亿补贴频道入口）→ 页内"手机数码馆" → Apple 品牌馆 `pincard_ask.html?top_goods_ids=`（有 iPhone/iPad/Mac 页签）→ 点商品卡进 `goods.html?goods_id=X`。注：老机型（如 iPhone 16 PM）可能已无百亿补贴在售。
2. **评论页**：`goods_comments.html?goods_id=X`。挂 `page.on('response')` 监听 **`/proxy/api/reviews/{goods_id}/list?page=N&size=10`**（body 带 anti_content=页面自算），`window.scrollTo(0,999999)` 匀速追底（4-5s/步+抖动），空 data 页即停。
3. **数据构成**：首屏 20 条（2 页）SSR 直出，**无 JSON 内嵌**（全部 script 无 review_id）——从 DOM 提取（昵称/规格/内容/图片/点赞回复数），review_id/绝对时间/评分三字段宁空勿错；API 页与展示序**位置映射**：展示序 1-20=SSR、21-130=API page3 起顺序（先验证 frag 命中率 100% 再免模糊匹配）。
4. **字段**：review_id/name/avatar/comment/time(unix)/pictures/favor_count/reply_count+reply_list/specs(颜色/容量/套餐/网络)/append_num/anonymous/is_default_review；拼多多无星级制（desc/logistics/service 三项分 1-5；stars=0+模板文案=默认好评）；列表深度平台封顶（页签"全部(659)"是同款聚合池，本列表到空页即止，全池需逐同款链接采集后按 review_id 去重）。
5. **用户主页**：H5 头像/昵称均不可点、无 profile 接口——**平台不暴露买家主页**，主页类字段（注册时长/IP属地/粉丝/会员）留空；评论者关联键用 sha256(头像URL) 截断（平台无数字 ID 可采）。
6. **脱敏**：回复楼里他人昵称是明文——建全量昵称词典（评论者+回复者+被回复者三类来源）对全部文本字段替换；交付前用已知原始昵称对产物文件做泄漏复查（verify 电池）。

**坑位速查**：
- 新号+程序化导航 = 风控升级最快路径；搜索 54001 后的每次重试都在加分——**立即停自动化，换人工**。
- 商品页"查看全部"有两个（商品参数/同款评价），span 在评价区头部；合成 el.click() 可能无效，用 scrollIntoView+真实 mouse.click 坐标。
- 商品列表页内部无滚动容器时用 window 滚动、有容器时滚容器——先探 `scrollHeight-clientHeight` 再滚。
- goods_comments 首屏 SSR 部分相对时间仅"刚刚"级可见，绝对时间只有 API 有——按来源分列标注数据来源（api/dom_ssr）。
