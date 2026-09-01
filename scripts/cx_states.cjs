#!/usr/bin/env node
/* 扩展口径：抓 A05 各证书状态（历史获证）全量，落盘 history_certs.jsonl */
const fs = require("node:fs");
const path = require("node:path");
const { loadChromium, sleep } = require("./browser_common.cjs");
const OUT_DIR = "/Users/kairanqin/Documents/Codex/2026-08-03/ni-shi/iso27001";
const HIST = path.join(OUT_DIR, "history_certs.jsonl");
const QUERY = { certItemOne:"A", certItemTwo:"A05", certItemThree:"A0501", country:"156" };
const PAGE_SIZE = 100, WAIT = 2500, MAX_RETRY = 3;

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
    await sleep(8000);
    await page.evaluate(() => {
      function setSel(id,v){const s=document.getElementById(id);if(!s)return;s.value=v;s.dispatchEvent(new Event("change",{bubbles:true}));}
      setSel("certItemTwo","A05"); setSel("country","156");
    });
    await sleep(5000);
    await page.evaluate(() => { const s3=document.getElementById("certItemThree"); if (s3 && [...s3.options].some(o=>o.value==="A0501")) { s3.value="A0501"; s3.dispatchEvent(new Event("change",{bubbles:true})); } });
    await sleep(1000);
  }
  async function triggerVerify() {
    const before = verifyQ.length;
    try {
      await page.evaluate(() => { const b=[...document.querySelectorAll("button, .btn")]; const q=b.find(x=>(x.textContent||"").trim().includes("询")); if(q) q.click(); });
    } catch(e) {}
    const t0=Date.now();
    while (Date.now()-t0<20000 && verifyQ.length<=before) await sleep(1000);
    return verifyQ.length>before ? verifyQ[verifyQ.length-1] : null;
  }
  async function api(v, pn, status) {
    const q = new URLSearchParams({ ...QUERY, ...(status?{certStatus:status}:{}), pageNum:String(pn), pageSize:String(PAGE_SIZE),
      lot_number:v.lot_number, pass_token:v.pass_token, gen_time:v.gen_time, captcha_output:v.captcha_output });
    return page.evaluate(async (qs) => {
      const r = await fetch("/CertECloud/result/listAuthresult?"+qs, {headers:{"Accept":"application/json"}});
      if (r.status===521) return {blocked:true};
      try { return await r.json(); } catch(e){ return {parse_err:String(e)}; }
    }, q.toString());
  }

  await ensurePage();
  // 状态列表：02暂停 03撤销 04注销 05过期失效 + 空(全部)
  const statuses = ["02","03","04","05",""];
  const all = [];
  fs.writeFileSync(HIST, "", "utf-8");
  for (const st of statuses) {
    let v = await triggerVerify();
    if (!v) { console.log(`[WARN] status=${st||'all'} 验证失败，刷新页面重试`); await ensurePage(); v = await triggerVerify(); if(!v){ console.log(`[WARN] status=${st||'all'} 跳过`); continue; } }
    let first = await api(v, 1, st);
    if (!first || first.pageCount === 0) {
      console.log(`[INFO] status=${st||'all'}: pageCount=0（无记录或参数失效），尝试重验`);
      v = await triggerVerify(); first = await api(v, 1, st);
    }
    const totalPages = first && first.pageCount ? first.pageCount : 0;
    if (!totalPages) { console.log(`[INFO] status=${st||'all'}: 0 条`); continue; }
    console.log(`[FETCH] status=${st||'all'}: ${totalPages} 页`);
    // 收集第一页
    if (first.rows) { all.push(...first.rows.map(r=>({...r,_certStatusQuery:st||"all"}))); fs.appendFileSync(HIST, first.rows.map(r=>JSON.stringify({...r,_certStatusQuery:st||"all"})).join("\n")+"\n", "utf-8"); }
    for (let pn = 2; pn <= totalPages; pn++) {
      v = await triggerVerify();
      if (!v) { console.log(`[WARN] p${pn} 验证失败`); continue; }
      let j = null;
      for (let r=0;r<MAX_RETRY&&!j;r++){ j=await api(v,pn,st); if(!j||j.parse_err){j=null;await sleep(5000);} }
      if (!j || !j.rows) { console.log(`[WARN] p${pn} 失败`); continue; }
      const rows=j.rows.map(r=>({...r,_certStatusQuery:st||"all"}));
      all.push(...rows);
      fs.appendFileSync(HIST, rows.map(r=>JSON.stringify(r)).join("\n")+"\n", "utf-8");
      console.log(`   status=${st||'all'} p${pn}/${totalPages}，累计 ${all.length}`);
      await sleep(WAIT);
    }
    await sleep(WAIT);
  }
  console.log(`[DONE] 历史+全部状态证书 ${all.length} 条 → ${HIST}`);
  await page.close(); await browser.close();
})().catch(e=>{console.log("ERR",e.message.slice(0,500));process.exit(1);});
