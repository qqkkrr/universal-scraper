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
  let navErr = null;
  await page.goto(url, { timeout: 45000, waitUntil: "domcontentloaded" }).catch(e => {
    // 审查八轮（M）：net:: DNS/连接类错误曾被一并吞掉 → parseDetail 全空仍计
    // 成功（done++，整批断网时输出 ok=N/fail=0 的全空数据）。只容忍 ABORTED
    // （页面 JS 中断导航但可能已渲染）与 Timeout（重试解析兜底）；其余记下，
    // 若解析三轮全空则如实上抛走 item_fail
    if (/ERR_ABORTED|Timeout/.test(String(e && e.message || e))) navErr = e;
    else throw e;
  });
  await sleep(3000);
  let data = await parseDetail(page);
  const waits = [2000, 3000, 4000];
  for (const w of waits) {
    if (data.params) break;
    await sleep(w);
    data = await parseDetail(page);
  }
  if (navErr && !data.title && !data.params && !data.price) throw navErr;
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
    // 审查八轮（M）：曾在此 process.exit(0)——跳过所有清理。改为返回标记，
    // 由 main 统一关自建页 + disconnect 后退出（exit 不执行 finally）
    return { links: [], bodyTxt, slider: true };
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
  // 审查修复（H）：parseInt(垃圾) = NaN 曾静默穿透——maxItems=NaN 使分页逻辑
  // 全部短路（每次都"已达上限"或永不到上限），NaN workers 被 clamp 成 1
  const _pint = (v, dft) => { const n = parseInt(v, 10); return Number.isFinite(n) && n > 0 ? n : dft; };
  const maxItems = _pint(args.max, 200);
  const workers = Math.max(1, Math.min(_pint(args.workers, 2), 4));
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
    // 审查八轮（H）：曾复用 ctx.pages()[0]——CDP 附着的是用户自己的 Chrome，
    // 用户正在用的标签页被直接导航去店铺页（登录态/表单现场被毁）。
    // captcha_bridge 实证纪律：CDP 模式必须专用 newPage
    const page = await ctx.newPage();
    let r;
    try {
      r = await collectShopLinks(page, shop);
    } finally {
      await page.close().catch(() => {});
    }
    const { links: got, bodyTxt } = r;
    out({ type: "meta", on_page: bodyTxt.slice(0, 60) });
    if (r.slider) {
      out({ type: "login", message: "店铺页要求滑块验证——请在 Chrome 里完成滑块（或先登录淘宝）后告诉我，我会继续" });
      await browser.disconnect().catch(() => {});
      process.exit(0);
    }
    if (got.length) links = got;
    out({ type: "meta", items_found: links.length });
    if (!links.length) {
      out({ type: "error", message: "未拿到商品链接（新版天猫店铺商品卡片无常规链接；请改用 Python 引擎 --mode shop 走移动端接口，或用 --links 提供商品链接）。页面：" + bodyTxt.slice(0,80) });
      await browser.disconnect().catch(() => {});
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
  // 审查修复（CRITICAL）：connectOverCDP 连的是用户自己的 Chrome——disconnect()
  // 只断开自动化连接。澄清（审查八轮，playwright/patchright 1.63 源码核实）：
  // CDP 附着下 close() 现版本也只断连不杀浏览器（历史版本行为不同），但
  // disconnect 语义明确且不依赖版本行为，保留
  await browser.disconnect().catch(()=>{});
  process.exit(0);
}

main().catch(e => { out({ type: "error", message: String(e && e.message || e) }); process.exit(1); });
