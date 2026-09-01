#!/usr/bin/env node
/* 逐家 ByOrg（快速版）：每家企业一次验证+查询，每 12 家换新 tab 防卡 */
const fs = require("node:fs");
const path = require("node:path");
const { loadChromium, sleep } = require("./browser_common.cjs");
const OUT_DIR = "/Users/kairanqin/Documents/Codex/2026-08-03/ni-shi/iso27001";
const ORGLOG = path.join(OUT_DIR, "org_check.jsonl");

(async () => {
  const lines = fs.readFileSync(path.join(OUT_DIR,"firms_151_fullname.csv"), "utf-8").split("\n");
  const firms = [];
  for (let i=1;i<lines.length;i++) { const c=lines[i].split(","); if (c.length>=4 && c[2]) firms.push({ Stkcd: c[1].trim(), ShortName: c[2].trim(), FullName: c[3].trim() }); }
  // 跳过已查的（org_check.jsonl 里已有）
  const done = new Set();
  if (fs.existsSync(ORGLOG)) for (const l of fs.readFileSync(ORGLOG,"utf-8").split("\n")) { try { done.add(JSON.parse(l).Stkcd); } catch(e){} }
  const todo = firms.filter(f => !done.has(f.Stkcd));
  console.log("[0] 待核对:", todo.length, "（已完成", done.size, "）");

  const chromium = loadChromium();
  const browser = await chromium.connectOverCDP("http://127.0.0.1:9222");
  const ctx = browser.contexts()[0];
  let page = await ctx.newPage();
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
  async function newTab() {
    try { await page.close(); } catch(e) {}
    verifyQ.length = 0;
    page = await ctx.newPage();
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
  async function call(v, endpoint, params) {
    const q = new URLSearchParams({ ...params,
      lot_number:v.lot_number, pass_token:v.pass_token, gen_time:v.gen_time, captcha_output:v.captcha_output });
    return page.evaluate(async (arg) => {
      const { ep, qs } = arg;
      const r = await fetch(ep+"?"+qs, {headers:{"Accept":"application/json"}});
      if (r.status===521) return {blocked:true};
      try { return { status: r.status, data: await r.json() }; } catch(e){ return { status: r.status, raw: (await r.text()).slice(0,200) }; }
    }, { ep: endpoint, qs: q.toString() });
  }

  await newTab();
  let hasRec = 0, fail = 0;
  for (let i=0;i<todo.length;i++) {
    const fm = todo[i];
    if (i>0 && i%12===0) { console.log(`[TAB] 换新标签页（第${i}家）`); await newTab(); }
    let v = await triggerVerify();
    if (!v) { console.log(`[WARN] ${i+1}/${todo.length} ${fm.ShortName}: 验证失败，换tab重试`); await newTab(); v = await triggerVerify(); }
    if (!v) { fail++; console.log(`[SKIP] ${fm.ShortName}`); continue; }
    const r1 = await call(v, "/CertECloud/result/listAuthresultByOrg", { orgName: fm.FullName });
    const rows1 = (r1.data && r1.data.rows) || [];
    const orgRec = rows1.length ? rows1[0] : null;
    const rec = { Stkcd: fm.Stkcd, ShortName: fm.ShortName, FullName: fm.FullName,
      byOrgRows: rows1.length, orgId: orgRec ? orgRec.orgId : "", orgCode: orgRec ? orgRec.orgCode : "",
      msg: (r1.data && r1.data.obj && r1.data.obj.info && r1.data.obj.info.msg) || (r1.raw||"") };
    fs.appendFileSync(ORGLOG, JSON.stringify(rec)+"\n", "utf-8");
    if (orgRec) hasRec++;
    const flag = orgRec ? "✅有平台记录" : "❌无记录";
    console.log(`[${i+1}/${todo.length}] ${fm.ShortName}: ${flag} ${orgRec?("orgCode="+orgRec.orgCode):""}`);
    await sleep(2500);
  }
  const total = done.size + todo.length;
  console.log(`[DONE] 平台有记录 ${hasRec}（本次）/ 总计已核对 ${total}，失败 ${fail}`);
  await browser.close();
})().catch(e=>{console.log("ERR",e.message.slice(0,400));process.exit(1);});
