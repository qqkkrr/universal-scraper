/* 实验：ByOrg + certItemTwo=A05 能否按项目过滤 */
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
    const setSel=(id,v)=>{const s=document.getElementById(id);if(!s)return;s.value=v;s.dispatchEvent(new Event("change",{bubbles:true}));};
    setSel("certItemTwo","A05"); setSel("country","156"); return undefined;
  });
  await sleep(5000);
  await page.evaluate(() => { const s3=document.getElementById("certItemThree"); if (s3 && [...s3.options].some(o=>o.value==="A0501")) { s3.value="A0501"; s3.dispatchEvent(new Event("change",{bubbles:true})); } return undefined; });
  await sleep(1000);
  await page.evaluate(() => { const b=[...document.querySelectorAll("button, .btn")]; const q=b.find(x=>(x.textContent||"").trim().includes("询")); if(q) q.click(); });
  const t0=Date.now();
  while (Date.now()-t0<25000 && verifyQ.length===0) await sleep(1000);
  const v = verifyQ[verifyQ.length-1];
  if (!v) { console.log("[FAIL] 无验证"); process.exit(1); }
  async function call(endpoint, params) {
    const q = new URLSearchParams({ ...params,
      lot_number:v.lot_number, pass_token:v.pass_token, gen_time:v.gen_time, captcha_output:v.captcha_output });
    return page.evaluate(async (arg) => {
      const { ep, qs } = arg;
      const r = await fetch(ep+"?"+qs, {headers:{"Accept":"application/json"}});
      try { return { status: r.status, data: await r.json() }; } catch(e){ return { status: r.status, raw: (await r.text()).slice(0,300) }; }
    }, { ep: endpoint, qs: q.toString() });
  }
  // 用利扬芯片测试：ByOrg 普通 + ByOrg 带 A05 过滤 + ByOrg 带 certStatus
  for (const params of [
    { orgName: "广东利扬芯片测试股份有限公司" },
    { orgName: "广东利扬芯片测试股份有限公司", certItemTwo: "A05" },
    { orgName: "广东利扬芯片测试股份有限公司", certItemTwo: "A05", certItemThree: "A0501" },
    { orgName: "广东利扬芯片测试股份有限公司", certItemTwo: "A05", certStatus: "01" },
  ]) {
    const r = await call("/CertECloud/result/listAuthresultByOrg", params);
    const rows = (r.data && r.data.rows) || [];
    const msg = r.data && r.data.obj && r.data.obj.info && r.data.obj.info.msg;
    console.log(JSON.stringify(params), "→ rows:", rows.length, "| msg:", msg, "| first:", JSON.stringify(rows[0]||{}).slice(0,200));
    await sleep(2000);
  }
  await page.close(); await browser.close();
})().catch(e=>{console.log("ERR",e.message.slice(0,400));process.exit(1);});
