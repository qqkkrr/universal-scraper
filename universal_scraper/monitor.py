#!/usr/bin/env python3
"""增量监控（R101 新能力，对标 Crawlee sitemap-diff）：
- sitemap_diff: 抓 sitemap → 与上次快照 diff → 新增/消失 URL 清单
- push_webhook: 把 diff 结果 POST 到用户 webhook（复用 middleware.webhook 的口径）
- watch: 供 webui 调度线程/cli monitor 调用的单轮监视（diff → 有变化才推送）

快照按 <md5(url)>.json 存 ~/.universal_scraper/monitor/<name>.json，
跨运行持久；损坏按空快照处理并隔离。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List

MONITOR_DIR = Path.home() / ".universal_scraper" / "monitor"


def _snapshot_path(name: str) -> Path:
    # R91 同口径：名字净化，防路径逃逸
    import re
    safe = re.sub(r"[^\w\u4e00-\u9fff.-]", "_", str(name or ""))
    return MONITOR_DIR / f"{safe}.json"


def _load_snapshot(name: str) -> Dict[str, Any]:
    p = _snapshot_path(name)
    try:
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("urls"), list):
                return data
    except Exception as e:
        # OCR R131（M）：读取失败曾静默——损坏/权限问题无从区分"从未监控过"
        import sys as _sys
        print(f"⚠️ 监控快照读取失败（{type(e).__name__}: {str(e)[:60]}）: {p}",
              file=_sys.stderr)
    # 损坏隔离（R18 cookies 同款口径）；审查八轮（L）：固定 .corrupt 后缀曾让
    # 二次损坏覆盖前一份证据——时间戳+uuid 后缀（PoolState/QuotaLedger 同款）
    try:
        if p.exists():
            import uuid as _uuid
            p.rename(p.with_suffix(f".corrupt.{int(time.time())}.{_uuid.uuid4().hex[:6]}"))
    except Exception:
        pass
    return {"urls": [], "ts": 0}


def _save_snapshot(name: str, urls: List[str]) -> None:
    p = _snapshot_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    # R105 修复（P3）：tmp+os.replace 原子写（并发 watch 不再交错写坏 JSON）
    # OCR R131（H）：固定 tmp 名曾让两个并发 watch 互相截断同一 tmp——
    # 换唯一临时名（mkstemp），写方各写各的，replace 原子生效
    import os, tempfile as _tf
    fd, tmp = _tf.mkstemp(dir=str(p.parent), suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"urls": urls, "ts": time.time()}, f, ensure_ascii=False)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def fetch_sitemap_urls(url: str, timeout: int = 30,
                       max_urls: int = 20000) -> List[str]:
    """抓 sitemap（含一层 sitemap index 递归），返回 URL 列表。"""
    from .core import make_http_client
    import re
    client = make_http_client({"min_interval": 0.2, "timeout": timeout, "max_retries": 1})
    out: List[str] = []
    seen: set = set()
    _fetch_errors: List[str] = []  # OCR R131（M）：网络异常 URL 记账（供调用方诊断）

    def _one(u: str, depth: int = 0):
        if u in seen or depth > 2 or len(out) >= max_urls:
            return
        # R128 修复：非 HTTP URL（file:/ftp: 等）传入 client.get 会抛 ValueError
        if not u.startswith(("http://", "https://")):
            return
        seen.add(u)
        try:
            res = client.get(u)
        except Exception:
            # OCR R131（M）：网络层异常曾与"取回失败"同权静默——至少计数可见
            _fetch_errors.append(u)
            return
        if not res.get("ok"):
            return
        text = res.get("text", "") or ""
        # 审查八轮（MEDIUM）：`.gz` 子 sitemap 曾被当索引递归抓——但正文是 gzip
        # **二进制**，`<loc>` 正则零命中 → 该子 sitemap 贡献 0 条且不记错（监控恒判
        # "抓取为空"，混合索引下还会把 gz 里全部 URL 当 removed 推假告警并覆写基线）。
        # 这里按 gzip 魔数/扩展名解压后再扫。
        _body = res.get("body")
        if (isinstance(_body, (bytes, bytearray)) and bytes(_body[:2]) == b"\x1f\x8b") \
                or u.split("?", 1)[0].lower().endswith(".gz"):
            try:
                import gzip as _gz
                _raw = bytes(_body) if isinstance(_body, (bytes, bytearray)) else text.encode("utf-8", "ignore")
                text = _gz.decompress(_raw).decode("utf-8", "ignore")
            except Exception as _ge:
                _fetch_errors.append(f"{u}（gzip 解压失败: {type(_ge).__name__}）")
                return
        for m in re.finditer(r"<loc>\s*([^<]+?)\s*</loc>", text[:32 * 1024 * 1024]):
            loc = m.group(1).strip()
            if loc in seen:
                continue
            # OCR R131（M）：'sitemap' in URL 曾把普通页面误判成子索引递归抓取
            # 审查八轮（M）：收紧后仍漏两类叶子——路径段 startswith("sitemap")
            # 命中 /article/sitemap-guide.html（末段按 - 切出 "sitemap"）；后缀
            # 一刀切命中 /download/feed.xml、/data.xml?id=1 等真叶子。它们被
            # 递归抓取且不进 out：监控覆盖面静默缺失 + 每轮白烧请求。收窄为
            # "文件名以 sitemap 开头的 xml/gz"——漏判索引只少递归一层（loc 仍
            # 进 out），误判叶子的代价是丢数据
            _path = loc.split("?", 1)[0].lower()
            _fname = _path.rstrip("/").rsplit("/", 1)[-1]
            _is_index = bool(re.match(r"sitemap.*(\.xml(\.gz)?|\.gz)$", _fname))
            if _is_index:
                _one(loc, depth + 1)
                if len(out) >= max_urls:
                    return
            else:
                seen.add(loc)
                out.append(loc)
                if len(out) >= max_urls:
                    return

    _one(url)
    if _fetch_errors:
        # OCR R131（M）：网络异常曾完全不可见——sitemap 空结果时无法区分
        # "站点真没了" vs "网络抖动"。stderr 汇总一行
        import sys as _sys
        print(f"⚠️ sitemap 抓取中 {len(_fetch_errors)} 个 URL 网络异常"
              f"（如 {' '.join(_fetch_errors[:2])}）", file=_sys.stderr)
    return out


def sitemap_diff(name: str, sitemap_url: str, timeout: int = 30,
                 max_urls: int = 20000) -> Dict[str, Any]:
    """抓当前 sitemap 并与快照 diff。返回 {added, removed, total, changed}。
    changed=True 时才落新快照（失败/空抓不动快照，防止网络抖动清空基线）。"""
    current = fetch_sitemap_urls(sitemap_url, timeout=timeout, max_urls=max_urls)
    if not current:
        return {"added": [], "removed": [], "total": 0, "changed": False,
                "note": "sitemap 抓取为空——不更新快照（防抖动清基线）"}
    old = set(_load_snapshot(name).get("urls") or [])
    # R105 修复（P2）：比例护栏——当前 URL 数不足基线 10% 视为截断/降级
    # （CDN 陈旧副本、动态 sitemap 半输出），跳过 diff 不砸基线不误报
    if old and len(current) * 10 < len(old):
        return {"added": [], "removed": [], "total": len(current),
                "changed": False, "baseline": len(old),
                "note": f"当前 {len(current)} 条不足基线 {len(old)} 的 10%——疑似截断，跳过本轮 diff"}
    cur = set(current)
    added = sorted(cur - old)
    removed = sorted(old - cur)
    changed = bool(added or removed)
    if changed:
        _save_snapshot(name, current)
    else:
        # OCR R131（L）：无变化时快照 ts 从不刷新——"监控还活着吗"无从判断。
        # 每小时心跳续期一次（不砸基线，只更新 ts）
        try:
            _meta = _load_snapshot(name)
            if time.time() - float(_meta.get("ts") or 0) > 3600:
                _save_snapshot(name, current)
        except Exception:
            pass
    return {"added": added[:200], "removed": removed[:200],
            "added_count": len(added), "removed_count": len(removed),
            "total": len(current), "changed": changed}


def push_webhook(url: str, payload: Dict[str, Any], timeout: int = 15) -> Dict[str, Any]:
    """把监控事件 POST 到用户 webhook（JSON）。返回 {ok, status, error}。"""
    from .core import make_http_client
    try:
        client = make_http_client({"min_interval": 0, "timeout": timeout, "max_retries": 1})
        res = client.post(url, json_data=payload,
                          headers={"Content-Type": "application/json"})
        return {"ok": bool(res.get("ok")), "status": res.get("status", 0),
                "error": "" if res.get("ok") else (res.get("text") or "")[:200]}
    except Exception as e:
        return {"ok": False, "status": 0, "error": f"{type(e).__name__}: {str(e)[:160]}"}


def watch_once(name: str, sitemap_url: str, webhook: str = "",
               timeout: int = 30) -> Dict[str, Any]:
    """单轮监视：diff → 有变化且配了 webhook 才推送。返回 diff + 推送结果。"""
    diff = sitemap_diff(name, sitemap_url, timeout=timeout)
    result: Dict[str, Any] = {"name": name, "sitemap": sitemap_url, **diff,
                              "webhook": None}
    if diff.get("changed") and webhook:
        result["webhook"] = push_webhook(webhook, {
            "event": "sitemap_changed", "monitor": name,
            "sitemap": sitemap_url,
            "added_count": diff.get("added_count", 0),
            "removed_count": diff.get("removed_count", 0),
            "added": diff.get("added", [])[:50],
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
    return result
