#!/usr/bin/env node
/**
 * 长驻浏览器会话池桥（v3 BrowserFetcher 默认使用）
 * - 一次启动浏览器/上下文，多请求复用（登录态、cookie、指纹跨页保留）
 * - Firecrawl 风格 actions 动作链 + stealth 反检测 + 自动关 cookie/遮罩弹窗
 * 请求协议（stdin 每行 JSON）:
 *   {"id":1,"url":"...","js":"...","wait":"#sel","scroll":3,
 *    "actions":[...],"stealth":true,"remove_overlays":true}
 * 响应协议（stdout 每行 JSON）:
 *   {"id":1,"html":"...","url":"...","bytes":123}
 * 环境变量: US_POOL_SIZE（并发页数，默认3） / US_HEADLESS / PW_EXECUTABLE
 */
const fs = require("node:fs");
const readline = require("node:readline");
const { CHROMIUM_EXE, loadChromium, sleep, runActions, applyStealth, dismissOverlays, parseProxy, waitCloudflare } = require("./browser_common.cjs");

const POOL_SIZE = Math.max(1, parseInt(process.env.US_POOL_SIZE || "3", 10));
const IDLE_MS = Math.max(1000, parseInt(process.env.US_POOL_IDLE_MS || "120000", 10));
const STOP_FILE = process.env.US_STOP_FILE || null;
const stopRequested = () => STOP_FILE && fs.existsSync(STOP_FILE);
const out = (o) => console.log(JSON.stringify(o));

async function renderPage(context, req, stealthApplied) {
  if (req.stealth && !stealthApplied.value) {
    await applyStealth(context);
    stealthApplied.value = true;
  }
  const page = await context.newPage();
  try {
    await page.goto(req.url, { timeout: 60000, waitUntil: "domcontentloaded" });
    // Cloudflare 5秒盾自动过（无头也能过：等 challenge JS + cf_clearance cookie）
    await waitCloudflare(page, context).catch(() => {});
    if (req.js) await page.evaluate(req.js);
    if (req.remove_overlays) await dismissOverlays(page);
    await runActions(page, req.actions);
    if (req.wait) await page.waitForSelector(req.wait, { timeout: 30000 }).catch(() => {});
    for (let s = 0; s < (req.scroll || 0); s++) {
      await page.evaluate(() => { const _h = document.documentElement ? document.documentElement.scrollHeight : (document.body ? document.body.scrollHeight : 0); window.scrollTo(0, _h); window.dispatchEvent(new Event("scroll")); });
      await sleep(1500);
    }
    const html = await page.evaluate(() => document.documentElement.outerHTML);
    // 渲染成功后把会话 cookie 回传（Python 侧按域名自动存档，下次任务自动复用）
    let cookies = [];
    try { cookies = await context.cookies().catch(() => []); } catch (e) {}
    return { html, url: page.url(), bytes: html.length, cookies };
  } finally {
    await page.close().catch(() => {});
  }
}

async function main() {
  // batch1600 战训：headless-shell 缺失时池进程直接退出，而 9222 调试 Chrome 常在。
  // 启动失败 → 探测本机 9222 → 活着就降级 attach（真实浏览器 + 登录态，更稳）。
  let browser;
  let isCdpFallback = false;
  try {
    browser = await loadChromium().launch({
      headless: process.env.US_HEADLESS !== "0",
      executablePath: CHROMIUM_EXE,
      args: ["--no-sandbox", "--ignore-certificate-errors", "--disable-blink-features=AutomationControlled"],
    });
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
    if (!fbOk) throw launchErr;
    process.stderr.write("[pool] 浏览器启动失败(" + String(launchErr.message || launchErr).slice(0, 100)
      + ") → 降级连接 9222 调试 Chrome\n");
    browser = await loadChromium().connectOverCDP(fallbackCdp, { timeout: 15000 });
    isCdpFallback = true;
  }
  const ss = process.env.US_STORAGE_STATE;
  const ctxOpts = { viewport: { width: 1440, height: 900 } };
  if (ss && fs.existsSync(ss) && !isCdpFallback) ctxOpts.storageState = ss;  // CDP 附带模式忽略外部会话（用真实登录态）
  const proxy = parseProxy(process.env.US_PROXY);
  if (proxy) ctxOpts.proxy = proxy;
  const context = await browser.newContext(ctxOpts);
  const stealthApplied = { value: false };

  const queue = [];
  const waiters = [];
  let closing = false;
  let active = 0;
  let lastActivity = Date.now();

  function touch() { lastActivity = Date.now(); }

  function push(req) {
    if (closing) return;
    touch();
    const waiter = waiters.shift();
    if (waiter) waiter(req);
    else queue.push(req);
  }
  function pop() {
    if (queue.length) return Promise.resolve(queue.shift());
    if (closing) return Promise.resolve(null);
    return new Promise((r) => waiters.push(r));
  }

  async function worker() {
    for (;;) {
      const req = await pop();
      if (!req) return;
      if (req.type === "close") { closing = true; return; }
      if (stopRequested()) { closing = true; return; }
      active++;
      try {
        const r = await renderPage(context, req, stealthApplied);
        out({ id: req.id, html: r.html, url: r.url, bytes: r.bytes, cookies: r.cookies || [] });
      } catch (e) {
        out({ id: req.id, html: "", url: req.url, error: String((e && e.message) || e) });
      } finally {
        active--;
        touch();
      }
    }
  }

  const workers = [];
  for (let i = 0; i < POOL_SIZE; i++) workers.push(worker());

  const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
  const shutdown = () => {
    if (closing) return;
    closing = true;
    // 审查修复：close 后必须排空停在 pop() 上的 worker（否则 done 永不 resolve，
    // 进程挂到被 Python 3s 强杀——优雅退出路径形同虚设）
    waiters.splice(0).forEach((w) => w(null));
  };
  rl.on("line", (line) => {
    line = line.trim();
    if (!line) return;
    try {
      const req = JSON.parse(line);
      if (req.type === "close" || req.close) { shutdown(); return; }
      push(req);
    } catch (e) {
      out({ id: null, error: "请求解析失败: " + String(e) });
    }
  });
  rl.on("close", () => { shutdown(); });

  const done = Promise.all(workers);
  // 空闲超时退出（US_POOL_IDLE_MS，默认 120s）：只在"无活跃渲染 && 队列空"时退出，
  // 绝不在页面渲染中途 kill（修复 30s 硬定时器杀活池的 bug）。
  const idleTimer = setInterval(() => {
    if (closing && active === 0) {
      // 审查修复：closing 后主动收尾（此前 closing 直接 return，定时器空转永不退）
      waiters.splice(0).forEach((w) => w(null));
      clearInterval(idleTimer);
      (isCdpFallback ? browser.disconnect().catch(() => {}) : browser.close().catch(() => {}));
      process.exit(0);
      return;
    }
    if (active === 0 && queue.length === 0 && waiters.length === 0 && Date.now() - lastActivity >= IDLE_MS) {
      shutdown();
      process.exit(0);
    }
  }, 2000);
  // CDP 附加模式 disconnect() 只断连接（close() 对 connected 浏览器同样是断开语义，
  // 这里显式用 disconnect 表达"绝不拥有这个浏览器"）
  done.then(() => {
    clearInterval(idleTimer);
    (isCdpFallback ? browser.disconnect().catch(() => {}) : browser.close().catch(() => {}));
    process.exit(0);
  });
}

main().catch((e) => {
  out({ id: null, error: String((e && e.message) || e) });
  process.exit(1);
});
