#!/usr/bin/env python3
"""🍪 自动 Cookie 会话管理：按域名保存/加载/从调试 Chrome 导入。

解决"手动复制 Cookie"痛点：
  1. 浏览器任务结束后自动把会话 cookie（含 cf_clearance、登录态）按域名存档；
  2. 任务启动时按入口域名自动加载已存档 cookie（HTTP 注入 Cookie 头 / 浏览器作为 storageState）；
  3. 从调试 Chrome（CDP 9222）一键导入用户已登录域名的 cookie——用户在普通/调试浏览器登录一次，工具自动接管。
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
def _cookie_dir() -> Path:
    """cookie 存档目录（收官十五轮：支持 US_COOKIE_DIR 覆盖）。

    CI/多任务隔离与 e2e 测试需要把凭据档案重定向到临时目录——此前只认固定的
    outputs/.cookies，测试只能污染真实档案（或者在 CI 里读到空目录而误判失败）。
    与仓库其它 US_* 环境开关同风格，默认行为不变。
    """
    env = os.environ.get("US_COOKIE_DIR", "").strip()
    if env:
        try:
            return Path(env).expanduser()
        except Exception:
            pass
    return ROOT / "outputs" / ".cookies"


COOKIE_DIR = _cookie_dir()
_LOCK = threading.Lock()

# 常见"登录态"cookie 名（健康检查用）：存档里一条都没有时提示可能未登录
LOGIN_COOKIE_NAMES = {
    "dper", "pt_key", "pt_pin", "SESSDATA", "sessionid", "token", "u", "uid",
    "sid", "sid_tt", "sessionid_ss", "passport_lt", "lt", "remind_saved_cmp",
    "SUB", "SUBP", "BA_HECTOR", "passport_auth", "auth_token", "access_token",
    "kps", "ctoken", "login_secs", "ETK", "RememberMe",
}

# 时钟偏斜容忍（秒）：提前判过期。审查二轮（H）：原公式 `exp < now - grace`
# 是"延长死 cookie 寿命 5 分钟"，与本文件"最坏情况处理"口径相反——早判的代价
# 只是早 5 分钟重登，晚判的代价是拿死登录态发请求
_EXPIRY_EARLY = 300.0


def _cookie_expired(c: Dict[str, Any], now: Optional[float] = None) -> bool:
    """单条 cookie 是否已硬过期（expires 为 0/负数 = 会话 cookie，不算过期）。
    审查修复（P2）：垃圾 expires（ISO 字符串等）曾 fail-open 成"永不过期"——
    验证函数的失配必须按最坏情况处理（判过期），否则死登录态被永信。"""
    raw = c.get("expires", -1)
    try:
        exp = float(raw or -1)
    except (TypeError, ValueError):
        return True
    if exp <= 0:
        return False
    return exp < (now if now is not None else time.time()) + _EXPIRY_EARLY


def _norm_domain(d: str) -> str:
    """精确剥离开头的一个 www. 标签。此前用 lstrip 按字符集剥离，会把
    weibo.com→eibo.com、wikipedia.org→ikipedia.org 等真实域名毁掉，
    且 mangled 名可能与其它真实域撞档导致登录态跨站外发。

    审查十一轮（H）：入口主机未规范化——端口/userinfo/尾点原样进档名与域匹配
    （engine_v3 主路径对 start_url 用字符串 split 取 host，`http://x.com:8443`
    → `x.com:8443` 作 cookie_domain）：存不进（域树过滤全拒）、读不出（档名不
    匹配），登录态静默全丢。auto.py 已用 urlparse.hostname 修过同型，此处统一。"""
    s = (d or "").strip().lower()
    if "//" in s:                        # 带 scheme/路径的整 URL → 取 host 段
        s = s.split("//", 1)[1]
    if "@" in s:                         # 剥 userinfo（user:pw@host）
        s = s.rsplit("@", 1)[1]
    if s.startswith("["):                # IPv6 字面量 [::1]:80
        s = s[1:].split("]", 1)[0]
    elif ":" in s:                       # 剥端口
        s = s.split(":", 1)[0]
    s = s.rstrip(".")                    # 剥尾点（合法 FQDN 形态 example.com.）
    return re.sub(r"^www\.", "", s)


def _safe_domain(domain: str) -> str:
    return re.sub(r"[^0-9A-Za-z_.-]", "_", domain or "").strip("_") or "unknown"


def _cookie_path(domain: str) -> Path:
    # R18 加固：登录凭据目录仅属主可进（0755 时其他本机用户可列出归档文件名）
    COOKIE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        COOKIE_DIR.chmod(0o700)  # 已存在时 mkdir 的 mode 不生效，显式补一次
    except Exception:
        pass
    return COOKIE_DIR / f"{_safe_domain(domain)}.json"


def _cookie_matches_archive(cdomain: str, domain: str) -> bool:
    """cookie 自身域与归档域是否同一棵域树（RFC6265 域匹配的宽松版）：
    归档域是 cookie 域的子域或完全相等。审查三轮（H）：曾双向包含——cookie 域
    是归档域的子域也命中（sub.example.com 的 cookie 种给 example.com，跨域）。
    Cookie 域匹配的 RFC 6265 语义是单向：请求域必须是 cookie 域或其子域。
    www 前缀先归一化：浏览器导出的 cookie 常挂 www 域，任务用裸域——用户实际
    场景必须互通（行为验证发现的回归），归一化后 RFC 单向照常拒绝真跨域。"""
    d = _norm_domain(str(cdomain or "").lower().lstrip("."))
    a = _norm_domain(str(domain or "").lower().lstrip("."))
    if not d or not a:
        return False
    return d == a or a.endswith("." + d)


def save_cookies(domain: str, cookies: List[Dict[str, Any]]) -> bool:
    """保存域名 cookie 数组（幂等，带时间戳）。返回是否保存。
    只保留与归档域同域树的条目：浏览器 context 全量 cookie 中的第三方域
    （统计/SSO 中间域等）不得混入——否则 HTTP 播种会把外源会话重放给目标站。"""
    if not domain or not cookies:
        return False
    domain = _norm_domain(domain)
    clean = []
    for c in cookies:
        if not isinstance(c, dict) or not c.get("name") or not c.get("value"):
            continue
        if not _cookie_matches_archive(c.get("domain"), domain):
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
        tmp = None
        try:
            p = _cookie_path(domain)
            # 原子写：临时文件 + os.replace，防跨进程并发写坏存档（截断 JSON 会被
            # load_cookies 误判为 corrupt 而丢失登录态）
            import uuid as _uuid
            tmp = p.with_suffix(f".{os.getpid()}.{_uuid.uuid4().hex[:6]}.tmp")
            # R18 修复：以 0600 创建临时文件再写入——此前先 write_text（umask 默认
            # 0644）后 chmod，存在毫秒级全局可读窗口，登录凭据明文短暂暴露
            _fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(_fd, "w", encoding="utf-8") as f:
                f.write(json.dumps({
                    "domain": domain, "saved_at": time.time(),
                    "cookies": clean,
                }, ensure_ascii=False, indent=1))
            try:
                os.chmod(tmp, 0o600)  # 双保险：属主可读写（防同机其他用户读取）
            except Exception:
                pass  # 特殊文件系统 chmod 失败不阻断保存
            os.replace(tmp, p)
            return True
        except Exception as e:
            # 清理残留临时文件：异常发生在 os.replace 之前时 tmp 会泄漏在存档目录
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            print(f"[WARN] Cookie save failed: {e}", file=sys.stderr)
            return False


def _load_exact(domain: str) -> List[Dict[str, Any]]:
    """读取该域**精确**档案（不做父域回退）。损坏隔离与过期过滤在此层。"""
    p = _cookie_path(domain)
    if not p.exists():
        return []
    try:
        raw = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []          # 并发删除：不是损坏
    except UnicodeDecodeError as e:
        # 审查十一轮（M）：编码损坏（旧版非原子写留下的半截多字节字符/非 UTF-8
        # 手编档）曾穿透此处——UnicodeDecodeError 不是 OSError，隔离逻辑永不触及，
        # 每次运行都重复裸抛。归入"内容损坏 → 隔离"路径
        raw = None
    except OSError as e:
        print(f"[WARN] Cookie 存档暂时读取失败（domain={domain}）："
              f"{type(e).__name__}: {e}；本次按未登录处理，档案未改动，可重试",
              file=sys.stderr, flush=True)
        return []
    try:
        if raw is None:
            raise ValueError("编码损坏（非 UTF-8 字节序列）")
        d = json.loads(raw)
        now = time.time()
        return [c for c in d.get("cookies", [])
                if isinstance(c, dict) and c.get("name") and not _cookie_expired(c, now)]
    except Exception as e:
        try:
            p.rename(p.with_name(p.name + ".corrupt"))
        except Exception:
            pass
        print(f"[WARN] ⚠️ Cookie 存档损坏，已隔离为 {p.name}.corrupt"
              f"（domain={domain}）：{e}；请重新登录/导入该域会话",
              file=sys.stderr, flush=True)
        return []


def _parent_domains(domain: str) -> List[str]:
    """逐级父域（父域档案回退用）：api.example.com → [example.com]；不走到 TLD。"""
    parts = [p for p in _norm_domain(domain).split(".") if p]
    return [".".join(parts[i:]) for i in range(1, len(parts) - 1)]


def load_cookies(domain: str) -> List[Dict[str, Any]]:
    """加载域名 cookie 数组；无则 []。存档**内容**损坏时隔离为 <path>.corrupt
    并告警（与"从没存过"区分：损坏说明曾存过但丢了，需要重新登录/导入）。

    深度改进③：已硬过期的 cookie 不再返回（留 5 分钟时钟偏差宽限）——
    过期令牌播种出去只会被登录墙吃掉，还常被误诊为"被封"。全过期时
    本函数返回 []，has_cookies() 随之为 False，acquire_for_task 会自动尝试
    从调试 Chrome 重新导入新会话。

    收官十轮（审查，实测复现）两处修复：
    ① 只有**内容**解析失败才隔离——原实现把任意异常都当损坏：瞬时系统错误
       （EMFILE/PermissionError）会把内容完好的凭据档案改名隔离，此后
       load/has_cookies 恒空 且 list_saved()（只 glob *.json）看不到 .corrupt，
       等于静默丢失登录态；并发删除也会打出假的"损坏"告警劝用户重新登录。
    ② 精确档案为空时按 RFC 6265 单向过滤回退父域档案：调试 Chrome 登录
       example.com 时站点把会话种在 .example.com → 导入落在 example.com.json，
       而任务入口是 api.example.com 时旧行为恒"未登录"，给出的处方（再登录
       一次 api.example.com）永远无效——站点仍只下发 .example.com。
    """
    domain = _norm_domain(domain)
    with _LOCK:
        out = _load_exact(domain)
        if out:
            return out
        # 审查十一轮（M）：曾"最近优先即返回"——b.example.com.json 只有
        # cf_clearance、example.com.json 有 SESSDATA/token 时，任务域
        # a.b.example.com 只拿到半截登录态。改为逐层向上合并所有祖先（同名
        # cookie 下级先加入者优先），循环结束统一返回
        seen = set()
        for cand in _parent_domains(domain):
            for c in _load_exact(cand):
                # RFC 6265 单向：请求域必须是 cookie 域或其子域，跨域一律丢弃
                if not _cookie_matches_archive(c.get("domain"), domain):
                    continue
                if c.get("name") in seen:
                    continue
                seen.add(c.get("name"))
                out.append(c)
    return out


def session_health(domain: str) -> Dict[str, Any]:
    """会话存档健康体检（深度改进③）：过期条数/有效条数/登录态/存档年龄。

    likely_expired=True = 存档存在但有效 cookie 为 0（全部硬过期）——
    任务启动会大声提示重新导入，死会话绝不静默播种。"""
    domain = _norm_domain(domain)
    out: Dict[str, Any] = {"domain": domain, "total": 0, "valid": 0, "expired": 0,
                           "age_days": None, "has_login": False, "likely_expired": False}
    with _LOCK:  # OCR R131（M）：与其余读写函数统一持锁（防读到写一半的瞬间）
        p = _cookie_path(domain)
        if not p.exists():
            return out
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(d, dict):
                # 存档是合法 JSON 但不是对象（数组/标量）——按损坏口径处理，
                # 否则下方 d.get 在 try 外抛 AttributeError 崩掉调用方
                raise ValueError("存档顶层不是 JSON 对象")
        except Exception:
            out["corrupt"] = True
            return out
    now = time.time()
    # 审查十一轮（M）：cookies 值为 null/非 list 时 was 裸 TypeError——且
    # acquire_for_task 是先 health 后 load，load 的隔离逻辑永远跑不到，档案
    # 永不隔离也永不提示重新登录。结构异常按损坏口径上报
    _raw_cks = d.get("cookies", [])
    if not isinstance(_raw_cks, list):
        out["corrupt"] = True
        return out
    cks = [c for c in _raw_cks if isinstance(c, dict) and c.get("name")]
    out["total"] = len(cks)
    for c in cks:
        if _cookie_expired(c, now):
            out["expired"] += 1
        else:
            out["valid"] += 1
            if c.get("name") in LOGIN_COOKIE_NAMES:
                out["has_login"] = True
    try:
        out["age_days"] = round((now - float(d.get("saved_at", now))) / 86400.0, 1)
    except (TypeError, ValueError):
        pass
    out["likely_expired"] = out["total"] > 0 and out["valid"] == 0
    return out


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


def find_cdp_port(preferred: int = 9222, span: int = 8, timeout: float = 0.4) -> Optional[int]:
    """在 preferred..preferred+span 内探测调试 Chrome 的 CDP 端口
    （MediaCrawler 端口自动搜索思路，2026-09 精读采纳）：9222 被占时
    open-debug-chrome 类脚本自动换端口，固定端口探测会误报"Chrome 未运行"。"""
    import urllib.request as _ur
    # 环形回环探测必须绕开环境代理（审查修复：挂着 Clash 时探测曾被代理劫持）
    _opener = _ur.build_opener(_ur.ProxyHandler({}))
    for port in range(preferred, preferred + span + 1):
        try:
            with _opener.open(f"http://127.0.0.1:{port}/json/version", timeout=timeout) as r:
                if 200 <= r.status < 300:
                    return port
        except Exception:
            continue
    return None


def acquire_for_task(domain: str, port: int = 9222, mode: str = "temp", log=None) -> Dict[str, Any]:
    """任务启动时获取目标域名 cookie：
      - mode=temp（默认，用完即删）：已有存档直接用；没有则自动从调试 Chrome 导入；任务结束应 release
      - mode=persist：长期复用（不自动删除）
      - mode=off：不使用 cookie
    返回 {mode, source: reused|imported|none, count, health?}。

    深度改进③（会话健康检查，审查修复后口径）：
      - 复用存档时若有效 cookie 中"识别不到常见登录态名"→ 大声提示可能未登录
        （部分过期场景：登录 cookie 过期、辅助 cookie 还活着——此前静默复用，
        用户撞登录墙还会误诊为"被封"）
      - 存档全部过期 → 先尝试从调试 Chrome 自动重新导入，失败才告警
        （告警在恢复尝试之后，不再出现"刚喊完未登录就恢复成功"的假警报）
    """
    if mode == "off" or not domain:
        return {"mode": mode, "source": "none", "count": 0}

    def _warn(m: str) -> None:
        if log:
            try:
                log(m)
                return
            except Exception:
                pass
        print(f"[WARN] {m}", file=sys.stderr, flush=True)

    health = session_health(domain)
    _cks = load_cookies(domain)  # OCR R131（M）：一次读档复用——曾 health/has/load 三连读同文件
    if _cks:
        if health.get("total") and not health.get("has_login"):
            _warn(f"⚠️ {domain} 会话存档的 {health['valid']}/{health['total']} 条有效 cookie 中"
                  f"未识别到常见登录态名（识别列表有限，仅供参考）——若目标站需要登录，"
                  f"本次可能未登录。处方: 调试 Chrome 登录后 `cli cookies --from-cdp` 重新导入")
        return {"mode": mode, "source": "reused", "count": len(_cks),
                "health": health}
    # 无有效 cookie（可能全部过期）→ 先尽力从调试 Chrome 重新导入
    # （端口自动搜索：9222 被占时脚本可能换了端口）
    _err = ""
    _imported_domains = 0
    _imported_list: List[str] = []
    try:
        _port = find_cdp_port(port) or port
        r = import_from_cdp_patchright(port=_port, log=log)
        _cks2 = load_cookies(domain)
        _imported_list = [str(x) for x in (r.get("domain_list") or [])]
        if r.get("ok") and _cks2:
            # 审查十一轮（M）：本次导入落盘的全部域随 acquired 返回——temp 模式
            # 结束只删目标域曾把同批导入的其他站登录态永久残留（隐私契约破裂）
            return {"mode": mode, "source": "imported", "count": len(_cks2),
                    "health": session_health(domain),
                    "imported_domains": _imported_list}
        _err = r.get("error", "")
        _imported_domains = int(r.get("domains", 0) or 0)
        if r.get("ok") and not _cks2:
            # 审查修复（P2）：Chrome 活着但没登录过本域——错误信息必须指向
            # 真正的处方（去登录），而不是误导用户去查 Chrome 进程
            _err = (f"调试 Chrome 在线（已导入 {_imported_domains} 个其他域的会话），"
                    f"但其中没有 {domain} 的登录态——请在该 Chrome 登录 {domain} 后重试")
    except Exception as e:
        _err = str(e)
    # 恢复失败才喊：存档全部过期（此时才确定"本次必然未登录"）
    if health.get("likely_expired"):
        _warn(f"⚠️ {domain} 的会话存档已全部过期（{health['total']} 条，"
              f"存档于 {health.get('age_days', '?')} 天前），且未能从调试 Chrome 自动重新导入"
              f"（{_err or '原因未知'}）——本次将以未登录状态请求。"
              f"处方: 打开调试 Chrome 登录后运行 `cli cookies --from-cdp`")
    return {"mode": mode, "source": "none", "count": 0, "error": _err,
            "health": health}


def release_temp(domain: str, acquired: Dict[str, Any]) -> bool:
    """任务结束后删除"本次自动导入"的临时 cookie（用完即删）。
    - source=imported（本次从调试 Chrome 拉取的）→ 删除（不留隐私）
    - source=reused（用户长期存档）→ 保留，不误删用户资产

    审查十一轮（M）：CDP 导入会按域**全量**落盘（调试 Chrome 里登录的所有站），
    而 release 曾只删目标域——其余站登录态永久残留。改为删除本次导入的全部域
    （imported_domains 缺失时回退删目标域，兼容旧调用）。"""
    if not domain or not acquired:
        return False
    if acquired.get("mode") == "temp" and acquired.get("source") == "imported":
        _doms = acquired.get("imported_domains") or [domain]
        _ok = True
        for _d in _doms:
            _ok = delete(str(_d)) and _ok
        return _ok
    return False


def list_saved() -> List[Dict[str, Any]]:
    """列出已保存的会话域名。"""
    out = []
    if not COOKIE_DIR.exists():
        return out
    for p in sorted(COOKIE_DIR.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue  # glob 后被并发删除（如另一进程 delete/cleanup）——不是损坏
        except Exception:
            # 审查修复（P2）：损坏存档曾静默跳过——只剩一个损坏存档时 --list
            # 会谎报"无存档会话"。诊断命令必须说实话。
            out.append({"domain": p.stem, "saved_at": None, "count": 0, "valid": 0,
                        "expired": 0, "has_clearance": False, "has_login": False,
                        "corrupt": True})
            continue
        try:
            cks = d.get("cookies", [])
            now = time.time()
            _valid = [c for c in cks if isinstance(c, dict) and c.get("name")
                      and not _cookie_expired(c, now)]
            out.append({
                "domain": d.get("domain", p.stem),
                "saved_at": d.get("saved_at"),
                "count": len(cks),
                "valid": len(_valid),
                "expired": len(cks) - len(_valid),
                "has_clearance": any(c.get("name") == "cf_clearance" for c in cks),
                # 口径与 session_health 一致：只看"有效"cookie（过期登录态不算数）
                "has_login": any(c.get("name") in LOGIN_COOKIE_NAMES for c in _valid),
            })
        except Exception:
            continue
    return out


def delete(domain: str) -> bool:
    """删除域名会话存档。False 仅表示"存档不存在"；删除动作本身失败（权限等）
    会打印 WARN——审查修复：此前一律静默吞掉，CLI 会把 PermissionError
    误报成"该域无存档"。"""
    domain = _norm_domain(domain)
    with _LOCK:
        p = _cookie_path(domain)
        if not p.exists():
            return False
        try:
            p.unlink()
            return True
        except OSError as e:
            print(f"[WARN] ⚠️ 会话存档删除失败（{p.name}）: {e}；请手动检查权限",
                  file=sys.stderr, flush=True)
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
        # 环形回环预检必须绕开环境代理（审查修复：与 find_cdp_port 同因）
        _opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with _opener.open(f"http://127.0.0.1:{port}/json", timeout=5) as r:
            _json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"ok": False, "error": f"调试 Chrome 未运行（{e}）"}

    return import_from_cdp_patchright(port, _lg)


def import_from_cdp_patchright(port: int = 9222, log=None) -> Dict[str, Any]:
    """用 patchright connectOverCDP 提取调试 Chrome 全部 cookie（跨域全量，最可靠）。"""
    def _lg(m):
        if log:
            log(m)

    import subprocess, os
    _root_js = json.dumps(str(ROOT))  # 审查二轮（H）：%s 裸拼曾可被路径引号破坏 JS 字面量
    script = r'''
const { loadChromium } = require(%s + "/scripts/browser_common.cjs");
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
''' % (_root_js, port)
    try:
        env = {**os.environ, "NODE_PATH": str(ROOT / "node_modules")}
        r = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                           env=env, timeout=60, cwd=str(ROOT))
        out = r.stdout.strip().splitlines()
        data = json.loads(out[-1]) if out else {"ok": False, "error": r.stderr[-300:]}
    except Exception as e:
        return {"ok": False, "error": f"CDP 导入失败：{e}"}
    # R128 修复（HIGH）：非零退出不可信——Node 崩溃/OOM/被信号杀之前可能已写出
    # {ok:true}（cookie 只抓到一半），仅凭 JSON ok 字段会把半截会话当成功存档
    if r.returncode != 0 and data.get("ok"):
        return {"ok": False,
                "error": f"导入进程异常退出（rc={r.returncode}）：{r.stderr[-200:]}"}
    if not data.get("ok"):
        err = data.get("error", "未知")
        if r.returncode != 0:
            err = f"{err}（退出码 {r.returncode}）"
        return {"ok": False, "error": err}
    cookies = data.get("cookies") or []
    # 按域名分组保存（去 www.）
    # OCR R131（M）：域归一与 save_cookies/load_cookies 同口径——不去 www. 时
    # www.example.com 与 example.com 分裂成两份存档，加载侧永远只看到一份
    by_domain: Dict[str, List] = {}
    for c in cookies:
        d = _norm_domain((c.get("domain") or "").lower().lstrip("."))
        if not d or not c.get("value"):
            continue
        by_domain.setdefault(d, []).append(c)
    saved = 0
    for d, cks in by_domain.items():
        if save_cookies(d, cks):
            saved += 1
    if cookies and saved == 0:
        # 审查修复（P3）：全部保存失败（磁盘满/目录不可写）曾只显示"覆盖 0 个域名"，
        # 成功语气的 ✅ 会让人以为登录态已到手
        _lg(f"⚠️ 提取到 {len(cookies)} 条 cookie 但一个域都没存上——请检查 "
            f"{COOKIE_DIR} 是否可写")
    _lg(f"✅ 从调试 Chrome 导入 {len(cookies)} 条 cookie，覆盖 {saved} 个域名")
    return {"ok": True, "imported": len(cookies), "domains": saved,
            "domain_list": sorted(by_domain.keys())}
