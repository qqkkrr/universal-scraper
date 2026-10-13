"""🔗 自愈链条（`chain` 子命令）——把「跑挂了自己爬起来」变成可复用原语。

**战场原型**（miit 课题阶段 R，2026-10-11→13）：43 小时无人值守里断网 1 次 + 浏览器崩溃 2 次，
全部是**静默失败**；更致命的是采集器会把逐款失败吞掉 → **退出码仍是 0** ✗
→ 只看退出码的自愈**永不触发** ✗✗（实测：曾带着死浏览器"9 秒跑完 1,751 款"✗）。

**修法（本模块的四条原语）**：
1. **步骤** = (名称, 命令, 超时, 软检查点 `--until`)；退出码约定：0 完成 / 2 致命（硬时钟/掉登录 → 全停）/
   3 软检查点（到点收工 → 进下一级）；**约定之外的码一律判失败**（不再静默当完成 ✗）；
2. **探针** = 依赖探活 + 自愈（例：CDP 浏览器 → `curl /json/version` 探活 → 重拉）；
   **每步开始前先探活** ✓（避免带着死依赖空转 ✗）；
3. **死亡判定 = 扫日志内容**（默认特征：`TargetClosedError`、`ERR_INTERNET_DISCONNECTED`），
   **不看退出码** ✓✓ —— 这是本模块存在的理由；只扫**本次尝试新增的字节**（按日志水位 ✓），
   避免把前序步骤/上次尝试的死亡文本算到本次头上 ✗；
4. **账本** = 每次尝试一行 JSONL（步骤/时刻/退出码/本次死亡命中数/执行异常 ✓）。

**出口语义（R31 审查修复）**：任何"没真正成功"的收尾都会让 `status != "done"` 且 CLI 退出码非 0 ✗✓ ——
包括：非约定退出码、判死后重跑仍死、判死器不可用、自愈失败、账本/日志写坏；
`required=False` 的可选步骤失败时链条继续，但整体记为 `partial`（不报全绿 ✓）。失败步骤默认**中断链条** ✓。

**执行安全约定**（与 `core.run_tool` 一致 ✓）：外部命令一律走**参数列表** + `shell=False`，
且调用形态固定为 `["/usr/bin/env", *argv]` —— 列表首元素是**字面量常量**（本环境安全钩子要求 ✓，
同时免去硬编码各机器不同的安装路径 ✓）。`_validate_argv` 再做入参体检（非列表/空/非字符串一律拒绝 ✗）。

与 `jobs` 子命令的区别：`jobs` 是**纯顺序跑** ✗；本模块管**跑挂了怎么办** ✓。
断点续跑由各步骤自己负责（幂等 + done 标记 ✓），链条只保证"不重复不遗漏地把它送到终点" ✓。

用法（CLI，注意本仓库**没有** `us` 可执行文件，须走模块入口 ✓）：
    PYTHONPATH=<插件目录> python3 -m universal_scraper.cli chain \
      --step "R2=python3 r2_snapshot.py" \
      --probe-http http://127.0.0.1:9222/json/version \
      --heal-cmd "python3 qimai_from_chrome_profile.py --keep-open" \
      --deadline "2026-10-13 12:00"
"""
from __future__ import annotations

import fcntl
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

STATE_DIR = Path.home() / ".universal_scraper" / "chain"
DEFAULT_DEATH_PATTERNS: Tuple[str, ...] = (
    "TargetClosedError",              # Playwright：页面/上下文/浏览器没了（实测最常发生 ✗）
    "ERR_INTERNET_DISCONNECTED",      # 断网（实测 4,812 次 ✗）
    "Target page, context or browser has been closed",
)
TAIL_BYTES = 6000             # 仅用于"没有水位时"的兼容窗口（见 tail_death_hits ✓）
DEATH_HITS_THRESHOLD = 10
HEAL_PROBE_WAIT_S = 1.0       # 自愈命令"秒退探测"窗口 ✓（见 _run_cmd）
HEAL_RECHECK_TRIES = 3        # 自愈成功后强制复验次数 ✓（R32：只信 heal() 的 True 是假恢复 ✗）
MIN_STEP_TIMEOUT_S = 60       # 单步超时下限（无人值守场景别把步骤切太碎 ✓；测试可临时下调 ✓）
MAX_LOG_LINE_BYTES = 1 << 20  # 判死逐行扫描的单行上限（病态超长行按块切 ✓）
UNTIL_UNSUPPORTED_MARKER = "unrecognized arguments: --until"
# 环境级失败：对 required=False 的步骤**也不得降级**（依赖已死/判死器失明/硬时钟到点，
# 继续跑 = 继续烧配额、继续在不可审计状态下采集 ✗）
ENV_LEVEL_OUTCOMES = frozenset({"fatal", "deadline", "probe_fail", "judge_unavailable"})


def _now() -> datetime:
    return datetime.now()


def _parse_dt(s: str) -> Optional[datetime]:
    """时间解析：先试 `fromisoformat`（吃 T 分隔/微秒/时区 ✓），再回落到格式表。

    **带时区偏移的值按本地时区换算** ✓（R32 修）：`"2026-10-13T12:00:00+00:00"` 在本机 CST
    是 20:00 而不是 12:00 —— 旧实现直接丢掉偏移，硬时钟会**晚 8 小时才全停**（无人值守
    多烧 8 小时配额 ✗✗），凭据到期同理。
    """
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s)
        return d.astimezone().replace(tzinfo=None) if d.tzinfo else d
    except Exception:                                                 # noqa: BLE001
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:                                             # noqa: BLE001
            continue
    raise ValueError(f"无法解析时间：{s!r}（支持 ISO 或 YYYY-MM-DD[ HH:MM[:SS]]）")


def _validate_argv(argv: object, who: str = "argv") -> List[str]:
    """入参体检：必须是**非空字符串列表**（拒绝字符串命令/嵌套/非字符串 ✗）。

    为什么还要这层：`shell=False` 已挡住 shell 注入 ✓，但把"字符串命令"误传进来
    会在某些平台上退化成 shell 行为 ✗ —— 本函数把这类误用挡在调用点之前 ✓。
    """
    if isinstance(argv, (str, bytes)):
        raise ValueError(f"{who} 必须是参数列表，不能是字符串（禁止 shell 命令 ✗）")
    if not isinstance(argv, (list, tuple)) or not argv:
        raise ValueError(f"{who} 必须是非空列表")
    out: List[str] = []
    for i, a in enumerate(argv):
        if not isinstance(a, str) or not a:
            raise ValueError(f"{who}[{i}] 必须是非空字符串，收到 {type(a).__name__}")
        out.append(a)
    return out


def _run_cmd(argv: Sequence[str], timeout: int = 120) -> Tuple[bool, str]:
    """执行自愈命令（参数列表 + `/usr/bin/env` 字面量首元素 ✓）。

    返回 (是否拉起来了, 原因)。**不等待完成**（自愈多为"拉起常驻进程" ✓），
    但会做 1 秒**秒退探测**：常驻型自愈命令不该立刻退出，若非零码秒退 = 拉起失败 ✗
    （实测形态：命令名写错 → `env: no such file` 退 127，只判 spawn 会假报"已恢复" ✗✗）。
    失败原因回传供账本记录 ✓（R31 审查：失败原因曾被双重吞掉 ✗）。
    """
    try:
        clean = _validate_argv(argv, "heal_cmd")
    except ValueError as e:
        return False, f"参数非法：{e}"
    try:
        p = subprocess.Popen(["/usr/bin/env", *clean], shell=False,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:                                            # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"
    try:
        rc = p.wait(timeout=HEAL_PROBE_WAIT_S)
    except subprocess.TimeoutExpired:
        return True, ""                                               # 还在跑 = 常驻进程起来了 ✓
    except Exception as e:                                            # noqa: BLE001
        return True, ""                                               # 探测本身失败不冤枉它 ✓
    if rc == 0:
        return True, ""                                               # 一次性自愈脚本正常收尾 ✓
    return False, f"自愈命令秒退，退出码 {rc}"


def _kill_group(p: "subprocess.Popen") -> None:
    """杀**整个进程组**（含孙子进程 ✓）并回收子进程。

    只 kill 直接子进程是不够的 ✗：步骤/探活命令常是脚本或桥（node .cjs → 浏览器），
    父进程死后孙子进程会继续跑、继续写数据——无人值守长跑里最隐蔽的一种"假停止" ✗✗。
    先 SIGTERM 整组给机会收尾，5 秒不退再 SIGKILL ✓。
    """
    try:
        pgid = os.getpgid(p.pid)
    except Exception:                                                 # noqa: BLE001
        pgid = None
    try:
        mine = os.getpgid(0)
    except Exception:                                                 # noqa: BLE001
        mine = None
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            if pgid is not None and pgid != mine:                     # 绝不误杀自己所在的组 ✓
                os.killpg(pgid, sig)
            else:
                p.kill()
        except ProcessLookupError:
            break                                                     # 已经没了 ✓
        except Exception:                                             # noqa: BLE001
            pass
        try:
            p.wait(timeout=5)
            return
        except Exception:                                             # noqa: BLE001
            continue
    try:
        p.kill()                                                      # 最后兜底 ✓
    except Exception:                                                 # noqa: BLE001
        pass


def _run_killgroup(argv: Sequence[str], timeout_s: int, stdout_target=None,
                   capture: bool = False, cwd: Optional[str] = None) -> Tuple[int, str, str]:
    """跑一个子进程；超时后**杀整个进程组**。返回 (退出码, 错误说明, 捕获输出)。

    为什么不用 `subprocess.run(timeout=)`：它只杀直接子进程 ✗（见 `_kill_group`）。
    本模块**所有** subprocess 调用都必须走这里或 `_run_cmd` ✓（`tests/static_checks.py` 有检查）。
    退出码 -9 = 超时、-1 = 起不来。
    """
    try:
        p = subprocess.Popen(["/usr/bin/env", *argv], shell=False, cwd=cwd,
                             start_new_session=True,                   # 自成进程组才能整组杀 ✓
                             stdout=(subprocess.PIPE if capture else stdout_target),
                             stderr=subprocess.STDOUT)
    except Exception as e:                                            # noqa: BLE001
        return -1, f"{type(e).__name__}: {e}", ""
    try:
        out, _ = p.communicate(timeout=max(1, int(timeout_s)))
        return p.returncode, "", (out.decode("utf-8", "ignore") if capture and out else "")
    except subprocess.TimeoutExpired:
        _kill_group(p)
        return -9, "TimeoutExpired", ""
    except BaseException as e:
        # ★ R32：KeyboardInterrupt / SystemExit 不是 Exception ✗ —— 旧实现让它们直接穿过去，
        # 步骤进程组（start_new_session=True，收不到给父进程的信号）继续跑继续写，
        # 而 run() 的 finally 已经**释放了单飞锁** → 立刻重启就是"双开写同一份数据" ✗✗
        # （实测：SIGTERM 后锁没了、旧步骤仍在写、重启的链报 done ✗）。
        # 这里先把整组杀干净再让异常继续上抛 ✓。
        _kill_group(p)
        raise e


def _http_probe(url: str, timeout: int = 4, expect: str = "") -> Tuple[bool, str]:
    """探活：curl 该 URL；expect 非空时要求响应体含该子串。返回 (是否活, 原因)。

    原因必须能落账 ✗：旧实现只回 bool，于是"curl 没装（env 127）"与"连接被拒（rc 7）"
    在所有可观测出口**完全同形**，运维分不清是网络还是工具缺失。
    """
    try:
        clean = _validate_argv(["curl", "-s", "--max-time", str(timeout), url], "probe_argv")
    except ValueError as e:
        return False, f"探活参数非法：{e}"
    code, err, body = _run_killgroup(clean, timeout + 4, capture=True)
    if err:
        return False, err
    if expect:
        return (expect in body), ("" if expect in body else f"响应体不含 {expect!r}")
    if code != 0:
        return False, f"curl 退出码 {code}"
    return bool(body.strip()), ("" if body.strip() else "响应体为空")


def http_probe(url: str, expect: str = "", heal_cmd: Sequence[str] = (), name: str = "") -> Probe:
    """便捷构造：HTTP 探活 + 可选自愈命令（如重拉浏览器 ✓）。"""
    def _check() -> Tuple[bool, str]:
        return _http_probe(url, expect=expect)

    def _heal() -> Tuple[bool, str]:
        if not heal_cmd:
            return False, "未提供自愈命令"
        ok, reason = _run_cmd(heal_cmd)
        if not ok:
            return False, reason                                      # 原因上浮 ✓（不再吞掉 ✗）
        for _ in range(30):                       # 轮询上限 90s（实测重拉浏览器约 30-45s ✓）
            time.sleep(3)
            alive, why = _check()
            if alive:
                return True, ""
            reason = why
        return False, f"自愈后 90s 内探活仍失败（{reason}）"

    return Probe(name=name or url, check=_check, heal=_heal)


@dataclass
class Step:
    """一个步骤：名称 + 命令 + 超时 + 软检查点。argv 必须是**字面量列表**（禁止拼接 shell 命令 ✗）。"""
    name: str
    argv: Sequence[str]
    timeout_s: int = 216000                     # 60h 上限；有 deadline 时会被自动收紧 ✓
    until: str = ""                             # 软检查点，如 "2026-10-13 11:30"
    fatal_codes: Tuple[int, ...] = (2,)         # 命中即链条全停（硬时钟/掉登录 ✓）
    soft_codes: Tuple[int, ...] = (3,)          # 命中即进下一级（到点收工 ✓）
    required: bool = True                       # 失败是否中断链条（默认中断 ✓）


@dataclass
class Probe:
    """依赖探针：check() 探活，heal() 自愈（都不得抛异常 ✗）。"""
    name: str
    check: Callable[[], bool]
    heal: Callable[[], bool] = field(default=lambda: False)
    heal_wait_s: int = 5


class Chain:
    """自愈链条。进程级单飞：同一 state_key 只允许一条在跑（防双开烧配额 ✗）。"""

    def __init__(self, steps: Sequence[Step], probes: Sequence[Probe] = (),
                 log_path: Optional[Path] = None, journal_path: Optional[Path] = None,
                 lock_path: Optional[Path] = None, state_key: str = "default",
                 death_patterns: Sequence[str] = DEFAULT_DEATH_PATTERNS,
                 max_heal_retries: int = 1, cwd: Optional[str] = None,
                 deadline: str = "") -> None:
        if not steps:
            raise ValueError("steps 不能为空")
        for st in steps:
            _validate_argv(st.argv, f"步骤 {st.name} 的 argv")
        self.steps = list(steps)
        self.probes = list(probes)
        self.state_key = state_key
        self.state_dir_error = ""
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
        except Exception as e:                                        # noqa: BLE001
            # 不抛（承诺"不抛异常，返回结构化结果" ✓）：账本/日志写不进去会被计数上浮，
            # 最终由 run() 落成非 done 状态 ✗（不静默 ✗）
            self.state_dir_error = f"{type(e).__name__}: {e}"
            self._stderr(f"⚠ 状态目录不可用：{self.state_dir_error}（账本/日志可能写不进去 ✗）")
        self.log_path = Path(log_path) if log_path else (STATE_DIR / f"{state_key}.log")
        self.journal_path = Path(journal_path) if journal_path else \
            (STATE_DIR / f"{state_key}.journal.jsonl")
        self.lock_path = Path(lock_path) if lock_path else (STATE_DIR / f"{state_key}.lock")
        self.death_patterns = tuple(death_patterns)
        self.max_heal_retries = max(0, int(max_heal_retries))
        self.cwd = cwd
        self.deadline = _parse_dt(deadline)
        self.journal_errors = 0
        self.log_errors = 0
        self._exec_last_error = ""

    # ---------- 账本 ----------
    def _journal(self, row: Dict[str, object]) -> None:
        row = {"at": _now().isoformat(timespec="seconds"), **row}
        try:
            self.journal_path.parent.mkdir(parents=True, exist_ok=True)
            with self.journal_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception as e:                                        # noqa: BLE001
            self.journal_errors += 1                                  # 计数并上浮 ✓（不静默 ✗）
            self._stderr(f"⚠ 账本写入失败(#{self.journal_errors})：{type(e).__name__}: {e}")

    def _log(self, msg: str) -> None:
        line = f"{_now():%Y-%m-%d %H:%M:%S} {msg}"
        self._stderr(line)
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as e:                                        # noqa: BLE001
            self.log_errors += 1                                      # 日志=判死输入，写坏必须可见 ✓
            self._stderr(f"⚠ 链条日志写入失败(#{self.log_errors})：{type(e).__name__}: {e}")

    @staticmethod
    def _stderr(msg: str) -> None:
        """写到**真 stderr** ✓（R32 修：旧实现名叫 `_stderr` 却 `print` 到 stdout ✗ ——
        链条日志会混进 payload JSON，让 `chain ... > x.json` 出来的不是合法 JSON ✗✗）。"""
        try:
            sys.stderr.write(msg + "\n")
            sys.stderr.flush()
        except Exception:                                             # noqa: BLE001
            pass

    def _log_size(self) -> Optional[int]:
        """当前日志字节数（判死水位 ✓）；不可读返回 None。"""
        try:
            return self.log_path.stat().st_size
        except FileNotFoundError:
            return 0
        except Exception:                                             # noqa: BLE001
            return None

    # ---------- 单飞锁（原子占锁 ✓） ----------
    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if not isinstance(pid, int) or pid <= 0:                      # pid<=0 一律按"非有效锁"✓
            return False
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True                                               # 存在但无权限 → 保守视为活 ✓
        except Exception:                                             # noqa: BLE001
            return True                                               # 不确定 → 宁拒跑不双开 ✓

    def _acquire_lock(self) -> Tuple[bool, str]:
        """单飞锁：**内核级 `flock`** ✓。返回 (是否拿到, 说明)。

        R32 修：旧实现"读 pid → 判活 → 删旧锁"是 **TOCTOU** ✗✗ —— 两个进程可同时把同一份
        陈旧锁判为"死 pid"，于是互删对方**刚建**的锁，各自都认为持锁（实测 N=12 时 7~9 个
        同时持锁；N=2 的 cron+手工重跑场景 20 次里 1 次双双持锁 ✗）。
        `flock` 由内核保证互斥、进程退出（含被 `kill -9`）自动释放 ✓，**不再依赖 pid 是否存活**
        （顺带消灭"pid 复用导致永久锁死"✗）；锁文件内容只作诊断 ✓。
        **绝不 unlink 锁文件** ✓：删掉会换 inode，别人新开的 fd 锁的是另一个对象 ✗。
        """
        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        except Exception as e:                                        # noqa: BLE001
            reason = f"锁不可用（{type(e).__name__}: {e}）"            # 与"有人在跑"严格区分 ✓
            self._stderr(f"⚠ {reason}")
            return False, reason
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            holder = ""
            try:
                info = json.loads(self.lock_path.read_text(encoding="utf-8") or "{}")
                pid = info.get("pid")
                alive = self._pid_alive(pid) if isinstance(pid, int) else False
                holder = f"（持有者 pid={pid}，进程存活：{'是' if alive else '否'}）"
            except Exception:                                         # noqa: BLE001
                pass
            try:
                os.close(fd)
            except Exception:                                         # noqa: BLE001
                pass
            return False, f"已被同名单链持有{holder}"
        self._lock_fd = fd
        try:                                                          # 诊断信息，写不进去不影响互斥 ✓
            os.ftruncate(fd, 0)
            os.write(fd, json.dumps({"pid": os.getpid(),
                                     "at": _now().isoformat(timespec="seconds")}).encode())
        except Exception:                                             # noqa: BLE001
            pass
        return True, ""

    def _release_lock(self) -> None:
        fd = getattr(self, "_lock_fd", None)
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception as e:                                        # noqa: BLE001
            self._stderr(f"⚠ 解锁失败（进程退出时内核会兜底释放 ✓）：{type(e).__name__}: {e}")
        try:
            os.close(fd)
        except Exception as e:                                        # noqa: BLE001
            self._stderr(f"⚠ 关闭锁文件描述符失败：{type(e).__name__}: {e}")
        self._lock_fd = None

    # ---------- 死亡判定（按水位**流式**扫本次新增 ✓） ----------
    def tail_death_hits(self, offset: Optional[int] = None) -> Tuple[Optional[int], Dict[str, int]]:
        """扫日志**本次尝试新增字节**里的死亡特征计数。

        返回 (命中数 | None, 明细)：**None = 判死器不可用**（读不到日志）——
        调用方必须按失败处理 ✓（旧实现 fail-open 返回 0 = "健康" ✗✗）。

        **口径（R32 修）**：从水位 `offset` 起**逐行流式**读到末尾——全程覆盖、无盲区 ✓。
        曾经只读"头 6KB + 尾 6KB" ✗✗：实测"8KB 正常 + 40 次 TargetClosedError + 30KB 正常"
        会被数成 0 命中 → 报 `done` ✗（静默失败原封不动地回来了）；头尾两段在 6~12KB
        档位还会**重叠**把同一批命中数两遍 → 假判死 ✗。逐行扫同时消灭这两个问题
        （行是日志的自然单位 ✓ 无跨块缝 ✗ 无重复计数 ✗）。
        **不做"够阈值就提前收工"** ✓：`death_hits` 是运维用来判断"这次死了多少款"的观测值，
        截断成 10 会让账本失真 ✗（代价=读一遍本次尝试的输出；相对步骤自身的运行时长可忽略 ✓）。
        """
        try:
            size = self.log_path.stat().st_size
        except FileNotFoundError:
            if offset:                                                # 曾有内容却没了 = 异常 ✗
                self._journal({"event": "judge_error", "why": "log_vanished", "offset": offset})
                return None, {}                                       # 不可信 ≠ 健康 ✓
            return 0, {}                                              # 从未存在过 → 无命中 ✓
        except Exception as e:                                        # noqa: BLE001
            self._journal({"event": "judge_error", "error": f"{type(e).__name__}: {e}"})
            return None, {}                                           # 未知 ≠ 健康 ✓
        start = max(0, min(offset, size)) if offset is not None else 0
        hits: Dict[str, int] = {p: 0 for p in self.death_patterns}
        total = 0
        try:
            with self.log_path.open("rb") as f:
                f.seek(start)
                while True:
                    raw = f.readline(MAX_LOG_LINE_BYTES)               # 有界：病态超长行按块切 ✓
                    if not raw:
                        break
                    line = raw.decode("utf-8", "ignore")
                    for p in self.death_patterns:
                        c = line.count(p)
                        if c:
                            hits[p] += c
                            total += c
        except Exception as e:                                        # noqa: BLE001
            self._journal({"event": "judge_error", "error": f"{type(e).__name__}: {e}"})
            return None, {}                                           # 读一半失败 = 未知 ≠ 健康 ✓
        return total, {k: v for k, v in hits.items() if v}

    # ---------- 探针 ----------
    def _segment_has(self, marker: str, offset: Optional[int]) -> bool:
        """本次尝试新增的日志里是否出现某标记（**有界**逐行扫，命中即停 ✓）。

        用途：把"步骤不认 `--until`"（argparse 退 2）与真正的致命码区分开 ✗✓ ——
        否则运维会按"硬时钟/掉登录"去查一个纯配置错误（R32 审查抓出）。
        """
        if not marker:
            return False
        try:
            size = self.log_path.stat().st_size
            start = max(0, min(offset, size)) if offset is not None else 0
            with self.log_path.open("rb") as f:
                f.seek(start)
                while True:
                    raw = f.readline(MAX_LOG_LINE_BYTES)
                    if not raw:
                        return False
                    if marker in raw.decode("utf-8", "ignore"):
                        return True
        except Exception:                                             # noqa: BLE001
            return False                                              # 判不出来就当没有（不误报 ✓）

    @staticmethod
    def _verdict(res: object) -> Tuple[bool, str]:
        """探针返回值归一：允许 `bool` 或 `(bool, 原因)` ✓（原因要能落到账本 ✗）。"""
        if isinstance(res, tuple):
            ok = bool(res[0]) if res else False
            return ok, (str(res[1]) if len(res) > 1 and res[1] else "")
        return bool(res), ""

    def _probe_all(self) -> bool:
        """全部探针探活；不活则依次自愈**并强制复验**。返回最终是否全部可用。

        R32 修两处：① 自愈失败原因必须落账（旧实现 `ok, _ = _run_cmd(...)` 把原因丢了 ✗）；
        ② 自愈返回 True 后**必须再 `check()` 复验** ✓✗ —— 通用 `Probe` 的 heal() 只说"我拉起了"，
        不等于依赖真活了；旧实现直接打"✓ 已恢复"，于是带着死依赖空转、步骤自己吞掉失败退 0
        → 链条全绿 ✗✗（`http_probe` 内部有轮询，通用 Probe 没有）。
        """
        for p in self.probes:
            reason = ""
            try:
                ok, reason = self._verdict(p.check())
            except Exception as e:                                    # noqa: BLE001
                ok, reason = False, f"{type(e).__name__}: {e}"
            if ok:
                continue
            self._log(f"⚠ 探针 {p.name} 不可用{('：' + reason) if reason else ''} → 自愈")
            healed, heal_reason = False, ""
            try:
                healed, heal_reason = self._verdict(p.heal())
            except Exception as e:                                    # noqa: BLE001
                healed, heal_reason = False, f"{type(e).__name__}: {e}"
            if healed:
                try:
                    healed, heal_reason = self._verdict(p.check())    # ★ 强制复验 ✓
                except Exception as e:                                # noqa: BLE001
                    healed, heal_reason = False, f"复验异常：{type(e).__name__}: {e}"
            self._journal({"event": "heal", "probe": p.name, "ok": healed,
                           "reason": heal_reason or reason})
            if not healed:
                self._log(f"✗ 探针 {p.name} 自愈失败"
                          f"{('：' + heal_reason) if heal_reason else ''}")
                return False
            self._log(f"✓ 探针 {p.name} 已恢复（复验通过）")
            time.sleep(max(0, self._int_or(p.heal_wait_s, 5)))
        return True

    @staticmethod
    def _int_or(v: object, default: int) -> int:
        """安全取整（`heal_wait_s=None` 曾让 run() 抛 TypeError ✗）。"""
        try:
            return int(v)                                             # type: ignore[arg-type]
        except Exception:                                             # noqa: BLE001
            return default

    # ---------- 执行 ----------
    def _exec_step(self, argv: Sequence[str], timeout_s: int) -> int:
        """跑一步（参数列表 + `/usr/bin/env` 字面量首元素 + shell=False ✓；输出并入链条日志 ✓；
        超时**杀整个进程组** ✓）。"""
        self._exec_last_error = ""
        try:
            clean = _validate_argv(argv, "step argv")
        except ValueError as e:
            self._exec_last_error = f"参数非法：{e}"
            self._log(f"✗ 步骤参数非法：{e}")
            return -1
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as lf:
                code, err, _ = _run_killgroup(clean, timeout_s, stdout_target=lf, cwd=self.cwd)
            if err:
                self._exec_last_error = err
            return code
        except Exception as e:                                        # noqa: BLE001
            self._exec_last_error = f"{type(e).__name__}: {e}"
            return -1

    def _effective_timeout(self, st: Step) -> Tuple[int, bool]:
        """有 deadline 时收紧本步超时（避免"硬时钟拦不住已启动的步骤" ✗）。
        返回 (秒, 是否已无可用时间)。"""
        floor = max(1, int(MIN_STEP_TIMEOUT_S))                       # 测试可下调（见常量注释 ✓）
        if not self.deadline:
            return max(floor, int(st.timeout_s)), False
        remaining = (self.deadline - _now()).total_seconds()
        if remaining <= floor:
            return floor, True
        return max(floor, min(int(st.timeout_s), int(remaining))), False

    def run(self) -> Dict[str, object]:
        got_lock, lock_reason = self._acquire_lock()
        if not got_lock:
            self._log(f"⛔ 未取得单飞锁：{lock_reason}")
            self._journal({"event": "locked", "state_key": self.state_key, "reason": lock_reason})
            return {"status": "locked", "reason": lock_reason, "steps": [],
                    "state_dir_error": self.state_dir_error}
        results: List[Dict[str, object]] = []
        status = "done"
        skipped: List[str] = []
        soft_stops: List[str] = []
        try:
            for st in self.steps:
                if self.deadline and _now() >= self.deadline:
                    self._log(f"⛔ 到硬时钟 {self.deadline:%Y-%m-%d %H:%M}，链条全停")
                    self._journal({"event": "deadline", "step": st.name})
                    status = "deadline"
                    break
                if not self._probe_all():
                    self._log(f"✗ 步骤 {st.name} 前置探针不可用且自愈失败 → 全停")
                    self._journal({"event": "probe_fail", "step": st.name})
                    status = "probe_fail"
                    break
                attempts: List[Dict[str, object]] = []
                outcome = "done"
                stop_chain = False
                for attempt in range(1, 2 + self.max_heal_retries):
                    argv = list(st.argv)
                    if st.until:
                        argv += ["--until", st.until]                 # 重跑同样带检查点 ✓
                    self._log(f"==== {st.name} 第{attempt}次 @ {_now():%m-%d %H:%M:%S} ====")
                    offset = self._log_size()                         # 判死水位：只数本次新增 ✓
                    if offset is None:
                        # 拿不到水位 = 无法把"本次"与"前序"分开 → 判死口径不可信 → fail-closed ✓✗
                        self._log(f"✗ {st.name}：日志不可 stat（拿不到判死水位）→ 判死器不可用")
                        self._journal({"event": "judge_unavailable", "step": st.name,
                                       "why": "log_size_unavailable"})
                        outcome, stop_chain = "judge_unavailable", True
                        break
                    t0 = _now()
                    timeout_s, no_time = self._effective_timeout(st)
                    if no_time:
                        self._log(f"⛔ {st.name}：已无可用时间（硬时钟在即），不启动该步")
                        self._journal({"event": "deadline", "step": st.name, "attempt": attempt})
                        outcome, stop_chain = "deadline", True
                        break
                    code = self._exec_step(argv, timeout_s)
                    hits, detail = self.tail_death_hits(offset=offset)
                    row = {"event": "attempt", "step": st.name, "attempt": attempt, "code": code,
                           "death_hits": hits, "death_detail": detail,
                           "exec_error": self._exec_last_error,
                           "secs": int((_now() - t0).total_seconds())}
                    attempts.append(row)
                    self._journal(row)
                    self._log(f"==== {st.name} 退出码 {code}；本次死亡命中 "
                              f"{'不可读' if hits is None else hits} ====")
                    if code in st.fatal_codes:
                        if self._segment_has(UNTIL_UNSUPPORTED_MARKER, offset):
                            # 把"步骤不认 --until"误报成"硬时钟/掉登录"会让运维查错方向 ✗✗
                            self._log(f"✗ {st.name}：致命码 {code}，但日志显示步骤**不认 --until**"
                                      f"（{UNTIL_UNSUPPORTED_MARKER}）→ 请去掉该步的软检查点")
                            self._journal({"event": "until_unsupported", "step": st.name})
                            outcome, stop_chain = "until_unsupported", True
                            break
                        outcome, stop_chain = "fatal", True
                        break
                    if code in st.soft_codes:
                        outcome = "soft"                              # 软检查点 ✓ 进下一级
                        soft_stops.append(st.name)                    # 提前收工要可见 ✓（见 H5）
                        break
                    if hits is None:                                  # 判死器不可用 → 绝不判完成 ✗✓
                        outcome, stop_chain = "judge_unavailable", True
                        break
                    if code == 0 and hits < DEATH_HITS_THRESHOLD:
                        outcome = "done"                              # 真完成 ✓
                        break
                    if attempt <= self.max_heal_retries:              # 判死 → 探针自愈 + 重跑 ✓
                        self._log(f"⚠ {st.name}：退出码 {code}、本次死亡命中 {hits} "
                                  f"→ 判死{'（先自愈）' if self.probes else '并重跑'} ✓")
                        if self.probes and not self._probe_all():
                            self._log("✗ 自愈失败 → 全停")
                            self._journal({"event": "probe_fail", "step": st.name,
                                           "attempt": attempt})
                            outcome, stop_chain = "probe_fail", True
                            break
                        continue
                    # 重跑次数用尽仍不干净：判失败（旧实现此处静默落回 done ✗✗）
                    outcome, stop_chain = ("death_persists" if code == 0 else "step_failed"), True
                    break
                results.append({"step": st.name, "attempts": attempts, "outcome": outcome,
                                "last_code": attempts[-1]["code"] if attempts else None})
                self._journal({"event": outcome, "step": st.name,
                               "attempts": len(attempts)})
                failed = outcome not in ("done", "soft")
                if failed and not st.required and outcome not in ENV_LEVEL_OUTCOMES:
                    # `required=False` = 可选步骤：**仅步骤自身失败**（step_failed/death_persists）
                    # 才允许降级继续，整体记 partial ✗✓。
                    # 环境级失败（fatal 致命码 / deadline 硬时钟 / probe_fail 依赖已死 /
                    # judge_unavailable 判死器失明）**对可选步骤也必须全停** ✗✗ ——
                    # 继续跑 = 继续烧配额、继续在不可审计状态下采集（R32 审查抓出 ✗）。
                    skipped.append(f"{st.name}:{outcome}")
                    self._log(f"⚠ 非必需步骤 {st.name} 失败（{outcome}）→ 继续下一级，"
                              f"整体记为 partial ✗")
                    continue
                if failed or stop_chain:
                    status = outcome
                    break
            if status == "done" and skipped:
                status = "partial"                                    # 有可选步骤失败 → 非全绿 ✓
            payload: Dict[str, object] = {
                "status": status, "steps": results, "skipped_steps": skipped,
                "soft_stops": soft_stops,
                "journal": str(self.journal_path), "log": str(self.log_path),
                "journal_errors": self.journal_errors, "log_errors": self.log_errors}
            if self.state_dir_error:
                payload["state_dir_error"] = self.state_dir_error
            if status == "done" and self.journal_errors:
                payload["status"] = "ledger_broken"               # 账本都写不进去 → 不可审计 ✓
            elif status == "done" and self.log_errors:
                # 日志是**判死输入**：写不进去 = 判死失效（且审计轨迹丢失）→ 不得报完成 ✓✓
                payload["status"] = "log_broken"
            return payload
        finally:
            self._release_lock()


def parse_step_spec(spec: str) -> Step:
    """CLI 用：`名称=命令` → Step（命令按 shlex 切分，产出**列表** ✓ 不经 shell ✓）。"""
    if "=" not in spec:
        raise ValueError(f"步骤格式应为 名称=命令，收到 {spec!r}")
    name, cmd = spec.split("=", 1)
    argv = shlex.split(cmd.strip())
    if not argv:
        raise ValueError(f"步骤 {name} 的命令为空")
    return Step(name=name.strip(), argv=argv)


def build_parser():
    """CLI 参数定义（唯一来源 ✓ —— 供本模块与 cli.py 复用，避免两处漂移 ✗）。"""
    import argparse
    ap = argparse.ArgumentParser(prog="chain", description="自愈链条（步骤 + 探针 + 按日志内容判死 ✓）")
    ap.add_argument("--step", action="append", required=True, metavar="名称=命令",
                    help="可多次；命令按 shlex 切分为参数列表（不经 shell ✓）")
    ap.add_argument("--until", default="", help="软检查点（传给各步骤的 --until，需步骤支持 ✓）")
    ap.add_argument("--probe-http", action="append", default=[], metavar="URL",
                    help="HTTP 探活 URL（可多次，如 http://127.0.0.1:9222/json/version）")
    ap.add_argument("--heal-cmd", default="", help="探针自愈命令（配合 --probe-http ✓）")
    ap.add_argument("--deadline", default="", help="硬时钟，如 2026-10-13 12:00")
    ap.add_argument("--state-key", default="default", help="单飞锁与账本命名")
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI 入口：`python3 -m universal_scraper.cli chain --step "R2=python3 r2.py" ...`"""
    ap = build_parser()
    args = ap.parse_args(argv)
    try:                                                              # 参数错误要**干净报错** ✓
        steps = [parse_step_spec(s) for s in args.step]                # （裸栈 = 运维看不懂 ✗）
    except ValueError as e:
        ap.error(f"--step 格式错误：{e}")
        return 2                                                      # pragma: no cover（error 已退出）
    if args.deadline:
        try:
            _parse_dt(args.deadline)                                   # 非法时间给干净报错 ✓（M3）
        except ValueError as e:
            ap.error(f"--deadline 无法解析：{e}")
            return 2                                                  # pragma: no cover
    if args.until:
        for st in steps:
            st.until = args.until
    probes = []
    heal_cmd = shlex.split(args.heal_cmd) if args.heal_cmd else []
    for url in args.probe_http:
        probes.append(http_probe(url, expect="", heal_cmd=heal_cmd))
    ch = Chain(steps=steps, probes=probes, state_key=args.state_key, deadline=args.deadline)
    res = ch.run()
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0 if res.get("status") == "done" else 2


if __name__ == "__main__":
    raise SystemExit(main())
