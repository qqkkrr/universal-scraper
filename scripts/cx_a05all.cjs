/* 测 A05 大类（certItemThree 留空）总量 */
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
  await sleep(9000);
  await page.evaluate(() => {
    const setSel=(id,v)=>{const s=document.getElementById(id);if(!s)return;s.value=v;s.dispatchEvent(new Event("change",{bubbles:true}));};
    setSel("certItemTwo","A05"); setSel("country","156"); return undefined;
  });
  await sleep(6000);
  // certItemThree 留空
  await sleep(1000);
  await page.evaluate(() => { const b=[...document.querySelectorAll("button, .btn")]; const q=b.find(x=>(x.textContent||"").trim().includes("询")); if(q) q.click(); });
  const t0=Date.now();
  while (Date.now()-t0<25000 && verifyQ.length===0) await sleep(1000);
  const v = verifyQ[verifyQ.length-1];
  if (!v) { console.log("[FAIL] 无验证"); process.exit(1); }
  const q = new URLSearchParams({ certItemOne:"A",certItemTwo:"A05",country:"156",certStatus:"01",pageNum:"1",pageSize:"100",
    lot_number:v.lot_number,pass_token:v.pass_token,gen_time:v.gen_time,captcha_output:v.captcha_output });
  const j = await page.evaluate(async (qs) => {
    const r = await fetch("/CertECloud/result/listAuthresult?"+qs, {headers:{"Accept":"application/json"}});
    try { return await r.json(); } catch(e){ return {parse_err:String(e)}; }
  }, q.toString());
  console.log("[RESULT] A05(不带A0501) 有效: pageCount=", j.pageCount, "| total=", j.total, "| rows=", (j.rows||[]).length);
  if (j.rows && j.rows.length) {
    console.log("样例:", j.rows[0].authProjCode, "|", (j.rows[0].authProjName||"").slice(0,30), "|", (j.rows[0].orgName||"").slice(0,30));
  }
  await page.close(); await browser.close();
})().catch(e=>{console.log("ERR",e.message.slice(0,400));process.exit(1);});
