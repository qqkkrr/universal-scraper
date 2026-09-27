#!/usr/bin/env python3
"""万能爬虫框架核心（zero-dependency，纯标准库）。

设计理念：几乎所有"爬虫场景"都可以拆成 4 件事 ——
  1) 取数据（HTTP 静态页 / JSON API / 需浏览器渲染的页面）
  2) 反爬应对（限速、重试、UA 轮换、代理、浏览器指纹）
  3) 遍历（分页 / 列表→详情）
  4) 导出（CSV / JSON / Excel）

本模块提供这些基础能力，具体站点只需写一个很小的"适配器"。
"""
from __future__ import annotations

from typing import NoReturn

import csv
import gzip
import base64
import hashlib
import os
import threading
import zlib
import json
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------- 常量
CACHE_DEFAULT_TTL = 86400.0      # HTTP 响应缓存默认有效期（秒，24h）
CACHE_MAX_FILES = 2000           # 缓存文件上限（超限删最旧）
DEFAULT_MAX_BODY = 20 * 1024 * 1024  # 默认响应体上限（20MB，流式限读）


def _budget_auto_mark(url: str, status: int, body=None, hours: float = 24.0):
    """batch1600 战训：被 403/412/421/52x 拒绝时自动记账域名封锁台账（跨运行可查）。
    幂等：同域已冷却中则直接跳过（不刷新时间戳也不写盘）；任何异常静默
    （台账绝不拖垮抓取）。注：在首次拒绝即记账（保守口径），后续成功不回滚——
    台账是建议性信息，供 budget --list / doctor 参考，不拦截请求。

    NMPA 战训修复（2026-09-10）：body 传响应体前缀；瑞数/Cloudflare 类
    "通道拦截"不记账不烧预算——那不是该域配额耗尽，是当前 HTTP 通道过不了，
    记账会误黑名单整个站（瑞数站换真实浏览器即可过）。处方每域每次进程只喊一次。
    签名陷阱修复：body 提到 hours 前面——本函数曾被测试以位置参数传 body，
    全部落进 hours 静默判空（审查复盘发现），位置传参必错不如把常用参数放前。"""
    try:
        from urllib.parse import urlsplit
        host = urlsplit(url).hostname or ""
        if not host:
            return
        # 响应体判型：命中免记账的通道拦截类型 → 大声给处方，跳过记账。
        # 412 特殊口径（审查修复）：瑞数标准应答是 412，但未命中签名的 412 变体
        # 也不能记账——412 在实践中几乎必是 challenge 页，记账必误伤整站
        try:
            from .diagnose import classify_block
            if isinstance(body, (bytes, bytearray)):
                text = body.decode("utf-8", errors="replace")
            elif body is not None:
                text = str(body)
            else:
                text = ""
            verdict = classify_block(int(status), text)
            if verdict.get("is_block") and verdict.get("burns_budget") is False:
                with _DIAG_LOCK:  # OCR R131（M）：check-then-add 竞态曾双份提示
                    _first = host not in _CHANNEL_HINT_LOGGED
                    _CHANNEL_HINT_LOGGED.add(host)
                if _first:
                    log(f"🧬 {verdict['name']}识别（{host}）：HTTP {status} 是通道拦截、非配额耗尽，"
                        f"不记封锁台账。处方: {verdict['prescription']}", "WARN")
                return
            if int(status) == 412:
                with _DIAG_LOCK:
                    _first = host not in _CHANNEL_HINT_LOGGED
                    _CHANNEL_HINT_LOGGED.add(host)
                if _first:
                    log(f"🧬 HTTP 412（{host}）：challenge 类拦截（未命中已知签名，"
                        f"可能是不认识的瑞数变体），不记封锁台账。"
                        f"处方: 先 `cli diagnose <url>` 机器判型再选通道", "WARN")
                return
        except ImportError:
            pass  # 裁剪版（Lite）无 diagnose 模块：静默跳过判型，直接保守记账
        except Exception as e:
            # 审查修复：曾静默 pass——判型一挂就无声回退记账，正是误伤事故的复燃路径
            try:
                log(f"判型异常，回退保守记账（{host}）: {type(e).__name__}: {str(e)[:120]}", "WARN")
            except Exception:
                pass
        from . import domain_budget as _db
        if _db.check(host)["in_cooldown"]:
            return
        _db.mark(host, hours=hours, note=f"HTTP {status} 自动记账")
    except Exception as e:
        # OCR R131（L）：记账链路整体炸掉曾全静默——best-effort 但留一行痕
        # （台账故障不影响抓取主流程的既有设计不变）
        try:
            log(f"自动记账失败（{host}，HTTP {status}）: {type(e).__name__}: {str(e)[:80]}", "WARN")
        except Exception:
            pass


# 每进程每域只提示一次"通道拦截 vs 配额"的区分（防刷屏）
# OCR R131（M）：set 的 contains/add 竞态曾让并发首漏打双份提示——统一持锁
_CHANNEL_HINT_LOGGED = set()
_DIAG_LOCK = threading.Lock()


# batch2200 战训（P3）：跨客户端共享的连续网络失败计数——
# 达到阈值时升级为一次性的"网络路径可能已变化"大声提示（每次连击只提示一次）
_NET_FAIL_STREAK = {"count": 0, "warned_at": 0}


def _note_net_result(ok: bool):
    """每次网络请求结束后调用：ok=False 累计连击，ok=True 清零。"""
    # OCR R131（M）：计数与 30min 告警闸并发下曾双告警/漏计数——串行化
    with _DIAG_LOCK:
        if ok:
            _NET_FAIL_STREAK["count"] = 0
            return
        _NET_FAIL_STREAK["count"] += 1
        # 审查修复（P2）：原条件 count == 3 and 超时30min 存在双死角——持续失败
        # （4,5,6...）永远不再告警，30 分钟内的新连击告警一次后也不再提示。
        # 改为每 3 次失败且距上次告警超 30 分钟才提示
        if _NET_FAIL_STREAK["count"] % 3 == 0 and time.time() - _NET_FAIL_STREAK["warned_at"] > 1800:
            _NET_FAIL_STREAK["warned_at"] = time.time()
            log("🌐 连续 3 次网络失败——出口/系统代理可能已变化（如退出 Clash/切换 VPN），"
                "建议运行 scripts/doctor.py 的网络链路体检复查", "WARN")


# ---- 任务级请求预算（NBS 考核战训：汇总"请求 0"不可信——计数器只读不写；
# "总请求数 ≤8"这类硬约束必须可审计、可硬闸） ----
# 唯一真源：三个 HTTP 客户端的 _throttle() 在每次"真实发出尝试"前调用，
# 在此累计——重试各计 1 次（每次都是真实请求，配额语义必须按 attempt 计）。
_HTTP_REQUEST_STATS = {"count": 0, "limit": 0}
_REQUEST_STATS_LOCK = threading.Lock()


class MaxRequestsExceeded(BaseException):
    """任务级请求预算硬闸触发（--max-requests / anti_bot.max_requests）。

    故意继承 BaseException：客户端重试循环普遍 `except Exception` 兜底，
    若用 RuntimeError 会被吞掉后原样重试，硬闸形同虚设。
    只允许在 engine/cli 边界显式捕获转译为正常返回。"""


def set_request_budget(limit: int) -> None:
    """设置本次任务的请求预算（0=不限）并清零计数。run/auto/agent 入口各调一次。"""
    with _REQUEST_STATS_LOCK:
        _HTTP_REQUEST_STATS["count"] = 0
        _HTTP_REQUEST_STATS["limit"] = max(0, int(limit or 0))


def request_budget() -> Dict[str, int]:
    """当前请求预算状态：used / limit / remaining（limit=0 时 remaining=-1 表示不限）。"""
    with _REQUEST_STATS_LOCK:
        used, limit = _HTTP_REQUEST_STATS["count"], _HTTP_REQUEST_STATS["limit"]
    return {"used": used, "limit": limit,
            "remaining": (limit - used) if limit else -1}


def _bump_request_count() -> None:
    """真实 HTTP 尝试计入任务预算；超限抛 MaxRequestsExceeded。

    收官三轮（审查 M）：被拦截的那次尝试此前也 +1 计数（先加后判）——
    汇总 budget.used 比 audit 实收行数多 1。改为先判后加：被拦截的
    尝试不计数（服务端没收到），只作为闸门信号。"""
    with _REQUEST_STATS_LOCK:
        limit = _HTTP_REQUEST_STATS["limit"]
        if limit and _HTTP_REQUEST_STATS["count"] >= limit:
            raise MaxRequestsExceeded(
                f"请求次数已达上限 {limit}（--max-requests / anti_bot.max_requests）——"
                f"如确需更多请求请调大预算，而不是反复重试")
        _HTTP_REQUEST_STATS["count"] += 1


# ---- 自适应限速（深度改进②，对标 Scrapy AutoThrottle）----
# 自适应降速此前只活在 v3 HttpFetcher 一层，v1 路径 / api_session / 脚本直用
# 客户端都享受不到。现在状态挂在 HTTP 客户端实例上，三后端共用同一实现。
#
# 信号写入契约（唯一写入口，防双重计数——E2E 实证 fetcher 与客户端各记一次
# 会让 403 单请求间隔翻 4 倍）：
#   - core 客户端 = HTTP 状态级信号唯一写者：429/403/5xx → note_block，
#     成功 → note_ok（静默，不打日志；事件落在 last_block / last_restore）
#   - fetcher 层 = 只补客户端看不见的内容级信号（status==200 的风控皮），
#     并负责播报（读事件后清零，不重复计数）
# client.min_interval 保持为"用户基准值"（唯一真源，运行时不再被改写），
# 自适应后的当前间隔由 self._at.current 承载，_throttle() 按 current 睡眠。
class AdaptiveThrottle:
    """自适应限速状态机（每个 HTTP 客户端实例一个，三后端共用同一实现）。

    - note_block(kind)：拦截信号 → 间隔翻倍（floor 起步、cap 封顶），成功连击清零；
      基准已高于 cap 的慢任务不放缓（nxt<=cur 直接不动）
    - note_ok()：连续成功 speedup_streak 次 → 间隔向基准收敛一半（防抖，不一步回原速）；
      返回新间隔（未变化返回 0，调用方据此决定是否播报）
    记账口径：**每次真实 HTTP 尝试各记一次** block/延迟。三后端的 429 策略不同
    （urllib 无 Retry-After 时内部退避重试故可能记多次；requests/curl_cffi 收到
    429 立即交还引擎调度故只记一次）——口径一致，差异在重试策略本身。
    线程安全：note_* 由请求线程在响应后调用，wait_seconds 在 _throttle 锁内调用，
    各自持独立小锁（不与客户端 _throttle_lock 嵌套，避免锁序问题）。"""

    def __init__(self, base_interval: float, enabled: bool = True,
                 floor: float = 2.0, cap: float = 8.0, speedup_streak: int = 3):
        self.base = max(float(base_interval or 0.0), 0.0)
        self.enabled = bool(enabled)
        self.floor = float(floor)
        self.cap = float(cap)
        self.speedup_streak = max(1, int(speedup_streak))
        self.ok_streak = 0
        self.current = self.base
        self.last_restore = 0.0    # 最近一次恢复的新间隔（供 fetcher 层播报后清零）
        self.last_block = 0.0      # 最近一次拦截后的新间隔（客户端静默写，fetcher 播报后清零）
        self._slow_streak = 0
        self._lock = threading.Lock()

    def note_block(self, kind: str = "") -> float:
        with self._lock:
            self.ok_streak = 0
            self._slow_streak = 0  # 拦截事件重置慢响应计数（防跨事件残留放大）
            if not self.enabled:
                return 0.0
            cur = self.current
            nxt = min(max(cur * 2.0, self.floor), max(self.cap, cur))
            if nxt <= cur + 1e-9:
                return 0.0
            self.current = nxt
            self.last_block = nxt
            return nxt

    def note_ok(self) -> float:
        with self._lock:
            if not self.enabled:
                return 0.0
            self.ok_streak += 1
            # 注意：这里不清 _slow_streak——调用方对同一响应成对调用
            # note_latency+note_ok（先记后清会把慢计数永远归零，×1.5 机制灭活）；
            # 慢计数的清零只发生在 note_latency 的快响应分支与 note_block。
            if self.ok_streak >= self.speedup_streak and self.current > self.base + 1e-9:
                self.current = max((self.current + self.base) / 2.0, self.base)
                self.ok_streak = 0
                self.last_restore = self.current
                return self.current
            return 0.0

    def note_latency(self, latency: float, slow_floor: float = 5.0) -> float:
        """延迟感知降速（Scrapy AutoThrottle 补充，2026-09 精读采纳）：
        连续慢响应在被风控掐住之前就温和放缓。3 连超阈值 → 间隔 ×1.5
        （仍受 cap 约束，复用 last_block 播报通道）。成功快响应清零计数。"""
        with self._lock:
            # base=0（min_interval=0）也保留延迟感知：慢阈值退化为纯 slow_floor。
            # 此前 `self.base <= 0` 门把延迟感知关死而 note_block 仍活跃（门控不对称）。
            if not self.enabled:
                return 0.0
            thr = max(slow_floor, self.base * 3.0)
            if float(latency or 0.0) >= thr:
                self._slow_streak += 1
                if self._slow_streak >= 3:
                    self._slow_streak = 0
                    cur = self.current
                    nxt = min(max(cur * 1.5, cur + 0.5), max(self.cap, cur))
                    if nxt > cur + 1e-9:
                        self.current = nxt
                        self.last_block = nxt
                        return nxt
            else:
                self._slow_streak = 0
            return 0.0

    def wait_seconds(self) -> float:
        """本次请求前应等待的间隔（_throttle 消费）。禁用时回落静态基准。"""
        return self.current if self.enabled else self.base

    def pop_block(self) -> float:
        """取走并清除待播报的拦截事件（锁内原子读清，多 worker 不丢不重）。"""
        with self._lock:
            v = self.last_block
            self.last_block = 0.0
            return v

    def pop_restore(self) -> float:
        """取走并清除待播报的恢复事件（锁内原子读清，多 worker 不丢不重）。"""
        with self._lock:
            v = self.last_restore
            self.last_restore = 0.0
            return v


def _norm_cookies(val: Any) -> Dict[str, str]:
    """cookies 容忍 dict 或 "k=v; k2=v2" 串（cookies 命令导出的即串）。
    文档曾按串教用户填写、实现却按 dict 消费导致必崩——两侧在此统一。"""
    if not val:
        return {}
    if isinstance(val, dict):
        return {str(k): str(v) for k, v in val.items()}
    if isinstance(val, str):
        out: Dict[str, str] = {}
        for part in val.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            k, _, v = part.partition("=")
            if k.strip():
                out[k.strip()] = v.strip()
        return out
    return {}

# ---------------------------------------------------------------- 日志

def log(msg: str, level: str = "INFO") -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] [{level}] {msg}", file=sys.stderr, flush=True)


def safe_fname(name: str) -> str:
    """任务名/会话名 → 安全文件名（R91 修复 P2）：路径分隔符与越权字符替换为
    _——../ 逃逸曾让登录态 storageState/日志/seen/spool 写到 out_dir 之外。"""
    import re as _re
    return _re.sub(r"[^\w\u4e00-\u9fff.-]", "_", str(name or ""))


def die(msg: str) -> "NoReturn":
    log(msg, "ERROR")
    raise SystemExit(1)


# ---------------------------------------------------------------- 相对时间解析
def parse_relative_time(text: str, now: Optional[datetime] = None) -> str:
    """国内站相对时间 → 绝对日期字符串（R48 改进）。

    支持格式：
      "3天前" / "昨天 19:37" / "编辑于 08-31" / "2小时前" / "刚刚"
      "07-16"（自动补当年） / "2024-03-15"（已是绝对日期，原样返回）
      "编辑于 2天前 浙江"（尾部 IP 归属地自动去除）

    返回 "YYYY-MM-DD HH:MM" 或 "YYYY-MM-DD"（时间不明确时只到日）。
    解析失败返回空字符串。
    """
    import re as _re
    from datetime import timedelta as _td

    if not text or not text.strip():
        return ""
    now = now or datetime.now()
    s = text.strip()
    s = _re.sub(r"^(编辑于|发布于|发表于)\s*", "", s)
    s = _re.sub(r"\s+[^\s]{2,4}$", "", s) if _re.search(r"\d", s) else s
    s = s.strip()

    m = _re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        h, mi = int(m.group(4) or 0), int(m.group(5) or 0)
        try:
            r = datetime(y, mo, d, h, mi)
            return r.strftime("%Y-%m-%d %H:%M") if (h or mi) else r.strftime("%Y-%m-%d")
        except ValueError:
            return ""

    m = _re.match(r"^(\d{1,2})[-月/](\d{1,2})[日]?$", s)
    if m:
        mo, d = int(m.group(1)), int(m.group(2))
        try:
            r = datetime(now.year, mo, d)
            if r > now + _td(days=1):
                r = r.replace(year=now.year - 1)
            return r.strftime("%Y-%m-%d")
        except ValueError:
            return ""

    m = _re.match(r"^(?:刚刚|(\d+)\s*(秒|分钟|小时|天|周|个月|月|年)前?)", s)
    if m:
        n = int(m.group(1) or 0)
        unit = m.group(2) or ""
        delta = {"": 0, "秒": n, "分钟": n * 60, "小时": n * 3600,
                 "天": n * 86400, "周": n * 604800,
                 "个月": n * 2592000, "月": n * 2592000, "年": n * 31536000}.get(unit, 0)
        r = now - _td(seconds=delta)
        return r.strftime("%Y-%m-%d %H:%M")

    m = _re.match(r"^(昨天|前天)(?:\s+(\d{1,2}):(\d{2}))?$", s)
    if m:
        offset = 1 if m.group(1) == "昨天" else 2
        r = (now - _td(days=offset))
        h, mi = int(m.group(2) or 0), int(m.group(3) or 0)
        return r.replace(hour=h, minute=mi).strftime("%Y-%m-%d %H:%M")

    m = _re.match(r"^(\d{1,2}):(\d{2})$", s)
    if m:
        return now.strftime("%Y-%m-%d") + f" {s}"

    return ""


# ---------------------------------------------------------------- 商品/推广关键词提取
PROMO_KEYWORDS_RE = None  # 延迟编译


def extract_promo(text: str) -> list:
    """从文本中提取商品/课程/推广相关句子（R50 改进——内置管线）。"""
    global PROMO_KEYWORDS_RE
    if PROMO_KEYWORDS_RE is None:
        PROMO_KEYWORDS_RE = re.compile(
            r"(课程|训练营|教程|私教|一对一|咨询|服务|报名|优惠|折扣|限时|拼团|"
            r"链接在评论区|评论区自取|主页橱窗|橱窗|商品|小店|带货|好物体验|"
            r"赞助|合作|广告|推广|商务合作|品牌合作)",
            re.IGNORECASE)
    if not text or not PROMO_KEYWORDS_RE.search(text):
        return []
    sentences = re.split(r"[。！？\n]", text)
    return [s.strip()[:120] for s in sentences if s.strip() and PROMO_KEYWORDS_RE.search(s)]


# ---------------------------------------------------------------- HTTP 客户端

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

UA_POOL = [
    DEFAULT_UA,
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]


def _decode_body(raw: bytes, headers: Optional[Dict[str, str]] = None) -> str:
    """智能解码（对标 charset_normalizer / trafilatura 编码探测）：
    按 BOM / Content-Type / <meta charset> 判断；声明编码解码质量差或为
    latin-1 等"万能可解码"编码时，自动在 utf-8/gb18030/gbk/big5 等候选里
    选"替换字符最少"的解码（修复 GBK 中文站乱码 / 错标 charset 的站）。"""
    if not raw:
        return ""
    if isinstance(raw, str):
        return raw  # 已解码的字符串直接返回（防调用方误传 str 崩溃）
    if raw.isascii():
        return raw.decode("ascii")  # 纯 ASCII 快路径：任何编码下结果相同（JSON/API 大头）
    enc = "utf-8"
    explicit = False
    ct = (headers or {}).get("content-type", "") or (headers or {}).get("Content-Type", "")
    m = re.search(r"charset=([\w-]+)", ct, re.I)
    if m:
        enc = m.group(1); explicit = True
    elif raw[:3] == b"\xef\xbb\xbf":
        enc = "utf-8-sig"
    else:
        head = raw[:2048].decode("utf-8", "ignore").lower()
        m2 = re.search(r"charset=[\"']?([\w-]+)", head)
        if m2:
            enc = m2.group(1); explicit = True

    def _score(e):
        try:
            # OCR R131（M）回退注记：曾试改 64KB 采样打分——采样边界把多字节
            # UTF-8 字符截成半截，utf-8 伪增错误数、gb18030 反超胜出（实测
            # 中文页全量乱码）。编码探测必须全量解码，保持原实现。
            s2 = raw.decode(e, "replace")
            return s2.count("\ufffd"), s2
        except LookupError:
            return 10 ** 9, None

    # 候选打分：替换字符最少者胜（顺序决定同分时优先：utf-8 → gb18030 → gbk …）
    best_err, best_s = 10 ** 9, None
    for cand in ("utf-8", "gb18030", "gbk", "big5", "shift_jis", "latin-1"):
        e, s2 = _score(cand)
        if s2 is not None and e < best_err:
            best_err, best_s = e, s2
    # charset_normalizer 仅作为"额外候选"参与打分（不作为权威，防误判）
    try:
        from charset_normalizer import from_bytes
        _c = from_bytes(raw[:65536]).best() if raw else None  # 只采样探测，控制 CPU 开销
        if _c is not None and _c.encoding:
            e, s2 = _score(_c.encoding)
            if s2 is not None and e < best_err:
                best_err, best_s = e, s2
    except Exception as _e:
        log(f"  charset_normalizer 探测失败: {_e}", "DEBUG")

    single_byte = (enc.lower() in ("latin-1", "latin1", "ascii", "iso-8859-1", "windows-1252", "cp1252")
                   or enc.lower().startswith("iso-8859") or enc.lower().startswith("windows-125")
                   or enc.lower().startswith("cp125"))
    if not explicit:
        return best_s if best_s is not None else raw.decode("utf-8", "replace")
    if single_byte:
        # latin-1 等"万能解码"：若候选能零错误解出（如实际是 UTF-8/GBK），优先用候选
        if best_err == 0 and best_s is not None:
            return best_s
        return raw.decode(enc, "replace")
    try:
        return raw.decode(enc, "strict")
    except (UnicodeDecodeError, LookupError):
        pass
    decl_err, _ = _score(enc)
    if best_s is not None and best_err < max(1, decl_err // 2):
        return best_s
    return raw.decode(enc, "replace")


def assert_http_url(url: str) -> str:
    """出站 URL scheme 校验：仅允许 http/https（防 file:/ftp: 等伪协议读取本地资源）。
    合法返回原 URL，否则抛 ValueError。"""
    scheme = (urllib.parse.urlsplit(url or "").scheme or "").lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"仅支持 http/https 协议，已拒绝: {str(url)[:60]!r}")
    return url


def smart_decode(raw: bytes, headers: Optional[Dict[str, str]] = None) -> str:
    """公开别名：智能解码（BOM/声明/候选打分）。"""
    return _decode_body(raw, headers)


# ---------------------------------------------------------------- Cookie 工具
# 多后端（urllib/curl_cffi/requests）会把多个 Set-Cookie 合并成一个逗号串，
# Expires 日期里也带逗号——直接喂 http.cookiejar 会解析错，这里先按"新 cookie 名=值"
# 特征切分，再把每条单独喂给 CookieJar（RFC 6265 语义）。

def split_set_cookie(value: str) -> List[str]:
    """把可能逗号合并的 Set-Cookie 串切成单条。"""
    if not value:
        return []
    parts: List[str] = []
    cur = ""
    for seg in re.split(r',(?=(?:[^"]*"[^"]*")*[^"]*$)', value):
        seg = seg.strip()
        if not seg:
            continue
        # 新 cookie：以 "名字=值" 开头（名字不含 =;,\s）
        if cur and not re.match(r"^[^=;,\s]+=", seg):
            cur += ", " + seg          # Expires 日期延续
        else:
            if cur:
                parts.append(cur)
            cur = seg
    if cur:
        parts.append(cur)
    return parts


def set_cookie_strings(headers: Optional[Dict[str, Any]] = None,
                       raw_headers: Any = None) -> List[str]:
    """从响应里取所有 Set-Cookie 字符串（优先 raw 的多值接口）。"""
    out: List[str] = []
    if raw_headers is not None:
        try:
            if hasattr(raw_headers, "get_all"):
                out = list(raw_headers.get_all("Set-Cookie") or [])
            elif hasattr(raw_headers, "getlist"):
                out = list(raw_headers.getlist("Set-Cookie") or [])
        except Exception:
            out = []
    if not out:
        v = (headers or {}).get("set-cookie", "") or (headers or {}).get("Set-Cookie", "")
        out = split_set_cookie(str(v))
    # raw 多值里可能仍有个别被合并，逐条再切一次（幂等）
    final: List[str] = []
    for o in out:
        final.extend(split_set_cookie(o))
    return final


def update_cookie_jar(jar, url: str, headers: Optional[Dict[str, Any]] = None,
                      raw_headers: Any = None) -> None:
    """把响应的 Set-Cookie 写入 CookieJar。"""
    if jar is None:
        return
    try:
        from http.cookiejar import CookieJar
        from http.client import HTTPMessage
        import urllib.request as _ur
        if not isinstance(jar, CookieJar):
            return
        req = _ur.Request(url or "http://localhost/")
        for sc in set_cookie_strings(headers, raw_headers):
            if not sc:
                continue
            msg = HTTPMessage()
            msg.add_header("Set-Cookie", sc)
            class _Resp:
                def __init__(self, m):
                    self._m = m
                def info(self):
                    return self._m
                def geturl(self):
                    return url or "http://localhost/"
            try:
                jar.extract_cookies(_Resp(msg), req)
            except Exception:
                continue
    except Exception:
        pass


def jar_cookie_header(jar, url: str) -> str:
    """从 CookieJar 生成 Cookie 请求头（无 cookie 返回 ""）。"""
    if jar is None:
        return ""
    try:
        import urllib.request as _ur
        req = _ur.Request(url or "http://localhost/")
        jar.add_cookie_header(req)
        return req.get_header("Cookie") or ""
    except Exception:
        return ""


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """审查修复（P1，R129）：urllib 默认重定向白名单含 ftp——公网站点 302 到
    「ftp://内网」即可绕过 HttpClient 的「仅 http/https」入口闸做内网探测。
    重定向目标一律重新过协议白名单（与入口闸同口径）。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl or "").scheme not in ("http", "https"):
            raise urllib.error.HTTPError(
                req.full_url, code, f"拒绝重定向到非 http/https 协议: {newurl!r}", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_bytes(url: str, headers: Optional[Dict[str, str]] = None, proxy: Optional[str] = None,
                timeout: int = 60, max_size: int = 64 * 1024 * 1024) -> Optional[bytes]:
    """下载原始字节（curl_cffi TLS 伪装优先，回退 urllib+gzip）。失败返回 None，
    失败原因记录在 fetch_bytes.last_error（供调用方诊断，不再双重静默吞错）。
    审查修复 P1：pipeline download 步骤走这里——曾绕过请求计数与硬闸。
    R129 修复 P1：响应体限读（默认 64MB，下载语义；0/None=不设限）——此前
    r.content / resp.read() 全量进内存，超大响应直接 OOM。超限报错不返回截断
    文件（半截 PDF/zip 比失败更有害）。
    OCR R131（M）：last_error 是共享函数属性——多线程并发下载时错误消息互踩。
    属诊断信息（非数据），接受 last-writer-wins 语义；需要精确归因时在调用侧串行。"""
    # 与 HttpClient 同一 SSRF 面：urllib 回退 opener 含 FileHandler，file:// 会读本地文件
    if urllib.parse.urlsplit(url or "").scheme not in ("http", "https"):
        fetch_bytes.last_error = f"拒绝非 http/https 协议: {url!r}"
        return None
    _bump_request_count()  # 真实下载尝试计入任务预算
    fetch_bytes.last_error = ""
    hdrs = {"User-Agent": random.choice(UA_POOL), "Accept-Language": "zh-CN,zh;q=0.9"}
    if headers:
        hdrs.update(headers)
    try:
        import curl_cffi.requests as cffi
        from curl_cffi import CurlOpt
        kw = {"headers": hdrs, "timeout": timeout, "impersonate": "chrome",
              # R129 修复（P1）：同 CurlCffiClient——重定向白名单收紧为 http/https
              "curl_options": {CurlOpt.REDIR_PROTOCOLS: 1 | 2}}
        if proxy:
            kw["proxies"] = {"http": proxy, "https": proxy}
        r = cffi.get(url, allow_redirects=True, stream=True, **kw)
        try:
            chunks = []
            total = 0
            for chunk in r.iter_content(chunk_size=65536):
                chunks.append(chunk)
                total += len(chunk)
                if max_size and total > max_size:
                    fetch_bytes.last_error = f"响应超过 max_size={max_size}，已中止"
                    return None
            raw = b"".join(chunks)
        finally:
            try:
                r.close()
            except Exception:
                pass
        if r.status_code < 400:
            return raw or None
        fetch_bytes.last_error = f"curl_cffi: HTTP {r.status_code}"
        return None
    except Exception as e:
        fetch_bytes.last_error = f"curl_cffi: {type(e).__name__}: {e}"
    try:
        req = urllib.request.Request(url, headers=hdrs)
        opener = urllib.request.build_opener(_SafeRedirectHandler())
        if proxy:
            opener.add_handler(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read(max_size + 1) if max_size else resp.read()
            if max_size and len(raw) > max_size:
                fetch_bytes.last_error = f"响应超过 max_size={max_size}，已中止"
                return None
            enc = resp.headers.get("Content-Encoding", "").lower()
            if enc == "gzip":
                try:
                    raw = gzip.decompress(raw)
                    if max_size and len(raw) > max_size:
                        fetch_bytes.last_error = f"解压后超过 max_size={max_size}，已中止"
                        return None
                except Exception:
                    pass
            return raw or None
    except Exception as e:
        fetch_bytes.last_error += f" | urllib: {type(e).__name__}: {e}"
        return None


fetch_bytes.last_error = ""  # 每次调用重置；失败时记录最后一次错误（诊断用）


def _cache_key(url: str, body: bytes, method: str) -> str:
    """HTTP 缓存键（HttpClient 与 CurlCffiClient 共用——两后端缓存行为一致）。
    非密码学用途，sha256 仅因钩子口径统一（md5 用于缓存键无碰撞对抗需求）。"""
    raw = f"{method}|{url}|{body.decode('utf-8', 'replace')}"
    return hashlib.sha256(raw.encode(), usedforsecurity=False).hexdigest() + ".json"


def _cache_encode(result: Dict[str, Any]) -> Dict[str, Any]:
    """缓存编码：body bytes → base64（json 无法直接存 bytes）；raw_headers 不落盘。"""
    out = dict(result)
    out.pop("raw_headers", None)
    b = out.get("body")
    if isinstance(b, bytes):
        out["body"] = "b64:" + base64.b64encode(b).decode("ascii")
    return out


def _cache_decode(cached: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(cached)
    b = out.get("body")
    if isinstance(b, str) and b.startswith("b64:"):
        out["body"] = base64.b64decode(b[4:])
    return out


def _cache_valid(path: Path, ttl: float = CACHE_DEFAULT_TTL) -> bool:
    try:
        return time.time() - path.stat().st_mtime <= ttl
    except Exception:
        return False


@dataclass
class HttpClient:
    """通用 HTTP 客户端：重试 + 退避 + 限速 + UA 轮换 + 代理 + 缓存。"""

    min_interval: float = 1.0          # 每次请求最小间隔（秒），防限流
    max_retries: int = 3
    backoff_base: float = 2.0          # 指数退避基数
    timeout: float = 20
    rotate_ua: bool = True
    proxy: Optional[str] = None        # 例如 http://127.0.0.1:7890
    use_system_proxy: bool = False     # False = 直连（绕过 macOS 系统代理/Clash）
    cache_dir: Optional[Path] = None   # 设置后按 URL+body 缓存响应
    extra_headers: Dict[str, str] = field(default_factory=dict)
    cookies: Dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.cookies = _norm_cookies(self.cookies)
        self._last_ts = 0.0
        self._throttle_lock = threading.Lock()
        self._at = AdaptiveThrottle(self.min_interval)
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    # -- 限速 --
    def _throttle(self) -> None:
        # 多 worker 并发调用：必须加锁，否则限速形同虚设（可 2 倍突发）
        with self._throttle_lock:
            wait = self._at.wait_seconds()
            elapsed = time.time() - self._last_ts
            if elapsed < wait:
                time.sleep(wait - elapsed)
            self._last_ts = time.time()
        _bump_request_count()

    def _headers(self, extra: Optional[Dict[str, str]]) -> Dict[str, str]:
        h = {
            "User-Agent": random.choice(UA_POOL) if self.rotate_ua else DEFAULT_UA,
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate",
        }
        h.update(self.extra_headers)
        if extra:
            h.update(extra)
        if self.cookies:
            h["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        return h

    def _opener(self) -> urllib.request.OpenerDirector:
        return self._opener_for(None)

    def _opener_for(self, proxy: Optional[str]) -> urllib.request.OpenerDirector:
        handlers: List[Any] = []
        p = proxy if proxy is not None else self.proxy
        if p:
            handlers.append(urllib.request.ProxyHandler({
                "http": p, "https": p}))
        elif not self.use_system_proxy:
            # 显式禁用代理：urllib 默认会读 macOS 系统代理（Clash），
            # 经代理做 HTTPS 时 TLS 握手可能无限挂起
            handlers.append(urllib.request.ProxyHandler({}))
        # build_opener 遇到默认 handler 的子类会替换默认实例——重定向由此走安全白名单
        return urllib.request.build_opener(_SafeRedirectHandler(), *handlers)

    def _cache_key(self, url: str, body: bytes, method: str) -> str:
        return _cache_key(url, body, method)

    def _cache_encode(self, result: Dict[str, Any]) -> Dict[str, Any]:
        return _cache_encode(result)

    def _cache_decode(self, cached: Dict[str, Any]) -> Dict[str, Any]:
        return _cache_decode(cached)

    def _cache_valid(self, path: Path, ttl: float = CACHE_DEFAULT_TTL) -> bool:
        return _cache_valid(path, ttl)

    def request(
        self,
        url: str,
        method: str = "GET",
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        use_cache: bool = False,
        allow_html_404: bool = False,
        proxy: Optional[str] = None,
        max_size: Optional[int] = None,
    ) -> Dict[str, Any]:
        """返回 {'ok':bool, 'status':int, 'body':bytes, 'json':dict|None, 'text':str, 'headers':dict}。
        proxy 传入时本请求走该代理（覆盖实例级 proxy，None 表示用实例配置）。"""
        if params:
            url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        # 非 ASCII URL（中文参数等）自动百分号编码，urllib 才能请求
        try:
            url.encode("ascii")
        except UnicodeEncodeError:
            from urllib.parse import urlsplit, urlunsplit, quote
            _p = urlsplit(url)
            url = urlunsplit((_p.scheme, _p.netloc,
                              quote(_p.path, safe="/%"),
                              quote(_p.query, safe="=&%+?/"),
                              _p.fragment))
        body_bytes = b""
        if data is not None:
            # R48 修复：字符串 body（http_json 配置的 form 串，如 cninfo 的
            # pageNum=1&...）曾被 urlencode 当 mapping 处理抛 TypeError——
            # urllib 回退链对每个带字符串体的 POST 直接报废。字符串原样编码
            # R55 修复：bytes body（json.dumps().encode() 的 JSON POST）原样
            # 透传，urlencode(bytes) 同样抛 TypeError
            if isinstance(data, str):
                body_bytes = data.encode()
            elif isinstance(data, (bytes, bytearray)):
                body_bytes = bytes(data)
            else:
                body_bytes = urllib.parse.urlencode(data).encode()
        elif json_data is not None:
            body_bytes = json.dumps(json_data, ensure_ascii=False).encode()
            headers = dict(headers or {})
            headers["Content-Type"] = "application/json"

        if use_cache and self.cache_dir and method == "GET" and not body_bytes:
            cf = self.cache_dir / self._cache_key(url, b"", method)
            if cf.exists():
                try:
                    cached = self._cache_decode(json.loads(cf.read_text(encoding="utf-8")))
                except Exception:
                    cached = None
                if cached is not None:
                    if self._cache_valid(cf):
                        return cached
                    # 对标 Scrapy HttpCache / Crawlee：TTL 过期但有验证器
                    # （ETag/Last-Modified）→ 条件 GET，304 则续期复用旧缓存
                    ch = cached.get("headers") or {}
                    _cond = {}
                    if ch.get("etag"):
                        _cond["If-None-Match"] = ch["etag"]
                    if ch.get("last-modified"):
                        _cond["If-Modified-Since"] = ch["last-modified"]
                    if _cond:
                        try:
                            res = self._request_once(url, b"", "GET",
                                                     {**(headers or {}), **_cond},
                                                     use_cache=False,
                                                     allow_html_404=allow_html_404,
                                                     proxy=proxy, max_size=max_size)
                            if res and res.get("status") == 304:
                                try:
                                    os.utime(cf, None)  # 304 = 未变：刷新 mtime 续期 TTL
                                except OSError:
                                    pass
                                return cached
                            if res and res.get("ok"):
                                try:  # 新内容回写缓存（条件请求带 use_cache=False）
                                    _ct = cf.with_suffix(".json.wtmp")
                                    _ct.write_text(json.dumps(self._cache_encode(res),
                                                              ensure_ascii=False),
                                                   encoding="utf-8")
                                    _ct.replace(cf)
                                except OSError:
                                    pass
                                return res
                            # 条件验证网络失败 → 旧缓存兜底（好过失败）
                            return cached
                        except Exception:
                            return cached

        result: Optional[Dict[str, Any]] = None
        # 注意：不要用 socket.setdefaulttimeout 改进程级全局超时——多线程 worker
        # 会互相覆盖（竞态）。urllib opener.open(timeout=...) 已覆盖连接/TLS/读取。
        try:
            result = self._request_once(url, body_bytes, method, headers, use_cache,
                                        allow_html_404=allow_html_404, proxy=proxy,
                                        max_size=max_size)
        finally:
            pass
        _note_net_result(bool(result and result.get("ok")))
        return result

    def _request_once(self, url, body_bytes, method, headers, use_cache,
                      allow_html_404=False, proxy=None, max_size=None):
        # SSRF 面：urllib 默认 opener 含 FileHandler，file:// 会直接读本地文件。
        # HttpClient 只做 HTTP 抓取，非 http/https 协议一律拒绝（不加私网 IP 过滤：
        # 爬虫需要访问任意公网站点，且本地回环测试依赖 127.0.0.1）。
        if urllib.parse.urlsplit(url or "").scheme not in ("http", "https"):
            raise ValueError(f"HttpClient 仅支持 http/https 协议，已拒绝: {url!r}")
        last_err = ""
        last_headers: Dict[str, str] = {}
        last_status = 0
        result: Optional[Dict[str, Any]] = None
        for attempt in range(1, self.max_retries + 1):
            self._throttle()
            req = urllib.request.Request(
                url, data=body_bytes or None,
                headers=self._headers(headers), method=method,
            )
            try:
                opener = self._opener_for(proxy)
                _t0 = time.time()
                with opener.open(req, timeout=self.timeout) as resp:
                    enc = resp.headers.get("Content-Encoding", "").lower()
                    # OCR R131（H）：max_size=None（默认）时 gzip 炸弹可无限解压——
                    # 落到 DEFAULT_MAX_BODY 硬顶（20MB），启用一直没被引用的常量
                    limit = (max_size + 1) if max_size else (DEFAULT_MAX_BODY + 1)
                    if enc == "gzip":
                        # 流式解压限读：gzip 炸弹也能被 max_size 截住
                        try:
                            import io as _io
                            _comp = resp.read(limit) if limit else resp.read()
                            gz = gzip.GzipFile(fileobj=_io.BytesIO(_comp))
                            # R36 修复（P0）：不限长分支曾 resp.read() 读原始压缩
                            # 字节——urllib 回退路径整条返回 gzip 乱码还 exit 0
                            raw = gz.read(limit) if limit else gz.read()
                        except Exception:
                            # 审查七轮（M）：解压失败曾回读 resp 残体——流已被消费一半，
                            # 返回的是压缩尾部字节还 ok=True。已缓冲体仍是 gzip 魔数
                            # （截断流）则弃空如实暴露失败；服务端谎报 gzip 但实体
                            # 是明文时保留原文
                            raw = b"" if _comp[:2] == b"\x1f\x8b" else _comp
                    elif enc == "deflate":
                        # R10 复查修正：resp.read() 曾不设上限——无限定长流照样
                        # 撑爆内存。先限读压缩字节，decompressobj 再限输出
                        raw = resp.read(limit) if limit else resp.read()
                        try:
                            _d = zlib.decompressobj()
                            raw = _d.decompress(raw, limit) if limit else _d.decompress(raw)
                        except Exception:
                            # 审查七轮（M）：zlib 头校验失败常见于服务器实发裸 deflate
                            # （wbits=-15）——此前 except:pass 把压缩字节原样当 ok=True
                            # 正文外泄。按 urllib3 口径重试裸 deflate，仍失败才保留原文
                            try:
                                _d = zlib.decompressobj(-15)
                                raw = _d.decompress(raw, limit) if limit else _d.decompress(raw)
                            except Exception:
                                pass
                        if limit:
                            raw = raw[:limit]
                    else:
                        raw = resp.read(limit) if limit else resp.read()
                    if max_size and len(raw) > max_size:
                        raw = raw[:max_size]
                    status = getattr(resp, "status", 200)
                    ctype = resp.headers.get("Content-Type", "")
                    parsed: Any = None
                    if "json" in ctype or raw[:1] in (b"{", b"["):
                        try:
                            parsed = json.loads(raw)
                        except Exception:
                            parsed = None
                    result = {
                        "ok": True,
                        "status": status,
                        "body": raw,
                        "text": _decode_body(raw, {k.lower(): v for k, v in resp.headers.items()}),
                        "json": parsed,
                        "url": resp.geturl() or url,
                        "headers": {k.lower(): v for k, v in resp.headers.items()},
                        "raw_headers": resp.headers,
                    }
                    self._at.note_latency(time.time() - _t0)
                    self._at.note_ok()
                    break
            except urllib.error.HTTPError as e:
                # 审查四轮（H）：max_size 缺省分支曾裸 e.read()——无上限读
                # （与 865 行成功路径的 DEFAULT_MAX_BODY 兜底同款事故）
                raw = e.read((max_size + 1) if max_size else (DEFAULT_MAX_BODY + 1))
                if max_size and len(raw) > max_size:
                    raw = raw[:max_size]
                # urllib 不自动解压 gzip：HTTPError 路径的 raw 仍是压缩字节，不解码全乱码
                if raw[:2] == b"\x1f\x8b":
                    import gzip as _gz
                    import io as _io
                    try:
                        # 审查修复（P2）：解压上限——GzipFile.read(limit) 既解压
                        # 又截断，压缩炸弹无法借 HTTPError 路径撑爆内存。
                        # 审查五轮（H）：max_size 缺省曾用 _gz.decompress 全解压
                        # ——解压后体无界，DEFAULT_MAX_BODY 兜底
                        _cap = (max_size + 1) if max_size else (DEFAULT_MAX_BODY + 1)
                        raw = _gz.GzipFile(fileobj=_io.BytesIO(raw)).read(_cap)
                    except Exception:
                        pass
                # batch1600 战训：403/421/52x 自动记账（urllib 兜底后端同样覆盖）。
                # 记账前先判型（需传解压后的 raw，瑞数/CF 通道拦截不记账）
                try:
                    if e.code in (403, 412, 421) or 520 <= e.code <= 529:
                        # 412 在列是瑞数口径（Challenge 应答）；未签名 412 在 _budget_auto_mark 内只提示不记账
                        _budget_auto_mark(url, e.code, body=raw[:8192])
                except Exception:
                    pass
                status = e.code
                # WAF/反爬常返回 404 的"假页面"，allow_html_404 表示接受这种 HTML 响应
                if allow_html_404 and status == 404:
                    result = {
                        "ok": True, "status": status, "body": raw,
                        "text": _decode_body(raw, {k.lower(): v for k, v in e.headers.items()} if e.headers else {}),
                        "json": None, "url": url,
                        "headers": {k.lower(): v for k, v in e.headers.items()} if e.headers else {},
                        "raw_headers": e.headers if e.headers else None,
                    }
                    # 与另两后端成功路径对齐：完成了一次 HTTP 往返即记成功+延迟
                    # （漏记会让 ok_streak 永不增长 → 降速后永不恢复）
                    self._at.note_ok()
                    self._at.note_latency(time.time() - _t0)
                    break
                if status in (429, 403, 500, 502, 503, 504):
                    last_err = f"HTTP {status}"
                    last_status = status
                    last_headers = {k.lower(): v for k, v in e.headers.items()} if e.headers else {}
                    self._at.note_block(f"http{status}")
                    if status == 429 and last_headers.get("retry-after"):
                        # 429 + Retry-After：不内部退避，立即交还引擎按服务端要求调度（避免耗掉窗口）
                        result = {"ok": False, "status": status, "body": raw,
                                  "text": _decode_body(raw, last_headers),
                                  "json": None, "url": url, "headers": last_headers}
                        break
                    wait = self.backoff_base ** attempt + random.uniform(0, 1)
                    log(f"  请求失败 {status}，{wait:.1f}s 后重试（{attempt}/{self.max_retries}）", "WARN")
                    time.sleep(wait)
                    continue
                last_err = f"HTTP {status}: {_decode_body(raw, {k.lower(): v for k, v in e.headers.items()} if e.headers else {})[:200]}"
                result = {"ok": False, "status": status, "body": raw,
                          "text": _decode_body(raw, {k.lower(): v for k, v in e.headers.items()} if e.headers else {}),
                          "json": None, "url": url,
                          "headers": {k.lower(): v for k, v in e.headers.items()} if e.headers else {}}
                break
            except Exception as e:  # 网络/超时
                last_err = str(e)
                wait = self.backoff_base ** attempt
                log(f"  网络异常: {e}，{wait:.1f}s 后重试（{attempt}/{self.max_retries}）", "WARN")
                time.sleep(wait)

        if result is None:
            # batch1800 战训（P3）：连续网络失败常因出口/系统代理变化（用户退 Clash/切 VPN）——
            # 附 doctor 复查提示，别让 agent 在错误诊断方向空转（连击计数在最外层统一记）
            return {"ok": False, "status": last_status, "body": b"", "text": last_err, "json": None, "url": url,
                    "headers": last_headers,
                    "hint": "连续网络失败：出口/系统代理可能已变化，可运行 scripts/doctor.py（网络链路组）复查"}
        if use_cache and result["ok"] and self.cache_dir and method == "GET" and not body_bytes:
            _cf = self.cache_dir / self._cache_key(url, b"", method)
            try:
                # 审查修复（P2）：缓存写入曾在 try 外——满盘时已成功的响应被
                # 当成请求失败重试，白烧预算。
                # 审查二轮（M）：直写曾让并发读者拿到半截 JSON——tmp+replace 原子化
                _ctmp = _cf.with_suffix(".json.tmp")
                _ctmp.write_text(json.dumps(self._cache_encode(result), ensure_ascii=False), encoding="utf-8")
                import os as _os
                _os.replace(_ctmp, _cf)
            except OSError as e:
                log(f"HTTP 缓存写入失败（不影响本次结果）: {e}", "DEBUG")
            # 简单淘汰：超过上限时删最旧的（防无限增长）
            try:
                _files = sorted(self.cache_dir.glob("*.json"), key=lambda f: f.stat().st_mtime)
                if len(_files) > CACHE_MAX_FILES:
                    for _old in _files[: len(_files) - CACHE_MAX_FILES]:
                        _old.unlink(missing_ok=True)
            except Exception:
                pass
        return result

    def get(self, url: str, **kw) -> Dict[str, Any]:
        return self.request(url, "GET", **kw)

    def post(self, url: str, **kw) -> Dict[str, Any]:
        return self.request(url, "POST", **kw)


# ---------------------------------------------------------------- HTML 解析（轻量，不依赖 bs4）

def html_find(html: str, tag: str, attrs: Optional[Dict[str, str]] = None, many: bool = False):
    """极简 HTML 元素查找（够用即可）。attrs 匹配属性子串。"""
    # OCR R131（H）：tag 直接内插正则——含正则特殊字符时行为未定义或 ReDoS
    _safe_tag = re.escape(tag)
    pattern = re.compile(r"<%s\b[^>]*>.*?</%s>|<%s\b[^>]*/>" % (_safe_tag, _safe_tag, _safe_tag), re.S)
    out = []
    for m in pattern.finditer(html):
        block = m.group(0)
        if attrs:
            if not all(f'{k}="' in block or f"{k}='" in block for k in attrs):
                continue
            if any(v and v not in block for v in attrs.values()):
                continue
        out.append(block)
    return out if many else (out[0] if out else None)


def html_text(block: str) -> str:
    """去掉标签、压缩空白。"""
    txt = re.sub(r"<script.*?</script>|<style.*?</style>", "", block, flags=re.S)
    txt = re.sub(r"<[^>]+>", "", txt)
    return re.sub(r"\s+", " ", txt).strip()


# ---------------------------------------------------------------- 导出

def export_rows(rows: List[Dict[str, Any]], out_dir: Path, base_name: str,
                formats: Optional[List[str]] = None) -> Dict[str, Path]:
    """按 formats 导出（默认 json+csv+xlsx）。设计要点（猎聘战例）：
    - 逐格式独立容错：一种格式失败不影响其余
    - dict/list 值安全序列化：xlsx/csv 不再因嵌套结构崩溃
    - 覆盖保护：目标 .json 若是配置文件（含 source/name），自动改用 *_data 后缀"""
    out_dir.mkdir(parents=True, exist_ok=True)
    want = [f.lower() for f in (formats or ["json", "csv", "xlsx"])]
    paths: Dict[str, Path] = {}

    def _safe(v: Any) -> Any:
        if isinstance(v, (dict, list)):
            return json.dumps(v, ensure_ascii=False)
        return v

    def _base() -> str:
        probe = out_dir / f"{base_name}.json"
        try:
            if probe.exists():
                head = probe.read_text(encoding="utf-8", errors="replace")[:400]
                if '"source"' in head and '"name"' in head:
                    log(f"⚠️ {probe.name} 疑似配置文件，数据改存 {base_name}_data.* 防覆盖")
                    return base_name + "_data"
        except Exception:
            pass
        return base_name

    base_name = _base()

    # JSON（永远写：机器可读主格式）
    jp = out_dir / f"{base_name}.json"
    jp.write_text(json.dumps(rows, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    paths["json"] = jp

    if "csv" in want:
        try:
            all_keys: List[str] = []
            for r in rows:
                for k in r:
                    if k not in all_keys:
                        all_keys.append(k)
            cp = out_dir / f"{base_name}.csv"
            with open(cp, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=all_keys, extrasaction="ignore")
                w.writeheader()

                def _csv_cell(v: Any) -> str:
                    # CSV 公式注入（审查 P2，R77）：以 =+-@/Tab/CR 开头的抓取文本
                    # 在 Excel 里会被当公式执行（=WEBSERVICE(...) 是标准注入向量）
                    s = "" if v is None else str(_safe(v))
                    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s

                for r in rows:
                    w.writerow({k: _csv_cell(v) for k, v in r.items()})
            paths["csv"] = cp
        except Exception as e:
            log(f"CSV 导出失败（跳过）: {e}", "WARN")

    if "xlsx" in want:
        try:
            import openpyxl
            from openpyxl.styles import Font
            xp = out_dir / f"{base_name}.xlsx"
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = (base_name[:28] or "data")
            all_keys: List[str] = []
            for r in rows:
                for k in r:
                    if k not in all_keys:
                        all_keys.append(k)
            ws.append(all_keys)
            for c in ws[1]:
                c.font = Font(bold=True)

            def _cell(v: Any) -> Any:
                v = _safe(v)
                if v is None:
                    return ""
                v = v if isinstance(v, (int, float, str)) else str(v)
                # 公式注入同防（R77）：字符串单元格以危险字符开头时加前缀
                if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
                    return "'" + v
                return v

            for r in rows:
                ws.append([_cell(r.get(k, "")) for k in all_keys])
            for col in ws.columns:
                letter = col[0].column_letter
                ws.column_dimensions[letter].width = min(max(len(str(c.value or "")) for c in col[:100]) + 2, 60)
            wb.save(xp)
            paths["xlsx"] = xp
        except ImportError:
            log("openpyxl 未安装，跳过 Excel 导出", "WARN")
        except Exception as e:
            log(f"Excel 导出失败（跳过，CSV/JSON 不受影响）: {e}", "WARN")

    if "parquet" in want:
        # R101 新能力（对标生态标配）：列式存储，大数据量分析友好。
        # schema 统一为字符串列（抓取数据天然异构，避免 pyarrow 类型推断炸整单）
        try:
            import pandas as _pd
            pp = out_dir / f"{base_name}.parquet"
            all_keys_p: List[str] = []
            for r in rows:
                for k in r:
                    if k not in all_keys_p:
                        all_keys_p.append(k)
            _df = _pd.DataFrame(
                [[("" if r.get(k) is None else str(_safe(r.get(k)))) for k in all_keys_p]
                 for r in rows],
                columns=all_keys_p)
            _df.to_parquet(pp, index=False)
            paths["parquet"] = pp
        except ImportError:
            log("pandas/pyarrow 未安装，跳过 parquet 导出", "WARN")
        except Exception as e:
            log(f"parquet 导出失败（跳过，CSV/JSON 不受影响）: {e}", "WARN")

    if not paths:
        log("⚠️ 没有任何格式导出成功", "WARN")
    return paths


# ---------------------------------------------------------------- 通用分页遍历

class _ReadCapped:
    """限读助手：读满 cap+1 即停（审查修复 R129，P1——超限后不读完也不关，
    连接不归还连接池）。cap 为 None 时全量读。"""
    @staticmethod
    def read(resp, cap: Optional[int]) -> bytes:
        if not cap:
            return resp.content
        chunks: List[bytes] = []
        total = 0
        for chunk in resp.iter_content(chunk_size=65536):
            chunks.append(chunk)
            total += len(chunk)
            if total > cap:
                break
        return b"".join(chunks)[:cap]


class RequestsClient:
    """基于 requests 的高性能客户端：连接池复用、gzip、会话 Cookie、限速、重试。"""

    def __init__(self, min_interval: float = 1.0, max_retries: int = 3,
                 timeout: float = 20, rotate_ua: bool = True,
                 proxy: Optional[str] = None, cookies: Optional[Dict[str, str]] = None,
                 extra_headers: Optional[Dict[str, str]] = None,
                 backoff_base: float = 2.0, verify: bool = True,
                 use_system_proxy: bool = False,
                 cache_dir: Optional[Path] = None):
        import requests
        self.requests = requests
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.timeout = timeout
        self.rotate_ua = rotate_ua
        self.proxy = proxy
        self.use_system_proxy = use_system_proxy
        self.backoff_base = backoff_base
        self.extra_headers = dict(extra_headers or {})
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._last_ts = 0.0
        self.session = requests.Session()
        # R112 修复（P2）：显式直连不读环境代理（http_proxy/https_proxy 环境变量
        # 会把 B站风控敏感流量劫进用户代理）——在会话创建后立即设置
        if not use_system_proxy:
            self.session.trust_env = False
        # R17 修复：默认校验 TLS 证书（与 urllib 后端一致）——默认关闭会让登录
        # Cookie 发给任意中间人；目标站证书链确有损坏时由调用方显式关闭校验
        self.session.verify = verify
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}
        if cookies:
            self.session.cookies.update(_norm_cookies(cookies))
        self._throttle_lock = threading.Lock()
        # 连接池
        adapter = requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=16, max_retries=0)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self._at = AdaptiveThrottle(min_interval)

    def _throttle(self) -> None:
        # 多 worker 并发调用：必须加锁，否则限速形同虚设（可 2 倍突发）
        with self._throttle_lock:
            wait = self._at.wait_seconds()
            elapsed = time.time() - self._last_ts
            if elapsed < wait:
                time.sleep(wait - elapsed)
            self._last_ts = time.time()
        _bump_request_count()

    def request(self, url: str, method: str = "GET", params=None, data=None,
                json_data=None, headers=None, use_cache: bool = False,
                allow_html_404: bool = False, proxy: Optional[str] = None,
                max_size: Optional[int] = None) -> Dict[str, Any]:
        h = {"Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
             "Accept-Encoding": "gzip, deflate"}
        h.update(self.extra_headers)
        if headers:
            h.update(headers)
        # 调用方/会话已给 UA 就不随机（三后端一致；登录会话不能被随机 UA 踢掉）
        if "User-Agent" not in h and self.rotate_ua:
            h["User-Agent"] = random.choice(UA_POOL)
        elif "User-Agent" not in h:
            h["User-Agent"] = DEFAULT_UA

        last_err = "requests 请求失败"
        last_status = 0
        for attempt in range(1, self.max_retries + 1):
            self._throttle()
            try:
                kw = {"params": params, "data": data, "json": json_data,
                      "headers": h, "timeout": self.timeout, "allow_redirects": True}
                if max_size:
                    # R130 修复（P1）：requests 默认 stream=False 会把整个响应体
                    # 急切缓冲进内存——_ReadCapped 只截返回值不截下载。限读必须
                    # 配 stream=True（延迟读体）+ 所有出口 close
                    kw["stream"] = True
                if proxy:
                    kw["proxies"] = {"http": proxy, "https": proxy}
                _t0 = time.time()
                resp = self.session.request(method.upper(), url, **kw)
                try:
                    # R129 修复（P1）：统一限读提前——此前 429 早退路径无视 max_size
                    # 全量读体（恶意 429 + 超大响应=内存放大）；重试路径读完不关（连接泄漏）
                    raw = _ReadCapped.read(resp, max_size)
                    # batch1600 战训（P1）：403/421/52x 自动记入域名封锁台账（budget --list 可查）。
                    # best-effort：台账故障绝不影响抓取主流程；已冷却中则不重复记账。
                    # 记账前先判型：瑞数/CF 通道拦截不记账（NMPA 战训）
                    try:
                        if resp.status_code in (403, 412, 421) or 520 <= resp.status_code <= 529:
                            # 412 在列是瑞数口径（Challenge 应答）；未签名 412 在 _budget_auto_mark 内只提示不记账
                            _budget_auto_mark(url, resp.status_code, body=raw[:8192])
                    except Exception:
                        pass
                    if resp.status_code == 429:
                        self._at.note_block("http429")
                        # HTTP 往返已完整走通（非网络层失败）——清零连击计数，
                        # 防止"网络失败→404/429→网络失败"误触发换线告警
                        _note_net_result(True)
                        _t = smart_decode(raw, {k.lower(): v for k, v in resp.headers.items()})
                        return {"ok": False, "status": 429, "body": raw, "text": _t,
                                "json": None, "url": resp.url,
                                "headers": {k.lower(): v for k, v in resp.headers.items()},
                                "raw_headers": getattr(resp, "raw", None) and getattr(resp.raw, "headers", None)}
                    if resp.status_code in (403, 500, 502, 503, 504):
                        last_status = resp.status_code
                        last_err = f"HTTP {resp.status_code}"
                        self._at.note_block(f"http{resp.status_code}")
                        wait = self.backoff_base ** attempt + random.uniform(0, 1)
                        log(f"  请求失败 {resp.status_code}，{wait:.1f}s 后重试（{attempt}/{self.max_retries}）", "WARN")
                        time.sleep(wait)
                        continue
                    if resp.status_code >= 400 and not (allow_html_404 and resp.status_code == 404):
                        # 同 429：拿到真实 HTTP 应答 = 网络通路正常，清零连击
                        _note_net_result(True)
                        _t = smart_decode(raw, {k.lower(): v for k, v in resp.headers.items()})
                        return {"ok": False, "status": resp.status_code, "body": raw, "text": _t,
                                "json": None, "url": resp.url,
                                "headers": {k.lower(): v for k, v in resp.headers.items()},
                                "raw_headers": getattr(resp, "raw", None) and getattr(resp.raw, "headers", None)}
                    parsed = None
                    ctype = resp.headers.get("Content-Type", "")
                    if "json" in ctype or raw[:1] in (b"{", b"["):
                        try:
                            parsed = json.loads(raw.decode("utf-8", "ignore"))
                        except Exception:
                            parsed = None
                    # 无 charset 头时 resp.text 按 ISO-8859-1 解码 → 中文全乱码
                    # （"百度安全验证"曾被误判为页面结构变化）。一律走 smart_decode 从 raw 解。
                    _text = (smart_decode(raw, {k.lower(): v for k, v in resp.headers.items()})
                             if raw else (resp.text or ""))
                    _note_net_result(True)
                    self._at.note_latency(time.time() - _t0)
                    self._at.note_ok()
                    return {"ok": True, "status": resp.status_code, "body": raw,
                            "text": _text, "json": parsed, "url": resp.url,
                            "headers": {k.lower(): v for k, v in resp.headers.items()},
                            "raw_headers": getattr(resp, "raw", None) and getattr(resp.raw, "headers", None)}
                finally:
                    # R130：stream 模式下半读/重试丢弃的响应必须显式关闭，归还连接
                    try:
                        resp.close()
                    except Exception:
                        pass
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                wait = self.backoff_base ** attempt
                log(f"  网络异常: {e}，{wait:.1f}s 后重试（{attempt}/{self.max_retries}）", "WARN")
                time.sleep(wait)
        _note_net_result(False)
        return {"ok": False, "status": last_status, "body": b"", "text": last_err, "json": None, "url": url,
                "headers": {},
                "hint": "连续网络失败：出口/系统代理可能已变化，可运行 scripts/doctor.py（网络链路组）复查"}

    def get(self, url: str, **kw) -> Dict[str, Any]:
        return self.request(url, "GET", **kw)

    def post(self, url: str, **kw) -> Dict[str, Any]:
        return self.request(url, "POST", **kw)

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass


# ---------------------------------------------------------------- 高级 HTTP 后端（可选依赖）

def _sanitize_headers(headers: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """🛡️ curl_cffi 对 None 值 header 会抛 TypeError（'in <string>' requires string）。
    统一过滤 None 值并字符串化，避免配置里出现 None header 导致整请求崩溃。"""
    if not headers:
        return {}
    return {k: str(v) for k, v in headers.items() if v is not None}


class CurlCffiClient:
    """curl_cffi 后端：伪装浏览器 TLS/JA3/HTTP2 指纹（curl-impersonate）。
    未安装 curl_cffi 时导入即抛 ImportError，调用方回退 requests/urllib。
    接口与 HttpClient 对齐：request/get/post。
    """

    def __init__(self, min_interval: float = 1.0, max_retries: int = 3,
                 timeout: float = 20, rotate_ua: bool = True,
                 proxy: Optional[str] = None, cookies: Optional[Dict[str, str]] = None,
                 extra_headers: Optional[Dict[str, str]] = None,
                 impersonate: str = "chrome", verify: bool = True,
                 backoff_base: float = 2.0,
                 use_system_proxy: bool = False,
                 cache_dir: Optional[Path] = None):
        __import__("curl_cffi.requests")  # 可用性探测：未安装则抛 ImportError
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.timeout = timeout
        self.use_system_proxy = use_system_proxy
        self.rotate_ua = rotate_ua
        self.proxy = proxy
        self.impersonate = impersonate
        # R17 修复：默认校验 TLS（原默认关闭，登录 Cookie 暴露给中间人）
        self.verify = verify
        # OCR R131（M）：曾不收 backoff_base——make_http_client 透传该参数时
        # TypeError 被降级 except 吞掉，静默改走 requests 后端（指纹伪装失效）
        self.backoff_base = backoff_base
        self.impersonate_pool = ["chrome", "safari17_0", "firefox133", "edge101"]  # impersonate="auto" 时轮换
        self._throttle_lock = threading.Lock()
        self.extra_headers = dict(extra_headers or {})
        self.cookies = _norm_cookies(cookies)
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._last_ts = 0.0
        self._at = AdaptiveThrottle(min_interval)

    def _throttle(self) -> None:
        # 多 worker 并发调用：必须加锁，否则限速形同虚设（可 2 倍突发）
        with self._throttle_lock:
            wait = self._at.wait_seconds()
            elapsed = time.time() - self._last_ts
            if elapsed < wait:
                time.sleep(wait - elapsed)
            self._last_ts = time.time()
        _bump_request_count()

    def request(self, url: str, method: str = "GET", params=None, data=None,
                json_data=None, headers=None, use_cache: bool = False,
                allow_html_404: bool = False, proxy: Optional[str] = None,
                max_size: Optional[int] = None) -> Dict[str, Any]:
        import curl_cffi.requests as cffi
        h = {"Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
        h.update(self.extra_headers)
        if headers:
            h.update(headers)
        h = _sanitize_headers(h)
        # 审查三轮（M）：对齐 RequestsClient 三态——调用方/会话显式 UA 优先
        # （登录会话不能被随机 UA 踢掉），rotate_ua 随机，否则默认
        if "User-Agent" not in h and self.rotate_ua:
            h["User-Agent"] = random.choice(UA_POOL)
        elif "User-Agent" not in h:
            h["User-Agent"] = DEFAULT_UA
        proxy_use = proxy if proxy is not None else self.proxy
        imp = self.impersonate
        if imp == "auto":
            # 调用方给了 UA（会话 UA）→ 指纹跟随 UA，避免 "Safari UA + Chrome TLS" 错配
            _ua = (h.get("User-Agent") or "").lower()
            if "firefox" in _ua:
                imp = "firefox133"
            elif "edg/" in _ua:
                imp = "edge101"
            elif "safari" in _ua and "chrome" not in _ua:
                imp = "safari17_0"
            else:
                imp = random.choice(self.impersonate_pool)
        kw = dict(impersonate=imp, timeout=self.timeout, headers=h,
                  allow_redirects=True, verify=self.verify)
        # R129 修复（P1）：收紧 libcurl 重定向协议白名单——系统 libcurl 默认
        # REDIR_PROTOCOLS 仍含 ftp，公网站点 302 → ftp://内网 即绕过「仅 http/https」
        # 入口闸（本地 302→ftp://127.0.0.1:21 实测复现）。位掩码：HTTP=1, HTTPS=2
        from curl_cffi import CurlOpt
        kw["curl_options"] = {CurlOpt.REDIR_PROTOCOLS: 1 | 2}
        if max_size:
            kw["stream"] = True  # iter_content 需要流式模式
        if proxy_use:
            kw["proxies"] = {"http": proxy_use, "https": proxy_use}
        elif not self.use_system_proxy:
            # R112 修复（P2）：显式直连屏蔽环境变量代理——实证 curl_cffi 只认
            # 空字符串（None 会被忽略，http_proxy 环境变量仍劫持）
            kw["proxies"] = {"http": "", "https": ""}
        if self.cookies:
            kw["cookies"] = self.cookies
        # ── HTTP 缓存 + 条件重验证（对齐 HttpClient，审查对标 Scrapy HttpCache）──
        # CurlCffiClient 曾收下 use_cache 却无视——两个后端缓存行为不一致。
        # 审查六轮（M4）：params 曾不进缓存键——同 URL 不同参数共键错数据复用
        # （对齐 HttpClient 先拼 params 的口径）
        if params:
            import urllib.parse as _up
            url = url + ("&" if "?" in url else "?") + _up.urlencode(params)
        _revalidating = False
        _reval_cond: Dict[str, str] = {}
        if use_cache and self.cache_dir and method == "GET" and not data and not json_data:
            cf = self.cache_dir / _cache_key(url, b"", method)
            if cf.exists():
                try:
                    cached = _cache_decode(json.loads(cf.read_text(encoding="utf-8")))
                except Exception:
                    cached = None
                if cached is not None:
                    if _cache_valid(cf):
                        return cached
                    ch = cached.get("headers") or {}
                    if ch.get("etag"):
                        _reval_cond["If-None-Match"] = ch["etag"]
                    if ch.get("last-modified"):
                        _reval_cond["If-Modified-Since"] = ch["last-modified"]
                    if _reval_cond:
                        _revalidating = True
                        h.update(_reval_cond)
        last_err = ""
        last_status = 0
        for attempt in range(1, self.max_retries + 1):
            self._throttle()
            try:
                _t0 = time.time()
                resp = cffi.request(method.upper(), url, params=params,
                                    data=data, json=json_data, **kw)
                try:
                    if max_size:
                        # 流式限读：读满 max_size+1 即停，避免超大响应占满内存
                        chunks = []
                        total = 0
                        for chunk in resp.iter_content(chunk_size=65536):
                            chunks.append(chunk)
                            total += len(chunk)
                            if total > max_size:
                                break
                        raw = b"".join(chunks)[:max_size]
                    else:
                        raw = resp.content
                    # batch1600 战训：403/421/52x 自动记账（须覆盖全部后端——curl_cffi 是默认后端，
                    # v1.12.0 曾只挂在 RequestsClient 导致默认路径静默失效）。
                    # NMPA 战训：记账挪到读体之后并传 body 判型——瑞数/CF 通道拦截不记账；
                    # 不能提前访问 resp.content，否则后续 iter_content 流式读会拿到空
                    try:
                        if resp.status_code in (403, 412, 421) or 520 <= resp.status_code <= 529:
                            # 412 在列是瑞数口径（Challenge 应答）；未签名 412 在 _budget_auto_mark 内只提示不记账
                            _budget_auto_mark(url, resp.status_code, body=raw[:8192])
                    except Exception:
                        pass
                    if resp.status_code == 429:
                        # 429 不内部重试：立即交回，让引擎按 Retry-After 调度（保留状态码）
                        self._at.note_block("http429")
                        # HTTP 往返已完整走通（非网络层失败）——清零连击计数，
                        # 防止"网络失败→404/429→网络失败"误触发换线告警
                        _note_net_result(True)
                        _t = smart_decode(raw, {k.lower(): v for k, v in resp.headers.items()})
                        return {"ok": False, "status": 429, "body": raw, "text": _t,
                                "json": None, "url": str(resp.url),
                                "headers": {k.lower(): v for k, v in resp.headers.items()},
                                "raw_headers": resp.headers}
                    if resp.status_code in (403, 500, 502, 503, 504):
                        last_err = f"HTTP {resp.status_code}"
                        last_status = resp.status_code
                        self._at.note_block(f"http{resp.status_code}")
                        wait = self.backoff_base ** attempt + random.uniform(0, 1)
                        log(f"  curl_cffi 请求失败 {resp.status_code}，{wait:.1f}s 后重试", "WARN")
                        time.sleep(wait)
                        continue
                    if resp.status_code >= 400 and not (allow_html_404 and resp.status_code == 404):
                        # 非 2xx/3xx 视为失败（404 仅在 allow_html_404 时接受）——与 urllib 后端一致
                        # 同 429：拿到真实 HTTP 应答 = 网络通路正常，清零连击
                        _note_net_result(True)
                        _t = smart_decode(raw, {k.lower(): v for k, v in resp.headers.items()})
                        return {"ok": False, "status": resp.status_code, "body": raw, "text": _t,
                                "json": None, "url": str(resp.url),
                                "headers": {k.lower(): v for k, v in resp.headers.items()},
                                "raw_headers": resp.headers}
                    parsed = None
                    # 一律从 raw 解析 JSON：stream 模式下 resp.json() 读不到已消费的流
                    if "json" in resp.headers.get("Content-Type", "") or raw[:1] in (b"{", b"["):
                        try:
                            parsed = json.loads(raw.decode("utf-8", "ignore"))
                        except Exception:
                            parsed = None
                    # 无 charset 头时 resp.text 按 ISO-8859-1 解码 → 中文全乱码
                    # （"百度安全验证"曾被误判为页面结构变化）。一律走 smart_decode 从 raw 解。
                    _text = (smart_decode(raw, {k.lower(): v for k, v in resp.headers.items()})
                             if raw else (resp.text or ""))
                    _note_net_result(True)
                    self._at.note_latency(time.time() - _t0)
                    self._at.note_ok()
                    if _revalidating and resp.status_code == 304:
                        # 条件重验证命中：内容未变——刷新 TTL 续期并复用旧缓存。
                        # 审查六轮（M3）：读缓存失败曾落到 res_out（ok=True+空体
                        # 304）——调用方按成功拿空数据。缓存读不出时如实报失败
                        try:
                            cf = self.cache_dir / _cache_key(url, b"", method)
                            cached = _cache_decode(json.loads(cf.read_text(encoding="utf-8")))
                            try:
                                os.utime(cf, None)
                            except OSError:
                                pass
                            return cached
                        except Exception:
                            return {"ok": False, "status": 304, "body": b"", "text": "",
                                    "json": None, "url": url, "headers": {},
                                    "error": "304 续期但缓存已不可读——请重试"}
                    res_out = {"ok": True, "status": resp.status_code, "body": raw,
                               "text": _text,
                               "json": parsed,
                               "url": str(resp.url), "headers": {k.lower(): v for k, v in resp.headers.items()},
                               "raw_headers": resp.headers}
                    # 收官三轮（审查 H）：cffi.request 是模块级无状态调用——服务端
                    # Set-Cookie 从不回写 self.cookies，"单会话 cookie jar" 在默认
                    # 后端不成立（登录态多步链拿错数据还报成功）。此处补回写：
                    # 会话级 cookies 随后续请求复用（与 RequestsClient 的 Session
                    # 行为对齐）。resp.cookies 是 cffi 的 Cookies 对象。
                    try:
                        _sc = getattr(resp, "cookies", None)
                        if _sc:
                            import http.cookies as _hcookies
                            for _ck in (_sc.jar or []):
                                _nm = getattr(_ck, "name", None)
                                if _nm and _nm not in self.cookies:
                                    # 只增不改：显式传入的 cookie 优先于服务端下发
                                    _vl = getattr(_ck, "value", "") or ""
                                    self.cookies[_nm] = _vl
                    except Exception:
                        pass
                    if use_cache and self.cache_dir and method == "GET":
                        try:
                            # 审查七轮（M）：直写曾让并发读者拿到半截 JSON 缓存——
                            # 与 HttpClient._request_once 同款 tmp+replace 原子化
                            _cf = self.cache_dir / _cache_key(url, b"", method)
                            _ctmp = _cf.with_suffix(".json.tmp")
                            _ctmp.write_text(
                                json.dumps(_cache_encode(res_out), ensure_ascii=False),
                                encoding="utf-8")
                            os.replace(_ctmp, _cf)
                        except OSError:
                            pass
                    return res_out
                finally:
                    # R129 修复（P1）：stream=True 的半读响应、重试 continue 丢弃的
                    # 响应都不再被引用却不关闭——curl 句柄/连接不归还，长跑 worker
                    # fd 持续泄漏直至 "too many open files"。所有出口统一在此关闭
                    try:
                        resp.close()
                    except Exception:
                        pass
            except Exception as e:
                last_err = str(e)
                wait = self.backoff_base ** attempt
                log(f"  curl_cffi 网络异常: {e}，{wait:.1f}s 后重试", "WARN")
                time.sleep(wait)
        _note_net_result(False)
        return {"ok": False, "status": last_status, "body": b"", "text": last_err,
                "json": None, "url": url, "headers": {},
                "hint": "连续网络失败：出口/系统代理可能已变化，可运行 scripts/doctor.py（网络链路组）复查"}

    # OCR R131（M）：原 @property backoff_base 硬编码 2.0（data descriptor 会
    # 让 __init__ 的实例赋值直接 AttributeError）——已改为构造参数 + 实例属性

    def get(self, url: str, **kw) -> Dict[str, Any]:
        return self.request(url, "GET", **kw)

    def post(self, url: str, **kw) -> Dict[str, Any]:
        return self.request(url, "POST", **kw)


def make_http_client(anti: Dict[str, Any], **kw) -> Any:
    """按 anti.http_backend 选择 HTTP 客户端：curl_cffi（伪装指纹）→ requests → urllib。
    anti 可含: http_backend("auto"/"curl_cffi"/"requests"/"urllib"), impersonate,
               min_interval, max_retries, timeout, rotate_ua, proxy, cookies, headers。
    """
    backend = str(anti.get("http_backend", "auto")).lower()
    common = dict(
        min_interval=float(anti.get("min_interval", 1.0)),
        max_retries=int(anti.get("max_retries", 3)),
        timeout=float(anti.get("timeout", 20)),
        rotate_ua=bool(anti.get("rotate_ua", True)),
        proxy=anti.get("proxy"),
        use_system_proxy=bool(anti.get("use_system_proxy", False)),
        cookies=anti.get("cookies") or {},
        # 审查五轮对标 Scrapy HttpCache：三后端缓存目录统一（use_cache 仍由
        # 调用方显式传——目录存在本身不改变任何行为）
        cache_dir=anti.get("cache_dir") or Path.home() / ".universal-scraper" / "http_cache",
        extra_headers=anti.get("headers") or {},
        # R17 修复：verify 默认 True（证书校验）；目标站证书链损坏时在任务里显式
        # 写 anti.verify=false 豁免——默认必须安全
        verify=bool(anti.get("verify", True)),
    )
    common.update(kw)
    # 自适应限速开关（深度改进②）：默认开；anti.autothrottle=false 可关（回静态限速）
    _at_enabled = bool(anti.get("autothrottle", True))
    if backend in ("auto", "curl_cffi"):
        try:
            client = CurlCffiClient(impersonate=anti.get("impersonate", "chrome"), **common)
            client._backend_name = "curl_cffi"
            client._at.enabled = _at_enabled
            return client
        except Exception as _e:
            # 降级必须可诊断（TLS 指纹伪装静默失效曾导致被反爬拦截却查不到原因）
            log(f"⚠️ HTTP 后端降级: curl_cffi 不可用({_e})，改用 requests", "WARN")
    if backend in ("auto", "curl_cffi", "requests"):
        try:
            __import__("requests")  # 可用性探测：未安装则抛 ImportError
            client = RequestsClient(**common)
            client._backend_name = "requests"
            client._at.enabled = _at_enabled
            return client
        except Exception as _e:
            log(f"⚠️ HTTP 后端降级: requests 不可用({_e})，改用 urllib", "WARN")
    client = HttpClient(min_interval=common["min_interval"], max_retries=common["max_retries"],
                        timeout=common["timeout"], rotate_ua=common["rotate_ua"],
                        proxy=common["proxy"], cookies=common["cookies"] or {},
                        extra_headers=common["extra_headers"],
                        # 审查 L2：urllib 兜底曾漏传 backoff_base（其余后端都收）
                        backoff_base=common.get("backoff_base", 2.0),
                        cache_dir=common.get("cache_dir"),
                        use_system_proxy=bool(anti.get("use_system_proxy", False)))
    client._backend_name = "urllib"
    client._at.enabled = _at_enabled
    return client
