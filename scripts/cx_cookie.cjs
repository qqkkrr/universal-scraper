#!/usr/bin/env node
const { loadChromium } = require("./browser_common.cjs");
(async () => {
  const chromium = loadChromium();
  const browser = await chromium.connectOverCDP("http://127.0.0.1:9222");
  const ctx = browser.contexts()[0];
  const cookies = await ctx.cookies();
  const cx = cookies.filter(c => c.domain.includes("cnca.cn") || c.name.includes("jsl"));
  console.log("cx 相关 cookie:");
  cx.forEach(c => console.log("  ", c.name, "=", c.value.slice(0, 40)));
  await browser.close();
})().catch(e=>{console.log("ERR",e.message.slice(0,300));process.exit(1);});
