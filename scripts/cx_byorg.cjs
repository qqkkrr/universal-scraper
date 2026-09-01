#!/usr/bin/env node
/* 逐家 ByOrg 核对 151 家：全称查询 → orgId → ForOrg 证书 → 识别 A05 */
const fs = require("node:fs");
const path = require("node:path");
const { loadChromium, sleep } = require("./browser_common.cjs");
const OUT_DIR = "/Users/kairanqin/Documents/Codex/2026-08-03/ni-shi/iso27001";
const CSV = path.join(OUT_DIR, "firms_151_fullname.csv");
const ORGLOG = path.join(OUT_DIR, "org_check.jsonl");
const CERTLOG = path.join(OUT_DIR, "org_certs.jsonl");
const WAIT = 2500;

(async () => {
  // 读 CSV
  const lines = fs.readFileSync(CSV, "utf-8").split("\n");
  const firms = [];
  for (let i=1;i<lines.length;i++) { const c=lines[i].split(","); if (c.length>=4 && c[2]) firms.push({ Stkcd: c[1].trim(), ShortName: c[2].trim(), FullName: c[3].trim() }); }
  console.log("[0] 待核对企业:", firms.length);

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
    await sleep(9000);
    await page.evaluate(() => {
      const setSel=(id,v)=>{const s=document.getElementById(id);if(!s)return;s.value=v;s.dispatchEvent(new Event("change",{bubbles:true}));};
      setSel("certItemTwo","A05"); setSel("country","156"); return undefined;
    });
    await sleep(5000);
    await page.evaluate(() => { const s3=document.getElementById("certItemThree"); if (s3 && [...s3.options].some(o=>o.value==="A0501")) { s3.value="A0501"; s3.dispatchEvent(new Event("change",{bubbles:true})); } return undefined; });
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
  async function call(v, endpoint, params) {
    const q = new URLSearchParams({ ...params,
      lot_number:v.lot_number, pass_token:v.pass_token, gen_time:v.gen_time, captcha_output:v.captcha_output });
    return page.evaluate(async (arg) => {
      const { ep, qs } = arg;
      const r = await fetch(ep+"?"+qs, {headers:{"Accept":"application/json"}});
      if (r.status===521) return {blocked:true};
      try { return { status: r.status, data: await r.json() }; } catch(e){ return { status: r.status, raw: (await r.text()).slice(0,300) }; }
    }, { ep: endpoint, qs: q.toString() });
  }

  await ensurePage();
  fs.writeFileSync(ORGLOG, "", "utf-8");
  fs.writeFileSync(CERTLOG, "", "utf-8");
  let ok = 0, hasA05 = 0;
  for (let i=0;i<firms.length;i++) {
    const fm = firms[i];
    // 每 8 家刷新一次防卡
    if (i>0 && i%8===0) { console.log(`[RESET] 刷新页面（第${i}家）`); await ensurePage(); }
    let v = await triggerVerify();
    if (!v) { console.log(`[WARN] ${i+1}/${firms.length} ${fm.ShortName}: 验证失败，刷新重试`); await ensurePage(); v = await triggerVerify(); }
    if (!v) { console.log(`[SKIP] ${i+1}/${firms.length} ${fm.ShortName}`); continue; }
    const r1 = await call(v, "/CertECloud/result/listAuthresultByOrg", { orgName: fm.FullName });
    const rows1 = (r1.data && r1.data.rows) || [];
    const orgRec = rows1.length ? rows1[0] : null;
    const logRec = { Stkcd: fm.Stkcd, ShortName: fm.ShortName, FullName: fm.FullName,
      byOrgRows: rows1.length, orgId: orgRec ? orgRec.orgId : "", orgCode: orgRec ? orgRec.orgCode : "",
      msg: (r1.data && r1.data.obj && r1.data.obj.info && r1.data.obj.info.msg) || (r1.raw||"") };
    fs.appendFileSync(ORGLOG, JSON.stringify(logRec)+"\n", "utf-8");
    if (orgRec && orgRec.orgId) {
      ok++;
      // ForOrg 取证书（新验证）
      const before=verifyQ.length;
      await page.evaluate(() => { const b=[...document.querySelectorAll("button, .btn")]; const q=b.find(x=>(x.textContent||"").trim().includes("询")); if(q) q.click(); });
      const t1=Date.now();
      while (Date.now()-t1<20000 && verifyQ.length<=before) await sleep(1000);
      const v2 = verifyQ.length>before ? verifyQ[verifyQ.length-1] : null;
      let certs = [];
      if (v2) {
        const r2 = await call(v2, "/CertECloud/result/listAuthresultForOrg", { orgId: orgRec.orgId, pageNum:"1", pageSize:"100" });
        certs = (r2.data && r2.data.rows) || [];
        if (!certs.length && r2.data && r2.data.obj && r2.data.obj.info) {
          // 二次校验失败尝试新验证
          const b2=verifyQ.length;
          await page.evaluate(() => { const b=[...document.querySelectorAll("button, .btn")]; const q=b.find(x=>(x.textContent||"").trim().includes("询")); if(q) q.click(); });
          const t2=Date.now();
          while (Date.now()-t2<15000 && verifyQ.length<=b2) await sleep(1000);
          const v3 = verifyQ.length>b2 ? verifyQ[verifyQ.length-1] : null;
          if (v3) { const r3 = await call(v3, "/CertECloud/result/listAuthresultForOrg", { orgId: orgRec.orgId }); certs = (r3.data && r3.data.rows) || []; }
        }
      }
      for (const c of certs) {
        const rec = { Stkcd: fm.Stkcd, ShortName: fm.ShortName, FullName: fm.FullName, ...c };
        fs.appendFileSync(CERTLOG, JSON.stringify(rec)+"\n", "utf-8");
        const proj = (c.authProjName||"") + (c.authProjCode||"");
        if (/信息安全|A05/.test(proj)) hasA05++;
      }
      console.log(`[${i+1}/${firms.length}] ${fm.ShortName}: orgId=${orgRec.orgId.slice(0,8)}... 证书${certs.length} 条${certs.some(c=>/信息安全/.test((c.authProjName||"")))?' ★含A05':''}`);
    } else {
      console.log(`[${i+1}/${firms.length}] ${fm.ShortName}: 平台无记录（${logRec.msg||''}）`);
    }
    await sleep(WAIT);
  }
  console.log(`[DONE] 有平台记录 ${ok}/${firms.length}，含 A05 证书记录 ${hasA05} 条`);
  await page.close(); await browser.close();
})().catch(e=>{console.log("ERR",e.message.slice(0,500));process.exit(1);});
