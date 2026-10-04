#!/usr/bin/env python3
r"""审查十四轮（R14）修复的回归测试——全部 hermetic。

覆盖：
- selectors：extract_embedded_json_rows 空壳一律 []（{} 单对象 / [{},{}]）
- book：coverage 补 missing/missing_rate 别名
- precise：img_src_hint 坏正则降级不炸（源级）
- auto：_save_learned 保留键补全 / headers 剥 Cookie / 0600 / @文件 白名单+凭据路径
- fetchers：capture 文件名按桥侧规则先找（源级）/ body+json_body 双给告警（源级）
- engine_v3：fetch_all 补 on_data/flush/fetcher.close（源级）
- ReDoS R3 保守拦截确认（交替+外层量词拦——实测 (\d+|color)+ 对 4k 输入真冻结）
"""
import inspect
import json
import sys
from pathlib import Path


SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL))


# ---------------- selectors ----------------

def test_selectors_empty_shell_returns_empty():
    from universal_scraper.selectors import extract_embedded_json_rows
    assert extract_embedded_json_rows('window.x = {}', "x") == []
    assert extract_embedded_json_rows('window.x = [{},{}]', "x") == []
    # 正常记录回归
    assert extract_embedded_json_rows('window.x = [{"a":1}]', "x") == [{"a": 1}]


# ---------------- book ----------------

def test_book_coverage_alias_keys():
    from universal_scraper import book_catalog as B
    src = inspect.getsource(B.build_catalog)
    # coverage 返回体同时带 missing/missing_rate（修前只有 all_* 键，
    # 消费 coverage["missing_rate"] 的调用方拿到 None）
    assert '"missing_rate": f"{missing' in src
    assert '"all_missing_rate": f"{missing' in src


# ---------------- precise ----------------

def test_precise_img_hint_bad_regex_guarded():
    from universal_scraper import precise_auto as P
    src = inspect.getsource(P._image_ranking_run)
    assert "re.compile(hint)" in src and "re.error" in src


# ---------------- auto ----------------

def test_auto_save_learned_strips_cookie_and_0600(tmp_path, monkeypatch):
    import os
    from universal_scraper import auto as A
    monkeypatch.setattr(A, "LEARNED_DIR", tmp_path)
    cfg = {
        "name": "t", "start_urls": ["https://shop.example.com/list"],
        "source": {"type": "http", "url": "https://shop.example.com/list",
                   "headers": {"Cookie": "SESSDATA=SECRET", "Referer": "https://shop.example.com/"},
                   "query": {"cat": "book"},
                   "actions": [{"type": "click", "selector": ".more"}]},
        "rules": [{"match": "contains", "pattern": "/", "parser": "default"}],
        "parsers": {"default": {"type": "html", "fields": {"t": "h1"}}},
    }
    A._save_learned(cfg, "抓 shop.example.com 图书", log=lambda *a, **k: None)
    files = list(tmp_path.glob("*.json"))
    assert files, "应已落盘"
    raw = files[0].read_text(encoding="utf-8")
    # Cookie 明文不落盘（修前 SESSDATA=SECRET 原样进 0644 文件）
    assert "SESSDATA=SECRET" not in raw
    assert "Referer" in raw                          # 非 Cookie 头保留
    # 保留键补全（修前漏 query/actions/login/verify）
    cfg_saved = json.loads(raw)["config"]["source"]
    assert cfg_saved.get("query", {}).get("cat") == "book"
    assert cfg_saved.get("actions")
    # 0600
    assert (os.stat(files[0]).st_mode & 0o777) == 0o600


def test_auto_file_refs_whitelist(tmp_path):
    from universal_scraper.auto import _resolve_file_refs
    d = tmp_path
    (d / "list.txt").write_text("600519", encoding="utf-8")
    (d / "secret.key").write_text("PRIVATE", encoding="utf-8")
    out = _resolve_file_refs(f"抓 @{d}/list.txt 里的公司", lambda *a, **k: None)
    assert "600519" in out
    out2 = _resolve_file_refs(f"读 @{d}/secret.key", lambda *a, **k: None)
    assert "PRIVATE" not in out2                     # 非白名单扩展名拒绝
    out3 = _resolve_file_refs("读 @~/.ssh/id_rsa", lambda *a, **k: None)
    assert "OPENSSH" not in out3 and "PRIVATE KEY" not in out3  # 凭据路径拒绝


# ---------------- fetchers ----------------

def test_fetchers_capture_name_bridge_rule():
    from universal_scraper.fetchers import BrowserFetcher
    src = inspect.getsource(BrowserFetcher._records_from_capture)
    # 桥侧净化规则（保留空格等）优先，safe_fname 兜底——修前只有 safe_fname 一种
    assert '_bridged = re.sub' in src
    assert "未捕获到文件" in src                       # 缺文件不再静默


def test_fetchers_body_json_both_warn():
    from universal_scraper.fetchers import HttpFetcher
    src = inspect.getsource(HttpFetcher._request)
    assert "_b = None" in src and "已优先 json_body" in src


# ---------------- engine_v3 ----------------

def test_engine_v3_fetch_all_wiring():
    from universal_scraper import engine_v3 as E
    src = inspect.getsource(E.EngineV3._run_fetch_all)
    assert "apply_item_hooks" in src                  # 插件钩子
    assert "mw.on_data" in src                        # 中间件 on_data
    assert "mw.flush" in src                          # 中间件 flush
    assert "self.fetcher.close()" in src              # 取数器收尾（修前泄漏）


# ---------------- ReDoS R3 保守拦截（实测确认 (\d+|color)+ 危险） ----------------

def test_regex_r3_alternation_plus_still_blocked():
    from universal_scraper.selectors import regex_is_dangerous
    # 交替 + 外层量词：切分组合仍指数（实测 (\d+|color)+ 对 4k 输入冻结）——
    # 首集不相交不豁免（保守拦截，引导用户改单字符类写法）
    assert regex_is_dangerous(r"(\d+|color)+") is True
