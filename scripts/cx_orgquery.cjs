#!/usr/bin/env node
/* 页面 orgName 查询测试：输入企业全称 + A05/有效 → 查询 → 监听 listAuthresult 响应 */
const { loadChromium, sleep } = require("./browser_common.cjs");
(async () => {
  const orgName = process.argv[2] || "广东利扬芯片测试股份有限公司";
  const chromium = loadChromium();
  const browser = await chromium.connectOverCDP("http://127.0.0.1:9222");
  const ctx = browser.contexts()[0];
  const page = await ctx.newPage();
  const authResp = [];
  page.on("response", async (resp) => {
    try {
      if (resp.url().includes("listAuthresult")) {
        const j = await resp.json().catch(()=>null);
        authResp.push({ url: resp.url(), data: j });
      }
    } catch(e) {}
  });
  await page.goto("https://cx.cnca.cn/CertECloud/result/skipResultList?certItemOne=A", { waitUntil: "domcontentloaded", timeout: 60000 });
  await sleep(10000);
  // 填表单：A05/A0501/156/01 + orgName
  const filled = await page.evaluate((name) => {
    const setSel=(id,v)=>{const s=document.getElementById(id);if(!s)return;s.value=v;s.dispatchEvent(new Event("change",{bubbles:true}));};
    setSel("certItemTwo","A05"); setSel("country","156"); setSel("certStatus","01");
    const inp=document.getElementById("orgName");
    if(inp){ inp.value=name; inp.dispatchEvent(new Event("input",{bubbles:true})); }
    return undefined;
  }, orgName);
  await sleep(6000);
  const f3 = await page.evaluate(() => {
    const s3=document.getElementById("certItemThree");
    if (s3 && [...s3.options].some(o=>o.value==="A0501")) { s3.value="A0501"; s3.dispatchEvent(new Event("change",{bubbles:true})); return "A0501 set"; }
    return "A0501 missing: " + (s3?[...s3.options].map(o=>o.value).join(","):"none");
  });
  console.log("[1] 表单:", filled, "|", f3);
  await sleep(1000);
  // 点查询
  await page.evaluate(() => { const b=[...document.querySelectorAll("button, .btn")]; const q=b.find(x=>(x.textContent||"").trim().includes("询")); if(q) q.click(); });
  console.log("[2] 已点查询，等待自动验证+查询…");
  await sleep(15000);
  console.log("[3] listAuthresult 响应数:", authResp.length);
  for (const a of authResp.slice(-3)) {
    const d=a.data;
    console.log("   pageCount=", d&&d.pageCount, "| total=", d&&d.total, "| rows=", (d&&d.rows||[]).length);
    for (const r of (d&&d.rows||[]).slice(0,3)) {
      console.log("     ", r.certNumber, "|", (r.orgName||"").slice(0,30), "|", r.authProjName, "|", r.certiStatusName, "|", r.certiEDate);
    }
  }
  // 也看页面 DOM 结果
  const dom = await page.evaluate(() => {
    const txt = document.body.innerText || "";
    const i = txt.indexOf("证书编号");
    return i>=0 ? txt.slice(i, i+400) : txt.slice(0, 300);
  });
  console.log("[4] 页面文本片段:", dom.replace(/\n+/g," | ").slice(0, 400));
  await page.close(); await browser.close();
})().catch(e=>{console.log("ERR",e.message.slice(0,400));process.exit(1);});
