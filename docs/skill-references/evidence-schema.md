# 证据与任务目录 schema（batch2400 战训：21 个并行子代理自创 4 种证据命名，汇总时费时费力）

并行批次/多代理场景下，**每个任务目录必须长一个样**——否则跨代理汇总就是灾难。
本 schema 是 batch 命令 + `verify --dir` 审计的约定。

## 任务目录布局

```
outputs/task_<编号>/
├── data.json | data.csv | data.xlsx   # 数据产出（至少一个；0 条按铁律 3 出诊断）
├── summary.json                        # 台账：{"id","status","result","evidence":[...]}
├── report.md                           # 交付说明：数量/去重/抽查/来源/注意事项
├── evidence_http.html                  # 被拦截/403/验证码页面的原始 HTML
├── evidence_capture.json               # capture_all 副本（接口侦察证据）
├── evidence_network.txt                # 网络失败时间线（URL+状态码）
└── evidence_<自定义>.<ext>             # 其他证据（前缀必须 evidence_）
```

## 字段约定

- **summary.json.evidence**：列出本任务实际产出的证据文件名数组——
  `verify --dir` 会核对引用的文件是否真的存在（防"报告说留了证据、实际没有"）。
- **证据文件名前缀必须 `evidence_`**（`verify --dir` 只认这个前缀做存在性统计）。
- **capture_all.json / last_page.html / recon_records.json** 由工具自动产出的
  原始侦察文件，保留原命名（它们是 `capture2config` 的输入）。

## 通用目录审计

```bash
PYTHONPATH="${SKILL_DIR}" python3 -m universal_scraper.cli verify --dir outputs/task_2061
```

verdict 判定：`ok`（有记录且引用证据齐全）/ `partial`（有数据但证据缺）/
`empty`（只有空文件）/ `no_data_files`（连数据都没有）。
并行批次收尾时对每个任务目录跑一次，汇总表直接可用。
