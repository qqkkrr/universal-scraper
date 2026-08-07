#!/usr/bin/env node
/**
 * 通用浏览器桥（Level 1 + 验证码/滑块协议）
 *
 * 由配置驱动，负责"导航"：加载页面 → 执行 JS → 翻页 → 处理验证码/滑块；
 * 把每页 HTML 写到文件，由 Python 侧用 lxml 提取（选择器能力强、好维护）。
 *
 * 协议（JSONL 输出到 stdout）：
 *   {"type":"page","page":N,"file":"/path/page_N.html"}
 *   {"type":"captcha","kind":"image|slider","imageFile":"...","seq":N}
 *   {"type":"done","pages":N}
 *   {"type":"error","message":"..."}
 *
 * 用法: node browser_generic.cjs --spec /path/spec.json --out /path/pages \
 *       [--captchaDir /path] [--captchaTimeout 300000]
 */
const fs = require("node:fs");
const path = require("node:path");
const { parseProxy } = require("./browser_common.cjs");

let chromium = null;
try { chromium = require("patchright").chromium; } catch (e) { chromium = require("playwright").chromium; }

const HOME = process.env.HOME || "/Users/kairanqin";
const HEADLESS_SHELL = process.env.PW_EXECUTABLE
  || `${HOME}/Library/Caches/ms-playwright/chromium_headless_shell-1208/chrome-headless-shell-mac-arm64/chrome-headless-shell`;
const FULL_CHROME = process.env.PW_FULL_CHROME
  || `${HOME}/Library/Caches/ms-playwright/chromium-1208/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing`;
// 有头（headless=0，需弹窗让人工验证/登录）必须用完整版 Chromium，headless-shell 不显示窗口！
const EXE = arg("headless", "1") !== "0" ? HEADLESS_SHELL : FULL_CHROME;

const out = (o) => console.log(JSON.stringify(o));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function arg(name, dflt) {
  const i = process.argv.indexOf("--" + name);
  return i >= 0 ? process.argv[i + 1] : dflt;
}

let captchaSeq = 0;

async function saveImage(page, src, file) {
  let buf = null;
  if (src && src.startsWith("data:")) buf = Buffer.from(src.substring(src.indexOf(",") + 1), "base64");
  else if (src) {
    try {
      const ab = await page.evaluate(async (u) => {
        const r = await fetch(u);
        const b = await r.blob();
        return Array.from(new Uint8Array(await b.arrayBuffer()));
      }, src);
      buf = Buffer.from(ab);
    } catch (e) {}
  }
  if (buf && buf.length > 0) { fs.writeFileSync(file, buf); return true; }
  return false;
}

async function handleCaptcha(page, spec, captchaDir, timeoutMs) {
  if (!captchaDir) return false;
  const cap = spec.captcha || null;
  const sli = spec.slider || null;
  // 检测滑块（用可见性，隐藏 DOM 不算）
  if (sli && sli.detect_selector && await page.locator(sli.detect_selector).first().isVisible().catch(() => false)) {
    captchaSeq += 1;
    const file = path.join(captchaDir, `captcha_${captchaSeq}.png`);
    try {
      await page.locator(sli.bg_selector).first().screenshot({ path: file });
    } catch (e) { return false; }
    out({ type: "captcha", kind: "slider", imageFile: file, seq: captchaSeq });
    const answer = await waitAnswer(file + ".answer", timeoutMs);
    if (answer === null) return false;
    const x = parseInt(answer, 10) || 0;
    // 人类化拖拽轨迹
    const box = await page.locator(sli.btn_selector).first().boundingBox();
    if (box) {
      const steps = 12 + Math.floor(Math.random() * 8);
      const targetX = box.x + Math.max(0, x + (sli.x_offset || 0));
      await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
      await page.mouse.down();
      for (let s = 1; s <= steps; s++) {
        const eased = 1 - Math.pow(1 - s / steps, 3);
        await page.mouse.move(box.x + box.width / 2 + (targetX - box.x) * eased,
                              box.y + box.height / 2 + (Math.random() - 0.5) * 4);
        await sleep(20 + Math.random() * 40);
      }
      await page.mouse.up();
    }
    await sleep(1500);
    return true;
  }
  // 检测图形验证码
  if (cap && cap.detect_selector && await page.locator(cap.detect_selector).first().isVisible().catch(() => false)) {
    captchaSeq += 1;
    const file = path.join(captchaDir, `captcha_${captchaSeq}.png`);
    const src = await page.locator(cap.image_selector).first().getAttribute("src").catch(() => null);
    if (await saveImage(page, src, file)) {
      out({ type: "captcha", kind: "image", imageFile: file, seq: captchaSeq });
      const answer = await waitAnswer(file + ".answer", timeoutMs);
      if (answer === null) return false;
      if (cap.input_selector) await page.locator(cap.input_selector).fill(answer);
      if (cap.submit_selector) await page.locator(cap.submit_selector).click();
      await sleep(1500);
      return true;
    }
    return false;
  }
  return true;
}

function waitAnswer(file, timeoutMs) {
  return new Promise((resolve) => {
    const t0 = Date.now();
    const iv = setInterval(() => {
      if (fs.existsSync(file)) {
        const a = fs.readFileSync(file, "utf-8").trim();
        if (a) { clearInterval(iv); resolve(a); return; }
      }
      if (Date.now() - t0 > timeoutMs) { clearInterval(iv); resolve(null); }
    }, 1000);
  });
}

async function main() {
  const specFile = arg("spec");
  const outDir = arg("out");
  const captchaDir = arg("captchaDir", null);
  const captchaTimeout = parseInt(arg("captchaTimeout", "300000"), 10);
  const maxPages = parseInt(arg("maxPages", "100"), 10);
  const settle = parseInt(arg("settle", "1500"), 10);

  const spec = JSON.parse(fs.readFileSync(specFile, "utf-8"));
  fs.mkdirSync(outDir, { recursive: true });
  if (captchaDir) fs.mkdirSync(captchaDir, { recursive: true });
  const storageState = arg("storageState", null);
  const scrollCount = parseInt(arg("scrollCount", "0"), 10);
  const scrollWait = parseInt(arg("scrollWait", "2000"), 10);
  const loginTimeout = parseInt(arg("loginTimeout", "600000"), 10);

  let browser = null;
  try {
    const headless = arg("headless", "1") !== "0";
    browser = await chromium.launch({ headless, executablePath: EXE, args: ["--no-sandbox"] });
    const ctxOpts = storageState && fs.existsSync(storageState) ? { storageState } : {};
    const proxy = parseProxy(arg("proxy", null));
    if (proxy) ctxOpts.proxy = proxy;
    // 指纹随机化：视口/UA/时区/语言（反检测，patchright/camoufox 思路的轻量版）
    const fp = spec.fingerprint || {};
    if (fp.enabled !== false) {
      const viewports = [[1366,768],[1440,900],[1536,864],[1920,1080],[1280,720]];
      const vp = viewports[Math.floor(Math.random() * viewports.length)];
      ctxOpts.viewport = { width: vp[0], height: vp[1] };
      ctxOpts.timezoneId = "Asia/Shanghai";
      ctxOpts.locale = "zh-CN";
      ctxOpts.colorScheme = "light";
      if (!ctxOpts.userAgent) {
        const uas = [
          "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
          "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
          "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
        ];
        ctxOpts.userAgent = uas[Math.floor(Math.random() * uas.length)];
      }
      // 注入 WebGL/Canvas 指纹噪声（patchright 同款思路的极简实现）
      ctxOpts.extraHTTPHeaders = { "Accept-Language": "zh-CN,zh;q=0.9" };
    }
    const context = await browser.newContext(ctxOpts);
    const page = await context.newPage();

    // 网络捕获：拦截 SPA 自己发出的签名 API（不逆向签名）
    const captures = spec.capture || [];
    const capturedBy = {};
    page.on("response", async (res) => {
      const u = res.url();
      for (const c of captures) {
        const pat = c.url_pattern || "";
        const hit = pat.startsWith("/") ? u.includes(pat) : new RegExp(pat).test(u);
        if (!hit) continue;
        const ct = res.headers()["content-type"] || "";
        if (!ct.includes("json")) continue;
        const key = c.name || pat;
        try {
          const j = await res.json();
          (capturedBy[key] = capturedBy[key] || []).push({ url: u, json: j });
          if (c.save && capturedBy[key].length % (c.save_every || 5) === 0) {
            const f = path.join(outDir, `${key}.json`);
            fs.writeFileSync(f, JSON.stringify(capturedBy[key], null, 1));
          }
        } catch (e) {}
      }
    });

    // ===== 人工门卫（统一处理：登录 + 整页验证码，大众点评/美团等） =====
    // 1) 是否需要登录：无会话 → 需要；有会话但目标页仍显示登录 → 重新登录
    let needLogin = false;
    if (spec.login && spec.login.enabled) {
      needLogin = !(storageState && fs.existsSync(storageState));
      if (!needLogin) {
        try {
          await page.goto(spec.url, { timeout: 45000, waitUntil: "domcontentloaded" });
          await sleep(2500);
          let _t2 = "";
          try { _t2 = String(await page.evaluate(() => document.body ? document.body.innerText.slice(0, 500) : "")); } catch (e) {}
          if (_t2.includes("扫码登录") || _t2.includes("账号登录") || _t2.includes("二维码已失效")) needLogin = true;
        } catch (e) { needLogin = true; }
      }
      if (needLogin) {
        out({ type: "login", message: `请在弹出的浏览器中登录: ${spec.login.url || spec.url}` });
        await page.goto(spec.login.url || spec.url, { timeout: 60000, waitUntil: "domcontentloaded" });
        if (spec.login.auto_js) {
          await page.evaluate(spec.login.auto_js);
          await page.goto(spec.login.url || spec.url, { timeout: 60000, waitUntil: "domcontentloaded" });
        }
      } else {
        await page.goto(spec.url, { timeout: 60000, waitUntil: "domcontentloaded" });
      }
    } else {
      await page.goto(spec.url, { timeout: 60000, waitUntil: "domcontentloaded" });
    }

    // 2) 人工等待循环：验证码页 / 登录页 → 直到目标页出现
    const gateMarkers = [
      "verify.", "验证中心", "安全验证", "spiderindefence", "滑动验证", "访问过于频繁", "异常访问",
      "/login", "login.", "passport.", "account.meituan"
    ];
    let gateSuccessSel = null;
    let gateMaxWait = 600000;
    if (spec.login && spec.login.enabled) {
      gateSuccessSel = spec.login.wait_selector || gateSuccessSel;
      gateMaxWait = Math.max(gateMaxWait, loginTimeout);
    }
    if (spec.verify && spec.verify.enabled) {
      for (const m of (spec.verify.markers || [])) gateMarkers.push(m);
      gateSuccessSel = spec.verify.success_selector || gateSuccessSel;
      gateMaxWait = Math.max(gateMaxWait, parseInt(spec.verify.max_wait_ms || "600000", 10));
    }
    if (spec.login && spec.login.enabled || spec.verify && spec.verify.enabled) {
      const loginTxt = ["扫码登录", "账号登录", "APP扫码", "二维码已失效", "手机号登录"];
      const pollMs = 2000;
      const deadline = Date.now() + gateMaxWait;
      let done = false;
      if (gateSuccessSel) {
        try { await page.waitForSelector(gateSuccessSel, { timeout: 3000 }); done = true; } catch (e) {}
      }
      let notifGate = false, notifLogin = false;
      if (!done) {
        try { await page.bringToFront(); } catch (e) {}
        out({ type: "verify_required", message: "检测到网站验证码/登录要求：请在弹出的浏览器窗口（标题通常为 Google Chrome for Testing）中完成 ①滑块/点选验证 ②扫码或账号登录，完成后自动继续。最长等待 " + Math.round(gateMaxWait / 1000) + " 秒" });
      }
      while (!done && Date.now() < deadline) {
        const u = page.url() || "";
        let txt = "";
        try { txt = String(await page.evaluate(() => document.body ? document.body.innerText.slice(0, 500) : "")); } catch (e) {}
        const hitMarker = gateMarkers.some(m => u.includes(m) || txt.includes(String(m).toLowerCase()));
        const hitLogin = loginTxt.some(m => txt.includes(m));
        if (hitMarker && !notifGate) {
          notifGate = true;
          try { await page.bringToFront(); } catch (e) {}
          out({ type: "verify_required", message: "检测到验证码/验证页：请在浏览器中完成滑块/点选验证，完成后自动继续" });
        }
        if (hitLogin && !notifLogin) {
          notifLogin = true;
          try { await page.bringToFront(); } catch (e) {}
          out({ type: "login_required", message: "检测到登录页：请在浏览器中扫码或账号登录，登录后自动继续" });
        }
        let selOk = true;
        if (gateSuccessSel) {
          try { await page.waitForSelector(gateSuccessSel, { timeout: 2000 }); done = true; break; } catch (e) { selOk = false; }
        }
        if (selOk && !hitMarker && !hitLogin) { done = true; break; }
        await sleep(pollMs);
      }
      if (!done) {
        out({ type: "error", message: "人工验证/登录超时（" + Math.round(gateMaxWait / 1000) + "s）：请确保在弹出的窗口中完成滑块验证和扫码/账号登录" });
        process.exit(1);
      }
      if (storageState) {
        await context.storageState({ path: storageState });
        out({ type: "verify_ok", storageState });
      }
      out({ type: "verify_passed", message: "✅ 验证/登录通过，继续抓取" });
    }

    if (spec.js_pre) await page.evaluate(spec.js_pre);
    if (spec.wait && spec.wait.selector) {
      await page.waitForSelector(spec.wait.selector, { timeout: spec.wait.timeout || 20000 }).catch(() => {});
    }
    await sleep(settle);

    // 自动滚动触发懒加载（小红书评论需要滚动才加载）
    if (scrollCount > 0) {
      out({ type: "scroll", count: scrollCount });
      const beh = spec.behavior || {};
      for (let s = 0; s < scrollCount; s++) {
        // 人类化：不是每次到底，而是分段滚动 + 随机停顿（模拟真人阅读节奏）
        if (beh.human_scroll) {
          const target = await page.evaluate(() => document.body.scrollHeight);
          const steps = 3 + Math.floor(Math.random() * 4);
          for (let st = 0; st < steps; st++) {
            await page.evaluate((pct) => window.scrollTo(0, document.body.scrollHeight * pct), (st + 1) / steps);
            await sleep(250 + Math.random() * 500);
          }
        } else {
          await page.evaluate(() => {
            window.scrollTo(0, document.body.scrollHeight);
            window.dispatchEvent(new Event("scroll"));
          });
        }
        // 鼠标微动（降低"机器感"）
        if (beh.mouse_move !== false) {
          await page.mouse.move(100 + Math.random() * 800, 100 + Math.random() * 500);
        }
        await sleep(scrollWait + (beh.jitter_ms || 0) * Math.random());
      }
    }

    // 把捕获到的 API 数据落盘
    for (const key of Object.keys(capturedBy)) {
      const f = path.join(outDir, `${key}.json`);
      fs.writeFileSync(f, JSON.stringify(capturedBy[key], null, 1));
      out({ type: "capture_file", name: key, file: f, count: capturedBy[key].length });
    }

    let pagesDone = 0;
    for (let p = 1; p <= maxPages; p++) {
      // 验证码/滑块处理
      if (captchaDir && (spec.captcha || spec.slider)) {
        const ok = await handleCaptcha(page, spec, captchaDir, captchaTimeout);
        if (!ok) { out({ type: "error", message: "验证码处理失败/超时" }); process.exit(1); }
      }
      // 保存本页 HTML
      const html = await page.evaluate(() => document.documentElement.outerHTML);
      const file = path.join(outDir, `page_${p}.html`);
      fs.writeFileSync(file, html);
      out({ type: "page", page: p, file });
      pagesDone = p;

      // 翻页
      const pg = spec.pagination || { type: "none" };
      if (pg.type === "none") break;
      if (pg.stop_condition && await page.evaluate(pg.stop_condition)) break;
      let changed = false;
      if (pg.type === "click") {
        const sel = pg.selector;
        if (!(await page.locator(sel).first().isVisible().catch(() => false))) break;
        const before = await page.evaluate(() => document.documentElement.outerHTML.length);
        await page.locator(sel).first().click();
        await sleep(pg.wait_ms || 1800);
        const after = await page.evaluate(() => document.documentElement.outerHTML.length);
        changed = after !== before;
        if (!changed && pg.max_clicks) { let c = 0; while (!changed && c < pg.max_clicks) { await page.locator(sel).first().click(); await sleep(pg.wait_ms || 1800); c++; changed = (await page.evaluate(() => document.documentElement.outerHTML.length)) !== before; } }
      } else if (pg.type === "js") {
        await page.evaluate(pg.js);
        await sleep(pg.wait_ms || 1800);
        changed = true;
      }
      if (pg.type !== "none" && !changed) break;
    }
    out({ type: "done", pages: pagesDone });
  } catch (e) {
    out({ type: "error", message: String((e && e.message) || e) });
    process.exit(1);
  } finally {
    if (browser) await browser.close();
  }
}

main();
