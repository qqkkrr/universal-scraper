#!/usr/bin/env node
const { loadChromium } = require("./browser_common.cjs");
(async () => {
  const chromium = loadChromium();
  const browser = await chromium.connectOverCDP("http://127.0.0.1:9222");
  let page=null;
  for (const c of browser.contexts()) for (const p of c.pages()) if (p.url().includes("cx.cnca.cn")) page=p;
  if (!page) { console.log("无页面"); process.exit(1); }
  const arr = await page.evaluate(() => window.__cx_captured || []);
  console.log("捕获", arr.length, "条:");
  arr.forEach((c,i) => console.log(`[${i}]`, c.url.slice(0,350), "|", c.body.slice(0,100)));
  await browser.close();
})().catch(e=>{console.log("ERR",e.message.slice(0,300));process.exit(1);});
