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
const { CHROMIUM_EXE, loadChromium, sleep, runActions, applyStealth, dismissOverlays, parseProxy } = require("./browser_common.cjs");

const POOL_SIZE = Math.max(1, parseInt(process.env.US_POOL_SIZE || "3", 10));
const out = (o) => console.log(JSON.stringify(o));

async function renderPage(context, req, stealthApplied) {
  if (req.stealth && !stealthApplied.value) {
    await applyStealth(context);
    stealthApplied.value = true;
  }
  const page = await context.newPage();
  try {
    await page.goto(req.url, { timeout: 60000, waitUntil: "domcontentloaded" });
    if (req.js) await page.evaluate(req.js);
    if (req.remove_overlays) await dismissOverlays(page);
    await runActions(page, req.actions);
    if (req.wait) await page.waitForSelector(req.wait, { timeout: 30000 }).catch(() => {});
    for (let s = 0; s < (req.scroll || 0); s++) {
      await page.evaluate(() => { window.scrollTo(0, document.body.scrollHeight); window.dispatchEvent(new Event("scroll")); });
      await sleep(1500);
    }
    const html = await page.evaluate(() => document.documentElement.outerHTML);
    return { html, url: page.url(), bytes: html.length };
  } finally {
    await page.close().catch(() => {});
  }
}

async function main() {
  const browser = await loadChromium().launch({
    headless: process.env.US_HEADLESS !== "0",
    executablePath: CHROMIUM_EXE,
    args: ["--no-sandbox", "--disable-blink-features=AutomationControlled"],
  });
  const ss = process.env.US_STORAGE_STATE;
  const ctxOpts = { viewport: { width: 1440, height: 900 } };
  if (ss && fs.existsSync(ss)) ctxOpts.storageState = ss;
  const proxy = parseProxy(process.env.US_PROXY);
  if (proxy) ctxOpts.proxy = proxy;
  const context = await browser.newContext(ctxOpts);
  const stealthApplied = { value: false };

  const queue = [];
  const waiters = [];
  let closing = false;

  function push(req) {
    if (closing) return;
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
      try {
        const r = await renderPage(context, req, stealthApplied);
        out({ id: req.id, html: r.html, url: r.url, bytes: r.bytes });
      } catch (e) {
        out({ id: req.id, html: "", url: req.url, error: String((e && e.message) || e) });
      }
    }
  }

  const workers = [];
  for (let i = 0; i < POOL_SIZE; i++) workers.push(worker());

  const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
  rl.on("line", (line) => {
    line = line.trim();
    if (!line) return;
    try {
      const req = JSON.parse(line);
      if (req.type === "close" || req.close) { closing = true; return; }
      push(req);
    } catch (e) {
      out({ id: null, error: "请求解析失败: " + String(e) });
    }
  });
  rl.on("close", () => { closing = true; });

  const done = Promise.all(workers);
  const timer = setTimeout(() => { closing = true; process.exit(0); }, 30000);
  done.then(() => { clearTimeout(timer); browser.close().catch(() => {}); process.exit(0); });
}

main().catch((e) => {
  out({ id: null, error: String((e && e.message) || e) });
  process.exit(1);
});
