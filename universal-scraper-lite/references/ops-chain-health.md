# 运维原语：自愈链条与源健康台账（R49 沉淀）

> 来源：miit 课题「阶段 R」43 小时无人值守采集（2026-10-11→13）的**四起事故**复盘。
> 代码：`universal_scraper/chain.py`（`chain` 子命令）、`universal_scraper/health.py`（`health` 子命令）。
> 本仓库**没有** `us` 可执行文件 ✗ —— 命令一律走模块入口：
> `PYTHONPATH="${SKILL_DIR}" python3 -m universal_scraper.cli <子命令> [参数]`（下同）。

## 一、为什么需要这两件（事故台账）

| 事故 | 表现 | 为什么工具没拦住 |
|---|---|---|
| 断网（10-12 ~05:00） | 4,812 个请求 `ERR_INTERNET_DISCONNECTED` | 采集器逐款 try/except 吞掉 ✗ → 退出码 0 ✗ |
| 浏览器崩溃 #1（10-11 23:25） | 1,078 款失败 | 同上 |
| 浏览器崩溃 #2（10-12 13:30） | 2,066 款失败 + **R3/R4"9 秒跑完 1,751 款"** ✗✗ | 自愈只认退出码 1 ✗ → 永不触发 ✗ |
| 面板配额耗尽（10-12 12:50） | 第 50 款起全失败，页面渲染配额提示 | 失败长得像"0 条" ✗ |

**共同点：全是静默失败** ✗ —— 逐款失败被吞、退出码干净、结果文件"看起来正常" ✓✗。
（四起事故归为**三类**死法：断网 / 依赖进程崩溃 / 配额耗尽 —— 前两类靠 `chain` 的判死+自愈，第三类靠 `health` 的空结果告警 ✓。）

## 二、`chain`：按**日志内容**判死

```bash
PYTHONPATH="${SKILL_DIR}" python3 -m universal_scraper.cli chain \
  --step "R2=python3 r2_snapshot.py" \
  --step "R5=python3 r5_versions.py" \
  --probe-http http://127.0.0.1:9222/json/version \
  --heal-cmd "python3 qimai_from_chrome_profile.py --keep-open" \
  --until "2026-10-13 11:30" \
  --deadline "2026-10-13 12:00" \
  --state-key rescue
```

四条原语：

1. **步骤**：退出码约定 `0 完成 / 2 致命（链条全停）/ 3 软检查点（进下一级）`；
   **约定之外的码一律判失败** ✓（曾把"退出码 1"静默当完成 ✗）；
2. **探针**：每步开始前先探活，不活则自愈（HTTP 探活 + 自愈命令，自愈后最多轮询 90s ✓）；
3. **判死 = 扫日志内容特征计数**（默认 `TargetClosedError`、`ERR_INTERNET_DISCONNECTED`）——
   **不看退出码** ✓✓：本次尝试新增内容命中 ≥10 次即判死 → **配了探针则先自愈**、然后**重跑一次** ✓；
   只数**本次尝试新增的字节**（按日志水位），不把前序步骤/上次尝试的死亡文本算进来 ✓；
4. **账本**：每次尝试一行 JSONL（`~/.universal_scraper/chain/<key>.journal.jsonl` ✓），
   跑挂后能回答"哪一步、因为什么、补了几次"。

附带：**进程级单飞锁**（同 `--state-key` 拒绝双开 ✓ —— 双开浏览器会互相抢配额 ✗，
原子占锁 + 陈旧锁清理 ✓；锁文件坏/pid 失效不会造成永久锁死 ✓）、
**硬时钟**（到点不再启动新步骤，且会用剩余时间**收紧当前步超时** ✓）。

**出口语义**：只要"没真正成功"，`status` 就**不是** `done` 且 CLI 退出码非 0 ✗✓ ——
包括判死后重跑仍死（`death_persists`）、非约定退出码（`step_failed`）、
判死器读不到日志（`judge_unavailable`，绝不 fail-open 当健康 ✓）、自愈失败（`probe_fail`）、
账本写不进去（`ledger_broken`）。断点续跑仍由各步骤自己负责（幂等 + done 标记 ✓）。

## 三、`health`：把"0 条"变成会报警的事

```bash
PYTHONPATH="${SKILL_DIR}" python3 -m universal_scraper.cli health \
  register qimai_offline --interval-h 24          # 登记源（预期更新间隔）
PYTHONPATH="${SKILL_DIR}" python3 -m universal_scraper.cli health \
  check qimai_offline --count 2293                 # 判定（退出码 1 = 需人工看 ✓）
PYTHONPATH="${SKILL_DIR}" python3 -m universal_scraper.cli health \
  record qimai_offline --count 2293                # 跑完记一笔
PYTHONPATH="${SKILL_DIR}" python3 -m universal_scraper.cli health \
  cred qimai_vip --expires "2026-10-13 14:05" --warn-days 3
PYTHONPATH="${SKILL_DIR}" python3 -m universal_scraper.cli health report
```

三条硬规则（全部来自事故 ✓）：

1. **空结果 ≠ 正常** ✓✓：`count == 0` 默认判 `empty_suspicious`（配额耗尽/页面改版都长这样 ✗）；
   确有空结果的源显式 `register --allow-empty` ✓；
2. **量级突变**：`< 0.5×` 或 `> 2×` 滚动中位（近 20 笔）→ `volume_drop` / `volume_spike`；
3. **凭据到期日历** ✓：登记 `--expires` 后，进入 `--warn-days` 窗口即进报告 ——
   七麦那次就是**临到期才发现** ✗（VIP 到期日必须进日历 ✓）；各凭据可自设窗口，
   `report` 默认**不覆盖**自设值 ✓。

另几条：**本次运行状态非 ok 立即判 `failing`** ✓（不必等 3 天 ✗）；账本最近 3 笔非 ok → `failing`；
距上次记录超过登记间隔 2 倍 → `stale`（时间不可解析 → `stale_unknown`，不静默放行 ✗）；
未提供 count → `no_count` ✗（不假装正常）。

判定词表：`ok / no_baseline / no_count / empty_suspicious / volume_drop / volume_spike /
stale / stale_unknown / failing / ledger_unreadable`。

**退出码是白名单**：只有 `ok` / `no_baseline` 返回 0，**其余一律 1** ✓
（`volume_drop`、`stale` 也要人工看 ✓；将来新增告警词默认落在"要人工看"一侧 ✓）。
账本整体不可读 → `ledger_unreadable`（与"没有历史"严格区分 ✓✗）；state 损坏 → 隔离重命名后重建并告警 ✓。

## 四、组合用法（推荐动线）

```bash
# 每个采集器跑完记一笔（在脚本末尾调用 health.record ✓）
# 链条包住多步采集，用 health 的判定做"要不要人工看"的闸门
PYTHONPATH="${SKILL_DIR}" python3 -m universal_scraper.cli chain \
  --step "daily=python3 daily_collect.py" --state-key daily
PYTHONPATH="${SKILL_DIR}" python3 -m universal_scraper.cli health \
  check daily --count "$(wc -l < out/daily.jsonl)"   # 退出码 1 → 触发人工复核 ✓
```

## 五、设计约束（勿放宽）

- 外部命令一律**参数列表** + `shell=False`，调用形态固定 `["/usr/bin/env", *argv]`
  （列表首元素字面量常量 ✓：本环境安全钩子要求，同时免去硬编码安装路径 ✓）；
  字符串命令被 `_validate_argv` 挡在调用点之前 ✗；
- **写失败必须可见** ✓：账本/日志/状态写不进去要计数上浮（链条 → 状态 `ledger_broken`；
  台账 → `ok:False`），绝不"看着成功" ✗；状态文件损坏则隔离重命名（不静默丢 ✗）；
- **判死器不可用 = 失败** ✓✗（读不到日志时返回 `None` → `judge_unavailable`，不允许 fail-open 判健康）；
- 所有函数**不抛异常** ✗（返回结构化结果 ✓），CLI 用退出码表达"要不要人工看" ✓；
- 这两件是**运维原语**，不替代各采集器自身的幂等与断点续跑 ✓。
