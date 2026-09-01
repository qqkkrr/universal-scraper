#!/usr/bin/env node
/* 干净 tab：填表 → 点查询 → 无感verify → 立即抓 page1 全量落盘 */
const fs = require("node:fs");
const path = require("node:path");
const { loadChromium, sleep } = require("./browser_common.cjs");
const OUT_DIR = "/Users/kairanqin/Documents/Codex/2026-08-03/ni-shi/iso27001";
const RAW_JSONL = path.join(OUT_DIR, "iso27001_certs_raw.jsonl");
const QUERY = { certItemOne:"A", certItemTwo:"A05", certItemThree:"A0501", country:"156", certStatus:"01" };
const PAGE_SIZE = 100;

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
  console.log("[1] 新 tab 打开…");
  await page.goto("https://cx.cnca.cn/CertECloud/result/skipResultList?certItemOne=A", { waitUntil: "domcontentloaded", timeout: 60000 });
  await sleep(8000);
  const filled = await page.evaluate(() => {
    function setSel(id, v) { const s=document.getElementById(id); if(!s) return id+":no"; s.value=v; s.dispatchEvent(new Event("change",{bubbles:true})); return id+":"+v; }
    const r=[setSel("certItemTwo","A05"),setSel("country","156"),setSel("certStatus","01")];
    return r;
  });
  console.log("[2] 先选 A05/156/01，等 A0501 级联加载…");
  await sleep(5000);
  const f2 = await page.evaluate(() => {
    const s3=document.getElementById("certItemThree");
    if (s3 && [...s3.options].some(o=>o.value==="A0501")) { s3.value="A0501"; s3.dispatchEvent(new Event("change",{bubbles:true})); return "A0501 已选"; }
    return "A0501 未加载: " + (s3 ? [...s3.options].map(o=>o.value).join(",") : "none");
  });
  console.log("[2.5]", f2);
  if (f2.includes("未加载")) { console.log("[FAIL] A0501 未加载"); process.exit(1); }
  await sleep(2000);
  await page.evaluate(() => { const b=[...document.querySelectorAll("button, .btn")]; const q=b.find(x=>(x.textContent||"").trim().includes("询")); if(q) q.click(); });
  console.log("[3] 已点查询，等无感验证…");
  const t0=Date.now();
  while (Date.now()-t0<25000 && verifyQ.length===0) await sleep(1000);
  if (!verifyQ.length) { console.log("[FAIL] 无感验证未触发"); await page.close(); process.exit(1); }
  const v = verifyQ[verifyQ.length-1];
  console.log("[4] 验证参数:", JSON.stringify({lot:v.lot_number.slice(0,12), gt:v.gen_time}));
  const q = new URLSearchParams({ ...QUERY, pageNum:"1", pageSize:String(PAGE_SIZE),
    lot_number:v.lot_number, pass_token:v.pass_token, gen_time:v.gen_time, captcha_output:v.captcha_output });
  const j = await page.evaluate(async (qs) => {
    const r = await fetch("/CertECloud/result/listAuthresult?"+qs, {headers:{"Accept":"application/json"}});
    const t = await r.text();
    try { return { status: r.status, data: JSON.parse(t) }; } catch(e) { return { status: r.status, raw: t.slice(0,500) }; }
  }, q.toString());
  if (j.status !== 200 || !j.data || j.data.pageCount === 0) {
    console.log("[FAIL] 查询失败:", JSON.stringify(j).slice(0,400));
    await page.close(); process.exit(1);
  }
  const rows = j.data.rows || [];
  console.log(`[OK] pageCount=${j.data.pageCount} | total=${j.data.total} | 本页 rows=${rows.length}`);
  fs.writeFileSync(RAW_JSONL, rows.map(r=>JSON.stringify(r)).join("\n")+"\n", "utf-8");
  console.log(`[DONE] 已落盘 ${rows.length} 条 → ${RAW_JSONL}`);
  // 打印前 3 条摘要
  rows.slice(0,3).forEach(r => console.log("   ", r.certNumber, "|", (r.orgName||"").slice(0,30), "|", r.certiEDate, "|", (r.rzjgName||"").slice(0,20)));
  await page.close();
  await browser.close();
})().catch(e=>{console.log("ERR",e.message.slice(0,500));process.exit(1);});
