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
  const info = await page.evaluate(() => {
    const keys = Object.keys(window).filter(k => /geetest|captcha|validate|gt/i.test(k));
    const out = {};
    keys.forEach(k => { try { out[k] = typeof window[k]; } catch(e) { out[k] = "?"; } });
    // initGeetest4 引用
    out["initGeetest4_type"] = typeof window.initGeetest4;
    // 极验 iframe / 容器
    const iframes = [...document.querySelectorAll("iframe")].map(f => f.src || f.id || "?");
    out["iframes"] = iframes.slice(0, 10);
    // 所有含 geetest 的 class 元素
    const gts = [...document.querySelectorAll("[class*=geetest], [id*=geetest]")].slice(0, 15).map(e => e.tagName + "." + (e.className || "").toString().slice(0, 50));
    out["geetest_dom"] = gts;
    return out;
  });
  console.log(JSON.stringify(info, null, 1));
  await browser.close();
})().catch(e => { console.log("ERR", e.message.slice(0, 400)); process.exit(1); });
