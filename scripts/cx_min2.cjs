const { loadChromium, sleep } = require("./browser_common.cjs");
(async () => {
  const chromium = loadChromium();
  const browser = await chromium.connectOverCDP("http://127.0.0.1:9222");
  const ctx = browser.contexts()[0];
  const page = await ctx.newPage();
  const r1 = await page.evaluate(async (a, b) => { return { a, b, ok: true }; }, "x", "y").catch(e=>"ERR:"+e.message);
  console.log("multi-arg async:", JSON.stringify(r1));
  await browser.close();
})().catch(e=>{console.log("FATAL",e.message.slice(0,300));process.exit(1);});
