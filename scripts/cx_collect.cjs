#!/usr/bin/env node
/* 最终采集：点查询→自动无感verify→立即抓页→失效自动重验→全量落盘 */
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
  let page = null;
  for (const c of browser.contexts()) for (const p of c.pages()) if (p.url().includes("cx.cnca.cn")) page = p;
  if (!page) { console.log("无 cx 页面"); process.exit(1); }
  const verifyQ = [];
  page.on("response", async (resp) => {
    try {
      const u = resp.url();
      if (u.includes("geetest.com/verify") || u.includes("gcaptcha4.com/verify")) {
        const t = await resp.text();
        const lot = (t.match(/"lot_number":"([^"]+)"/) || [])[1];
        const pass = (t.match(/"pass_token":"([^"]+)"/) || [])[1];
        const gen = (t.match(/"gen_time":"?([0-9]+)"?/) || [])[1];
        const cap = (t.match(/"captcha_output":"([^"]+)"/) || [])[1];
        if (lot && pass && gen && cap && t.includes('"success"')) {
          verifyQ.push({ lot_number: lot, pass_token: pass, gen_time: gen, captcha_output: cap, ts: Date.now() });
        }
      }
    } catch(e) {}
  });

  async function triggerVerify() {
    // 点查询触发（页面会自动无感 verify）
    const before = verifyQ.length;
    try {
      await page.evaluate(() => {
        const btns=[...document.querySelectorAll("button, .btn")];
        const q=btns.find(b=>(b.textContent||"").trim().includes("询"));
        if(q) q.click();
      });
    } catch(e) {}
    const t0 = Date.now();
    while (Date.now() - t0 < 20000 && verifyQ.length <= before) await sleep(1000);
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

  // 先拿总页数（page1，用新验证）
  console.log("[2] 获取总页数…");
  let curV = await triggerVerify();
  if (!curV) { console.log("[FAIL] 无法获取验证参数"); process.exit(1); }
  let first = await api(curV, 1);
  if (!first || first.pageCount === 0) { console.log("[WARN] page1=0，重验一次"); curV = await triggerVerify(); first = await api(curV, 1); }
  const totalPages = first && first.pageCount ? first.pageCount : 0;
  if (!totalPages) { console.log("[FAIL] pageCount 拿不到:", JSON.stringify(first).slice(0,300)); process.exit(1); }
  console.log(`[OK] 总页数=${totalPages}（pageSize=${PAGE_SIZE}）`);
  const tokenLog = [];
  const all = [];
  fs.writeFileSync(RAW_JSONL, "", "utf-8");
  for (let pn = 1; pn <= totalPages; pn++) {
    // 每页都重新触发验证（平台验证参数一次性，验证后立即查询）
    const t0v = Date.now();
    curV = await triggerVerify();
    if (!curV) { console.log(`[WARN] p${pn} 验证失败，重试一次`); curV = await triggerVerify(); }
    if (!curV) { console.log(`[WARN] p${pn} 验证仍失败，跳过`); continue; }
    tokenLog.push(`page ${pn}: new lot=${curV.lot_number.slice(0,8)}... (verify耗时 ${Date.now()-t0v}ms)`);
    let j = null;
    for (let r = 0; r < MAX_RETRY && !j; r++) {
      j = await api(curV, pn);
      if (j && j.blocked) { console.log(`[BLOCK] p${pn} 521，等待 10 分钟`); await sleep(BLOCK_WAIT); }
      if (!j || j.parse_err) { j = null; await sleep(5000); }
    }
    if (!j || !j.rows) { console.log(`[WARN] p${pn} 查询失败，跳过`); continue; }
    const rows = j.rows || [];
    all.push(...rows);
    fs.appendFileSync(RAW_JSONL, rows.map(r=>JSON.stringify(r)).join("\n")+(rows.length?"\n":""), "utf-8");
    if (pn % 5 === 0 || pn === totalPages) console.log(`   第${pn}/${totalPages}页，累计 ${all.length} 条`);
    await sleep(WAIT);
  }
  const seen = new Set(); let dup = 0;
  for (const r of all) { const k = (r.orgName||"")+"|"+(r.certNumber||""); if (seen.has(k)) dup++; seen.add(k); }
  console.log(`[DONE] 抓取 ${all.length} 条，去重 ${seen.size}（重复 ${dup}）`);
  fs.writeFileSync(path.join(OUT_DIR, "token_test_log.txt"), tokenLog.join("\n"), "utf-8");
  await browser.close();
})().catch(e=>{console.log("ERR",e.message.slice(0,500));process.exit(1);});
