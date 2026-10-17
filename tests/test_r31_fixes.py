"""R31 验证电池：自愈链条（chain.py）+ 源健康台账（health.py）。

覆盖两条**战场教训**与 R31 四路审查点名的缺陷：
A. 采集器吞掉逐款失败 → 退出码 0 但日志满屏死亡特征 ✗ → 链条必须**按日志内容判死并重跑**，
   且**没真正成功就绝不能报 done** ✓✓（审查 C1：旧实现判死后仍 status=done ✗）；
B. 配额耗尽/页面改版表现为"0 条" ✗ → 台账必须**空结果可疑** ✓✓，且告警词一律非零退出 ✓。

隔离：autouse fixture 把 chain/health 的状态路径全部改到 tmp_path（**不写真实家目录** ✓✗）。
"""
from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from universal_scraper import chain as C          # noqa: E402
from universal_scraper import health as H         # noqa: E402

DEATH_SCRIPT = "for i in $(seq 12); do echo 'TargetClosedError: x'; done; exit 0"


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    """全部状态落到 tmp（Chain 构造期会 mkdir ✓ —— 旧用例曾写真实家目录 ✗）。"""
    monkeypatch.setattr(C, "STATE_DIR", tmp_path / "chain")
    monkeypatch.setattr(H, "STATE_DIR", tmp_path / "health")
    monkeypatch.setattr(H, "LEDGER_PATH", tmp_path / "health" / "ledger.jsonl")
    monkeypatch.setattr(H, "STATE_PATH", tmp_path / "health" / "state.json")
    yield


def _chain(tmp_path, steps, probes=(), **kw):
    return C.Chain(steps=steps, probes=list(probes), state_key="t",
                   log_path=tmp_path / "chain.log",
                   journal_path=tmp_path / "j.jsonl",
                   lock_path=tmp_path / "l.lock", **kw)


def _rows(path):
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


# ================= chain：入参校验与工具 =================
def test_validate_argv_rejects_string():
    with pytest.raises(ValueError):
        C._validate_argv("python3 x.py")


def test_validate_argv_rejects_empty_and_nonstr():
    with pytest.raises(ValueError):
        C._validate_argv([])
    with pytest.raises(ValueError):
        C._validate_argv(["ok", 123])


def test_validate_argv_accepts_list():
    assert C._validate_argv(["python3", "x.py"]) == ["python3", "x.py"]


def test_step_requires_list_argv():
    with pytest.raises(ValueError):
        C.Chain(steps=[C.Step("bad", "python3 x.py")])


def test_chain_rejects_empty_steps():
    with pytest.raises(ValueError):
        C.Chain(steps=[])


def test_parse_step_spec_ok():
    st = C.parse_step_spec("R2=python3 r2.py --limit 3")
    assert st.name == "R2" and st.argv == ["python3", "r2.py", "--limit", "3"]


def test_parse_step_spec_rejects_bad():
    with pytest.raises(ValueError):
        C.parse_step_spec("no-equals")
    with pytest.raises(ValueError):
        C.parse_step_spec("name=   ")


def test_parse_dt_accepts_iso_t_and_space():
    """★ 审查：账本/锁写的是 ISO（带 T）→ 解析必须吃 ✓（旧实现只吃空格 ✗）。"""
    assert C._parse_dt("2026-10-13T12:00:00") == datetime(2026, 10, 13, 12, 0)
    assert C._parse_dt("2026-10-13T12:00:00.123456").replace(microsecond=0) == \
        datetime(2026, 10, 13, 12, 0)                  # 微秒保留，不抛 ✓
    assert C._parse_dt("2026-10-13 12:00") == datetime(2026, 10, 13, 12, 0)
    assert C._parse_dt("") is None
    with pytest.raises(ValueError):
        C._parse_dt("nope")


def test_pid_alive_edges():
    assert C.Chain._pid_alive(os.getpid()) is True
    assert C.Chain._pid_alive(-1) is False          # pid<=0 = 非有效锁 ✓
    assert C.Chain._pid_alive(0) is False
    assert C.Chain._pid_alive(None) is False


def test_run_cmd_reports_failure_reason(tmp_path):
    """★ 审查：自愈失败原因不得被双重吞掉 ✓（返回 (ok, reason)）。"""
    ok, reason = C._run_cmd(["/definitely/not/a/binary"])
    assert ok is False and reason
    ok2, reason2 = C._run_cmd("curl x")              # 字符串命令 → 拒绝 ✓
    assert ok2 is False and "参数非法" in reason2


def test_http_probe_dead_port_is_false():
    ok, why = C._http_probe("http://127.0.0.1:1/none", timeout=1)
    assert ok is False and why                                   # 原因必须上浮 ✓（不再只回 bool ✗）


# ================= chain：流程与出口语义 =================
def test_chain_step_success_single_attempt(tmp_path):
    """干净成功必须**只跑一次** ✓（杀掉"每次重跑白烧配额"的变异 ✗）。"""
    ch = _chain(tmp_path, [C.Step("s1", ["/bin/echo", "hello"])])
    res = ch.run()
    assert res["status"] == "done"
    assert len(res["steps"][0]["attempts"]) == 1
    assert res["steps"][0]["outcome"] == "done"


def test_chain_fatal_code_stops(tmp_path):
    ch = _chain(tmp_path, [C.Step("s1", ["/bin/sh", "-c", "exit 2"]),
                           C.Step("s2", ["/bin/echo", "should-not-run"])])
    res = ch.run()
    assert res["status"] == "fatal"
    assert len(res["steps"]) == 1


def test_chain_soft_code_continues(tmp_path):
    ch = _chain(tmp_path, [C.Step("s1", ["/bin/sh", "-c", "exit 3"]),
                           C.Step("s2", ["/bin/echo", "ran"])])
    res = ch.run()
    assert res["status"] == "done"
    assert len(res["steps"]) == 2
    assert res["steps"][0]["outcome"] == "soft"


def test_chain_unconventional_exit_code_is_failure(tmp_path):
    """★ 审查 C1：非约定退出码（如 1）**不得**静默当完成 ✓✓。"""
    ch = _chain(tmp_path, [C.Step("s1", ["/bin/sh", "-c", "exit 1"])])
    res = ch.run()
    assert res["status"] == "step_failed"
    assert res["steps"][0]["outcome"] == "step_failed"
    assert len(res["steps"][0]["attempts"]) == 2      # 失败会重跑一次 ✓


def test_chain_missing_binary_is_failure(tmp_path):
    ch = _chain(tmp_path, [C.Step("s1", ["/definitely/not/a/binary"])])
    res = ch.run()
    assert res["status"] != "done"                     # env 找不到 → 127 ✓
    assert res["steps"][0]["attempts"][0]["code"] == 127


def test_chain_death_by_log_content_then_retry(tmp_path):
    """★ 核心教训 A：**步骤自身输出**里的死亡特征也要被抓 ✓（杀掉 stdout=DEVNULL 变异 ✗）。"""
    ch = _chain(tmp_path, [C.Step("s1", ["/bin/sh", "-c", DEATH_SCRIPT])])
    res = ch.run()
    a = res["steps"][0]["attempts"]
    assert len(a) == 2                                 # 判死 → 重跑一次 ✓
    assert a[0]["code"] == 0 and a[0]["death_hits"] >= C.DEATH_HITS_THRESHOLD
    assert "TargetClosedError" in ch.log_path.read_text(encoding="utf-8")
    assert res["status"] == "death_persists"           # 重跑仍死 → 失败（旧实现报 done ✗✗）


def test_chain_death_hits_only_count_current_attempt(tmp_path):
    """★ 审查 F18：判死只数**本次尝试新增字节** ✓（旧实现把前次命中算到本次 ✗）。"""
    ch = _chain(tmp_path, [C.Step("s1", ["/bin/sh", "-c", DEATH_SCRIPT])],
                max_heal_retries=2)
    res = ch.run()
    a = res["steps"][0]["attempts"]
    assert len(a) == 3
    assert [x["death_hits"] for x in a] == [12, 12, 12]   # 各自只数自己 ✓（不是 12/24/36）


def test_chain_heal_called_before_step_when_probe_dead(tmp_path):
    """★ 审查 M3：判死→自愈路径（旧测试里 heal 是死代码 ✗）。"""
    ch = _chain(tmp_path, [C.Step("s1", ["/bin/sh", "-c", DEATH_SCRIPT])])
    calls = {"n": 0}

    def _check():
        return calls["n"] > 0                          # 前置探活 False → 自愈后才活 ✓

    def _heal():
        calls["n"] += 1
        return True

    ch.probes = [C.Probe("fake", _check, _heal, heal_wait_s=0)]
    res = ch.run()
    assert calls["n"] == 1                             # 自愈恰好一次 ✓
    assert len(res["steps"][0]["attempts"]) == 2       # 判死后重跑 ✓
    assert res["status"] == "death_persists"
    assert any(r.get("event") == "heal" and r.get("ok") is True for r in _rows(ch.journal_path))


def test_chain_heal_failure_stops_with_probe_fail(tmp_path):
    """★ 审查 H2：自愈返回 True 后**必须复验** ✓✗ —— 通用 Probe 的 heal() 只说"我拉起了"，
    不等于依赖真活了；不复验就会打"已恢复"然后带着死依赖空转 ✗✗。"""
    ch = _chain(tmp_path, [C.Step("s1", ["/bin/echo", "x"])])
    seen = {"n": 0}

    def _check():
        seen["n"] += 1
        return False                                   # 一直不活 ✓

    def _heal():
        return True                                    # 自愈自称成功 ✗ 但复验会戳穿 ✓

    ch.probes = [C.Probe("liar", _check, _heal, heal_wait_s=0)]
    res = ch.run()
    assert res["status"] == "probe_fail"
    assert res["steps"] == []                          # 一步都没跑（探针不可用就别开跑 ✓）
    rows = _rows(ch.journal_path)
    assert any(r.get("event") == "heal" and r.get("ok") is False for r in rows)   # 复验失败落账 ✓


def test_chain_heal_failure_after_judgement_stops(tmp_path):
    """判死后的自愈失败也要全停（且此时已有一次尝试记录 ✓）。"""
    ch = _chain(tmp_path, [C.Step("s1", ["/bin/sh", "-c", DEATH_SCRIPT])])
    calls = {"n": 0}

    def _check():
        calls["n"] += 1
        return calls["n"] <= 1                         # 步骤开始前活 ✓；判死后复探死 ✗

    def _heal():
        return False

    ch.probes = [C.Probe("fake", _check, _heal, heal_wait_s=0)]
    res = ch.run()
    assert res["status"] == "probe_fail"
    assert len(res["steps"][0]["attempts"]) == 1


def test_chain_max_heal_retries_zero(tmp_path):
    ch = _chain(tmp_path, [C.Step("s1", ["/bin/sh", "-c", DEATH_SCRIPT])], max_heal_retries=0)
    res = ch.run()
    assert len(res["steps"][0]["attempts"]) == 1       # 不重跑 ✓
    assert res["status"] == "death_persists"           # 但绝不报 done ✓✓


def test_chain_judge_unavailable_is_failure(tmp_path, monkeypatch):
    """★ 审查 F1：判死器读不到日志 → 绝不判"健康" ✓✗（旧实现 fail-open 返回 0 ✗）。"""
    ch = _chain(tmp_path, [C.Step("s1", ["/bin/echo", "x"])])
    monkeypatch.setattr(ch, "_log_size", lambda: 0)
    monkeypatch.setattr(ch, "tail_death_hits", lambda offset=None: (None, {}))
    res = ch.run()
    assert res["status"] == "judge_unavailable"


def test_chain_until_passed_on_every_attempt(tmp_path, monkeypatch):
    """★ 审查 I5：重跑**同样**带 --until ✓（旧实现只第 1 次带 ✗）。"""
    seen = []
    ch = _chain(tmp_path, [C.Step("s1", ["/bin/echo", "x"], until="2099-01-01 00:00")],
                max_heal_retries=1)
    monkeypatch.setattr(ch, "_exec_step", lambda argv, timeout_s: seen.append(list(argv)) or 0)
    monkeypatch.setattr(ch, "tail_death_hits", lambda offset=None: (12, {"x": 12}))
    ch.run()
    assert len(seen) == 2 and all("--until" in a for a in seen)


def test_chain_probe_fail_stops(tmp_path):
    """探针在**任何步骤开始前**就不可用 → 全停，且不产生步骤记录（细节在账本里 ✓）。"""
    ch = _chain(tmp_path, [C.Step("s1", ["/bin/echo", "x"])],
                probes=[C.Probe("dead", lambda: False, lambda: False)])
    res = ch.run()
    assert res["status"] == "probe_fail"
    assert res["steps"] == []
    assert any(r.get("event") == "probe_fail" and r.get("step") == "s1"
               for r in _rows(ch.journal_path))


def test_chain_probe_exception_is_unhealthy_not_crash(tmp_path):
    def _boom():
        raise RuntimeError("probe boom")

    ch = _chain(tmp_path, [C.Step("s1", ["/bin/echo", "x"])],
                probes=[C.Probe("boom", _boom, lambda: False)])
    assert ch.run()["status"] == "probe_fail"          # 不外抛 ✓


def test_chain_deadline_stops(tmp_path):
    past = (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M")
    ch = _chain(tmp_path, [C.Step("s1", ["/bin/echo", "x"])], deadline=past)
    assert ch.run()["status"] == "deadline"


def test_chain_deadline_clamps_step_timeout(tmp_path):
    """★ 审查 I6：临近硬时钟时本步超时被收紧 ✓（不再可能跑 60h ✗）。"""
    soon = (datetime.now() + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M")
    ch = _chain(tmp_path, [C.Step("s1", ["/bin/echo", "x"], timeout_s=216000)], deadline=soon)
    t, no_time = ch._effective_timeout(ch.steps[0])
    assert not no_time and t <= 5 * 60 + 2


def test_chain_single_flight_lock(tmp_path):
    """★ R32：锁改用内核 `flock` ✓ —— "持有"必须是**真的持有 flock**，
    写个 pid 到文件里不再能骗过它（旧实现信 pid，于是互删对方的锁 → TOCTOU 双飞 ✗✗）。"""
    ch = _chain(tmp_path, [C.Step("s1", ["/bin/echo", "x"])])
    holder = os.open(ch.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.write(holder, json.dumps({"pid": os.getpid()}).encode())
        res = ch.run()
        assert res["status"] == "locked"
        assert "持有者 pid" in res["reason"]                 # 说明里带持有者信息 ✓
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)


def test_chain_lock_file_content_never_blocks(tmp_path):
    """锁文件内容只作诊断 ✓：坏 JSON / 缺 pid / 死 pid 都不得锁死（旧实现靠 pid 判活 ✗）。"""
    for bad in ('{"note":"x"}', '{"pid":null}', "not-json", '{"pid":999999}'):
        ch = _chain(tmp_path, [C.Step("s1", ["/bin/echo", "x"])])
        ch.lock_path.write_text(bad, encoding="utf-8")
        assert ch.run()["status"] == "done", bad


def test_chain_dead_pid_lock_overwritten(tmp_path):
    ch = _chain(tmp_path, [C.Step("s1", ["/bin/echo", "x"])])
    ch.lock_path.write_text(json.dumps({"pid": 999999}), encoding="utf-8")
    assert ch.run()["status"] == "done"


def test_chain_lock_released_after_run(tmp_path):
    """跑完锁必须释放（flock 由内核在 close/退出时释放 ✓；锁文件本身保留 ✓）。"""
    ch = _chain(tmp_path, [C.Step("s1", ["/bin/echo", "x"])])
    ch.run()
    again = _chain(tmp_path, [C.Step("s1", ["/bin/echo", "x"])])
    assert again.run()["status"] == "done"


def test_chain_journal_written_and_counts(tmp_path):
    ch = _chain(tmp_path, [C.Step("s1", ["/bin/echo", "x"])])
    res = ch.run()
    assert any(r.get("event") == "attempt" and r.get("step") == "s1" for r in _rows(ch.journal_path))
    assert res["journal_errors"] == 0 and res["log_errors"] == 0


def test_chain_journal_write_failure_surfaces(tmp_path):
    """★ 审查 F9：账本写不进去 → 状态不得是 done ✓（不可审计≠成功 ✗）。"""
    ch = _chain(tmp_path, [C.Step("s1", ["/bin/echo", "x"])])
    (tmp_path / "blocked").write_text("not a dir", encoding="utf-8")   # 让 mkdir 失败 ✓
    ch.journal_path = tmp_path / "blocked" / "j.jsonl"
    res = ch.run()
    assert res["journal_errors"] > 0
    assert res["status"] == "ledger_broken"


def test_chain_log_write_failure_is_not_done(tmp_path, monkeypatch):
    """★ 日志是**判死输入**：写不进去 = 判死失效 + 审计轨迹丢失 → 不得报完成 ✓✓。

    真实场景=**步骤跑完后磁盘写满**（日志 open 成功、后续写入失败、判死器仍读得到）✓；
    若日志路径整体不可用，则会先撞 `judge_unavailable`（同样非 done ✓，见上一个用例）。
    """
    ch = _chain(tmp_path, [C.Step("s1", ["/bin/echo", "x"])])
    ch.log_path.write_text("旧日志\n", encoding="utf-8")
    ch.log_path.chmod(0o444)                             # 只读文件：追加必失败、读取仍可 ✓
    monkeypatch.setattr(ch, "_exec_step", lambda argv, t: 0)           # 步骤本身成功 ✓
    res = ch.run()
    assert res["log_errors"] > 0
    assert res["status"] == "log_broken"
    assert res["steps"][0]["attempts"][0]["death_hits"] == 0           # 判死器仍可用 ✓


def test_chain_optional_step_failure_continues_but_partial(tmp_path):
    """★ `required=False` 曾是**死字段** ✗（所有失败路径都 stop_chain=True，字段从不生效）：
    可选步骤失败应继续下一级，但整体**不得报全绿** ✗✓。"""
    ch = _chain(tmp_path, [C.Step("opt", ["/bin/sh", "-c", "exit 1"], required=False),
                           C.Step("must", ["/bin/echo", "ran"])], max_heal_retries=0)
    res = ch.run()
    assert len(res["steps"]) == 2                        # 链条继续 ✓
    assert res["status"] == "partial"                    # 但非全绿 ✓✓
    assert res["skipped_steps"] and "opt" in res["skipped_steps"][0]
    assert res["steps"][1]["outcome"] == "done"


def test_chain_required_step_failure_still_stops(tmp_path):
    """对照组：必需步骤失败仍必须中断（默认行为不许被 partial 改造放宽 ✗）。"""
    ch = _chain(tmp_path, [C.Step("must", ["/bin/sh", "-c", "exit 1"]),
                           C.Step("later", ["/bin/echo", "should-not-run"])], max_heal_retries=0)
    res = ch.run()
    assert len(res["steps"]) == 1 and res["status"] == "step_failed"


def test_chain_death_burst_at_head_of_long_output(tmp_path):
    """★ 判死**召回**：死亡爆发集中在本次输出**开头**、其后刷出 150KB 正常日志 ✗✗
    （实测"带着死浏览器 9 秒跑完 1,751 款"就是这形态）——只扫尾部会误判健康 ✗。"""
    script = ("for i in $(seq 12); do echo 'TargetClosedError: x'; done; "
              "i=0; while [ $i -lt 4000 ]; do echo 'a-normal-log-line-that-is-long-enough-ok'; "
              "i=$((i+1)); done")
    ch = _chain(tmp_path, [C.Step("s1", ["/bin/sh", "-c", script])])
    res = ch.run()
    assert res["steps"][0]["attempts"][0]["death_hits"] >= C.DEATH_HITS_THRESHOLD
    assert res["status"] == "death_persists"             # 判死 → 重跑仍死 ✓


def test_chain_death_patterns_custom(tmp_path):
    ch = _chain(tmp_path, [C.Step("s1", ["/bin/echo", "x"])], death_patterns=("BOOM",))
    ch.log_path.write_text("BOOM BOOM\n", encoding="utf-8")
    hits, detail = ch.tail_death_hits()
    assert hits == 2 and detail.get("BOOM") == 2


def test_chain_tail_death_missing_log_is_zero(tmp_path):
    ch = _chain(tmp_path, [C.Step("s1", ["/bin/echo", "x"])])
    ch.log_path = tmp_path / "nope.log"
    assert ch.tail_death_hits() == (0, {})             # 从未存在过 ≠ 不可读 ✓


def test_chain_tail_death_unreadable_is_none(tmp_path):
    ch = _chain(tmp_path, [C.Step("s1", ["/bin/echo", "x"])])
    ch.log_path = tmp_path / "adir"
    ch.log_path.mkdir()
    assert ch.tail_death_hits()[0] is None             # 未知 ≠ 健康 ✓✓


def test_chain_probe_heal_raising_is_probe_fail(tmp_path):
    """自愈函数自己抛异常 → 不得穿透（承诺"不外抛" ✓）。"""
    def _boom():
        raise RuntimeError("heal boom")

    ch = _chain(tmp_path, [C.Step("s1", ["/bin/echo", "x"])],
                probes=[C.Probe("boom", lambda: False, _boom)])
    res = ch.run()
    assert res["status"] == "probe_fail"
    rows = _rows(ch.journal_path)
    assert any(r.get("event") == "heal" and r.get("ok") is False and "boom" in str(r.get("reason"))
               for r in rows)                          # 失败原因进账本 ✓


def test_chain_exec_timeout_kills_whole_process_group(tmp_path, monkeypatch):
    """★ 超时必须杀**整个进程组** ✗✗：`subprocess.run(timeout=)` 只杀直接子进程，
    而步骤常是脚本/桥（node .cjs → 浏览器）——父进程被杀后**孙子进程会继续跑继续写** ✗。
    本用例实测：父进程起一个后台孙子进程持续写文件 → 超时 → 文件必须停止增长 ✓。"""
    monkeypatch.setattr(C, "MIN_STEP_TIMEOUT_S", 1)          # 生产下限 60s；测试下调 ✓
    marker = tmp_path / "orphan.txt"
    script = (f"(while true; do echo tick >> {marker}; sleep 0.2; done) & "
              "sleep 30")                                     # 父进程睡死 → 必超时 ✓
    ch = _chain(tmp_path, [C.Step("s1", ["/bin/sh", "-c", script], timeout_s=2)],
                max_heal_retries=0)
    res = ch.run()
    row = res["steps"][0]["attempts"][0]
    assert row["code"] == -9 and row["exec_error"] == "TimeoutExpired"
    assert res["status"] != "done"                            # 超时绝不报完成 ✓
    time.sleep(0.3)
    size1 = marker.stat().st_size if marker.exists() else 0
    time.sleep(1.2)
    size2 = marker.stat().st_size if marker.exists() else 0
    assert size1 > 0, "孙子进程没跑起来，用例无效"
    assert size2 == size1, f"孤儿进程仍在写 ✗（进程组没杀干净：{size1}→{size2}）"


def test_chain_effective_timeout_no_time_left(tmp_path):
    """★ 硬时钟在即 → 不启动该步（返回 no_time ✓），且状态非 done ✓。"""
    soon = (datetime.now() + timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M:%S")
    ch = _chain(tmp_path, [C.Step("s1", ["/bin/echo", "x"])], deadline=soon)
    t, no_time = ch._effective_timeout(ch.steps[0])
    assert t == 60 and no_time is True
    res = ch.run()
    assert res["status"] == "deadline"
    assert res["steps"][0]["attempts"] == []           # 一步都没跑 ✓


def test_chain_lock_open_failure_is_locked_not_crash(tmp_path, monkeypatch):
    """占锁本身失败（磁盘/权限）→ 结构化 locked，不外抛 ✓。"""
    ch = _chain(tmp_path, [C.Step("s1", ["/bin/echo", "x"])])

    def _boom(*a, **kw):
        raise OSError("no space")

    monkeypatch.setattr(C.os, "open", _boom)
    assert ch.run()["status"] == "locked"


def test_chain_does_not_write_real_home(tmp_path):
    """隔离回归：本套件任何用例都不得在真实家目录新增状态 ✓✗。"""
    home_chain = Path.home() / ".universal_scraper" / "chain"
    before = set(home_chain.glob("*")) if home_chain.exists() else set()
    _chain(tmp_path, [C.Step("s1", ["/bin/echo", "x"])]).run()
    after = set(home_chain.glob("*")) if home_chain.exists() else set()
    assert after <= before


# ================= health：台账与判定 =================
def _hpaths(tmp_path):
    return tmp_path / "ledger.jsonl", tmp_path / "state.json"


def test_health_record_and_read(tmp_path):
    led, st = _hpaths(tmp_path)
    r = H.record("src", count=10, ledger_path=led)
    assert r["ok"] and H.baseline("src", ledger_path=led)["last_count"] == 10


def test_health_empty_is_suspicious(tmp_path):
    """★ 核心教训 B：空结果 ≠ 正常 ✓✓。"""
    led, st = _hpaths(tmp_path)
    for _ in range(5):
        H.record("src", count=100, ledger_path=led)
    assert H.check("src", 0, ledger_path=led, state_path=st)["verdict"] == "empty_suspicious"


def test_health_allow_empty_ok(tmp_path):
    led, st = _hpaths(tmp_path)
    H.register("src", allow_empty=True, state_path=st)
    for _ in range(5):
        H.record("src", count=100, ledger_path=led)
    assert H.check("src", 0, ledger_path=led, state_path=st)["verdict"] == "ok"


def test_health_volume_drop_and_spike(tmp_path):
    led, st = _hpaths(tmp_path)
    for _ in range(10):
        H.record("src", count=1000, ledger_path=led)
    assert H.check("src", 200, ledger_path=led, state_path=st)["verdict"] == "volume_drop"
    assert H.check("src", 5000, ledger_path=led, state_path=st)["verdict"] == "volume_spike"


def test_health_no_baseline_first_run(tmp_path):
    led, st = _hpaths(tmp_path)
    assert H.check("newsrc", 5, ledger_path=led, state_path=st)["verdict"] == "no_baseline"


def test_health_no_count_is_flagged(tmp_path):
    """★ 审查 F13：不传 count 不得静默算 ok ✓。"""
    led, st = _hpaths(tmp_path)
    for _ in range(3):
        H.record("src", count=100, ledger_path=led)
    assert H.check("src", None, ledger_path=led, state_path=st)["verdict"] == "no_count"


def test_health_current_status_non_ok_fails_immediately(tmp_path):
    """★ 审查 F14：本次状态非 ok → 立即 failing ✓（旧实现要凑 3 连击 = 3 天才响 ✗）。"""
    led, st = _hpaths(tmp_path)
    for _ in range(3):
        H.record("src", count=100, ledger_path=led)
    v = H.check("src", 100, status="blocked", ledger_path=led, state_path=st)
    assert v["verdict"] == "failing" and v["last_status"] == "blocked"


def test_health_failing_streak_from_ledger(tmp_path):
    led, st = _hpaths(tmp_path)
    for s in ("error", "timeout", "blocked"):
        H.record("src", count=None, status=s, ledger_path=led)
    assert H.check("src", None, ledger_path=led, state_path=st)["verdict"] == "failing"


def test_health_stale_and_stale_unknown(tmp_path):
    led, st = _hpaths(tmp_path)
    H.register("src", expect_interval_h=1, state_path=st)
    old = (datetime.now() - timedelta(hours=5)).isoformat(timespec="seconds")
    led.write_text(json.dumps({"at": old, "source": "src", "count": 10, "status": "ok"}) + "\n",
                   encoding="utf-8")
    assert H.check("src", 10, ledger_path=led, state_path=st)["verdict"] == "stale"
    led.write_text(json.dumps({"at": "not-a-date", "source": "src", "count": 10,
                               "status": "ok"}) + "\n", encoding="utf-8")
    assert H.check("src", 10, ledger_path=led, state_path=st)["verdict"] == "stale_unknown"


def test_health_baseline_stats_and_window(tmp_path):
    led, st = _hpaths(tmp_path)
    for c in (10, 20, 30, 40, 50):
        H.record("src", count=c, ledger_path=led)
    b = H.baseline("src", ledger_path=led)
    assert b["n"] == 5 and b["median"] == 30
    b1 = H.baseline("src", window=1, ledger_path=led)
    assert b1["n"] == 1 and b1["median"] == 50          # 取最新一笔 ✓


def test_health_multi_source_isolated(tmp_path):
    """★ 审查 M6：源之间不得串味 ✓。"""
    led, st = _hpaths(tmp_path)
    for _ in range(10):
        H.record("srcA", count=1000, ledger_path=led)
    for _ in range(10):
        H.record("srcB", count=1, ledger_path=led)
    assert H.baseline("srcA", ledger_path=led)["median"] == 1000
    assert H.baseline("srcB", ledger_path=led)["median"] == 1
    assert H.check("srcB", 1, ledger_path=led, state_path=st)["verdict"] == "ok"


def test_health_dirty_count_does_not_raise(tmp_path):
    """★ 审查 F6/I2：账本里的脏 count 不得让判定/报告崩栈 ✓。"""
    led, st = _hpaths(tmp_path)
    led.write_text("\n".join([
        json.dumps({"source": "src", "count": "N/A", "status": "ok"}),
        json.dumps({"source": "src", "count": None, "status": "ok"}),
        json.dumps({"source": "src", "count": True, "status": "ok"}),
        json.dumps({"source": "src", "count": 100, "status": "ok"}),
    ]) + "\n", encoding="utf-8")
    assert H.baseline("src", ledger_path=led)["median"] == 100     # bool 不当 1 用 ✓
    assert H.check("src", 100, ledger_path=led, state_path=st)["verdict"] == "ok"
    assert "src" in H.report(ledger_path=led, state_path=st)


def test_health_ledger_unreadable_distinct_from_empty(tmp_path):
    """★ 审查 F4：账本不可读 ≠ 没有历史 ✓✗。"""
    led = tmp_path / "ledger_dir"
    led.mkdir()
    st = tmp_path / "state.json"
    assert H._read_ledger(led) is None
    assert H.check("src", 10, ledger_path=led, state_path=st)["verdict"] == "ledger_unreadable"
    assert "不可读" in H.report(ledger_path=led, state_path=st)


def test_health_corrupt_state_isolated(tmp_path):
    led, st = _hpaths(tmp_path)
    st.write_text("{not json", encoding="utf-8")
    assert H.register("src", state_path=st)["ok"]
    assert list(tmp_path.glob("state.corrupt.*.json"))   # 真隔离（旧断言恒真 ✗）


def test_health_two_corruptions_keep_both_backups(tmp_path):
    """★ 同一秒内二次损坏不得覆盖前一份证据 ✗（备份名曾只到秒级 ✗）。"""
    led, st = _hpaths(tmp_path)
    for i in range(2):
        st.write_text(f"{{broken{i}", encoding="utf-8")
        assert H.register("src", state_path=st)["ok"]
    assert len(list(tmp_path.glob("state.corrupt.*.json"))) == 2


def test_health_state_wrong_shape_isolated_not_silently_normalized(tmp_path):
    """★ 审查 C2：合法 JSON 但结构坏（sources 为 list / credentials 为 null）**必须隔离+报警** ✗✗
    —— 旧实现静默置空：登记与凭据日历一起消失，且 `expect_interval_h` 丢失会让陈旧规则静默关闭
    （同一条检查从 stale 退 1 变成 ok 退 0 —— 毁掉数据反而变绿 ✗✗）。"""
    led, st = _hpaths(tmp_path)
    st.write_text(json.dumps({"sources": ["a"], "credentials": None}), encoding="utf-8")
    assert H.register("src", state_path=st)["ok"]
    assert list(tmp_path.glob("state.corrupt.*.json"))                 # 真隔离 ✓
    v = H.check("src", 1, ledger_path=led, state_path=st)
    assert v["verdict"] == "state_recovered"                           # 非安心值 ✓
    assert "复核" in " ".join(v["reasons"])                            # 明确要求人工复核登记 ✓


def test_health_registered_but_never_recorded_alarms(tmp_path):
    """★ 审查 C1（静默失败猎手）：登记了"每 N 小时一次"却**从未落账** → 必须告警 ✗✗。
    旧实现落 no_baseline（安心值，退 0）——"采集器压根没跑/配额耗尽后静默停摆"正是这形态，
    健康体系却全绿 ✗✗。"""
    led, st = _hpaths(tmp_path)
    H.register("qimai_offline", expect_interval_h=24, state_path=st)
    fresh = H.check("qimai_offline", 2293, ledger_path=led, state_path=st)
    assert fresh["verdict"] == "no_baseline"                           # 刚登记：宽限期 ✓
    st.write_text(json.dumps({"sources": {"qimai_offline": {
        "expect_interval_h": 24, "allow_empty": False,
        "registered_at": (datetime.now() - timedelta(days=10)).isoformat(timespec="seconds")}},
        "credentials": []}), encoding="utf-8")
    v = H.check("qimai_offline", 2293, ledger_path=led, state_path=st)
    assert v["verdict"] == "no_record" and "无任何账本记录" in " ".join(v["reasons"])
    H.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)             # 给 CLI 用全局路径 ✓
    H.STATE_PATH.write_text(st.read_text(encoding="utf-8"), encoding="utf-8")
    assert H.main(["check", "qimai_offline", "--count", "2293"]) == 1  # 非零退出 ✓


def test_health_dirty_source_config_does_not_raise(tmp_path):
    """★ 审查 H6：`sources: {"s": "oops"}` 曾让 check()/report() 抛 AttributeError 裸栈 ✗。"""
    led, st = _hpaths(tmp_path)
    st.write_text(json.dumps({"sources": {"s": "oops"}, "credentials": []}), encoding="utf-8")
    v = H.check("s", 5, ledger_path=led, state_path=st)                # 不抛 ✓
    assert v["verdict"] == "config_invalid"
    assert "结构异常" in H.report(ledger_path=led, state_path=st)      # 报告也不崩 ✓


def test_health_record_repairs_crashed_partial_line(tmp_path):
    """★ 审查 H4：上次写盘崩溃留下的**无换行残行**会把新记录粘死 → 永久不可读，而 record 还报 ok ✗✗。"""
    led, st = _hpaths(tmp_path)
    led.write_text(json.dumps({"source": "src", "count": 100, "status": "ok"}), encoding="utf-8")
    r = H.record("src", count=2293, ledger_path=led)
    assert r["ok"] and "换行" in str(r.get("warning"))                  # 补换行并留痕 ✓
    assert H.baseline("src", ledger_path=led)["last_count"] == 2293     # 新记录读得回来 ✓


def test_health_record_fails_when_ledger_undecodable(tmp_path):
    """★ 审查 H5：账本混入非法 UTF-8（整体不可读）时 record 仍退 0 ✗ → 必须 ok:False ✓。"""
    led, st = _hpaths(tmp_path)
    led.write_bytes(b'{"source":"src","count":1}\n\xff\xfe bad bytes\n')
    r = H.record("src", count=5, ledger_path=led)
    assert r["ok"] is False and "不可解码" in r["error"]


def test_health_record_rejects_blank_source(tmp_path):
    led, st = _hpaths(tmp_path)
    assert H.record("", count=5, ledger_path=led)["ok"] is False       # 空 source 读不回来 ✗


def test_health_allow_empty_strict_bool(tmp_path):
    """★ 审查 L5：字符串 `"false"` 曾被当真 → 空结果判健康 ✗（只认布尔真 ✓）。"""
    led, st = _hpaths(tmp_path)
    st.write_text(json.dumps({"sources": {"src": {"allow_empty": "false"}},
                              "credentials": []}), encoding="utf-8")
    for _ in range(5):
        H.record("src", count=100, ledger_path=led)
    v = H.check("src", 0, ledger_path=led, state_path=st)
    assert v["verdict"] in H.ALARM_VERDICTS                            # 不被字符串骗过 → 告警侧 ✓
    assert any("allow_empty" in r for r in v["reasons"])               # 且明确点出配置异常 ✓
    assert any("count=0" in r for r in v["reasons"])                   # 空结果本身也照样报 ✓


def test_health_credential_warn_days_zero_respected(tmp_path):
    """★ 审查 L3：显式 `warn_days=0`（只想过期后提醒）曾被 `or 7` 吃成 7 ✗。"""
    led, st = _hpaths(tmp_path)
    soon = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d")
    assert H.add_credential("c", soon, warn_days=0, state_path=st)["warn_days"] == 0
    assert H.due_credentials(state_path=st) == []                      # 3 天后到期 + 窗口 0 → 不进日历 ✓


def test_health_no_count_does_not_mask_failing(tmp_path):
    """★ 审查 L2：count 脏值的提前返回曾把已算出的 failing 覆盖成 no_count ✗。"""
    led, st = _hpaths(tmp_path)
    for _ in range(3):
        H.record("src", count=100, ledger_path=led)
    v = H.check("src", "不是数字", status="error", ledger_path=led, state_path=st)
    assert v["verdict"] == "failing"                                   # 更高严重度保留 ✓


def test_health_aware_datetime_converted_to_local(tmp_path):
    """★ 审查 M2：带偏移的时间曾被丢掉偏移当本地时间（差 8h ✗）→ 必须换算 ✓。"""
    d = H._parse_dt("2026-10-14T10:00:00+00:00")
    assert d == datetime.fromtimestamp(datetime(2026, 10, 14, 10, 0,
                                                tzinfo=__import__("datetime").timezone.utc).timestamp())


def test_health_report_alarms_public_exit(tmp_path):
    """程序化调用方要能拿到告警清单（旧实现 report() 把 alarms 丢了 ✗）。"""
    led, st = _hpaths(tmp_path)
    assert H.report_alarms(ledger_path=led, state_path=st) == []        # 全空 → 无告警 ✓
    past = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    H.add_credential("vip", past, state_path=st)
    assert any("vip" in a for a in H.report_alarms(ledger_path=led, state_path=st))


def test_health_bad_ledger_lines_skipped(tmp_path):
    led, st = _hpaths(tmp_path)
    led.write_text("garbage\n" + json.dumps({"source": "src", "count": 7, "status": "ok"}) + "\n",
                   encoding="utf-8")
    assert H.baseline("src", ledger_path=led)["last_count"] == 7


def test_health_write_failures_return_not_ok(tmp_path):
    """★ 审查 F10/M8：写失败**不得**返回 ok:True ✓✗。"""
    led_dir = tmp_path / "ledger_dir"
    led_dir.mkdir()
    assert H.record("src", count=1, ledger_path=led_dir)["ok"] is False
    (tmp_path / "nodir").write_text("not a dir", encoding="utf-8")
    bad_state = tmp_path / "nodir" / "s.json"
    assert H.register("src", state_path=bad_state)["ok"] is False
    assert H.add_credential("vip", "2099-01-01", state_path=bad_state)["ok"] is False


def test_health_credentials_due_and_expired(tmp_path):
    led, st = _hpaths(tmp_path)
    soon = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d %H:%M")
    past = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
    H.add_credential("vip", soon, warn_days=7, state_path=st)
    H.add_credential("token", past, warn_days=7, state_path=st)
    due = H.due_credentials(state_path=st)
    assert len(due) == 2 and due[0]["name"] == "token" and due[0]["expired"] is True


def test_health_credential_own_warn_days_not_overridden(tmp_path):
    """★ 审查 I2/M7：凭据自设 warn_days 生效（旧实现被 report 默认 7 覆盖 ✗）。"""
    led, st = _hpaths(tmp_path)
    H.add_credential("vip", (datetime.now() + timedelta(days=20)).strftime("%Y-%m-%d"),
                     warn_days=30, state_path=st)
    assert len(H.due_credentials(state_path=st)) == 1                 # 用自设 30 天 ✓
    assert H.due_credentials(warn_days=7, state_path=st) == []        # 显式覆盖 ✓
    assert "vip" in H.report(state_path=st)                           # report 默认不覆盖 ✓


def test_health_credential_null_warn_days_no_raise(tmp_path):
    led, st = _hpaths(tmp_path)
    st.write_text(json.dumps({"sources": {}, "credentials": [
        {"name": "vip", "expires_at": "2099-01-01", "warn_days": None}]}), encoding="utf-8")
    assert isinstance(H.due_credentials(state_path=st), list)         # 不抛 ✓


def test_health_credential_bad_entry_reported(tmp_path):
    led, st = _hpaths(tmp_path)
    st.write_text(json.dumps({"sources": {}, "credentials": [
        {"name": "x", "expires_at": "not-a-date"}]}), encoding="utf-8")
    due = H.due_credentials(state_path=st)
    assert due and due[0].get("error")                                # 显式报异常条目 ✓
    assert "条目异常" in H.report(state_path=st)


def test_health_add_credential_bad_date(tmp_path):
    led, st = _hpaths(tmp_path)
    assert H.add_credential("x", "not-a-date", state_path=st)["ok"] is False


def test_health_register_rejects_blank(tmp_path):
    led, st = _hpaths(tmp_path)
    assert H.register("  ", state_path=st)["ok"] is False


def test_health_count_non_numeric_is_no_count(tmp_path):
    led, st = _hpaths(tmp_path)
    for _ in range(3):
        H.record("src", count=100, ledger_path=led)
    assert H.check("src", "N/A", ledger_path=led, state_path=st)["verdict"] == "no_count"


def test_health_state_json_list_isolated(tmp_path):
    """state 是合法 JSON 但顶层不是对象（如列表）→ 同样按损坏隔离 ✓。"""
    led, st = _hpaths(tmp_path)
    st.write_text("[1, 2, 3]", encoding="utf-8")
    assert H.register("src", state_path=st)["ok"]
    assert list(tmp_path.glob("state.corrupt.*.json"))


def test_health_report_marks_recovered_state(tmp_path):
    """隔离重建后，报告必须把"登记可能已丢"讲出来（否则用户以为登记还在 ✓✗）。"""
    led, st = _hpaths(tmp_path)
    st.write_text("{broken", encoding="utf-8")
    H.register("src", state_path=st)
    txt = H.report(ledger_path=led, state_path=st)
    assert "隔离重建" in txt and "复核" in txt


def test_health_due_credentials_non_dict_entry(tmp_path):
    led, st = _hpaths(tmp_path)
    st.write_text(json.dumps({"sources": {}, "credentials": ["oops", 42]}), encoding="utf-8")
    due = H.due_credentials(state_path=st)
    assert len(due) == 2 and all(d.get("error") for d in due)      # 不静默漏 ✓


# ================= health：CLI 退出码（白名单 ✓） =================
def test_health_cli_exit_codes(tmp_path, capsys):
    """CLI 走**模块级** LEDGER_PATH/STATE_PATH（fixture 已改到 tmp ✓），故直接记到那里。"""
    for _ in range(5):
        H.record("src", count=100, ledger_path=H.LEDGER_PATH)
    assert H.main(["check", "src", "--count", "100"]) == 0        # ok ✓
    assert H.main(["check", "src", "--count", "0"]) == 1          # 空结果 ✓
    assert H.main(["check", "src", "--count", "10"]) == 1         # 掉量也非零 ✓✓
    assert H.main(["check", "src", "--count", "99999"]) == 1      # 突增也非零 ✓✓
    assert H.main(["check", "src", "--count", "100", "--status", "error"]) == 1
    assert H.main(["check", "newsrc", "--count", "5"]) == 0       # no_baseline 是安心值 ✓
    assert H.main(["register", "src2", "--interval-h", "24"]) == 0
    assert H.main(["cred", "vip", "--expires", "2099-01-01"]) == 0
    assert H.main(["cred", "bad", "--expires", "nope"]) == 1
    assert H.main(["record", "src", "--count", "100"]) == 0


def test_health_cli_report_flags_expired(tmp_path, capsys):
    past = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    assert H.main(["cred", "vip", "--expires", past]) == 0
    assert H.main(["report"]) == 1                                # 已过期 → 非零 ✓


def test_health_cli_report_flags_expiring_within_window(tmp_path, capsys):
    """临期（未过期）也算要人工看 ✓ —— 七麦的教训就是"临到期才发现" ✗。"""
    soon = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d %H:%M")
    assert H.main(["cred", "vip", "--expires", soon, "--warn-days", "7"]) == 0
    assert H.main(["report"]) == 1


def test_health_cli_report_flags_source_alarm(tmp_path, capsys):
    """★ 审查：report 曾**完全没看源告警** ✗ —— 掉量/空结果也要退 1 ✓。"""
    for _ in range(10):
        H.record("src", count=1000, ledger_path=H.LEDGER_PATH)
    H.record("src", count=10, ledger_path=H.LEDGER_PATH)          # 末笔掉量 ✓
    assert H.main(["report"]) == 1
    led = H.LEDGER_PATH.read_text(encoding="utf-8")
    H.LEDGER_PATH.write_text("".join(l + "\n" for l in led.splitlines()[:-1]), encoding="utf-8")
    for _ in range(9):
        H.record("src", count=1000, ledger_path=H.LEDGER_PATH)     # 回到全正常 ✓
    assert H.main(["report"]) == 0


def test_health_cli_report_not_fooled_by_string_match(tmp_path, capsys):
    """★ 审查：退出码曾靠**字符串匹配**判 ✗ —— 源名带"不可读""条目异常"就会误报 ✓✗。"""
    H.record("不可读条目异常", count=100, ledger_path=H.LEDGER_PATH)
    assert H.main(["report"]) == 0                                # 全 ok → 必须退 0 ✓


def test_health_custom_drop_spike_thresholds(tmp_path):
    """阈值是参数，得真的生效（默认 0.5×/2× ✓）。"""
    led, st = _hpaths(tmp_path)
    for _ in range(5):
        H.record("src", count=100, ledger_path=led)
    assert H.check("src", 50, ledger_path=led, state_path=st)["verdict"] == "ok"       # 边界=不判 ✓
    assert H.check("src", 50, drop=0.9, ledger_path=led, state_path=st)["verdict"] == "volume_drop"
    assert H.check("src", 150, ledger_path=led, state_path=st)["verdict"] == "ok"
    assert H.check("src", 150, spike=1.2, ledger_path=led, state_path=st)["verdict"] == "volume_spike"


def test_health_window_changes_verdict(tmp_path):
    """★ 窗口决定基线：同一 count 在近 20 笔下判掉量、近 5 笔下正常 ✓。"""
    led, st = _hpaths(tmp_path)
    for _ in range(20):
        H.record("src", count=1000, ledger_path=led)
    for _ in range(5):
        H.record("src", count=100, ledger_path=led)
    assert H.check("src", 100, ledger_path=led, state_path=st)["verdict"] == "volume_drop"
    assert H.check("src", 100, window=5, ledger_path=led, state_path=st)["verdict"] == "ok"


# ================= CLI 接线（含"未知参数不得静默忽略"回归 ✓） =================
def _run_cli(monkeypatch, argv):
    from universal_scraper import cli as CLI
    monkeypatch.setattr(sys, "argv", ["universal-scraper", *argv])
    return CLI.main()


def test_cli_rejects_unknown_args_for_normal_subcommands(monkeypatch, capsys):
    """★ 回归：为透传 chain/health 改用 parse_known_args 后，**其它子命令的参数拼错
    必须仍然报错** ✗✗（否则 `run --confg x.json` 会带着默认值跑起来）。"""
    with pytest.raises(SystemExit) as e:
        _run_cli(monkeypatch, ["run", "--confg", "/tmp/nope.json"])
    assert e.value.code == 2
    assert "无法识别的参数" in capsys.readouterr().err


def test_cli_chain_unknown_args_reach_module_parser(monkeypatch, capsys):
    """chain 仍要透传（模块 parser 是唯一来源 ✓），且走 tmp 状态目录（fixture 隔离 ✓）。"""
    rc = _run_cli(monkeypatch, ["chain", "--step", "ok=/bin/echo hi", "--state-key", "cli_t"])
    assert rc == 0
    assert '"status": "done"' in capsys.readouterr().out


def test_cli_health_h_exits_zero(monkeypatch, capsys):
    """`health -h` 必须显示**模块自己的**子命令帮助（曾只显示空用法 ✗）。"""
    with pytest.raises(SystemExit) as e:
        _run_cli(monkeypatch, ["health", "-h"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "register" in out and "report" in out


def test_cli_chain_h_exits_zero(monkeypatch, capsys):
    with pytest.raises(SystemExit) as e:
        _run_cli(monkeypatch, ["chain", "-h"])
    assert e.value.code == 0
    assert "--deadline" in capsys.readouterr().out
