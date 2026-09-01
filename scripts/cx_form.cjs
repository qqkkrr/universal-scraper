#!/usr/bin/env node
const { loadChromium } = require("./browser_common.cjs");
(async () => {
  const chromium = loadChromium();
  const browser = await chromium.connectOverCDP("http://127.0.0.1:9222");
  let page = null;
  for (const c of browser.contexts()) for (const p of c.pages()) if (p.url().includes("cx.cnca.cn")) page = p;
  if (!page) { console.log("无 cx 页面"); process.exit(1); }
  const info = await page.evaluate(() => {
    const out = { selects: [], inputs: [] };
    document.querySelectorAll("select").forEach((s, i) => {
      out.selects.push({ i, id: s.id, name: s.name, cls: (s.className||"").slice(0,30),
        options: [...s.options].slice(0, 12).map(o => ({ v: o.value, t: (o.textContent||"").trim().slice(0,30) })) });
    });
    document.querySelectorAll("input[type=text], input:not([type])").forEach((s, i) => {
      if (s.offsetParent !== null) out.inputs.push({ i, id: s.id, name: s.name, ph: s.placeholder || "", cls: (s.className||"").slice(0,30) });
    });
    return out;
  });
  console.log(JSON.stringify(info, null, 1));
  await browser.close();
})().catch(e => { console.log("ERR", e.message.slice(0,400)); process.exit(1); });
