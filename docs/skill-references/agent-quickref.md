# Agent Quick Reference · universal-scraper

> 给自主 AI agent 的单页速查。人类小白请回读 SKILL.md（面向小白的引导教程）。

## 命令模板

```bash
PYTHONPATH="${SKILL_DIR}" python3 -m universal_scraper.cli <cmd> [args]
```

## 判型 → 命令速查

| 目标页特征 | 命令 | 备注 |
|---|---|---|
| 静态 HTML，数据在源码里 | `fetch <url>` | 出 markdown，`--json` 出信封结构 |
| JS 渲染壳 | `fetch <url> --browser` | playwright 桥接，CDP 降级 |
| JSON API（已知端点） | `run --config`（http_json） | 或直接 curl_cffi |
| 接口未知（SPA） | `fetch <url> --browser` + capture_all | 找 POST 体→改 http_json |
| 列表+详情 | `run --config`（browser + detail） | |
| PDF 下载 | `pdf --download 清单.json --out 目录` | %PDF 校验+断点 |
| PDF 表格 | `pdf --tables x.pdf` | pdfplumber |
| 批量队列 | `batch --queue q.json next/claim/done/fail/nodata/retry/status` | |

## 失败 → 处置

| 症状 | 判定 | 动作 |
|---|---|---|
| 0 条 + 页面 200 | JS 壳 | → fetch --browser |
| 0 条 + 页面 403/421 | IP 封/配额 | → budget 已自动记；换 IP 或冷却 |
| 0 条 + 空数据 | nodata | → WebSearch 核验 → batch nodata |
| 连续 3 次网络失败 | 出口变化 | → doctor.py 复查 |
| 字段完整率 < 0.9 | 部分成功 | → verify --dir 看细节 |

## fetch --json 输出契约

```json
{"url": "...", "status": 200, "text": "<页面markdown，内嵌JSON则为转义字符串>"}
```

text 内含 JSON 时先找到 JSON 起始位置再解析，不要把信封当数据。

## capture_all.json 输出契约

```json
[{"url": "...", "method": "POST", "post_data": "...", "request_headers": {...}, "json": {...}}]
```

## 运行时行为

- HTTP 客户端默认 curl_cffi + chrome 指纹 + verify=False + 3 次重试
- 403/421/52x 自动记入域名封锁台账（budget --list 查）
- 连续 3 次网络失败自动提示 doctor 复查
- strategy=none + 空 records_path → 自动识别常见键(records/items/list/results/data)
- source.single_record=true → 整响应体作为一条记录（GraphQL 类）

## 配额管理

- `budget --mark 域名 --hours 24`：手动记账
- `budget --check 域名`：查冷却状态（exit 0=可访问, 2=冷却中）
- `budget --list`：全台账

## 知识引用

- 反爬升级阶梯 / 配额四分类 / 三分叉 → `references/anti-block-playbook.md`
- 配方 R1~R25 → `references/recipes.md`
- 配置字段 → `references/spec-schema.md`
- 交易所索引 → `references/data-sources-exchanges.md`
- 证据 schema → `references/evidence-schema.md`
