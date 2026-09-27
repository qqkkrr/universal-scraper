#!/usr/bin/env python3
"""⬇️ 慢站大文件 Range 并行下载器（实战反馈四#4，aqistudy 存档下载战训固化）。

与 safe_http_template 的单流 _polite_get 互补：历史存档站（QuotSoft/统计网盘）
单连接只有几十 KB/s，Range 分段并行 × 文件级并发 + 断点续传是通用能力。

用法:
  from universal_scraper.rangedl import rangedl
  rangedl(url, out=Path("data.zip"))          # 默认 8 段 × 并发 3

  python3 -m universal_scraper.cli rangedl <url> --out data.zip [--segments 8]
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, Optional

from .core import make_http_client, log

_PART_OK_BYTES = 0  # .partN 文件完成标记（空文件不存在=未完成）


def _client(anti: Optional[dict] = None):
    base = {"min_interval": 0.0, "timeout": 60, "http_backend": "auto"}
    base.update(anti or {})
    return make_http_client(base)


def rangedl(url: str, out: str | Path, segments: int = 8, concurrency: int = 3,
            headers: Optional[Dict[str, str]] = None, log_fn=log) -> Dict[str, Any]:
    """Range 分段并行下载 + 断点续传。

    - HEAD 探测 Content-Length 与 Accept-Ranges；不支持 Range 时退回单流下载
    - 分段落盘 <out>.part{N}；已存在的 part 且大小吻合直接复用（断点续传）
    - 全部就位后按序拼接为 out，成功后清理 part
    - 返回 {"ok", "size", "segments", "resumed", "error"}
    """
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    # 审查五轮（LOW）：与 fetch_url/jsrecon 同口径——CLI/import 直达也可被指向
    # 云元数据。仅 http/https + 解析地址拒绝私网/环回/保留段
    import ipaddress as _ipa
    from urllib.parse import urlsplit as _usp
    _sp = _usp(url or "")
    if _sp.scheme not in ("http", "https"):
        return {"ok": False, "error": f"仅允许 http/https: {url}"}
    import socket as _sock
    try:
        for _info in _sock.getaddrinfo(_sp.hostname, None):
            _ip = _ipa.ip_address(_info[4][0])
            if _ip.is_private or _ip.is_loopback or _ip.is_reserved or _ip.is_link_local:
                return {"ok": False, "error": f"拒绝私有/保留地址: {_sp.hostname}"}
    except Exception as _e:
        return {"ok": False, "error": f"域名解析失败: {_e}"}
    client = _client()
    # ── 探测 ──
    head = client.request(url, "HEAD")
    if not head.get("ok"):
        # 部分 server 不支持 HEAD——退 GET Range: 0-0 探测
        probe = client.get(url, headers={"Range": "bytes=0-0", **(headers or {})})
        if not probe.get("ok"):
            return {"ok": False, "error": f"探测失败 HTTP {probe.get('status')}: {url[:80]}"}
        rng = (probe.get("headers") or {}).get("Content-Range") or \
              (probe.get("headers") or {}).get("content-range", "")
        m = re.search(r"/(\d+)$", rng)
        if not m:
            return {"ok": False, "error": "服务器不支持 Range（无 Content-Range）——请用单流下载"}
        total = int(m.group(1))
        accept_ranges = True
    else:
        h = {k.lower(): v for k, v in (head.get("headers") or {}).items()}
        total = int(h.get("content-length") or 0)
        accept_ranges = "bytes" in (h.get("accept-ranges") or "")
    if total <= 0:
        return {"ok": False, "error": "Content-Length 未知（动态生成？）——请用单流下载"}
    if not accept_ranges or segments <= 1:
        log_fn(f"  服务器不支持/不需要 Range——单流下载 {total}B")
        res = client.get(url, headers=headers)
        if not res.get("ok"):
            return {"ok": False, "error": f"单流下载失败 HTTP {res.get('status')}"}
        body = res.get("body") or b""
        # 审查六轮（L2）：单流回退曾不验尺寸——动态生成/截断的响应照样 ok=True
        if total and len(body) != total:
            return {"ok": False, "error": f"单流尺寸不符（{len(body)} != {total}）"}
        _write(out, body)
        return {"ok": True, "size": len(body), "segments": 1, "resumed": False}

    # ── 分段计划 ──
    # 审查五轮（LOW）：total < segments 时产生 bytes=N-(N-1) 非法区间（段段 416）
    segments = min(segments, total)
    seg = max(1, total // segments)
    ranges = [(i * seg, (total - 1) if i == segments - 1 else (i + 1) * seg - 1)
              for i in range(segments)]
    parts = {i: out.with_suffix(out.suffix + f".part{i}") for i in range(segments)}
    resumed = 0

    # 审查五轮（HIGH）：_need 曾只看 part 大小——同 URL 内容变化（存档更新）时
    # 旧 part 被误复用，静默产出混合体且 ok=True（数据丢失级）。防御三层：
    # ① sidecar 清单（url 指纹+total+segments）缺/不匹配 → part 全作废；
    # ② 无清单的存量 part 一律作废（来源不可信）；
    # ③ 段 0 恒重下，其内容指纹与清单比对——同 URL 同尺寸换内容（清单字段全同）
    #    的终极场景只有内容指纹能拦（Agent HIGH 的原始形态）
    import hashlib as _hl
    manifest = out.with_suffix(out.suffix + ".manifest.json")
    _mf = {"url_fp": _hl.md5(url.encode(), usedforsecurity=False).hexdigest()[:16],
           "total": total, "segments": segments}
    import json as _json
    _old_mf = None
    if manifest.exists():
        try:
            _old_mf = _json.loads(manifest.read_text(encoding="utf-8"))
        except Exception:
            _old_mf = None
        # 比较只用核心字段（清单里还存着 seg0_fp 内容指纹——上轮下载写入，
        # 直接整 dict 比较会恒不等 → 续传永不生效，r5 实测踩中）
        _old_core = {k: _old_mf.get(k) for k in _mf} if isinstance(_old_mf, dict) else None
        if _old_core != _mf:
            for p in parts.values():
                p.unlink(missing_ok=True)
            log_fn("  ⚠️ 清单与本次下载不符（URL/大小/段数变了）——旧分段全部作废重下")
    elif any(p.exists() for p in parts.values()):
        # 存量 part 来自无清单旧版本——来源不可信，作废
        for p in parts.values():
            p.unlink(missing_ok=True)
        log_fn("  ⚠️ 发现无清单的遗留分段——来源不可信，全部作废重下")
    _safe_manifest = manifest.with_suffix(".json.tmp")
    _safe_manifest.write_text(_json.dumps({**_mf, **({"seg0_fp": _old_mf.get("seg0_fp")}
                                                     if isinstance(_old_mf, dict) and _old_mf.get("seg0_fp") else {})}),
                              encoding="utf-8")
    _safe_manifest.replace(manifest)

    def _need(i: int) -> bool:
        if i == 0:
            return True  # 段 0 恒重下：用作内容指纹校验（见 _dl 内 seg0_fp）
        p = parts[i]
        want = ranges[i][1] - ranges[i][0] + 1
        return not (p.exists() and p.stat().st_size == want)

    todo = [i for i in range(segments) if _need(i)]
    resumed = segments - len(todo)
    if resumed:
        # OCR R6 终审：resumed 本就不含段 0（_need(0) 恒 True）——再减 1 曾把
        # 续传进度报少一段（8 段就位 7 段时报成 6/8）
        log_fn(f"  断点续传：{resumed}/{segments} 段已就位，另段 0 恒校验重下")

    # ── 并行抓段 ──
    class _ServerIgnoredRange(Exception):
        pass

    def _dl(i: int) -> None:
        lo, hi = ranges[i]
        res = client.get(url, headers={"Range": f"bytes={lo}-{hi}", **(headers or {})})
        # 审查五轮（MED）：200 曾被放行——服务器忽略 Range 时每段各拉全量
        # （8×带宽+8×内存后才发现失败）。206 才是分段语义
        if res.get("status") == 200:
            raise _ServerIgnoredRange()
        if res.get("status") != 206:
            raise RuntimeError(f"段 {i} HTTP {res.get('status')}")
        body = res.get("body") or b""
        if len(body) != hi - lo + 1:
            raise RuntimeError(f"段 {i} 字节数不齐（{len(body)} != {hi - lo + 1}）")
        _write(parts[i], body)

    errors = []
    ignored_range = False
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
        futs = {ex.submit(_dl, i): i for i in todo}
        for fu in as_completed(futs):
            i = futs[fu]
            try:
                fu.result()
                log_fn(f"  段 {i + 1}/{segments} ✓")
            except _ServerIgnoredRange:
                ignored_range = True
                for f in futs:
                    f.cancel()
            except Exception as e:
                errors.append(f"段{i}: {type(e).__name__}: {str(e)[:60]}")
    if not ignored_range and not errors and todo:
        # 审查五轮（HIGH 终闭环）：段 0 内容指纹比对——同 URL 同尺寸换内容时
        # url/total/segments 清单字段全同，唯有内容指纹能拦混合体
        _seg0_fp = _hl.md5(parts[0].read_bytes(), usedforsecurity=False).hexdigest()[:16]
        _prev_fp = (_old_mf or {}).get("seg0_fp")
        if _prev_fp and _prev_fp != _seg0_fp:
            for p in parts.values():
                p.unlink(missing_ok=True)
            # 以本轮实测指纹固化清单——否则重跑永远对着旧指纹拦（r5 实测踩中）
            _sm2 = manifest.with_suffix(".json.tmp")
            _sm2.write_text(_json.dumps(dict(_mf, seg0_fp=_seg0_fp)), encoding="utf-8")
            _sm2.replace(manifest)
            _old_mf = dict(_mf, seg0_fp=_seg0_fp)
            return {"ok": False,
                    "error": "段 0 内容指纹与上次下载不一致（同 URL 数据已更新）——"
                             "全部分段已作废，请重跑本命令重新下载（不会产出混合体）"}
        # 校验通过：把本次段 0 指纹固化进清单，供下次续传比对
        if _prev_fp != _seg0_fp:
            _mf2 = dict(_mf, seg0_fp=_seg0_fp)
            _sm2 = manifest.with_suffix(".json.tmp")
            _sm2.write_text(_json.dumps(_mf2), encoding="utf-8")
            _sm2.replace(manifest)
    if ignored_range:
        # 服务器不支持 Range——撤掉全部 part，退单流
        for p in parts.values():
            p.unlink(missing_ok=True)
        log_fn("  服务器忽略 Range——退回单流下载")
        res = client.get(url, headers=headers)
        if not res.get("ok"):
            return {"ok": False, "error": f"单流下载失败 HTTP {res.get('status')}"}
        body = res.get("body") or b""
        if total and len(body) != total:
            return {"ok": False, "error": f"单流尺寸不符（{len(body)} != {total}）"}
        _write(out, body)
        return {"ok": True, "size": len(body), "segments": 1, "resumed": False}
    if errors:
        _have = sum(1 for i in range(segments) if not _need(i))
        return {"ok": False,
                "error": f"{len(errors)} 段失败（{_have}/{segments} 段已就位可续传）: " + "; ".join(errors[:3])}

    # ── 拼接 ──
    # 审查五轮（MED-LOW）：曾原地写 out 且先删 part 后验大小——异常路径留截断
    # 残file冒充成品。改 tmp 拼接 → 验尺寸 → os.replace → 清 part
    tmp_out = out.with_suffix(out.suffix + ".tmp")
    with open(tmp_out, "wb") as w:
        for i in range(segments):
            w.write(parts[i].read_bytes())
    if tmp_out.stat().st_size != total:
        tmp_out.unlink(missing_ok=True)
        return {"ok": False, "error": f"拼接后大小不符（{tmp_out.stat().st_size} != {total}）"}
    tmp_out.replace(out)
    for p in parts.values():
        p.unlink(missing_ok=True)
    # manifest 保留：此刻 part 已清（无害），下次中断续传仍需清单校验 part 来源
    # （审查五轮实测：删清单曾让正常续传被"无清单作废"逻辑误杀）
    log_fn(f"✅ 下载完成 {out}（{total}B，{segments} 段）")
    return {"ok": True, "size": total, "segments": segments, "resumed": resumed > 0}


def _write(p: Path, data: bytes) -> None:
    tmp = p.with_suffix(p.suffix + ".dl")
    tmp.write_bytes(data)
    tmp.replace(p)
