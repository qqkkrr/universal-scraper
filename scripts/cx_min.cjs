const { loadChromium, sleep } = require("./browser_common.cjs");
(async () => {
  const chromium = loadChromium();
  const browser = await chromium.connectOverCDP("http://127.0.0.1:9222");
  const ctx = browser.contexts()[0];
  const page = await ctx.newPage();
  console.log("tab url:", await page.evaluate(() => location.href).catch(e => "ERR:"+e.message));
  await page.goto("https://cx.cnca.cn/CertECloud/result/skipResultList?certItemOne=A", { waitUntil: "domcontentloaded", timeout: 60000 });
  await sleep(6000);
  console.log("after goto:", await page.evaluate(() => document.title).catch(e => "ERR:"+e.message));
  const r1 = await page.evaluate(() => { const setSel=(id,v)=>{const s=document.getElementById(id);if(!s)return;s.value=v;s.dispatchEvent(new Event("change",{bubbles:true}));}; setSel("certItemTwo","A05"); return undefined; }).catch(e => "ERR:"+e.message);
  console.log("evaluate1:", r1);
  const r2 = await page.evaluate(() => { const b=[...document.querySelectorAll("button")]; const q=b.find(x=>(x.textContent||"").trim().includes("询")); if(q) q.click(); }).catch(e => "ERR:"+e.message);
  console.log("evaluate2:", r2);
  await browser.close();
})().catch(e=>{console.log("FATAL",e.message.slice(0,300));process.exit(1);});
