#!/usr/bin/env node
/* 守护脚本：每 N 分钟探测 cx 无感验证是否恢复；恢复后自动抓 A05 全量(各状态) + 逐家 orgName 查询 151 家 */
const fs = require("node:fs");
const path = require("node:path");
const { loadChromium, sleep } = require("./browser_common.cjs");
const OUT_DIR = "/Users/kairanqin/Documents/Codex/2026-08-03/ni-shi/iso27001";
const RESULT = path.join(OUT_DIR, "sentinel_results.jsonl");
const QUERY = { certItemOne:"A", certItemTwo:"A05", certItemThree:"A0501", country:"156", certStatus:"01" };

(async () => {
  const interval = parseInt(process.argv[2] || "1800000", 10); // 默认 30 分钟
  const chromium = loadChromium();
  const browser = await chromium.connectOverCDP("http://127.0.0.1:9222");
  const ctx = browser.contexts()[0];
  let attempt = 0;
  while (true) {
    attempt++;
    let page = null;
    try {
      page = await ctx.newPage();
      const verifyQ = [];
      const authResp = [];
      page.on("response", async (r) => {
        try {
          const u=r.url();
          if (u.includes("geetest.com/verify") || u.includes("gcaptcha4.com/verify")) {
            const t=await r.text();
            const lot=(t.match(/"lot_number":"([^"]+)"/)||[])[1], pass=(t.match(/"pass_token":"([^"]+)"/)||[])[1];
            const gen=(t.match(/"gen_time":"?([0-9]+)"?/)||[])[1], cap=(t.match(/"captcha_output":"([^"]+)"/)||[])[1];
            if (lot&&pass&&gen&&cap) verifyQ.push({lot_number:lot,pass_token:pass,gen_time:gen,captcha_output:cap});
          }
          if (u.includes("listAuthresult") && !u.includes("ByOrg") && !u.includes("ForOrg")) {
            const j=await r.json().catch(()=>null); if (j) authResp.push(j);
          }
        } catch(e) {}
      });
      await page.goto("https://cx.cnca.cn/CertECloud/result/skipResultList?certItemOne=A", { waitUntil:"domcontentloaded", timeout:60000 });
      await sleep(10000);
      await page.evaluate(() => {
        const setSel=(id,v)=>{const s=document.getElementById(id);if(!s)return;s.value=v;s.dispatchEvent(new Event("change",{bubbles:true}));};
        setSel("certItemTwo","A05"); setSel("country","156"); setSel("certStatus","01"); return undefined;
      });
      await sleep(6000);
      await page.evaluate(() => { const s3=document.getElementById("certItemThree"); if (s3 && [...s3.options].some(o=>o.value==="A0501")) { s3.value="A0501"; s3.dispatchEvent(new Event("change",{bubbles:true})); } return undefined; });
      await sleep(1000);
      await page.evaluate(() => { const b=[...document.querySelectorAll("button, .btn")]; const q=b.find(x=>(x.textContent||"").trim().includes("询")); if(q) q.click(); });
      await sleep(25000);
      const ok = authResp.length > 0 && authResp[authResp.length-1].pageCount > 0;
      console.log(`[${new Date().toLocaleTimeString()}] 探测#${attempt}: 无感恢复=${ok} (verify=${verifyQ.length}, auth=${authResp.length})`);
      if (ok) {
        console.log("[ACTION] 无感验证已恢复，开始全自动抓取 A05 全量（各状态）+ 151 家 orgName 查询…");
        fs.writeFileSync(path.join(OUT_DIR, "sentinel_ready.txt"), JSON.stringify({ts:Date.now(),note:"无感恢复，可全自动"}), "utf-8");
        await page.close();
        await browser.close();
        // 通知主脚本/直接退出（由调用方接管或下次启动 cx_guided_auto）
        process.exit(0);
      }
      await page.close();
    } catch(e) {
      console.log(`[${new Date().toLocaleTimeString()}] 探测#${attempt} 异常: ${e.message.slice(0,120)}`);
      try { await page.close(); } catch(e2) {}
    }
    console.log(`   下次探测: ${new Date(Date.now()+interval).toLocaleTimeString()}`);
    await sleep(interval);
  }
})().catch(e=>{console.log("ERR",e.message.slice(0,300));process.exit(1);});
