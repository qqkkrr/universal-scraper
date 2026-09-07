#!/usr/bin/env python3
"""📖 并行子代理规范生成器（batch2400 战训：GLM 用 21 个并行子代理跑 200 项，
自建 AGENT_GUIDE.md 才让规范在并发下不变形——本模块把它内置为标准件）。

用法:
  python3 -m universal_scraper.cli guide --out AGENT_GUIDE.md
发给每个并行子代理，保证铁律/证据契约/续跑约定在多代理下不变形。
"""
from __future__ import annotations

TEMPLATE = """# 子代理执行规范（AGENT_GUIDE）

> 由 universal-scraper 生成。并行批次的每个子代理必须遵守本文件。
> 上级代理（调度者）负责分派任务；你（子代理）负责单个任务的执行与记账。

## 一、任务领取与记账（崩溃可续的关键）

1. 领取任务用 `cli batch --queue <队列文件> claim`（返回 running 状态的任务）；
   禁止自己遍历 JSON 找 pending——会双跑。
2. 任务崩溃/超时由调度器自动回收（30 分钟过期回 pending），**不要**手工改状态文件。
3. 完成 → `cli batch --queue <队列> done <id> --result "<核对结论>"`；
   数据不存在于公开渠道 → `nodata <id>`（附 WebSearch 证据，区别于 failed）；
   需要用户/境内网络/验证码 → `blocked <id>`；修复工具后 → `retry <id>`。

## 二、任务目录契约（每任务一个目录）

```
outputs/task_<编号>/
  data.json|data.csv    # 数据产出（必须有，0 条按铁律 3 出诊断报告）
  summary.json          # {"id":..., "status":..., "evidence":["evidence_xx", ...]}
  evidence_<类型>       # 证据文件（见下）
  report.md             # 数量/去重/抽查/来源/注意事项
```

## 三、证据 schema（反爬证据必须留档，禁止只说"失败了"）

- `evidence_http.html`：被拦截/403/验证码页面的原始 HTML
- `evidence_capture.json`：capture_all 副本（接口侦察证据）
- `evidence_network.txt`：网络失败时间线（403/421/超时的 URL+状态码）
- `budget --list`：域名封锁台账（403/421/52x 自动记账，doctor 会显示）

## 四、并发纪律

- **HTTP 并发上限 ≤ 6**；同域请求间隔 ≥1s（礼貌限速，playbook 第三章）
- **LLM API 调用限速**：并发子代理数 ≤8；遇 429 指数退避（2s/4s/8s），
  连续 3 批 429 → 收窄并发减半（batch2400 战训：19 并发吃了 8 批 1302 失败）
- 同一域名禁止被多个子代理同时猛打——调度者按域名分片

## 五、铁律（不可违反）

0 结果不假成功（0 条必须出诊断）｜样本先行/或数据型替代验证（字段完整率+
数值合理性+交叉源）｜不绕登录墙/不逆签名/不破验证码｜数据只落地本地｜
换 IP/配额问题先判型（playbook 第七章）再动手。

## 六、验收

任务完成前：`cli verify --dir outputs/task_<编号>` 自检（记录数/字段完整率/
证据存在性），verdict=ok 才交付；partial 说明缺什么。
"""

TELEMETRY = """📖 AGENT_GUIDE 已生成：建议随任务分派发给每个并行子代理，
并把此文件路径写进调度 prompt（"先读 AGENT_GUIDE.md 再开工"）。"""


def render() -> str:
    return TEMPLATE


def emit(out: str | Path = "AGENT_GUIDE.md") -> Path:
    from pathlib import Path
    p = Path(out).expanduser()
    p.write_text(render(), encoding="utf-8")
    return p
