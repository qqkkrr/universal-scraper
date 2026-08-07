#!/usr/bin/env python3
"""给已抓取的公告列表补全详情正文（直连抓取，支持断点续跑）。

用法:
  python3 ggzy_details.py --input outputs/ggzy_数据中心_2025-01-01_2025-01-31_合并.json \
    --out-dir outputs --interval 0.5
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from universal_scraper.core import HttpClient, export_rows, log
from ggzy_crawl import fetch_detail, full_url  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--out-dir", default="outputs")
    ap.add_argument("--interval", type=float, default=0.5)
    ap.add_argument("--timeout", type=float, default=15)
    ap.add_argument("--checkpoint-every", type=int, default=20)
    args = ap.parse_args()

    in_path = Path(args.input)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cp_path = out_dir / "checkpoint_detail.json"

    data = json.loads(in_path.read_text(encoding="utf-8"))
    log(f"加载 {len(data)} 条记录")

    # 断点续跑：已有 detail_body 的跳过
    todo = [r for r in data if not (r.get("detail_body") or "").strip()]
    done = len(data) - len(todo)
    log(f"已有详情 {done} 条，待抓 {len(todo)} 条")

    http = HttpClient(min_interval=args.interval, timeout=args.timeout)

    ok, fail = 0, 0
    for i, r in enumerate(todo, 1):
        url = full_url(r.get("url") or "")
        if not url:
            continue
        try:
            det = fetch_detail(http, url)
            for k, v in det.items():
                r[k] = v
            ok += 1
        except Exception as e:
            r["detail_status"] = f"ERR:{type(e).__name__}"
            fail += 1
            log(f"  [{i}/{len(todo)}] 失败: {e}", "WARN")
        if i % args.checkpoint_every == 0:
            cp_path.write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")
            log(f"  进度 {i}/{len(todo)}（成功 {ok} 失败 {fail}），已存检查点")
        time.sleep(args.interval)

    cp_path.write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")

    # 导出：分阶段 + 合并
    merged = data
    base = in_path.stem.replace("合并", "").rstrip("_")
    paths = export_rows(merged, out_dir, f"{base}_合并")
    log("合并导出: " + ", ".join(f"{k}={v}" for k, v in paths.items()))

    for label, stage in (("招标公告", "0001"), ("中标公告", "0002")):
        rows = [r for r in merged if r.get("stage") == stage]
        if rows:
            paths = export_rows(rows, out_dir, f"{base}_{label}")
            log(f"{label} 导出: {len(rows)} 条 -> " + ", ".join(f"{k}={v}" for k, v in paths.items()))

    has_body = sum(1 for r in merged if (r.get("detail_body") or "").strip())
    log(f"完成：{len(merged)} 条，其中详情正文成功 {has_body} 条（失败 {len(merged)-has_body}）")


if __name__ == "__main__":
    main()
