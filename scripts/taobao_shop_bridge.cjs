#!/usr/bin/env node
/**
 * 淘宝/天猫旗舰店精配桥（CDP 模式）
 *
 * 为什么不用 AI 猜配置：淘宝/天猫 = 登录 + 滑块 + 全 JS 接口，选择器必须基于
 * 真实登录态实抓。本桥附着用户自己已登录的 Chrome（CDP 9222）：
 *   1. 打开店铺首页/商品列表页
 *   2. 收集所有商品链接（a[href*='item.htm']，去重）
 *   3. 逐个打开商品详情页，抓标题/价格/产品参数（天猫通用结构）
 *   4. JSONL 流式输出
 *
 * 用法:
 *   node taobao_shop_bridge.cjs --cdp http://127.0.0.1:9222 --shop <URL> [--max 50]
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

// 详情页参数：优先点「参数信息」后抓文本段；兼容老版选择器
async function fetchDetail(page, url) {
  await page.goto(url, { timeout: 45000, waitUntil: "domcontentloaded" }).catch(e => {
    if (!/ERR_ABORTED|Timeout|net::/.test(String(e && e.message || e))) throw e;
  });
  await sleep(4000);
  // 点「参数信息」/「全部参数」（可能有多个，全部点，展开参数表）
  await page.evaluate(() => {
    const els = [...document.querySelectorAll("a, span, div, li, button")];
    els.filter(x => /^参数信息$|^全部参数$/.test((x.innerText||"").trim()) && x.children.length <= 1)
       .slice(0, 4).forEach(e => { try { e.click(); } catch(err){} });
  }).catch(()=>{});
  await sleep(3500);
  const data = await page.evaluate(() => {
    const clean = (s) => (s||"").replace(/\s+/g, " ").trim();
    const t = document.body.innerText.replace(/\s+/g, " ");
    const title = clean(document.querySelector("h1, .tb-detail-hd h1, .tb-main-title") ? (document.querySelector("h1, .tb-detail-hd h1, .tb-main-title").innerText) : "");
    // 参数区在「参数信息」最后一次出现之后（前面出现的是 tab 标签）
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
      // 兜底：找参数关键词段
      const m = t.match(/(材质成分|是否商场同款|适用场景|品牌|货号)[\s\S]{0,600}/);
      if (m) params = clean(m[0]).slice(0, 1800);
    }
    return { title: title.slice(0, 120), params };
  });
  return data;
}
const TITLE_SELS = ["h1.tb-main-title", ".tb-detail-hd h1", "#J_Title h3", ".tb-main-title"];
const PRICE_SELS = [".tm-price", ".tb-rmb-num", ".price", "#J_PromoPrice .tm-price", ".tb-detail-price .tm-price"];
const SHOP_SELS = [".slogo-shopname", ".shop-name a", ".tb-shop-name", ".shop-title a"];

function clean(s) { return (s || "").replace(/\s+/g, " ").trim(); }

async function main() {
  const args = parseArgs(process.argv);
  const cdp = args.cdp || "http://127.0.0.1:9222";
  const shop = args.shop || "";
  const linksArg = (args.links || "").split(",").map(s => s.trim()).filter(Boolean);
  const maxItems = parseInt(args.max || "50", 10);
  if (!shop && linksArg.length === 0) { out({ type: "error", message: "缺少 --shop URL 或 --links 商品链接列表" }); process.exit(1); }

  let browser;
  try {
    browser = await chromium.connectOverCDP(cdp);
  } catch (e) {
    out({ type: "error", message: "无法连接 CDP " + cdp + "：" + (e.message||e) + "（请先双击「启动淘宝调试Chrome.command」）" });
    process.exit(1);
  }
  const ctx = browser.contexts()[0] || await browser.newContext();
  const page = ctx.pages()[0] || await ctx.newPage();

  let links = linksArg.map(u => ({ url: u.startsWith("//") ? "https:" + u : u, title: "" }));
  let bodyTxt = "";
  out({ type: "meta", stage: linksArg.length ? "direct_links" : "open_shop", shop, direct: linksArg.length });
  if (linksArg.length === 0) {
    try {
      await page.goto(shop, { timeout: 60000, waitUntil: "domcontentloaded" });
    } catch (e) {
      out({ type: "error", message: "打开店铺页失败：" + (e.message||e).slice(0,150) });
      process.exit(1);
    }
    // 等商品出现（滚动几次触发懒加载）
    await sleep(2500);
    for (let i = 0; i < 5; i++) {
      await page.mouse.wheel(0, 1500).catch(()=>{});
      await sleep(900);
    }
    await sleep(1500);

    bodyTxt = await page.evaluate(() => document.body ? document.body.innerText.slice(0, 120) : "");
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
      return arr.slice(0, 200);
    });
    if (collected.length) links = collected;
  }
  out({ type: "meta", items_found: links.length, on_page: bodyTxt.slice(0, 60) });
  if (links.length === 0) {
    out({ type: "error", message: "未拿到商品链接（新版天猫店铺商品卡片无常规链接；请改用 --links 提供商品链接，或用搜索页/详情页）。页面：" + bodyTxt.slice(0,80) });
    process.exit(0);
  }

  const targets = links.slice(0, maxItems);
  out({ type: "meta", to_fetch: targets.length });
  let done = 0, fail = 0;
  for (const t of targets) {
    try {
      const np = await ctx.newPage();
      const data = await fetchDetail(np, t.url);
      await np.close().catch(()=>{});
      // 参数保持完整段落（不再按字切碎），Python 侧再整理
      out({ type: "item", title: data.title, price: "", shop: "", url: t.url, list_title: t.title, params: data.params ? [data.params] : [] });
      done++;
    } catch (e) {
      fail++;
      out({ type: "item_fail", url: t.url, error: (e.message||e).slice(0,100) });
    }
    if (done + fail >= targets.length) break;
    await sleep(600);
  }
  out({ type: "done", ok: done, fail });
  await browser.close().catch(()=>{});
  process.exit(0);
}

main().catch(e => { out({ type: "error", message: String(e && e.message || e) }); process.exit(1); });
