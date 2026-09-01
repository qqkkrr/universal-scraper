#!/usr/bin/env node
/* 补抓 A05 状态 02/03/04（单参数模式，每状态独立验证+刷新防卡） */
const fs = require("node:fs");
const path = require("node:path");
const { loadChromium, sleep } = require("./browser_common.cjs");
const OUT_DIR = "/Users/kairanqin/Documents/Codex/2026-08-03/ni-shi/iso27001";
const HIST = path.join(OUT_DIR, "history_certs.jsonl");
const QUERY = { certItemOne:"A", certItemTwo:"A05", certItemThree:"A0501", country:"156" };
const PAGE_SIZE = 100, WAIT = 2500;

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
  async function ensurePage() {
    await page.goto("https://cx.cnca.cn/CertECloud/result/skipResultList?certItemOne=A", { waitUntil: "domcontentloaded", timeout: 60000 });
    await sleep(10000);
    await page.evaluate(() => {
      const setSel=(id,v)=>{const s=document.getElementById(id);if(!s)return;s.value=v;s.dispatchEvent(new Event("change",{bubbles:true}));};
      setSel("certItemTwo","A05"); setSel("country","156"); return undefined;
    });
    await sleep(6000);
    await page.evaluate(() => { const s3=document.getElementById("certItemThree"); if (s3 && [...s3.options].some(o=>o.value==="A0501")) { s3.value="A0501"; s3.dispatchEvent(new Event("change",{bubbles:true})); } return undefined; });
    await sleep(1000);
  }
  async function triggerVerify() {
    const before = verifyQ.length;
    try { await page.evaluate(() => { const b=[...document.querySelectorAll("button, .btn")]; const q=b.find(x=>(x.textContent||"").trim().includes("询")); if(q) q.click(); }); } catch(e) {}
    const t0=Date.now();
    while (Date.now()-t0<20000 && verifyQ.length<=before) await sleep(1000);
    return verifyQ.length>before ? verifyQ[verifyQ.length-1] : null;
  }
  async function api(v, pn, status) {
    const q = new URLSearchParams({ ...QUERY, ...(status?{certStatus:status}:{}), pageNum:String(pn), pageSize:String(PAGE_SIZE),
      lot_number:v.lot_number, pass_token:v.pass_token, gen_time:v.gen_time, captcha_output:v.captcha_output });
    return page.evaluate(async (arg) => {
      const r = await fetch("/CertECloud/result/listAuthresult?"+arg, {headers:{"Accept":"application/json"}});
      if (r.status===521) return {blocked:true};
      try { return await r.json(); } catch(e){ return {parse_err:String(e)}; }
    }, q.toString());
  }

  await ensurePage();
  const statuses = ["02","03","04"];
  for (const st of statuses) {
    let v = await triggerVerify();
    if (!v) { console.log(`[RESET] status=${st} 验证失败，刷新`); await ensurePage(); v = await triggerVerify(); }
    if (!v) { console.log(`[WARN] status=${st} 跳过`); continue; }
    let first = await api(v, 1, st);
    if (!first || first.pageCount === 0) { console.log(`[INFO] status=${st}: 0 条`); continue; }
    const totalPages = first.pageCount || 0;
    console.log(`[FETCH] status=${st}: ${totalPages} 页`);
    const rows0 = first.rows || [];
    fs.appendFileSync(HIST, rows0.map(r=>JSON.stringify({...r,_certStatusQuery:st})).join("\n")+"\n", "utf-8");
    for (let pn=2; pn<=totalPages; pn++) {
      v = await triggerVerify();
      if (!v) { console.log(`[WARN] p${pn} 验证失败`); continue; }
      let j = await api(v, pn, st);
      if (!j || !j.rows) { console.log(`[WARN] p${pn} 失败`); continue; }
      fs.appendFileSync(HIST, j.rows.map(r=>JSON.stringify({...r,_certStatusQuery:st})).join("\n")+"\n", "utf-8");
      console.log(`   p${pn}/${totalPages}`);
      await sleep(WAIT);
    }
    await sleep(WAIT);
  }
  console.log("[DONE] 02/03/04 补充完成");
  await page.close(); await browser.close();
})().catch(e=>{console.log("ERR",e.message.slice(0,400));process.exit(1);});
