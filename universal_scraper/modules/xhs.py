#!/usr/bin/env python3
"""小红书一等公民采集器（实战反馈五#1 收编，源自 xhs_damo_task 实战验证实现）。

打法（合规红线决定，见 SKILL.md）：真实登录态浏览器（patchright 持久上下文）+
页面自身签名（不逆向 x-s）+ 验证码人工在环 + 守护进程在场捕获。本模块只做编排、
捕获解析与组装；浏览器驱动在 scripts/capture_daemon.cjs / xhs_collect_note.cjs，
守护进程的启停由 cli 层负责（xhs.py 不做任何子进程操作）。

组件对应（实战产物 → 入库）:
  xhs_launch.cjs   → scripts/capture_daemon.cjs（通用化：hosts/proxy/profile 参数化）
  collect_note.cjs → scripts/xhs_collect_note.cjs（唯一差异：CAPTURE_PATH 环境变量）
  extract_state.py → universal_scraper/ssr_state.py（通用化：任意变量名）

用法:
  python3 -m universal_scraper.cli xhs --keyword "#大模型" --top 20 --comments 50
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List

from ..ssr_state import extract_state, project

ROOT = Path(__file__).resolve().parent.parent.parent  # modules/ → universal_scraper/ → 技能根
PROFILE_DIR = Path.home() / ".universal-scraper" / "xhs_profile"
XHS_HOST = "xiaohongshu.com"


def _runtime():
    from ..runtime import resolve_node, resolve_node_path
    return resolve_node(), resolve_node_path()


# ── 守护进程状态（启停在 cli 层；本模块只构造命令与读状态）──────────────────

def _daemon_paths(capture: Path) -> Dict[str, Path]:
    return {"status": Path(str(capture) + ".status.json"), "pid": Path(str(capture) + ".pid")}


def daemon_cmd(capture: Path, proxy: str = "", headless: bool = False) -> List[str]:
    """构造守护进程启动命令（参数列表形式，无 shell 参与——cli 层直接 Popen）。"""
    node, node_path = _runtime()
    env_hint = node_path
    cmd = [
        node, str(ROOT / "scripts" / "capture_daemon.cjs"),
        "--hosts", XHS_HOST,
        "--capture", str(capture),
        "--profile", str(PROFILE_DIR),
        "--cdp", "9222",
        "--node-path", env_hint,
    ]
    if proxy:
        cmd.extend(["--proxy", proxy])
    if headless:
        cmd.append("--headless")
    return cmd


def _pid_alive_is_daemon(pid: int) -> bool:
    """审查六轮（M6）：pid 存活 ≠ 守护进程在跑——系统可能把 pid 复用给无关
    进程（此时 stop 会误杀）。核验 cmdline 含 capture_daemon.cjs 才认活。"""
    try:
        out = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                             capture_output=True, text=True, timeout=5).stdout
        return "capture_daemon.cjs" in (out or "")
    except Exception:
        return False


def daemon_status(capture: Path) -> Dict[str, Any]:
    paths = _daemon_paths(capture)
    if not paths["status"].exists():
        return {"running": False}
    try:
        d = json.loads(paths["status"].read_text(encoding="utf-8"))
    except Exception:
        return {"running": False}
    pid = d.get("pid")
    alive = False
    if pid:
        try:
            os.kill(int(pid), 0)
            alive = _pid_alive_is_daemon(int(pid))  # 审查六轮（M6）：pid 复用防误判
        except (OSError, ValueError):
            pass
    return {**d, "running": alive}


def daemon_stop(capture: Path) -> Dict[str, Any]:
    st = daemon_status(capture)
    pid = st.get("pid")
    if not st.get("running") or not pid:
        return {"stopped": False, "note": "守护进程未在运行"}
    try:
        os.kill(int(pid), signal.SIGTERM)
    except (OSError, ValueError) as e:
        return {"stopped": False, "error": str(e)}
    for _ in range(40):
        time.sleep(0.25)
        if not daemon_status(capture).get("running"):
            return {"stopped": True}
    # 审查六轮（Agent）：TERM 超时曾止步于"请手动处理"——升级 SIGKILL 兜底
    try:
        os.kill(int(pid), signal.SIGKILL)
        time.sleep(0.5)
        return {"stopped": True, "note": "TERM 超时，已升级 SIGKILL"}
    except (OSError, ValueError) as e:
        return {"stopped": False, "error": f"TERM+KILL 均失败: {e}"}


# ── 捕获解析 ────────────────────────────────────────────────────────────────

def _iter_capture(capture: Path, since_offset: int = 0):
    """增量读捕获 JSONL（yield (文件尾偏移, record)）。半行（写一半）跳过。"""
    if not capture.exists():
        return
    with open(capture, "rb") as f:
        f.seek(since_offset)
        # 审查六轮（Agent H）：`for line in f` 走 read-ahead 缓冲，f.tell() 返回
        # 缓冲位置而非行尾——增量 offset 会重复/漏解析。readline() 模式 tell() 精确
        while True:
            line = f.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                yield f.tell(), json.loads(line)
            except Exception:
                continue


def _find_cards(obj: Any, out: list, depth: int = 0) -> None:
    """递归找搜索卡片形状 {id, xsec_token, ...}（接口路径随版本变，按形状找）。"""
    if depth > 8:
        return
    if isinstance(obj, dict):
        if obj.get("id") and obj.get("xsec_token") and (obj.get("note_card") or obj.get("title")):
            out.append(obj)
        for v in obj.values():
            _find_cards(v, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            _find_cards(v, out, depth + 1)


def parse_search_cards(capture: Path, since_offset: int = 0) -> List[Dict[str, Any]]:
    """从捕获里解析搜索结果卡片：id/xsec_token/标题/点赞（去重，点赞降序）。"""
    cards: Dict[str, Dict[str, Any]] = {}
    for _, rec in _iter_capture(capture, since_offset):
        if "search" not in str(rec.get("url", "")):
            continue
        body = rec.get("body")
        if not isinstance(body, str) or not body:
            continue
        try:
            data = json.loads(body)
        except Exception:
            continue
        found: list = []
        _find_cards(data, found)
        for c in found:
            nid = str(c.get("id"))
            if nid in cards:
                continue
            nc = c.get("note_card") or {}
            like = ((nc.get("interact_info") or {}).get("liked_count")) or 0
            # 审查六轮（H1）："1.2万".replace("万","0000") → int() ValueError → 0
            # （爆款笔记排序沉底，--top 取到错误集合）。小数形态按 float 换算
            try:
                like_s = str(like)
                like_n = int(float(like_s[:-1]) * 10000) if like_s.endswith("万") else int(like_s)
            except (ValueError, TypeError):
                like_n = 0
            cards[nid] = {"note_id": nid,
                          "xsec_token": c.get("xsec_token", ""),
                          "title": (nc.get("display_title") or c.get("title") or "")[:80],
                          "liked_count_raw": str(like),
                          "liked_count": like_n,
                          "type": nc.get("type", "")}
    return sorted(cards.values(), key=lambda x: x["liked_count"], reverse=True)


def parse_note_detail(capture: Path, note_id: str) -> Dict[str, Any]:
    """从捕获的 SSR HTML / 详情接口解析单篇笔记字段。"""
    detail: Dict[str, Any] = {}
    for _, rec in _iter_capture(capture):
        # 审查八轮（LOW）：曾用子串匹配（`note_id in url`）——短 id 会命中"含该
        # 前缀的其它笔记 URL"（如 id=123 命中 .../explore/12345），把别的笔记正文
        # 当本篇解析。改为字母数字边界内的精确出现（兼容各种 URL 形态，仍拒绝前缀撞车）。
        import re as _re
        _u = str(rec.get("url", ""))
        if _re.search(rf"(?<![0-9A-Za-z]){_re.escape(str(note_id))}(?![0-9A-Za-z])", _u) is None:
            continue
        body = rec.get("body")
        if not isinstance(body, str):
            continue
        if "<html" in body[:200].lower() or "window.__INITIAL_STATE__" in body:
            st = extract_state(body)
            nd = project(st, f"note.noteDetailMap.{note_id}.note") if st else None
            if isinstance(nd, dict):
                detail = nd
                break
        elif body.startswith("{"):
            try:
                data = json.loads(body).get("data") or {}
                if note_id in str(data):
                    detail = (data.get("items") or [{}])[0].get("note_card") or detail
            except Exception:
                pass
    return detail


def parse_comments(capture: Path, note_id: str) -> List[Dict[str, Any]]:
    """解析捕获的 comment/page（顶层）与 comment/sub/page（回复），展平去重。"""
    comments: Dict[str, Dict[str, Any]] = {}
    for _, rec in _iter_capture(capture):
        url = str(rec.get("url", ""))
        if f"comment/page?note_id={note_id}" not in url and f"comment/sub/page?note_id={note_id}" not in url:
            continue
        body = rec.get("body")
        if not isinstance(body, str) or not body.startswith("{"):
            continue
        try:
            data = json.loads(body).get("data") or {}
        except Exception:
            continue
        is_sub = "sub/page" in url
        for c in data.get("comments") or []:
            cid = str(c.get("id"))
            if cid in comments:
                continue
            pics = c.get("pictures") or []
            comments[cid] = {
                "评论ID": cid,
                "评论内容": c.get("content", ""),
                "评论用户昵称": (c.get("user_info") or {}).get("nickname", ""),
                "评论用户ID": (c.get("user_info") or {}).get("user_id", ""),
                "评论时间": _ts(c.get("create_time")),
                "点赞数": c.get("like_count", 0),
                "回复数": c.get("sub_comment_count", 0),
                "评论图片": pics[0].get("url_default", "") if pics else "",
                "层级": "回复" if is_sub else "顶层",
            }
    return list(comments.values())


def parse_user_profile(capture: Path, user_id: str) -> Dict[str, Any]:
    """从捕获的用户主页 SSR 解析公开资料。"""
    for _, rec in _iter_capture(capture):
        if user_id not in str(rec.get("url", "")):
            continue
        body = rec.get("body")
        if not isinstance(body, str):
            continue
        st = extract_state(body)
        up = project(st, "user.userPageData") if st else None
        if isinstance(up, dict) and up:
            tags = up.get("tagMap") or {}
            return {
                "昵称": up.get("nickname", ""),
                "小红书号": up.get("redId", ""),
                "粉丝数": tags.get("fanss") or up.get("fans", ""),
                "关注数": tags.get("follows") or up.get("follows", ""),
                "获赞与收藏": tags.get("interaction") or "",
                "认证类型": (up.get("official") or {}).get("title", "") or "无",
                "简介": up.get("desc", ""),
                "头像链接": up.get("imageb", "") or up.get("images", ""),
                "作品数": tags.get("notes") or "",
            }
    return {}


def _ts(ms: Any) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(ms) / 1000))
    except (TypeError, ValueError, OSError):
        return ""


# ── 出口预检（实战反馈五#3 落地：请用户扫码前先确认出口未被封）───────────────

def check_egress(proxy: str = "") -> Dict[str, Any]:
    """登录/采集前的出口预检：真实出口 IP + 运营商。
    审查六轮（M5）：proxy 非空时经代理探测——采集走代理，预检也必须看代理出口。"""
    out: Dict[str, Any] = {"proxy": proxy or None}
    try:
        from ..net import detect_ip
        d = detect_ip(proxy=proxy)
        out["egress_ip"] = d.get("ip")
        out["egress_note"] = (d.get("city") or "") + " " + (d.get("isp") or "")
        if proxy:
            out["egress_note"] += "（经代理出口）"
    except Exception as e:
        out["egress_note"] = f"出口检测失败: {type(e).__name__}: {e}"
    return out
