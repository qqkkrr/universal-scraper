#!/usr/bin/env node
/* 半自动引导器：逐家填企业名→查询→等人工点四宫格→读结果→下一家 */
const fs = require("node:fs");
const path = require("node:path");
const { loadChromium, sleep } = require("./browser_common.cjs");
const OUT_DIR = "/Users/kairanqin/Documents/Codex/2026-08-03/ni-shi/iso27001";
const RESULT = path.join(OUT_DIR, "guided_results.jsonl");

(async () => {
  const lines = fs.readFileSync(path.join(OUT_DIR,"firms_151_fullname.csv"), "utf-8").split("\n");
  const firms = [];
  for (let i=1;i<lines.length;i++) { const c=lines[i].split(","); if (c.length>=4 && c[2]) firms.push({ Stkcd: c[1].trim(), ShortName: c[2].trim(), FullName: c[3].trim() }); }
  const done = new Set();
  if (fs.existsSync(RESULT)) for (const l of fs.readFileSync(RESULT,"utf-8").split("\n")) { try { done.add(JSON.parse(l).Stkcd); } catch(e){} }
  const todo = firms.filter(f => !done.has(f.Stkcd));
  console.log("[0] 待查:", todo.length, "已完成:", done.size);

  const chromium = loadChromium();
  const browser = await chromium.connectOverCDP("http://127.0.0.1:9222");
  const ctx = browser.contexts()[0];
  const page = await ctx.newPage();
  let authResp = null;
  page.on("response", async (r) => {
    try {
      if (r.url().includes("listAuthresult") && !r.url().includes("ByOrg") && !r.url().includes("ForOrg")) {
        const j = await r.json().catch(()=>null);
        if (j) authResp = { data: j, url: r.url() };
      }
    } catch(e) {}
  });
  async function resetPage() {
    try { await page.goto("https://cx.cnca.cn/CertECloud/result/skipResultList?certItemOne=A", { waitUntil: "domcontentloaded", timeout: 60000 }); } catch(e){}
    await sleep(8000);
    await page.evaluate(() => {
      const setSel=(id,v)=>{const s=document.getElementById(id);if(!s)return;s.value=v;s.dispatchEvent(new Event("change",{bubbles:true}));};
      setSel("certItemTwo","A05"); setSel("country","156"); setSel("certStatus","01"); return undefined;
    });
    await sleep(5000);
    await page.evaluate(() => { const s3=document.getElementById("certItemThree"); if (s3 && [...s3.options].some(o=>o.value==="A0501")) { s3.value="A0501"; s3.dispatchEvent(new Event("change",{bubbles:true})); } return undefined; });
    await sleep(1000);
  }

  await resetPage();
  for (let i=0;i<todo.length;i++) {
    const fm = todo[i];
    if (i>0 && i%8===0) { console.log(`[RESET] 第${i}家，刷新页面`); await resetPage(); }
    authResp = null;
    // 填企业名（真实输入，触发页面校验）
    try {
      await page.locator("#orgName").fill(fm.FullName, { timeout: 8000 });
    } catch(e) {
      console.log(`[WARN] ${fm.ShortName} 输入失败: ${e.message.slice(0,80)}`);
    }
    await sleep(1500);
    // 点查询
    await page.evaluate(() => { const b=[...document.querySelectorAll("button, .btn")]; const q=b.find(x=>(x.textContent||"").trim().includes("询")); if(q) q.click(); });
    // 等响应（不超时：点完四宫格自动继续；每 30s 提示一次）
    console.log(`[${i+1}/${todo.length}] ${fm.ShortName}：请在弹出的四宫格验证中点选（点完自动继续）…`);
    const t0=Date.now();
    while (!authResp) {
      await sleep(1000);
      if (Date.now()-t0>30000 && (Date.now()-t0)%30000<1000) {
        const st = await page.evaluate(() => { const gt=document.querySelector("[class*=geetest_captcha]"); return gt? (getComputedStyle(gt).display!=="none" && gt.offsetParent!==null):false; }).catch(()=>false);
        console.log(`      ⏳ 等待中…（极验可见=${st}，${Math.round((Date.now()-t0)/1000)}s）`);
      }
    }
    const rows = (authResp.data && authResp.data.rows) || [];
    const pc = authResp.data ? authResp.data.pageCount : -1;
    const isA05 = rows.some(r=>/信息安全|A05/.test((r.authProjName||"")+(r.authProjCode||"")));
    for (const r of rows) {
      fs.appendFileSync(RESULT, JSON.stringify({Stkcd:fm.Stkcd,ShortName:fm.ShortName,FullName:fm.FullName,status:"done",certNumber:r.certNumber,orgName:r.orgName,authProjName:r.authProjName,certiStatusName:r.certiStatusName,certiEDate:r.certiEDate,rzjgName:r.rzjgName})+ "\n","utf-8");
    }
    if (!rows.length) fs.appendFileSync(RESULT, JSON.stringify({Stkcd:fm.Stkcd,ShortName:fm.ShortName,FullName:fm.FullName,status:"done",rows:0})+ "\n","utf-8");
    console.log(`[${i+1}/${todo.length}] ${fm.ShortName}: pageCount=${pc} rows=${rows.length}${isA05?" ★A05":""}`);
    await sleep(4000);
  }
  console.log("[DONE] 队列完成");
  await browser.close();
})().catch(e=>{console.log("ERR",e.message.slice(0,500));process.exit(1);});
