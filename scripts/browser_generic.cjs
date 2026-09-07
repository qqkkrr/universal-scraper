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
const { parseProxy, waitCloudflare } = require("./browser_common.cjs");

let chromium = null;
// 优先 NODE_PATH 的 playwright（本地 patchright 旧版可能被 WAF 识别），再回退本地 patchright
const _path = require("node:path");
const _nps = (process.env.NODE_PATH || "").split(":").filter(Boolean);
const _cands = _nps.map((p) => _path.join(p, "playwright")).concat(["patchright", "playwright"]);
for (const _c of _cands) {
  try { chromium = require(_c).chromium; break; } catch (e) { /* 下一个 */ }
}
if (!chromium) throw new Error("找不到 playwright/patchright");

const os = require("node:os");
const HOME = process.env.HOME || os.homedir() || "/tmp";
const USER_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
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

// Cloudflare/Turnstile 自动点击（对标 cloudflare-solver / FlareSolverr 思路）：
// 检测 challenges.cloudflare.com / turnstile iframe，点掉"我不是机器人"复选框，
// 成功后等待 cf_clearance cookie / 页面离开挑战页。失败静默（转人工）。
async function trySolveCloudflare(page, context) {
  const frames = page.frames();
  for (const f of frames) {
    const u = f.url() || "";
    if (u.includes("challenges.cloudflare.com") || u.includes("turnstile") || u.includes("hcaptcha.com")) {
      try {
        const sel = "input[type=checkbox], .ctp-checkbox-label, .cb-c, #challenge-stage input, .h-captcha iframe";
        const loc = f.locator(sel).first();
        if (await loc.count()) {
          await loc.click({ timeout: 4000 });
          out({ type: "cf_click", message: "已自动点击 Cloudflare/Turnstile 验证框" });
          return true;
        }
      } catch (e) {}
      return false;
    }
  }
  try {
    const box = page.locator("iframe[src*='turnstile'], iframe[src*='challenges.cloudflare.com'], iframe[src*='hcaptcha.com']").first();
    if (await box.count()) {
      await box.click({ timeout: 3000 });
      out({ type: "cf_click", message: "已自动点击 Cloudflare/Turnstile 验证框(页级)" });
      return true;
    }
  } catch (e) {}
  return false;
}

async function main() {
  const specFile = arg("spec");
  const outDir = arg("out");
  const captchaDir = arg("captchaDir", null);
  const captchaTimeout = parseInt(arg("captchaTimeout", "300000"), 10);
  const maxPages = parseInt(arg("maxPages", "100"), 10);
  const startPage = parseInt(arg("startPage", "1"), 10);
  const settle = parseInt(arg("settle", "1500"), 10);

  const spec = JSON.parse(fs.readFileSync(specFile, "utf-8"));
  fs.mkdirSync(outDir, { recursive: true });
  if (captchaDir) fs.mkdirSync(captchaDir, { recursive: true });
  const storageState = arg("storageState", null);
  const debugDir = arg("debugDir", null);
  if (debugDir) fs.mkdirSync(debugDir, { recursive: true });
  const snap = async (tag) => { if (debugDir) { try { await page.screenshot({ path: path.join(debugDir, tag + "_" + Date.now() + ".png") }); } catch (e) {} } };
  const scrollCount = parseInt(arg("scrollCount", "0"), 10);
  const scrollWait = parseInt(arg("scrollWait", "2000"), 10);
  const loginTimeout = parseInt(arg("loginTimeout", "600000"), 10);
  const profileDir = arg("profile", null);
  const cdpUrl = arg("cdp", null);
  const stopFile = arg("stopFile", null);
  const stopRequested = () => stopFile && fs.existsSync(stopFile);

// mac 有头窗口：居中 + 放大到接近全屏（System Events 需辅助功能权限，失败静默）
// 解决"弹出的 Chrome 窗口在屏幕角落/太小，验证滑块锁在右下角点不到"
function focusWindowMac() {
  if (process.platform !== "darwin") return;
  try {
    const { execSync } = require("node:child_process");
    execSync(
      `osascript -e 'tell application "System Events" to tell (first process whose name contains "chrome") to set position of front window to {120, 60}' ` +
      `-e 'tell application "System Events" to tell (first process whose name contains "chrome") to set size of front window to {1680, 1050}' ` +
      `-e 'tell application "System Events" to tell (first process whose name contains "chrome") to set frontmost to true'`,
      { timeout: 4000 }
    );
  } catch (e) { /* 无辅助功能权限或非 mac：忽略 */ }
}

// 把 fixed/absolute 的验证码弹窗强制改到屏幕正中央（Boss直聘易盾锁右下角的修复）
function centerCaptcha(page) {
  return page.evaluate(() => {
    const sels = [".yidun_panel", ".yidun--light", ".yidun", ".nc-container", ".nc_wrapper",
                  ".nc_scale", "[class*='yidun']", "[class*='nc_']", "[class*='verify']",
                  "[class*='captcha']", "[class*='slider']"];
    let moved = 0;
    for (const sel of sels) {
      document.querySelectorAll(sel).forEach((el) => {
        const cs = getComputedStyle(el);
        if (cs.position === "fixed" || cs.position === "absolute") {
          el.style.position = "fixed";
          el.style.left = "50%";
          el.style.top = "50%";
          el.style.transform = "translate(-50%, -50%)";
          el.style.margin = "0";
          el.style.zIndex = "999999";
          moved++;
        }
      });
    }
    return moved;
  }).catch(() => 0);
}
  // 安全正则：非法 pattern 不抛异常（配置可能来自 LLM），返回 null 表示"永不匹配"
  function safeRe(pattern) {
    try { return new RegExp(pattern); } catch (e) { return null; }
  }
  // 真实 Chrome（用户日常浏览器）比 Chrome for Testing 更接近真人，风控识别率低
  const CHROME_EXE = fs.existsSync(USER_CHROME) ? USER_CHROME : FULL_CHROME;

  let browser = null;
  let context = null;
  let page = null;
  const ownPages = [];  // 本进程创建的标签页——finally 必须关闭（CDP 模式防泄漏）
  try {
    const headless = arg("headless", "1") !== "0";
    const ctxOpts = (!profileDir && storageState && fs.existsSync(storageState)) ? { storageState } : {};
    const proxy = parseProxy(arg("proxy", null));
    if (proxy) ctxOpts.proxy = proxy;
    // 指纹随机化：视口/UA/时区/语言（反检测，patchright/camoufox 思路的轻量版）
    const fp = spec.fingerprint || {};
    if (fp.enabled !== false) {
      // 有头(人工操作验证)固定用大视口，避免滑块按钮超出屏幕；无头抓取保持随机指纹
      const vp = arg("headless", "0") !== "0"
        ? [1366, 768 + Math.floor(Math.random() * 300)]
        : [[1920, 1080], [1536, 864], [1440, 900]][Math.floor(Math.random() * 3)];
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
    if (cdpUrl) {
      // CDP 直连：附着到用户已开的真实 Chrome（--remote-debugging-port=9222）。
      // 真实浏览器 = 真实指纹 + 真实登录态，是 MediaCrawler/DrissionPage CDP 模式的思路，
      // 对京东/知乎/微博/小红书这类强风控站最稳。
      out({ type: "cdp", message: "正在连接真实浏览器 CDP: " + cdpUrl });
      // 闲鱼战例：iterate 多轮反复重连 CDP 会偶发超时——重试 2 次（间隔 3s）
      let lastErr = null;
      for (let attempt = 0; attempt < 3; attempt++) {
        try { browser = await chromium.connectOverCDP(cdpUrl, { timeout: 20000 }); break; }
        catch (e) { lastErr = e; out({ type: "diag", message: `CDP 连接失败(第${attempt + 1}次): ${e.message}` }); await sleep(3000); }
      }
      if (!browser) throw lastErr || new Error("CDP 连接失败");
      context = browser.contexts()[0] || await browser.newContext();
      page = await context.newPage();
      ownPages.push(page);
    } else if (profileDir) {
      // 持久档案模式：登录态保存在档案目录，登录一次永久复用（最接近真实浏览器）
      // headless 默认有头（登录需要），可显式 --headless 1 无头（登录态已存在时抓取用）
      fs.mkdirSync(profileDir, { recursive: true });
      // 清残留锁（Python 侧已按任务隔离 profile，双保险防 SingletonLock 卡启动）
      for (const _lk of ["SingletonLock", "SingletonCookie", "SingletonSocket"]) {
        try { fs.rmSync(path.join(profileDir, _lk), { force: true }); } catch (e) {}
      }
      context = await chromium.launchPersistentContext(profileDir, {
        ...ctxOpts,
        headless: arg("headless", "0") !== "0",
        executablePath: CHROME_EXE,
        args: ["--no-sandbox", "--ignore-certificate-errors", "--disable-blink-features=AutomationControlled", "--lang=zh-CN"],
      });
      page = context.pages()[0] || await context.newPage();
      if (arg("headless", "0") === "0") focusWindowMac();  // 有头：窗口居中放大
    } else {
      // batch1401 战训：headless-shell 缺失时池直接崩，而 9222 调试 Chrome 往往可用。
      // 启动失败 → 自动探测本机 9222 CDP，活着就降级附加（真实浏览器反而更稳）。
      try {
        browser = await chromium.launch({ headless, executablePath: EXE, args: ["--no-sandbox", "--ignore-certificate-errors"] });
        context = await browser.newContext(ctxOpts);
        page = await context.newPage();
      } catch (launchErr) {
        const fallbackCdp = "http://127.0.0.1:9222";
        let fbOk = false;
        try {
          const http = require("http");
          fbOk = await new Promise((resolve) => {
            const req = http.get(fallbackCdp + "/json/version", { timeout: 2500 }, (r) => resolve(r.statusCode === 200));
            req.on("error", () => resolve(false));
            req.on("timeout", () => { req.destroy(); resolve(false); });
          });
        } catch (e) {}
        if (fbOk) {
          out({ type: "diag", message: "浏览器启动失败(" + String(launchErr.message || launchErr).slice(0, 120) + ") → 降级连接 9222 调试 Chrome（注意：proxy/指纹/storageState 配置不生效，走真实 Chrome 出口与登录态）" });
          try {
            browser = await chromium.connectOverCDP(fallbackCdp, { timeout: 15000 });
            context = browser.contexts()[0] || await browser.newContext();
            page = await context.newPage();
            ownPages.push(page);
          } catch (e2) {
            throw launchErr;  // 降级也失败：抛原始启动错误（诊断信息更准）
          }
        } else {
          throw launchErr;
        }
      }
    }
    // 控制台/页面错误捕获（诊断"页面为什么没加载数据"的关键）
    // 节流：每类最多输出 50 条，防止 JS 重页面刷爆协议流
    const diagBudget = { console: 50, pageerror: 50, reqfailed: 50, http4xx: 50 };
    let diagTruncated = false;
    const noteTruncated = () => {
      if (!diagTruncated) {
        diagTruncated = true;
        out({ type: "diag_truncated", message: "诊断输出超过上限（每类 50 条），已截断" });
      }
    };
    page.on("console", (msg) => {
      const t = msg.type();
      if (t === "error" || t === "warning") {
        if (diagBudget.console-- > 0) out({ type: "console", level: t, text: String(msg.text()).slice(0, 400) });
        else noteTruncated();
      }
    });
    page.on("pageerror", (err) => {
      if (diagBudget.pageerror-- > 0) out({ type: "pageerror", text: String(err).slice(0, 400) });
      else noteTruncated();
    });
    page.on("requestfailed", (req) => {
      if (diagBudget.reqfailed-- > 0) {
        out({ type: "reqfailed", url: req.url().slice(0, 160), err: String(req.failure() && req.failure().errorText).slice(0, 120) });
      } else noteTruncated();
    });

    // 网络捕获：拦截 SPA 自己发出的签名 API（不逆向签名）
    // capture 契约：数组=声明式捕获；true(布尔)=全捕获（配置常传布尔，绝不能迭代它）
    const captures = Array.isArray(spec.capture) ? spec.capture : [];
    const capturedBy = {};
    // capture_all：把页面上所有 JSON 响应都存下来（不知道接口名也能事后挖数据）
    const captureAll = !!spec.capture_all || spec.capture === true;
    const capturedAll = [];
    // JSONP 兼容：mtotp9({...}) 等 wrapper 自动剥壳，解析失败保留原始 body 兜底（盒马战例）
    async function jsonMaybe(res) {
      try { return { json: await res.json() }; } catch (e) {}
      try {
        const t = await res.text();
        const m = t.match(/^\s*[\w$]+\(([\s\S]*)\)\s*;?\s*$/);
        if (m) { try { return { json: JSON.parse(m[1]) }; } catch (e2) {} }
        try { return { json: JSON.parse(t) }; } catch (e2) {}
        return { raw: t.slice(0, 4000) };
      } catch (e) { return {}; }
    }
    // 上下文级捕获：附加 CDP 时同一 Chrome 的既有标签页 XHR 也会录到（智联战例——
    // page 级监听漏掉数据接口所在的既有标签页）。声明式 capture 仍按 url_pattern 过滤。
    context.on("response", async (res) => {
      const u = res.url();
      if (res.status() >= 400) {
        if (diagBudget.http4xx-- > 0) out({ type: "http_4xx", status: res.status(), url: u.slice(0, 260) });
        else noteTruncated();
      }
      for (const c of captures) {
        const pat = c.url_pattern || "";
        let patRe = null;
        if (!pat.startsWith("/")) {
          try { patRe = new RegExp(pat); } catch (e) { patRe = null; }
        }
        const hit = pat.startsWith("/") ? u.includes(pat) : (patRe ? patRe.test(u) : false);
        if (!hit) continue;
        const ct = res.headers()["content-type"] || "";
        const key = c.name || pat;
        // 4xx/5xx：记录 URL（诊断 403 卡点）
        if (res.status() >= 400) {
          (capturedBy[key] = capturedBy[key] || []).push({ url: u, status: res.status(), httpError: true });
          out({ type: "capture_http_error", name: key, url: u.slice(0, 220), status: res.status() });
          continue;
        }
        // JSONP（application/javascript）也算 JSON 族——盒马/淘宝 mtop 接口全是它
        if (!(ct.includes("json") || ct.includes("javascript"))) continue;
        try {
          const r2 = await jsonMaybe(res);
          const arr = (capturedBy[key] = capturedBy[key] || []);
          if (arr.length < 5000) {  // 命名捕获上限，防长任务内存爆炸
            // 声明式捕获同样落盘 method/请求体（batch1401 战训：chinamoney/xkz/NAFMII
            // 三次"接口捕到、参数拿不到"都栽在这里——没有 post_data 就无法改写 http_json 配置）
            let _pd = "", _m = "GET", _ct = "";
            try {
              const rq = res.request();
              _m = rq.method();
              _pd = rq.postData() || "";
              _ct = (rq.headers() || {})["content-type"] || "";
            } catch (e) {}
            const rec = { url: u, method: _m,
                          post_data: _pd || undefined, request_content_type: _ct || undefined };
            arr.push(r2.json !== undefined ? Object.assign(rec, { json: r2.json })
                                            : Object.assign(rec, { raw: r2.raw }));
          }
          if (c.save && arr.length % (c.save_every || 5) === 0) {
            const f = path.join(outDir, `${key}.json`);
            fs.writeFileSync(f, JSON.stringify(arr, null, 1));
          }
        } catch (e) {}
      }
      const ctAll = res.headers()["content-type"] || "";
      if (captureAll && res.status() < 400 && (ctAll.includes("json") || ctAll.includes("javascript"))) {
        if (capturedAll.length < 2000) {
          try {
            // 小米有品战例：同时落盘请求体/方法/关键头——一次浏览器侦察即可改写成 http_json 配置
            const r2 = await jsonMaybe(res);
            let post_data = "";
            let method = "GET";
            let req_ct = "";
            try {
              const rq = res.request();
              method = rq.method();
              post_data = rq.postData() || "";
              const hh = rq.headers();
              req_ct = hh["content-type"] || "";
            } catch (e) {}
            const rec = { url: u, method, post_data: post_data || undefined,
                          request_content_type: req_ct || undefined };
            capturedAll.push(r2.json !== undefined
              ? Object.assign(rec, { json: r2.json })
              : Object.assign(rec, { raw: r2.raw }));
            if (capturedAll.length % 50 === 0) {
              fs.writeFileSync(path.join(outDir, "capture_all.json"), JSON.stringify(capturedAll, null, 1));
            }
          } catch (e) {}
        }
      }
    });

    // ===== 人工门卫（统一处理：登录 + 整页验证码，大众点评/美团等） =====
    // 1) 是否需要登录：无会话 → 需要；有会话但目标页仍显示登录 → 重新登录
    let needLogin = false;
    if (spec.login && spec.login.enabled) {
      needLogin = !profileDir && !(storageState && fs.existsSync(storageState));
      if (!needLogin) {
        try {
          await page.goto(spec.url, { timeout: 45000, waitUntil: "domcontentloaded" });
        await waitCloudflare(page, context).catch(() => {});
          await sleep(2500);
          let _t2 = "";
          try { _t2 = String(await page.evaluate(() => document.body ? document.body.innerText.slice(0, 500) : "")); } catch (e) {}
          if (_t2.includes("扫码登录") || _t2.includes("账号登录") || _t2.includes("二维码已失效")) needLogin = true;
        } catch (e) { needLogin = true; }
      }
      if (needLogin) {
        out({ type: "login", message: `请在弹出的浏览器中登录: ${spec.login.url || spec.url}` });
        await page.goto(spec.login.url || spec.url, { timeout: 90000, waitUntil: "domcontentloaded" });
        if (spec.login.auto_js) {
          await page.evaluate(spec.login.auto_js);
          await page.goto(spec.login.url || spec.url, { timeout: 90000, waitUntil: "domcontentloaded" });
        }
      } else {
        await page.goto(spec.url, { timeout: 90000, waitUntil: "domcontentloaded" });
      }
    } else {
      await page.goto(spec.url, { timeout: 90000, waitUntil: "domcontentloaded" });
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

    // 登录硬校验：某些站点（京东）必须出现指定 cookie（pt_key/pt_pin）才算真正登录，
    // 否则 wait_selector 命中（如 #ttbar-login 常驻节点）会造成"假登录通过"。
    const requireCookie = (spec.login && spec.login.require_cookie) || null;
    async function hasReqCookie(ctx) {
      if (!requireCookie) return true;
      try {
        const cs = await ctx.cookies();
        return cs.some(c => (safeRe(requireCookie) || /a^/).test(c.name || ""));
      } catch (e) { return false; }
    }

    if (spec.login && spec.login.enabled || spec.verify && spec.verify.enabled) {
      const loginTxt = ["扫码登录", "账号登录", "APP扫码", "二维码已失效", "手机号登录"];
      const pollMs = 2000;
      const deadline = Date.now() + gateMaxWait;
      let done = false;
      if (gateSuccessSel) {
        try { await page.waitForSelector(gateSuccessSel, { timeout: 3000 });
              if (await hasReqCookie(context)) done = true; } catch (e) {}
      }
      let notifGate = false, notifLogin = false, notifWaf = false;
      if (!done) {
        try { await page.bringToFront(); } catch (e) {}
        out({ type: "verify_required", message: "检测到网站验证码/登录要求：请在弹出的浏览器窗口（标题通常为 Google Chrome for Testing）中完成 ①滑块/点选验证 ②扫码或账号登录，完成后自动继续。最长等待 " + Math.round(gateMaxWait / 1000) + " 秒" });
        // 诊断：输出当前 URL 与页面文本，便于定位"已登录但识别不了"的情况
        try {
          const _g = String(await page.evaluate(() => document.body ? document.body.innerText.slice(0, 300) : ""));
          out({ type: "gate_info", url: page.url(), text_len: _g.length, text: _g.replace(/\s+/g, " ").slice(0, 300) });
        } catch (e) {
          out({ type: "gate_info", url: page.url(), text_len: 0, text: "(evaluate失败: " + String(e).slice(0,80) + ")" });
        }
      }
      let diagSent = 0;
      let autoClickedVerify = false;
      const gateStartTs = Date.now();
      while (!done && Date.now() < deadline) {
        if (stopRequested()) {
          out({ type: "stopped", message: "收到停止信号（.stop）" });
          process.exit(0);
        }
        // Boss直聘"点击按钮进行验证"：自动点一次（点击型验证，点完弹滑块交给用户拖）
        if (!autoClickedVerify) {
          try {
            const btnPos = await page.evaluate(() => {
              const els = Array.from(document.querySelectorAll("button, a, div, span, em"));
              const target = els.find(e => /点击按钮进行验证|点击验证|开始验证/.test((e.textContent || "").trim())
                && e.offsetWidth > 0 && e.offsetHeight > 0);
              if (!target) return null;
              const r = target.getBoundingClientRect();
              return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
            });
            if (btnPos) {
              await page.mouse.click(btnPos.x, btnPos.y);
              autoClickedVerify = true;
              out({ type: "verify_required", message: "✅ 已自动点击「点击按钮进行验证」，滑块弹出后请拖动完成" });
              await sleep(1500);
            }
          } catch (e) {}
        }
        const u = page.url() || "";
        let txt = "";
        // 取前 5000 字符（原 500 会让 txtLen>2000 的"长页面=登录成功"兜底永远不成立）
        try { txt = String(await page.evaluate(() => document.body ? document.body.innerText.slice(0, 5000) : "")); } catch (e) {}
        const hitMarker = gateMarkers.some(m => u.includes(m) || txt.includes(String(m).toLowerCase()));
        // CWAP/WZWS 滑块 WAF：专门识别并强制等待用户拖动（普通"长文本即通过"兜底会误放行）
        const wafMarkers = ["waf_slider", "wzws-waf-cgi", "wzws_waf", "CWAP-waf", "请完成安全验证", "滑动填", "拖动滑块"];
        const hitWaf = !!(spec.verify && spec.verify.enabled) && wafMarkers.some(m => u.includes(m) || txt.includes(String(m).toLowerCase()));
        // 验证码元素锁在右下角/视口外（用户点不到按钮）：
        // ① fixed 弹窗强制居中 ② 普通元素滚进视口中央
        try {
          await centerCaptcha(page);
          await page.evaluate(() => {
            const sel = ".yidun_slider,.nc_scale,.nc_wrapper,.nc_iconfont,.btn_ok,[class*='yidun'],[class*='nc_'],[class*='verify'],[class*='captcha'],[class*='slider']";
            const el = document.querySelector(sel);
            if (el) { el.scrollIntoView({ block: "center", inline: "center" }); return true; }
            return false;
          }).catch(() => {});
        } catch (e) {}
        const hitLogin = loginTxt.some(m => txt.includes(m))
          && !(u.includes("m.dianping.com") && !u.includes("/login"));  // 移动版首页不算登录页（登录后常跳这里）
        // Cloudflare/Turnstile：先尝试自动点击（无需人工）
        if (u.includes("challenges.cloudflare.com") || u.includes("turnstile") ||
            u.includes("hcaptcha.com") || txt.toLowerCase().includes("just a moment") ||
            txt.includes("验证") || txt.includes("安全")) {
          await trySolveCloudflare(page, context).catch(() => {});
          await sleep(800);
        }
        if (hitMarker && !notifGate) {
          notifGate = true;
          await snap("verify");
          try { await page.bringToFront(); } catch (e) {}
          out({ type: "verify_required", message: "检测到验证码/验证页：请在浏览器中完成滑块/点选验证，完成后自动继续" });
        }
        if (hitLogin && !notifLogin) {
          notifLogin = true;
          await snap("login");
          try { await page.bringToFront(); } catch (e) {}
          out({ type: "login_required", message: "检测到登录页：请在浏览器中扫码或账号登录，登录后自动继续" });
        }
        if (hitWaf && !notifWaf) {
          notifWaf = true;
          await snap("verify");
          try { await page.bringToFront(); } catch (e) {}
          out({ type: "verify_required", message: "🛡️ 检测到 WAF 滑块验证（CWAP/wzws）：请在浏览器窗口中拖动滑块完成拼图，完成后自动继续" });
        }
        if (gateSuccessSel && !hitWaf) {
          try { await page.waitForSelector(gateSuccessSel, { timeout: 2000 });
                if (await hasReqCookie(context)) { done = true; break; } } catch (e) {}
        }
        // 通过条件：不在"登录/验证"专用 URL 上，且 ①出现商家特征（人均/条评价/点评）或 ②页面文本足够长（真实内容页）
        // 修复：marker 仅用于提示，不再阻塞放行——正常页面文本里也可能带"安全验证/滑动验证"字样，
        //       否则已登录的真实内容页会被误判成验证页永久卡住（Boss直聘实测踩坑）
        // 修复2：需要 require_cookie 的站点（京东），即使页面看起来"通过"也必须出现登录 cookie，防假登录
        const txtLen = txt.length;
        const hasShop = txt.includes("人均") || txt.includes("条评价") || txt.includes("点评");
        const inLoginUrl = u.includes("/login") || u.includes("passport.") || u.includes("verify.") || u.includes("account.meituan");
        if (!hitWaf && !inLoginUrl && (hasShop || txtLen > 800) && await hasReqCookie(context)) { done = true; break; }
        // 每 8s 输出一次循环内诊断（定位"已登录但识别不了"）
        if (diagSent < 3 && Date.now() - gateStartTs > 6000 + diagSent * 8000) {
          diagSent++;
          const hitWhich = gateMarkers.filter(m => u.includes(m) || txt.includes(String(m).toLowerCase())).slice(0, 5);
          out({ type: "gate_info", url: u, text_len: txtLen,
                hitMarker: hitWhich.join("|"), hitLogin, inLoginUrl });
        }
        await sleep(pollMs);
      }
      if (!done) {
        let _u = "", _tt = "";
        try { _u = page.url() || ""; _tt = await page.title(); } catch (e) {}
        await snap("timeout");
        let _cookieInfo = "";
        if (requireCookie) {
          const cs = await context.cookies().catch(() => []);
          const names = cs.map(c => c.name).filter(n => (safeRe(requireCookie) || /a^/).test(n)).join(",");
          _cookieInfo = " | 登录cookie(" + requireCookie + "): " + (names || "❌ 未出现——说明尚未真正登录");
        }
        out({ type: "error", message: "人工验证/登录超时（" + Math.round(gateMaxWait / 1000) + "s）。当前页面: " + _u + " | 标题: " + _tt + _cookieInfo + "。请确保在弹出的窗口中完成滑块验证（WAF 滑块需拖动拼图）和扫码/账号登录" });
        process.exit(1);
      }
      // 登录/验证通过后，若被跳走（如移动版 dphome），强制回到目标页再抓
      try {
        const cur = page.url() || "";
        const target = spec.url || "";
        const sameHost = (cur.split("?")[0].replace(/\/$/, "") === target.split("?")[0].replace(/\/$/, ""))
          || (cur.includes("/search/") && target.includes("/search/"));
        if (!sameHost) {
          out({ type: "redirect", message: "登录后跳转到 " + cur + "，正在回到目标页 " + target });
          await page.goto(target, { timeout: 90000, waitUntil: "domcontentloaded" });
          await sleep(2500);
          if (gateSuccessSel) {
            try { await page.waitForSelector(gateSuccessSel, { timeout: 5000 }); } catch (e) {}
          }
        }
      } catch (e) {}
      if (storageState) {
        await context.storageState({ path: storageState });
        out({ type: "verify_ok", storageState });
      }
      let _ck = "";
      if (requireCookie) {
        const cs = await context.cookies().catch(() => []);
        _ck = "（登录cookie: " + cs.map(c => c.name).filter(n => (safeRe(requireCookie) || /a^/).test(n)).join(",") + "）";
      }
      out({ type: "verify_passed", message: "✅ 验证/登录通过" + _ck + "，继续抓取" });
    }

    if (spec.js_pre) await page.evaluate(spec.js_pre);
    if (spec.wait && spec.wait.selector) {
      await page.waitForSelector(spec.wait.selector, { timeout: spec.wait.timeout || 20000 }).catch(() => {});
    }
    await sleep(settle);
    if (stopRequested()) {
      out({ type: "stopped", message: "收到停止信号（.stop）" });
      process.exit(0);
    }

    // 真人浏览捕获模式：保持窗口打开，让用户手动操作（验证/登录/滚动），期间持续捕获接口
    if (spec.hold_ms) {
      out({ type: "hold", ms: parseInt(spec.hold_ms, 10), message: "⏳ 请在弹出的浏览器中手动完成验证/登录并浏览商家列表（可滚动/点击/搜索），工具会自动捕获数据接口。等待 " + Math.round(parseInt(spec.hold_ms,10)/1000) + " 秒" });
      await sleep(parseInt(spec.hold_ms, 10));
    }

    // 自动滚动触发懒加载（小红书评论需要滚动才加载）
    if (scrollCount > 0) {
      out({ type: "scroll", count: scrollCount });
      const beh = spec.behavior || {};
      for (let s = 0; s < scrollCount; s++) {
        if (stopRequested()) {
          out({ type: "stopped", message: "收到停止信号（.stop）" });
          process.exit(0);
        }
        // 人类化：不是每次到底，而是分段滚动 + 随机停顿（模拟真人阅读节奏）
        if (beh.human_scroll) {
          const steps = 3 + Math.floor(Math.random() * 4);
          for (let st = 0; st < steps; st++) {
            await page.evaluate((pct) => { const _h = document.documentElement ? document.documentElement.scrollHeight : (document.body ? document.body.scrollHeight : 0); window.scrollTo(0, _h * pct); }, (st + 1) / steps);
            await sleep(250 + Math.random() * 500);
          }
        } else {
          await page.evaluate(() => {
            const _h = document.documentElement ? document.documentElement.scrollHeight : (document.body ? document.body.scrollHeight : 0); window.scrollTo(0, _h);
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
    if (captureAll && capturedAll.length) {
      fs.writeFileSync(path.join(outDir, "capture_all.json"), JSON.stringify(capturedAll, null, 1));
      out({ type: "capture_file", name: "capture_all", file: path.join(outDir, "capture_all.json"), count: capturedAll.length });
    }
    for (const key of Object.keys(capturedBy)) {
      const f = path.join(outDir, `${key}.json`);
      fs.writeFileSync(f, JSON.stringify(capturedBy[key], null, 1));
      out({ type: "capture_file", name: key, file: f, count: capturedBy[key].length });
    }

    // ===== spec.actions：页内动作链（闲鱼战例——排序点击/输入/等待，v1.9 起透传到桥） =====
    if (Array.isArray(spec.actions) && spec.actions.length) {
      for (const [ai, a] of spec.actions.entries()) {
        try {
          if (a.type === "click") {
            const loc = page.locator(a.selector).first();
            await loc.waitFor({ state: "visible", timeout: a.timeout_ms || 10000 });
            await loc.click();
            out({ type: "diag", message: `actions[${ai}] click ${a.selector} ✓` });
          } else if (a.type === "type" || a.type === "fill") {
            await page.locator(a.selector).first().fill(String(a.value ?? ""), { timeout: a.timeout_ms || 10000 });
            out({ type: "diag", message: `actions[${ai}] fill ${a.selector} ✓` });
          } else if (a.type === "press") {
            await page.keyboard.press(a.key || "Enter");
          } else if (a.type === "wait") {
            if (a.selector) await page.locator(a.selector).first().waitFor({ state: "visible", timeout: a.timeout_ms || 15000 }).catch(() => {});
            else await sleep(a.ms || 1000);
            out({ type: "diag", message: `actions[${ai}] wait ✓` });
          } else if (a.type === "js") {
            const ret = await page.evaluate(a.js);
            out({ type: "diag", message: `actions[${ai}] js 返回: ${String(ret).slice(0, 120)}` });
          } else if (a.type === "screenshot") {
            await page.screenshot({ path: a.path, fullPage: !!a.fullPage });
            out({ type: "diag", message: `actions[${ai}] screenshot -> ${a.path}` });
          }
          if (a.wait_ms) await sleep(a.wait_ms);
        } catch (e) {
          out({ type: "diag", message: `actions[${ai}] ${a.type} 失败: ${String(e.message).slice(0, 120)}` });
        }
      }
    }

    // ===== spec.urls：批量详情模式（#2 详情 browser 后端）——单连接顺序导航，一页一存 =====
    if (Array.isArray(spec.urls) && spec.urls.length) {
      const waitMs = spec.detail_wait_ms || 1200;
      let n = 0;
      for (const [i, u] of spec.urls.entries()) {
        try {
          await page.goto(u, { timeout: 45000, waitUntil: "domcontentloaded" });
          await sleep(waitMs);
          const html = await page.evaluate(() => document.documentElement.outerHTML);
          const file = path.join(outDir, `detail_${i}.html`);
          fs.writeFileSync(file, html);
          out({ type: "detail_page", index: i, url: u, file, bytes: html.length });
          n++;
        } catch (e) {
          out({ type: "detail_page", index: i, url: u, error: String(e.message).slice(0, 160) });
        }
      }
      out({ type: "done", pages: n });
      setTimeout(() => { try { process.exit(0); } catch (e) {} }, 8000).unref();
      return;
    }

    let pagesDone = 0;
    // startPage：定向分页（配深链 URL 使用，如列表第 1161 页）——页号从 startPage 计
    for (let p = startPage; p <= maxPages; p++) {
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
      if (html.length < 300) {
        out({ type: "diag", message: `页面 DOM 仅 ${html.length}B——evaluate/上下文可能异常`
            + "（网络捕获不受影响；此类站改用 url 深链翻页可绕开 DOM）" });
      }
      pagesDone = p;

      // 翻页
      const pg = spec.pagination || { type: "none" };
      if (pg.type === "none") break;
      if (pg.stop_condition) {
        try {
          const sc = await page.evaluate(pg.stop_condition);
          out({ type: "diag", message: `stop_condition 结果: ${String(sc).slice(0, 80)}` });
          if (sc) break;
        } catch (e) {
          out({ type: "diag", message: `stop_condition 执行失败: ${String(e.message).slice(0, 120)}` });
        }
      }
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
        const ret = await page.evaluate(pg.js);
        out({ type: "diag", message: `js 翻页返回: ${String(ret).slice(0, 160) || "(空——检查选择器)"}` });
        await sleep(pg.wait_ms || 1800);
        changed = true;
      } else if (pg.type === "url") {
        // URL 深链翻页（猎聘/Next.js 类 SPA 最稳）：template 里 {page} 占位，绕开 DOM 交互
        const nxt = String(pg.template || "").replace("{page}", String(p + 1));
        if (!nxt || nxt === String(pg.template)) {
          out({ type: "error", message: "url 翻页需要 template 且含 {page} 占位" });
          process.exit(1);
        }
        await page.goto(nxt, { waitUntil: pg.wait_until || "domcontentloaded", timeout: pg.timeout_ms || 30000 });
        await sleep(pg.wait_ms || 1800);
        changed = true;
      }
      if (pg.type !== "none" && !changed) break;
    }
    out({ type: "done", pages: pagesDone });
    // done 已发出：8s 内必须退出（即使 browser.close 挂住，也不让 Python 等 EOF 卡死）
    setTimeout(() => { try { process.exit(0); } catch (e) {} }, 8000).unref();
  } catch (e) {
    out({ type: "error", message: String((e && e.message) || e) });
    process.exit(1);
  } finally {
    // 闲鱼战例：CDP 附加模式下 browser.close() 只断连接，自建标签页会泄漏积压
    // （曾积到 62 个标签页拖垮 Chrome）——先关掉我们自己开的页再断开
    for (const p of ownPages) { try { await p.close(); } catch (e) {} }
    if (browser) await browser.close();
  }
}

main();
