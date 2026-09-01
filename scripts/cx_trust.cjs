#!/usr/bin/env node
/* 信任期实验：用户点一次四宫格 → 连续查询 5 家，统计"自动"vs"需人工" */
const fs = require("node:fs");
const path = require("node:path");
const { loadChromium, sleep } = require("./browser_common.cjs");
const OUT = "/Users/kairanqin/Documents/Codex/2026-08-03/ni-shi/iso27001";
(async () => {
  const lines = fs.readFileSync(path.join(OUT,"firms_151_fullname.csv"),"utf-8").split("\n");
  const firms=[];
  for (let i=1;i<lines.length;i++){const c=lines[i].split(",");if(c.length>=4&&c[2])firms.push({Stkcd:c[1].trim(),ShortName:c[2].trim(),FullName:c[3].trim()});}
  const test = firms.slice(0,5);
  const chromium = loadChromium();
  const browser = await chromium.connectOverCDP("http://127.0.0.1:9222");
  const ctx = browser.contexts()[0];
  const page = await ctx.newPage();
  let authCount = 0;
  page.on("response", async (r) => { try { if (r.url().includes("listAuthresult") && !r.url().includes("ByOrg") && !r.url().includes("ForOrg")) { await r.json().catch(()=>null); authCount++; } } catch(e){} });
  async function resetPage() {
    await page.goto("https://cx.cnca.cn/CertECloud/result/skipResultList?certItemOne=A", { waitUntil:"domcontentloaded", timeout:60000 });
    await sleep(8000);
    await page.evaluate(() => { const setSel=(id,v)=>{const s=document.getElementById(id);if(!s)return;s.value=v;s.dispatchEvent(new Event("change",{bubbles:true}));}; setSel("certItemTwo","A05"); setSel("country","156"); setSel("certStatus","01"); return undefined; });
    await sleep(5000);
    await page.evaluate(() => { const s3=document.getElementById("certItemThree"); if (s3 && [...s3.options].some(o=>o.value==="A0501")) { s3.value="A0501"; s3.dispatchEvent(new Event("change",{bubbles:true})); } return undefined; });
    await sleep(1000);
  }
  await resetPage();
  const log = [];
  for (let i=0;i<test.length;i++) {
    const fm=test[i];
    const before=authCount;
    await page.locator("#orgName").fill(fm.FullName, {timeout:8000}).catch(()=>{});
    await sleep(1200);
    await page.evaluate(() => { const b=[...document.querySelectorAll("button, .btn")]; const q=b.find(x=>(x.textContent||"").trim().includes("询")); if(q) q.click(); });
    // 等最多 30s：若 authCount 增加 = 自动通过；否则需人工
    const t0=Date.now();
    let auto=false;
    while (Date.now()-t0<30000 && authCount===before) await sleep(1000);
    if (authCount>before) auto=true;
    const needManual = !auto;
    log.push({i:i+1, Stkcd:fm.Stkcd, ShortName:fm.ShortName, auto, needManual});
    console.log(`[${i+1}/${test.length}] ${fm.ShortName}: ${auto?"✅自动通过(免验证)":"❌需人工点选"}`);
    if (needManual) {
      console.log("   ⏳ 请点选四宫格…（最多等 45s）");
      const t1=Date.now();
      while (Date.now()-t1<45000 && authCount===before) await sleep(1000);
      if (authCount>before) { console.log("   已点选，响应收到"); } else { console.log("   45s 无响应，跳过"); }
    }
    await sleep(3000);
  }
  console.log("\n[结论] 点一次四宫格能自动通过的次数:", log.filter(x=>x.auto).length, "/", log.length);
  fs.writeFileSync("/tmp/cx_trust_log.json", JSON.stringify(log,null,1),"utf-8");
  await browser.close();
})().catch(e=>{console.log("ERR",e.message.slice(0,400));process.exit(1);});
