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
const { CHROMIUM_EXE, loadChromium, sleep, runActions, applyStealth, dismissOverlays, parseProxy, waitCloudflare, applyResourceBlocking } = require("./browser_common.cjs");

const POOL_SIZE = Math.max(1, parseInt(process.env.US_POOL_SIZE || "3", 10));
const IDLE_MS = Math.max(1000, parseInt(process.env.US_POOL_IDLE_MS || "120000", 10));
const STOP_FILE = process.env.US_STOP_FILE || null;
const stopRequested = () => STOP_FILE && fs.existsSync(STOP_FILE);
const out = (o) => console.log(JSON.stringify(o));

async function renderPage(context, req, stealthApplied, blockingApplied) {
  if (req.stealth && !stealthApplied.value) {
    // 审查修复（H）：flag 的 check-then-act 无锁——多 worker 并发首渲染时
    // applyStealth 可能并发执行（注入的 init 脚本重复叠加）。用 await 锁串行化
    while (stealthApplied.locked) { await new Promise(r => setTimeout(r, 50)); }
    if (!stealthApplied.value) {
      stealthApplied.locked = true;
      try {
        await applyStealth(context);
        stealthApplied.value = true;
      } finally { stealthApplied.locked = false; }
    }
  }
  if (!blockingApplied.value) {
    // 与 stealth 同款锁语义：route 重复注册会叠加 handler 层（行为仍对但浪费）
    while (blockingApplied.locked) { await new Promise(r => setTimeout(r, 50)); }
    if (!blockingApplied.value) {
      blockingApplied.locked = true;
      try {
        await applyResourceBlocking(context);
        blockingApplied.value = true;
      } finally { blockingApplied.locked = false; }
    }
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
    // 审查修复（L）：.catch 已兜底——外层 try 冗余，简化
    cookies = await context.cookies().catch(() => []);
    // OCR R131 终审（M）：html.length 是 UTF-16 码元数，中英混排页偏差可达 30%+；
    // 上游按字节数判"空页/截断"，统一用真实字节
    return { html, url: page.url(), bytes: Buffer.byteLength(html), cookies };
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
  const stealthApplied = { value: false, locked: false };  // locked：并发首渲染互斥
  const blockingApplied = { value: false, locked: false };  // 资源拦截一次性注册（同锁语义）

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
        const r = await renderPage(context, req, stealthApplied, blockingApplied);
        out({ id: req.id, html: r.html, url: r.url, bytes: r.bytes, cookies: r.cookies || [] });
      } catch (e) {
        // 审查修复（H）：shutdown 排空时在途请求必须有错误应答——此前渲染中
        // 被 shutdown 打断的请求静默消失，Python 侧 120s 等到 TimeoutError
        out({ id: req.id, html: "", url: req.url, error: "池正在关闭，请求被中止" });
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
  // 审查修复（P1）：close().catch() 不 await 即同步 exit(0)——CDP 关闭命令还没
  // 写到 socket 进程就退了，等价于没关（headless Chrome 孤儿，R8 修复实际无效）。
  // 统一：close 完成后再 exit
  const closeThenExit = () => {
    Promise.resolve(isCdpFallback ? browser.disconnect() : browser.close())
      .catch(() => {})
      .then(() => process.exit(0));
  };
  const idleTimer = setInterval(() => {
    if (closing && active === 0) {
      // 审查修复：closing 后主动收尾（此前 closing 直接 return，定时器空转永不退）
      waiters.splice(0).forEach((w) => w(null));
      clearInterval(idleTimer);
      closeThenExit();
      return;
    }
    if (active === 0 && queue.length === 0 && waiters.length === 0 && Date.now() - lastActivity >= IDLE_MS) {
      clearInterval(idleTimer);
      // R8 修复：idle 超时曾直接 exit 跳过 browser.close——headless Chrome 成孤儿
      closeThenExit();
    }
  }, 2000);
  // CDP 附加模式 disconnect() 只断连接（close() 对 connected 浏览器同样是断开语义，
  // 这里显式用 disconnect 表达"绝不拥有这个浏览器"）
  done.then(() => {
    clearInterval(idleTimer);
    closeThenExit();
  });
}

main().catch((e) => {
  out({ id: null, error: String((e && e.message) || e) });
  process.exit(1);
});
