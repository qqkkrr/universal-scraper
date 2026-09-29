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
    # 云元数据。仅 http/https + 解析地址拒绝私网/环回/保留段。
    # US_ALLOW_PRIVATE=1 白名单开关：内网存档服务器/本地单测场景（默认仍拒绝）
    import ipaddress as _ipa
    from urllib.parse import urlsplit as _usp
    import os as _os
    _sp = _usp(url or "")
    if _sp.scheme not in ("http", "https"):
        return {"ok": False, "error": f"仅允许 http/https: {url}"}
    import socket as _sock
    _allow_private = _os.environ.get("US_ALLOW_PRIVATE") == "1"
    try:
        for _info in _sock.getaddrinfo(_sp.hostname, None):
            _ip = _ipa.ip_address(_info[4][0])
            if not _allow_private and (
                    _ip.is_private or _ip.is_loopback or _ip.is_reserved or _ip.is_link_local):
                return {"ok": False, "error": f"拒绝私有/保留地址: {_sp.hostname}"
                        "（内网/本地测试可设 US_ALLOW_PRIVATE=1）"}
    except Exception as _e:
        return {"ok": False, "error": f"域名解析失败: {_e}"}
    client = _client()
    # 收官十轮（审查）：全链路显式 Accept-Encoding: identity——curl_cffi 默认带
    # gzip/br 并**透明解压**，而 total 取自 HEAD 的 Content-Length（压缩后长度），
    # 两者语义不同 → 文本/CSV/XML 存档恒报"尺寸不符"、下载彻底失败（实测 gzip 的
    # CSV：解压后 33422B vs Content-Length 12848B）。Range 下载本就不该压缩。
    _hdr = {"Accept-Encoding": "identity", **(headers or {})}
    total = 0
    _etag = _lm = ""

    def _single_stream(reason: str = "", _noline: bool = False) -> Dict[str, Any]:
        """Range 不可用/被忽略时的统一单流回退（三处调用同口径）。"""
        if reason:
            log_fn(f"  {reason}" if not _noline else reason)
        res = client.get(url, headers=_hdr)
        if not res.get("ok"):
            return {"ok": False, "error": f"单流下载失败 HTTP {res.get('status')}"}
        body = res.get("body") or b""
        # 审查六轮（L2）：单流回退曾不验尺寸——动态生成/截断的响应照样 ok=True
        if total and len(body) != total:
            return {"ok": False, "error": f"单流尺寸不符（{len(body)} != {total}）"}
        _write(out, body)
        return {"ok": True, "size": len(body), "segments": 1, "resumed": False}

    # ── 探测 ──
    head = client.request(url, "HEAD", headers=_hdr)
    _head_h = {k.lower(): v for k, v in ((head.get("headers") or {}) if head.get("ok") else {}).items()}
    accept_ranges = False
    if head.get("ok"):
        total = int(_head_h.get("content-length") or 0)
        accept_ranges = "bytes" in (_head_h.get("accept-ranges") or "")
        _etag = str(_head_h.get("etag") or "")
        _lm = str(_head_h.get("last-modified") or "")
    if not head.get("ok") or total <= 0 or not accept_ranges:
        # 部分 server 不支持 HEAD / 不给 Accept-Ranges——退 GET Range: 0-0 探测。
        # 收官十轮（审查 M5）：探测失败或拿不到 Content-Range 时**退回单流**——
        # 原实现直接 return 错误，而同函数已有单流实现（WAF 拒 HEAD 的站点
        # 连 segments=1 都下不动，实测如此）
        probe = client.get(url, headers={"Range": "bytes=0-0", **_hdr})
        if probe.get("ok"):
            _ph = {k.lower(): v for k, v in (probe.get("headers") or {}).items()}
            rng = _ph.get("content-range") or ""
            m = re.search(r"/(\d+)$", rng)
            if m:
                total = int(m.group(1))
                accept_ranges = True
            else:
                # 无 Content-Range = 服务器忽略 Range（回了 200 全量）——此时探测
                # 响应的 Content-Length 即文件总长，走单流即可（收官十轮 M5：
                # 曾直接报错，WAF 拒 HEAD 且忽略 Range 的站点连单流都下不动）
                try:
                    total = int(_ph.get("content-length") or 0)
                except (TypeError, ValueError):
                    total = 0
                accept_ranges = False
            if not _etag:
                _etag = str(_ph.get("etag") or "")
            if not _lm:
                _lm = str(_ph.get("last-modified") or "")
    if total <= 0:
        return {"ok": False, "error": "Content-Length 未知（动态生成？）——请用单流下载"}
    if not accept_ranges or segments <= 1:
        return _single_stream(f"服务器不支持/不需要 Range——单流下载 {total}B")

    # ── 分段计划 ──
    # 审查五轮（LOW）：total < segments 时产生 bytes=N-(N-1) 非法区间（段段 416）
    segments = min(segments, total)
    seg = max(1, total // segments)
    ranges = [(i * seg, (total - 1) if i == segments - 1 else (i + 1) * seg - 1)
              for i in range(segments)]
    parts = {i: out.with_suffix(out.suffix + f".part{i}") for i in range(segments)}
    resumed = 0

    # 审查五轮（HIGH）：_need 曾只看 part 大小——同 URL 内容变化（存档更新）时
    # 旧 part 被误复用，静默产出混合体且 ok=True（数据丢失级）。防御四层：
    # ① sidecar 清单（url 指纹+total+segments）缺/不匹配 → part 全作废；
    # ② 无清单的存量 part 一律作废（来源不可信）；
    # ③ 段 0 恒重下，其内容指纹与清单比对——同 URL 同尺寸换内容（清单字段全同）
    #    的终极场景只有内容指纹能拦（Agent HIGH 的原始形态）；
    # ④ 收官十轮（审查）新增：**逐段指纹**——清单记录每个 part 的 md5，复用时
    #    逐段核对；无记录指纹的存量 part 一律重下。原实现只记段 0，导致
    #    首次下载被打断（清单尚无 seg0_fp）或远端只改中间段时，旧 part 被
    #    直接复用拼出新旧混合文件并返回 ok=True（两例均由审查实测复现）。
    #    另记录 ETag/Last-Modified：远端更新时最直接的作废信号。
    import hashlib as _hl
    manifest = out.with_suffix(out.suffix + ".manifest.json")
    _mf = {"url_fp": _hl.md5(url.encode(), usedforsecurity=False).hexdigest()[:16],
           "total": total, "segments": segments}
    import json as _json

    def _fp_of(p: Path) -> str:
        try:
            return _hl.md5(p.read_bytes(), usedforsecurity=False).hexdigest()[:16]
        except OSError:
            return ""

    def _clear_parts(msg: str) -> None:
        for p in parts.values():
            p.unlink(missing_ok=True)
        if msg:
            log_fn(msg)

    _old_mf: Dict[str, Any] = {}
    if manifest.exists():
        try:
            _loaded = _json.loads(manifest.read_text(encoding="utf-8"))
            _old_mf = _loaded if isinstance(_loaded, dict) else {}
        except Exception:
            _old_mf = {}
        # 比较只用核心字段（清单里还存着 seg0_fp/part_fps/验证器——上轮下载写入，
        # 直接整 dict 比较会恒不等 → 续传永不生效，r5 实测踩中）
        _old_core = {k: _old_mf.get(k) for k in _mf}
        if _old_core != _mf:
            _clear_parts("  ⚠️ 清单与本次下载不符（URL/大小/段数变了）——旧分段全部作废重下")
            _old_mf = {}
        else:
            # ④ ETag/Last-Modified 校验：远端已更新 → 全部作废（可比字段都存在才判）
            for _k in ("etag", "lm"):
                _ov = str(_old_mf.get(_k) or "")
                _nv = _etag if _k == "etag" else _lm
                if _ov and _nv and _ov != _nv:
                    _clear_parts(f"  ⚠️ 远端 {_k} 已变化（{_ov[:24]} → {_nv[:24]}）"
                                 "——旧分段全部作废重下")
                    _old_mf = {}
                    break
    elif any(p.exists() for p in parts.values()):
        # 存量 part 来自无清单旧版本——来源不可信，作废
        _clear_parts("  ⚠️ 发现无清单的遗留分段——来源不可信，全部作废重下")
    _safe_manifest = manifest.with_suffix(".json.tmp")
    _safe_manifest.write_text(_json.dumps(_mf), encoding="utf-8")
    _safe_manifest.replace(manifest)

    # ④ 逐段复用判定：大小吻合 **且** 与清单记录指纹一致才复用
    _recorded = _old_mf.get("part_fps") if isinstance(_old_mf.get("part_fps"), dict) else {}
    _reuse = set()
    for _i in range(1, segments):
        _p = parts[_i]
        _want = ranges[_i][1] - ranges[_i][0] + 1
        if not (_p.exists() and _p.stat().st_size == _want):
            continue
        _rec = _recorded.get(str(_i))
        if not _rec:
            continue          # 无记录指纹 = 来源不可信（首次下载被打断的残留）→ 重下
        if _fp_of(_p) == _rec:
            _reuse.add(_i)

    def _need(i: int) -> bool:
        if i == 0:
            return True  # 段 0 恒重下：用作内容指纹校验（见 _dl 内 seg0_fp）
        return i not in _reuse

    todo = [i for i in range(segments) if _need(i)]
    resumed = segments - len(todo)
    if resumed:
        # OCR R6 终审：resumed 本就不含段 0（_need(0) 恒 True）——再减 1 曾把
        # 续传进度报少一段（8 段就位 7 段时报成 6/8）
        log_fn(f"  断点续传：{resumed}/{segments} 段已就位（指纹校验通过），另段 0 恒校验重下")

    # ── 并行抓段 ──
    class _ServerIgnoredRange(Exception):
        pass

    def _dl(i: int) -> None:
        lo, hi = ranges[i]
        res = client.get(url, headers={"Range": f"bytes={lo}-{hi}", **_hdr})
        # 审查五轮（MED）：200 曾被放行——服务器忽略 Range 时每段各拉全量
        # （8×带宽+8×内存后才发现失败）。206 才是分段语义
        if res.get("status") == 200:
            raise _ServerIgnoredRange()
        if res.get("status") != 206:
            raise RuntimeError(f"段 {i} HTTP {res.get('status')}")
        body = res.get("body") or b""
        if len(body) != hi - lo + 1:
            raise RuntimeError(f"段 {i} 字节数不齐（{len(body)} != {hi - lo + 1}）")
        # 收官十轮（审查 M1）：曾只查 body 长度——服务器回"起点被忽略但长度正确"
        # 的 206（如 Content-Range: bytes 0-999/4000）时四段拿到同一段错字节，
        # 拼出大小正确、内容全错的成品且 ok=True。Content-Range 必须与请求一致
        _cr = ""
        for _hk, _hv in (res.get("headers") or {}).items():
            if str(_hk).lower() == "content-range":
                _cr = str(_hv)
                break
        if _cr:
            _m = re.match(r"\s*bytes\s+(\d+)\s*-\s*(\d+)\s*/", _cr)
            if not _m or int(_m.group(1)) != lo or int(_m.group(2)) != hi:
                raise RuntimeError(f"段 {i} Content-Range 与请求不符（{_cr.strip()[:40]}"
                                   f" 应为 bytes {lo}-{hi}）")
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
    def _write_manifest(**extra) -> None:
        """清单落盘（tmp+replace 原子）；带 part_fps/etag/lm 供下轮复用时校验。"""
        _sm = manifest.with_suffix(".json.tmp")
        _sm.write_text(_json.dumps({**_mf, **extra}), encoding="utf-8")
        _sm.replace(manifest)

    if not ignored_range and not errors and todo:
        # 审查五轮（HIGH 终闭环）：段 0 内容指纹比对——同 URL 同尺寸换内容时
        # url/total/segments 清单字段全同，唯有内容指纹能拦混合体
        _seg0_fp = _fp_of(parts[0])
        _prev_fp = _old_mf.get("seg0_fp")
        if _prev_fp and _prev_fp != _seg0_fp:
            _clear_parts("")
            # 以本轮实测指纹固化清单——否则重跑永远对着旧指纹拦（r5 实测踩中）
            _write_manifest(seg0_fp=_seg0_fp, etag=_etag, lm=_lm)
            return {"ok": False,
                    "error": "段 0 内容指纹与上次下载不一致（同 URL 数据已更新）——"
                             "全部分段已作废，请重跑本命令重新下载（不会产出混合体）"}
        # 校验通过：固化段 0 指纹 + **逐段指纹**（+验证器），供下次续传逐段核对
        _write_manifest(seg0_fp=_seg0_fp, etag=_etag, lm=_lm,
                        part_fps={str(i): _fp_of(parts[i]) for i in range(segments)})
    elif not ignored_range and todo:
        # 收官十轮（审查 H1）：中断路径同样固化**已完成段**的指纹与段 0 指纹——
        # 否则下一轮续传面对"无记录指纹的存量 part"，只能整份重下（或如原实现
        # 般盲信大小而复用来源不明的 part，拼出新旧混合体）
        _seg0_fp = _fp_of(parts[0]) if parts[0].exists() else ""
        _write_manifest(**({"seg0_fp": _seg0_fp} if _seg0_fp else {}),
                        etag=_etag, lm=_lm,
                        part_fps={str(i): fp for i in range(segments)
                                  if parts[i].exists() and (fp := _fp_of(parts[i]))})
    if ignored_range:
        # 服务器不支持 Range——撤掉全部 part，退单流
        _clear_parts("  服务器忽略 Range——退回单流下载")
        return _single_stream(_noline=True)
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
    _tmp_size = tmp_out.stat().st_size
    if _tmp_size != total:
        tmp_out.unlink(missing_ok=True)
        # 收官十轮（审查 L1）：曾先 unlink 再在错误串里 stat()——删除后 stat 抛
        # FileNotFoundError，本该返回的结构化失败变成异常冒泡
        return {"ok": False, "error": f"拼接后大小不符（{_tmp_size} != {total}）"}
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
