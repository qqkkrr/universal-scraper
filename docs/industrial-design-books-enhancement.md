# 万能爬虫工具：工业设计书籍目录采集改造规格

## 背景

2026-08-26 工业设计书籍采集实测暴露了三个问题：

1. 豆瓣读书详情、豆瓣“在哪儿买”、当当搜索被拆成三个临时任务包，重复代码多，普通用户不知道如何复用。
2. 京东 PC/移动搜索页会跳登录墙；工具没有既有的“不绕过登录、记录聚合价、保留跳转链接、标记 PARTIAL”能力。
3. 零结果场景虽然已手工诊断，但工具本身没有一个统一的书籍目录工作流，无法保证以后每个任务都输出诊断与行动方案。

## 目标

把这次采集固化为万能爬虫工具的内置能力：

- 任何人都可以用一个命令完成“书名/ISBN 列表 → 豆瓣详情 → 京东/当当价格 → CSV/MD/日志 → 封面”。
- 不再要求用户手写三个任务包。
- 单本书或整批书失败时，输出字段级诊断和行动方案，不允许“0 条但显示成功”。
- 不提供整本电子书/PDF 下载，不绕过付费墙或版权限制。

## 范围

### 必须实现

1. `universal_scraper/book_catalog.py`
   - 可离线测试的 `parse_douban_book_detail`
   - `parse_douban_book_buylinks`
   - `parse_douban_book_search`
   - `parse_dangdang_search`
   - `build_catalog(spec, out_dir, ...)`
   - `build_catalog` 按 ISBN 合并去重；字段缺失写 `N/A`；每本书都有 `status`、`diagnostics`。
   - 零结果/登录墙：逐字段写入 `NO_RESULT/NO_VENDOR/LOGIN_WALL/FETCH_ERROR` 结构化诊断与行动方案，不抛异常，不显示假成功。
   - 封面下载失败不影响目录生成，本地 `cover_file` 写 `N/A`，在线 `douban_cover_url` 保留供人工访问，并写入 `COVER_DOWNLOAD_FAILED` 字段级诊断。
   - 不下载正文。

2. `universal_scraper/sites.py`
   - 注册豆瓣读书详情、豆瓣在哪儿买、豆瓣搜索、当当搜索四个精配解析器。
   - 保持现有豆瓣电影/榜单精配不回归。
   - 更新高频站点表，明确京东使用“聚合价+登录墙诊断”而不是声称直抓成功。

3. CLI
   - 新增 `books` 命令：
     `universal-scraper books --spec books.spec.json --out <目录>`
   - `--spec` 支持 `{ "books": [ {group,title,isbn,douban_subject_id,language,...} ] }`；ISBN-10/13 是主键且必须通过校验位。
   - `--no-covers` 跳过封面下载；`--interval` 可调请求间隔（默认 1.0 秒）。
   - CLI 输出 JSON 摘要，含 `rows/total/coverage/files/diagnostics`。

4. 离线测试
   - 解析器全部使用内存 HTML fixture，不联网。
   - 构建目录用注入 fetch/cover 函数，不联网。
   - 测试 0 结果、部分缺失、N/A 字段、ISBN 去重、封面失败降级。

5. 文档与示例
   - `examples/books.spec.json`
   - README 增加 `books` 用法和合规说明。

### 明确不做

- 不新增京东登录/扫码、不破解 h5st 签名、不做验证码绕过。
- 不下载图书正文/PDF。
- 不要求 API Key，不写入密钥。
- 不改变现有 `run/auto/journal` 命令行为。

## 验收标准

- `python3 -m pytest tests/ -q` 全量通过。
- `universal-scraper books --spec examples/books.spec.json --out /tmp/books-test --no-covers` 离线不会崩溃，且有诊断输出。
- 精配解析测试覆盖：详情、在哪儿买、当当、零结果。
- 最终代码连续 5 轮 code-review 无未修复 findings。

## 增补范围（ZCode 接续会话，2026-08-27）

在 CLI 已落地的基础上，把同一能力暴露到两个入口；合规约束（第 5 节）全部继续适用。

### 6. MCP 暴露（`universal_scraper/mcp_server.py`）

- 新增第 6 个工具 `books`：
  - 参数：`spec`（内联 `{"books":[…]}` 对象）或 `spec_path`（本地 JSON 路径，相对路径按仓库根解析）、
    `out`、`download_covers`、`interval`。
  - 返回：`status / total / coverage / files / sample(≤10 行精简字段) / diagnostics(≤20 条)`；
    非 OK 行带逐行 `problem_rows`。
  - 诚实性：`INVALID_SPEC` 与空结果直接返回 `error`；所有行 `NO_DATA` 时返回
    “0 条有效结果”错误 + 行动方案——两者都会让 JSON-RPC 层 `isError=true`；
    `PARTIAL` 不算失败但必须带 `warning`（指向 crawl_log.md 的字段级诊断与建议做法），
    绝不伪装成功。

### 7. WebUI 集成（`universal_scraper/webui.py` + `webui/index.html`）

- 后端：
  - `POST /api/books/start`：`{spec(json 字符串或 .json 文件路径), out, covers, interval}`
    → 创建 `books` 类型后台任务，全程实时日志；间隔钳制到 [0,10] 秒。
  - `GET /api/books/example`：返回 `examples/books.spec.json` 内容供一键填入。
  - `run_books_job` 三态语义：非法 spec → 任务失败并给出正确结构；全部行 `NO_DATA` →
    任务失败「0 条有效结果」+ 行动方案（不假成功）；部分失败 → ⚠️ 摘要列出来源计数、
    指向 crawl_log.md；全成功 → ✅ 摘要含必填缺失率。失败自动挂解决方案卡片。
- 前端（导航 04，「期刊下载」之后）：
  - spec 粘贴框（支持 JSON 或 .json 路径）、示例一键填入、格式化、输入实时校验
    （结构 + ISBN 存在性）；输出目录 / 请求间隔 / 封面开关。
  - 结果统计条对“部分失败”（total>0 且 errors>0）显式显示黄色警示横幅，不再一律绿灯。
  - 最近任务列表支持 `books` 类型的标签、回看与重跑；设置/报告页签顺延重编号（05/06）。
- 明确不做：不新增京东登录逻辑、不逆向 h5st、不下载正文；API Key 不涉及。

### 离线测试增补

- `tests/test_mcp_books.py`：注册检查、happy path（CSV/MD/JSON 导出 + isError=false）、
  坏 spec → isError=true、全源失败 → 错误 + 行动方案、PARTIAL → warning + 逐行诊断、
  spec_path 缺失可行动报错。
- `tests/test_webui_books.py`：任务三态语义同上 + .json 路径读取 + 坏 JSON 可行动报错。
