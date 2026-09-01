#!/usr/bin/env node
/* 完整守护：每 N 分钟探测无感验证；恢复后自动执行 A05 各状态全量 + 151 家逐家核实，落盘 */
const fs = require("node:fs");
const path = require("node:path");
const { loadChromium, sleep } = require("./browser_common.cjs");
const OUT = "/Users/kairanqin/Documents/Codex/2026-08-03/ni-shi/iso27001";
const HISTORY = path.join(OUT, "history_certs.jsonl");
const GUIDE = path.join(OUT, "guided_results.jsonl");   // 151 家逐家结果
const READY = path.join(OUT, "sentinel_done.txt");
const QUERY = { certItemOne:"A", certItemTwo:"A05", certItemThree:"A0501", country:"156" };

function log(msg){ console.log(`[${new Date().toLocaleTimeString()}] ${msg}`); }

(async () => {
  const interval = parseInt(process.argv[2] || "1800000", 10);
  const chromium = loadChromium();
  const browser = await chromium.connectOverCDP("http://127.0.0.1:9222");
  const ctx = browser.contexts()[0];
  let attempt = 0;

  function hook(p, box) {
    p.on("response", async (r) => {
      try {
        const u = r.url();
        if (u.includes("geetest.com/verify") || u.includes("gcaptcha4.com/verify")) {
          const t = await r.text();
          const lot=(t.match(/"lot_number":"([^"]+)"/)||[])[1], pass=(t.match(/"pass_token":"([^"]+)"/)||[])[1];
          const gen=(t.match(/"gen_time":"?([0-9]+)"?/)||[])[1], cap=(t.match(/"captcha_output":"([^"]+)"/)||[])[1];
          if (lot&&pass&&gen&&cap) box.verifyQ.push({lot_number:lot,pass_token:pass,gen_time:gen,captcha_output:cap});
        }
        if (u.includes("listAuthresult") && !u.includes("ByOrg") && !u.includes("ForOrg")) {
          const j = await r.json().catch(()=>null);
          if (j) box.auth.push(j);
        }
      } catch(e) {}
    });
  }

  async function newPage(box) {
    try { await box.page.close(); } catch(e) {}
    box.page = await ctx.newPage();
    box.verifyQ = []; box.auth = [];
    hook(box.page, box);
    await box.page.goto("https://cx.cnca.cn/CertECloud/result/skipResultList?certItemOne=A", { waitUntil:"domcontentloaded", timeout:60000 });
    await sleep(9000);
    await box.page.evaluate(() => {
      const setSel=(id,v)=>{const s=document.getElementById(id);if(!s)return;s.value=v;s.dispatchEvent(new Event("change",{bubbles:true}));};
      setSel("certItemTwo","A05"); setSel("country","156"); setSel("certStatus","01"); return undefined;
    });
    await sleep(5000);
    await box.page.evaluate(() => { const s3=document.getElementById("certItemThree"); if (s3 && [...s3.options].some(o=>o.value==="A0501")) { s3.value="A0501"; s3.dispatchEvent(new Event("change",{bubbles:true})); } return undefined; });
    await sleep(1000);
  }

  async function doQuery(box, orgName, status) {
    // 填条件（若 orgName 给出则填输入框）
    const before = box.auth.length;
    if (orgName) {
      await box.page.locator("#orgName").fill(orgName, { timeout: 8000 }).catch(()=>{});
      await sleep(1500);
    }
    await box.page.evaluate(() => { const b=[...document.querySelectorAll("button, .btn")]; const q=b.find(x=>(x.textContent||"").trim().includes("询")); if(q) q.click(); });
    // 等自动响应（无感验证会自动过）；最多 25s
    const t0=Date.now();
    while (Date.now()-t0<25000 && box.auth.length===before) await sleep(1000);
    return box.auth.length>before ? box.auth[box.auth.length-1] : null;
  }

  // ---- 探测循环 ----
  for (;;) {
    attempt++;
    const box = { page: null, verifyQ: [], auth: [] };
    let ok = false;
    try {
      await newPage(box);
      // 用全量条件探测（不带 orgName）
      const j = await doQuery(box, null, null);
      ok = !!(j && j.pageCount > 0);
      log(`探测#${attempt}: 无感恢复=${ok} (verify=${box.verifyQ.length}, pageCount=${j?j.pageCount:"-"})`);
      if (ok) {
        log("🚀 无感恢复！开始全自动抓取…");
        break;
      }
      await box.page.close().catch(()=>{});
    } catch(e) {
      log(`探测#${attempt} 异常: ${e.message.slice(0,120)}`);
      try { await box.page.close(); } catch(e2) {}
    }
    log(`下次探测: ${new Date(Date.now()+interval).toLocaleTimeString()}`);
    await sleep(interval);
  }

  // ---- 全自动抓取 ----
  const box = { page: null, verifyQ: [], auth: [] };
  await newPage(box);

  // 1) A05 各状态全量（01/02/03/04/05 + all）
  const statuses = ["01","02","03","04","05",""];
  for (const st of statuses) {
    const q = { ...QUERY, ...(st?{certStatus:st}:{}) };
    // 页面设置 certStatus
    await box.page.evaluate((v) => {
      const s=document.getElementById("certStatus");
      if (s) { s.value=v; s.dispatchEvent(new Event("change",{bubbles:true})); }
      const o=document.getElementById("orgName");
      if (o) { o.value=""; o.dispatchEvent(new Event("input",{bubbles:true})); }
      return undefined;
    }, st);
    await sleep(1000);
    const first = await doQuery(box, null, st);
    const totalPages = first && first.pageCount ? first.pageCount : 0;
    log(`状态 ${st||"all"}: ${totalPages} 页`);
    if (first && first.rows) {
      fs.appendFileSync(HISTORY, first.rows.map(r=>JSON.stringify({...r,_certStatusQuery:st||"all"})).join("\n")+"\n","utf-8");
    }
    for (let pn=2; pn<=totalPages; pn++) {
      await box.page.evaluate((pn2) => {
        // 翻页：直接 fetch 带参数（页面分页逻辑）；简化：重新查询 pageNum 需通过页面分页控件，这里用直接 fetch
        return undefined;
      }, pn);
      // 用页面内 fetch 翻页（无感 token 已由 doQuery 获取，但参数一次性 → 重新 doQuery 无法指定 pageNum）
      // 简化：页面分页控件点击下一页
      try {
        await box.page.evaluate(() => {
          const btns=[...document.querySelectorAll(".jpage a, .page a, [class*=next], [class*=下一页]")];
          const n=btns.find(b=>/next|下一页|>/.test((b.textContent||"").trim()));
          if (n) n.click();
        });
        const t0=Date.now();
        const before=box.auth.length;
        while (Date.now()-t0<20000 && box.auth.length===before) await sleep(1000);
        const j2 = box.auth[box.auth.length-1];
        if (j2 && j2.rows) {
          fs.appendFileSync(HISTORY, j2.rows.map(r=>JSON.stringify({...r,_certStatusQuery:st||"all"})).join("\n")+"\n","utf-8");
        }
      } catch(e) {}
      await sleep(2500);
    }
    await sleep(2500);
  }
  log("A05 各状态全量抓取完成");

  // 2) 151 家逐家核实（orgName 查询）
  const lines = fs.readFileSync(path.join(OUT,"firms_151_fullname.csv"),"utf-8").split("\n");
  const firms=[];
  for (let i=1;i<lines.length;i++){const c=lines[i].split(",");if(c.length>=4&&c[2])firms.push({Stkcd:c[1].trim(),ShortName:c[2].trim(),FullName:c[3].trim()});}
  for (let i=0;i<firms.length;i++) {
    const fm=firms[i];
    if (i>0 && i%8===0) { log(`换新标签页（${i}/${firms.length}）`); await newPage(box); }
    // 重置状态为有效（逐家核实用 certStatus=01，另可再查全部状态；先用 01）
    await box.page.evaluate(() => {
      const s=document.getElementById("certStatus");
      if (s) { s.value="01"; s.dispatchEvent(new Event("change",{bubbles:true})); }
      return undefined;
    });
    await sleep(800);
    const j = await doQuery(box, fm.FullName, "01");
    const rows = (j && j.rows) || [];
    const isA05 = rows.some(r=>/信息安全|A05/.test((r.authProjName||"")+(r.authProjCode||"")));
    fs.appendFileSync(GUIDE, JSON.stringify({Stkcd:fm.Stkcd,ShortName:fm.ShortName,FullName:fm.FullName,status:"done",rows:rows.length,isA05:isA05?1:0,certs:rows.map(r=>({certNumber:r.certNumber,authProjName:r.authProjName,certiStatusName:r.certiStatusName,certiEDate:r.certiEDate}))})+ "\n","utf-8");
    if (i%10===0 || i===firms.length-1) log(`逐家 ${i+1}/${firms.length}（累计 A05 命中 ${/* 后续统计 */"?"}）`);
    await sleep(2500);
  }
  fs.writeFileSync(READY, JSON.stringify({ts:Date.now(),note:"无感恢复后全自动抓取完成（A05各状态全量 + 151家逐家核实）"}),"utf-8");
  log("🎉 全部完成：" + READY);
  await browser.close();
  process.exit(0);
})().catch(e=>{console.log("ERR",e.message.slice(0,500));process.exit(1);});
