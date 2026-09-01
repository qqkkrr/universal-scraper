#!/usr/bin/env node
/* 干净标签页一体化采集：新tab → 等jQuery → 填表 → 每页重验 → 全量 */
const fs = require("node:fs");
const path = require("node:path");
const { loadChromium, sleep } = require("./browser_common.cjs");
const OUT_DIR = "/Users/kairanqin/Documents/Codex/2026-08-03/ni-shi/iso27001";
const RAW_JSONL = path.join(OUT_DIR, "iso27001_certs_raw.jsonl");
const QUERY = { certItemOne:"A", certItemTwo:"A05", certItemThree:"A0501", country:"156", certStatus:"01" };
const PAGE_SIZE = 100, WAIT = 2500, MAX_RETRY = 3, BLOCK_WAIT = 600000;

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

  console.log("[1] 打开新标签页…");
  await page.goto("https://cx.cnca.cn/CertECloud/result/skipResultList?certItemOne=A", { waitUntil: "domcontentloaded", timeout: 60000 });
  // 等 jQuery 就绪（最长 30s）
  let jq = false;
  for (let i=0;i<30;i++) { await sleep(1000); jq = await page.evaluate(() => typeof window.jQuery !== "undefined" && typeof window.$ !== "undefined").catch(()=>false); if (jq) break; }
  console.log("[2] jQuery 就绪:", jq);
  if (!jq) { console.log("[FAIL] jQuery 未加载"); process.exit(1); }
  await sleep(3000);
  // 填表单
  const filled = await page.evaluate(() => {
    function setSel(id, v) { const s = document.getElementById(id); if (!s) return id+":no"; s.value = v; s.dispatchEvent(new Event("change",{bubbles:true})); return id+":"+v; }
    const r = [setSel("certItemTwo","A05"), setSel("country","156"), setSel("certStatus","01")];
    const s3 = document.getElementById("certItemThree");
    if (s3 && [...s3.options].some(o=>o.value==="A0501")) { s3.value="A0501"; s3.dispatchEvent(new Event("change",{bubbles:true})); r.push("certItemThree:A0501"); }
    return r;
  });
  console.log("[3] 填表:", filled.join(", "));
  await sleep(1500);

  async function triggerVerify() {
    const before = verifyQ.length;
    try {
      await page.evaluate(() => { const b=[...document.querySelectorAll("button, .btn")]; const q=b.find(x=>(x.textContent||"").trim().includes("询")); if(q) q.click(); });
    } catch(e) {}
    const t0 = Date.now();
    while (Date.now()-t0 < 20000 && verifyQ.length <= before) await sleep(1000);
    return verifyQ.length > before ? verifyQ[verifyQ.length-1] : null;
  }
  async function api(v, pn) {
    const q = new URLSearchParams({ ...QUERY, pageNum:String(pn), pageSize:String(PAGE_SIZE),
      lot_number:v.lot_number, pass_token:v.pass_token, gen_time:v.gen_time, captcha_output:v.captcha_output });
    return page.evaluate(async (qs) => {
      const r = await fetch("/CertECloud/result/listAuthresult?"+qs, {headers:{"Accept":"application/json"}});
      if (r.status===521) return {blocked:true};
      try { return await r.json(); } catch(e){ return {parse_err:String(e)}; }
    }, q.toString());
  }

  // 拿总页数
  let curV = await triggerVerify();
  if (!curV) { console.log("[FAIL] 初始验证失败"); process.exit(1); }
  let first = await api(curV, 1);
  if (!first || first.pageCount === 0) { console.log("[WARN] p1=0 重验"); curV = await triggerVerify(); first = await api(curV, 1); }
  const totalPages = first && first.pageCount ? first.pageCount : 0;
  console.log(`[4] 总页数=${totalPages} | 首页样例数=${(first.rows||[]).length} | total字段=${first.total}`);
  if (!totalPages) { console.log("[FAIL] 无 pageCount:", JSON.stringify(first).slice(0,200)); process.exit(1); }

  const all = [];
  fs.writeFileSync(RAW_JSONL, "", "utf-8");
  for (let pn = 1; pn <= totalPages; pn++) {
    curV = await triggerVerify();
    if (!curV) { console.log(`[WARN] p${pn} 验证失败`); continue; }
    let j = null;
    for (let r = 0; r < MAX_RETRY && !j; r++) { j = await api(curV, pn); if (j&&j.blocked){console.log(`[BLOCK] p${pn}`);await sleep(BLOCK_WAIT);} if(!j||j.parse_err){j=null;await sleep(5000);} }
    if (!j || !j.rows) { console.log(`[WARN] p${pn} 查询失败`); continue; }
    const rows = j.rows || [];
    all.push(...rows);
    fs.appendFileSync(RAW_JSONL, rows.map(r=>JSON.stringify(r)).join("\n")+(rows.length?"\n":""), "utf-8");
    console.log(`   第${pn}/${totalPages}页，累计 ${all.length} 条`);
    await sleep(WAIT);
  }
  const seen = new Set(); let dup = 0;
  for (const r of all) { const k=(r.orgName||"")+"|"+(r.certNumber||""); if(seen.has(k))dup++; seen.add(k); }
  console.log(`[DONE] 抓取 ${all.length} 条，去重 ${seen.size}（重复 ${dup}）`);
  await page.close();
  await browser.close();
})().catch(e=>{console.log("ERR",e.message.slice(0,500));process.exit(1);});
