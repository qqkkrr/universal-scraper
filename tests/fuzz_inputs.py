#!/usr/bin/env python3
"""模糊测试（收官十五轮新增）：畸形输入不得抛出非受控异常。

三类输入：
1. 配置校验：随机畸形配置 → validate/validate_task 只允许抛 ConfigError（或正常返回），
   不允许 AttributeError/TypeError/KeyError/IndexError 等裸异常
2. 管道步骤：随机畸形 step dict → Pipeline.process 允许跳过该步，不允许崩
3. HTML 抽取：随机畸形 HTML → html_to_markdown / extract_tables / extract_article 不崩

说明：本文件用 `random.Random(<常量种子>)` 是**故意的**——测试夹具必须可复现
（同一批畸形输入每轮一致，失败可精确定位），与加密用途无关。

用法: python3 tests/fuzz_inputs.py [轮数，默认 300]
"""
import json
import random
import string
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(("  ✅ " if cond else "  ❌ ") + name + (f" | {detail}" if detail and not cond else ""))


ATOMS = [None, True, False, 0, 1, -1, 3.14, "", "x", "0", [], {}, "null", "None",
         {"a": 1}, [1, 2], "配置", "  ", "\n", "\t", "a" * 300, 10 ** 20, float("inf"),
         float("nan"), ["x", "y"], {"a": {"b": {"c": [1, 2, {"d": None}]}}}]
STEPS = ["filter", "dedup", "cast", "add", "rename", "template", "default", "validate",
         "regex_extract", "transform", "parse_date", "dedup_content", "download",
         "split", "unknown_step", None, 123, ["filter"]]
FIELDS = ["field", "type", "key", "fields", "op", "value", "pattern", "group", "to",
          "out", "tmpl", "mapping", "min", "max", "as", "required", "dir", "name"]


def rand_step(rng) -> object:
    if rng.random() < 0.15:
        return rng.choice(ATOMS)
    st = {"type": rng.choice(STEPS)}
    for _ in range(rng.randint(0, 4)):
        st[rng.choice(FIELDS)] = rng.choice(ATOMS)
    return st


def rand_cfg(rng) -> dict:
    cfg = {"name": rng.choice(["t", "", None, 123, "配置名"]),
           "source": rng.choice([{"type": "http_html", "url": "https://x.test/l"},
                                 {"type": "http", "url": "https://x.test/l"},
                                 {"type": rng.choice(ATOMS)}, "not-a-dict", {}])}
    if rng.random() < 0.8:
        cfg["pipeline"] = [rand_step(rng) for _ in range(rng.randint(0, 3))]
    if rng.random() < 0.6:
        cfg["detail"] = rng.choice([
            {"enabled": True, "url_field": "url", "extract": [rand_step(rng)]},
            {"enabled": True, "post_pipeline": [rand_step(rng)], "filters": [rand_step(rng)]},
            rng.choice(ATOMS)])
    if rng.random() < 0.5:
        cfg["record"] = rng.choice([{"fields": rng.choice(ATOMS)}, rng.choice(ATOMS)])
    if rng.random() < 0.4:
        cfg["source"] = dict(cfg.get("source") or {}) if isinstance(cfg.get("source"), dict) else {}
        cfg["source"].update({"fields": rng.choice(ATOMS), "row_css": rng.choice(ATOMS)})
    return cfg


def fuzz_configs(n: int) -> list:
    from universal_scraper.config import validate, validate_task, ConfigError
    rng = random.Random(1234)
    bad = []
    for i in range(n):
        cfg = rand_cfg(rng)
        for fn, kw in ((validate, {}), (validate_task, {"has_custom_fetcher": False,
                                                        "has_custom_parser": False,
                                                        "has_custom_storage": False})):
            try:
                fn(dict(cfg), **kw)
            except ConfigError:
                pass
            except SystemExit:
                pass          # v2 validate 对部分错误走 SystemExit（CLI 风格）
            except Exception as e:
                bad.append(f"#{i} {fn.__name__}: {type(e).__name__}: {str(e)[:70]} cfg={json.dumps(cfg, default=str)[:110]}")
    return bad


def fuzz_pipelines(n: int) -> list:
    """管道模糊：**在临时 CWD 里跑**——download 步骤会按 dir 建目录，此前在仓库根
    跑出过 `dl_out/` 与名为两个空格的垃圾目录（收官十六轮审查实测）。"""
    import os as _os
    import tempfile as _tf
    from universal_scraper.modules.pipelines import Pipeline
    rng = random.Random(4321)
    bad = []
    row = {"标题": "x", "n": "12", "url": "https://x.test/d/1", "raw": "€ 9.9"}
    _cwd0 = _os.getcwd()
    _tmp = _tf.mkdtemp(prefix="us_fuzz_")
    _os.chdir(_tmp)
    try:
        for i in range(n):
            steps = [rand_step(rng) for _ in range(rng.randint(1, 3))]
            try:
                p = Pipeline(steps, {})
                out = p.process(dict(row))
                json.dumps(out, default=str)      # 结果必须可序列化
            except Exception as e:
                bad.append(f"#{i}: {type(e).__name__}: {str(e)[:70]} "
                           f"steps={json.dumps(steps, default=str)[:110]}")
    finally:
        _os.chdir(_cwd0)          # 无论成败都还原，避免影响后续用例
    return bad


HTML_FRAGS = ["<table>", "</table>", "<tr><td>", "<div class='x'>", "<script>",
              "<style>", "<th>a</th>", "<html>", "<!--", "&nbsp;", "\u0000", "ä½ å¥½",
              "<a href=//x>", "<input", "<form", "<option value=>", "&#x27;", "<![CDATA[",
              "<p>" * 50, "</p>" * 50]


def fuzz_html(n: int) -> list:
    from universal_scraper.extractors import html_to_markdown, extract_tables, extract_article, discover_forms
    rng = random.Random(999)
    bad = []
    for i in range(n):
        parts = []
        for _ in range(rng.randint(1, 25)):
            parts.append(rng.choice(HTML_FRAGS))
            if rng.random() < 0.3:
                parts.append("".join(rng.choice(string.printable) for _ in range(rng.randint(0, 12))))
        html = "".join(parts)
        for fn, args in ((html_to_markdown, (html,)), (extract_tables, (html,)),
                         (extract_article, (html,)), (discover_forms, (html, "https://x.test/"))):
            try:
                json.dumps(fn(*args), default=str)
            except RecursionError:
                bad.append(f"#{i} {fn.__name__}: RecursionError html={html[:80]!r}")
            except Exception as e:
                bad.append(f"#{i} {fn.__name__}: {type(e).__name__}: {str(e)[:60]} html={html[:80]!r}")
    return bad


def main(n: int = 300):
    print(f"== 模糊测试（每类 {n} 轮） ==")
    b1 = fuzz_configs(n)
    check("配置校验只抛 ConfigError/SystemExit", not b1, f"{len(b1)} 例，前 3: {b1[:3]}")
    b2 = fuzz_pipelines(n)
    check("管道步骤畸形输入不崩", not b2, f"{len(b2)} 例，前 3: {b2[:3]}")
    b3 = fuzz_html(n)
    check("HTML 抽取畸形输入不崩", not b3, f"{len(b3)} 例，前 3: {b3[:3]}")
    bad = [x for x in RESULTS if not x[1]]
    print(f"\n模糊测试 {len(RESULTS)} 项，失败 {len(bad)}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 300))
