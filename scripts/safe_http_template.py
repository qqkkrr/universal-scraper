#!/usr/bin/env python3
"""安全 HTTP 采集模板（R102 汽车之家战报沉淀）——复制到任务目录后按需修改。

Mimosa 预适配：pathlib 写文件 / sha256 非安全哈希 / 白名单出站 / 列表参数子进程。
内建：请求预算硬闸 / 封禁退避 / 礼貌限速 / JSONL 追加写（断点续跑）。
与 universal-scraper 的 request_budget / verify --dir 无缝对接。
"""
from __future__ import annotations

import json
import time
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

# ─── 安全口径 ────────────────────────────────────────────────────────────────
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

# 出站白名单：只允许目标站 API 域（按任务修改）
_ALLOWED_HOSTS = {"www.autohome.com.cn", "api.autohome.com.cn",
                  "koubeiipv6.app.autohome.com.cn"}

# 请求预算硬闸（超过即停，防失控烧预算）
# OCR R131 反馈 #5：全局共享 outputs/.budget.json 曾跨任务串账——侦察消耗漏进
# 正式跑。改为按任务名分桶：BUDGET_TASK 环境变量（或模板复制时改）隔离各任务
BUDGET_TASK = os.environ.get("US_BUDGET_TASK", "default")
BUDGET_FILE = Path("outputs") / f".budget_{BUDGET_TASK}.json"
BUDGET_LIMIT = 4500


def _check_budget() -> bool:
    """预算闸：超过上限返回 False。读-判-写整个文件被 flock 串行化——
    并发进程同时读到相同 used 会双双放行（审查修复 H：docstring 曾声称
    "原子替换防并发"但 read→write 本身不原子，竞态下超额）。"""
    import fcntl
    used = 0
    BUDGET_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(f"{BUDGET_FILE}.lock", "a") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        if BUDGET_FILE.exists():
            try:
                used = json.loads(BUDGET_FILE.read_text()).get("used", 0)
            except Exception:
                pass
        if used >= BUDGET_LIMIT:
            return False
        # tmp+rename：读方永远看到完整 JSON
        _tmp = BUDGET_FILE.with_suffix(".json.tmp")
        _tmp.write_text(json.dumps({"used": used + 1, "limit": BUDGET_LIMIT}))
        os.replace(_tmp, BUDGET_FILE)
        return True


def _check_url(url: str) -> str:
    """出站守卫：白名单域 + http/https + 拒绝私网/环回。"""
    sp = urlsplit(url or "")
    if (sp.scheme or "").lower() not in ("http", "https") or not sp.hostname:
        raise ValueError(f"非法 URL: {url!r}")
    import ipaddress
    try:
        ip = ipaddress.ip_address(sp.hostname)
    except ValueError:
        pass  # 域名
    else:
        if ip.is_private or ip.is_loopback or ip.is_reserved:
            raise ValueError(f"私网/环回地址已拒绝: {url!r}")
    if sp.hostname not in _ALLOWED_HOSTS:
        raise ValueError(f"非白名单域，已拒绝: {sp.hostname}（允许: {_ALLOWED_HOSTS}）")
    return url


def _safe_write(path: Path, data: str, mode: int = 0o644) -> None:
    """临时文件 + os.replace 原子替换（并发/中断下的读者永远见完整内容；
    OCR R131（M）：docstring 曾声称原子写，实现却是直接 open+truncate）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _safe_append_jsonl(path: Path, record: dict) -> None:
    """JSONL 追加写（断点续跑友好，每行独立）。"""
    import os
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


# ─── 礼貌 HTTP 客户端 ─────────────────────────────────────────────────────────
import curl_cffi.requests as _cf_requests

_session = None
_last_ts = 0.0
MIN_INTERVAL = 1.2   # 秒；单并发礼貌限速
_BACKOFF = 2.0       # 封禁退避基数


def _get_session():
    global _session
    if _session is None:
        _session = _cf_requests.Session(impersonate="chrome")
    return _session


def _polite_get(url: str, headers: dict = None, timeout: int = 20) -> dict:
    """礼貌 GET：限速 + 预算闸 + 封禁退避。返回 {ok, status, text, json}。"""
    global _last_ts
    _check_url(url)
    if not _check_budget():
        raise RuntimeError(f"请求预算耗尽（{BUDGET_LIMIT}）——停止采集")

    wait = MIN_INTERVAL - (time.time() - _last_ts)
    if wait > 0:
        time.sleep(wait)

    s = _get_session()
    h = {"User-Agent": _UA}
    if headers:
        h.update(headers)
    # 审查修复（CRITICAL）：allow_redirects=True 曾绕过 _check_url——白名单域 302
    # 到内网/任意地址即穿透。禁自动跟随，手动逐跳复检（每跳重新过 _check_url）
    for _hop in range(5):
        resp = s.get(url, headers=h, timeout=timeout, allow_redirects=False)
        if resp.status_code in (301, 302, 303, 307, 308):
            _loc = resp.headers.get("Location", "")
            if not _loc:
                break
            from urllib.parse import urljoin
            url = urljoin(url, _loc)
            _check_url(url)  # 每跳复检：非白名单/私网重定向目标直接拒绝
            continue
        break
    _last_ts = time.time()
    status = resp.status_code
    text = resp.text

    # 封禁退避（403/412/429）：按封锁类型定档，不做 status%10 的伪分级
    # （OCR R131（M）：`2 ** min(3, status%10)` 对 403/412/429 恒等于 2^3=8 倍——
    #  公式从未真正按状态分级过，现在显式定档；上限 60s 防任务被拖死）
    if status in (403, 412, 429):
        _tier = {"403": 1, "412": 2, "429": 3}.get(str(status), 1)
        backoff = min(_BACKOFF * (2 ** _tier), 60.0)
        print(f"⚠️ HTTP {status}——退避 {backoff:.0f}s 后继续", flush=True)
        time.sleep(backoff)
        return {"ok": False, "status": status, "text": text[:200]}

    try:
        data = resp.json()
    except Exception:
        data = None
    return {"ok": 200 <= status < 300, "status": status, "text": text, "json": data}


# ─── 日期口径预检（OCR R131 反馈 #4：任务日期已过是高频坑，机械化前置 30 秒解决）───
def check_task_date(*dates: str, presale_days: int = 15) -> dict:
    """检查任务日期是否已过或超出预售窗口。在写采集脚本前调用，30 秒排掉高频坑。
    dates: 一个或多个 "YYYY-MM-DD" 日期字符串（乘车日/出发日等）。
    presale_days: 12306 等预售窗口天数（当前 15 天，部分渠道 30 天）。
    返回 {"ok": bool, "warnings": [str], "dates": [{date, status, detail}]}
    """
    from datetime import date as _date
    out = {"ok": True, "warnings": [], "dates": []}
    today = _date.today()
    seen = set()
    for d in dates:
        d = d.strip()
        if d in seen:
            continue
        seen.add(d)
        try:
            td = _date.fromisoformat(d.replace("/", "-").replace(".", "-"))
        except ValueError:
            out["dates"].append({"date": d, "status": "invalid", "detail": "无法解析为 YYYY-MM-DD"})
            out["ok"] = False
            continue
        if td < today:
            out["dates"].append({"date": d, "status": "past",
                                 "detail": f"已过去 {(today - td).days} 天——历史数据无法实时查询，"
                                           f"请确认你要的是实时余票还是历史归档"})
            out["warnings"].append(f"日期 {d} 已过去")
            out["ok"] = False
        elif (td - today).days > presale_days:
            out["dates"].append({"date": d, "status": "beyond_presale",
                                 "detail": f"距今 {(td - today).days} 天，超出常见预售窗口 {presale_days} 天——"
                                           f"接口可能暂无数据，建议临近再试或确认预售期"})
            out["warnings"].append(f"日期 {d} 超出预售窗口")
        else:
            out["dates"].append({"date": d, "status": "ok", "detail": f"距今 {(td - today).days} 天，在预售窗口内"})
    return out


# ─── 数据可用性探测（实战反馈四#1，aqistudy：接口全打通才发现历史深度只有 1 年——
#     前半条链路白搭。接口打通后 30 秒探测任务时间窗可查性，再决定写不写全量采集器）───
def check_data_coverage(probe_fn, dates: list, expect: str = "") -> dict:
    """任务时间窗数据深度探测：对代表日期逐点实查，返回可查性结论。

    probe_fn: 单点查询函数 f(date_iso: str) -> {"ok": bool, "hit": bool, "note": str}。
              hit=False = 该日期无数据（接口通但没这笔账）。异常按 miss 记。
    dates:    ["YYYY-MM-DD", ...] 任务窗口的代表日期（调用方给 start/end/中间点；
              建议首/尾/最大深度三点，日期多时自动二分）。
    expect:   可选命中标记（resp 里应出现的站名/字段名），hit 判定参考。

    返回 {"ok": bool, "verdict": str, "points": [{date, status, detail}]}
    ok=False 时**先停**：别给一个只有半截历史的任务写全量采集器。
    """
    from datetime import date as _d
    out = {"ok": True, "verdict": "", "points": []}
    clean = []
    for x in dates:
        try:
            clean.append(_d.fromisoformat(str(x).replace("/", "-")).isoformat())
        except ValueError:
            out["points"].append({"date": str(x), "status": "invalid", "detail": "无法解析"})
    for d in clean:
        try:
            r = probe_fn(d) or {}
            hit = bool(r.get("hit"))
            if expect and r.get("text"):
                hit = hit and (expect in str(r["text"]))
            out["points"].append({"date": d, "status": "hit" if hit else "miss",
                                  "detail": str(r.get("note", ""))[:120]})
        except Exception as e:
            out["points"].append({"date": d, "status": "error",
                                  "detail": f"{type(e).__name__}: {str(e)[:80]}"})
    misses = [p["date"] for p in out["points"] if p["status"] in ("miss", "error", "invalid")]
    if out["points"] and not misses:
        out["verdict"] = "探点全部命中——时间窗可查，可写全量采集器"
    elif out["points"] and len(misses) < len(out["points"]):
        out["ok"] = False
        out["verdict"] = (f"部分探点无数据（{', '.join(misses)}）——数据深度可能早于预期，"
                          "先二分定位可查边界，再按边界收敛任务窗口")
    else:
        out["ok"] = False
        out["verdict"] = "全部探点无数据——接口虽通但无此时间窗数据（或参数口径不对），勿写全量采集器"
    return out


# ─── 同页双抓 diff（实战反馈四#2，aqistudy：数值列是随机干扰值，靠复核才抓出来。
#     交付前同一 URL 抓两次比对——数值列变了 = 干扰值。极低成本的通用检测）───
def double_fetch_diff(url: str, fetch_fn=None, headers: dict | None = None) -> dict:
    """同 URL 连抓两次，返回两次响应的关键差异摘要。

    fetch_fn: f(url, headers) -> {"text": str}；缺省复用本模板已带 SSRF 守卫/
    预算/退避的 _polite_get（勿在此新增裸请求）。
    返回 {"same_len": bool, "same": bool, "changed_spans": int, "verdict": str}
    same=False 且差异集中在数值段 → 强烈提示干扰值/动态噪声，交付前必须换源或
    抓页面上下文解密（见 R39）。
    """
    if fetch_fn is None:
        def fetch_fn(u, headers=None):
            r = _polite_get(u, headers=headers, timeout=20)
            return {"text": r.get("text", "") or "", "error": "" if r.get("ok") else f"HTTP {r.get('status')}"}
    r1 = fetch_fn(url, headers) or {}
    r2 = fetch_fn(url, headers) or {}
    # 审查五轮（MED）：抓取失败曾照样比对——两次同样拿到 403 拦截页被判
    # "两次一致——静态值"（质检假通过，比假失败危害大）。任一失败即无法比对
    _e1 = str(r1.get("error") or "")
    _e2 = str(r2.get("error") or "")
    if _e1 or _e2 or (r1.get("text") is None) or (r2.get("text") is None):
        return {"same_len": False, "same": None, "changed_spans": None,
                "verdict": (f"两次抓取未成功（{_e1 or _e2 or 'text 缺失'}）——无法比对，"
                            "先解决可用性再做干扰值检测")}
    t1, t2 = str(r1["text"]), str(r2["text"])
    same_len = len(t1) == len(t2)
    changed = 0
    if not same_len:
        changed = -1  # 长度都不同：结构级差异（模板/广告位），干扰值只是其一
    else:
        # 等长时数差异字符段（块级聚合，防逐字符刷屏）
        i = 0
        in_span = False
        while i < len(t1):
            if t1[i] != t2[i]:
                if not in_span:
                    changed += 1
                    in_span = True
            else:
                in_span = False
            i += 1
    same = (t1 == t2)
    if same:
        verdict = "两次一致——静态值"
    elif changed == -1:
        verdict = "两次长度不同——结构级动态内容（模板/时间戳/广告位），关注数据列是否稳定"
    else:
        verdict = (f"两次有 {changed} 处差异（等长）——若差异落在数值列，为随机干扰值，"
                   "交付前必须换源或改走页面上下文解密（R39）")
    return {"same_len": same_len, "same": same, "changed_spans": changed, "verdict": verdict}


# ─── verify 对接（交付前审计）────────────────────────────────────────────────
def export_for_verify(out_dir: Path, name: str, rows: list) -> Path:
    """导出 JSON 供 `us verify --dir` 审计（一条记录一行）。"""
    _safe_write(out_dir / f"{name}.json",
                json.dumps(rows, ensure_ascii=False, indent=1, default=str))
    return out_dir / f"{name}.json"


# ─── 示例：汽车之家车型详情抓取 ──────────────────────────────────────────────
if __name__ == "__main__":
    out = Path("outputs/ah_demo")
    print(f"预算: {BUDGET_LIMIT} | 白名单: {_ALLOWED_HOSTS}")
    print("复制本文件到任务目录后按需修改 _ALLOWED_HOSTS / BUDGET_LIMIT / MIN_INTERVAL")
