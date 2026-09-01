#!/usr/bin/env node
/* 捕获极验4参数 v2：page.on('request') 抓所有请求（含iframe） */
const fs = require("node:fs");
const { CHROMIUM_EXE, loadChromium, sleep } = require("./browser_common.cjs");

(async () => {
  const chromium = loadChromium();
  const browser = await chromium.launch({ headless: false, executablePath: CHROMIUM_EXE,
    args: ["--no-sandbox", "--ignore-certificate-errors"] });
  const ctx = await browser.newContext({ locale: "zh-CN" });
  const page = await ctx.newPage();
  const hits = [];
  page.on("request", (req) => {
    const url = req.url();
    if (/listAuthresult|captcha|validate/i.test(url)) {
      const post = req.postData() || "";
      hits.push({ url: url.slice(0, 300), post: post.slice(0, 600), ts: Date.now() });
    }
  });
  await page.goto("https://cx.cnca.cn/CertECloud/result/skipResultList?certItemOne=A", { waitUntil: "domcontentloaded", timeout: 60000 });
  await sleep(6000);
  console.log("[1] 页面已打开，5 秒后自动点击【查询】…");
  await sleep(5000);
  try {
    await page.evaluate(() => {
      const btns = [...document.querySelectorAll("button, .btn")];
      const q = btns.find(b => (b.textContent || "").trim().includes("查") && (b.textContent || "").trim().includes("询"));
      if (q) { q.click(); return "clicked"; }
      return "no btn";
    });
  } catch(e) { console.log("[WARN] 自动点击失败:", e.message.slice(0,100)); }
  console.log("[2] 已触发查询，请在弹出的浏览器窗口完成【滑块验证】（拖动滑块到指定位置）。等待最多 5 分钟…");
  const t0 = Date.now();
  let got = null;
  while (Date.now() - t0 < 300000) {
    await sleep(2000);
    for (const h of hits) {
      const txt = h.url + " " + h.post;
      const lot = (txt.match(/lot_number=([^&"\\s]+)/) || txt.match(/"lot_number":"([^"]+)"/) || [])[1];
      if (lot) {
        const pass = (txt.match(/pass_token=([^&"\\s]+)/) || txt.match(/"pass_token":"([^"]+)"/) || [])[1] || "";
        const gen = (txt.match(/gen_time=([^&"\\s]+)/) || txt.match(/"gen_time":"([^"]+)"/) || [])[1] || "";
        const cap = (txt.match(/captcha_output=([^&"\\s]+)/) || txt.match(/"captcha_output":"([^"]+)"/) || [])[1] || "";
        if (pass && gen && cap) {
          got = { lot_number: lot, pass_token: pass, gen_time: gen, captcha_output: cap };
          break;
        }
      }
    }
    if (got) break;
  }
  if (got) {
    console.log("[OK] 捕获参数:", JSON.stringify(got));
    fs.writeFileSync("/tmp/cx_validate.json", JSON.stringify(got, null, 1), "utf-8");
    const test = await page.evaluate(async (v) => {
      const q = new URLSearchParams({ certItemOne:"A", certItemTwo:"A05", certItemThree:"A0501", country:"156", certStatus:"01", pageNum:"1", pageSize:"5",
        lot_number:v.lot_number, pass_token:v.pass_token, gen_time:v.gen_time, captcha_output:v.captcha_output });
      const r = await fetch("/CertECloud/result/listAuthresult?" + q.toString());
      const j = await r.json();
      return { status: r.status, pageCount: j.pageCount, rows: (j.rows||[]).length, sample: (j.rows||[])[0] };
    }, got);
    console.log("[TEST] 带参数第1页 →", JSON.stringify(test).slice(0, 600));
  } else {
    console.log("[FAIL] 未捕获。近 30 条请求:");
    hits.slice(-30).forEach(h => console.log("   ", h.url.slice(0, 120), "|", h.post.slice(0, 80)));
  }
  await browser.close();
})().catch(e => { console.log("ERR", e.message.slice(0, 500)); process.exit(1); });
