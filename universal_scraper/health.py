"""🩺 源健康台账（`health` 子命令）——把「静默失败」变成会报警的账本。

**战场教训**（miit 课题阶段 R，2026-10-11→13）：三类事故**都表现为"0 条/无变化"** ✗✗
—— 配额耗尽（下载面板 50/天，第 50 款起静默失败 ✗）、页面改版、浏览器崩溃；
人工盯日志才发现 ✗ → 本模块把三条硬规则代码化：

1. **空结果 ≠ 正常** ✓✓：`count == 0` 默认判 `empty_suspicious`（除非该源显式 `allow_empty=True`）；
2. **量级突变告警**：`count < drop × 中位` 或 `> spike × 中位` → 告警（默认 0.5× / 2.0×）；
3. **凭据到期日历**：VIP/Token 到期前 N 天提醒 —— 七麦那次就是**临到期才发现** ✗。

外加：**连续失败计数**（最近 3 笔非 ok → `failing`，且**本次状态非 ok 立即判失败** ✓）与
**陈旧检测**（`last_at` 超过该源登记间隔的 2 倍 → `stale`；时间不可解析 → `stale_unknown` ✓）。

**出口语义（R31 审查修复）**：判定词表按**白名单**映射退出码 —— `check` 只有 `ok` / `no_baseline`
返回 0，其余（含 `volume_drop` / `volume_spike` / `stale` / `failing` / `empty_suspicious` /
`ledger_unreadable`）一律返回 1 ✓；`report` 只要报告里出现**程序化收集到的告警条目**
（源告警 / 凭据临期或过期 / 账本不可读 / 凭据条目异常）就返回 1 ✓
（曾用字符串匹配判退出码 ✗ —— 源名里带"不可读"会误报，且完全没看源告警 ✗✗）；
未来新增告警词默认落在"要人工看"一侧 ✓。

存储：`~/.universal_scraper/health/{ledger.jsonl, state.json}`；**state.json 损坏按隔离重命名** ✓、
**账本整体不可读 → 显式 `ledger_unreadable`** ✓（不假装"没有历史" ✗）、坏行跳过 ✓。

用法（CLI，注意本仓库**没有** `us` 可执行文件，须走模块入口 ✓）：
    PYTHONPATH=<插件目录> python3 -m universal_scraper.cli health register qimai_offline --interval-h 24
    PYTHONPATH=<插件目录> python3 -m universal_scraper.cli health check qimai_offline --count 2293
    PYTHONPATH=<插件目录> python3 -m universal_scraper.cli health record qimai_offline --count 2293
    PYTHONPATH=<插件目录> python3 -m universal_scraper.cli health cred qimai_vip --expires "2026-10-13 14:05"
    PYTHONPATH=<插件目录> python3 -m universal_scraper.cli health report
"""
from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

STATE_DIR = Path.home() / ".universal_scraper" / "health"
LEDGER_PATH = STATE_DIR / "ledger.jsonl"
STATE_PATH = STATE_DIR / "state.json"

# 判定词表 → 是否"要人工看"（白名单：只有这两类是安心值 ✓，其余一律告警 ✓）
OK_VERDICTS = frozenset({"ok", "no_baseline"})
ALARM_VERDICTS = frozenset({"empty_suspicious", "volume_drop", "volume_spike", "stale",
                            "failing", "ledger_unreadable", "stale_unknown", "no_count"})


def _now() -> datetime:
    return datetime.now()


def _stderr(msg: str) -> None:
    try:
        print(msg, flush=True)
    except Exception:                                                 # noqa: BLE001
        pass


def _parse_dt(s: str) -> Optional[datetime]:
    """时间解析：先 `fromisoformat`（吃 T 分隔/微秒/时区 ✓ —— 账本写的就是这种 ✓），
    再回落到格式表；仍失败抛 ValueError（调用方决定降级语义 ✓）。"""
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


def _as_int(v: Any) -> Optional[int]:
    """安全取整：bool/None/非数字字符串一律返回 None（**不抛** ✗✓ —— 账本里可能混入脏值）。"""
    if isinstance(v, bool) or v is None:
        return None
    try:
        return int(v)
    except Exception:                                                 # noqa: BLE001
        return None


def _load_state(path: Optional[Path] = None) -> Dict[str, Any]:
    """读 state；损坏 → 隔离重命名（并告知 ✓）后返回空档案。结构异常按字段归一 ✓。"""
    p = Path(path) if path else STATE_PATH
    if not p.exists():
        return {"sources": {}, "credentials": []}
    recovered = None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(d, dict):
            raise ValueError("state 不是对象")
    except Exception as e:                                            # noqa: BLE001
        try:
            bad = p.with_suffix(f".corrupt.{int(_now().timestamp())}.json")
            p.rename(bad)
            recovered = str(bad)
        except Exception:                                             # noqa: BLE001
            pass
        _stderr(f"⚠ state 损坏（{type(e).__name__}）→ 已隔离重建{('：' + recovered) if recovered else ''}")
        return {"sources": {}, "credentials": [], "_recovered_from": recovered}
    if not isinstance(d.get("sources"), dict):                        # 合法 JSON 但结构坏 ✓
        d["sources"] = {}
    if not isinstance(d.get("credentials"), list):
        d["credentials"] = []
    return d


def _save_state(state: Dict[str, Any], path: Optional[Path] = None) -> bool:
    """写 state；**返回是否成功** ✓（旧实现吞异常而调用方无条件 ok:True ✗✗）。"""
    p = Path(path) if path else STATE_PATH
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        return True
    except Exception as e:                                            # noqa: BLE001
        _stderr(f"⚠ state 写入失败：{type(e).__name__}: {e}")
        return False


def _read_ledger(path: Optional[Path] = None, limit: int = 5000) -> Optional[List[Dict[str, Any]]]:
    """读账本：**整体不可读 → None**（与"空账本"严格区分 ✓✗）；坏行跳过 ✓。"""
    p = Path(path) if path else LEDGER_PATH
    if not p.exists():
        return []
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except Exception as e:                                            # noqa: BLE001
        _stderr(f"⚠ 账本不可读：{type(e).__name__}: {e}（判定将按 ledger_unreadable 处理 ✓）")
        return None
    rows: List[Dict[str, Any]] = []
    for l in lines[-limit:]:
        try:
            r = json.loads(l)
            if isinstance(r, dict) and r.get("source"):
                rows.append(r)
        except Exception:                                             # noqa: BLE001
            continue                                                  # 坏行跳过（append 崩溃的正常残留 ✓）
    return rows


def register(source: str, expect_interval_h: Optional[float] = None,
             allow_empty: bool = False, note: str = "",
             state_path: Optional[Path] = None) -> Dict[str, Any]:
    """登记一个源：预期更新间隔（小时，用于陈旧检测）+ 是否允许空结果。"""
    if not source or not str(source).strip():
        return {"ok": False, "error": "source 不能为空"}
    st = _load_state(state_path)
    st["sources"][source] = {"expect_interval_h": expect_interval_h,
                             "allow_empty": bool(allow_empty), "note": note,
                             "registered_at": _now().isoformat(timespec="seconds")}
    if not _save_state(st, state_path):
        return {"ok": False, "error": "state 写入失败（登记未生效，请检查磁盘/权限）"}
    return {"ok": True, "source": source, **st["sources"][source]}


def record(source: str, count: Optional[int] = None, status: str = "ok", note: str = "",
           run_id: str = "", elapsed_s: Optional[float] = None,
           ledger_path: Optional[Path] = None) -> Dict[str, Any]:
    """记一笔运行结果（一行 JSONL ✓）。status 建议 ok / error / timeout / blocked。"""
    row = {"at": _now().isoformat(timespec="seconds"), "source": source, "count": count,
           "status": status, "note": note, "run_id": run_id, "elapsed_s": elapsed_s}
    try:
        p = Path(ledger_path) if ledger_path else LEDGER_PATH
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:                                            # noqa: BLE001
        return {"ok": False, "error": f"账本写入失败：{type(e).__name__}: {e}"}
    return {"ok": True, **row}


def baseline(source: str, window: int = 20, ledger_path: Optional[Path] = None) -> Dict[str, Any]:
    """滚动基线：最近 window 笔 ok 且 count 可解析的记录的统计量。

    另报 `rows_seen`（该源全部记录数 ✓）——用于区分"没有历史"与"有历史但全无可比 count" ✓。
    """
    rows = _read_ledger(ledger_path)
    if rows is None:
        return {"n": 0, "median": None, "p10": None, "p90": None, "last_at": None,
                "last_count": None, "rows_seen": 0, "ledger_unreadable": True}
    mine = [r for r in rows if r.get("source") == source]
    counts: List[int] = []
    for r in mine:
        if r.get("status") != "ok":
            continue
        v = _as_int(r.get("count"))
        if v is not None:
            counts.append(v)
    recent = counts[-max(1, int(window)):]
    if not recent:
        return {"n": 0, "median": None, "p10": None, "p90": None, "last_at": None,
                "last_count": None, "rows_seen": len(mine)}
    return {"n": len(recent), "median": statistics.median(recent),
            "p10": sorted(recent)[max(0, int(len(recent) * 0.1) - 1)],
            "p90": sorted(recent)[min(len(recent) - 1, int(len(recent) * 0.9))],
            "last_at": mine[-1].get("at"), "last_count": recent[-1], "rows_seen": len(mine)}


def check(source: str, count: Optional[int], window: int = 20, drop: float = 0.5,
          spike: float = 2.0, status: Optional[str] = None,
          ledger_path: Optional[Path] = None, state_path: Optional[Path] = None,
          now: Optional[datetime] = None) -> Dict[str, Any]:
    """判定一次运行是否健康。

    `status` 可传"本次运行的状态"（ok/error/timeout/blocked ✓）—— 非 ok 立即计一次失败 ✓，
    不必等账本里凑满 3 连击 ✓（旧实现要 3 天才响 ✗）。
    verdict ∈ ok / no_baseline / no_count / empty_suspicious / volume_drop / volume_spike /
              stale / stale_unknown / failing / ledger_unreadable。
    """
    st = _load_state(state_path)
    cfg = (st.get("sources") or {}).get(source) or {}
    allow_empty = bool(cfg.get("allow_empty", False))
    rows = _read_ledger(ledger_path)
    reasons: List[str] = []
    t = now or _now()
    if rows is None:
        return {"verdict": "ledger_unreadable", "reasons": ["账本不可读 → 一切基线/连击/陈旧判定不可信"],
                "baseline": {"n": 0, "median": None, "rows_seen": 0}, "source": source,
                "last_status": None, "last_at": None}
    mine = [r for r in rows if r.get("source") == source]
    last = mine[-1] if mine else None
    last_status = status if status is not None else (last or {}).get("status")
    verdict = "ok"

    if last_status is not None and last_status != "ok":               # 本次状态非 ok → 立即失败 ✓
        verdict = "failing"
        reasons.append(f"最近一次运行状态 = {last_status}（非 ok）")
    elif len(mine) >= 3 and all(r.get("status") != "ok" for r in mine[-3:]):
        verdict = "failing"
        reasons.append("账本最近 3 笔全部非 ok")

    exp = cfg.get("expect_interval_h")
    if exp and mine:
        try:
            last_at = _parse_dt(str(mine[-1].get("at")))
            if last_at and t - last_at > timedelta(hours=2 * float(exp)):
                if verdict == "ok":
                    verdict = "stale"
                reasons.append(f"距上次记录 {(t - last_at).total_seconds() / 3600:.1f}h，"
                               f"超过预期间隔 {exp}h 的 2 倍")
        except Exception as e:                                        # noqa: BLE001
            if verdict == "ok":
                verdict = "stale_unknown"
            reasons.append(f"最近记录时间无法解析（{e}）→ 陈旧判定不可用 ⚠")

    base = baseline(source, window=window, ledger_path=ledger_path)
    if count is None:
        if verdict == "ok":
            verdict = "no_count" if mine else "no_baseline"
            reasons.append("本次未提供 count（无法与基线比较）" if mine else "首次运行（无历史基线）")
        return {"verdict": verdict, "reasons": reasons, "baseline": base, "source": source,
                "last_status": last_status, "last_at": (last or {}).get("at")}

    ci = _as_int(count)
    if ci is None:
        return {"verdict": "no_count", "reasons": [f"count 非数字（{count!r}）→ 无法判定"],
                "baseline": base, "source": source, "last_status": last_status,
                "last_at": (last or {}).get("at")}

    if ci == 0 and not allow_empty:                                   # 规则 1：空结果 ≠ 正常 ✓✓
        if verdict == "ok":
            verdict = "empty_suspicious"
        reasons.append("count=0 且该源未登记 allow_empty（配额耗尽/改版都长这样 ✗）")
    if base["median"] and not (ci == 0 and allow_empty):              # 规则 2：量级突变 ✓
        med = float(base["median"])
        if med > 0 and ci < drop * med:
            if verdict == "ok":
                verdict = "volume_drop"
            reasons.append(f"count={ci} < {drop}×中位 {med:g}（掉量）")
        elif med > 0 and ci > spike * med:
            if verdict == "ok":
                verdict = "volume_spike"
            reasons.append(f"count={ci} > {spike}×中位 {med:g}（突增）")
    elif base["n"] == 0 and verdict == "ok":
        verdict = "no_baseline"
        reasons.append(f"无可比基线（该源历史 {base.get('rows_seen', 0)} 笔，"
                       f"其中 ok 且 count 可解析的为 0）" if base.get("rows_seen")
                       else "首次运行（记一笔后即建立基线）")
    return {"verdict": verdict, "reasons": reasons, "baseline": base, "source": source,
            "last_status": last_status, "last_at": (last or {}).get("at")}


def add_credential(name: str, expires_at: str, warn_days: int = 7, note: str = "",
                   state_path: Optional[Path] = None) -> Dict[str, Any]:
    """登记凭据到期日（规则 3 ✓ —— 七麦 VIP 临到期才发现 ✗）。"""
    try:
        _parse_dt(expires_at)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    st = _load_state(state_path)
    creds = [c for c in st["credentials"] if isinstance(c, dict) and c.get("name") != name]
    wd = _as_int(warn_days) or 7
    creds.append({"name": name, "expires_at": expires_at, "warn_days": wd,
                  "note": note, "added_at": _now().isoformat(timespec="seconds")})
    st["credentials"] = creds
    if not _save_state(st, state_path):
        return {"ok": False, "error": "state 写入失败（凭据未登记，请检查磁盘/权限）"}
    return {"ok": True, "name": name, "expires_at": expires_at, "warn_days": wd}


def due_credentials(warn_days: Optional[int] = None, state_path: Optional[Path] = None,
                    now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """返回已过期或进入提醒窗口的凭据（按剩余时间升序 ✓）。

    `warn_days=None` 时**用凭据自己的设定** ✓（旧实现被 report 的默认 7 天覆盖 ✗）；
    解析失败/字段异常的凭据以 `error` 行返回（不静默漏出日历 ✓✗）。
    """
    t = now or _now()
    out: List[Dict[str, Any]] = []
    for c in _load_state(state_path).get("credentials") or []:
        if not isinstance(c, dict):
            out.append({"name": "?", "error": "凭据条目结构异常（非对象）"})
            continue
        try:
            exp = _parse_dt(str(c.get("expires_at")))
        except ValueError as e:
            out.append({"name": c.get("name"), "error": str(e)})
            continue
        if not exp:
            out.append({"name": c.get("name"), "error": "expires_at 为空"})
            continue
        wd = _as_int(warn_days)
        if wd is None:
            wd = _as_int(c.get("warn_days"))
        if wd is None:
            wd = 7
        days_left = (exp - t).total_seconds() / 86400
        if days_left <= wd:
            out.append({"name": c.get("name"), "expires_at": c.get("expires_at"),
                        "days_left": round(days_left, 1), "expired": days_left <= 0,
                        "note": c.get("note", "")})
    return sorted(out, key=lambda x: x.get("days_left", -1e9))


def _build_report(warn_days: Optional[int] = None, ledger_path: Optional[Path] = None,
                  state_path: Optional[Path] = None, window: int = 20):
    """产出 (报告行, 告警条目 ✓)。

    告警**程序化收集**（R31 审查：CLI 曾用字符串匹配判退出码 ✗ —— 源名里带"不可读"
    就会误报，而且**完全没看源告警** ✗✗）。告警 = 账本不可读 / 凭据临期(含已过期/条目异常)
    / 任一源判定不在 `OK_VERDICTS` 白名单 ✓。
    """
    st = _load_state(state_path)
    rows = _read_ledger(ledger_path)
    alarms: List[str] = []
    sources = sorted(set(list((st.get("sources") or {}).keys())
                         + sorted({r["source"] for r in rows or []})))
    lines = ["# 源健康台账", "",
             f"生成：{_now():%Y-%m-%d %H:%M}；账本 {len(rows) if rows is not None else '不可读'}"
             f" 笔；源 {len(sources)} 个", ""]
    if rows is None:
        alarms.append("账本整体不可读")
        lines += ["> ⚠ 账本整体不可读 ✗ → 下列判定不可信，请先修复账本文件", ""]
    if st.get("_recovered_from"):
        lines += [f"> ⚠ 状态档案曾损坏并隔离重建（{st['_recovered_from']}）"
                  "→ 登记信息可能已丢失，请复核 register/cred 登记", ""]
    creds = due_credentials(warn_days=warn_days, state_path=state_path)
    if creds:
        lines += ["## ⚠ 凭据到期日历", "", "| 凭据 | 到期 | 剩余天 | 状态 |", "|---|---|---|---|"]
        for c in creds:
            if c.get("error"):
                alarms.append(f"凭据条目异常：{c.get('name')}")
                lines.append(f"| {c.get('name')} | — | — | 条目异常 ✗（{c['error']}） |")
            else:
                alarms.append(f"凭据{'已过期' if c['expired'] else '临期'}：{c['name']}"
                              f"（{c['days_left']} 天）")
                lines.append(f"| {c['name']} | {c['expires_at']} | {c['days_left']} | "
                             f"{'已过期 ✗' if c['expired'] else '即将到期 ⚠'} |")
        lines.append("")
    lines += ["## 源状态", "", "| 源 | 最近一次（真实末笔） | 状态 | count | 中位(近20) | 判定 |",
              "|---|---|---|---|---|---|"]
    for s in sources:
        b = baseline(s, window=window, ledger_path=ledger_path)
        v = check(s, b.get("last_count"), window=window, ledger_path=ledger_path,
                  state_path=state_path)
        if v["verdict"] not in OK_VERDICTS:
            alarms.append(f"源 {s}：{v['verdict']}")
        lines.append(f"| {s} | {v.get('last_at') or '—'} | {v.get('last_status') or '—'} | "
                     f"{b.get('last_count') if b.get('last_count') is not None else '—'} | "
                     f"{b.get('median') if b.get('median') is not None else '—'} | "
                     f"{v['verdict']}{'（' + '；'.join(v['reasons'][:1]) + '）' if v['reasons'] else ''} |")
    lines += ["", "## 判读口径", "",
              "- **空结果 ≠ 正常**：count=0 默认判 empty_suspicious（配额耗尽/页面改版都长这样 ✗）",
              "- 量级突变：低于 0.5× 或高于 2× 中位 → 告警",
              "- 陈旧：距上次记录超过登记间隔的 2 倍 → stale；本次状态非 ok 或连续 3 笔非 ok → failing",
              "- **退出码白名单**：`check` 只有 ok / no_baseline 返回 0；`report` 只要报告里出现"
              "告警条目（源告警/凭据临期或过期/账本不可读）就返回 1 ✓",
              ""]
    return lines, alarms


def report(warn_days: Optional[int] = None, ledger_path: Optional[Path] = None,
           state_path: Optional[Path] = None, window: int = 20) -> str:
    """生成 Markdown 健康报告（源状态 + 基线 + 凭据日历 ✓）。`warn_days=None` 用各凭据自设 ✓。"""
    return "\n".join(_build_report(warn_days, ledger_path, state_path, window)[0])


def build_parser():
    """CLI 参数定义（唯一来源 ✓ —— 供本模块与 cli.py 复用，避免两处漂移 ✗）。"""
    import argparse
    ap = argparse.ArgumentParser(prog="health",
                                 description="源健康台账（空结果≠正常 ✓ + 凭据日历 ✓）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("register", help="登记源（预期间隔/是否允许空）")
    r.add_argument("source")
    r.add_argument("--interval-h", type=float, default=None)
    r.add_argument("--allow-empty", action="store_true")
    r.add_argument("--note", default="")
    rc = sub.add_parser("record", help="记一笔运行结果")
    rc.add_argument("source")
    rc.add_argument("--count", type=int, default=None)
    rc.add_argument("--status", default="ok")
    rc.add_argument("--note", default="")
    ck = sub.add_parser("check", help="判定一次运行是否健康（退出码 1 = 要人工看 ✓）")
    ck.add_argument("source")
    ck.add_argument("--count", type=int, default=None)
    ck.add_argument("--status", default=None, help="本次运行状态（ok/error/timeout/blocked ✓）")
    cr = sub.add_parser("cred", help="登记凭据到期日")
    cr.add_argument("name")
    cr.add_argument("--expires", required=True, help='如 "2026-10-13 14:05"（含空格须加引号 ✓）')
    cr.add_argument("--warn-days", type=int, default=7)
    cr.add_argument("--note", default="")
    rp = sub.add_parser("report", help="生成 Markdown 健康报告")
    rp.add_argument("--warn-days", type=int, default=None, help="覆盖各凭据自设的提醒窗口（默认不覆盖 ✓）")
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI：`python3 -m universal_scraper.cli health register|record|check|cred|report ...`"""
    args = build_parser().parse_args(argv)
    if args.cmd == "register":
        res = register(args.source, expect_interval_h=args.interval_h,
                       allow_empty=args.allow_empty, note=args.note)
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return 0 if res.get("ok") else 1
    if args.cmd == "record":
        res = record(args.source, count=args.count, status=args.status, note=args.note)
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return 0 if res.get("ok") else 1
    if args.cmd == "check":
        res = check(args.source, args.count, status=args.status)
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return 0 if res.get("verdict") in OK_VERDICTS else 1        # 白名单 ✓
    if args.cmd == "cred":
        res = add_credential(args.name, args.expires, warn_days=args.warn_days, note=args.note)
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return 0 if res.get("ok") else 1
    lines, alarms = _build_report(args.warn_days)
    print("\n".join(lines))
    for a in alarms:                                              # 告警条目上浮（可脚本消费 ✓）
        _stderr(f"⚠ {a}")
    return 1 if alarms else 0


if __name__ == "__main__":
    raise SystemExit(main())
