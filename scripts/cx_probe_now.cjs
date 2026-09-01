const { loadChromium, sleep } = require("./browser_common.cjs");
(async () => {
  const chromium = loadChromium();
  const browser = await chromium.connectOverCDP("http://127.0.0.1:9222");
  const ctx = browser.contexts()[0];
  const page = await ctx.newPage();
  const resp = [];
  page.on("response", async (r) => { try { if (r.url().includes("listAuthresult")) resp.push({u:r.url().slice(0,120), j: await r.json().catch(()=>null)}); } catch(e){} });
  await page.goto("https://cx.cnca.cn/CertECloud/result/skipResultList?certItemOne=A", { waitUntil: "domcontentloaded", timeout: 60000 });
  await sleep(10000);
  await page.evaluate(() => {
    const setSel=(id,v)=>{const s=document.getElementById(id);if(!s)return;s.value=v;s.dispatchEvent(new Event("change",{bubbles:true}));};
    setSel("certItemTwo","A05"); setSel("country","156"); setSel("certStatus","01"); return undefined;
  });
  await sleep(6000);
  await page.evaluate(() => { const s3=document.getElementById("certItemThree"); if (s3 && [...s3.options].some(o=>o.value==="A0501")) { s3.value="A0501"; s3.dispatchEvent(new Event("change",{bubbles:true})); } return undefined; });
  await sleep(1000);
  await page.evaluate(() => { const b=[...document.querySelectorAll("button, .btn")]; const q=b.find(x=>(x.textContent||"").trim().includes("询")); if(q) q.click(); });
  await sleep(20000);
  console.log("listAuthresult 响应数:", resp.length);
  resp.forEach(r=>console.log("  pageCount=", r.j&&r.j.pageCount, "total=", r.j&&r.j.total, "rows=", (r.j&&r.j.rows||[]).length));
  // 四宫格可见？
  const st = await page.evaluate(() => {
    const gt=document.querySelector("[class*=geetest_captcha]");
    const visible = gt ? getComputedStyle(gt).display!=="none" && gt.offsetParent!==null : false;
    // 找四宫格文本
    const t=document.body.innerText||"";
    return { geetestVisible: visible, hasFourGrid: /四宫格|点选|点击.*图标|依次点击/.test(t), bodySnippet: t.slice(0,150).replace(/\n+/g,"|") };
  });
  console.log("状态:", JSON.stringify(st));
  await browser.close();
})().catch(e=>{console.log("ERR",e.message.slice(0,300));process.exit(1);});
