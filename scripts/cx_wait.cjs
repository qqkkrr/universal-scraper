#!/usr/bin/env node
/* 等人工滑块 → 捕获参数 → token 测试 → 全量抓取（滑块已弹出场景） */
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
  await page.evaluate(() => {
    window.__cx_captured = [];
    const of = window.fetch;
    window.fetch = function(...a) {
      try { const u = typeof a[0]==="string"?a[0]:(a[0]&&a[0].url)||""; const b = (a[1]&&a[1].body)||""; if (/listAuthresult|lot_number/i.test(u+String(b))) window.__cx_captured.push({url:u, body:String(b)}); } catch(e){}
      return of.apply(this, a);
    };
    const oo = XMLHttpRequest.prototype.open, os = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function(m,u,...r){ this.__u=u; return oo.call(this,m,u,...r); };
    XMLHttpRequest.prototype.send = function(b){ try{ if(/listAuthresult|lot_number/i.test(String(this.__u)+String(b))) window.__cx_captured.push({url:this.__u, body:String(b||"")}); }catch(e){} return os.call(this,b); };
  });
  console.log("[1] hook 已注入。检查是否有已捕获参数…");
  let validate = null;
  if (fs.existsSync("/tmp/cx_validate.json")) {
    try { const v = JSON.parse(fs.readFileSync("/tmp/cx_validate.json","utf-8"));
      if (v && v.pass_token && v.lot_number && v.gen_time && v.captcha_output) validate = v;
    } catch(e) {}
  }
  if (validate) {
    console.log("[OK] 复用已捕获参数:", JSON.stringify({lot: validate.lot_number.slice(0,12)+"...", gt: validate.gen_time}));
  } else {
    console.log("[!] 未找到参数。若滑块已在页面弹出，请完成它；否则脚本会尝试自动触发（极验4 疑似无感通过）。等待最多 10 分钟…");
  }
  const t0 = Date.now();
  while (!(validate && validate.pass_token) && Date.now() - t0 < 600000) {
    await sleep(2500);
    try {
      const arr = await page.evaluate(() => window.__cx_captured || []);
      for (const c of arr) {
        const txt = c.url + " " + c.body;
        const lot = (txt.match(/lot_number=([^&"\\s]+)/) || txt.match(/"lot_number":"([^"]+)"/) || [])[1];
        if (lot) {
          validate = {
            lot_number: lot,
            pass_token: (txt.match(/pass_token=([^&"\\s]+)/) || txt.match(/"pass_token":"([^"]+)"/) || [])[1] || "",
            gen_time: (txt.match(/gen_time=([^&"\\s]+)/) || txt.match(/"gen_time":"([^"]+)"/) || [])[1] || "",
            captcha_output: (txt.match(/captcha_output=([^&"\\s]+)/) || txt.match(/"captcha_output":"([^"]+)"/) || [])[1] || "",
          };
          if (validate.pass_token && validate.gen_time && validate.captcha_output) break;
        }
      }
      if (validate && validate.pass_token) break;
    } catch(e) {}
  }
  if (!validate || !validate.pass_token) { console.log("[FAIL] 未捕获"); process.exit(1); }
  console.log("[OK] 已捕获参数:", JSON.stringify({lot: validate.lot_number.slice(0,12)+"...", gt: validate.gen_time}));
  fs.writeFileSync("/tmp/cx_validate.json", JSON.stringify(validate, null, 1), "utf-8");

  async function api(pn) {
    const q = new URLSearchParams({ ...QUERY, pageNum:String(pn), pageSize:String(PAGE_SIZE),
      lot_number:validate.lot_number, pass_token:validate.pass_token, gen_time:validate.gen_time, captcha_output:validate.captcha_output });
    return page.evaluate(async (qs) => {
      const r = await fetch("/CertECloud/result/listAuthresult?"+qs, {headers:{"Accept":"application/json"}});
      if (r.status===521) return {blocked:true};
      try { return await r.json(); } catch(e){ return {parse_err:String(e)}; }
    }, q.toString());
  }
  console.log("[TEST] token 有效期测试（1/2/3 页，间隔 40s）…");
  const pc = [];
  for (const pn of [1,2,3]) {
    let j=null;
    for (let r=0; r<MAX_RETRY && !j; r++) { j=await api(pn); if (j&&j.blocked){console.log("[BLOCK] 521");await sleep(BLOCK_WAIT);} if(!j||j.parse_err){j=null;await sleep(5000);} }
    pc.push(j?(j.pageCount??-1):-1);
    console.log(`   page ${pn}: pageCount=${j?j.pageCount:"FAIL"}`);
    await sleep(40000);
  }
  if (!pc.every(x=>x>0)) { console.log("[FAIL] token 短效"); process.exit(1); }
  const first = await api(1);
  const totalPages = first.pageCount||0;
  console.log(`[FETCH] 总页数=${totalPages}`);
  const all=[]; fs.writeFileSync(RAW_JSONL,"","utf-8");
  for (let pn=1; pn<=totalPages; pn++) {
    let j=null;
    for (let r=0; r<MAX_RETRY && !j; r++) { j=await api(pn); if (j&&j.blocked){console.log(`[BLOCK] p${pn} 521`);await sleep(BLOCK_WAIT);} if(!j||j.parse_err){j=null;await sleep(5000);} }
    if (!j) { console.log(`[WARN] p${pn} 失败`); continue; }
    const rows=j.rows||[]; all.push(...rows);
    fs.appendFileSync(RAW_JSONL, rows.map(r=>JSON.stringify(r)).join("\n")+(rows.length?"\n":""), "utf-8");
    if (pn%10===0||pn===totalPages) console.log(`   第${pn}/${totalPages}页，累计 ${all.length} 条`);
    await sleep(WAIT);
  }
  const seen=new Set(); let dup=0;
  for (const r of all){const k=(r.orgName||"")+"|"+(r.certNumber||""); if(seen.has(k))dup++; seen.add(k);}
  console.log(`[DONE] 抓取 ${all.length} 条，去重 ${seen.size}（重复 ${dup}）`);
  await browser.close();
})().catch(e=>{console.log("ERR",e.message.slice(0,500));process.exit(1);});
