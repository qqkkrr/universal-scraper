#!/usr/bin/env node
/* 刷新页面 → 过 JS challenge → 重新填表单（A05/A0501/156/01） */
const { loadChromium, sleep } = require("./browser_common.cjs");
(async () => {
  const chromium = loadChromium();
  const browser = await chromium.connectOverCDP("http://127.0.0.1:9222");
  let page = null;
  for (const c of browser.contexts()) for (const p of c.pages()) if (p.url().includes("cx.cnca.cn")) page = p;
  if (!page) { console.log("无页面"); process.exit(1); }
  await page.reload({ waitUntil: "domcontentloaded", timeout: 60000 });
  await sleep(6000);
  const r = await page.evaluate(() => {
    function setSel(id, v) { const s = document.getElementById(id); if (!s) return id+":no"; s.value = v; s.dispatchEvent(new Event("change",{bubbles:true})); return id+":"+v; }
    const res = [setSel("certItemTwo","A05"), setSel("country","156"), setSel("certStatus","01")];
    const s3 = document.getElementById("certItemThree");
    if (s3) { const has = [...s3.options].some(o=>o.value==="A0501"); if (has) { s3.value="A0501"; s3.dispatchEvent(new Event("change",{bubbles:true})); res.push("certItemThree:A0501"); } }
    return res;
  });
  console.log("[OK] 已刷新并填充:", r.join(", "));
  await browser.close();
})().catch(e=>{console.log("ERR",e.message.slice(0,300));process.exit(1);});
