#!/usr/bin/env python3
"""B站精配（R101 沉淀：影视飓风 UP主战役 2026-09 的四通道战法代码化）。

四条通道（全部纯 HTTP，浏览器只在拿行为指纹时值一次）：
  ① 视频元数据  x/web-interface/view?bvid=            无需 wbi
  ② 弹幕        api.bilibili.com/x/v1/dm/list.so?oid=  无需 wbi（deflate XML）
  ③ 评论        x/v2/reply?type=1&oid=                无需 wbi（匿名可见约前 3 页，
                 之后 is_end 且 3 条登录墙——留 L4 正路：登录一次 cookie 复用）
  ④ UP主视频列表 x/space/wbi/arc/search               需 wbi 签名 + 真实 dm_img_*
                 行为指纹（指纹串参与风控评分，随机伪造必挂 -352→-412；
                 获取：浏览器 capture 一次目标页，取 capture_all.json 里真实
                 dm_img_str/dm_cover_img_str/dm_img_inter 原值重放）

已知风控口径（anti-block-playbook B站条目）：
  - 先 GET 一次 www.bilibili.com 预热拿 buvid cookie 并固定 UA（漂移再触发 -352）
  - -412 退避 40s 自愈；连续 -352 = 行为指纹被拒，回 capture 重取 dm_img_*
  - 全程单并发 + 1.1s 起步间隔（本模块经 make_http_client 强制）

用法（CLI `us bili` 封装了常用组合）：
    from universal_scraper import bili
    meta = bili.video_meta("BV1abc")          # 含 cid
    rows = bili.danmaku(meta["cid"], limit=5000)
    cs   = bili.comments(meta["aid"], limit=500)
"""
from __future__ import annotations

import json
import re
import time
import zlib
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode  # OCR R131（L）：循环体内重复导入已清

# 仅允许 B站 API 域（出站白名单，防配置注入改打别的目标）
_ALLOWED_HOSTS = ("api.bilibili.com", "www.bilibili.com")

# wbi 混淆表（B站公开算法，逆向自 NavigationBar 加载器；社区稳定口径）
_WBI_MIXIN = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27,
              43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48,
              7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54,
              21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52]

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

# B站年份判定固定按东八区——本机时区（UTC 容器/云主机常见）会把年界偏移 8 小时
_CST = timezone(timedelta(hours=8))


def _check_url(url: str) -> str:
    """出站守卫：仅 B站 API 域 + http/https（Mimosa 口径）。"""
    from urllib.parse import urlsplit
    sp = urlsplit(url or "")
    if (sp.scheme or "").lower() not in ("http", "https") or not sp.hostname:
        raise ValueError(f"非法 URL: {str(url)[:60]!r}")
    if sp.hostname not in _ALLOWED_HOSTS:
        raise ValueError(f"非 B站 API 域，已拒绝: {sp.hostname}")
    return url


def _client():
    """B站专用 HTTP 客户端：buvid 预热 + 固定 UA + Referer（-352 三件套）。
    R111 修复：curl_cffi 后端每次请求新开会话、Set-Cookie 不自动续——预热后
    手动解析 Set-Cookie 里的 buvid3/buvid4 塞回 client.cookies 才真正生效。"""
    from .core import make_http_client
    client = make_http_client({"min_interval": 1.1, "timeout": 20, "max_retries": 1,
                               "rotate_ua": False, "use_system_proxy": False,
                               "headers": {"User-Agent": _UA}})
    _check_url("https://www.bilibili.com/")
    res = client.get("https://www.bilibili.com/")  # buvid 预热
    sc = str((res.get("headers") or {}).get("set-cookie", ""))
    for name in ("buvid3", "buvid4"):
        m = re.search(name + r"=([^;,]+)", sc)  # OCR R131（L）：局部 re 遮蔽已清
        if m:
            client.cookies[name] = m.group(1)
    return client


def _get_json(client, url: str) -> Dict[str, Any]:
    """GET 并解析 B站标准 JSON 信封；风控/挑战给出行动处方而非裸码。"""
    _check_url(url)
    res = client.get(url, headers={"Referer": "https://www.bilibili.com/"})
    if not res.get("ok"):
        _st = res.get("status", 0)
        _head = (res.get("text") or "").lstrip()[:40]
        # R113 补全：挑战/限流页（HTML 或 412/429 状态）给行动处方而非原文倾倒
        if _st in (412, 429) or _head.startswith("<"):
            raise RuntimeError(f"B站返回挑战/限流页（HTTP {_st}）。"
                               "若是 UP主列表（arc/search）：需真实 dm_img_* 指纹——"
                               "浏览器 capture 一次目标页取原值重放（recipes R34）；"
                               "-412 类退避 40s 可自愈")
        raise RuntimeError(f"B站请求失败: HTTP {_st} {res.get('text', '')[:120]}")
    data = res.get("json")
    if data is None:
        head = (res.get("text") or "")[:60].lstrip()
        if head.startswith("<"):
            # 风控挑战页（HTML）曾原文倾倒——arc/search 需真实 dm_img 指纹
            raise RuntimeError("B站返回 HTML 挑战页而非 JSON（风控/登录墙拦截）。"
                               "若是 UP主列表（arc/search）：需真实 dm_img_* 指纹——"
                               "浏览器 capture 一次目标页取原值重放（recipes R34）；"
                               "其余接口稍后重试或换通道")
        raise RuntimeError(f"B站返回非 JSON（疑似风控页）: {res.get('text', '')[:120]}")
    code = data.get("code")
    if code == -352:
        if "arc/search" in url:
            raise RuntimeError("-352 风控拒绝（arc/search）：行为指纹被判伪造——"
                               "按 playbook 用浏览器 capture 一次 UP主空间页，"
                               "取真实 dm_img_* 原值重放")
        raise RuntimeError("-352 风控拒绝：退避后重试；连续出现按 playbook 判型")
    if code == -412:
        raise RuntimeError("-412 限流：退避 40s 后自愈；请加大 min_interval 后重试")
    if code not in (0, None):
        raise RuntimeError(f"B站 API code={code}: {data.get('message', '')}")
    return data.get("data") or {}


# ---------------------------------------------------------------- wbi 签名

def get_wbi_keys(client=None) -> Dict[str, str]:
    """从 nav 接口取当日 wbi 密钥（img_key/sub_key，取文件名去扩展名）。"""
    c = client or _client()
    _check_url("https://api.bilibili.com/x/web-interface/nav")
    res = c.get("https://api.bilibili.com/x/web-interface/nav",
                headers={"Referer": "https://www.bilibili.com/"})
    if not res.get("ok"):
        raise RuntimeError(f"nav 请求失败: {res.get('text', '')[:120]}")
    data = (res.get("json") or {}).get("data") or {}
    wbi = (data.get("wbi_img") or {})
    img = (wbi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
    sub = (wbi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
    if not img or not sub:
        raise RuntimeError("nav 未返回 wbi_img（接口口径变化？）")
    return {"img_key": img, "sub_key": sub}


def wbi_sign(params: Dict[str, Any], img_key: str, sub_key: str) -> Dict[str, Any]:
    """wbi 参数签名：注入 wts → 混淆表取前 32 位密钥 → md5 得 w_rid。"""
    import hashlib
    # 标准算法：按混淆表对拼接串（img+sub，64 字符）取前 32 位
    combined = img_key + sub_key
    mixin = "".join(combined[i] for i in _WBI_MIXIN[:32])
    signed = dict(params)
    signed["wts"] = int(time.time())
    query = urlencode(sorted(
        ((k, re.sub(r"[!'()*]", "", str(v))) for k, v in signed.items()),
        key=lambda kv: kv[0]))
    # 注意：w_rid 必须是 MD5——这是 B站服务端的协议校验算法（非安全用途），
    # 改成 sha256 会导致全部签名被拒（Mimosa 提示按协议兼容性豁免）
    signed["w_rid"] = hashlib.md5((query + mixin).encode()).hexdigest()
    return signed


# ---------------------------------------------------------------- ① 元数据

def video_meta(client, bvid: str) -> Dict[str, Any]:
    """视频元数据（view 接口，无需 wbi）。返回统一字段 + cid/aid 供下游通道。
    用法: client = bili._client(); meta = bili.video_meta(client, "BV1abc")"""
    _check_url(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}")
    d = _get_json(client, f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}")
    stat = d.get("stat") or {}
    _pd = d.get("pubdate", 0)
    return {
        "bvid": d.get("bvid", bvid),
        "aid": d.get("aid", ""),
        "标题": d.get("title", ""),
        # 审查二轮（M）：pubdate 缺失曾渲染成 1970 假日期（同 videos 列表口径）
        "发布时间": (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(_pd))
                     if _pd else ""),
        "时长秒": d.get("duration", 0),
        "播放量": stat.get("view", 0), "弹幕总数": stat.get("danmaku", 0),
        "评论总数": stat.get("reply", 0), "点赞数": stat.get("like", 0),
        "投币数": stat.get("coin", 0), "收藏数": stat.get("favorite", 0),
        "分享数": stat.get("share", 0),
        "分区": d.get("tname", ""),
        # 标签：view API 不返回 tag（实际在 x/tag/archive/tags）——字段保留为空
        "标签": d.get("tag", ""),
        "简介": d.get("desc", ""), "封面": d.get("pic", ""),
        "cid": d.get("cid", ""), "_parser": "bili_meta",
    }


# ---------------------------------------------------------------- ② 弹幕

def danmaku(client, cid: Any, limit: int = 5000) -> List[Dict[str, Any]]:
    """全部弹幕（list.so，deflate XML，无需 wbi）。

    弹幕字段: 视频内时间点秒 / 类型(1滚动 4底部 5顶部)/颜色/真实发送时间/UID(部分脱敏)。"""
    _check_url(f"https://api.bilibili.com/x/v1/dm/list.so?oid={cid}")
    res = client.get(f"https://api.bilibili.com/x/v1/dm/list.so?oid={cid}",
                     headers={"Referer": "https://www.bilibili.com/"})
    if not res.get("ok"):
        raise RuntimeError(f"弹幕请求失败: {res.get('text', '')[:120]}")
    raw = res.get("body") or b""
    # list.so 返回 deflate 压缩的 XML：0x78 zlib 头则解压，否则按已解压文本处理
    if raw[:1] == b"\x78":
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            try:
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
            except zlib.error:
                pass
    text = raw.decode("utf-8", errors="replace")
    from html import unescape as _unescape
    rows: List[Dict[str, Any]] = []
    # 上限 20000：高级/代码弹幕（BAS JSON）远超旧的 2000——超限行会被整条漏掉
    for m in re.finditer(r'<d p="([^"]+)">(.{0,20000}?)</d>', text, re.S):
        parts = (m.group(1) + ",,,,,,,").split(",")[:8]
        try:
            rows.append({
                "视频内时间秒": round(float(parts[0] or 0), 2),
                "类型": {"1": "滚动", "4": "底部", "5": "顶部", "6": "逆向滚动",
                         "7": "高级", "8": "代码", "9": "BAS"}.get(parts[1], "滚动"),
                "弹幕颜色": f"#{int(parts[3] or 16777215):06x}",
                "用户ID": parts[6],          # 服务端已部分脱敏（哈希）
                "真实发送时间": time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(int(parts[4] or 0))),
                "内容": _unescape(m.group(2)),  # R121：&amp;/&lt; 等实体还原
            })
        except (ValueError, IndexError):
            continue
        if len(rows) >= limit:
            break
    return rows


# ---------------------------------------------------------------- ③ 评论

def comments(client, aid: Any, limit: int = 500, with_replies: bool = True,
             wbi: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    """评论 + 楼中楼（wbi/main 接口，需 wbi 签名——R101 实测匿名裸 v2/reply
    已返回空列表）。

    已知登录墙：匿名翻几页后 is_end 且条目骤减——这不是翻页深度问题，
    正路是登录一次复用 cookie（playbook L4），如实停在这里不硬刚。
    楼中楼深度：只读内联首页（≤3 条）——更深层需 x/v2/reply/reply 分页，
    `回复数` 字段可核对缺口（R121 记录的已知边界）。"""
    # R113 修复（P3）：wbi 密钥当日有效——调用方可缓存传入，省一次 nav 往返
    wbi = wbi or get_wbi_keys(client)
    out: List[Dict[str, Any]] = []
    _wbi_retried = False

    def _row(c: Dict[str, Any], parent: str = "") -> Dict[str, Any]:
        # OCR R131（M）：曾定义在翻页循环内——每页重建函数对象；提升到循环外
        member = c.get("member") or {}
        ctl = c.get("reply_control") or {}
        row = {
            "评论ID": c.get("rpid", ""),
            "用户昵称": member.get("uname", ""),
            "用户ID": member.get("mid", ""),
            "用户等级": (member.get("level_info") or {}).get("current_level", ""),
            "头像": member.get("avatar", ""),
            "内容": (c.get("content") or {}).get("message", ""),
            "评论时间": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(c.get("ctime", 0))),
            "点赞数": c.get("like", 0), "回复数": c.get("rcount", 0),
            "含图片": bool((c.get("content") or {}).get("pictures")),
            "IP属地": (ctl.get("location") or "").replace("IP属地：", ""),
        }
        if parent:
            row["父评论ID"] = parent
        return row
    offset = ""
    for _page in range(1, 60):  # 绝对保险丝
        params = {"oid": aid, "type": 1, "mode": 3, "plat": 1,
                  "web_location": 1315875,
                  # R101 实测：必须无空格紧凑 JSON——带空格 forms 被 B站 -403 拒绝
                  "pagination_str": json.dumps({"offset": offset},
                                               separators=(",", ":"))}
        signed = wbi_sign(params, wbi["img_key"], wbi["sub_key"])
        _check_url("https://api.bilibili.com/x/v2/reply/wbi/main")
        try:
            d = _get_json(client, "https://api.bilibili.com/x/v2/reply/wbi/main?"
                          + urlencode(signed))
        except RuntimeError as e:
            # R121 修复（P2）：wbi 密钥每日轮换——跨午夜的长任务首次失败时刷新重试
            if ("风控" in str(e) or "-403" in str(e) or "-352" in str(e)) and not _wbi_retried:
                _wbi_retried = True
                # OCR R116 审查：先取新密钥再清旧——get_wbi_keys 网络失败时
                # 不至于把调用方的 wbi dict 清成永久空壳
                _new_keys = get_wbi_keys(client)
                wbi.clear()
                wbi.update(_new_keys)  # 原地刷新——调用方持有同一 dict
                signed = wbi_sign(params, wbi["img_key"], wbi["sub_key"])
                d = _get_json(client, "https://api.bilibili.com/x/v2/reply/wbi/main?"
                              + urlencode(signed))
            else:
                raise
        page_replies = d.get("replies") or []
        if not page_replies:
            break  # 匿名登录墙/无更多评论：如实停止

        for c in page_replies:
            out.append(_row(c))
            if with_replies:  # 楼中楼首页内联在 replies 字段
                for rc_ in (c.get("replies") or []):
                    out.append(_row(rc_, parent=str(c.get("rpid", ""))))
            if len(out) >= limit:
                return out[:limit]
        cursor = d.get("cursor") or {}
        if cursor.get("is_end"):
            break
        offset = ((cursor.get("pagination_reply") or {}).get("next_offset") or "")
        if not offset:
            break
        # OCR R131（H）：comments 翻页曾零间隔连打（user_videos 是 1.1s/页）——
        # 60 页翻页直接触发 B 站风控。对齐同模块 1.1s 起步间隔
        time.sleep(1.1)
    return out[:limit]


# ---------------------------------------------------------------- ④ UP主视频列表

def user_videos(client, mid: Any, wbi: Dict[str, str],
                dm_img_str: str = "", dm_cover_img_str: str = "",
                dm_img_inter: str = "", max_pages: int = 10,
                year: Optional[int] = None) -> List[Dict[str, Any]]:
    """UP主投稿列表（arc/search，需 wbi + 真实 dm_img_* 行为指纹）。

    dm_img_* 获取：浏览器 capture 一次 UP主空间页，从 capture_all.json 取真实
    原值（指纹串非会话绑定可复用；随机伪造必挂 -352——playbook B站条目）。
    year 传年份时按 pubdate 过滤（服务端无该参数，客户端过滤）。"""
    base = {"mid": mid, "ps": 30, "pn": 1, "order": "pubdate",
            "dm_img_list": "[]",
            "dm_img_str": dm_img_str or "base64",
            "dm_cover_img_str": dm_cover_img_str or "base64",
            "dm_img_inter": dm_img_inter or '{"ds":[],"wh":[0,0,0],"of":[0,0,0]}'}
    out: List[Dict[str, Any]] = []
    _seen_bvid: set = set()
    boundary_crossed = False   # 已见到早于目标年份的视频（下界越过分明，结果完整）
    reached_end = False        # 短页/空页（自然耗尽）
    total_known: Optional[int] = None  # arc/search 响应携带的投稿总数
    for pn in range(1, max_pages + 1):
        base["pn"] = pn
        signed = wbi_sign(base, wbi["img_key"], wbi["sub_key"])
        _check_url("https://api.bilibili.com/x/space/wbi/arc/search")
        d = _get_json(client, "https://api.bilibili.com/x/space/wbi/arc/search?"
                      + urlencode(signed))
        if isinstance(d.get("total"), int):
            total_known = d["total"]
        arcs = ((d.get("list") or {}).get("vlist")) or []
        if not arcs:
            reached_end = True
            break
        stop = False
        for a in arcs:
            bv = a.get("bvid", "")
            if not bv or bv in _seen_bvid:
                continue  # R113 修复（P3）：跨页重复 bvid 去重
            _seen_bvid.add(bv)
            pub = a.get("created", 0)
            if year and not pub:
                continue  # 过滤模式下无日期行无法判定年份——跳过（R113 P3）
            # 年份固定按 +08:00 判定（B站面向中国大陆用户；用本机时区在 UTC 主机上
            # 会把年界偏移 8 小时，跨界视频被误删/误留）
            if year and datetime.fromtimestamp(pub, _CST).year != year:
                # 列表按 pubdate 倒序——越过目标年份上界即停（更早的还在后面）
                if pub < datetime(year, 1, 1, tzinfo=_CST).timestamp():
                    boundary_crossed = True
                    stop = True
                    break
                continue
            out.append({"bvid": bv, "标题": a.get("title", ""),
                        # OCR R131（M）：created 缺失曾渲染成 1970-01-01 假日期
                        "发布时间": (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(pub))
                                     if pub else ""),
                        "时长秒": a.get("length", ""), "播放量": a.get("play", 0),
                        "弹幕总数": a.get("video_review", 0), "aid": a.get("aid", "")})
        # R122/R123：total 耗尽判定在处理之后——len(out) >= total = 自然耗尽
        if (total_known is not None and len(out) >= total_known):
            reached_end = True
            break
        if stop:
            break
        time.sleep(1.1)
    # R121 修复（P1）：year 模式既未越界也未自然耗尽 = 页数耗尽截断——
    # 曾静默返回不完整结果报 exit 0（"年度全量"承诺被打破）。
    # R122 修复（P3）：报错区分"cap 截断"与"扫描不足"，并给出可达的目标值
    if year and not (reached_end or boundary_crossed):
        _next_cap = (max_pages + 2) * 30
        raise RuntimeError(
            f"UP主 {mid} 的投稿在 {max_pages} 页内未扫描完"
            + (f"（接口总数 {total_known}）" if total_known is not None else "")
            + f"——{year} 年结果可能不完整。请增大 --max-videos 至 ≥{_next_cap} 后重跑")
    return out
