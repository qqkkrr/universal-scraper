/* 收集 cx 页面 JS，找 listAuthresultForOrg 调用方式 */
const { loadChromium } = require("./browser_common.cjs");
(async () => {
  const chromium = loadChromium();
  const browser = await chromium.connectOverCDP("http://127.0.0.1:9222");
  const ctx = browser.contexts()[0];
  const page = await ctx.newPage();
  await page.goto("https://cx.cnca.cn/CertECloud/result/skipResultList?certItemOne=A", { waitUntil: "domcontentloaded", timeout: 60000 });
  await new Promise(r=>setTimeout(r,8000));
  const js = await page.evaluate(() => {
    return [...document.querySelectorAll("script[src]")].map(s=>s.src).filter(u=>u.includes("cx.cnca.cn"));
  });
  console.log("页面 JS:");
  js.forEach(u=>console.log(" ", u));
  await browser.close();
})().catch(e=>{console.log("ERR",e.message.slice(0,300));process.exit(1);});
