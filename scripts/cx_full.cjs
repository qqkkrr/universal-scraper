#!/usr/bin/env node
/* cx.cnca.cn A05 全量采集：交互过极验4 → token测试 → 全量分页 → 落盘 */
const fs = require("node:fs");
const path = require("node:path");
const { CHROMIUM_EXE, loadChromium, sleep } = require("./browser_common.cjs");

const OUT_DIR = "/Users/kairanqin/Documents/Codex/2026-08-03/ni-shi/iso27001";
const RAW_JSONL = path.join(OUT_DIR, "iso27001_certs_raw.jsonl");
const QUERY = { certItemOne:"A", certItemTwo:"A05", certItemThree:"A0501", country:"156", certStatus:"01" };
const PAGE_SIZE = 100;
const WAIT = 2500;          // 单请求间隔 ≥2s
const MAX_RETRY = 3;
const BLOCK_WAIT = 600000;  // 521 后等 10 分钟

async function api(page, validate, pageNum) {
  const q = new URLSearchParams({ ...QUERY, pageNum: String(pageNum), pageSize: String(PAGE_SIZE),
    lot_number: validate.lot_number, pass_token: validate.pass_token,
    gen_time: validate.gen_time, captcha_output: validate.captcha_output });
  return page.evaluate(async (qs) => {
    const r = await fetch("/CertECloud/result/listAuthresult?" + qs, { headers: { "Accept": "application/json" } });
    if (r.status === 521) return { blocked: true };
    try { return await r.json(); } catch(e) { return { parse_err: String(e) }; }
  }, q.toString());
}

(async () => {
  const chromium = loadChromium();
  const browser = await chromium.launch({ headless: false, executablePath: CHROMIUM_EXE,
    args: ["--no-sandbox", "--ignore-certificate-errors"] });
  const ctx = await browser.newContext({ locale: "zh-CN" });
  const page = await ctx.newPage();
  const hits = [];
  page.on("request", (req) => {
    const url = req.url();
    if (/listAuthresult|captcha/i.test(url)) hits.push({ url, post: req.postData() || "" });
  });

  // 1) 打开页面（过 JS challenge）
  await page.goto("https://cx.cnca.cn/CertECloud/result/skipResultList?certItemOne=A", { waitUntil: "domcontentloaded", timeout: 60000 });
  await sleep(6000);
  // 2) 已有验证参数？没有则交互捕获
  let validate = null;
  if (fs.existsSync("/tmp/cx_validate.json")) {
    try { validate = JSON.parse(fs.readFileSync("/tmp/cx_validate.json", "utf-8")); } catch(e) {}
  }
  if (!validate || !validate.pass_token) {
    console.log("[!] 请在弹出的浏览器窗口操作：若滑块已弹出请直接完成；若没有，请点击页面【查询】按钮触发。等待中…");
    try {
      await page.evaluate(() => {
        const btns = [...document.querySelectorAll("button, .btn")];
        const q = btns.find(b => (b.textContent || "").trim().includes("查") && (b.textContent || "").trim().includes("询"));
        if (q) q.click();
      });
    } catch(e) {}
    const t0 = Date.now();
    while (Date.now() - t0 < 600000) {
      await sleep(2000);
      for (const h of hits) {
        const txt = h.url + " " + h.post;
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
    }
    if (!validate || !validate.pass_token) { console.log("[FAIL] 10 分钟内未完成滑块"); await browser.close(); process.exit(1); }
    fs.writeFileSync("/tmp/cx_validate.json", JSON.stringify(validate, null, 1), "utf-8");
    console.log("[OK] 已捕获极验4 参数:", JSON.stringify({lot_number: validate.lot_number.slice(0,12)+"...", pass_token: validate.pass_token.slice(0,12)+"...", gen_time: validate.gen_time}));
  } else {
    console.log("[OK] 复用已有验证参数:", JSON.stringify({lot_number: validate.lot_number.slice(0,12)+"...", gen_time: validate.gen_time}));
  }

  // 3) token 有效期测试：第 1/2/3 页，间隔 40s
  console.log("[TEST] token 有效期测试：连抓第 1/2/3 页（间隔 40s）…");
  const pc = [];
  for (const pn of [1, 2, 3]) {
    let j = null;
    for (let r = 0; r < MAX_RETRY && !j; r++) {
      j = await api(page, validate, pn);
      if (j && j.blocked) { console.log("[BLOCK] 521 封禁，等待 10 分钟…"); await sleep(BLOCK_WAIT); }
      if (!j || (j.parse_err)) { console.log(`[WARN] 第${pn}页异常，重试`); j = null; await sleep(5000); }
    }
    pc.push(j ? (j.pageCount ?? -1) : -1);
    console.log(`   page ${pn}: pageCount=${j ? j.pageCount : "FAIL"}`);
    await sleep(40000);
  }
  const tokenOK = pc.every(x => x > 0);
  console.log("[TEST] token 复用结论:", tokenOK ? "✅ 同一 token 可连抓多页" : "⚠️ token 短效，需每页重过");
  if (!tokenOK) { console.log("[FAIL] token 测试未通过，请重新运行并再次完成滑块"); await browser.close(); process.exit(1); }

  // 4) 全量分页
  const first = await api(page, validate, 1);
  const totalPages = first.pageCount || 0;
  const totalRows = first.obj && first.obj.info;
  console.log(`[FETCH] 总页数=${totalPages}，页面信息=${JSON.stringify(totalRows)}`);
  const all = [];
  fs.writeFileSync(RAW_JSONL, "", "utf-8");
  for (let pn = 1; pn <= totalPages; pn++) {
    let j = null;
    for (let r = 0; r < MAX_RETRY && !j; r++) {
      j = await api(page, validate, pn);
      if (j && j.blocked) { console.log(`[BLOCK] 第${pn}页 521，等待 10 分钟…`); await sleep(BLOCK_WAIT); }
      if (!j || j.parse_err) { j = null; await sleep(5000); }
    }
    if (!j) { console.log(`[WARN] 第${pn}页最终失败，跳过`); continue; }
    const rows = j.rows || [];
    all.push(...rows);
    fs.appendFileSync(RAW_JSONL, rows.map(r => JSON.stringify(r)).join("\n") + (rows.length ? "\n" : ""), "utf-8");
    if (pn % 10 === 0 || pn === totalPages) console.log(`   第${pn}/${totalPages}页，累计 ${all.length} 条`);
    await sleep(WAIT);
  }
  console.log(`[DONE] 抓取完成：${all.length} 条原始记录（pageCount=${totalPages}）`);
  // 简单去重统计
  const seen = new Set(); let dup = 0;
  for (const r of all) { const k = (r.orgName||"") + "|" + (r.certNumber||""); if (seen.has(k)) dup++; seen.add(k); }
  console.log(`[DONE] 去重后 ${seen.size} 条（重复 ${dup}）`);
  await browser.close();
})().catch(e => { console.log("ERR", e.message.slice(0, 500)); process.exit(1); });
