#!/usr/bin/env node
const { CHROMIUM_EXE, loadChromium, sleep } = require("./browser_common.cjs");
(async () => {
  const chromium = loadChromium();
  const browser = await chromium.launch({ headless: true, executablePath: CHROMIUM_EXE,
    args: ["--no-sandbox", "--ignore-certificate-errors"] });
  const ctx = await browser.newContext({ locale: "zh-CN" });
  const page = await ctx.newPage();
  await page.goto("https://cx.cnca.cn/CertECloud/result/skipResultList?certItemOne=A", { waitUntil: "domcontentloaded", timeout: 60000 });
  await sleep(5000);
  // 找按钮/输入
  const btns = await page.evaluate(() => {
    const out = [];
    document.querySelectorAll("button, input[type=button], a.btn, .btn").forEach((b, i) => {
      out.push({ i, tag: b.tagName, text: (b.textContent || "").trim().slice(0, 20), cls: (b.className || "").slice(0, 40), id: b.id });
    });
    return out.slice(0, 30);
  });
  console.log("== 按钮 ==");
  btns.forEach(b => console.log(b.i, b.tag, b.id, b.cls, b.text));
  // 极验 gt 对象
  const gtInfo = await page.evaluate(() => {
    const info = {};
    info.hasWindowGt = typeof window.gt !== "undefined";
    info.hasCaptcha = typeof window.captcha !== "undefined";
    info.gtKeys = window.gt ? Object.keys(window.gt).slice(0, 20) : [];
    info.gtType = typeof window.gt;
    return info;
  });
  console.log("== gt 对象 ==", JSON.stringify(gtInfo));
  await browser.close();
})().catch(e => { console.log("ERR", e.message.slice(0, 400)); process.exit(1); });
