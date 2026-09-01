#!/usr/bin/env node
const { loadChromium, sleep } = require("./browser_common.cjs");
(async () => {
  const chromium = loadChromium();
  const browser = await chromium.connectOverCDP("http://127.0.0.1:9222");
  let page = null;
  for (const c of browser.contexts()) for (const p of c.pages()) if (p.url().includes("cx.cnca.cn")) page = p;
  if (!page) { console.log("无 cx 页面"); process.exit(1); }
  // 填充表单
  const filled = await page.evaluate(() => {
    function setSelect(id, val) {
      const s = document.getElementById(id);
      if (!s) return id + ":no";
      s.value = val;
      s.dispatchEvent(new Event("change", { bubbles: true }));
      return id + ":" + val;
    }
    const res = [];
    res.push(setSelect("certItemTwo", "A05"));
    res.push(setSelect("country", "156"));
    res.push(setSelect("certStatus", "01"));
    return res;
  });
  console.log("[1] 填充:", filled.join(", "));
  await sleep(2000);
  // certItemThree 是否级联加载了 A0501
  const three = await page.evaluate(() => {
    const s = document.getElementById("certItemThree");
    return s ? [...s.options].map(o => o.value + "=" + (o.textContent||"").trim().slice(0,20)) : "none";
  });
  console.log("[2] certItemThree 选项:", three);
  // 若 A0501 存在则选中
  if (Array.isArray(three)) {
    const has0501 = three.some(x => x.startsWith("A0501"));
    if (has0501) {
      await page.evaluate(() => {
        const s = document.getElementById("certItemThree");
        s.value = "A0501";
        s.dispatchEvent(new Event("change", { bubbles: true }));
      });
      console.log("[3] 已选 certItemThree=A0501");
    }
  }
  await sleep(1000);
  // 点击查询（真实点击）
  try {
    await page.locator("button.btn-primary").filter({ hasText: "查" }).first().click({ timeout: 8000 });
    console.log("[4] 已点击【查询】，请在 Chrome 中完成滑块验证…");
  } catch(e) {
    console.log("[4] 点击失败:", e.message.slice(0,100));
  }
  await sleep(4000);
  const st = await page.evaluate(() => {
    const gt = document.querySelector("[class*=geetest_captcha]");
    return { visible: gt ? (getComputedStyle(gt).display !== "none" && gt.offsetParent !== null) : false,
             frames: [...document.querySelectorAll("iframe")].map(f => f.src || f.id).slice(0,6),
             captured: (window.__cx_captured || []).length };
  });
  console.log("[5] 滑块状态:", JSON.stringify(st));
  await page.screenshot({ path: "/tmp/cx_filled.png" });
  await browser.close();
})().catch(e => { console.log("ERR", e.message.slice(0,400)); process.exit(1); });
