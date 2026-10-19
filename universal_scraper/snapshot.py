#!/usr/bin/env python3
"""页面快照（snapshot）：一次抓取、落盘、离线反复解析。

适用：选择器/正则口径反复调整、但不想反复打目标站的场景。
  - 每个 URL 保存 page_0001.json（状态/最终 URL/时间/文本/哈希）
  - 同时保存 page_0001.md（可读文本），供 Python/正则离线解析
  - 不做签名逆向、不伪造指纹、不替用户过验证码
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .quick import fetch_url


def _safe_slug(i: int) -> str:
    return f"page_{i:04d}"


def capture(urls: Iterable[str], out_dir: Path, *, browser: bool = False,
            proxy: Optional[str] = None, interval: float = 2.0,
            timeout: float = 60, resume: bool = False,
            cookies: Optional[str] = None) -> Dict[str, Any]:
    urls = [str(u) for u in urls if str(u or "").strip()]
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    seen: set = set()
    files: List[str] = []
    errors: List[Dict[str, str]] = []
    ok_count = 0
    for i, raw in enumerate(urls, 1):
        url = str(raw or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        name = _safe_slug(i)
        meta_path = out / f"{name}.json"
        md_path = out / f"{name}.md"
        if resume and meta_path.exists():
            try:
                old = json.loads(meta_path.read_text(encoding="utf-8"))
                if old.get("url") == url and md_path.exists():
                    files.append(str(meta_path))
                    if int(old.get("status") or 0) and not old.get("error"):
                        ok_count += 1
                    continue
            except Exception:
                pass
        try:
            res = fetch_url(url, browser=browser, proxy=proxy, timeout=timeout,
                            cookie=cookies)
        except Exception as e:
            # 单 URL 失败不拖垮整批：落一条错误快照，继续下一个
            res = {"url": url, "status": 0, "text": "", "error": f"{type(e).__name__}: {e}"}
        text = str(res.get("text") or "")
        is_html = text.lstrip().startswith(("<", "<!doctype", "<!DOCTYPE"))
        md_text = text
        html_name = ""
        if is_html:
            try:
                from .extractors import html_to_markdown
                md_text = html_to_markdown(text, base_url=res.get("url") or url)
            except Exception:
                md_text = text
            html_path = out / f"{name}.html"
            htmp = html_path.with_suffix(".html.tmp")
            htmp.write_text(text, encoding="utf-8")
            htmp.replace(html_path)
            html_name = html_path.name
        meta = {
            "url": url,
            "final_url": res.get("url") or url,
            "status": int(res.get("status") or 0),
            "error": res.get("error") or "",
            "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "chars": len(text),
            "sha256": hashlib.sha256(text.encode("utf-8", "replace")).hexdigest(),
            "text_file": md_path.name,
            "html_file": html_name,
            "browser": bool(browser),
        }
        tmp = meta_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(meta_path)
        mtmp = md_path.with_suffix(".md.tmp")
        mtmp.write_text(md_text, encoding="utf-8")
        mtmp.replace(md_path)
        # 快照可能含登录页/个人信息：属主可读写即可
        for _p in (meta_path, md_path):
            try:
                os.chmod(_p, 0o600)
            except OSError:
                pass
        if html_name:
            try:
                os.chmod(out / html_name, 0o600)
            except OSError:
                pass
        files.append(str(meta_path))
        if meta["error"] or not meta["status"]:
            errors.append({"url": url, "error": meta["error"] or "no status"})
        else:
            ok_count += 1
        if interval and i < len(urls):
            time.sleep(max(0.0, float(interval)))
    return {"out": str(out), "captured": len(files), "ok": ok_count,
            "errors": errors, "files": files}
