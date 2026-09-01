#!/usr/bin/env node
const { loadChromium } = require("./browser_common.cjs");
(async () => {
  const chromium = loadChromium();
  const browser = await chromium.connectOverCDP("http://127.0.0.1:9222");
  let page = null;
  for (const c of browser.contexts()) for (const p of c.pages()) if (p.url().includes("cx.cnca.cn")) page = p;
  if (!page) { console.log("无页面"); process.exit(1); }
  const info = await page.evaluate(() => {
    const out = { url: location.href.slice(0,90) };
    // 所有可见的弹窗层
    out.layers = [...document.querySelectorAll(".layui-layer, [class*=layer], [class*=modal], [class*=alert], [class*=popup]")]
      .filter(el => el.offsetParent !== null)
      .slice(0,10)
      .map(el => ({ cls: (el.className||"").toString().slice(0,50), text: (el.textContent||"").trim().slice(0,60) }));
    // 表单值
    ["certItemTwo","certItemThree","country","certStatus"].forEach(id => { const s=document.getElementById(id); out[id]=s?s.value:"?"; });
    // 页面 JS 状态：jQuery 是否正常
    out.hasJQuery = typeof window.jQuery !== "undefined";
    out.$ = typeof window.$ !== "undefined";
    // 查询按钮可见性
    const btns=[...document.querySelectorAll("button, .btn")];
    const q=btns.find(b=>(b.textContent||"").trim().includes("询"));
    out.queryBtnVisible = q ? q.offsetParent !== null : false;
    out.consoleErrors = window.__cx_errs || [];
    return out;
  });
  console.log(JSON.stringify(info, null, 1));
  await browser.close();
})().catch(e=>{console.log("ERR",e.message.slice(0,300));process.exit(1);});
