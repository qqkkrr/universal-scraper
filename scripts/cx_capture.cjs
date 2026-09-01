#!/usr/bin/env node
/* 交互式：headed 打开查询页 → hook fetch/XHR → 自动点查询 → 等人工滑极验4 → 捕获4参数 */
const fs = require("node:fs");
const { CHROMIUM_EXE, loadChromium, sleep } = require("./browser_common.cjs");

(async () => {
  const chromium = loadChromium();
  const browser = await chromium.launch({ headless: false, executablePath: CHROMIUM_EXE,
    args: ["--no-sandbox", "--ignore-certificate-errors"] });
  const ctx = await browser.newContext({ locale: "zh-CN" });
  const page = await ctx.newPage();
  const captured = [];

  // hook fetch + XHR，捕获含 lot_number 的请求
  await page.addInitScript(() => {
    const origFetch = window.fetch;
    window.fetch = function(...args) {
      try {
        const url = typeof args[0] === "string" ? args[0] : (args[0] && args[0].url) || "";
        const opts = args[1] || {};
        let body = "";
        try { body = typeof opts.body === "string" ? opts.body : JSON.stringify(opts.body || {}); } catch(e) {}
        if (/listAuthresult|lot_number|captcha/i.test(url + body)) {
          window.__cx_captured = window.__cx_captured || [];
          window.__cx_captured.push({ url, body, ts: Date.now() });
        }
      } catch(e) {}
      return origFetch.apply(this, args);
    };
    const origOpen = XMLHttpRequest.prototype.open;
    const origSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function(method, url, ...rest) {
      this.__cx_url = url; this.__cx_method = method;
      return origOpen.call(this, method, url, ...rest);
    };
    XMLHttpRequest.prototype.send = function(body) {
      try {
        const url = this.__cx_url || "";
        const b = typeof body === "string" ? body : "";
        if (/listAuthresult|lot_number|captcha/i.test(url + b)) {
          window.__cx_captured = window.__cx_captured || [];
          window.__cx_captured.push({ url, body: b, ts: Date.now() });
        }
      } catch(e) {}
      return origSend.call(this, body);
    };
  });

  await page.goto("https://cx.cnca.cn/CertECloud/result/skipResultList?certItemOne=A", { waitUntil: "domcontentloaded", timeout: 60000 });
  await sleep(6000);
  console.log("[INFO] 页面已打开（headed 窗口），等待 5 秒后自动点击【查询】触发极验滑块…");
  await sleep(5000);
  try {
    await page.evaluate(() => {
      const btns = [...document.querySelectorAll("button, .btn")];
      const q = btns.find(b => (b.textContent || "").trim().includes("查") && (b.textContent || "").trim().includes("询"));
      if (q) q.click();
    });
    console.log("[INFO] 已点击【查询】，请在弹出的窗口中完成滑块验证（若未弹滑块，请手动点页面上的查询按钮）…");
  } catch(e) { console.log("[WARN] 自动点击失败，请手动点击查询按钮:", e.message.slice(0,120)); }

  // 轮询捕获 4 参数（最长 5 分钟等人工）
  const t0 = Date.now();
  let got = null;
  while (Date.now() - t0 < 300000) {
    await sleep(2000);
    try {
      const arr = await page.evaluate(() => window.__cx_captured || []);
      for (const c of arr) {
        const txt = c.url + " " + c.body;
        const m = txt.match(/lot_number=([^&"\\s]+)/) || txt.match(/"lot_number":"([^"]+)"/);
        if (m) {
          const lot_number = m[1];
          const pass = (txt.match(/pass_token=([^&"\\s]+)/) || txt.match(/"pass_token":"([^"]+)"/) || [])[1] || "";
          const gen = (txt.match(/gen_time=([^&"\\s]+)/) || txt.match(/"gen_time":"([^"]+)"/) || [])[1] || "";
          const cap = (txt.match(/captcha_output=([^&"\\s]+)/) || txt.match(/"captcha_output":"([^"]+)"/) || [])[1] || "";
          if (lot_number && pass && gen && cap) {
            got = { lot_number, pass_token: pass, gen_time: gen, captcha_output: cap, raw_url: c.url.slice(0,200), raw_body: c.body.slice(0,500) };
            break;
          }
        }
      }
      if (got) break;
    } catch(e) {}
  }
  if (got) {
    console.log("[OK] 已捕获极验4 参数:");
    console.log(JSON.stringify({lot_number: got.lot_number, pass_token: got.pass_token, gen_time: got.gen_time, captcha_output: got.captcha_output}, null, 1));
    fs.writeFileSync("/tmp/cx_validate.json", JSON.stringify(got, null, 1), "utf-8");
    // 立即用参数测第 1 页（页面上下文，带 cookie）
    const test = await page.evaluate(async (v) => {
      const q = new URLSearchParams({ certItemOne:"A", certItemTwo:"A05", certItemThree:"A0501", country:"156", certStatus:"01", pageNum:"1", pageSize:"5",
        lot_number:v.lot_number, pass_token:v.pass_token, gen_time:v.gen_time, captcha_output:v.captcha_output });
      const r = await fetch("/CertECloud/result/listAuthresult?" + q.toString());
      const j = await r.json();
      return { status: r.status, pageCount: j.pageCount, total: j.obj && j.obj.info, rows: (j.rows||[]).length, sample: (j.rows||[])[0] || null };
    }, got);
    console.log("[TEST] 带参数测第1页 →", JSON.stringify(test).slice(0, 500));
  } else {
    console.log("[FAIL] 5 分钟内未捕获到验证参数（可能未完成滑块）");
  }
  await browser.close();
})().catch(e => { console.log("ERR", e.message.slice(0, 500)); process.exit(1); });
