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

// 天猫/淘宝详情页通用参数选择器（多套兜底）
const ATTR_SELS = [
  ".Ptable .Ptable-item dl",        // 天猫新版参数表
  "#J_AttrUL li",                   // 天猫/淘宝属性列表
  ".attributes-list li",            // 淘宝参数列表
  ".tb-attr li",                    // 旧版
  "#attributes li",                 // 旧版
  ".tm-clear .tb-detail-attr li",   // 属性行
  ".J_AttrMore .tb-attr li",
];
const TITLE_SELS = ["h1.tb-main-title", ".tb-detail-hd h1", "#J_Title h3", ".tb-main-title"];
const PRICE_SELS = [".tm-price", ".tb-rmb-num", ".price", "#J_PromoPrice .tm-price", ".tb-detail-price .tm-price"];
const SHOP_SELS = [".slogo-shopname", ".shop-name a", ".tb-shop-name", ".shop-title a"];

function clean(s) { return (s || "").replace(/\s+/g, " ").trim(); }

async function main() {
  const args = parseArgs(process.argv);
  const cdp = args.cdp || "http://127.0.0.1:9222";
  const shop = args.shop || "";
  const maxItems = parseInt(args.max || "50", 10);
  if (!shop) { out({ type: "error", message: "缺少 --shop URL" }); process.exit(1); }

  let browser;
  try {
    browser = await chromium.connectOverCDP(cdp);
  } catch (e) {
    out({ type: "error", message: "无法连接 CDP " + cdp + "：" + (e.message||e) + "（请先双击「启动淘宝调试Chrome.command」）" });
    process.exit(1);
  }
  const ctx = browser.contexts()[0] || await browser.newContext();
  const page = ctx.pages()[0] || await ctx.newPage();

  out({ type: "meta", stage: "open_shop", shop });
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

  const bodyTxt = await page.evaluate(() => document.body ? document.body.innerText.slice(0, 120) : "");
  if (/拖动|滑块|验证/.test(bodyTxt)) {
    out({ type: "login", message: "店铺页要求滑块验证——请在 Chrome 里完成滑块（或先登录淘宝）后告诉我，我会继续" });
    process.exit(0);
  }

  const links = await page.evaluate(() => {
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
  out({ type: "meta", items_found: links.length, on_page: bodyTxt.slice(0, 60) });
  if (links.length === 0) {
    out({ type: "error", message: "店铺页未找到商品链接（可能未登录/页面结构特殊/需滑块）。页面：" + bodyTxt.slice(0,80) });
    process.exit(0);
  }

  const targets = links.slice(0, maxItems);
  out({ type: "meta", to_fetch: targets.length });
  let done = 0, fail = 0;
  for (const t of targets) {
    try {
      const np = await ctx.newPage();
      await np.goto(t.url, { timeout: 45000, waitUntil: "domcontentloaded" });
      await sleep(1500);
      const data = await np.evaluate((ATTR_SELS, TITLE_SELS, PRICE_SELS, SHOP_SELS) => {
        const q = (sels) => { for (const s of sels) { const e = document.querySelector(s); if (e) return e; } return null; };
        const title = q(TITLE_SELS) ? q(TITLE_SELS).innerText.trim().replace(/\s+/g," ") : "";
        const price = q(PRICE_SELS) ? q(PRICE_SELS).innerText.trim() : "";
        const shop = q(SHOP_SELS) ? q(SHOP_SELS).innerText.trim().replace(/\s+/g," ") : "";
        // 参数：拼接所有匹配
        const params = [];
        const seen = new Set();
        ATTR_SELS.forEach(s => {
          document.querySelectorAll(s).forEach(el => {
            const t = el.innerText.trim().replace(/\s+/g, " ");
            if (t && t.length < 120 && !seen.has(t)) { seen.add(t); params.push(t); }
          });
        });
        return { title, price, shop, params: params.slice(0, 60) };
      }, ATTR_SELS, TITLE_SELS, PRICE_SELS, SHOP_SELS);
      await np.close().catch(()=>{});
      out({ type: "item", ...data, url: t.url, list_title: t.title });
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
