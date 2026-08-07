#!/usr/bin/env node
/* 万能浏览器执行器：Playwright + stealth + 动作序列 + 提取规则 + 网络/WS 收集。
 *
 * 用法:
 *   node scripts/browser_agent.cjs \
 *     --url http://... \
 *     --actions '[{"type":"wait","ms":800},{"type":"drag","from":{"x":20,"y":60},"to":{"x":260,"y":60}}]' \
 *     --extract '[{"name":"token","css":"#token"},{"name":"title","js":"document.title"}]' \
 *     --stealth 1 --network 1 --cookies 1 --viewport '{"width":1280,"height":800}'
 *
 * 输出 JSONL：{type:"result", ...} / {type:"log", ...} / {type:"error", ...}
 * ⚠️ 仅测试专用（tests/learnspider_solver.py），产品取数请走 modules/fetchers 或 quick。
 */
const fs = require("node:fs");
const { chromium } = (() => {
  try { return require("playwright"); } catch (e) { return require("playwright-core"); }
})();

const CHROME_EXE = process.env.UNIVERSAL_SCRAPER_CHROME ||
  `${process.env.HOME}/Library/Caches/ms-playwright/chromium-1208/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing`;

function arg(name, dflt) {
  const i = process.argv.indexOf("--" + name);
  if (i === -1) return dflt;
  const v = process.argv[i + 1];
  return v === undefined ? true : v;
}
function argJson(name, dflt) {
  const v = arg(name, null);
  if (v === null || v === true) return dflt;
  try { return JSON.parse(v); } catch (e) { return dflt; }
}

const STEALTH_INIT = `(() => {
  // 最小 stealth：去掉 automation 痕迹（niespodd/browser-fingerprinting 思路）
  Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
  try { delete Navigator.prototype.webdriver; } catch (e) {}
  try { Object.defineProperty(Navigator.prototype, 'webdriver', { value: undefined, configurable: true }); } catch (e) {}
  if (!window.chrome) { window.chrome = { runtime: { id: 'fake-extension-id-123' }, loadTimes: () => ({}), csi: () => ({}) }; } else if (!window.chrome.runtime || !window.chrome.runtime.id) { try { window.chrome.runtime = window.chrome.runtime || {}; window.chrome.runtime.id = 'fake-extension-id-123'; } catch (e) {} }
  try {
    const pl = ['PDF Viewer', 'Chrome PDF Viewer', 'Chromium PDF Viewer', 'Microsoft Edge PDF Viewer',
      'WebKit built-in PDF', 'Portable Document Format', 'Chrome PDF Plugin'];
    Object.defineProperty(navigator, 'plugins', { get: () => {
      const arr = [];
      pl.forEach((n, i) => { arr.push({ name: n, filename: n + '.plugin', description: n, length: 1, item: (j) => arr[j], namedItem: () => arr[0] }); arr.length = i + 1; });
      return arr;
    }});
    Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
    try {
      const mimeList = [
        { type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format' },
        { type: 'text/pdf', suffixes: 'pdf', description: 'Portable Document Format' },
        { type: 'application/x-google-chrome-pdf', suffixes: 'pdf', description: 'Portable Document Format' },
        { type: 'application/x-nacl', suffixes: '', description: 'Native Client Executable' },
        { type: 'application/x-pnacl', suffixes: '', description: 'Portable Native Client Executable' }
      ];
      Object.defineProperty(navigator, 'mimeTypes', { get: () => {
        const obj = { length: mimeList.length, item: (j) => mimeList[j] || null, namedItem: (n) => mimeList.find(x => x.type === n) || null };
        mimeList.forEach((m, i) => { obj[i] = { ...m, enabledPlugin: null }; obj[m.type] = { ...m, enabledPlugin: null }; });
        return obj;
      }});
    } catch (e) {}
  } catch (e) {}
  if (window.outerWidth === 0) { try { Object.defineProperty(window, 'outerWidth', { get: () => innerWidth }); } catch (e) {} }
  const origQuery = window.navigator.permissions && window.navigator.permissions.query;
  if (origQuery) {
    window.navigator.permissions.query = (p) => p && p.name === 'notifications'
      ? Promise.resolve({ state: Notification.permission }) : origQuery(p);
  }
})();`;

async function main() {
  const url = arg("url", "");
  const actions = argJson("actions", []);
  const extract = argJson("extract", []);
  const waitMs = parseInt(arg("wait-ms", "800"), 10);
  const stealth = parseInt(arg("stealth", "1"), 10) === 1;
  const network = parseInt(arg("network", "1"), 10) === 1;
  const cookies = parseInt(arg("cookies", "1"), 10) === 1;
  const html = parseInt(arg("html", "0"), 10) === 1;
  const viewport = argJson("viewport", { width: 1280, height: 800 });
  const ua = arg("ua", null);
  const locale = arg("locale", "zh-CN");
  const geo = argJson("geo", null);
  const timeout = parseInt(arg("timeout", "45000"), 10);

  const out = (obj) => { process.stdout.write(JSON.stringify(obj) + "\n"); };
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));

  let browser;
  try {
    browser = await chromium.launch({
      headless: true,
      executablePath: fs.existsSync(CHROME_EXE) ? CHROME_EXE : undefined,
      args: ["--disable-blink-features=AutomationControlled", "--no-sandbox", "--lang=zh-CN"],
    });
    const ctx = await browser.newContext({
      viewport, locale,
      userAgent: ua || undefined,
      geolocation: geo || undefined,
      permissions: geo ? ["geolocation"] : undefined,
      timezoneId: "Asia/Shanghai",
      serviceWorkers: "allow",
    });
    if (stealth) await ctx.addInitScript(STEALTH_INIT);
    const page = await ctx.newPage();

    const netEvents = [];
    const wsFrames = [];
    if (network) {
      page.on("response", async (res) => {
        const entry = {
          url: res.url(), status: res.status(),
          headers: res.headers(), // 含 set-cookie 数组
        };
        try {
          const ct = (res.headers()["content-type"] || "").toLowerCase();
          if (ct.includes("json") || ct.includes("text")) {
            entry.body = (await res.text()).slice(0, 20000);
          }
        } catch (e) {}
        if (netEvents.length < 200) netEvents.push(entry);  // 网络收集上限，防结果爆炸
      });
      page.on("websocket", (ws) => {
        ws.on("framereceived", (ev) => {
          if (wsFrames.length >= 500) return;  // WS 帧收集上限
          try { wsFrames.push(JSON.parse(ev.payload)); } catch (e) { wsFrames.push(String(ev.payload).slice(0, 500)); }
        });
      });
    }

    // 默认先导航（除非动作序列以 goto 开头）
    if (!(actions.length > 0 && actions[0].type === "goto")) {
      try {
        await page.goto(url, { waitUntil: "domcontentloaded", timeout });
      } catch (e) {
        out({ type: "log", level: "warn", msg: `goto failed: ${e.message.split("\n")[0]}` });
      }
    }

    // 执行动作序列
    for (const a of actions) {
      const t = a.type;
      try {
        if (t === "goto") {
          await page.goto(a.url || url, { waitUntil: "domcontentloaded", timeout });
        } else if (t === "wait") {
          await sleep(parseInt(a.ms || a.timeout || 500, 10));
        } else if (t === "wait_selector") {
          await page.waitForSelector(a.selector, { timeout: parseInt(a.timeout || 10000, 10), state: a.state || "visible" });
        } else if (t === "click") {
          await page.click(a.selector, { timeout: parseInt(a.timeout || 10000, 10) });
        } else if (t === "fill") {
          await page.fill(a.selector, a.value || "");
        } else if (t === "press") {
          await page.keyboard.press(a.key || "Enter");
        } else if (t === "scroll") {
          if (a.selector) await page.locator(a.selector).scrollIntoViewIfNeeded();
          else await page.mouse.wheel(0, parseInt(a.y || a.px || 800, 10));
        } else if (t === "mouse_move") {
          // 带轨迹的鼠标移动（挑战 7/35/50）
          const steps = parseInt(a.steps || 8, 10);
          const x0 = a.from.x, y0 = a.from.y, x1 = a.to.x, y1 = a.to.y;
          await page.mouse.move(x0, y0);
          for (let i = 1; i <= steps; i++) {
            const t = i / steps;
            const ease = t * t * (3 - 2 * t);
            await page.mouse.move(x0 + (x1 - x0) * ease, y0 + (y1 - y0) * ease + Math.sin(i * 1.7) * 1.5);
            await sleep(parseInt(a.step_ms || 20, 10));
          }
        } else if (t === "drag") {
          // 拖拽：先移动到起点，按下，轨迹移动到终点，松开
          await page.mouse.move(a.from.x, a.from.y);
          await page.mouse.down();
          const steps = parseInt(a.steps || 12, 10);
          for (let i = 1; i <= steps; i++) {
            const t = i / steps;
            await page.mouse.move(a.from.x + (a.to.x - a.from.x) * t,
              a.from.y + (a.to.y - a.from.y) * t + Math.sin(i * 1.3) * 1.2);
            await sleep(parseInt(a.step_ms || 15, 10));
          }
          await page.mouse.up();
        } else if (t === "click_point") {
          await page.mouse.click(a.x, a.y);
        } else if (t === "evaluate") {
          await page.evaluate(a.expression);
        } else if (t === "screenshot") {
          await page.screenshot({ path: a.path || "/tmp/browser_agent_shot.png" });
        }
      } catch (e) {
        out({ type: "log", level: "warn", msg: `action ${t} failed: ${e.message.split("\n")[0]}` });
      }
    }

    await sleep(waitMs);

    // 提取
    const extracted = {};
    for (const ex of extract) {
      try {
        if (ex.js) {
          const v = await page.evaluate(ex.js);
          extracted[ex.name] = (typeof v === "string" || typeof v === "number") ? v : JSON.stringify(v);
        } else if (ex.css) {
          if (ex.attr) {
            extracted[ex.name] = await page.getAttribute(ex.css, ex.attr).catch(() => null);
          } else {
            // 先看是否存在：textContent 对不存在的选择器会 auto-wait 30s，必须避免
            const cnt = await page.locator(ex.css).count().catch(() => 0);
            extracted[ex.name] = cnt > 0 ? (await page.textContent(ex.css).catch(() => null)) : null;
          }
        }
      } catch (e) {
        extracted[ex.name] = null;
      }
    }

    const result = {
      type: "result",
      url: page.url(),
      title: await page.title().catch(() => ""),
      extracted,
      network: network ? netEvents : [],
      ws_frames: wsFrames,
      cookies: cookies ? await ctx.cookies() : [],
      html: html ? (await page.content()).slice(0, 300000) : undefined,
    };
    out(result);
  } catch (e) {
    out({ type: "error", message: e.message, stack: (e.stack || "").split("\n").slice(0, 5) });
  } finally {
    if (browser) await browser.close().catch(() => {});
  }
}

main();
