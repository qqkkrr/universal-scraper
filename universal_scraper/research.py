#!/usr/bin/env python3
"""科研批量采集引擎（五任务实战动线的引擎化，2026-09-26）。

把 2026-09-24/25 巨潮年报面板（15,638 项）验证过的完整动线固化为可复用命令：
清单×年份窗 → 逐项检索（标题年度强校验）→ PDF 即采即弃 → 文本档案(.txt.gz)
→ 关键词面板 xlsx + 断点续跑 + 防封禁自熔断。

核心纪律（全部来自实战踩坑，勿"优化"掉）：
- 报告年度 = 标题『XXXX年年度报告』的会计年度；检索窗口用 Y+1~Y+2（发布年）
- stock 参数缺 orgId 时部分站点静默返回 0 条——topSearch 兜底必须保留
- 单一提取管线：全部数值出自同一 PDF 提取器（默认 pymupdf），禁止跨管线混算
- PDF 不落盘：下载→提取→核验→.txt.gz→即删（磁盘恒定；文本即充分统计量）
- 连续 N 次 API 异常即落盘退出（防把封禁窗口烧穿）
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import random
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

DEFAULT_KEYWORDS = {
    "compliance": ["合规", "监管", "审查", "审批", "备案", "负面清单", "资质", "许可",
                   "准入", "安全审查", "数据出境", "跨境资金"],
    "overseas": ["海外", "境外", "国际", "子公司", "对外投资", "OFDI", "跨境交付", "服务出口"],
    "shrink": ["清算", "注销", "处置", "转让", "剥离", "退出", "减持"],
}
_EXCLUDE_TITLE = ["摘要", "英文版", "取消", "提示性", "问询", "已撤"]
_TITLE_YEAR = re.compile(r"(\d{4})\s*年年度报告")
_SENT = re.compile(r"[。！？!?;\n；]")
_ILLEGAL_XLSX = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ufffe\uffff]")


def urlsplit_host(url: str) -> str:
    from urllib.parse import urlsplit
    return (urlsplit(url).hostname or "").lower()


def _is_traditional(text: str) -> bool:
    """简繁判别（前 2 万字符）：常用简繁对照字频比较——繁体显著占优即判繁体。
    T3 实测：A+H 公司的 H 股版年报是繁体（"股份代號/運輸/網絡"），简体词典
    在其中计数全零（南航 2016 简体合规词 0 次即此因）。"""
    head = text[:20000]
    fan = sum(head.count(c) for c in "國關於這時總運網發後來區員會務東車語")
    jian = sum(head.count(c) for c in "国关于这时总运网发后来区员会务东车语")
    return fan > jian * 2 and fan > 10


def _to_int(v) -> int:
    """清单 CSV 常见 '2010.0'（pandas 浮点导出）——直接 int() 会 ValueError。"""
    try:
        return int(float(v))
    except (TypeError, ValueError) as e:
        raise SystemExit(f"✗ 年份字段非法 {v!r}: {e}") from e


def log(msg: str) -> None:
    print(f"[{time.strftime('%m-%d %H:%M:%S')}] {msg}", flush=True)


def load_keywords(path: Optional[str]) -> Dict[str, List[str]]:
    """词典文件是任务包一等资源：JSON {"组名": [词,...]}；缺省用内置三组。

    文件缺失/语法损坏/结构不识别/全组无效 → 报错退出（词典错则整批口径错，
    静默回落默认词典会造成"以为在用自备词典实际没有"的科研事故）。"""
    if not path:
        return dict(DEFAULT_KEYWORDS)
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except Exception as e:
        raise SystemExit(f"✗ 词典文件读取失败: {type(e).__name__}: {e}")
    if not isinstance(d, dict):
        raise SystemExit(f"✗ 词典 JSON 顶层须为 {{组:[词]}} 映射，实为 {type(d).__name__}")
    kw = {str(k): [str(w) for w in v] for k, v in d.items() if isinstance(v, (list, tuple)) and v}
    if not kw:
        raise SystemExit("✗ 词典无任何有效组（每组须为非空词表）")
    return kw


def load_universe(path: str, year_from: int, year_to: int) -> List[Tuple[str, int]]:
    """清单 CSV：stkcd[,first_year,last_year] × 年份窗（含清单窗口裁剪——
    全量批实战教训：只过滤 last_year 漏掉 first_year，越界行混进面板）。"""
    try:
        rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
    except Exception as e:
        raise SystemExit(f"✗ 清单 CSV 读取失败: {type(e).__name__}: {e}")
    if rows and "stkcd" not in rows[0]:
        raise SystemExit(f"✗ 清单缺 stkcd 列: 实有 {list(rows[0])[:6]}")
    pending: List[Tuple[str, int]] = []
    for r in rows:
        code = "".join(ch for ch in str(r.get("stkcd", "")) if ch.isdigit()).zfill(6)
        if len(code) != 6:
            continue
        fy = _to_int(r.get("first_year") or year_from)
        ly = _to_int(r.get("last_year") or year_to)
        for y in range(max(year_from, fy), min(year_to, ly) + 1):
            pending.append((code, y))
    return pending


class ResearchRunner:
    """通用科研批量 runner。fetch_announce 可被子类/适配器替换（默认巨潮年报）。"""

    def __init__(self, out_dir: Path, keywords: Dict[str, List[str]],
                 max_sentences: int = 5, min_text: int = 500,
                 api_interval: float = 2.0, jitter: float = 1.2,
                 fuse: int = 5):
        import requests
        self.sess = requests.Session()
        self.sess.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": "http://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/search",
        })
        self.out = out_dir
        self.texts = out_dir / "texts"
        try:
            self.texts.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            raise SystemExit(f"✗ 输出目录不可创建 {out_dir}（被文件占用/无权限？）: "
                             f"{type(e).__name__}: {e}") from e
        self.kw = keywords
        self.kw_names = list(keywords.keys())
        self.max_sentences = max_sentences
        self.min_text = min_text
        self.api_interval = api_interval
        self.jitter = jitter
        self.fuse = fuse
        self.progress = self._load_json(out_dir / "progress.json", {"done": {}})
        self.org_cache = self._load_json(out_dir / "orgid_cache.json", {})
        if not isinstance(self.progress, dict) or not isinstance(self.progress.get("done"), dict):
            raise SystemExit(f"✗ progress.json 结构损坏（须为含 done 键的对象，实为 "
                             f"{type(self.progress).__name__}）——修复或删除后重跑")
        if not isinstance(self.org_cache, dict):
            self.org_cache = {}  # orgId 可重建，损坏即弃

    # ---------- 基础设施 ----------
    @staticmethod
    def _load_json(p: Path, default):
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:
            return default

    def _save(self) -> None:
        # 原子落盘：tmp + os.replace——进程死在 json.dump 中途不再撕裂进度文件
        # （撕裂会被 _load_json 静默吞成零进度，触发无告警全量重抓）
        # 审查八轮（MEDIUM）：曾用**固定**临时名 progress.json.tmp / orgid_cache.json.tmp
        # ——同一 --out 上并发/重入跑两个 run 时两进程写同一 inode：一方 replace 成功后
        # 另一方 replace 抛 FileNotFoundError，被下面统一兜成 SystemExit（"被目录占用
        # 或无权限？"误诊）直接中止整批；进度文件还可能成为两次写入的交错混合体。
        # 改用 mkstemp 唯一临时名（与原目录同盘，保证 replace 原子）。
        import os as _os
        import tempfile as _tf

        def _atomic_write(path: Path, obj) -> None:
            fd, tmp = _tf.mkstemp(dir=str(self.out), prefix=path.name + ".", suffix=".tmp")
            try:
                with _os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(obj, f, ensure_ascii=False)
                _os.replace(tmp, path)
            except Exception:
                try:
                    _os.unlink(tmp)
                except OSError:
                    pass
                raise

        try:
            _atomic_write(self.out / "progress.json", self.progress)
            _atomic_write(self.out / "orgid_cache.json", self.org_cache)
        except Exception as e:
            raise SystemExit(f"✗ 状态落盘失败（progress/orgid_cache 被目录占用或无权限？）: "
                             f"{type(e).__name__}: {e}") from e

    def _allowed(self, url: str) -> str:
        from urllib.parse import urlsplit
        sp = urlsplit(url)
        host = sp.hostname or ""
        if sp.scheme not in ("http", "https") or not (
                host == "www.cninfo.com.cn" or host == "static.cninfo.com.cn"):
            raise ValueError(f"目标域不在白名单: {url!r}")
        return url

    # ---------- 巨潮适配 ----------
    def _org_id(self, code: str) -> str:
        """取该公司 orgId。**瞬时故障一律上抛**（审查八轮修复）。

        此前非 200 / 异常都静默落回 ""，调用方随即以裸 code 检索——巨潮对缺 orgId
        的 stock 参数静默返回 0 条，于是该企业-年被判 `nodata`，而 nodata 不在
        run() 的重试白名单里（只认 pdf_fail:/scan），**永久漏采**且面板把它当
        "检索窗口内无候选披露"。现在瞬时故障抛异常，交给 run() 的 API_FAIL 分支
        计数 + 保持 pending（"稍后重跑即续"语义），并由熔断闸防止无限重试。
        只有"HTTP 200 且响应可解析、确实无匹配 code"才视为确定无 orgId（沿用裸
        code 检索——未上市年份/未披露公司的正常路径）。
        """
        oid = self.org_cache.get(code)
        if oid is not None:
            return oid
        try:
            r = self.sess.post(self._allowed("http://www.cninfo.com.cn/new/information/topSearch/query"),
                               data={"keyWord": code, "maxNum": 10}, timeout=20)
        except Exception as e:
            time.sleep(3)  # 原行为保留：失败也退避一次再上抛
            raise RuntimeError(f"topSearch 请求失败（瞬时故障，未固化）："
                               f"{type(e).__name__}: {e}") from e
        if r.status_code != 200:
            time.sleep(3)
            raise RuntimeError(f"topSearch HTTP {r.status_code}（瞬时故障，未固化）")
        try:
            items = r.json() or []
        except Exception as e:
            time.sleep(3)
            raise RuntimeError(f"topSearch 响应非 JSON（瞬时故障，未固化）："
                               f"{type(e).__name__}: {e}") from e
        oid = ""
        for it in items:
            if str(it.get("code")) == code:
                oid = it.get("orgId") or ""
                break
        # 仅 200 且可解析才落缓存：瞬时故障的 "" 一旦固化，该公司全部年份退化为
        # 无 orgId 检索（部分站点静默 0 条 → 假 nodata 且永不重试）
        self.org_cache[code] = oid
        time.sleep(self.api_interval * 0.8 + random.random() * self.jitter)
        return oid

    def fetch_announce(self, code: str, year: int) -> Optional[List[Tuple[str, str]]]:
        """返回候选列表 [(标题, adjunctUrl), ...] 按发布时间降序（最新优先）；
        None=该年度无候选。标题年度必须==year。多候选供简繁回退（A+H 公司同年
        常同时发简体 A 股版与繁体 H 股版，取错版本会让简体词典计数全零）。"""
        oid = self._org_id(code)
        data = {"pageNum": 1, "pageSize": 30, "column": "szse", "tabName": "fulltext",
                "category": "category_ndbg_szsh", "seDate": f"{year + 1}-01-01~{year + 2}-12-31",
                "isHLtitle": "true", "stock": f"{code},{oid}" if oid else code}
        r = self.sess.post(self._allowed("http://www.cninfo.com.cn/new/hisAnnouncement/query"),
                           data=data, timeout=30)
        if r.status_code != 200:
            # 非 200 一律走异常：限流页若被当 nodata 落盘则永不重试，且会归零熔断计数
            raise RuntimeError(f"hisAnnouncement HTTP {r.status_code}")
        cands = []
        for a in (r.json().get("announcements") or []):
            t = (a.get("announcementTitle") or "").replace("<em>", "").replace("</em>", "")
            m = _TITLE_YEAR.search(t)
            if not m or int(m.group(1)) != year or any(x in t for x in _EXCLUDE_TITLE):
                continue
            cands.append((a.get("announcementTime") or 0, t, a.get("adjunctUrl") or ""))
        if not cands:
            return None
        cands.sort()
        return [(t, a) for _, t, a in reversed(cands)]

    def download_bytes(self, adjunct: str) -> bytes:
        r = self.sess.get(self._allowed("http://static.cninfo.com.cn/" + adjunct.lstrip("/")), timeout=90)
        # 白名单覆盖每一跳：requests 默认跟随 30x，只验首跳可被重定向带离
        for resp in ([r] + list(getattr(r, "history", []) or [])):
            if urlsplit_host(resp.url) not in ("static.cninfo.com.cn", "www.cninfo.com.cn"):
                raise ValueError(f"重定向越出白名单: {resp.url[:80]}")
        if not r.content.startswith(b"%PDF"):
            raise ValueError(f"not pdf ({len(r.content)}B)")
        return r.content

    @staticmethod
    def extract_text(pdf_bytes: bytes) -> Tuple[str, int]:
        """单一提取管线（pymupdf）。扫描件走 OCR 兜底链，来源标注进 note。"""
        import pymupdf
        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
            tf.write(pdf_bytes)
            path = tf.name
        try:
            doc = pymupdf.open(path)
            text = "".join(pg.get_text() for pg in doc)
            pages = doc.page_count
            doc.close()
            return text, pages
        finally:
            os.unlink(path)

    # ---------- 度量 ----------
    def analyze(self, text: str) -> Dict:
        total = len(text)
        counts = {k: sum(text.count(w) for w in words) for k, words in self.kw.items()}
        sents, seen = [], set()
        hit_words = [w for words in self.kw.values() for w in words]
        for s in _SENT.split(text):
            s = s.strip()
            if not (8 <= len(s) <= 300):
                continue
            if any(w in s for w in hit_words):
                if s[:80] not in seen:
                    seen.add(s[:80])
                    sents.append(s)
            if len(sents) >= self.max_sentences:
                break
        row = {"total_chars": total}
        for k, c in counts.items():
            row[f"{k}_kw_count"] = c
            row[f"{k}_kw_freq"] = round(c * 2000 / total, 3) if total else None
        row["key_sentences"] = " || ".join(sents)
        return row

    # ---------- 主循环 ----------
    def run(self, pending: List[Tuple[str, int]]) -> int:
        done = self.progress["done"]
        # 瞬时下载故障不固化：pdf_fail/scan 类键每次重跑自动重试（与 API_FAIL 语义对齐
        # ——封禁窗口期 static 主机同样不可达，恰是"稍后重跑即续"要找回的场景）
        stale = [k for k, v in done.items() if str(v).startswith(("pdf_fail:", "scan"))]
        for k in stale:
            done.pop(k, None)
        todo = [(c, y) for (c, y) in pending if f"{c}_{y}" not in done]
        log(f"research 批量: 待办 {len(todo)}（已完成 {len(done)}）")
        if not todo:
            return 0
        # 崩溃原子性：rows.jsonl 以"行已写完且 done 已标记"为提交点。进程在写行与标 done
        # 之间被杀时，重跑会把该键当待办重抓——重复行由 panel 构建端按键去重（rows 用
        # dict 覆盖语义），progress 每 20 项+熔断时落盘，最坏回退窗口 = 20 项。
        # 半行几何修复：append 打开前若文件尾不是换行（上次崩溃残留），先补一个换行，
        # 否则新 JSON 拼在半行尾部合成必失败的行——重抓数据全部随半行被丢弃。
        rows_path = self.out / "rows.jsonl"
        if rows_path.exists() and not rows_path.is_file():
            raise SystemExit(f"✗ {rows_path} 存在但不是文件（目录同名占用？）——清理后重跑")
        try:
            if rows_path.exists() and rows_path.stat().st_size > 0:
                with open(rows_path, "rb") as f:
                    f.seek(-1, 2)
                    if f.read(1) != b"\n":
                        with open(rows_path, "a", encoding="utf-8") as f:
                            f.write("\n")
            rows_f = open(rows_path, "a", encoding="utf-8")
        except PermissionError as e:
            raise SystemExit(f"✗ rows.jsonl 不可读写（权限？）: {e}") from e
        bad = ok = nodata = fail = 0
        t0 = time.time()
        for i, (code, year) in enumerate(todo):
            key = f"{code}_{year}"
            try:
                got = self.fetch_announce(code, year)
                bad = 0
                if got is None:
                    done[key] = "nodata"
                    nodata += 1
                else:
                    # 候选按发布时间降序；A+H 公司同年常并存简体 A 股版与繁体 H 股版，
                    # 取错版本会让简体词典计数全零——繁体判定命中时回退次候选
                    picked = None
                    last_err = None
                    for ci, (title, adjunct) in enumerate(got[:3]):
                        try:
                            blob = self.download_bytes(adjunct)
                            text, pages = self.extract_text(blob)
                            if _is_traditional(text) and ci + 1 < min(len(got), 3):
                                log(f"  {key}: 候选{ci}为繁体版（{title[:24]}），回退次候选")
                                continue
                            picked = (title, adjunct, blob, text, pages)
                            break
                        except Exception as e:
                            last_err = e
                            continue
                    if picked is None:
                        raise last_err or RuntimeError("所有候选下载失败")
                    title, adjunct, blob, text, pages = picked
                    # 复审闭环（2026-09-27）：末候选仍繁体（A+H 公司仅有繁体披露版）时
                    # 必须源头标注——简体词典计数失真是研究者必须知情的事实
                    if _is_traditional(text):
                        log(f"  {key}: 仅繁体披露版可用——备注标注（简体词典计数失真）")
                    try:
                        row = {"stkcd": code, "year": year, "report_section": "全文", "title": title}
                        if len(text.strip()) < self.min_text:
                            ocr_text = ocr_fallback(blob)
                            if ocr_text and len(ocr_text.strip()) >= self.min_text:
                                text, note = ocr_text, "OCR提取（扫描件）"
                            else:
                                row.update({"total_chars": None, "pdf_pages": pages,
                                            **{f"{k}_kw_{s}": None for k in self.kw_names for s in ("count", "freq")},
                                            "key_sentences": None,
                                            "备注": "PDF无法解析（扫描件，OCR亦失败）"})
                                done[key] = "scan_unreadable"
                                rows_f.write(json.dumps(row, ensure_ascii=False) + "\n")
                                rows_f.flush()
                                fail += 1
                                continue
                        else:
                            note = ""
                        if _is_traditional(text):
                            note = (note + "|" if note else "") + "繁体披露版——简体词典计数失真"
                        with gzip.open(self.texts / f"{code}_{year}.txt.gz", "wb", compresslevel=9) as g:
                            g.write(text.encode("utf-8"))
                        row.update(self.analyze(text))
                        row["pdf_pages"] = pages
                        row["备注"] = note
                        rows_f.write(json.dumps(row, ensure_ascii=False) + "\n")
                        rows_f.flush()
                        done[key] = "ok"
                        ok += 1
                    except Exception as e:
                        done[key] = f"pdf_fail:{type(e).__name__}"
                        fail += 1
                        log(f"  PDF_FAIL {key}: {str(e)[:80]}")
            except Exception as e:
                bad += 1
                fail += 1
                log(f"  API_FAIL {key}: {type(e).__name__}: {str(e)[:80]} ({bad}/{self.fuse})")
                if bad >= self.fuse:
                    log(f"!!! 连续 {self.fuse} 次异常——疑似封禁，落盘退出（稍后重跑即续）")
                    self._save()
                    return 3
                time.sleep(8 + random.random() * 7)
            if i % 20 == 0 or i == len(todo) - 1:
                self._save()
                rate = (i + 1) / max(time.time() - t0, 1)
                log(f"进度 {i + 1}/{len(todo)} ok={ok} nodata={nodata} fail={fail} "
                    f"{rate * 60:.1f}条/分 ETA={(len(todo) - i - 1) / max(rate, 1e-9) / 3600:.1f}h")
            time.sleep(self.api_interval + random.random() * self.jitter)
        self.progress["finished"] = True
        self._save()
        log(f"=== 完成: ok={ok} nodata={nodata} fail={fail} ===")
        return 0


def ocr_fallback(pdf_bytes: bytes) -> Optional[str]:
    """扫描件兜底链：pymupdf 渲染 → RapidOCR。失败返回 None（不阻塞主流程）。"""
    try:
        import pymupdf
        from rapidocr_onnxruntime import RapidOCR
        import tempfile, os
        ocr = RapidOCR()
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
            tf.write(pdf_bytes)
            path = tf.name
        try:
            doc = pymupdf.open(path)
            parts = []
            for pg in doc:  # 只渲染前 40 页：OCR 成本高，面板度量通常足够
                if pg.number >= 40:
                    break
                pix = pg.get_pixmap(dpi=150)
                img = pix.tobytes("png")
                res, _ = ocr(img)
                if res:
                    parts.extend(line[1] for line in res)
            doc.close()
            return "\n".join(parts)
        finally:
            os.unlink(path)
    except Exception:
        return None


def build_panel_xlsx(out_dir: Path, universe_csv: str, keywords: Dict[str, List[str]],
                     year_from: int, year_to: int, xlsx_path: Path) -> Dict[str, int]:
    """rows.jsonl → 面板 xlsx（含清单外数据表/缺口清单/说明——审计后交付格式）。"""
    import csv
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from collections import Counter

    rows_p = out_dir / "rows.jsonl"
    if not rows_p.is_file():
        raise SystemExit(f"✗ {rows_p} 不存在或不是文件——先跑 research run 再生成面板")
    try:
        done = json.load(open(out_dir / "progress.json", encoding="utf-8")).get("done", {})
        assert isinstance(done, dict)
    except Exception:
        done = {}
    win = {}
    try:
        universe_rows = list(csv.DictReader(open(universe_csv, encoding="utf-8-sig")))
    except Exception as e:
        raise SystemExit(f"✗ 清单 CSV 读取失败: {type(e).__name__}: {e}") from e
    if universe_rows and "stkcd" not in universe_rows[0]:
        raise SystemExit(f"✗ 清单缺 stkcd 列: 实有 {list(universe_rows[0])[:6]}")
    for r in universe_rows:
        code = "".join(ch for ch in str(r.get("stkcd", "")) if ch.isdigit()).zfill(6)
        win[code] = (_to_int(r.get("first_year") or year_from), _to_int(r.get("last_year") or year_to))

    rows = {}
    try:
        rows_iter = open(out_dir / "rows.jsonl", encoding="utf-8", errors="replace")
    except Exception as e:
        raise SystemExit(f"✗ rows.jsonl 读取失败: {type(e).__name__}: {e}") from e
    for line in rows_iter:
        try:
            r = json.loads(line)  # 半行（崩溃窗口）在此被丢弃；同键后写覆盖先写
            rows[(r["stkcd"], int(r["year"]))] = r
        except Exception:
            pass

    panel, outside = [], []
    for (code, year), r in sorted(rows.items()):
        if code in win and win[code][0] <= year <= win[code][1]:
            panel.append(r)
        else:
            outside.append(r)

    cols = (["stkcd", "year", "report_section", "title", "total_chars", "pdf_pages"]
            + [f"{k}_kw_{s}" for k in keywords for s in ("count", "freq")]
            + ["key_sentences", "备注"])
    stat = Counter()
    wb = Workbook()
    wb.remove(wb.active)
    ws = wb.create_sheet("面板")
    ws.append(cols)
    for r in panel:
        ws.append([_ILLEGAL_XLSX.sub("", str(r.get(c))) if isinstance(r.get(c), str) else r.get(c) for c in cols])
        stat["real" if r.get("total_chars") else "scan"] += 1
    for c in ws[1]:
        c.font = Font(bold=True)
    ws.freeze_panes = "A2"
    wso = wb.create_sheet("清单外数据")
    wso.append(cols + ["原因"])
    for r in outside:
        wso.append([_ILLEGAL_XLSX.sub("", str(r.get(c))) if isinstance(r.get(c), str) else r.get(c) for c in cols]
                   + ["不在清单或超出 [first_year,last_year] 窗口"])
    wg = wb.create_sheet("缺口清单")
    wg.append(["项目", "数量", "说明"])
    wg.append(["nodata", sum(1 for v in done.values() if v == "nodata"), "检索窗口内无候选公告（发布窗 Y+1~Y+2 + 标题年度强校验）"])
    wg.append(["扫描件不可解析", stat["scan"], "保留占位行，备注标注；OCR 亦失败才计此列"])
    fails = [k for k, v in done.items() if str(v).startswith(("pdf_fail", "scan"))]
    wg.append(["pdf/解析失败", len(fails), "；".join(fails[:8])])
    wn = wb.create_sheet("说明")
    wn.append([f"科研批量采集产物：面板 {len(panel)} 行（真实统计 {stat['real']}）；关键词组 {'/'.join(keywords)}；"
               f"freq=count×2000/total_chars；单一提取管线 pymupdf（OCR 行已标注）；"
               f"文本档案 texts/*.txt.gz 可对任意词典零成本重算；报告年度=标题年度（强校验）。"])
    try:
        wb.save(xlsx_path)
    except Exception as e:
        raise SystemExit(f"✗ 面板 xlsx 保存失败（检查目录存在/权限）: {type(e).__name__}: {e}") from e
    return {"panel": len(panel), "real": stat["real"], "outside": len(outside)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="research", description="科研批量采集（五任务实战动线引擎化）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run_p = sub.add_parser("run", help="清单×年份窗 批量采集（断点续跑/自熔断/文本档案）")
    run_p.add_argument("--universe", required=True, help="企业清单 CSV（stkcd[,first_year,last_year]）")
    run_p.add_argument("--out", required=True, help="输出目录（progress/texts/rows.jsonl）")
    run_p.add_argument("--keywords", help="关键词词典 JSON（缺省内置 合规/出海/收缩 三组）")
    run_p.add_argument("--year-from", type=int, default=2010)
    run_p.add_argument("--year-to", type=int, default=2024)
    run_p.add_argument("--fuse", type=int, default=5, help="连续 API 异常熔断阈值")
    x_p = sub.add_parser("panel", help="rows.jsonl → 面板 xlsx（含清单外/缺口/说明）")
    x_p.add_argument("--out", required=True)
    x_p.add_argument("--universe", required=True)
    x_p.add_argument("--keywords")
    x_p.add_argument("--year-from", type=int, default=2010)
    x_p.add_argument("--year-to", type=int, default=2024)
    x_p.add_argument("--xlsx", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "run":
        kw = load_keywords(getattr(args, "keywords", None))
        pending = load_universe(args.universe, args.year_from, args.year_to)
        runner = ResearchRunner(Path(args.out), kw, fuse=args.fuse)
        return runner.run(pending)
    kw = load_keywords(getattr(args, "keywords", None))
    st = build_panel_xlsx(Path(args.out), args.universe, kw, args.year_from, args.year_to, Path(args.xlsx))
    log(f"面板 xlsx: {st}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
