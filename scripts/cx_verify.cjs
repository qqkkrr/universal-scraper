#!/usr/bin/env node
/* 监听 gcaptcha verify 响应 + listAuthresult 请求，提取 4 参数 */
const fs = require("node:fs");
const { loadChromium, sleep } = require("./browser_common.cjs");
(async () => {
  const chromium = loadChromium();
  const browser = await chromium.connectOverCDP("http://127.0.0.1:9222");
  let page = null;
  for (const c of browser.contexts()) for (const p of c.pages()) if (p.url().includes("cx.cnca.cn")) page = p;
  if (!page) { console.log("无 cx 页面"); process.exit(1); }
  const verifyResp = [];
  const authReqs = [];
  page.on("response", async (resp) => {
    const u = resp.url();
    if (u.includes("gcaptcha4.com/verify") || u.includes("geetest.com/verify")) {
      try { const t = await resp.text(); verifyResp.push({ url: u.slice(0,160), body: t.slice(0,1200) }); } catch(e) {}
    }
  });
  page.on("request", (req) => {
    const u = req.url();
    if (u.includes("listAuthresult")) authReqs.push({ url: u.slice(0,400), post: (req.postData()||"").slice(0,200) });
  });
  // 重新点查询（触发 verify）
  await page.evaluate(() => {
    const btns=[...document.querySelectorAll("button, .btn")];
    const q=btns.find(b=>(b.textContent||"").trim().includes("询"));
    if(q) q.click();
  });
  console.log("[1] 已点击查询，等待 verify/查询请求（10 秒）…");
  await sleep(10000);
  console.log("[2] verify 响应数:", verifyResp.length);
  verifyResp.forEach(v => console.log("   VERIFY:", v.body.slice(0,600)));
  console.log("[3] listAuthresult 请求数:", authReqs.length);
  authReqs.slice(-5).forEach(a => console.log("   AUTH:", a.url.slice(0,300)));
  // 若 verify 响应含 4 参数，直接保存
  let got = null;
  for (const v of verifyResp) {
    const m = v.body.match(/\{[\s\S]*"result":"success"[\s\S]*\}/);
    const lot = (v.body.match(/"lot_number":"([^"]+)"/) || [])[1];
    const pass = (v.body.match(/"pass_token":"([^"]+)"/) || [])[1];
    const gen = (v.body.match(/"gen_time":"?([0-9]+)"?/) || [])[1];
    const cap = (v.body.match(/"captcha_output":"([^"]+)"/) || [])[1];
    if (lot && pass && gen && cap) { got = { lot_number: lot, pass_token: pass, gen_time: gen, captcha_output: cap }; break; }
  }
  if (got) {
    console.log("[OK] 从 verify 响应提取参数:", JSON.stringify({lot: got.lot_number.slice(0,12)+"...", gt: got.gen_time}));
    fs.writeFileSync("/tmp/cx_validate.json", JSON.stringify(got, null, 1), "utf-8");
  } else {
    console.log("[WARN] 未从 verify 响应提取到完整参数");
  }
  await browser.close();
})().catch(e=>{console.log("ERR",e.message.slice(0,400));process.exit(1);});
