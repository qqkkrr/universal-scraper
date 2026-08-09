#!/usr/bin/env node
/**
 * ggzy.gov.cn 全国公共资源交易平台 — Playwright 浏览器桥
 *
 * 目标站有 WAF（对非浏览器客户端返回 404），必须用真实浏览器。
 * 本桥在浏览器里加载历史交易列表页，通过页面 Vue 实例设置查询条件并分页抓取，
 * 结果以 JSONL 流式输出到 stdout：
 *   {"type":"meta","total":N,"pages":N}
 *   {"type":"page","page":N,"records":[...]}
 *   {"type":"captcha","message":"..."}
 *   {"type":"done"}
 */
let chromium = null;
// 优先 NODE_PATH 的 playwright（本地 patchright 旧版可能被 WAF 识别），再回退本地 patchright
const _path = require("node:path");
const _nps = (process.env.NODE_PATH || "").split(":").filter(Boolean);
const _cands = _nps.map((p) => _path.join(p, "playwright")).concat(["patchright", "playwright"]);
for (const _c of _cands) {
  try { chromium = require(_c).chromium; break; } catch (e) { /* 下一个 */ }
}
if (!chromium) throw new Error("找不到 playwright/patchright");
const fs = require("node:fs");
// (fs unused)

const os = require("node:os");
const HOME = process.env.HOME || os.homedir() || "/tmp";
const EXE = process.env.PW_EXECUTABLE || `${HOME}/Library/Caches/ms-playwright/chromium_headless_shell-1208/chrome-headless-shell-mac-arm64/chrome-headless-shell`;
const HISTORY_URL = "https://www.ggzy.gov.cn/history/dealList.html";
const API_PATH = "/his/information/pubTradingInfo/getTradList";

function saveCaptchaImage(page, prefix, captchaOut) {
  return page.evaluate(() => {
    const vm = document.querySelector("#app").__vue__;
    const imgEl = document.querySelector("#validationCodeImg");
    return { img: imgEl ? imgEl.src : null, token: vm.captchaToken || null, code: vm.verifyCode || null };
  }).then(async ({ img, token }) => {
    if (img && captchaOut) {
      const b64 = img.startsWith("data:") ? img.substring(img.indexOf(",") + 1) : img;
      const buf = Buffer.from(b64, "base64");
      const f = `${captchaOut}/${prefix}_captcha.png`;
      fs.writeFileSync(f, buf);
      return { file: f, token };
    }
    return { file: null, token };
  });
}

let captchaSeq = 0;

async function saveCaptchaImageFile(page, dir) {
  captchaSeq += 1;
  const file = `${dir}/captcha_${captchaSeq}.png`;
  const info = await page.evaluate(() => {
    const vm = document.querySelector("#app").__vue__;
    const imgEl = document.querySelector("#validationCodeImg") || document.querySelector("#verifyCodeModal img");
    return { src: imgEl ? imgEl.src : null, token: vm.captchaToken || "" };
  });
  let buf = null;
  if (info.src && info.src.startsWith("data:")) {
    buf = Buffer.from(info.src.substring(info.src.indexOf(",") + 1), "base64");
  } else if (info.src) {
    try {
      const ab = await page.evaluate(async (u) => {
        const r = await fetch(u);
        const b = await r.blob();
        return Array.from(new Uint8Array(await b.arrayBuffer()));
      }, info.src);
      buf = Buffer.from(ab);
    } catch (e) { buf = null; }
  }
  if (buf && buf.length > 0) {
    fs.writeFileSync(file, buf);
    return { file, token: info.token };
  }
  return { file: null, token: info.token };
}

async function solveCaptchaAndRetry(page, getListPage, captchaDir, captchaTimeout, maxTries = 3) {
  for (let t = 1; t <= maxTries; t++) {
    const cap = await saveCaptchaImageFile(page, captchaDir);
    if (!cap.file) { out({ type: "captcha", message: "验证码图片为空", token: cap.token }); return false; }
    const answerFile = `${cap.file}.answer`;
    try { fs.unlinkSync(answerFile); } catch (e) {}
    out({ type: "captcha", imageFile: cap.file, token: cap.token, seq: captchaSeq });
    // 等 Python 侧写好答案
    const deadline = Date.now() + captchaTimeout;
    let answer = null;
    while (Date.now() < deadline) {
      if (fs.existsSync(answerFile)) {
        const a = fs.readFileSync(answerFile, "utf-8").trim();
        if (a) { answer = a; break; }
      }
      await sleep(1000);
    }
    if (!answer) { out({ type: "captcha_timeout", seq: captchaSeq }); return false; }
    // 填答案重试
    await page.evaluate(({ token, code, pn }) => {
      const vm = document.querySelector("#app").__vue__;
      if (token) vm.captchaToken = token;
      vm.verifyCode = code;
      vm.getList(pn, code);
    }, { token: cap.token, code: answer, pn: getListPage });
    const st = await waitListReady(page, getListPage);
    if (st.captcha) { out({ type: "captcha_retry", attempt: t }); continue; }
    const api = lastApiRef();
    if (isRateLimited(api)) { await sleep(30000); continue; }
    return st.ok;
  }
  return false;
}

function parseArgs(argv) {
  const a = {};
  for (let i = 2; i < argv.length; i++) {
    const k = argv[i];
    if (k.startsWith("--")) a[k.slice(2)] = argv[i + 1];
  }
  return a;
}

const out = (obj) => console.log(JSON.stringify(obj));

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function getVue(page) {
  // 只返回布尔（Vue 实例有循环引用，patchright 序列化不可靠）
  return page.evaluate(() => {
    const app = document.querySelector("#app");
    return !!(app && app.__vue__);
  });
}

function lastApiRef() { return globalThis.__lastApi ? globalThis.__lastApi() : null; }

function isRateLimited(j) {
  return j && (j.code === 800 || j.code === 801 || j.code === 804);
}

async function getListWithRetry(page, wantPage, getLastApi, maxRetries = 6, deadline = 0) {
  for (let a = 1; a <= maxRetries; a++) {
    if (deadline > 0 && Date.now() > deadline) {
      out({ type: "deadline", page: wantPage, attempt: a });
      return { ok: false, captcha: false, deadline: true };
    }
    await page.evaluate((pn) => { document.querySelector("#app").__vue__.getList(pn); }, wantPage);
    const st = await waitListReady(page, wantPage);
    if (st.captcha) return { ok: false, captcha: true };
    const api = getLastApi();
    if (st.ok && isRateLimited(api)) {
      out({ type: "ratelimited", attempt: a, code: api.code, waitSec: 45 });
      await sleep(45000);
      continue;
    }
    if (!st.ok) {
      // 页面等待超时（WAF 限流/接口慢）：先退避再重试，避免高频硬刚
      out({ type: "retry_wait", attempt: a, waitSec: 8 });
      await sleep(8000);
      continue;
    }
    return st;
  }
  return { ok: false, captcha: false, ratelimited: true };
}

async function waitListReady(page, wantPage, timeoutMs = 40000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    const st = await page.evaluate((want) => {
      const vm = document.querySelector("#app").__vue__;
      return { loading: vm.loading, cur: vm.currentPage, verify: !!vm.showVerify, captcha: !!vm.showVerify };
    }, wantPage);
    if (st.captcha || st.verify) return { ok: false, captcha: true };
    if (!st.loading && st.cur === wantPage) return { ok: true, captcha: false };
    await sleep(400);
  }
  return { ok: false, captcha: false };
}

async function main() {
  const args = parseArgs(process.argv);
  const keyword = args.keyword || "";
  const begin = args.begin || "";
  const end = args.end || "";
  const stage = args.stage || "0001";
  const maxPages = parseInt(args.maxpages || "200", 10);
  const settleMs = parseInt(args.settle || "1200", 10);
  const deadlineMs = parseInt(args.deadlineMs || "360000", 10);
  const deadline = Date.now() + deadlineMs;
  const captchaOut = args.captchaOut || null;
  const captchaDir = args.captchaDir || null;
  const captchaTimeout = parseInt(args.captchaTimeout || "300000", 10);

  let browser = null;
  try {
    if (captchaDir) fs.mkdirSync(captchaDir, { recursive: true });
    browser = await chromium.launch({ headless: true, executablePath: EXE, args: ["--no-sandbox", "--ignore-certificate-errors"] });
    // 关键：WAF 对 HeadlessChrome UA 直接返回拦截页（无 #app），必须用真实 Chrome UA
    const ctx = await browser.newContext({
      userAgent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
      locale: "zh-CN",
      viewport: { width: 1440, height: 900 },
    });
    const page = await ctx.newPage();
    let lastApi = null;
    globalThis.__lastApi = () => lastApi;
    page.on("response", (res) => {
      if (res.url().includes("getTradList") && res.status() === 200) {
        res.json().then((j) => { lastApi = j; }).catch(() => {});
      }
    });
    await page.goto(HISTORY_URL, { timeout: 45000, waitUntil: "networkidle" });
    // Vue 挂载可能慢于 networkidle：先给足时间，再按需重载重试
    await sleep(5000);

    let vm = await getVue(page);
    for (let _r = 1; !vm && _r <= 3; _r++) {
      await sleep(3000);
      try { await page.reload({ timeout: 45000, waitUntil: "networkidle" }); } catch (e) {}
      await sleep(5000);
      vm = await getVue(page);
    }
    if (!vm) { out({ type: "error", message: "无法获取页面 Vue 实例（WAF 拦截页，重试3次后仍失败）" }); process.exit(1); }

    // 设置查询条件
    await page.evaluate(({ keyword, begin, end, stage }) => {
      const vm = document.querySelector("#app").__vue__;
      if (keyword) vm.myFindTxt = keyword;
      if (begin) vm.timeBegin = begin;
      if (end) vm.timeEnd = end;
      if (stage) vm.myDealStage = stage;
    }, { keyword, begin, end, stage });

    // 请求第 1 页，拿到总数/总页数
    let first = await getListWithRetry(page, 1, () => lastApi, 6, deadline);
    if (first.captcha) {
      if (!captchaDir) { out({ type: "captcha", message: "第 1 页触发验证码（未配置 captchaDir，无法自动解）" }); process.exit(0); }
      const solved = await solveCaptchaAndRetry(page, 1, captchaDir, captchaTimeout);
      if (!solved) { out({ type: "error", message: "验证码未能解出，已放弃" }); process.exit(1); }
      first = await getListWithRetry(page, 1, () => lastApi, 6, deadline);
      if (first.captcha) { out({ type: "captcha", message: "第 1 页仍触发验证码" }); process.exit(0); }
    }
    if (!first.ok) { out({ type: "error", message: "第 1 页等待超时" }); process.exit(1); }

    const meta = await page.evaluate(() => { const v = document.querySelector("#app").__vue__; return { total: v.ttlrow, pages: v.ttlpage, current: v.currentPage }; });
    out({ type: "meta", total: meta.total, pages: meta.pages });
    const pages = Math.min(meta.pages || 1, maxPages);

    for (let p = 1; p <= pages; p++) {
      if (deadline > 0 && Date.now() > deadline) {
        out({ type: "error", message: `整体时限到期（${deadlineMs / 1000}s），已抓 ${p - 1} 页` });
        process.exit(0);
      }
      if (p > 1) {
        await sleep(settleMs);
        let st = await getListWithRetry(page, p, () => lastApi, 6, deadline);
        if (st.captcha) {
          if (!captchaDir) { out({ type: "captcha", message: `第 ${p} 页触发验证码` }); process.exit(0); }
          const solved = await solveCaptchaAndRetry(page, p, captchaDir, captchaTimeout);
          if (!solved) { out({ type: "error", message: `第 ${p} 页验证码未解出` }); process.exit(1); }
          st = await getListWithRetry(page, p, () => lastApi, 6, deadline);
        }
        if (!st.ok) { out({ type: "error", message: `第 ${p} 页等待超时` }); process.exit(1); }
      }
      const records = await page.evaluate(() => document.querySelector("#app").__vue__.records || []);
      out({ type: "page", page: p, count: records.length, records });
    }
    out({ type: "done", total: meta.total, fetchedPages: pages });
  } catch (e) {
    out({ type: "error", message: String(e && e.message || e) });
    process.exit(1);
  } finally {
    if (browser) await browser.close();
  }
}

main();
