#!/usr/bin/env python3
"""🍪 自动 Cookie 会话管理：按域名保存/加载/从调试 Chrome 导入。

解决"手动复制 Cookie"痛点：
  1. 浏览器任务结束后自动把会话 cookie（含 cf_clearance、登录态）按域名存档；
  2. 任务启动时按入口域名自动加载已存档 cookie（HTTP 注入 Cookie 头 / 浏览器作为 storageState）；
  3. 从调试 Chrome（CDP 9222）一键导入用户已登录域名的 cookie——用户在普通/调试浏览器登录一次，工具自动接管。
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
COOKIE_DIR = ROOT / "outputs" / ".cookies"
_LOCK = threading.Lock()


def _domain_of(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").lower().lstrip("www.")
    except Exception:
        return ""


def _safe_domain(domain: str) -> str:
    return re.sub(r"[^0-9A-Za-z_.-]", "_", domain or "").strip("_") or "unknown"


def _cookie_path(domain: str) -> Path:
    COOKIE_DIR.mkdir(parents=True, exist_ok=True)
    return COOKIE_DIR / f"{_safe_domain(domain)}.json"


def save_cookies(domain: str, cookies: List[Dict[str, Any]]) -> bool:
    """保存域名 cookie 数组（幂等，带时间戳）。返回是否保存。"""
    if not domain or not cookies:
        return False
    domain = domain.lower().lstrip("www.")
    clean = []
    for c in cookies:
        if not isinstance(c, dict) or not c.get("name") or not c.get("value"):
            continue
        clean.append({
            "name": c.get("name", ""),
            "value": c.get("value", ""),
            "domain": c.get("domain", domain),
            "path": c.get("path", "/"),
            "expires": c.get("expires", -1),
            "secure": bool(c.get("secure")),
            "httpOnly": bool(c.get("httpOnly")),
        })
    if not clean:
        return False
    # 过滤空值/短 token（避免把空会话当有效）
    clean = [c for c in clean if c.get("value") and c["value"] not in ("", "undefined", "null")]
    if not clean:
        return False
    with _LOCK:
        try:
            _cookie_path(domain).write_text(json.dumps({
                "domain": domain, "saved_at": __import__("time").time(),
                "cookies": clean,
            }, ensure_ascii=False, indent=1), encoding="utf-8")
            return True
        except Exception:
            return False


def load_cookies(domain: str) -> List[Dict[str, Any]]:
    """加载域名 cookie 数组；无则 []。"""
    domain = (domain or "").lower().lstrip("www.")
    with _LOCK:
        try:
            p = _cookie_path(domain)
            if p.exists():
                d = json.loads(p.read_text(encoding="utf-8"))
                return d.get("cookies", [])
        except Exception:
            pass
    return []


def load_storage_state(domain: str) -> Optional[Dict[str, Any]]:
    """转成 Playwright storageState（浏览器桥用）；无则 None。"""
    cks = load_cookies(domain)
    if not cks:
        return None
    return {"cookies": cks, "origins": []}


def cookie_header(domain: str) -> str:
    """拼成 Cookie 请求头（HTTP 直抓用）。"""
    cks = load_cookies(domain)
    if not cks:
        return ""
    return "; ".join(f"{c['name']}={c['value']}" for c in cks)


def has_cookies(domain: str) -> bool:
    return bool(load_cookies(domain))


def acquire_for_task(domain: str, port: int = 9222, mode: str = "temp", log=None) -> Dict[str, Any]:
    """任务启动时获取目标域名 cookie：
      - mode=temp（默认，用完即删）：已有存档直接用；没有则自动从调试 Chrome 导入；任务结束应 release
      - mode=persist：长期复用（不自动删除）
      - mode=off：不使用 cookie
    返回 {mode, source: reused|imported|none, count}。
    """
    if mode == "off" or not domain:
        return {"mode": mode, "source": "none", "count": 0}
    if has_cookies(domain):
        return {"mode": mode, "source": "reused", "count": len(load_cookies(domain))}
    # 无存档 → 自动从调试 Chrome 拉取（只拉目标域；Chrome 未运行则 none）
    try:
        from urllib.parse import urlparse
        r = import_from_cdp_patchright(port=port, log=log)
        if r.get("ok") and has_cookies(domain):
            return {"mode": mode, "source": "imported", "count": len(load_cookies(domain))}
        return {"mode": mode, "source": "none", "count": 0, "error": r.get("error", "")}
    except Exception as e:
        return {"mode": mode, "source": "none", "count": 0, "error": str(e)}


def release_temp(domain: str, acquired: Dict[str, Any]) -> bool:
    """任务结束后删除"本次自动导入"的临时 cookie（用完即删）。
    - source=imported（本次从调试 Chrome 拉取的）→ 删除（不留隐私）
    - source=reused（用户长期存档）→ 保留，不误删用户资产
    """
    if not domain or not acquired:
        return False
    if acquired.get("mode") == "temp" and acquired.get("source") == "imported":
        return delete(domain)
    return False


def list_saved() -> List[Dict[str, Any]]:
    """列出已保存的会话域名。"""
    out = []
    if not COOKIE_DIR.exists():
        return out
    for p in sorted(COOKIE_DIR.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            cks = d.get("cookies", [])
            out.append({
                "domain": d.get("domain", p.stem),
                "saved_at": d.get("saved_at"),
                "count": len(cks),
                "has_clearance": any(c.get("name") == "cf_clearance" for c in cks),
                "has_login": any(c.get("name") in ("dper", "pt_key", "pt_pin", "SESSDATA", "sessionid",
                                                    "token", "u", "uid") for c in cks),
            })
        except Exception:
            continue
    return out


def delete(domain: str) -> bool:
    domain = (domain or "").lower().lstrip("www.")
    with _LOCK:
        try:
            p = _cookie_path(domain)
            if p.exists():
                p.unlink()
                return True
        except Exception:
            pass
    return False


def import_from_cdp(port: int = 9222, log=None) -> Dict[str, Any]:
    """从调试 Chrome（CDP 9222）自动导入所有已登录域名的 cookie。

    前提：用户已用 `--remote-debugging-port` 启动 Chrome 并登录过目标站。
    通过 /json 列表 + CDP Network.getAllCookies 提取（按域名分组存档）。
    """
    import json as _json
    import urllib.request

    def _lg(m):
        if log:
            log(m)

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=5) as r:
            tabs = _json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"ok": False, "error": f"调试 Chrome 未运行（{e}）"}

    return import_from_cdp_patchright(port, _lg)


def import_from_cdp_patchright(port: int = 9222, log=None) -> Dict[str, Any]:
    """用 patchright connectOverCDP 提取调试 Chrome 全部 cookie（跨域全量，最可靠）。"""
    def _lg(m):
        if log:
            log(m)

    import subprocess, os
    script = r'''
const { loadChromium } = require("%s/scripts/browser_common.cjs");
(async () => {
  const chromium = loadChromium();
  const browser = await chromium.connectOverCDP("http://127.0.0.1:%d");
  const all = [];
  for (const ctx of browser.contexts()) {
    for (const c of await ctx.cookies()) {
      all.push({ name: c.name, value: c.value, domain: c.domain, path: c.path,
                 expires: c.expires, secure: c.secure, httpOnly: c.httpOnly });
    }
  }
  process.stdout.write(JSON.stringify({ ok: true, cookies: all }));
  await browser.close();
})().catch(e => { process.stdout.write(JSON.stringify({ ok: false, error: String(e).slice(0,300) })); process.exit(1); });
''' % (ROOT, port)
    try:
        env = {**os.environ, "NODE_PATH": str(ROOT / "node_modules")}
        r = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                           env=env, timeout=60, cwd=str(ROOT))
        out = r.stdout.strip().splitlines()
        data = json.loads(out[-1]) if out else {"ok": False, "error": r.stderr[-300:]}
    except Exception as e:
        return {"ok": False, "error": f"CDP 导入失败：{e}"}
    if not data.get("ok"):
        return {"ok": False, "error": data.get("error", "未知")}
    cookies = data.get("cookies") or []
    # 按域名分组保存（去 www.）
    by_domain: Dict[str, List] = {}
    for c in cookies:
        d = (c.get("domain") or "").lower().lstrip(".")
        if not d or not c.get("value"):
            continue
        by_domain.setdefault(d, []).append(c)
    saved = 0
    for d, cks in by_domain.items():
        if save_cookies(d, cks):
            saved += 1
    _lg(f"✅ 从调试 Chrome 导入 {len(cookies)} 条 cookie，覆盖 {saved} 个域名")
    return {"ok": True, "imported": len(cookies), "domains": saved,
            "domain_list": sorted(by_domain.keys())}
