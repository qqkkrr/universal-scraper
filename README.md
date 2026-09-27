# 万能爬虫技能（universal-scraper skill）

给 AI 编程助手（ZCode / Claude Code / Codex 等）安装的**引导式网页采集技能**。
装好之后，你只要用大白话告诉 AI"想从哪个网站拿什么数据"，它会：

1. 替你踩点网站、判断有没有反爬；
2. 先抓 5 条样本给你过目，字段不对就改；
3. 你说"开抓"才全量采集，随时播报进度；
4. 交付 Excel/CSV + 图表，附数量、抽查结果和注意事项。

## 两个版本

| 版本 | 目录 | 适合 |
|---|---|---|
| **Full（本仓库根目录）** | `universal_scraper/` + `scripts/` + `webui/` | 完整引擎：配置驱动采集、科研批量（`cli research`）、交付审计（`cli audit`）、浏览器桥、验证码协同、WebUI |
| **Lite（`universal-scraper-lite/`）** | 纯文档 | 只要"兵法+军纪"：判型表（30+ 判例行）、配方库 R1-R41、20+ 实战判例档案、采集铁律与出口审计清单——不含引擎代码，任何执行者可参考 |

## 安装

把本目录放进 `~/.agents/skills/universal-scraper/`，然后跑一次：

```bash
bash ~/.agents/skills/universal-scraper/scripts/setup.sh
```

看到"全部就绪"即可。以后每次用不需要再装。（只想读知识手册的话，把 `universal-scraper-lite/` 单独拷走即可，零依赖。）

## 使用

直接对 AI 说人话，例如：

- "把这个列表页的商品都抓下来，要名称、价格、链接，存桌面"
- "帮我下载这 10 篇论文的 PDF"
- "这个网站要登录，我扫码登录一次之后你接着抓"
- "盯着这个页面，有更新就告诉我"

科研批量（企业清单×年份窗×关键词词典 → 年报面板+文本档案）：

```bash
PYTHONPATH=. python3 -m universal_scraper.cli research run --universe 清单.csv --out 输出目录 --year-from 2010 --year-to 2024
PYTHONPATH=. python3 -m universal_scraper.cli research panel --out 输出目录 --universe 清单.csv --xlsx 面板.xlsx
PYTHONPATH=. python3 -m universal_scraper.cli audit panel --xlsx 面板.xlsx --texts 输出目录/texts --keywords 词典.json
```

## 目录说明

| 路径 | 内容 |
|---|---|
| `SKILL.md` | AI 读取的工作流程（五幕引导 + 铁律） |
| `references/` | 反爬升级手册 / 配置字段参考 / 任务配方 |
| `universal_scraper/` | 采集引擎本体（10 轮审计 + 科研五任务实战 + 数十轮对抗审查） |
| `scripts/` | 环境安装、体检、调试 Chrome 启动器、浏览器桥（含资源拦截提速） |
| `webui/` | 可选的本地可视化界面（`cli webui` 启动） |
| `universal-scraper-lite/` | 纯知识与纪律手册（判型表+配方库+验收清单） |
| `安装与使用说明.md` | 详细安装/依赖/自检说明 |

## 边界（诚实条款）

- 不绕过登录墙、不逆向签名、不破解付费/版权墙——登录站走"你亲手登录一次、工具复用"。
- 个别对自动化全量封锁的站点（如 gov.cn），会明确告诉你并给出"手动另存 + AI 解析"的替代方案。
