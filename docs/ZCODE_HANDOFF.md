# ZCode 交接：完成「万能爬虫工具」主任务

> 本文件由 Codex 会话生成，供 ZCode 读取后继续同一主任务。

## 1. 任务与目标

主任务：**把「万能爬虫工具」完善到“每个任务都能在工具引导下完整完成；0 条结果也必须给出诊断和行动方案，不假成功”**。

当前已完成并验证的改造（本会话）：

- 新增 `universal_scraper/book_catalog.py`
  - 豆瓣读书详情/搜索/在哪儿买、当当 ISBN 搜索解析器
  - `build_catalog`：ISBN 主键去重、字段缺失写 `N/A`、字段级 `diagnostics` + 行动方案
  - ISBN-10/13 校验、封面 magic 校验、空页/登录墙/零结果绝不假成功
- 新增 CLI：`universal-scraper books --spec <json> --out <目录> [--no-covers] [--interval]`
- `universal_scraper/sites.py` 注册 4 个精配：`douban_book_detail / douban_book_search / douban_book_buylinks / dangdang_search`
- 离线测试：`tests/test_book_catalog.py`、`tests/test_book_sites.py`
- 文档：`docs/industrial-design-books-enhancement.md`、`docs/review-rounds-20260827.md`
- 示例：`examples/books.spec.json`
- 测试现状：`python3 -m pytest tests/ -q` → **94 passed**

## 2. 项目与关键路径

- 项目根：`/Users/kairanqin/Documents/Codex/2026-08-03/ni-ha/outputs/universal-scraper`
- 新模块：`universal_scraper/book_catalog.py`
- 精配注册表：`universal_scraper/sites.py`
- CLI：`universal_scraper/cli.py`
- 测试：`tests/test_book_catalog.py`、`tests/test_book_sites.py`
- 规格与审查记录：`docs/industrial-design-books-enhancement.md`、`docs/review-rounds-20260827.md`
- 示例 spec：`examples/books.spec.json`

## 3. 常用命令

```bash
cd /Users/kairanqin/Documents/Codex/2026-08-03/ni-ha/outputs/universal-scraper

# 全量回归
python3 -m pytest tests/ -q

# 图书目录采集（示例 2 本书，不下载封面）
python3 -m universal_scraper.cli books \
  --spec examples/books.spec.json --out outputs/book_catalog --no-covers --interval 1.0

# 精配单页验证
python3 -m universal_scraper.cli sites --run 'https://book.douban.com/subject/4230237/'
python3 -m universal_scraper.cli sites --run 'https://search.dangdang.com/?key=9787563394180'
```

## 4. 待办 / 建议下一步

1. **WebUI 优化**：用 `design-taste-frontend` skill 优化 `webui/index.html`，让 `books`/精配能力在界面上可直接用。
2. **MCP 暴露**：考虑把 `books` 加入 `universal_scraper/mcp_server.py`，让 ZCode/其他 AI 客户端可直接调用。
3. **模型通道对照**：`scripts/model_channel_test.py` 继续做官网 DeepSeek vs 硅基流动/本地代理对照（Key 只从环境变量读）。
4. **持续质量**：保持“连续 N 轮 code-review 零问题”的习惯；新增功能必须补离线测试。
5. **监控/自动化**：如需定时采集书目或监控页面变化，用 `schedule`/`monitor` 命令。

## 5. 约束与合规（必须遵守）

- API Key 一律从环境变量读取，**不写入文档或代码**。
- 京东搜索/详情页触发登录墙时：**不绕过登录、不逆向 h5st、不获取提交登录态**；用豆瓣“在哪儿买”公开聚合价 + 跳转链接，并写 `LOGIN_WALL` 诊断。
- 不下载整本电子书/PDF，不绕过版权或付费墙；只采集书目、公开封面与价格。
- 0 条结果必须输出诊断和行动方案，不能显示“成功”。

## 6. 如何继续（ZCode 侧）

1. 在 ZCode 打开项目：
   `/Users/kairanqin/Documents/Codex/2026-08-03/ni-ha/outputs/universal-scraper`
2. 新会话第一条消息可写：
   “先读取 `docs/ZCODE_HANDOFF.md`，按里面的任务目标和待办继续完善万能爬虫工具，先跑 `python3 -m pytest tests/ -q` 确认基线。”
3. 或者用 ZCode CLI 一键接手（见下方命令）。

## 7. ZCode CLI 一键接手命令

```bash
cd /Users/kairanqin/Documents/Codex/2026-08-03/ni-ha/outputs/universal-scraper
node /Applications/ZCode.app/Contents/Resources/glm/zcode.cjs \
  --prompt "先读取 docs/ZCODE_HANDOFF.md，按其中的任务目标与待办继续完成万能爬虫工具；先运行 python3 -m pytest tests/ -q 确认基线。" \
  --cwd "$PWD"
```

> 若想延续某个已存在会话：`node .../zcode.cjs -c`（继续当前目录最近会话）或 `--resume <sessionId>`。
> 若想把本文件作为附件：追加 `--attach docs/ZCODE_HANDOFF.md`。
