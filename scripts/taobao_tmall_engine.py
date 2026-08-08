#!/usr/bin/env python3
"""淘宝/天猫快引擎（实测通：2026-08）

三段式，全部基于「用户已登录的 CDP Chrome」导出的 cookie，无需重新登录：
  A. 商品列表（纯 HTTP，秒级）
       - 天猫店铺:  https://{shop}.m.tmall.com/shop/shop_auction_search.do  (JSONP, 全店分页)
       - 淘宝搜索:  MTOP mtop.taobao.wsearch.appsearch (HMAC-MD5 签名 + curl_cffi TLS 指纹)
  B. 详情产品参数（CDP 浏览器，免点击）
       - 详情页加载后「参数信息」即存在于 DOM，直接解析；多标签页并行（--workers）

用法:
  python3 scripts/taobao_tmall_engine.py --mode shop    --target https://cultum.tmall.com/shop/view_shop.htm [--max 50]
  python3 scripts/taobao_tmall_engine.py --mode search  --target cultum [--max 50]
  python3 scripts/taobao_tmall_engine.py --mode links   --target "url1,url2"
  通用: [--skip-details] [--workers 2] [--out outputs/xxx.json] [--cdp http://127.0.0.1:9222]
"""
import argparse, json, os, re, subprocess, sys, time, hashlib, random
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

APPKEY = "12574478"
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1")


# ─────────────────────────── CDP 工具 ───────────────────────────

DUMP_COOKIES_JS = r"""
const { chromium } = require("patchright");
(async () => {
  const browser = await chromium.connectOverCDP(process.env.US_CDP || "http://127.0.0.1:9222");
  const all = [];
  for (const ctx of browser.contexts()) {
    const cookies = await ctx.cookies();
    for (const c of cookies) {
      if (/taobao|tmall/.test(c.domain))
        all.push({ name: c.name, value: c.value, domain: c.domain, path: c.path,
                   expires: c.expires, httpOnly: c.httpOnly, secure: c.secure });
    }
  }
  process.stdout.write(JSON.stringify(all));
  await browser.close();
})().catch(e => { console.error(e); process.exit(1); });
"""


def resolve_node():
    try:
        from universal_scraper.runtime import resolve_node, resolve_node_path
        return resolve_node(), resolve_node_path()
    except Exception:
        return "node", ""


def dump_cookies(cdp: str) -> list:
    """从 CDP Chrome 导出淘宝/天猫 cookie；失败抛 RuntimeError。"""
    node, npath = resolve_node()
    env = {**os.environ, "NODE_PATH": npath, "US_CDP": cdp}
    p = subprocess.run([node, "-e", DUMP_COOKIES_JS], capture_output=True, text=True,
                       env=env, timeout=60)
    if p.returncode != 0 or not p.stdout.strip():
        raise RuntimeError("从 CDP 导出 cookie 失败：" + (p.stderr or p.stdout or "无输出")[-200:]
                           + "（请先双击「启动淘宝调试Chrome.command」并登录淘宝一次）")
    try:
        return json.loads(p.stdout.strip().splitlines()[-1])
    except Exception as e:
        raise RuntimeError(f"cookie JSON 解析失败: {e}")


def session_for(cookies, domains: list):
    from curl_cffi import requests as creq
    s = creq.Session(impersonate="chrome")
    for c in cookies:
        d = c.get("domain", "")
        if any(k in d for k in domains):
            try:
                s.cookies.set(c["name"], c["value"], domain="." + d.lstrip("."))
            except Exception:
                pass
    return s



def normalize_detail_url(url: str) -> str:
    """统一详情页 URL 为桌面版 detail.tmall.com/item.htm?id=...（移动端详情页在新标签是空壳）。"""
    m = re.search(r"[?&]id=(\d+)", url or "")
    if m:
        return f"https://detail.tmall.com/item.htm?id={m.group(1)}"
    return url


# ─────────────────────────── A. 商品列表 ───────────────────────────

def tmall_shop_items(shop_sub: str, cookies, max_items: int = 500):
    """天猫移动端店铺商品接口（JSONP）。返回 [{item_id,title,price,sold,url,img}]"""
    from curl_cffi import requests as creq
    s = session_for(cookies, ["tmall"])
    base = f"https://{shop_sub}.m.tmall.com/shop/shop_auction_search.do"
    out, seen, page = [], set(), 1
    total_page = None
    while len(out) < max_items:
        num = random.randint(83739921, 87739530)
        url = f"{base}?sort=s&p={page}&page_size=24&from=h5&ajson=1&_tm_source=tmallsearch&callback=jsonp_{num}"
        r = s.get(url, timeout=20)
        m = re.search(r'^[^(]*\((.*)\)\s*$', r.text, re.S)
        if not m:
            if "baxia" in r.text or "login" in r.text.lower() and len(r.text) < 4000:
                raise RuntimeError("天猫店铺接口被要求登录/验证——请确认 CDP Chrome 已登录淘宝")
            raise RuntimeError(f"店铺接口返回异常（{r.status_code}，len={len(r.text)}）")
        data = json.loads(m.group(1))
        total_page = int(data.get("total_page") or 1)
        items = data.get("items") or []
        if not items:
            break
        for it in items:
            iid = str(it.get("item_id") or "")
            if not iid or iid in seen:
                continue
            seen.add(iid)
            u = it.get("url") or f"//detail.m.tmall.com/item.htm?id={iid}"
            out.append({
                "item_id": iid,
                "title": (it.get("title") or "").strip(),
                "price": str(it.get("price") or "").strip(),
                "sold": str(it.get("sold") or it.get("totalSoldQuantity") or "").strip(),
                "url": ("https:" + u) if u.startswith("//") else u,
                "img": it.get("img") or "",
            })
            if len(out) >= max_items:
                break
        if page >= total_page:
            break
        page += 1
        time.sleep(0.6 + random.random() * 0.4)
    return out


def mtop_search(keyword: str, cookies, max_items: int = 100, pages: int = 3):
    """MTOP 淘宝搜索（HMAC-MD5 签名）。返回 [{item_id,title,price,realSales,shop,url}]"""
    s = session_for(cookies, ["taobao"])

    def get_token():
        try:
            c = s.cookies.get("_m_h5_tk", domain=".taobao.com")
        except Exception:
            c = None
        return c.split("_")[0] if c else ""

    def call(page_no: int):
        data = json.dumps({"q": keyword, "search_action": "initiative", "page": str(page_no),
                           "n": "24", "sversion": "9.9.9"}, separators=(",", ":"), ensure_ascii=False)
        for attempt in range(4):
            token = get_token()
            t = str(int(time.time() * 1000))
            sign = hashlib.md5(f"{token}&{t}&{APPKEY}&{data}".encode()).hexdigest()
            params = {"jsv": "2.7.2", "appKey": APPKEY, "t": t, "sign": sign,
                      "api": "mtop.taobao.wsearch.appsearch", "v": "1.0",
                      "type": "originaljson", "dataType": "json", "H5Request": "true", "data": data}
            r = s.get("https://h5api.m.taobao.com/h5/mtop.taobao.wsearch.appsearch/1.0/",
                      params=params, timeout=20,
                      headers={"Referer": "https://h5.m.taobao.com/", "Accept": "application/json",
                               "Origin": "https://h5.m.taobao.com"})
            try:
                j = r.json()
            except Exception:
                j = {}
            ret = j.get("ret", [])
            ret_str = "::".join(str(x) for x in ret) if isinstance(ret, list) else str(ret)
            if any("TOKEN" in str(x) for x in ret) and attempt < 3:
                time.sleep(1)
                continue
            return j
        return {}

    out, seen = [], set()
    for page_no in range(1, pages + 1):
        j = call(page_no)
        data = j.get("data") or {}
        arr = data.get("itemsArray") or []
        if not arr:
            break
        for it in arr:
            iid = str(it.get("item_id") or "")
            if not iid or iid in seen:
                continue
            seen.add(iid)
            psi = it.get("priceShowWithIcon") or {}
            shop_info = it.get("shopInfo") or {}
            slist = shop_info.get("shopInfoList") or []
            shop = slist[0] if slist else ""
            out.append({
                "item_id": iid,
                "title": (it.get("title") or "").strip(),
                "price": str(psi.get("price") or it.get("price") or "").strip(),
                "sold": str(it.get("realSales") or "").strip(),
                "shop": shop,
                "url": f"https://item.taobao.com/item.htm?id={iid}",
            })
            if len(out) >= max_items:
                return out
        time.sleep(0.8 + random.random() * 0.4)
    return out


# ─────────────────────────── B. 详情参数 ───────────────────────────

def fetch_details(rows: list, cdp: str, workers: int, timeout: int = 1800):
    """调 CDP 桥批量抓详情参数；把结果写回 rows（新增 标题_详情/价格_详情/店铺_详情/产品参数）。"""
    from universal_scraper.runtime import resolve_node, resolve_node_path
    if not rows:
        return
    node = os.environ.get("UNIVERSAL_SCRAPER_NODE", resolve_node())
    npath = os.environ.get("UNIVERSAL_SCRAPER_NODE_PATH", resolve_node_path())
    bridge = ROOT / "scripts" / "taobao_shop_bridge.cjs"
    links = ",".join(normalize_detail_url(r["url"]) for r in rows)
    cmd = [node, str(bridge), "--cdp", cdp, "--links", links, "--workers", str(workers),
           "--max", str(len(rows))]
    env = {**os.environ, "NODE_PATH": npath}
    print(f"🕐 详情参数：{len(rows)} 个商品，{workers} 个并行标签…（约 {len(rows)*5//workers}s）")
    p = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=timeout)
    by_url = {}
    for line in (p.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("type") == "item":
            by_url[obj.get("url", "")] = obj
    for r in rows:
        d = by_url.get(normalize_detail_url(r["url"])) or {}
        if d:
            if d.get("params"):
                r["产品参数"] = "；".join(str(x) for x in d["params"] if str(x).strip())
            r["标题_详情"] = d.get("title") or ""
            r["价格_详情"] = d.get("price") or ""
            r["店铺_详情"] = d.get("shop") or ""
    ok = sum(1 for r in rows if r.get("产品参数"))
    missing = [r for r in rows if not r.get("产品参数")]
    if missing:
        print(f"⚠️ 首轮缺参数 {len(missing)} 条，5 秒后补抓一轮…")
        time.sleep(5)
        links2 = ",".join(normalize_detail_url(r["url"]) for r in missing)
        cmd2 = [node, str(bridge), "--cdp", cdp, "--links", links2, "--workers", str(max(1, workers // 2)),
                "--max", str(len(missing))]
        try:
            p2 = subprocess.run(cmd2, capture_output=True, text=True, env=env, timeout=timeout)
            by_url2 = {}
            for line in (p2.stdout or "").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if obj.get("type") == "item":
                    by_url2[obj.get("url", "")] = obj
            for r in missing:
                d = by_url2.get(normalize_detail_url(r["url"])) or {}
                if d and d.get("params"):
                    r["产品参数"] = "；".join(str(x) for x in d["params"] if str(x).strip())
                    if d.get("title"):
                        r["标题_详情"] = d.get("title")
        except Exception as e:
            print(f"⚠️ 补抓失败：{e}")
        ok = sum(1 for r in rows if r.get("产品参数"))
    print(f"✅ 详情参数：成功 {ok}/{len(rows)}")
    return ok


# ─────────────────────────── 入口 ───────────────────────────

def infer_shop_sub(target: str):
    m = re.search(r'https?://([a-z0-9\-]+)\.m?\.(tmall|taobao)\.com', target)
    if m:
        return m.group(1), m.group(2)
    m2 = re.search(r'(?:^|//)([a-z0-9\-]+)\.(tmall|taobao)\.com', target)
    if m2:
        return m2.group(1), m2.group(2)
    return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["shop", "search", "links"], required=True)
    ap.add_argument("--target", required=True, help="店铺URL / 搜索关键词 / 逗号分隔商品链接")
    ap.add_argument("--max", type=int, default=50)
    ap.add_argument("--skip-details", action="store_true", help="只抓列表，不抓详情参数")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--out", default="")
    ap.add_argument("--cdp", default=os.environ.get("US_CDP", "http://127.0.0.1:9222"))
    ap.add_argument("--json", action="store_true", help="末尾输出 RESULT_JSON 供程序调用")
    args = ap.parse_args()

    cookies = dump_cookies(args.cdp)
    rows = []
    list_src = ""

    if args.mode == "shop":
        sub, tld = infer_shop_sub(args.target)
        if not sub:
            raise SystemExit("无法从目标识别店铺名，请给完整店铺 URL，如 https://cultum.tmall.com/shop/view_shop.htm")
        if tld != "tmall":
            raise SystemExit("当前快路径只支持天猫店铺（xxx.tmall.com）；淘宝店铺请用 --mode search 或 --mode links")
        print(f"🔎 天猫店铺接口: {sub}.m.tmall.com …")
        rows = tmall_shop_items(sub, cookies, args.max)
        list_src = f"shop:{sub}"
    elif args.mode == "search":
        print(f"🔎 MTOP 搜索: {args.target} …")
        rows = mtop_search(args.target, cookies, args.max)
        list_src = f"search:{args.target}"
    else:
        for u in args.target.split(","):
            u = u.strip()
            if not u:
                continue
            if u.startswith("//"):
                u = "https:" + u
            rows.append({"item_id": "", "title": "", "price": "", "sold": "",
                         "shop": "", "url": u})
        list_src = "links"

    if not rows:
        raise SystemExit("❌ 列表为空：请确认 CDP Chrome 已登录淘宝，且店铺/关键词正确")

    print(f"📦 列表 {len(rows)} 条（{list_src}）")
    if not args.skip_details:
        fetch_details(rows, args.cdp, args.workers)

    # 组装最终行
    final = []
    for r in rows:
        final.append({
            "标题": r.get("标题_详情") or r.get("title") or "",
            "价格": r.get("价格_详情") or r.get("price") or "",
            "店铺": r.get("店铺_详情") or r.get("shop") or "",
            "商品ID": r.get("item_id") or "",
            "销量": r.get("sold") or "",
            "链接": r.get("url") or "",
            "产品参数": r.get("产品参数") or "",
        })
    for r in final:
        if r["商品ID"] and r["链接"].find("id=") < 0:
            r["链接"] += ("&" if "?" in r["链接"] else "?") + f"id={r['商品ID']}"

    out_path = args.out or str(ROOT / "outputs" / f"taobao_{args.mode}_{int(time.time())}.json")
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
    csv_path = str(Path(out_path).with_suffix(".csv"))
    try:
        import csv
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(final[0].keys()))
            w.writeheader()
            w.writerows(final)
    except Exception:
        csv_path = ""

    n_params = sum(1 for r in final if r["产品参数"])
    print(f"✅ 完成 {len(final)} 条，其中含产品参数 {n_params} 条")
    print(f"📄 JSON: {out_path}")
    if csv_path:
        print(f"📄 CSV : {csv_path}")
    for r in final[:5]:
        print(f"  - {r['标题'][:44]} | ¥{r['价格']} | 参数 {len(r['产品参数'])} 字符")

    if args.json:
        print("RESULT_JSON " + json.dumps({"rows": final, "file": out_path, "csv": csv_path,
                                            "ok": n_params, "total": len(final)},
                                           ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
