#!/usr/bin/env node
/* 验证→立即查询 page1，打印完整响应 */
const fs = require("node:fs");
const { loadChromium, sleep } = require("./browser_common.cjs");
(async () => {
  const chromium = loadChromium();
  const browser = await chromium.connectOverCDP("http://127.0.0.1:9222");
  let page = null;
  for (const c of browser.contexts()) for (const p of c.pages()) if (p.url().includes("cx.cnca.cn")) page = p;
  if (!page) { console.log("无页面"); process.exit(1); }
  const verifyQ = [];
  page.on("response", async (resp) => {
    try {
      const u = resp.url();
      if (u.includes("geetest.com/verify") || u.includes("gcaptcha4.com/verify")) {
        const t = await resp.text();
        const lot=(t.match(/"lot_number":"([^"]+)"/)||[])[1], pass=(t.match(/"pass_token":"([^"]+)"/)||[])[1];
        const gen=(t.match(/"gen_time":"?([0-9]+)"?/)||[])[1], cap=(t.match(/"captcha_output":"([^"]+)"/)||[])[1];
        if (lot&&pass&&gen&&cap) verifyQ.push({lot_number:lot,pass_token:pass,gen_time:gen,captcha_output:cap});
      }
    } catch(e) {}
  });
  await page.evaluate(() => { const b=[...document.querySelectorAll("button, .btn")]; const q=b.find(x=>(x.textContent||"").trim().includes("询")); if(q) q.click(); });
  const t0=Date.now();
  while (Date.now()-t0<20000 && verifyQ.length===0) await sleep(1000);
  if (!verifyQ.length) { console.log("[FAIL] 无 verify"); process.exit(1); }
  const v = verifyQ[verifyQ.length-1];
  console.log("[OK] 参数:", JSON.stringify({lot:v.lot_number.slice(0,12),gt:v.gen_time}));
  const q = new URLSearchParams({certItemOne:"A",certItemTwo:"A05",certItemThree:"A0501",country:"156",certStatus:"01",pageNum:"1",pageSize:"100",
    lot_number:v.lot_number,pass_token:v.pass_token,gen_time:v.gen_time,captcha_output:v.captcha_output});
  const j = await page.evaluate(async (qs) => {
    const r = await fetch("/CertECloud/result/listAuthresult?"+qs, {headers:{"Accept":"application/json"}});
    const t = await r.text();
    return { status: r.status, body: t.slice(0, 2000) };
  }, q.toString());
  console.log("[RESP] status:", j.status);
  console.log(j.body.slice(0, 1500));
  await browser.close();
})().catch(e=>{console.log("ERR",e.message.slice(0,400));process.exit(1);});
