#!/usr/bin/env python3
"""淘宝/天猫商品详情参数批量抓取（CDP 已登录 Chrome）。

用法:
  python3 scripts/taobao_params_batch.py "链接1,链接2,链接3..." [--out outputs/taobao_params.json]
  或从文件读链接（每行一个）: python3 scripts/taobao_params_batch.py --file links.txt
"""
import argparse, json, os, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("links", nargs="?", default="", help="逗号分隔的商品链接")
    ap.add_argument("--file", default="", help="链接文件（每行一个）")
    ap.add_argument("--out", default="outputs/taobao_params.json")
    ap.add_argument("--cdp", default=os.environ.get("US_CDP", "http://127.0.0.1:9222"))
    args = ap.parse_args()

    links = []
    if args.file:
        links = [l.strip() for l in Path(args.file).read_text(encoding="utf-8").splitlines() if l.strip()]
    if args.links:
        links += [l.strip() for l in args.links.split(",") if l.strip()]
    links = list(dict.fromkeys(links))
    if not links:
        print("❌ 请提供商品链接（--links 或 --file）")
        return 1

    from universal_scraper.runtime import resolve_node, resolve_node_path
    node = os.environ.get("UNIVERSAL_SCRAPER_NODE", resolve_node())
    npath = os.environ.get("UNIVERSAL_SCRAPER_NODE_PATH", resolve_node_path())
    bridge = ROOT / "scripts" / "taobao_shop_bridge.cjs"
    cmd = [node, str(bridge), "--cdp", args.cdp, "--links", ",".join(links)]
    env = {**os.environ, "NODE_PATH": npath}
    print(f"🕐 开始抓取 {len(links)} 个商品（CDP {args.cdp}）...")
    p = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=3600)
    rows = []
    errs = []
    for line in (p.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("type") == "item":
            params = "; ".join(obj.get("params") or [])
            rows.append({
                "标题": obj.get("title") or obj.get("list_title") or "",
                "链接": obj.get("url", ""),
                "产品参数": params,
            })
        elif obj.get("type") == "item_fail":
            errs.append(obj.get("url", ""))
        elif obj.get("type") == "error":
            print("❌ " + str(obj.get("message", ""))[:300])
            return 1
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ 成功 {len(rows)}/{len(links)}，失败 {len(errs)}")
    print(f"📄 输出: {out}")
    for r in rows[:5]:
        print(f"  - {str(r['标题'])[:40]} | 参数 {len(r['产品参数'])} 字符")
    return 0


if __name__ == "__main__":
    sys.exit(main())
