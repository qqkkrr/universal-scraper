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
4. **账本**：每次尝试一行 JSONL（`~/.universal-scraper/chain/<key>.journal.jsonl` ✓），
   跑挂后能回答"哪一步、因为什么、补了几次"。

附带：**进程级单飞锁**（同 `--state-key` 拒绝双开 ✓ —— 双开浏览器会互相抢配额 ✗，
原子占锁 + 陈旧锁清理 ✓；锁文件坏/pid 失效不会造成永久锁死 ✓）、
**硬时钟**（到点不再启动新步骤，且会用剩余时间**收紧当前步超时** ✓）、
**超时杀整个进程组** ✓✗（`subprocess.run(timeout=)` 只杀直接子进程，桥/脚本的孙子进程会继续
跑继续写 ✗ —— 见第五节）。

**出口语义**：只要"没真正成功"，`status` 就**不是** `done` 且 CLI 退出码非 0 ✗✓ ——
包括判死后重跑仍死（`death_persists`）、非约定退出码（`step_failed`）、
判死器读不到日志（`judge_unavailable`，绝不 fail-open 当健康 ✓）、自愈失败（`probe_fail`）、
账本写不进去（`ledger_broken`）、日志写不进去（`log_broken` —— 日志是判死输入，
写坏=判死失效且审计轨迹丢失 ✗）、步骤不认 `--until`（`until_unsupported` —— 否则会被误报成
"硬时钟/掉登录"，运维查错方向 ✗）。
`Step(required=False)` 的**可选步骤**：只有**步骤自身**失败（`step_failed`/`death_persists`）
才允许"继续下一级 + 整体记 `partial`" ✓；**环境级失败**（致命码/硬时钟/探针死/判死器失明）
**对可选步骤也必须全停** ✗✗ —— 继续跑 = 继续烧配额、继续在不可审计状态下采集。
`soft_stops` 列出"到软检查点提前收工"的步骤 ✓（收工是**计划内**动作，退出码仍 0 ✓，
但必须在 payload 里看得见，否则与"真跑完"不可区分 ✗）。
断点续跑仍由各步骤自己负责（幂等 + done 标记 ✓）。

**单飞锁 = 内核 `flock`** ✓（R32 修）：旧实现"读 pid → 判活 → 删旧锁"是 **TOCTOU** ✗✗ ——
两个进程可同时把同一份陈旧锁判为"死 pid"，于是互删对方刚建的锁、各自都认为持锁
（实测 N=12 时 7~9 个同时持锁 ✗）。`flock` 由内核保证互斥、进程退出（含 `kill -9`）自动释放 ✓，
锁文件内容只作诊断（坏 JSON/缺 pid/死 pid 都不会锁死 ✓），且**绝不 unlink 锁文件** ✓
（删掉会换 inode，别人新开的 fd 锁的是另一个对象 ✗）。

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
   `report` 默认**不覆盖**自设值 ✓（`--warn-days 0` = 只想过期后提醒，不会被吃成 7 ✓）。

⚠ **`allow_empty` 的准确口径**（R32 澄清）：它声明"这个源**确实可能空**"，因此 `count=0` 时
规则 1 与规则 2 **都不告警** ✓（否则同一个 0 会被两条规则重复报同一件事 ✗）。
要区分"允许空"与"配额耗尽"，**别用 allow_empty**——那是把两种语义混在一个开关里 ✗；
应登记**独立的源名**（如 `x_panel` 与 `x_panel_quota_sensitive`）或期待区间 ✓。

另几条：**本次运行状态非 ok 立即判 `failing`** ✓（不必等 3 天 ✗）；账本最近 3 笔非 ok → `failing`；
距上次记录超过登记间隔 2 倍 → `stale`（时间不可解析 → `stale_unknown`，不静默放行 ✗）；
未提供 count → `no_count` ✗（不假装正常；**首次运行**无历史时是 `no_baseline` ✓ 属安心值）。

**R32 新增三条判词（都是"看着健康其实出事"）**：
- `no_record` ✗✗：源**登记了**"每 N 小时一次"却**从未落过账**（超过 2× 间隔）→ 采集器可能压根没跑
  或已静默停摆。旧实现落 `no_baseline`（安心值、退 0）——事故原型（配额耗尽后 0 条）正是这形态 ✗✗；
- `state_recovered` ✗：状态档案损坏被隔离重建 → 登记与凭据日历可能已丢（且 `expect_interval_h`
  随之消失会让陈旧规则**静默关闭**：同一条检查从 `stale` 退 1 变成 `ok` 退 0 ✗✗——毁数据反而变绿）；
- `config_invalid` ✗：某源的登记值类型不对（如 `allow_empty: "false"` 字符串、sources 值不是对象）
  → 按保守侧处理并告警（旧实现要么当真是真 ✗，要么直接抛 AttributeError 裸栈 ✗）。

判定词表：`ok / no_baseline / no_count / empty_suspicious / volume_drop / volume_spike /
stale / stale_unknown / no_record / state_recovered / config_invalid / failing / ledger_unreadable`。

**退出码是白名单**：只有 `ok` / `no_baseline` 返回 0，**其余一律 1** ✓
（`volume_drop`、`stale` 也要人工看 ✓；将来新增告警词默认落在"要人工看"一侧 ✓）。
账本整体不可读 → `ledger_unreadable`（与"没有历史"严格区分 ✓✗）；
state 损坏（含**合法 JSON 但结构错**）→ 隔离重命名后重建，并在 `check`/`report` 里报警 ✓。
`record` 也会体检账本：**尾部缺换行**（上次写盘崩溃的残行）会先补 `\n` 再记 ✓（否则新记录被粘死、
永久不可读 ✗），账本**尾部不可解码**则拒绝记录并返回 `ok:false` ✓（写侧与审计可用性挂钩 ✓）。

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
- **超时必须杀整个进程组** ✓✗：`subprocess.run(timeout=)` 只杀**直接子进程**，而步骤常常是
  脚本/桥（node .cjs → 浏览器），父进程被杀后**孙子进程会继续跑并继续写数据** ✗。
  正确形态：`Popen(..., start_new_session=True)` + 超时后 `os.killpg(os.getpgid(p.pid), SIGKILL)`
  （chain 的步骤执行与探活都走同一个 helper `_run_killgroup`，本模块**所有** subprocess 调用
  集中在 `_run_killgroup` / `_run_cmd` 两个函数内——`tests/static_checks.py` 有对应检查 ✓）。
  **停机路径同样要杀** ✓：`KeyboardInterrupt`/`SystemExit` **不是** `Exception` ✗，旧实现让它们
  直接穿过 → 步骤进程组还在跑而 `finally` 已**释放单飞锁** → 立刻重启就是"双开写同一份数据" ✗✗
  （实测：SIGTERM 后锁没了、旧步骤仍在写、重启的链报 done ✗）。现在 `_run_killgroup` 对
  `BaseException` 也先整组杀再上抛 ✓。
  ⚠ **硬约束（不可修，写清不假装）**：若步骤脚本让后代 `setsid()`/`nohup` **脱离会话**
  （Node `detached:true`、浏览器守护化），`killpg` **打不到**它 ✗ —— 那时代后已换会话、
  `ps` 里的 sess 也不是步骤 pid，事后无从追溯。**步骤脚本不得让后代脱离会话** ✓；
  ⚠ **全仓同类站点待分诊**（2026-10-13 普查：18 处 `subprocess.*(timeout=...)` 仅 chain 做了进程组隔离）：
  高危=桥/浏览器类（`sites.py` ×3、`browser_agent.py`、`modules/xhs.py`——它们的子进程会拉起浏览器）；
  低危=叶子命令（`net.py` ×3、`doctor.py` ×2、`cookies.py`、`runtime.py`、`webui.py`、
  `core.py`、`modules/fetchers.py` ×2 多为 curl/git 类，无后代）。
  桥类站点**不能盲改**：有的桥故意让浏览器在调用之间存活（杀组会改语义 ✗）→ 需逐处读意图后处理；
- **写失败必须可见** ✓：账本/日志/状态写不进去要计数上浮（链条 → 状态 `ledger_broken`；
  台账 → `ok:False`），绝不"看着成功" ✗；状态文件损坏则隔离重命名（不静默丢 ✗）；
- **判死器不可用 = 失败** ✓✗（读不到日志时返回 `None` → `judge_unavailable`，不允许 fail-open 判健康）；
- 所有函数**不抛异常** ✗（返回结构化结果 ✓），CLI 用退出码表达"要不要人工看" ✓；
- 这两件是**运维原语**，不替代各采集器自身的幂等与断点续跑 ✓。
