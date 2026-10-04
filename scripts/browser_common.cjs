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
const path = require("node:path");

// 浏览器可执行文件解析链（MediaCrawler browser_launcher 思路；2026-09 实证：
// playwright 升级换缓存修订版后，硬编码路径让全部浏览器功能一起瘫）：
//   1. PW_EXECUTABLE 环境变量
//   2. ms-playwright 缓存扫描（任意修订版，headless-shell 与全量 chromium 都认）
//   3. 系统 Chrome/Edge（macOS / Windows / Linux 全渠道，含 Beta/Dev/Canary）
//   4. 兜底旧硬编码路径（保证错误信息仍然可读）
function _isExe(p) {
  // 审查修复（L）：`fs.accessSync() === undefined` 依赖未文档化的返回值——
  // 改用 try/catch 异常语义（等价但意图明确）
  try { fs.accessSync(p, fs.constants.X_OK); return fs.statSync(p).isFile(); }
  catch { return false; }
}

function _scanPlaywrightCache() {
  const roots = [
    path.join(HOME, "Library", "Caches", "ms-playwright"),                 // macOS
    path.join(HOME, ".cache", "ms-playwright"),                            // Linux
    path.join(process.env.LOCALAPPDATA || path.join(HOME, "AppData", "Local"), "ms-playwright"), // Windows
  ];
  const exeNames = new Set(["chrome", "chrome.exe", "chromium", "chromium.exe",
                            "chrome-headless-shell", "chrome-headless-shell.exe"]);
  // 全量 chromium 优先于 headless-shell（审查修复：有头模式用 headless-shell
  // 必挂——browser_single --headless 0 / captcha_bridge own 模式都是有头），
  // 修订版按数字倒序（字典序会让 999 排在 1000 前面）
  const groups = [["chromium-", true], ["chromium_headless_shell-", false]];
  for (const root of roots) {
    for (const [prefix] of groups) {
      let dirs = [];
      try { dirs = fs.readdirSync(root).filter((d) => d.startsWith(prefix)); } catch { continue; }
      dirs.sort((a, b) => (parseInt(b.replace(/\D+/g, ""), 10) || 0) - (parseInt(a.replace(/\D+/g, ""), 10) || 0));
      for (const d of dirs) {
        const base = path.join(root, d);
        // 常见布局直查（快路径）
        const fast = prefix === "chromium-"
          ? [
              path.join(base, "chrome-mac-arm64", "Chromium.app", "Contents", "MacOS", "Chromium"),
              path.join(base, "chrome-mac", "Chromium.app", "Contents", "MacOS", "Chromium"),
              path.join(base, "chrome-linux", "chrome"),
              path.join(base, "chrome-win", "chrome.exe"),
            ]
          : [
              path.join(base, "chrome-headless-shell-mac-arm64", "chrome-headless-shell"),
              path.join(base, "chrome-headless-shell-mac", "chrome-headless-shell"),
              path.join(base, "chrome-headless-shell-linux64", "chrome-headless-shell"),
              path.join(base, "chrome-headless-shell-win64", "chrome-headless-shell.exe"),
            ];
        for (const f of fast) { if (_isExe(f)) return f; }
        // 未知布局：限深 3 层搜可执行名
        const stack = [[base, 0]];
        while (stack.length) {
          const [dir, depth] = stack.pop();
          let entries = [];
          try { entries = fs.readdirSync(dir, { withFileTypes: true }); } catch { continue; }
          for (const e of entries) {
            const f = path.join(dir, e.name);
            if (e.isDirectory()) { if (depth < 3 && !e.name.startsWith(".")) stack.push([f, depth + 1]); }
            else if (exeNames.has(e.name.toLowerCase()) && _isExe(f)) return f;
          }
        }
      }
    }
  }
  return "";
}

function _scanSystemBrowsers() {
  const sys = process.platform;
  let cands = [];
  if (sys === "darwin") {
    cands = [
      "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
      "/Applications/Google Chrome Beta.app/Contents/MacOS/Google Chrome Beta",
      "/Applications/Google Chrome Dev.app/Contents/MacOS/Google Chrome Dev",
      "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
      "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
      "/Applications/Microsoft Edge Beta.app/Contents/MacOS/Microsoft Edge Beta",
      "/Applications/Chromium.app/Contents/MacOS/Chromium",
      "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    ];
  } else if (sys === "win32") {
    const pf = process.env["ProgramFiles"] || "C:\\Program Files";
    const pf86 = process.env["ProgramFiles(x86)"] || "C:\\Program Files (x86)";
    const lap = process.env["LOCALAPPDATA"] || path.join(HOME, "AppData", "Local");
    cands = [
      path.join(pf, "Google", "Chrome", "Application", "chrome.exe"),
      path.join(pf86, "Google", "Chrome", "Application", "chrome.exe"),
      path.join(lap, "Google", "Chrome", "Application", "chrome.exe"),
      path.join(pf, "Microsoft", "Edge", "Application", "msedge.exe"),
      path.join(pf86, "Microsoft", "Edge", "Application", "msedge.exe"),
      path.join(lap, "Chromium", "Application", "chrome.exe"),
    ];
  } else {
    cands = ["/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
             "/usr/bin/google-chrome-beta", "/usr/bin/chromium-browser",
             "/usr/bin/chromium", "/snap/bin/chromium",
             "/usr/bin/microsoft-edge", "/usr/bin/microsoft-edge-stable"];
  }
  for (const c of cands) { if (_isExe(c)) return c; }
  return "";
}

function resolveChromiumExe() {
  if (process.env.PW_EXECUTABLE) {
    if (_isExe(process.env.PW_EXECUTABLE)) return process.env.PW_EXECUTABLE;
    // 审查修复：用户显式指定的路径失效必须喊出来——静默改道会引发
    // "指纹/行为莫名漂移"式的疑难杂症
    console.error(`[browser_common] PW_EXECUTABLE 不存在或不可执行，已忽略: ${process.env.PW_EXECUTABLE}`);
  }
  const cached = _scanPlaywrightCache();
  if (cached) return cached;
  const sys = _scanSystemBrowsers();
  if (sys) return sys;
  // 兜底旧硬编码：不存在也让下游报错信息可读（playwright 自己的报错带处方）
  return `${HOME}/Library/Caches/ms-playwright/chromium_headless_shell-1208/chrome-headless-shell-mac-arm64/chrome-headless-shell`;
}

const CHROMIUM_EXE = resolveChromiumExe();

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

/** Firecrawl 风格动作链；失败默认抛错，action.optional=true 可跳过。
 * epub 战训修复（2026-09）：
 *   - fill/type/write 同时接受 text 与 value 键（文档曾只写一个，另一个静默填空）
 *   - 新增 wait_for_url 动作：{type, url_pattern, timeout} —— js 触发跳转后
 *     page.evaluate 会炸（Execution context destroyed），此动作用 waitForURL 容错
 *   - 动作失败带序号+类型 loudly 抛出（不再静默沉在 diag 日志） */
async function runActions(page, actions) {
  if (!Array.isArray(actions)) return;
  for (let i = 0; i < actions.length; i++) {
    const a = actions[i] || {};
    const t = String(a.type || "").toLowerCase();
    // R2 复查修复（P2-4）：wait_ms/timeout_ms 是文档与 browser_generic 实际支持
    // 的键——pool/single 路径此前静默丢弃（validate 通过但等待不发生）。
    // 统一在此归一：a.timeout_ms 覆盖 a.timeout；动作尾部统一补 a.wait_ms 睡眠
    const _to = (a) => parseInt(a.timeout_ms || a.timeout || 15000, 10);
    const _postWait = async () => {
      const w = parseInt(a.wait_ms || a.ms || 0, 10);
      if (w > 0) await sleep(w);
    };
    try {
      if (t === "wait" || t === "wait_time") {
        if (a.selector) {
          // wait+selector 形态（fetchers 与 browser_generic 消费的同款契约）
          await page.waitForSelector(a.selector, { timeout: _to(a) });
        }
        await sleep(parseInt(a.ms || a.milliseconds || a.wait_ms || 1000, 10));
      } else if (t === "wait_for_selector" || t === "waitfor") {
        await page.waitForSelector(a.selector, { timeout: _to(a) === 15000 ? 30000 : _to(a) });
      } else if (t === "wait_for_url" || t === "wait_for_navigation") {
        // 导航容错（epub 战训）：js/click 触发整页跳转后，用 URL 模式等新页面就绪
        const pat = a.url_pattern || a.urlPattern || a.url;
        if (!pat) throw new Error("wait_for_url 需要 url_pattern（字符串子串或 /正则/）");
        // OCR R131（M）：/regex/flags 解析曾用 lastIndexOf("/") 取 flags——
        // 正则体内含 / 时截断错位。收紧：仅匹配尾部 /flags 段（flags 只含字母）
        let isRe = false, reBody = "", reFlags = "";
        if (typeof pat === "string" && pat.startsWith("/") && pat.length > 2) {
          const m = pat.match(/^\/(.*)\/([a-z]*)$/s);
          if (m) { reBody = m[1]; reFlags = m[2]; isRe = true; }
        }
        const re = isRe ? new RegExp(reBody, reFlags) : null;
        const to = _to(a) === 15000 ? 30000 : _to(a);
        const t0 = Date.now();
        let matched = false;
        while (Date.now() - t0 < to) {
          const u = page.url();
          // R98 修复（P2）：命中曾用 return 直接退出整条动作链——后续步骤
          // 全部静默丢弃（epub 的 submit→wait→click 模式断在半路）。改 break
          if (re ? re.test(u) : String(u).includes(pat)) { matched = true; break; }
          await sleep(300);
        }
        if (!matched)
          throw new Error(`等待 URL 匹配 ${pat} 超时（${to}ms，当前 ${page.url()}）`);
      } else if (t === "click") {
        const loc = a.index != null ? page.locator(a.selector).nth(parseInt(a.index, 10)) : page.locator(a.selector).first();
        await loc.click({ timeout: parseInt(a.timeout || 15000, 10) });
        await sleep(parseInt(a.ms || 300, 10));
      } else if (t === "type" || t === "write" || t === "fill") {
        const loc = a.index != null ? page.locator(a.selector).nth(parseInt(a.index, 10)) : page.locator(a.selector).first();
        await loc.fill(a.value != null ? a.value : (a.text != null ? a.text : ""), { timeout: parseInt(a.timeout || 15000, 10) });
      } else if (t === "type_real" || t === "fill_real") {
        // 商标网战训（2026-09）：Angular/React 组件 fill() 合成事件不触发
        // （自动补全下拉不展开、model 不更新）。降级第 2 阶：真实键盘事件——
        // 先点击聚焦再逐键输入（每键触发完整 keydown/keypress/input/keyup）
        const loc = a.index != null ? page.locator(a.selector).nth(parseInt(a.index, 10)) : page.locator(a.selector).first();
        await loc.click({ timeout: parseInt(a.timeout || 15000, 10) });
        await loc.fill("", { timeout: 5000 }).catch(() => {});  // 清已有值（失败容忍）
        const val = String(a.value != null ? a.value : (a.text != null ? a.text : ""));
        if (typeof loc.pressSequentially === "function") {
          await loc.pressSequentially(val, { delay: parseInt(a.delay_ms || a.ms || 80, 10), timeout: parseInt(a.timeout || 20000, 10) });
        } else {
          await page.keyboard.type(val, { delay: parseInt(a.delay_ms || a.ms || 80, 10) });
        }
        await sleep(parseInt(a.ms || 300, 10));
      } else if (t === "press") {
        await page.keyboard.press(a.key || "Enter");
        await sleep(parseInt(a.ms || 200, 10));
      } else if (t === "select") {
        // OCR R131（M）：a.value/a.label/a.index 全缺时曾静默选 index 0（第一项）——
        // 三者全空说明配置错误，跳过该步骤并告警比瞎选安全
        if (a.value == null && a.label == null && a.index == null) {
          console.error(`[browser_common] select 步骤缺少 value/label/index，跳过: ${a.selector || "?"}`);
        } else {
          const loc = a.index != null ? page.locator(a.selector).nth(parseInt(a.index, 10)) : page.locator(a.selector).first();
          await loc.selectOption(a.value != null ? a.value : (a.label != null ? { label: a.label } : { index: parseInt(a.index, 10) }));
        }
      } else if (t === "scroll") {
        if (a.direction === "up") await page.evaluate((d) => window.scrollBy(0, -d), a.amount || 600);
        else if (a.direction === "down") await page.evaluate((d) => window.scrollBy(0, d), a.amount || 600);
        else await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
        await page.evaluate(() => window.dispatchEvent(new Event("scroll"))).catch(() => {});
        await sleep(parseInt(a.ms || 800, 10));
      } else if (t === "exec" || t === "js" || t === "execute_javascript") {
        try {
          await page.evaluate(a.js || a.code || "");
        } catch (e) {
          // epub 战训：js 触发 form.submit()/location 跳转时 evaluate 必炸
          // （执行上下文随导航销毁）——这不是脚本错误，等导航完成即可
          const msg = String((e && e.message) || e);
          if (/Execution context was destroyed|navigator is not defined|Target closed/i.test(msg)) {
            await sleep(parseInt(a.ms || 800, 10));
          } else {
            throw e;
          }
        }
        await sleep(parseInt(a.ms || 300, 10));
      } else if (t === "screenshot") {
        await page.screenshot({ path: a.path || "/tmp/us_screenshot.png", fullPage: !!a.fullPage });
      } else if (t && t !== "noop") {
        throw new Error(`未知动作类型: ${a.type}`);
      }
      // R2（P2-4）：所有动作尾部统一尊重 wait_ms（此前仅部分动作读 ms）
      if (t !== "wait" && t !== "wait_time" && a.wait_ms) {
        await _postWait();
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

/** R20 隐身开关：anti_bot.stealth_opts 的内置广告/追踪域名清单。
 *  刻意保持"小而准"（常见第三方追踪/广告域），用户可用 blocked_domains
 *  追加自己的清单；不照搬大清单（体积与误杀都要付代价）。 */
const AD_DOMAINS = [
  "doubleclick.net", "googlesyndication.com", "googleadservices.com",
  "google-analytics.com", "googletagmanager.com", "googletagservices.com",
  "adservice.google.com", "ads.yahoo.com", "adnxs.com", "criteo.com",
  "criteo.net", "taboola.com", "outbrain.com", "pubmatic.com",
  "rubiconproject.com", "openx.net", "casalemedia.com", "smartadserver.com",
  "scorecardresearch.com", "quantserve.com", "moatads.com", "adform.net",
  "sharethrough.com", "teads.tv", "zedo.com", "sizmek.com",
];

/** 解析 --stealthOpts '<json>'（Python 侧 anti_bot.stealth_opts 透传）。
 *  返回 {args, ctx, blocked, blockAds}；非法 JSON/类型一律返回空（不阻塞主流程）。
 *  键：hide_canvas / allow_webgl / block_webrtc / dns_over_https / extra_flags /
 *      timezone / locale / user_agent / blocked_domains / block_ads */
function parseStealthOpts(raw) {
  const out = { args: [], ctx: {}, blocked: [], blockAds: false };
  if (!raw || typeof raw !== "string") return out;
  let o;
  try { o = JSON.parse(raw); } catch (e) { return out; }
  if (!o || typeof o !== "object") return out;
  const push = (a) => { if (typeof a === "string" && a.startsWith("--") && a.length < 200) out.args.push(a); };
  if (o.hide_canvas === true) push("--fingerprinting-canvas-image-data-noise");
  if (o.allow_webgl === false) {
    push("--disable-webgl"); push("--disable-webgl-image-chromium"); push("--disable-webgl2");
  }
  if (o.block_webrtc === true) {
    push("--webrtc-ip-handling-policy=disable_non_proxied_udp");
    push("--force-webrtc-ip-handling-policy");
  }
  if (o.dns_over_https === true) {
    push("--dns-over-https-mode=secure");
    push("--dns-over-https-templates=https://cloudflare-dns.com/dns-query");
  }
  if (Array.isArray(o.extra_flags)) o.extra_flags.slice(0, 10).forEach(push);
  if (typeof o.timezone === "string" && o.timezone) out.ctx.timezoneId = o.timezone;
  if (typeof o.locale === "string" && o.locale) out.ctx.locale = o.locale;
  if (typeof o.user_agent === "string" && o.user_agent) out.ctx.userAgent = o.user_agent;
  if (Array.isArray(o.blocked_domains)) {
    for (const d of o.blocked_domains.slice(0, 200)) {
      if (typeof d === "string" && /^[a-z0-9.-]+$/i.test(d)) out.blocked.push(d.toLowerCase());
    }
  }
  out.blockAds = o.block_ads === true;
  return out;
}

/** 域名匹配：精确或子域（a.b.com 命中 b.com）。返回命中的域名或 null。 */
function matchBlockedDomain(url, domains) {
  let host = "";
  try { host = new URL(url).hostname.toLowerCase(); } catch (e) { return null; }
  for (const d of domains) {
    if (host === d || host.endsWith("." + d)) return d;
  }
  return null;
}

/** 资源拦截（对标 Crawlee blockRequests）：默认阻断 font/media（零功能风险，
 * 文本/DOM 采集不受影响；字体图标变方块不影响字段抽取），image 默认保留——
 * 本插件有图片采集用例（xhs 图片直链等）。
 * 环境变量：US_BLOCK_IMAGES=1 追加阻断图片（纯文本任务提速 2-5 倍）；
 *          US_BLOCK_EXTRA="websocket,other" 追加任意 resourceType；
 *          US_BLOCK_ADS=1 / US_BLOCK_DOMAINS="a.com,b.com"（R20 域名级）。
 * 失败静默（route 注册失败不阻塞主流程）；abort/continue 均 catch（页面关闭竞态）。 */
async function applyResourceBlocking(context, opts = {}) {
  const env = (k) => process.env[k];
  const types = new Set(["font", "media"]);
  if (opts.blockImages === true || (opts.blockImages === undefined && env("US_BLOCK_IMAGES") === "1")) {
    types.add("image");
  }
  if (typeof env("US_BLOCK_EXTRA") === "string" && env("US_BLOCK_EXTRA").trim()) {
    for (const t of env("US_BLOCK_EXTRA").split(",")) {
      if (t.trim()) types.add(t.trim());
    }
  }
  // R20：域名级阻断（广告清单 + 用户清单；env 兜底）
  const domains = [];
  if (opts.blockAds === true || (opts.blockAds === undefined && env("US_BLOCK_ADS") === "1")) {
    domains.push(...AD_DOMAINS);
  }
  if (Array.isArray(opts.blockedDomains)) domains.push(...opts.blockedDomains);
  if (typeof env("US_BLOCK_DOMAINS") === "string" && env("US_BLOCK_DOMAINS").trim()) {
    domains.push(...env("US_BLOCK_DOMAINS").split(",").map((s) => s.trim().toLowerCase()).filter(Boolean));
  }
  if (!types.size && !domains.length) return;
  try {
    await context.route("**/*", (route) => {
      let rt = null;
      let u = "";
      try { rt = route.request().resourceType(); u = route.request().url(); } catch (e) { /* 竞态 */ }
      if (rt && types.has(rt)) return route.abort("blockedbyclient").catch(() => {});
      if (u && domains.length && matchBlockedDomain(u, domains)) {
        return route.abort("blockedbyclient").catch(() => {});
      }
      return route.continue().catch(() => {});
    });
  } catch (e) { /* 静默：老版本内核不支持 route 时退化为无拦截 */ }
}

module.exports = { CHROMIUM_EXE, loadChromium, sleep, runActions, applyStealth, dismissOverlays, parseProxy, waitCloudflare, applyResourceBlocking, parseStealthOpts, matchBlockedDomain, AD_DOMAINS };

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
