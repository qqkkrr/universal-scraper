#!/usr/bin/env node
const { loadChromium } = require("./browser_common.cjs");
(async () => {
  const chromium = loadChromium();
  const browser = await chromium.connectOverCDP("http://127.0.0.1:9222");
  let page = null;
  for (const c of browser.contexts()) for (const p of c.pages()) if (p.url().includes("cx.cnca.cn")) page = p;
  if (!page) { console.log("无 cx 页面"); process.exit(1); }
  const info = await page.evaluate(() => {
    const out = {};
    const gt = document.querySelector("[class*=geetest_captcha]");
    if (gt) {
      const cs = getComputedStyle(gt);
      const r = gt.getBoundingClientRect();
      out.geetest = { display: cs.display, visibility: cs.visibility, rect: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)], z: cs.zIndex };
      out.geetestParentVisible = gt.parentElement ? getComputedStyle(gt.parentElement).display !== "none" : "?";
    } else out.geetest = "none";
    out.frames = [...document.querySelectorAll("iframe")].map(f => (f.src||f.id).slice(0,80));
    out.captured = (window.__cx_captured || []).length;
    out.alert = (document.querySelector(".layui-layer-content")||{}).textContent?.slice(0,60) || "";
    return out;
  });
  console.log(JSON.stringify(info, null, 1));
  await page.screenshot({ path: "/tmp/cx_state.png" });
  await browser.close();
})().catch(e=>{console.log("ERR",e.message.slice(0,300));process.exit(1);});
