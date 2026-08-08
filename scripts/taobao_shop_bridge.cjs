#!/usr/bin/env node
/**
 * 淘宝/天猫精配桥（CDP 模式 · 快速版）
 *
 * 要点（基于实测优化）：
 *   - 商品详情页的「参数信息」在页面加载后即已存在于 DOM innerText，无需点击展开
 *     → 免点击，单商品从 ~10s 降到 ~4-6s
 *   - 复用专用标签页（不再每商品新建/关闭标签），支持 --workers 并行
 *   - 店铺商品列表优先走 Python 引擎的移动端接口；本桥专注详情参数
 *
 * 用法:
 *   node taobao_shop_bridge.cjs --cdp http://127.0.0.1:9222 --links u1,u2 [--workers 2] [--max 200]
 *   node taobao_shop_bridge.cjs --cdp http://127.0.0.1:9222 --shop <URL> [--workers 2] [--max 50]
 */
const path = require("node:path");
const nps = (process.env.NODE_PATH || "").split(":").filter(Boolean);
const cands = nps.map(p => path.join(p, "playwright")).concat(["patchright", "playwright"]);
let chromium = null;
for (const c of cands) { try { chromium = require(c).chromium; break; } catch(e){} }
if (!chromium) throw new Error("找不到 playwright/patchright");
const out = (obj) => console.log(JSON.stringify(obj));
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

function parseArgs(argv) {
  const a = {};
  for (let i = 2; i < argv.length; i++) {
    const k = argv[i];
    if (k.startsWith("--")) a[k.slice(2)] = argv[i + 1];
  }
  return a;
}

// 详情页解析（免点击：加载后「参数信息」已在 DOM）
async function parseDetail(page) {
  return await page.evaluate(() => {
    const clean = (s) => (s || "").replace(/\s+/g, " ").trim();
    const t = document.body ? document.body.innerText.replace(/\s+/g, " ") : "";
    const g = (sels) => { for (const s of sels) { const el = document.querySelector(s); if (el && el.innerText && el.innerText.trim()) return clean(el.innerText); } return ""; };
    const title = g(["h1", ".tb-detail-hd h1", ".tb-main-title", "#J_Title h3"]) || "";
    const price = g([".tm-price", ".tb-rmb-num", "#J_PromoPrice .tm-price", ".tb-detail-price .tm-price"]) || "";
    const shop = g([".slogo-shopname", ".shop-name a", ".tb-shop-name", ".shop-title a"]) || "";
    // 参数区：最后一次「参数信息」之后 → 尺码信息/图文详情/用户评价
    let params = "";
    const i = t.lastIndexOf("参数信息");
    if (i >= 0) {
      let j = t.indexOf("尺码信息", i + 4);
      if (j < 0) j = t.indexOf("图文详情", i + 4);
      if (j < 0) j = t.indexOf("用户评价", i + 4);
      if (j < 0) j = i + 1800;
      params = clean(t.slice(i + 4, j)).slice(0, 1800);
    }
    if (!params) {
      const m = t.match(/(材质成分|是否商场同款|适用场景|品牌|货号)[\s\S]{0,600}/);
      if (m) params = clean(m[0]).slice(0, 1800);
    }
    return { title: title.slice(0, 120), price: price.slice(0, 60), shop: shop.slice(0, 80), params };
  }).catch(() => ({ title: "", price: "", shop: "", params: "" }));
}

// 详情页：导航后等首屏渲染（item.taobao.com 会 302 到 detail.tmall.com），
// 最多重试 3 次解析（并行多标签时部分页面渲染慢）。
async function fetchDetail(page, url) {
  await page.goto(url, { timeout: 45000, waitUntil: "domcontentloaded" }).catch(e => {
    if (!/ERR_ABORTED|Timeout|net::/.test(String(e && e.message || e))) throw e;
  });
  await sleep(3000);
  let data = await parseDetail(page);
  const waits = [2000, 3000, 4000];
  for (const w of waits) {
    if (data.params) break;
    await sleep(w);
    data = await parseDetail(page);
  }
  return data;
}

async function collectShopLinks(page, shop) {
  await page.goto(shop, { timeout: 60000, waitUntil: "domcontentloaded" }).catch(e => {
    if (!/ERR_ABORTED|Timeout|net::/.test(String(e && e.message || e))) throw e;
  });
  await sleep(2500);
  for (let i = 0; i < 5; i++) {
    await page.mouse.wheel(0, 1500).catch(()=>{});
    await sleep(700);
  }
  await sleep(1000);
  const bodyTxt = await page.evaluate(() => document.body ? document.body.innerText.slice(0, 120) : "");
  if (/拖动|滑块|验证/.test(bodyTxt)) {
    out({ type: "login", message: "店铺页要求滑块验证——请在 Chrome 里完成滑块（或先登录淘宝）后告诉我，我会继续" });
    process.exit(0);
  }
  const collected = await page.evaluate(() => {
    const clean = (s) => (s || "").replace(/\s+/g, " ").trim();
    const seen = new Set(); const arr = [];
    document.querySelectorAll("a[href*='item.htm'], a[href*='/item/']").forEach(a => {
      let h = a.getAttribute("href") || "";
      if (h.startsWith("//")) h = "https:" + h;
      if (!/item\.htm/i.test(h)) return;
      if (!/^https?:\/\//.test(h)) return;
      const idm = h.match(/[?&]id=(\d+)/);
      const key = idm ? idm[1] : h;
      if (seen.has(key)) return;
      seen.add(key);
      const t = clean(a.innerText);
      if (t) arr.push({ url: h, title: t.slice(0, 80) });
    });
    return arr.slice(0, 300);
  });
  return { links: collected, bodyTxt };
}

async function main() {
  const args = parseArgs(process.argv);
  const cdp = args.cdp || "http://127.0.0.1:9222";
  const shop = args.shop || "";
  const linksArg = (args.links || "").split(",").map(s => s.trim()).filter(Boolean);
  const maxItems = parseInt(args.max || "200", 10);
  const workers = Math.max(1, Math.min(parseInt(args.workers || "2", 10), 4));
  if (!shop && linksArg.length === 0) { out({ type: "error", message: "缺少 --shop URL 或 --links 商品链接列表" }); process.exit(1); }

  let browser;
  try {
    browser = await chromium.connectOverCDP(cdp);
  } catch (e) {
    out({ type: "error", message: "无法连接 CDP " + cdp + "：" + (e.message||e) + "（请先双击「启动淘宝调试Chrome.command」）" });
    process.exit(1);
  }
  const ctx = browser.contexts()[0] || await browser.newContext();

  let links = linksArg.map(u => ({ url: u.startsWith("//") ? "https:" + u : u, title: "" }));
  if (linksArg.length === 0) {
    out({ type: "meta", stage: "open_shop", shop });
    const page = ctx.pages()[0] || await ctx.newPage();
    const { links: got, bodyTxt } = await collectShopLinks(page, shop);
    out({ type: "meta", on_page: bodyTxt.slice(0, 60) });
    if (got.length) links = got;
    out({ type: "meta", items_found: links.length });
    if (!links.length) {
      out({ type: "error", message: "未拿到商品链接（新版天猫店铺商品卡片无常规链接；请改用 Python 引擎 --mode shop 走移动端接口，或用 --links 提供商品链接）。页面：" + bodyTxt.slice(0,80) });
      process.exit(0);
    }
  } else {
    out({ type: "meta", stage: "direct_links", direct: linksArg.length });
  }

  const targets = links.slice(0, maxItems);
  out({ type: "meta", to_fetch: targets.length, workers });
  let done = 0, fail = 0, idx = 0;
  const results = new Array(targets.length);

  async function worker() {
    const page = await ctx.newPage();
    try {
      while (true) {
        const i = idx++;
        if (i >= targets.length) break;
        const t = targets[i];
        try {
          const data = await fetchDetail(page, t.url);
          results[i] = { type: "item", title: data.title, price: data.price, shop: data.shop, url: t.url, list_title: t.title, params: data.params ? [data.params] : [] };
          done++;
          out({ type: "item", title: data.title, price: data.price, shop: data.shop, url: t.url, list_title: t.title, params: data.params ? [data.params] : [] });
        } catch (e) {
          fail++;
          results[i] = { type: "item_fail", url: t.url, error: (e.message||e).slice(0,100) };
          out({ type: "item_fail", url: t.url, error: (e.message||e).slice(0,100) });
        }
        await sleep(400);
      }
    } finally {
      await page.close().catch(()=>{});
    }
  }

  await Promise.all(Array.from({ length: workers }, () => worker()));
  out({ type: "done", ok: done, fail });
  await browser.close().catch(()=>{});
  process.exit(0);
}

main().catch(e => { out({ type: "error", message: String(e && e.message || e) }); process.exit(1); });
