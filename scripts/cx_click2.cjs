#!/usr/bin/env node
const { loadChromium, sleep } = require("./browser_common.cjs");
(async () => {
  const chromium = loadChromium();
  const browser = await chromium.connectOverCDP("http://127.0.0.1:9222");
  let page = null;
  for (const c of browser.contexts()) for (const p of c.pages()) if (p.url().includes("cx.cnca.cn")) page = p;
  if (!page) { console.log("无 cx 页面"); process.exit(1); }
  const r = await page.evaluate(() => {
    // 确认条件
    const s2 = document.getElementById("certItemTwo");
    const s3 = document.getElementById("certItemThree");
    const st = document.getElementById("certStatus");
    const co = document.getElementById("country");
    // 用 form submit 或按钮 click
    const btns = [...document.querySelectorAll("button, .btn, input[type=button]")];
    const q = btns.find(b => (b.textContent || "").trim().replace(/\\s/g, "").includes("询")) ||
              btns.find(b => (b.value || "").includes("查"));
    if (q) { q.click(); return { clicked: true, vals: [s2.value, s3.value, st.value, co.value] }; }
    return { clicked: false, vals: [s2.value, s3.value, st.value, co.value], btns: btns.map(b => (b.textContent||b.value||"").trim().slice(0,10)) };
  });
  console.log("[1] 点击结果:", JSON.stringify(r));
  await sleep(5000);
  const st = await page.evaluate(() => {
    const gt = document.querySelector("[class*=geetest_captcha]");
    return { visible: gt ? (getComputedStyle(gt).display !== "none" && gt.offsetParent !== null) : false,
             frames: [...document.querySelectorAll("iframe")].map(f => f.src || f.id).slice(0,8),
             captured: (window.__cx_captured || []).length,
             alert: (document.querySelector(".layui-layer-content, .modal-content, [class*=layer-content]") || {}).textContent?.slice(0,80) || "" };
  });
  console.log("[2] 点击后:", JSON.stringify(st));
  await page.screenshot({ path: "/tmp/cx_click2.png" });
  await browser.close();
})().catch(e => { console.log("ERR", e.message.slice(0,400)); process.exit(1); });
