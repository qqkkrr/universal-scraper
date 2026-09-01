#!/usr/bin/env node
const { loadChromium } = require("./browser_common.cjs");
(async () => {
  const chromium = loadChromium();
  const browser = await chromium.connectOverCDP("http://127.0.0.1:9222");
  let page = null;
  for (const c of browser.contexts()) for (const p of c.pages()) if (p.url().includes("cx.cnca.cn")) page = p;
  if (!page) { console.log("未找到 cx 页面"); process.exit(1); }
  const info = await page.evaluate(() => {
    const out = { url: location.href.slice(0, 90) };
    out.hookInjected = !!(window.__cx_captured && Array.isArray(window.__cx_captured));
    out.capturedCount = (window.__cx_captured || []).length;
    out.captured = (window.__cx_captured || []).slice(-5).map(c => ({ url: c.url.slice(0,120), body: c.body.slice(0,120) }));
    // 极验弹窗是否可见
    const gt = document.querySelector(".geetest_captcha, .geetest_panel, [class*=geetest_captcha]");
    out.geetestVisible = gt ? (getComputedStyle(gt).display !== "none" && gt.offsetParent !== null) : false;
    const ifr = [...document.querySelectorAll("iframe")].map(f => f.src || f.id);
    out.iframes = ifr.slice(0, 5);
    // 查询按钮
    const btns = [...document.querySelectorAll("button, .btn")].map(b => (b.textContent||"").trim().slice(0,10));
    out.buttons = btns.slice(0, 10);
    return out;
  });
  console.log(JSON.stringify(info, null, 1));
  await browser.close();
})().catch(e => { console.log("ERR", e.message.slice(0, 400)); process.exit(1); });
