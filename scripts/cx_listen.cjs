#!/usr/bin/env node
/* 点查询 → 监听 verify + listAuthresult，从任一来源提取参数 */
const fs = require("node:fs");
const { loadChromium, sleep } = require("./browser_common.cjs");
(async () => {
  const chromium = loadChromium();
  const browser = await chromium.connectOverCDP("http://127.0.0.1:9222");
  let page = null;
  for (const c of browser.contexts()) for (const p of c.pages()) if (p.url().includes("cx.cnca.cn")) page = p;
  if (!page) { console.log("无页面"); process.exit(1); }
  const events = [];
  page.on("response", async (resp) => {
    try {
      const u = resp.url();
      if (u.includes("geetest.com/verify") || u.includes("gcaptcha4.com/verify")) {
        const t = await resp.text();
        events.push({ kind: "verify", body: t.slice(0,400) });
      }
    } catch(e) {}
  });
  page.on("request", (req) => {
    const u = req.url();
    if (u.includes("listAuthresult")) events.push({ kind: "auth", url: u.slice(0,500) });
  });
  await page.evaluate(() => { const b=[...document.querySelectorAll("button, .btn")]; const q=b.find(x=>(x.textContent||"").trim().includes("询")); if(q) q.click(); });
  console.log("[1] 已点查询，监听 15 秒…");
  await sleep(15000);
  console.log("[2] 事件数:", events.length);
  events.forEach(e => {
    if (e.kind === "verify") console.log("  VERIFY:", e.body.slice(0,250));
    else console.log("  AUTH:", e.url.slice(0,350));
  });
  // 从 auth URL 提取参数
  for (const e of events) {
    if (e.kind === "auth") {
      const u = e.url;
      const lot = (u.match(/lot_number=([^&]+)/)||[])[1];
      const pass = (u.match(/pass_token=([^&]+)/)||[])[1];
      const gen = (u.match(/gen_time=([^&]+)/)||[])[1];
      const cap = (u.match(/captcha_output=([^&]+)/)||[])[1];
      if (lot&&pass&&gen&&cap) {
        const v = { lot_number: lot, pass_token: pass, gen_time: gen, captcha_output: cap };
        fs.writeFileSync("/tmp/cx_validate.json", JSON.stringify(v,null,1), "utf-8");
        console.log("[OK] 从 AUTH URL 提取参数并保存");
        break;
      }
    }
  }
  await browser.close();
})().catch(e=>{console.log("ERR",e.message.slice(0,400));process.exit(1);});
