#!/usr/bin/env node
/* 联动：点查询 → verify 自动通过 → 页面自动发查询 → 记录其响应 */
const { loadChromium, sleep } = require("./browser_common.cjs");
(async () => {
  const chromium = loadChromium();
  const browser = await chromium.connectOverCDP("http://127.0.0.1:9222");
  let page = null;
  for (const c of browser.contexts()) for (const p of c.pages()) if (p.url().includes("cx.cnca.cn")) page = p;
  if (!page) { console.log("无 cx 页面"); process.exit(1); }
  const ev = { verify: [], auth: [] };
  page.on("response", async (resp) => {
    const u = resp.url();
    if (u.includes("geetest.com/verify")) { try { ev.verify.push((await resp.text()).slice(0,800)); } catch(e){} }
    if (u.includes("listAuthresult")) { try { ev.auth.push({ url: u.slice(0,400), body: (await resp.text()).slice(0,600) }); } catch(e){} }
  });
  await page.evaluate(() => {
    const btns=[...document.querySelectorAll("button, .btn")];
    const q=btns.find(b=>(b.textContent||"").trim().includes("询"));
    if(q) q.click();
  });
  console.log("[1] 已点击查询，等待 verify + 自动查询…");
  await sleep(12000);
  console.log("[2] verify:", ev.verify.length, "| auth:", ev.auth.length);
  ev.verify.forEach(v => console.log("  VERIFY:", v.slice(0,200)));
  ev.auth.forEach(a => {
    console.log("  AUTH URL:", a.url.slice(0,250));
    console.log("  AUTH BODY:", a.body.slice(0,400));
  });
  await browser.close();
})().catch(e=>{console.log("ERR",e.message.slice(0,400));process.exit(1);});
