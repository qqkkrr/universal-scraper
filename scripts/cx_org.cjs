#!/usr/bin/env node
/* 按组织查询证书（listAuthresultByOrg + listAuthresultForOrg），核对标准 */
const fs = require("node:fs");
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
  if (!verifyQ.length) { console.log("[FAIL] 验证未触发"); process.exit(1); }
  const v = verifyQ[verifyQ.length-1];
  console.log("[OK] 验证参数:", v.lot_number.slice(0,12));
  // 按组织查 orgId
  const orgName = encodeURIComponent("广东利扬芯片测试股份有限公司");
  const q1 = `orgName=${orgName}&lot_number=${v.lot_number}&pass_token=${v.pass_token}&gen_time=${v.gen_time}&captcha_output=${v.captcha_output}`;
  const r1 = await page.evaluate(async (qs) => {
    const r = await fetch("/CertECloud/result/listAuthresultByOrg?"+qs, {headers:{"Accept":"application/json"}});
    try { return { status: r.status, data: await r.json() }; } catch(e){ return { status: r.status, raw: (await r.text()).slice(0,500) }; }
  }, q1);
  console.log("[BYORG]", JSON.stringify(r1).slice(0, 1200));
  // 按 orgId 取证书
  const orgId = (r1.data && r1.data.rows && r1.data.rows[0] && r1.data.rows[0].orgId) || (r1.data && r1.data.obj && r1.data.obj.orgId) || "";
  if (orgId) {
    // 需要新验证
    const before = verifyQ.length;
    await page.evaluate(() => { const b=[...document.querySelectorAll("button, .btn")]; const q=b.find(x=>(x.textContent||"").trim().includes("询")); if(q) q.click(); });
    const t1=Date.now();
    while (Date.now()-t1<20000 && verifyQ.length<=before) await sleep(1000);
    const v2 = verifyQ[verifyQ.length-1];
    if (v2) {
      const q2 = `orgId=${orgId}&lot_number=${v2.lot_number}&pass_token=${v2.pass_token}&gen_time=${v2.gen_time}&captcha_output=${v2.captcha_output}`;
      const r2 = await page.evaluate(async (qs) => {
        const r = await fetch("/CertECloud/result/listAuthresultForOrg?"+qs, {headers:{"Accept":"application/json"}});
        try { return { status: r.status, data: await r.json() }; } catch(e){ return { status: r.status, raw: (await r.text()).slice(0,500) }; }
      }, q2);
      console.log("[FORORG]", JSON.stringify(r2).slice(0, 2000));
    }
  }
  await page.close(); await browser.close();
})().catch(e=>{console.log("ERR",e.message.slice(0,500));process.exit(1);});
