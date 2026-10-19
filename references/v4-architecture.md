# v4 统一入口与 CrawlPlan

## 一句话

v4 把所有采集入口（`--config` / `--task`）先归一为 **CrawlPlan**，再由统一
`v4.run_plan()` 路由到成熟执行后端；前端、计划格式、版本号统一，执行器按
`profile` 共存，后续可逐个替换而不改用户接口。

## 命令

```bash
# 默认走 v4（行为与旧版一致）
python3 -m universal_scraper.cli run --config task.json
python3 -m universal_scraper.cli run --task tasks/my_task

# 排障回退：直连旧执行器
python3 -m universal_scraper.cli run --config task.json --engine legacy

# 把归一化计划落盘审阅（不改变执行）
python3 -m universal_scraper.cli run --config task.json --plan-out plan.json
```

## CrawlPlan 结构

```json
{
  "schema_version": 4,
  "name": "quotes",
  "origin": "config",
  "profile": "linear",
  "features": ["config", "pagination", "detail"],
  "seeds": ["https://quotes.toscrape.com/"],
  "source": {},
  "extraction": {},
  "pagination": {},
  "detail": {},
  "pipeline": [],
  "output": {},
  "run": {},
  "storage": {},
  "rules": [],
  "parsers": {},
  "base_dir": "...",
  "task_dir": null,
  "raw": {}
}
```

`origin` 为 `config`（v2 配置）或 `task`（v3 任务包）；`profile` 为
`linear` 或 `queue`；`features` 是诊断标签；`raw` 保留原始配置供执行器消费。

## 两个 profile 的分工

| profile | 来源 | 执行后端 | 适用 |
|---|---|---|---|
| `linear` | `--config` 的 v2 JSON | 成熟 v2 线性执行器 | 列表+详情、JSON API、固定分页、iterate、sitemap、cache/replay |
| `queue` | `--task` 的 v3 任务包 | 成熟 v3 队列执行器 | 递归爬取、rules 路由、精确 resume、插件模块、分布式队列 |

## 为什么先保留两个后端

把 v2 的全部语义（page_param/offset/template、iterate、sitemap、cache/replay、
incremental、browser_script 桥）一次性搬进 queue 内核，属于高风险重写；
本阶段先统一**入口与计划层**，保证：

- 现有配置/任务包零改动；
- 出问题可 `--engine legacy` 一键回退；
- 后续替换执行器时，用户命令与 plan 格式不变。

## 迁移下一阶段

1. 在 queue 内核增加 linear 编译器（v2 分页/详情/iterate 适配器）。
2. 差分测试：同一 fixture 的 v4-linear 与 v4-queue 逐行一致。
3. 默认切换后，`legacy` 保留 1~2 个版本再删除。

## 代码位置

- `universal_scraper/plan.py`：IR + `from_config` / `from_task` / `dump`
- `universal_scraper/v4.py`：`run_plan` / `run_config` / `run_task`
- `tests/test_v4_plan.py`：归一化、路由、与 legacy 结果一致性
