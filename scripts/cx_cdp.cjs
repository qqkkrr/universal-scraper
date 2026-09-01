#!/usr/bin/env node
/* cx.cnca.cn A05 全量采集（CDP 附着系统 Chrome）：注入hook → 触发查询 → 人工滑极验4 → token测试 → 全量 */
const fs = require("node:fs");
const path = require("node:path");
const { loadChromium, sleep } = require("./browser_common.cjs");

const OUT_DIR = "/Users/kairanqin/Documents/Codex/2026-08-03/ni-shi/iso27001";
const RAW_JSONL = path.join(OUT_DIR, "iso27001_certs_raw.jsonl");
const QUERY = { certItemOne:"A", certItemTwo:"A05", certItemThree:"A0501", country:"156", certStatus:"01" };
const PAGE_SIZE = 100;
const WAIT = 2500, MAX_RETRY = 3, BLOCK_WAIT = 600000;

(async () => {
  const chromium = loadChromium();
  const browser = await chromium.connectOverCDP("http://127.0.0.1:9222");
  let ctx = null, page = null;
  for (const c of browser.contexts()) {
    for (const p of c.pages()) {
      if (p.url().includes("cx.cnca.cn")) { ctx = c; page = p; break; }
    }
    if (page) break;
  }
  if (!page) {
    // 打开新 tab
    ctx = browser.contexts()[0] || await browser.newContext();
    page = await ctx.newPage();
    await page.goto("https://cx.cnca.cn/CertECloud/result/skipResultList?certItemOne=A", { waitUntil: "domcontentloaded", timeout: 60000 });
  }
  await sleep(4000);
  console.log("[1] 已附着页面:", page.url().slice(0, 80));

  // 注入 hook（捕获带验证参数的请求）
  await page.evaluate(() => {
    window.__cx_captured = [];
    const origFetch = window.fetch;
    window.fetch = function(...args) {
      try {
        const url = typeof args[0] === "string" ? args[0] : (args[0] && args[0].url) || "";
        const opts = args[1] || {};
        let body = "";
        try { body = typeof opts.body === "string" ? opts.body : JSON.stringify(opts.body || {}); } catch(e) {}
        if (/listAuthresult|lot_number/i.test(url + body)) {
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
        if (/listAuthresult|lot_number/i.test(url + b)) {
          window.__cx_captured = window.__cx_captured || [];
          window.__cx_captured.push({ url, body: b, ts: Date.now() });
        }
      } catch(e) {}
      return origSend.call(this, body);
    };
    return "hook injected";
  });
  console.log("[2] hook 已注入。将自动点击【查询】触发滑块——【请在弹出的 Chrome 窗口里完成滑块验证】…");
  await sleep(1500);
  try {
    const clicked = await page.evaluate(() => {
      const btns = [...document.querySelectorAll("button, .btn")];
      const q = btns.find(b => (b.textContent || "").trim().includes("查") && (b.textContent || "").trim().includes("询"));
      if (q) { q.click(); return "clicked"; }
      return "no btn";
    });
    console.log("[3] 自动点击结果:", clicked);
  } catch(e) { console.log("[WARN] 自动点击失败，请手动点【查询】:", e.message.slice(0,100)); }

  // 等人工滑块（最长 10 分钟）
  let validate = null;
  const t0 = Date.now();
  while (Date.now() - t0 < 600000) {
    await sleep(2000);
    try {
      const arr = await page.evaluate(() => window.__cx_captured || []);
      for (const c of arr) {
        const txt = c.url + " " + c.body;
        const lot = (txt.match(/lot_number=([^&"\\s]+)/) || txt.match(/"lot_number":"([^"]+)"/) || [])[1];
        if (lot) {
          validate = {
            lot_number: lot,
            pass_token: (txt.match(/pass_token=([^&"\\s]+)/) || txt.match(/"pass_token":"([^"]+)"/) || [])[1] || "",
            gen_time: (txt.match(/gen_time=([^&"\\s]+)/) || txt.match(/"gen_time":"([^"]+)"/) || [])[1] || "",
            captcha_output: (txt.match(/captcha_output=([^&"\\s]+)/) || txt.match(/"captcha_output":"([^"]+)"/) || [])[1] || "",
          };
          if (validate.pass_token && validate.gen_time && validate.captcha_output) break;
        }
      }
      if (validate && validate.pass_token) break;
    } catch(e) {}
  }
  if (!validate || !validate.pass_token) {
    console.log("[FAIL] 10 分钟内未捕获验证参数");
    const arr = await page.evaluate(() => window.__cx_captured || []).catch(() => []);
    arr.slice(-20).forEach(c => console.log("   ", c.url.slice(0, 100), "|", c.body.slice(0, 80)));
    process.exit(1);
  }
  console.log("[OK] 捕获验证参数:", JSON.stringify({lot_number: validate.lot_number.slice(0,12)+"...", gen_time: validate.gen_time}));
  fs.writeFileSync("/tmp/cx_validate.json", JSON.stringify(validate, null, 1), "utf-8");

  async function api(pageNum) {
    const q = new URLSearchParams({ ...QUERY, pageNum: String(pageNum), pageSize: String(PAGE_SIZE),
      lot_number: validate.lot_number, pass_token: validate.pass_token,
      gen_time: validate.gen_time, captcha_output: validate.captcha_output });
    return page.evaluate(async (qs) => {
      const r = await fetch("/CertECloud/result/listAuthresult?" + qs, { headers: { "Accept": "application/json" } });
      if (r.status === 521) return { blocked: true };
      try { return await r.json(); } catch(e) { return { parse_err: String(e) }; }
    }, q.toString());
  }

  // token 有效期测试
  console.log("[TEST] token 有效期：连抓第 1/2/3 页（间隔 40s）…");
  const pc = [];
  for (const pn of [1, 2, 3]) {
    let j = null;
    for (let r = 0; r < MAX_RETRY && !j; r++) {
      j = await api(pn);
      if (j && j.blocked) { console.log("[BLOCK] 521，等 10 分钟"); await sleep(BLOCK_WAIT); }
      if (!j || j.parse_err) { j = null; await sleep(5000); }
    }
    pc.push(j ? (j.pageCount ?? -1) : -1);
    console.log(`   page ${pn}: pageCount=${j ? j.pageCount : "FAIL"}`);
    await sleep(40000);
  }
  const tokenOK = pc.every(x => x > 0);
  console.log("[TEST] 结论:", tokenOK ? "✅ 同一 token 可连抓多页" : "⚠️ token 短效");
  if (!tokenOK) { console.log("[FAIL] token 测试未通过"); process.exit(1); }

  // 全量
  const first = await api(1);
  const totalPages = first.pageCount || 0;
  console.log(`[FETCH] 总页数=${totalPages}`);
  const all = [];
  fs.writeFileSync(RAW_JSONL, "", "utf-8");
  for (let pn = 1; pn <= totalPages; pn++) {
    let j = null;
    for (let r = 0; r < MAX_RETRY && !j; r++) {
      j = await api(pn);
      if (j && j.blocked) { console.log(`[BLOCK] 第${pn}页 521，等 10 分钟`); await sleep(BLOCK_WAIT); }
      if (!j || j.parse_err) { j = null; await sleep(5000); }
    }
    if (!j) { console.log(`[WARN] 第${pn}页失败`); continue; }
    const rows = j.rows || [];
    all.push(...rows);
    fs.appendFileSync(RAW_JSONL, rows.map(r => JSON.stringify(r)).join("\n") + (rows.length ? "\n" : ""), "utf-8");
    if (pn % 10 === 0 || pn === totalPages) console.log(`   第${pn}/${totalPages}页，累计 ${all.length} 条`);
    await sleep(WAIT);
  }
  const seen = new Set(); let dup = 0;
  for (const r of all) { const k = (r.orgName||"") + "|" + (r.certNumber||""); if (seen.has(k)) dup++; seen.add(k); }
  console.log(`[DONE] 抓取 ${all.length} 条，去重 ${seen.size} 条（重复 ${dup}）`);
  await browser.close();
})().catch(e => { console.log("ERR", e.message.slice(0, 500)); process.exit(1); });
