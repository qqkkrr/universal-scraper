#!/usr/bin/env node
/* 侦察 cx.cnca.cn：JS Challenge + 接口可用性 */
const { CHROMIUM_EXE, loadChromium, sleep } = require("./browser_common.cjs");

(async () => {
  const chromium = loadChromium();
  const browser = await chromium.launch({ headless: true, executablePath: CHROMIUM_EXE,
    args: ["--no-sandbox", "--ignore-certificate-errors"] });
  const ctx = await browser.newContext({
    userAgent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    locale: "zh-CN",
  });
  const page = await ctx.newPage();
  const logs = [];
  page.on("console", (m) => { if (m.type() === "error") logs.push(m.text().slice(0, 200)); });
  try {
    // 1) 打开查询页
    await page.goto("https://cx.cnca.cn/CertECloud/result/skipResultList?certItemOne=A", { waitUntil: "domcontentloaded", timeout: 60000 });
    await sleep(4000);
    let cookies = await ctx.cookies();
    let jsl = cookies.find(c => c.name.includes("jsl"));
    console.log("[1] 首次加载 status 200, __jsl_clearance_s =", jsl ? jsl.value.slice(0, 40) : "无");
    // 2) 若 521/无 cookie → 等待混淆 JS 执行后重载
    if (!jsl) {
      await sleep(6000);
      await page.reload({ waitUntil: "domcontentloaded" });
      await sleep(4000);
      cookies = await ctx.cookies();
      jsl = cookies.find(c => c.name.includes("jsl"));
      console.log("[2] 重载后 __jsl_clearance_s =", jsl ? jsl.value.slice(0, 40) : "仍无");
    }
    // 3) 页面内 fetch 测接口（带浏览器 cookie）
    const apiRes = await page.evaluate(async () => {
      try {
        const r = await fetch("/CertECloud/result/listAuthresult?certItemOne=A&certItemTwo=A05&certItemThree=A0501&country=156&certStatus=01&pageNum=1&pageSize=5", { headers: { "Accept": "application/json" } });
        const txt = await r.text();
        return { status: r.status, body: txt.slice(0, 800) };
      } catch (e) { return { status: 0, body: String(e) }; }
    });
    console.log("[3] 接口 pageContext fetch →", apiRes.status);
    console.log("    ", apiRes.body.slice(0, 500));
    // 4) 页面标题/是否有查询按钮/极验
    const html = await page.content();
    const hasQueryBtn = html.includes("查") || html.includes("查询");
    const hasGt = html.includes("gt.js") || html.includes("captcha") || html.includes("极验");
    console.log("[4] 页面有查询按钮:", hasQueryBtn, "| 极验相关:", hasGt, "| html len:", html.length);
    console.log("[5] cookie 列表:", cookies.map(c => c.name).join(","));
  } catch (e) {
    console.log("ERR:", e.message.slice(0, 500));
  }
  await browser.close();
})();
