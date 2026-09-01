#!/usr/bin/env node
const { loadChromium, sleep } = require("./browser_common.cjs");
(async () => {
  const chromium = loadChromium();
  const browser = await chromium.connectOverCDP("http://127.0.0.1:9222");
  let page = null;
  for (const c of browser.contexts()) for (const p of c.pages()) if (p.url().includes("cx.cnca.cn")) page = p;
  if (!page) { console.log("未找到 cx 页面"); process.exit(1); }
  // 用真实鼠标点击查询按钮（更接近用户行为）
  try {
    const btn = page.locator("button.btn-primary").filter({ hasText: "查" });
    await btn.first().click({ timeout: 8000 });
    console.log("[1] 已真实点击【查询】");
  } catch(e) {
    console.log("[1] locator 点击失败，改用 JS 点击:", e.message.slice(0,100));
    await page.evaluate(() => {
      const btns = [...document.querySelectorAll("button, .btn")];
      const q = btns.find(b => (b.textContent || "").trim().replace(/\\s/g,"").includes("查询"));
      if (q) q.click();
    });
  }
  await sleep(4000);
  const info = await page.evaluate(() => {
    const out = {};
    const gt = document.querySelector(".geetest_captcha, [class*=geetest_captcha]");
    out.geetestVisible = gt ? (getComputedStyle(gt).display !== "none" && gt.offsetParent !== null) : false;
    out.geetestClass = gt ? gt.className.slice(0, 60) : "none";
    // 极验 iframe
    out.frames = [...document.querySelectorAll("iframe")].map(f => f.src || f.id);
    out.captured = (window.__cx_captured || []).length;
    return out;
  });
  console.log("[2] 点击后状态:", JSON.stringify(info));
  // 截图
  const shot = "/tmp/cx_after_click.png";
  await page.screenshot({ path: shot, fullPage: false });
  console.log("[3] 截图:", shot);
  await browser.close();
})().catch(e => { console.log("ERR", e.message.slice(0, 400)); process.exit(1); });
