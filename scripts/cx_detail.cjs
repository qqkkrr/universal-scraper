#!/usr/bin/env node
const { loadChromium, sleep } = require("./browser_common.cjs");
(async () => {
  const chromium = loadChromium();
  const browser = await chromium.connectOverCDP("http://127.0.0.1:9222");
  const ctx = browser.contexts()[0];
  const page = await ctx.newPage();
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
  await page.goto("https://cx.cnca.cn/CertECloud/result/skipResultList?certItemOne=A", { waitUntil: "domcontentloaded", timeout: 60000 });
  await sleep(8000);
  await page.evaluate(() => {
    function setSel(id, v) { const s=document.getElementById(id); if(!s) return; s.value=v; s.dispatchEvent(new Event("change",{bubbles:true})); }
    setSel("certItemTwo","A05"); setSel("country","156"); setSel("certStatus","01");
  });
  await sleep(5000);
  await page.evaluate(() => { const s3=document.getElementById("certItemThree"); if (s3 && [...s3.options].some(o=>o.value==="A0501")) { s3.value="A0501"; s3.dispatchEvent(new Event("change",{bubbles:true})); } });
  await sleep(1000);
  await page.evaluate(() => { const b=[...document.querySelectorAll("button, .btn")]; const q=b.find(x=>(x.textContent||"").trim().includes("询")); if(q) q.click(); });
  const t0=Date.now();
  while (Date.now()-t0<25000 && verifyQ.length===0) await sleep(1000);
  const v = verifyQ[verifyQ.length-1];
  if (!v) { console.log("[FAIL] 无验证"); process.exit(1); }
  // 试多个明细接口
  const cert = "01126IS00025R101";
  const endpoints = [
    "/CertECloud/result/listAuthresultDetail?certNumber=" + cert,
    "/CertECloud/result/certDetail?certNumber=" + cert,
    "/CertECloud/result/getCertDetail?certNumber=" + cert,
  ];
  for (const ep of endpoints) {
    const q = ep + `&lot_number=${v.lot_number}&pass_token=${v.pass_token}&gen_time=${v.gen_time}&captcha_output=${v.captcha_output}`;
    const r = await page.evaluate(async (qs) => {
      const rr = await fetch(qs, {headers:{"Accept":"application/json"}});
      const t = await rr.text();
      return { status: rr.status, body: t.slice(0, 800) };
    }, q);
    console.log("[EP]", ep.split("?")[0], "→", r.status, "|", r.body.slice(0,200).replace(/\n/g," "));
  }
  await page.close(); await browser.close();
})().catch(e=>{console.log("ERR",e.message.slice(0,400));process.exit(1);});
