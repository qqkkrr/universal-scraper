#!/usr/bin/env node
/**
 * 浏览器桥共享能力（browser_pool.cjs / browser_single.cjs 复用）
 * - runActions:     Firecrawl 风格动作链 click/type/press/select/wait/
 *                   wait_for_selector/scroll/exec/screenshot
 * - applyStealth:   crawl4ai magic-mode 式反检测（遮 webdriver、伪造指纹）
 * - dismissOverlays: 自动关 cookie/遮罩弹窗（browserless blockConsentModals 思路）
 */
const fs = require("node:fs");

const os = require("node:os");
const HOME = process.env.HOME || os.homedir() || "/tmp";
const CHROMIUM_EXE = process.env.PW_EXECUTABLE || `${HOME}/Library/Caches/ms-playwright/chromium_headless_shell-1208/chrome-headless-shell-mac-arm64/chrome-headless-shell`;

// 优先 NODE_PATH 的 playwright（本地 patchright 旧版可能被 WAF 识别），再回退本地 patchright
function loadChromium() {
  const path = require("node:path");
  const nps = (process.env.NODE_PATH || "").split(":").filter(Boolean);
  const cands = nps.map((p) => path.join(p, "playwright")).concat(["patchright", "playwright"]);
  for (const c of cands) {
    try { return require(c).chromium; } catch (e) { /* 下一个 */ }
  }
  throw new Error("找不到 playwright/patchright");
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Firecrawl 风格动作链；失败默认抛错，action.optional=true 可跳过 */
async function runActions(page, actions) {
  if (!Array.isArray(actions)) return;
  for (let i = 0; i < actions.length; i++) {
    const a = actions[i] || {};
    const t = String(a.type || "").toLowerCase();
    try {
      if (t === "wait" || t === "wait_time") {
        await sleep(parseInt(a.ms || a.milliseconds || 1000, 10));
      } else if (t === "wait_for_selector" || t === "waitfor") {
        await page.waitForSelector(a.selector, { timeout: parseInt(a.timeout || 30000, 10) });
      } else if (t === "click") {
        const loc = a.index != null ? page.locator(a.selector).nth(parseInt(a.index, 10)) : page.locator(a.selector).first();
        await loc.click({ timeout: parseInt(a.timeout || 15000, 10) });
        await sleep(parseInt(a.ms || 300, 10));
      } else if (t === "type" || t === "write" || t === "fill") {
        const loc = a.index != null ? page.locator(a.selector).nth(parseInt(a.index, 10)) : page.locator(a.selector).first();
        await loc.fill(a.text || "", { timeout: parseInt(a.timeout || 15000, 10) });
      } else if (t === "press") {
        await page.keyboard.press(a.key || "Enter");
        await sleep(parseInt(a.ms || 200, 10));
      } else if (t === "select") {
        const loc = a.index != null ? page.locator(a.selector).nth(parseInt(a.index, 10)) : page.locator(a.selector).first();
        await loc.selectOption(a.value != null ? a.value : (a.label != null ? { label: a.label } : { index: parseInt(a.index || 0, 10) }));
      } else if (t === "scroll") {
        if (a.direction === "up") await page.evaluate((d) => window.scrollBy(0, -d), a.amount || 600);
        else if (a.direction === "down") await page.evaluate((d) => window.scrollBy(0, d), a.amount || 600);
        else await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
        await page.evaluate(() => window.dispatchEvent(new Event("scroll"))).catch(() => {});
        await sleep(parseInt(a.ms || 800, 10));
      } else if (t === "exec" || t === "js" || t === "execute_javascript") {
        await page.evaluate(a.js || a.code || "");
        await sleep(parseInt(a.ms || 300, 10));
      } else if (t === "screenshot") {
        await page.screenshot({ path: a.path || "/tmp/us_screenshot.png", fullPage: !!a.fullPage });
      } else if (t && t !== "noop") {
        throw new Error(`未知动作类型: ${a.type}`);
      }
    } catch (e) {
      if (!a.optional) throw new Error(`动作[${i}] ${t} 失败: ${(e && e.message) || e}`);
    }
  }
}

/** crawl4ai magic-mode 式反检测：遮自动化痕迹 + 伪造常见指纹 */
const STEALTH_SCRIPT = `
(() => {
  try {
    // —— 基础自动化痕迹 ——
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en-US', 'en'] });
    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
    Object.defineProperty(navigator, 'maxTouchPoints', { get: () => 5 });
    Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
    Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
    Object.defineProperty(navigator, 'vendor', { get: () => 'Google Inc.' });
    Object.defineProperty(navigator, 'platform', { get: () => 'MacIntel' });
    Object.defineProperty(navigator, 'appVersion', { get: () => '5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36' });
    Object.defineProperty(navigator, 'userAgent', { get: () => 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36' });
    window.chrome = window.chrome || { runtime: {} };
    if (!window.chrome.runtime) window.chrome.runtime = {};
    window.chrome.loadTimes = window.chrome.loadTimes || function () { return {}; };
    // 自动化标记常见检测点
    try { delete Object.getOwnPropertyDescriptor(HTMLIFrameElement.prototype, 'contentWindow'); } catch (e) {}
    const _origToString = Function.prototype.toString;
    Function.prototype.toString = function () {
      if (this === window.chrome.runtime) return '[object Object]';
      return _origToString.call(this);
    };
    const origQuery = window.navigator.permissions && window.navigator.permissions.query;
    if (origQuery) {
      window.navigator.permissions.query = (p) =>
        p && p.name === 'notifications'
          ? Promise.resolve({ state: Notification.permission })
          : origQuery(p);
    }
    // —— Canvas 指纹噪声（会话内稳定，playwright_stealth 思路）——
    const _seed = Math.floor(Math.random() * 32);
    const _origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
    CanvasRenderingContext2D.prototype.getImageData = function (x, y, w, h) {
      const img = _origGetImageData.call(this, x, y, w, h);
      for (let i = 0; i < img.data.length; i += 4) {
        img.data[i] = (img.data[i] + _seed) % 256;
        img.data[i + 3] = img.data[i + 3];
      }
      return img;
    };
    // —— WebGL 厂商/渲染器伪造 ——
    const _gl = document.createElement('canvas').getContext('webgl');
    if (_gl) {
      const _origParam = _gl.getParameter.bind(_gl);
      _gl.getParameter = function (p) {
        if (p === 37445) return 'Intel Inc.';
        if (p === 37446) return 'ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)';
        return _origParam(p);
      };
    }
  } catch (e) {}
})();
`;

async function applyStealth(context) {
  await context.addInitScript({ content: STEALTH_SCRIPT }).catch(() => {});
}

/** 自动关闭常见 cookie 弹窗/遮罩（browserless blockConsentModals 思路），失败静默 */
const OVERLAY_SELECTORS = [
  "#onetrust-banner-sdk", ".cookie-banner", ".cookie-consent", "#cookie-banner",
  "#cookie-modal", ".cc-window", ".CybotCookiebotDialog", ".gdpr-banner",
  ".modal-backdrop", ".modal-overlay", ".popup-overlay", "[class*='cookie']",
  "[id*='cookie']", "[class*='consent']", "[id*='consent']", ".fc-dialog",
  ".qc-cmp2-container", "#didomi-host",
];
const ACCEPT_SELECTORS = [
  "#onetrust-accept-btn-handler", ".accept-cookie", "#accept-cookie",
  "#accept", ".accept", ".btn-accept", ".cc-accept", ".cookie-accept",
  "button:has-text('接受')", "button:has-text('同意')", "button:has-text('Accept')",
  "button:has-text('Agree')", "button:has-text('允许')", "#didomi-notice-agree-button",
];

async function dismissOverlays(page) {
  try {
    // 1) Playwright 定位点击常见"接受"按钮（支持 :has-text 等 Playwright 伪类）
    for (const sel of ACCEPT_SELECTORS) {
      try {
        const loc = page.locator(sel).first();
        if (await loc.count()) await loc.click({ timeout: 800 }).catch(() => {});
      } catch (e) { /* 单个选择器失败不影响其它 */ }
    }
    // 2) 浏览器内删除残留遮罩（只用纯 CSS 选择器，避免 :has-text 在 DOM 中报错）
    await page.evaluate((ov) => {
      const clickable = (el) => {
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
      };
      for (const sel of ov) {
        let els;
        try { els = Array.from(document.querySelectorAll(sel)); } catch (e) { continue; }
        for (const el of els) { if (clickable(el)) el.remove(); }
      }
    }, OVERLAY_SELECTORS);
    await sleep(300);
  } catch (e) { /* 静默 */ }
}

module.exports = { CHROMIUM_EXE, loadChromium, sleep, runActions, applyStealth, dismissOverlays, parseProxy, waitCloudflare };

/** 解析代理串（http://user:pass@host:port / socks5://host:port / host:port）为 Playwright proxy 配置 */
function parseProxy(proxy) {
  if (!proxy) return null;
  let s = String(proxy).trim();
  let scheme = "http";
  const m = s.match(/^(https?|socks5|socks4):\/\//i);
  if (m) { scheme = m[1].toLowerCase(); s = s.slice(m[0].length); }
  let username, password;
  const at = s.lastIndexOf("@");
  if (at >= 0) {
    const cred = s.slice(0, at);
    s = s.slice(at + 1);
    const ci = cred.indexOf(":");
    if (ci >= 0) { username = decodeURIComponent(cred.slice(0, ci)); password = decodeURIComponent(cred.slice(ci + 1)); }
    else username = decodeURIComponent(cred);
  }
  return { server: `${scheme}://${s}`, username: username || undefined, password: password || undefined };
}

// ============================================================
// Cloudflare 5秒盾自动过（反反爬：无头也能过，等 challenge JS 执行完成）
// 检测 "Just a moment" / cf-chl- / challenge-platform / __cf_chl_tk →
// 轮询 cf_clearance cookie / 页面变化，最多等 18s，然后 reload 一次再确认
// ============================================================
async function waitCloudflare(page, context, timeoutMs = 18000) {
  const looksLikeChallenge = async () => {
    try {
      const t = await page.evaluate(() => {
        const txt = (document.body ? document.body.innerText : "") || "";
        const hasCfMark = !!document.querySelector("#challenge-form, [id*=challenge], [class*=challenge], [class*=cf-chl]")
          || /just a moment|cf-chl|challenge-platform|__cf_chl_tk/i.test(txt + location.search);
        return { hasCfMark, title: document.title || "", len: txt.length };
      }).catch(() => ({ hasCfMark: false, title: "", len: 0 }));
      return t.hasCfMark || /just a moment|attention required|cf-chl/i.test(t.title);
    } catch (e) { return false; }
  };
  const hasClearance = async () => {
    try {
      const ck = await context.cookies();
      return ck.some(c => c.name === "cf_clearance" && c.value);
    } catch (e) { return false; }
  };
  try {
    if (!(await looksLikeChallenge()) || await hasClearance()) return true;
  } catch (e) { return true; }
  // 等 challenge JS 自动执行（轮询 cookie + 页面退出 challenge）
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    await sleep(2000);
    try {
      if (await hasClearance()) break;
      if (!(await looksLikeChallenge())) break;
    } catch (e) { break; }
  }
  // 再 reload 一次让 challenge 后页面正常加载（Cloudflare 常见流程）
  try {
    if (await hasClearance() || !(await looksLikeChallenge())) {
      await page.reload({ waitUntil: "domcontentloaded", timeout: 30000 }).catch(() => {});
      await sleep(4000);
    }
  } catch (e) {}
  return await hasClearance() || !(await looksLikeChallenge());
}
