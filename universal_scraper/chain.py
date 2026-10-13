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
包括：非约定退出码、判死后重跑仍死、判死器不可用、自愈失败、账本写坏。失败步骤默认**中断链条** ✓。

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

import json
import os
import shlex
import subprocess
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
TAIL_BYTES = 6000
DEATH_HITS_THRESHOLD = 10
HEAL_PROBE_WAIT_S = 1.0       # 自愈命令"秒退探测"窗口 ✓（见 _run_cmd）


def _now() -> datetime:
    return datetime.now()


def _parse_dt(s: str) -> Optional[datetime]:
    """时间解析：先试 `fromisoformat`（吃 T 分隔/微秒/时区 ✓），再回落到格式表。"""
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s)
        return d.replace(tzinfo=None) if d.tzinfo else d
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


def _http_probe(url: str, timeout: int = 4, expect: str = "") -> bool:
    """探活：curl 该 URL；expect 非空时要求响应体含该子串。"""
    try:
        clean = _validate_argv(["curl", "-s", "--max-time", str(timeout), url], "probe_argv")
        r = subprocess.run(["/usr/bin/env", *clean], shell=False, capture_output=True,
                           text=True, timeout=timeout + 4)
        body = r.stdout or ""
        return (expect in body) if expect else (r.returncode == 0 and bool(body.strip()))
    except Exception:                                                 # noqa: BLE001
        return False


def http_probe(url: str, expect: str = "", heal_cmd: Sequence[str] = (), name: str = "") -> Probe:
    """便捷构造：HTTP 探活 + 可选自愈命令（如重拉浏览器 ✓）。"""
    def _check() -> bool:
        return _http_probe(url, expect=expect)

    def _heal() -> bool:
        if not heal_cmd:
            return False
        ok, _ = _run_cmd(heal_cmd)
        if not ok:
            return False
        for _ in range(30):                       # 轮询上限 90s（实测重拉浏览器约 30-45s ✓）
            time.sleep(3)
            if _check():
                return True
        return False

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
        STATE_DIR.mkdir(parents=True, exist_ok=True)
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
        try:
            print(msg, flush=True)
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

    def _acquire_lock(self) -> bool:
        for _ in range(2):
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(json.dumps({"pid": os.getpid(), "at": _now().isoformat()}))
                return True
            except FileExistsError:
                try:
                    old = json.loads(self.lock_path.read_text(encoding="utf-8"))
                    pid = old.get("pid")
                except Exception:                                     # noqa: BLE001
                    pid = None                                        # 坏锁按 stale 处理 ✓
                if self._pid_alive(pid if isinstance(pid, int) else -1):
                    return False
                try:
                    self.lock_path.unlink()                           # stale → 清掉重试一次 ✓
                except Exception:                                     # noqa: BLE001
                    return False
            except Exception as e:                                    # noqa: BLE001
                self._stderr(f"⚠ 占锁失败：{type(e).__name__}: {e}")
                return False
        return False

    def _release_lock(self) -> None:
        try:
            if self.lock_path.exists():
                cur = json.loads(self.lock_path.read_text(encoding="utf-8"))
                if int(cur.get("pid") or -1) == os.getpid():          # 只删自己的锁 ✓
                    self.lock_path.unlink()
        except Exception:                                             # noqa: BLE001
            pass

    # ---------- 死亡判定（按水位扫本次新增 ✓） ----------
    def tail_death_hits(self, offset: Optional[int] = None) -> Tuple[Optional[int], Dict[str, int]]:
        """扫日志**本次尝试新增字节**里的死亡特征计数。

        返回 (命中数 | None, 明细)：**None = 判死器不可用**（读不到日志）——
        调用方必须按失败处理 ✓（旧实现 fail-open 返回 0 = "健康" ✗✗）。
        """
        try:
            size = self.log_path.stat().st_size
            start = max(0, size - TAIL_BYTES)
            if offset is not None:
                start = max(start, min(offset, size))
            with self.log_path.open("rb") as f:
                f.seek(start)
                tail = f.read().decode("utf-8", "ignore")
        except FileNotFoundError:
            return 0, {}                                              # 从未存在过 → 无命中 ✓
        except Exception as e:                                        # noqa: BLE001
            self._journal({"event": "judge_error", "error": f"{type(e).__name__}: {e}"})
            return None, {}                                           # 未知 ≠ 健康 ✓
        hits = {p: tail.count(p) for p in self.death_patterns}
        return sum(hits.values()), {k: v for k, v in hits.items() if v}

    # ---------- 探针 ----------
    def _probe_all(self) -> bool:
        """全部探针探活；不活则依次自愈。返回最终是否全部可用。"""
        for p in self.probes:
            try:
                ok = bool(p.check())
            except Exception as e:                                    # noqa: BLE001
                self._journal({"event": "probe_error", "probe": p.name,
                               "error": f"{type(e).__name__}: {e}"})
                ok = False
            if ok:
                continue
            self._log(f"⚠ 探针 {p.name} 不可用 → 自愈")
            healed, reason = False, ""
            try:
                healed = bool(p.heal())
            except Exception as e:                                    # noqa: BLE001
                healed, reason = False, f"{type(e).__name__}: {e}"
            self._journal({"event": "heal", "probe": p.name, "ok": healed, "reason": reason})
            if not healed:
                self._log(f"✗ 探针 {p.name} 自愈失败{('：' + reason) if reason else ''}")
                return False
            self._log(f"✓ 探针 {p.name} 已恢复")
            time.sleep(max(0, int(p.heal_wait_s)))
        return True

    # ---------- 执行 ----------
    def _exec_step(self, argv: Sequence[str], timeout_s: int) -> int:
        """跑一步（参数列表 + `/usr/bin/env` 字面量首元素 + shell=False ✓；输出并入链条日志 ✓）。"""
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
                return subprocess.run(["/usr/bin/env", *clean], shell=False, cwd=self.cwd,
                                      stdout=lf, stderr=subprocess.STDOUT,
                                      timeout=max(60, int(timeout_s))).returncode
        except subprocess.TimeoutExpired:
            self._exec_last_error = "TimeoutExpired"
            return -9
        except Exception as e:                                        # noqa: BLE001
            self._exec_last_error = f"{type(e).__name__}: {e}"
            return -1

    def _effective_timeout(self, st: Step) -> Tuple[int, bool]:
        """有 deadline 时收紧本步超时（避免"硬时钟拦不住已启动的步骤" ✗）。
        返回 (秒, 是否已无可用时间)。"""
        if not self.deadline:
            return max(60, int(st.timeout_s)), False
        remaining = (self.deadline - _now()).total_seconds()
        if remaining <= 60:
            return 60, True
        return max(60, min(int(st.timeout_s), int(remaining))), False

    def run(self) -> Dict[str, object]:
        if not self._acquire_lock():
            self._log("⛔ 已有同名单链在跑（单飞锁 ✓），本次退出")
            self._journal({"event": "locked", "state_key": self.state_key})
            return {"status": "locked", "steps": []}
        results: List[Dict[str, object]] = []
        status = "done"
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
                        outcome, stop_chain = "fatal", True
                        break
                    if code in st.soft_codes:
                        outcome = "soft"                              # 软检查点 ✓ 进下一级
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
                if stop_chain or (st.required and outcome not in ("done", "soft")):
                    status = outcome
                    break
            payload: Dict[str, object] = {
                "status": status, "steps": results,
                "journal": str(self.journal_path), "log": str(self.log_path),
                "journal_errors": self.journal_errors, "log_errors": self.log_errors}
            if self.journal_errors and status == "done":
                payload["status"] = "ledger_broken"               # 账本都写不进去 → 不可审计 ✓
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
    args = build_parser().parse_args(argv)
    steps = [parse_step_spec(s) for s in args.step]
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
