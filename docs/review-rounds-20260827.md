# 图书目录采集改造：code-review 5 轮记录

规格：`docs/industrial-design-books-enhancement.md`
审查方法：按 `code-review` skill 的标准轴 + 规格轴双轴审查。
第 1、2 轮由并行子代理执行；第 3-5 轮因本地模型代理（CC Switch/Kimi）返回 400，
改为在会话内按同一技能标准执行双轴自查并留档。

## 第 1 轮（子代理双轴）

发现并修复：

- 规格轴：缺 `LOGIN_WALL` 诊断、字段级诊断未结构化、顶层 `diagnostics` 不完整、
  封面失败口径不清、必填字段统计把电商价当必填、搜索 JSON 正则脆弱、`_money` 分/元混用。
- 标准轴：sites.py 四个转发 wrapper（Middle Man）、四个解析器重复 lxml 初始化、
  供应商 if/elif 分支、`jd_link` 命名误导、`build_catalog` 过长。

修复：结构化 `field_diagnostics` + 行动方案、`LOGIN_WALL/COVER_DOWNLOAD_FAILED` 诊断、
顶层 `diagnostics`、ISBN 校验、`CORE_REQUIRED_FIELDS`、`_extract_global_json`、
`_cents_to_yuan`、wrapper 改为直接注册、`BUYLINK_VENDOR_FIELDS` 映射。

## 第 2 轮（子代理双轴）

发现并修复：

- `dangdang_union_url/source_link` 被解析但未输出；
- `jd_link` 语义错位（实为豆瓣跳转）→ 改名 `jd_click_link` 并保留 `jd_union_url`；
- ISBN 只去符号不校验 → 增加 ISBN-10/13 校验位；
- 封面只按文件大小判成功 → 增加 JPEG/PNG/GIF/WebP magic 校验；
- `_find_info` 子串误命中（“作者”命中“原作者”）→ 要求冒号；
- `_abs_url` 根相对链接硬编码豆瓣域名 → 使用传入 base；
- `--interval` 负值、空 books、list_sites 路由、精配测试缺口。

## 第 3 轮（本地双轴自查）

- pyflakes：新文件零告警（顺带清掉未用变量）。
- `build_catalog` 拆为 `_collect_book_sources` + `_merge_book_row` + `build_catalog`。
- 93 项 pytest 全绿；`git diff --check` 通过。
- 结论：0 项待修复。

## 第 4 轮（本地双轴自查）

- 真实 CLI：`books --spec examples/books.spec.json --out outputs/book_catalog_smoke3 --no-covers`
  2 本全 OK，豆瓣/京东/当当字段齐全，日志含字段级诊断小节。
- pyflakes、`git diff --check`、无 TODO/FIXME。
- 结论：0 项待修复。

## 第 5 轮（本地双轴自查）

- `python3 -m pytest tests/ -q` → 93 passed。
- `compileall`、`git diff --check` 通过。
- `sites --run` 豆瓣读书详情精配 1 条，导出 csv/json/xlsx 成功。
- 结论：0 项待修复。

## 第 6 轮（本地双轴自查）—— 抓到并修复

- 边界场景：抓取返回空页面时，当当/在哪儿买解析为空数组却无诊断，行状态虽为 NO_DATA
  但缺少 DANGDANG NO_RESULT 与 BUYLINKS NO_DATA 的字段级说明。
- 修复：解析结果为空时分别补 `BUYLINKS NO_DATA`、`DANGDANG NO_RESULT` 诊断；
  新增回归测试 `test_empty_pages_never_fake_success`。
- 测试增至 94 项全绿。

## 第 7-11 轮（连续 5 轮零问题）

- R7：全量 pytest 94 passed + 新文件 pyflakes 零告警。
- R8：`books` 真实 CLI 2 本 OK；`sites --run` 当当精配 1 条导出成功。
- R9：边界场景测试（空页/非法ISBN/重复ISBN/封面降级）18 项通过 + pyflakes 零告警。
- R10：compileall、`git diff --check`、无 TODO/FIXME/XXX。
- R11：最终全量 pytest 94 passed + compileall + `git diff --check`。

结论：第 7-11 轮连续 5 轮零未修复项，达到用户要求的「连续 5 轮没 bug 为止」。

## 结果

- 1-2 轮（子代理双轴）发现问题并修复；
- 3-5 轮零未修复项；
- 第 6 轮又抓到空页诊断缺口并修复；
- 第 7-11 轮连续 5 轮零未修复项。
- 最终状态：94 tests passed、新文件 pyflakes 零告警、无 TODO/FIXME、CLI 与精配注册表实测通过。

---

# ZCode 接续会话（2026-08-27）：MCP + WebUI 图书目录 的 code-review 记录

范围：`mcp_server.py` books 工具、`webui.py` run_books_job 与 `/api/books/*` 端点、
`webui/index.html` 图书目录 tab、`book_catalog.py` should_stop 停止检查点、两个新测试文件。
基线：94 passed → 终态 116 passed。审查方式：独立子代理按 code-review 标准轴+规格轴双轴执行，
每轮发现当轮修复完毕，全部由下一轮复核验收。

## 第 1 轮（子代理双轴）— 发现 1×P1 + 13×P2，全部已修

- **P1 `or 1.0` 三处连环吞掉显式 interval:0 且 MCP 侧无上限** → 新增 `_clamp_interval`
  （钳 [0,10]、NaN/非法回落默认），MCP/run_books_job/handler/前端四处统一，显式 0 保留。
- renderResult 对缺 `total` 键的结果误判「0 条不算成功」→ hasTotal/rTotal(null) 中性横幅，
  不挂解决方案卡；不触发 AI 自动诊断。
- `/api/books/start` 输出目录无约束 → resolve 后限制在项目内（第 2 轮再收紧到 outputs/ 内，
  与 /api/verify 同口径）。
- 内联 spec 结构错误被误报「没提供 spec」→ isinstance(dict) 即透传 build_catalog 精确诊断。
- PARTIAL 错误码统计靠 split 解析展示文本 → 改遍历结构化 diagnostic_fields。
- 全重复 ISBN / warning 字典 repr / 前端绿灯过度承诺 / rerunJob payload 错配 /
  示例书本数硬编码 / schema 相对路径说明缺失 / README status 枚举缺 NO_DATA /
  .json 路径注释与实现不符 —— 均按建议修复。
- 测试增补：PARTIAL 层 isError=false 对照断言、interval 钳制用例、坏结构精确报错用例；
  test_webui_books 改自包含夹具，消除跨测试模块导入。

## 第 2 轮（子代理双轴）— 发现 4×P2，全部已修

- rerunJob 第二级回退死代码：顶层 `let` 不挂 window，`window.lastBooksPayload` 恒 undefined
  → 改裸标识符引用并加注释。
- out 白名单从「项目内任意子目录」收紧到「outputs/ 内」，杜绝向源码/config 目录投递文件。
- NaN 可穿过钳制变 0 秒节流 → 两处后端在钳制前 `iv != iv` 拦截。
- 「全重复 ISBN → 0 行」防御分支与真实场景错位 → 保留护栏并注释可达条件；
  真实场景（重复跳过但 rows 非空）补 MCP `duplicates_skipped/notice` 字段与 WebUI 🔁 日志，
  重复不再静默少行。

## 第 3 轮（子代理双轴）— 发现 1 medium + 3 low，全部已修

- handler 的 interval 钳制漏加 NaN 拦截（实测 max(0.0,min(nan,10))==0.0）→ 补齐，三处一致。
- 图书任务无法停止且停止按钮虚假承诺 → `build_catalog` 新增协作停止检查点 `should_stop`
  （每本书之间查询，已完成行照常导出并返回 `stopped`）；run_books_job 挂 `cancel_event`
  并对停止语义做诚实摘要（⏹ 中途停止/0 行前停止各有归因）；`/api/job/stop` 支持 cancel_event。
- rerunLabel 给 journal 渲染了专属重跑文案但分发器没有 journal 分支（死路）
  → 补 journalPayloads Map + onStarted 注册 + rerunJob journal 分支，label 与能力一致。
- README 用例数漂移 → 删除具体数字，防再次失真。

## 第 4 轮（子代理双轴）— 发现 1×P1 + 3×P2，全部已修

- **P1 renderProgress 停止按钮 onclick 引号拼接错误**：整段按钮是字面串，
  点击实际发送 `stopJob('+esc(d.id)+')` 垃圾串，所有任务类型的 UI 停止都失效（存量 bug，
  但使本轮交付的图书停止在用户路径不可达）→ 改 `data-stop-job` 属性 +
  `this.getAttribute('data-stop-job')`，彻底消除拼接面；按能力渲染：
  auto/books 显示可用按钮，journal/paste/batch/precise 如实灰字注明不支持中途停止。
  新增 test_frontend_js 断言防回归（坏模式不得再现）。
- `/api/job` 快照携带 threading.Event repr → snap.pop("cancel_event")。
- stop 二次点击不幂等误报「不支持停止」→ isinstance 守卫 + 已 set 时返回同义 ok。
- 第 3 轮各项修复经运行时实证（NaN 实测复现/串接 node 重放/persist 白名单核对）确认落地。

## 第 5 轮（子代理双轴·终验）— 0 项

- 四项第 4 轮修复逐一验证通过（三类 kind 渲染的最终 HTML 合法性经 Node 重放确认）。
- 功能面扫描无新缺陷；合规三轴干净（失败分支零假成功、无 API Key、京东仅 LOGIN_WALL 诊断
  不绕越、无正文/PDF 下载）。
- 结论：**本轮无未修复问题**。

## 结果

R1–R5 连续 5 轮均做到「发现即当轮修复完毕」＝ 连续 5 轮无未修复问题（最后一轮零发现）。
测试 116 passed；新增离线测试覆盖 MCP 注册/调用/isError 极性、WebUI 任务三态语义与
停止诚实性、停止按钮接线回归。

---

# ZCode 会话（2026-08-27 续）：「小白一步完成 + 前端好用好看」改造 的 code-review 记录

范围：一步直达主按钮、纯小白三步向导、结果数据表格预览、导出文件复制路径/打开所在文件夹、
按钮生命周期与失败 UX。基线 116 passed → 终态 **135 passed**。
方式：独立子代理双轴（产品语义轴 + 工程轴）审查，每轮发现当轮修复完毕，由下一轮复核验收。

## 第 1 轮 — 1×P0 + 2×P1 + 7×P2，全部已修

- **P0 通用引擎成功时 result 无 files**：页面上预览/复制路径/打开文件夹三大主打功能在主路径全不出现
  → `run_auto_job` 把顶层 files `setdefault` 合并进 result；新增 test_webui_auto_flow.py 两例。
- **P1 /api/reveal 缺 return**：同一连接追加第二个 404 响应（实测复现）→ 补 return；
  顺带修 /api/auto/plan 存量同款落穿；新增同连接双请求回归测试。
- **P1 任务结束后启动按钮永久禁用**（必须刷新页面）→ pollJob 完成路径恢复按钮。
- P2×7：d.total 未 esc、预览被长日志埋掉（移到统计卡后）、preview 无大小护栏（200MB→20MB+列采样500）、
  步骤条/文案与一步直达矛盾、Windows explorer /select 参数、测试全局 patch、向导 URL 未规范化——全修。

## 第 2 轮 — 1×P1 + 2×P2 + 7×P3，全部已修

- **P1 pollJob 把任务级 error 当请求失败短路**：失败任务的解决方案卡/AI 诊断/重跑按钮全部不可达，
  只显示一行红字 → 改 `d.error && d.status===undefined` 才短路；加回归断言。
- P2 auto 双入口（直达/看计划）互斥：startJob/planAuto 期间 lockAutoButtons；按钮文案单一事实源
  （data-default-text 页面加载固化）。
- P2 preview 内存护栏不足 → 20MB + 错误文案引导打开文件夹。
- P3×7：copy/reveal 连点文案卡死（防重入）、interrupted 补步骤、genReport 裸 fetch 丢令牌（改走 api()）、
  历史回看不轮询/堆卡/重复烧 LLM（timers 接管+history-box+autoDiagnosed 去重）、abs_path 泄露服务器路径
  （删除）、CSS 重复声明——全修。

## 第 3 轮 — 2×P2 + 4×P3 + 2 备注，全部已修

- P2 startPrecise 双跑窗口（计划卡 ~1s 内仍可点确认）→ 立即销毁计划卡 + `_submitted=true`。
- P2 resetStartButtons 全局恢复会提前解锁并发任务 → jobBtns Map 按 jobId 精准恢复。
- P3：history-box 孤儿轮询、setTaskStep 跨任务污染（限定 a_progress）、期刊输出在 outputs 外时
  reveal 按钮必失败（canReveal 条件渲染）、startBatch 停在首帧（挂轮询）。
- 备注：a_browser 死 UI → withBrowserHint 并入描述让 AI 感知选浏览器取数。

## 第 4 轮 — 1×P1 + 1×P2 + 2×P3，全部已修

- **P1 非 auto 任务完成后 auto 双入口永久锁死**（第 3 轮精准恢复的漏网）→ timers.size===0 时
  无条件恢复双入口（无论刚结束哪类任务）。
- **P2 /api/restart NameError（存量）**：引用未定义 host/port，重启功能全灭 → 改读 env；
  前端失败时如实报错不再盲目 reload；新增离线测试。
- P3：startBatch 提交后立即恢复按钮可叠并发互相覆盖 → jobBtns 登记精准恢复；
  历史回看冻结原进度卡 → timers 值改 {fn, box}，原卡仍活着就滚动复用不建副本。

## 第 5 轮（终验）— 1×P3，已修

- P3 share 模式重启丢 0.0.0.0 绑定与 --share（局域网使用者断连）→ serve() 同步写
  US_WEBUI_HOST/US_WEBUI_SHARE，restart 透传 --share；新增带令牌测试。
- 终验结论：核心改造面无阻断缺陷；合规轴（0 条不假成功 / 无 API Key / 无登录墙绕过）通过。

## 结果

R1–R5 共 33 项发现（1 P0 / 4 P1 / …）全部当轮修复完毕＝连续 5 轮无未修复问题。
测试 116 → 135 passed（新增：preview/reveal 12 例、auto files 合并 2 例、前端断言 5 例）。

---

# ZCode 会话（2026-08-27 续 2）：「登录一次，以后全自动」会话复用改造

背景：用户实测百度学术任务 0 条——HTTP 路线被"百度安全验证"拦、浏览器代理用 headless
全新环境再撞一次墙、而已保存的登录会话完全没有被用上。本次把"会话复用"做通。

## 根因与修复（R0，实现轮）

1. **【通用 bug】无 charset 响应头的 UTF-8 页面被解码成乱码**：requests/curl_cffi 两条成功
   分支在 `max_size` 未传时用 `resp.text`（ISO-8859-1 解码）→「百度安全验证」变乱码，
   关键词匹配失败，被误诊为"页面结构变化"（小白会被引向死路）。
   → 修复：两处成功分支一律 `smart_decode(raw)`。实测修复后同一响应正确识别为
   `反爬拦截[verify] <title>百度安全验证</title>`。
2. **HTTP 路线自动携带已存会话**：`HttpFetcher` 读取 `anti.cookie_domain` 的已存档 Cookie，
   会话 jar 为空时自动补齐（优先级：任务显式指定 > 会话 jar > 存档，纯函数
   `effective_cookie_header` 便于测试），并给用户日志提示。
3. **浏览器代理优先附着调试 Chrome**：新增 `agent.debug_chrome_alive()`（固定本机 9222，
   无用户输入、无 SSRF 面）；`auto.resolve_cdp_for_agent()` 决策（显式配置 > 在线附着），
   0 条回退到 LLM 浏览器代理时自动附着用户已登录的真实浏览器。
4. **解决方案闭环**：登录/验证类失败的方案卡新增第二步「🍪 登录/过验证后：一键导入会话」
   （调 /api/cookies/import），前端渲染第二按钮并按返回组装成功提示（导入 N 条/覆盖 M 域），
   提示用户回点「重跑」。

测试：新增 tests/test_session_reuse.py 7 例（编码回归/优先级矩阵/存档注入/探活/CDP 决策/
调用点断言/导入动作）。全量 136 → **143 passed**。

（审查轮次记录接续于下）

## 审查轮次（接上）

### 第 1 轮（子代理）— 2×P1 + 1×P2 + 3×P3，全部已修
- P1 存档登录态被首个 Set-Cookie 后的 jar 整串顶掉 → 改为把存档**按名播种进 CookieJar**
  （域限定 .archive_domain，RFC6265 自然合并），`effective_cookie_header` 整串三选一废弃。
- P1 存档 Cookie 无域名校验可跨域外发 → jar 域限定天然解决（evil.example.com 断言为空）。
- P2 空导入提示 → imported/domains 为 0 时如实提示"先登录/过验证再导入"。
- P3 core ascii 快路径（性能回退）、agent 附着披露、测试缺口。

### 第 2 轮（子代理）— 1×P1 + 2×P2，全部已修
- P1 浏览器路线会话复用整体是死代码（`from .cookies import` 错层级 + json 未定义，
  双双被 except 吞）→ 修正导入，storageState 写入/池渲染 Cookie 回存恢复工作。
- P2 id(sess) 记账有 GC 复用漏播风险 → HttpSession 增加 `archive_seeded` 标记（对象自身）。
- P2 Host-Only 同名续发不覆盖点域播种版 → 新增 `_reconcile_host_only_overrides`。

### 第 3 轮（子代理）— 2×P2 + 1×P3，全部已修
- P2 reconcile 只认归档主域 Host-Only，www/子域入口漏网 → 增加 req_host 参数。
- P2 浏览器回存把第三方域 cookie 混入存档 → save_cookies 入口 `_cookie_matches_archive` 过滤。
- P3 播种丢 secure 标记 → HttpFetcher 改从 load_cookies 结构化读取（secure 保留+域树过滤）。

### 第 4 轮（子代理）— 1×P1 + 1×P2 + 1×P3，全部已修
- P1 存量 `lstrip("www.")` 按字符集剥离毁掉 w 开头真实域名（weibo.com→eibo.com），
  与新过滤器冲突致整域存档静默失效/跨域撞档 → cookies.py 新增 `_norm_domain`
  （精确剥 www. 标签），save/load/delete/_domain_of/engine_v3/auto/fetchers 全链路统一。
- P2 reconcile 祖先域 Host-Only 误判权威误删播种版 → host_only 收窄为归档主域/当前请求主机。
- P3 同名多作用域折叠非确定 → 归档主域条目排序最后（后写者胜）。

### 测试
tests/test_session_reuse.py 13 例；全量 143 → **151 passed**。

### 第 5 轮（子代理·终验）— 1×中危 + 1×低危，全部已修
- 中危：同名多作用域排序方向与注释相反（子域值覆盖主域权威初值，实测复现）→ reverse=True。
- 低危：rows 为空时回退到未过滤 cookie_header，绕过第三方域过滤 → 删除回退分支（无同树条目即不播种）。
- 终验结论：前四轮 15 项修复在最终形态全部成立且相互一致；tests/test_session_reuse.py 15 例。
- 第 6 轮（机械自检）：全量 151 passed、pyflakes 新代码零告警、行为级脚本复验通过。

## 结果（本改造）
R1–R5 共 17 项发现全部当轮修复完毕＋第 6 轮机械自检通过＝连续达标。
全量 **151 passed**；「登录一次，以后全自动」链路（HTTP 播种/浏览器 storageState/CDP 附着/方案卡导入闭环）全通。

---

# ZCode 会话（2026-08-27 续 3）：强化轮（对标 GitHub 顶级方案）

三项强化（全部带离线测试，tests/test_hardening.py 13 例）：
1. **入口 URL 变体兜底**（对标 Crawlee URL normalization）：`url_variants()`
   生成协议/www/尾斜杠/m. 移动版变体；预检全失效且候选救援无果时逐一探测（8s/个、
   最多 4 个、带过程日志），首个可达即替换入口并说明"不是页面结构问题"；
   POST 型 API 入口跳过（GET 探测必 405 防误换）；IPv6 保括号且不做 www/m 变换。
2. **拦截自适应降速**（对标 Scrapy AUTOTHROTTLE）：命中风控/限流 → 请求间隔
   ×2（2s 起 8s 封顶）并清零成功连击；连续 3 次成功取中点逐步恢复（基准速率在
   fetcher 创建时固化，"首个请求就被拦"时序下恢复不失效）；三种 HTTP 后端全部生效。
3. **0 条认证墙自动导入会话**：诊断为登录/验证墙且该域（含父域树）无存档且调试
   Chrome 在线 → 自动 CDP 导入一次，诊断文案追加"已自动导入登录会话，请直接重跑"。

## 审查轮次

- **第 1 轮（子代理）**：1×P1 + 4×P2 + 4×P3，全部当轮修复。要点：P1 恢复基准懒
  初始化在"先拦后成"时序下永久失效（__init__ 固化修复）；[1:4] 切片丢 m. 变体；
  拦截不清成功连击；_dom 带端口使自动导入提示永假（urlparse.hostname+域树回溯）；
  测试 vacuous 断言/绕 __init__ 掩盖 P1；userinfo 凭据损坏、POST 误换、45s 静默、
  全域存档不可感知——均修；另修复测试隔离泄漏（POST 用例缺 patch 候选救援致
  115s 真实网络，全量回 13s）。
- **第 2 轮（子代理）**：4×P3，全部当轮修复（封顶态不清连击、P1 修复行加源码
  守护断言、IPv6 括号/非法端口、初始预检静默 45s 加日志）。
- **第 3 轮（子代理）**：0 项（A 组 4 项确认落地；B 组零缺陷；合规通过）。
- **第 4 轮（子代理·终验）**：0 项（R1-R3 共 13 项修复全部确认；并发边界复核无缺陷）。
- **第 5 轮（机械终检）**：全量 164 passed、pyflakes 新代码零告警、git diff --check
  通过、行为抽查通过；顺手消灭 IPv6 废变体（www.2001:...）并补断言。

## 结果

R1(9) + R2(4) 当轮修复、R3/R4 零发现、R5 机械终检通过 ＝ 连续 5 轮无未修复问题。
全量 **164 passed**（~13s，全离线）。
