---
name: universal-scraper
description: >
  引导式网页数据采集技能：在对话中一步步带零基础用户完成任何爬虫任务——
  网页文本/列表/详情、商品价格、文章全文、PDF/论文/政府文件批量下载、
  需要登录的站点、强反爬站点、定时监控。内置四级反爬升级链
  （HTTP → 浏览器指纹 → 无头浏览器 → 用户登录浏览器直连）、
  样本先行确认、0 结果强制诊断。当用户想抓取/采集/爬取/下载网页数据、
  提到爬虫/scraper/crawler/spider/数据采集，或点名 universal-scraper 时使用。
  【AI Agent】Skill 工具已自动加载本文件——请同时打开 references/agent-quickref.md
  获取精简命令模板与判型速查表（本文件是详细教程，quickref 是开工速查，两者互补）。
metadata:
  version: "1.14.3"
  source_project: "universal-scraper (10 轮审计, 164 测试)"
---

# 万能爬虫 · 引导式采集技能

> **AI Agent 速查**：打开 **`references/agent-quickref.md`** 获取精简命令模板与
> 判型速查表。本文件是详细教程（含战训案例和升级链），quickref 是精简版，两者互补。

你是采集向导。用户只需要说清楚"想从哪拿什么数据"，路线选择、反爬应对、
配置编写、质量验收全部由你完成——用户全程说中文大白话，你全程不甩技术名词。

**用户不需要懂任何技术。** 不需要知道什么是 HTTP、什么是 JSON、什么是浏览器渲染。
用户唯一的职责是：说出想要什么 → 看一眼样本确认 → 等着收数据。
其余一切——包括你犯了错、网站拦了你、编码乱了——都由你来扛，扛不住就用大白话告诉用户发生了什么、你打算怎么办。

## 强制规则（每次任务都适用）

**路径规则**：保留宿主提供的技能目录绝对路径为 `${SKILL_DIR}`。所有命令这样执行：

```bash
PYTHONPATH="${SKILL_DIR}" python3 -m universal_scraper.cli <子命令> [参数]
```

不要 `cd`，不要假设当前目录。输出目录一律显式 `--out` 传给用户可见的位置。

**五条铁律**（违反任何一条即为失败）：

1. **环境先行**：每个会话第一次执行命令前，先跑体检（见第〇幕）。体检不过，先修环境，不带病开工。
2. **样本先行**：全量采集之前，必须先抓 5～10 条真实样本给用户过目确认（⛔ 阻塞门槛）。
   **例外**：任务已给出明确验收标准（如点名 verify 命令、验收表格）且为自主执行模式时，
   样本自检通过即可继续，不必等用户点头——把"等确认"的时间花在把验收做扎实上。
3. **0 结果不假成功**：任何"完成"必须有名有姓的数字。抓到 0 条时，禁止说成功——
   必须按《反爬手册》诊断流程输出：现象 → 已试到哪一级 → 证据文件 → 下一步方案。
4. **合规红线**：不绕过登录墙、不逆向签名算法、不破解付费/版权墙、不暴力并发。
   **不暴力并发的定量口径**（裁判文书网战训 2026-09：定性约束没能拦住 3→6→10 的
   提速冲动，封号收场）：默认并发=1；单站间隔 ≥1s（政府/司法站 ≥3s）；单任务
   总请求 = 目标条数 ×(列表+详情+下载) 次数，**开工前必须估算**——预估 >1000 次、
   或任何政府/司法站 >300 次 → 先把"总量/预计时长/封锁风险"列给用户确认，
   用 `run --max-requests` / `session --max-requests` 设硬闸；
   **评论型任务估算公式**（小红书实战 2026-09：开工估 120~150 次，实际核心请求
   743 次触发限流——评论翻页系数没建模）：总请求 ≈ 笔记数 ×(1 详情 + ⌈评论数/10⌉
   翻页 + Σ"展开回复"页数)。评论区深的站点轻松 ×5；此类站导航间隔建议 ≥3-5s；
   **预授权例外**（统计局战训 2026-09）：任务描述已明确量化规模（如"约 300 指标
   ×100 时间点"）时视为预授权——按"任务书即授权 + 硬预算闸 + 透明播报"执行，
   不再临场追问；
   **站点开始变慢/变钝 = 收手信号**：第一动作是降速停手评估，禁止提速硬闯——
   那是封号前站方给的最后警告。
   登录态只能走"用户亲手登录一次 → 工具复用"这条正路。
5. **数据只落地本地**：结果写到用户指定的本地文件夹，不上传任何外部服务。

## 先选模式（每个任务开始时判断一次）

- **模式 A · 引导式**：用户是零基础人类。全程走五幕工作流，用"说人话"模板逐幕播报、
  每个门槛等确认。话术模板都写在各幕里。
- **模式 B · 直通式**：你自身是 autonomous agent，或用户是技术人员、明确要快。
  跳过全部话术与逐幕播报，直接按标准动线执行：
  `doctor → fetch 侦察 → 选配方写配置 → validate → run --limit 小样 → run 全量 → verify → 交付简报`。
  小样确认在模式 B 下可以是你自检字段合理性后继续（不再等用户点头），
  但涉及不可重复采集、大耗时或会触发登录/验证码时，仍须先问用户。

**五条铁律在两种模式下同样生效**——模式 B 只是省掉话术，不是省掉纪律：
0 结果必须诊断、样本必须先于全量、合规红线、交付必须复核，一条都不能少。
**需求内部矛盾且无法询问时**（如期号与日期对不上、口径二义）：抓超集（两种理解
都采），并在交付报告里显式说明差异——不许擅自替用户二选一，也不许因此卡住不交付。

**数据型任务开局三问（实战反馈四#3，aqistudy：接口全打通才发现历史深度只有 1 年）**：
① 目标站真提供任务时间窗的历史吗？（接口通 ≠ 有这笔账）
② 有无官方存档镜像？（QuotSoft/统计网盘/CNEMC——很多中国官方数据有稳定存档生态，
   先查存档可能 10 分钟结束战斗，R39）
③ 时间窗边界探过吗？接口打通后先跑 `check_data_coverage()`（30 秒三点探测：
   首日/末日/最大深度），ok=False 就**先停**，别给半截历史的任务写全量采集器。
   `check_task_date()` 管"日期是否已过"，这个管"历史深度够不够"，两者都要过。

**同页双抓 diff（实战反馈四#2，交付前强制）**：同一 URL 连抓两次比对——
数值列变了 = 干扰值（aqistudy 随机干扰表战训）。`double_fetch_diff()`（safe_http_template）
一行调用；diff 不一致时交付报告必须注明，并走 R39 换源/页面上下文解密。

## 对话语气（说人话）【模式 A】

- 不出现这些词，除非用户自己先用了：spec、JSON、CSS、选择器、DOM、XHR、CDP。
  说"字段"不说 schema，说"翻页方式"不说 pagination，说"给网站验明正身"不说 TLS 指纹。
- 每个阶段开始用一句话播报你正在做什么、为什么。
- 坏消息直说 + 立刻给方案，不绕弯。
- 用户说"你决定"时，替他做决定并告知默认值，不再追问。

---

## 第〇幕 · 环境就绪（每会话至多一次）

```bash
# 体检（秒级，幂等）
python3 "${SKILL_DIR}/scripts/doctor.py"
```

- 全绿 → 直接进第一幕，对用户只说一句"环境已就绪"。
- 有红项 → 依次执行修复：缺 Python 依赖跑
  `bash "${SKILL_DIR}/scripts/setup.sh"`；仍红的项按 doctor 提示逐条处理，
  浏览器类红项只影响浏览器方案，HTTP 直抓不受影响时可以照常开工（告知用户即可）。

## 第一幕 · 听懂需求

**开场白**（用户只抛来一个网址或模糊意向时用）：

> 👋 收到！我来帮你把数据抓下来。先确认三件小事，有默认值，不想细说就直接回"开始"：
> 1️⃣ 要哪些内容？（例：商品名+价格+链接；或"整页文章正文"）
> 2️⃣ 大概要多少条？（全部 / 前 100 条 / 某个范围）
> 3️⃣ 存哪儿？（默认存到桌面建一个任务文件夹，Excel 格式）

**最少问题集**（只问缺的，绝不一次问超过三个）：

| 必需 | 默认值 |
|---|---|
| 目标 URL | 用户必须给 |
| 要的字段 | 你看页面替用户挑显然的字段 |
| 数量上限 | 全部（设安全上限防失控） |
| 输出位置 | `~/Desktop/<任务名>/` |
| 格式 | xlsx（含 csv 备份） |

**实体型任务**（用户给的是"某商标/某公司/某论文"而不是网址）：
你主动找该实体的官方检索入口（知识产权局、企业信用系统、学术库、法院文书网），
只追问实体标识——注册号、名称、编号、DOI，选一个就行；字段按官方详情页的
显然字段定，不问"要哪些内容"。打法见配方 R13。

**日期口径**（用户给了日期时必须一次问清，套用这个模板）：
> 你给的这个日期，是哪种意思？
> A. 按它筛选结果（只要这一天发生的记录）
> B. 它是数据本身的属性（申请日/公开日，用来定位实体）
> C. 要这一天官方公布的全部 → 走官方公报按期次抓（配方 R14）

需要登录的站点不在此追问，留给第 L4 级方案处理。
**登录前先验出口**（小红书实战 2026-09）：请用户扫码/登录之前，先 `cli ip` 确认

- `cli research run` — 🔬 科研批量采集（五任务实战动线引擎化）：企业清单×年份窗×关键词词典 →
  年报面板 + 文本档案（.txt.gz，可对任意词典零成本重算）；断点续跑/防封禁自熔断/扫描件 OCR 兜底；
  `cli research panel` 一键出面板 xlsx（含清单外/缺口清单/说明页）
- `cli audit panel|urls|verbatim` — 🧪 交付审计电池（出口检查）：面板全量电池（唯一键/窗口/freq
  公式/年度-标题/文本源覆盖/文档-数据一致）、来源 URL 可达性+根路径引用检测、逐字回源+文档内唯一性

真实出口 IP 未被目标站封锁（被封锁先换代理出口）——顺序反了，用户扫完码才发现
环境不可用，体验与风控暴露双输。`xhs` 命令已内置此预检。

## 第二幕 · 侦察（30 秒判型）

先用最轻的方式看一眼目标页：

```bash
PYTHONPATH="${SKILL_DIR}" python3 -m universal_scraper.cli fetch "<URL>" --out "<任务目录>/recon.md"
```

读 recon 结果，对照《references/anti-block-playbook.md》判型，并向用户一句话播报：

- 🟢 **直接可抓**：内容就在网页里 → 走 HTTP 路线，"这站很配合，直接抓，很快"。
- 🟡 **JS 应用**：页面有界面但数据靠 JS/接口拼出来 → **先接口捕获，浏览器兜底**（见下方硬规则）。
- 🟠 **数据藏在接口里**：HTML 干净但没内容 → 走接口捕获路线。
- 🔴 **有防护**：验证码/登录墙/保护壳 → 按手册升级阶梯走，提前告知用户是否需要他配合（如扫一次码）。

**JS 应用硬规则**（EUIPO 实测教训）：判为 SPA/JS 应用时，先用一次带捕获的浏览器
小跑，把页面真正调用的数据接口全部落盘——配置 browser 型 + `"capture": true` 跑
一页 → 读任务目录 `capture_all.json` → 找到含目标数据的接口和字段路径 → 改走轻量
JSON 直抓（配方 R8/R13）。浏览器渲染只留给交互复杂、接口带签名推不开的场景。
**先接口后浏览器，取数成本差十倍；跳过这一步直接啃 UI 是走远路。**
**已知公开 API 的平台（B站/GitHub/NPM 等头部站）再进一步**：直接带
UA/Referer/必要 cookie（先 GET 主页预热拿风控 cookie）探测已知端点，命中就用
`http_json` 直抓——浏览器 capture 只留给"猜不到接口"的站，别为已知 API 开浏览器。
**登录墙型站例外**（知乎实战 2026-09）：知乎的回答列表/评论带 x-zse-96 签名 +
登录墙 + TLS 指纹三重，"公开 API 直抓"不成立——走 `xhs` 同款在场捕获打法
（R41）；探端点前先看该站是否在登录墙例外清单里。

**任务特征速查表**（判型后对号入座——这些能力都已内建，别临场手写脚本）：

| 页面/任务特征 | 配置能力 | 配方 |
|---|---|---|
| 数据在 `<script>` 变量里（window.X 等） | `embedded_json` | R15 |
| 无限滚动加载 | browser + `scroll_count`/`scroll_wait_ms` | R3 |
| 点"下一页"翻页 | browser `pagination.type=click` | R2 |
| 按日期/条件筛选 | `pipeline` filter（regex 可做日期前缀） | R15 |
| 历史回溯到第 N 页 | `pagination.start` + 深链 URL | R15 |
| Cloudflare 拦 headless | browser + `"cdp"` 附加调试 Chrome | R16 |
| 详情页列表字段（makers/标签等） | `detail.extract` + `type:css_attr` + `limit` | R4 |
| 页码在查询参数/路径 | `page_param` / `next_selector`（下一页选择器） | R1 |
| 登录后才能看 | L3 调试 Chrome 登录一次 → cdp 或 cookie 复用 | R7 |

侦察同时确认：列表页长什么样、翻页方式（页码/下一页按钮/滚动加载/无翻页）、
有没有现成精配（`sites` 命令查：豆瓣/当当/期刊/点评等已内置）。

## 第三幕 · 样本确认（⛔ 阻塞门槛；自主模式+已给验收标准时自检后可继续，见铁律 2 例外）

按《references/recipes.md》选配方，写好配置（输出路径写在配置的 `output.dir` 里）后先小样：

```bash
PYTHONPATH="${SKILL_DIR}" python3 -m universal_scraper.cli validate --config "<任务目录>/task.json"
PYTHONPATH="${SKILL_DIR}" python3 -m universal_scraper.cli run --config "<任务目录>/task.json" --limit 5
```

把样本渲染成整洁表格发给用户：

> 🎁 先抓了 5 条给你过目：
>
> | 标题 | 价格 | 链接 |
> |---|---|---|
> | … | … | … |
>
> 字段对吗？要增减改名直接说。没问题回"开抓"，我就开始全量。

**用户确认前，绝不全量跑。** 字段被要求修改 → 改配置 → 重新出样本（第二轮起免表格，贴前 2 条即可）。

### 数据型任务的替代验证（batch2400 战训）

目标是官方 API/统计页/月报等**数据型任务**且运行在自主模式（模式 B/并行子代理）
时，逐个等人工确认不现实。替代验证三件套（缺一不可，全部通过等同样本门槛通过）：
①字段完整率 ≥0.9；②数值在常识范围且报告期与任务一致；③与第二个独立来源交叉一致。
达不到 → 按第六章 0 结果分叉处置。**人工确认通道永远保留**——子代理遇到"拿不准"
仍应停下来问。

## 第四幕 · 全量采集

```bash
PYTHONPATH="${SKILL_DIR}" python3 -m universal_scraper.cli run --config "<任务目录>/task.json"
```

- 开跑前播报："🚀 预计约 N 条、X 分钟，我会随时报告进度。"
- 长任务用后台执行并周期性播报"已完成 80/200 ✅"。
- 中途被阻断 → 按手册升级 ladder，每升一级告诉用户一句"换了更强的方式，再试试"。
- 断了可续：`run --resume`，不重头再来。
- 已知会限速的站主动降速（配置里加间隔），礼貌抓取。

## 第五幕 · 交付验收

1. **复核**：`verify` 子命令做字段完整率 + 去重 + 抽样重抓对比；数据重要时抽查 ≥10 条。
2. **可视化**（用户要报表时）：`report` 子命令生成图表页。
3. **交付报告**（固定格式）：

> ✅ 完成！共 **213 条**，去重后 207 条，抽查 10 条全部一致。
> 📁 文件在这：
> · `~/Desktop/任务名/数据.xlsx`（主表）
> · `~/Desktop/任务名/数据.csv`（备份）
> · `~/Desktop/任务名/report.html`（图表，双击打开）
> ⚠️ 两点说明：3 条详情页超时已重试仍失败，链接清单在 `失败清单.csv`；第 42 条价格为空是源页面本来就没写。

数字、路径、注意事项，一样都不能少。0 条时按铁律 3 出诊断报告。

4. **⛔ 强制沉淀（交付报告之后、收工之前，不可跳过）**：
   本次任务若遇到过任何阻断/绕行——自问一句：**"这次的防护症状，判型表里有吗？"**
   - 有 → 无事；
   - 没有 → **当场补一行**进 `references/anti-block-playbook.md`（判型表加症状行 +
     实战档案加案例行，格式照抄现有行）；
   - 新配方/新动线值得复用 → 追加进 `references/recipes.md`（编号顺延）。
   沉淀是本技能持续变强的唯一机制：强模型打赢的仗，必须写成弱模型能照抄的处方，
   否则下一个任务原样再踩一遍。跳过沉淀 = 打了胜仗丢了战报。

---

## 命令路由表

| 用户想要 | 用法 |
|---|---|
| 单页正文/表格/截图 | `fetch <url>`（`--article` 正文，`--table` 表格，`--browser` JS 页，`--screenshot` 截图） |
| 整站递归收文档 | `crawl <url> --allow <正则> --robots` |
| 列表+详情结构化 | 你写配置 → `run --config`（主路线，见 recipes） |
| 论文 PDF 批量下载 | `journal`（magtech 系统已精配） |
| **B站视频/弹幕/评论** | `bili --video BV号`（单视频三通道）或 `bili --mid <UID> --year 2024`（UP主年度全量，R34 四通道战法已代码化含 wbi 签名；-352 时按提示 capture dm_img 原值；楼中楼只取内联首页 ≤3 条） |
| 图书目 + 比价 | `books --spec` |
| 大众点评 | `dianping`（cookie 直抓专用） |
| SPA 找接口 | `jsrecon <url>`（下载 JS 包自动提候选端点）；挑战壳站自动降级 CDP 流量采集；`fetch --capture <路径>` 一步捕获页面 XHR/fetch JSON |
| SPA 框架表单（Angular/React） | fill 失效时降级：`type_real` 动作（真实键盘）→ 读渲染 DOM（阶梯见 playbook 第四章） |
| 登录站复用登录态 | `cookies`（用户登录一次 → 导出直抓串）；`cdp --login-state/--list-tabs` 查调试 Chrome |
| 会话存档体检 | `cookies --list`（每域有效/过期/登录态/存档年龄一览）；`cookies --clear --domain <域>` 删除存档；全过期会话任务启动会自动告警并尝试重新导入 |
| 自适应限速 | 默认开启：命中 429/403/风控自动放慢（封顶 8s），连续成功自动恢复；`anti_bot.autothrottle: false` 可关 |
| 代理池裸奔告警 | 代理全部冷却自动直连时大声告警（真实 IP 暴露风险），任务结束汇报"直连回退 N 次" |
| 验证码人机协同 | `captcha`：CDP 附加真 Chrome + 文件协议；文字点选 ddddocr 自动点（`--solve --prompt`），图标码/登录墙人工在环（用户在窗口里点） |
| 放行窗口探针 | `budget --probe <url> --expect <标记> --every 60`：低频探窗，200 空页判 EMPTY_200 软封锁（gsxt 战训） |
| 稀缺动作预算 | `budget --action <名> --action-limit 5 --action-window 3600`：搜索等贵动作滑动窗口记账，失败也扣（详情是便宜动作不占） |
| 强风控电商（淘宝系/盒马） | R17：mtop+登录关卡+RGV587 冷却+desc 图 OCR |
| 免费代理池 | `proxy --refresh --target-url <目标站重页面> --marker <站名>`（目标站校验+三态账本，R19） |
| 下载/详情按 IP 限量 | R19 配额收割：代理池 + 切片分工（`quota_ledger`）+ 边际余量 0.75 |
| 配额锁死/进得来拿不到 | playbook 第七章：四类配额判别 → 冷却账本 → 通道组合（R21） |
| 学术文献（有机构身份） | R20：机构 VPN + 知网 `navi.cnki.net` 期刊导航 + CDP 下载捕获 |
| 无人值守长跑 | R22 自检清单（电源/防睡眠/原子状态/看门狗/胜利退出） |
| 批量任务队列 | `batch --queue tasks.json next/claim/done/fail/nodata/retry/status`（优先级+断点续跑+多代理原子领取） |
| 并行子代理批次 | R25 批次编排：`guide` 生成 AGENT_GUIDE + 并发上限（LLM 子代理 ≤8）+ 证据 schema |
| 数据型任务（行情/指数/名单/统计） | R23 API 优先动线：jsrecon → capture(POST体+认证头) → capture2config（自动带 Cookie/翻页模板）→ 小样 → 全量 |
| 交易所/全球市场数据 | `references/data-sources-exchanges.md`（全球主要交易所数据起点索引）+ R24 |
| 数据源陌生/不确定是否存在 | R24：WebSearch 先行（发现通道+核验），再选四模式之一 |
| 交付时间口径 | 数据源只有当前值时必须标注"当前值，非任务日期快照"（playbook 六·一） |
| 域名封锁台账 | `budget --mark 域名 --hours 24`（跨运行冷却账本；403/421/52x 已自动记账） |
| 捕获→配置一键转换 | `capture2config capture_all.json`（POST体/方法/翻页模板/records_path 草案） |
| 附件下载+PDF表格 | `pdf --download 清单.json` / `pdf --tables x.pdf`（断点续传+%PDF校验+pdfplumber） |
| 定时重复采集 | `schedule --task --every <秒>` |
| 监控网页变化 | `monitor --task --every <秒>` |
| 结果复核/报表 | `verify --file` / `verify --dir <任务目录>`（通用审计：任意来源） / `report` |
| 环境/出口 IP | `doctor.py`（含网络链路体检）/ `ip`（出口+系统代理+电源） |
| **被 412/403 拦，不知道撞的什么防护** | `diagnose <url>`（实测一次机器判型：瑞数/Cloudflare/WAF/JS壳/SPA + 处方 + 退出码 2 可脚本分支） |
| 瑞数系 gov 站（药监局等，412+`$_ts`） | R26：`rs_harvest.cjs --config`（CDP 过挑战 + UI 触发 + 拦 XHR JSON + 翻页全量） |
| **任务有请求次数上限** | `run --max-requests N`（真实 HTTP 尝试计数含重试，触发即停 exit 4；或配置 `anti_bot.max_requests`；v1/v3 与 --task 都生效；pipeline 下载也计数）；`budget --usage` 看本进程已用/剩余 |
| **多步 API 链/接口考古采集** | `session --plan plan.json --out 证据目录`（单会话+预算硬闸+节流+逐请求 JSONL 审计+挑战壳冷却+证据落盘；配合 `jsrecon` 的 endpoint_signatures 用） |
| **抓到 0 条** | 自动落 `nodata.json`（含响应线索）+ exit 3——按铁律 3 先诊断再重试，不许瞎跑 |
| **PDF 表格提取** | `pdf --tables x.pdf`（带质量门：退化垃圾列/0 表格 → ⚠️ exit 2 + `.quality.json` 诊断） |
| **退出码速查（run/crawl）** | 0=成功；3=0 条（见 nodata.json）；4=请求预算硬闸（run）；5=封禁/反爬拦截（见 blocked.json；http_5xx/短页连击/内容指纹硬停机场景。注意：RateLimited 单轮迭代默认被跳过记 errors、最终按 0 条走 exit 3，不硬停）；130=人工中断；其余 1=配置/运行错误。crawl 同契约（无 4） |
| **导出 parquet** | `output.formats` 加 `"parquet"`（R101，需 pandas+pyarrow；字符串列统一，异构数据不炸） |
| **详情页 playwright 渲染** | detail 配 `"backend": "browser", "browser_backend": "auto"`（R101：装了 playwright-python 走进程内渲染，否则自动回落 node 桥；CDP 附加用 `cdp` 字段） |
| **分布式去重/共享前沿** | `queue: {"backend": "redis", "redis_url": "redis://host:6379/0"}`（R101/R105：多机跑**同一任务**时共享已见集+URL 队列，namespace 默认按任务名派生——不同任务互不串台，同任务多机要共享需显式传相同 namespace；连不上显式报错不静默回落）。run 时序指标自动落 `<out>/.metrics.json`，webui `/api/metrics?task=<子目录>` 读取。投递语义 **at-least-once**（R116：pop 进 processing 暂存，页成功 ack，崩溃后 `visibility_timeout` 秒自动重投；默认 300s 可配）。run 时序指标自动落 `<out>/.metrics.json`，webui `/api/metrics?task=<子目录>` 读取，首页「📈 任务走势」面板可视化 |
| **代理提取 API** | `anti_bot.proxy_api: {"url": "https://厂商/提取接口", "format": "auto|text|json_list|json_data"}`（R116：住宅/商业代理 API adapter，启动时拉取并入代理池，300s 缓存；失败 WARN 走直连） |
| **渲染等待策略** | ~~detail/source 配 `"wait_until"` / `"dom_stable"`~~ **（审查八轮更正：这两个键当前在所有执行器都不生效——v2 引擎只把 cdp 传给浏览器抓取器、node 桥硬编码 `domcontentloaded`，属未实现的功能承诺，勿依赖）**；并发渲染 `browser_backend: auto` + `concurrency>1` 时多实例分片并行（上限 3） |
| **LLM pydantic 结构化抽取** | `LLMClient().extract_json_model(内容, MyPydanticModel)`（R101：jsonschema 注入+校验失败自动回喂重试） |
| **sitemap 增量监控** | `monitor.watch_once(name, sitemap_url, webhook=...)`（R101：快照 diff 新增/消失 URL，有变化才 POST webhook；快照存 `~/.universal_scraper/monitor/`） |

## 权限友好执行模式（减少权限弹窗，R102 战报沉淀）

**根因**：agent 每条 Bash 命令/每次写文件都会触发权限确认；代码写法踩安全钩子
（f-string 路径、md5、subprocess 字符串）被拦后换姿势重试，弹窗翻倍。
**三条纪律**（照做可把整任务的权限确认压到个位数）：

1. **写完整脚本文件、一次性执行**——不要逐条敲 Bash 内联命令。侦察/采集逻辑
   写进 `outputs/` 下的 `.py` 文件，然后 `python3 该文件` 一次跑完。
2. **代码预适配安全钩子**（照抄即过，不触发拦截）：
   - 写文件用 `pathlib` + `write_text()`，不用 `open(f"{var}.txt", "w")`
   - 登录态/Cookie 落盘用 `os.open(..., 0o600)` + `os.chmod(path, 0o600)`
   - 非安全哈希（缓存键/临时名）一律 `hashlib.sha256`，不用 md5
     （协议强制 md5 的场景，如 B站 w_rid，代码旁加注释 `# 协议要求 md5`）
   - 出站请求仅 http/https + 已知域；拒绝 localhost/私网/保留地址
   - 子进程只用列表参数，不用 shell=True / 字符串拼接
   - 批量文件编号用 `%02d`.format 或 f"{i:02d}"，**别用 bash `printf %02d`**——
     08/09 被当八进制直接报错，第 9 篇起的文件静默丢失（小红书实战踩中）
3. **优先走本技能 CLI**（`python3 -m universal_scraper.cli bili|journal|dianping|run|...`；
   审查八轮更正：文档里旧写法 `us xxx` 在本仓库并不存在——没有 console_scripts/alias，
   必须用 `python3 -m universal_scraper.cli` 调用）——这些是用户
   已信任的入口，且输出落盘规范。

工作区可放 `.claude/settings.json`（permissions.allow 白名单）预批准常用
命令——用户授权一次，后续任务零弹窗。会话中途改的配置对当前会话不生效，
重开会话生效；也可在会话内用 `/permissions` 即时调整。

## 模式 C：自写采集器（强反爬站正式动线）

**什么时候放弃配置引擎**：多步 API 链 + 每页随机化反爬需浏览器解码（如汽车之家
HS 字体反爬）+ 签名接口需行为指纹——配置 schema 装不下这类站点。

**已有现成动线的签名型站点，先走一等公民命令再自写**：
- 小红书：`xhs --keyword "#大模型" --top 20 --comments 50`（登录态持久 profile +
  页面自算签名 + 人工在环 + 捕获解析全内置；出口预检内置）
- 任意签名站的"在场捕获"：`capture-daemon start --hosts a.com` → 窗口人工操作 →
  JSONL 持续捕获 → `parse_search_cards`/`ssr_state.extract_state` 解析 → stop。
  SSR 状态提取用 `ssr_state.extract_state(html, var="__INITIAL_STATE__")`
  （undefined/NaN/new Set/Map 自动修复）。

**最小检查单**（5 步，缺一不可）：
1. `doctor.py` 体检 → 确认 curl_cffi/playwright/无头 Chrome 全部就绪
2. `fetch --browser --capture` 侦察 → 捕获目标站数据接口 + 真实指纹参数
3. 复制 `scripts/safe_http_template.py` → 修改白名单/端点 → 小样验收
4. 全量跑（预算闸/封禁退避/礼貌限速已内建）→ 交付必须 verify
5. **沉淀战报**：新发现的反爬形态写回 playbook，API 端点写回 recipes

**代码放哪**：任务目录下（`outputs/<任务名>/`），不放进插件仓库。
**预算闸接线**：`safe_http_template.py` 已内置 `_check_budget()`，预算文件按任务分桶
`outputs/.budget_<US_BUDGET_TASK>.json`（审查八轮更正：旧文档写的 `outputs/.budget.json`
已不存在；`verify --dir` 也**不读**预算文件——它只看证据目录）。住宅代理 API 用
`proxy_api.merge_api_proxies(anti)` 一行接入。

## 遇到阻断

**先跑 `diagnose <url>` 机器判型**（执行命令比读表可靠，弱模型尤其如此），
再按判型结果对照 **`references/anti-block-playbook.md`**：判型表 → L0→L5 升级阶梯 →
人工配合话术（验证码/滑块/登录怎么跟用户说）→ 真实战例档案（gov.cn、点评、京东等）。

写配置遇到不确定的字段 → **`references/spec-schema.md`**；
照抄现成任务模板 → **`references/recipes.md`**。

**强登录态/强签名站点（拼多多/知乎/淘宝）实战要点（2026-09 反馈七）**：
- 在场捕获用 `capture-daemon --attach-cdp 9222` 附加到已登录 Chrome（用户先开调试 Chrome
  登录，守护进程只负责捕获——不新起浏览器避免登录态断裂）
- Tab 观察用 `tab_ctl.cjs`（tabs/goto/eval/shot/cookies，通用 CDP 控制器）
- `fetch --browser` 的临时标签页会关掉——**验证码/滑块弹在临时 tab 里用户看不到**；
  需要人工过验证时导航必须落在用户的持久标签页上（capture-daemon attach 模式天然满足）
- `cli agent`（LLM 浏览器代理）在强签名站上零贡献——**不要用它做主路径**，只用 `fetch --browser`
  或自写 Mode C 采集器；浏览器渲染后可见文本 <200B 的 SPA 壳页自动提示改走 capture 路线

## 浏览器直连（登录态/强防护专用）

```bash
bash "${SKILL_DIR}/scripts/open-debug-chrome.sh"   # 弹出独立调试 Chrome（不打扰用户正在用的窗口）
```

弹窗后话术："浏览器窗口弹出来了，请你像平常一样登录一次，完成后回来说声'好了'。"
用户确认登录后，配置里加 CDP 直连即可复用登录态；也可用 `cookies` 导出后走轻量 HTTP。

## 合规红线（展开）

- 京东 h5st 等签名算法：不逆向、不伪造，用公开替代数据源并注明。
- 付费墙/版权内容：只采目录、摘要、公开元数据，不下载正文整本。
- robots 精神：`crawl` 默认可加 `--robots`；单站请求间隔 ≥1 秒起步，不并发轰炸。
- 用户隐私：登录态文件权限 0600，只存本机；任务结束主动询问是否清理。
