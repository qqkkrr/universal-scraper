#!/usr/bin/env node
const { loadChromium, sleep } = require("./browser_common.cjs");
(async () => {
  const chromium = loadChromium();
  const browser = await chromium.connectOverCDP("http://127.0.0.1:9222");
  let page = null;
  for (const c of browser.contexts()) for (const p of c.pages()) if (p.url().includes("cx.cnca.cn")) page = p;
  if (!page) { console.log("无页面"); process.exit(1); }
  await sleep(3000);
  const r = await page.evaluate(() => {
    const s3 = document.getElementById("certItemThree");
    const opts = s3 ? [...s3.options].map(o=>o.value) : [];
    if (s3 && opts.includes("A0501")) { s3.value="A0501"; s3.dispatchEvent(new Event("change",{bubbles:true})); return {opts, set:"A0501"}; }
    return {opts, set:"none"};
  });
  console.log(JSON.stringify(r));
  await browser.close();
})().catch(e=>{console.log("ERR",e.message.slice(0,300));process.exit(1);});
