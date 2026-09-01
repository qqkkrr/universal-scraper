const fs = require("node:fs");
const { loadChromium } = require("./browser_common.cjs");
(async () => {
  const chromium = loadChromium();
  const browser = await chromium.connectOverCDP("http://127.0.0.1:9222");
  const ctx = browser.contexts()[0];
  const page = await ctx.newPage();
  await page.goto("https://cx.cnca.cn/CertECloud/result/skipResultList?certItemOne=A", { waitUntil: "domcontentloaded", timeout: 60000 });
  await new Promise(r=>setTimeout(r,7000));
  const js = await page.evaluate(async () => {
    const r = await fetch("/CertECloud/resourses/js/result/cx_result_list.js?t=20200926");
    return await r.text();
  });
  fs.writeFileSync("/tmp/cx_result_list.js", js, "utf-8");
  console.log("JS 长度:", js.length);
  // 找 ForOrg 相关
  const idx = js.indexOf("listAuthresultForOrg");
  if (idx >= 0) {
    console.log("=== listAuthresultForOrg 上下文 ===");
    console.log(js.slice(Math.max(0,idx-500), idx+800));
  } else {
    console.log("未找到 listAuthresultForOrg，搜 ByOrg:");
    const i2 = js.indexOf("listAuthresultByOrg");
    console.log(js.slice(Math.max(0,i2-300), i2+600));
  }
  await browser.close();
})().catch(e=>{console.log("ERR",e.message.slice(0,300));process.exit(1);});
