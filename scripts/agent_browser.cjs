#!/usr/bin/env node
/**
 * LLM 浏览器代理桥（browser-use 路线）
 *
 * 长驻进程：stdin 逐行 JSON 指令，stdout 逐行 JSON 响应。
 * LLM 通过 snapshot 观察页面，发出 click/type/scroll/extract 动作，
 * 对没精配的网站自适应（不依赖预写选择器）。
 *
 * 启动：--cdp http://127.0.0.1:9222（优先附着用户已登录 Chrome=登录态复用）
 *       --headless 1（默认，独立无头浏览器）
 *
 * 指令：
 *   {"op":"goto","url":...}
 *   {"op":"snapshot","max_links":30,"max_text":1500}
 *   {"op":"click","selector":...}
 *   {"op":"type","selector":...,"text":...}
 *   {"op":"press","key":"Enter"}
 *   {"op":"scroll","dir":"down|up|bottom"}
 *   {"op":"wait","ms":1000}
 *   {"op":"html","max_chars":8000}
 *   {"op":"close"}
 */
const path = require("node:path");
const readline = require("node:readline");

const nps = (process.env.NODE_PATH || "").split(":").filter(Boolean);
const cands = nps.map(p => path.join(p, "playwright")).concat(["patchright", "playwright"]);
let chromium = null;
for (const c of cands) { try { chromium = require(c).chromium; break; } catch(e){} }
if (!chromium) throw new Error("找不到 playwright/patchright");

const out = (obj) => console.log(JSON.stringify(obj));
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
// 审查修复（H）：硬编码单机用户路径换机器即坏。优先环境变量 → 标准缓存布局
// （按平台）→ 兜底旧路径仅在存在时使用
const _nodePathMod = require("path");
const _fsMod = require("fs");
function _resolve_exe() {
  const env = process.env.PW_EXECUTABLE || process.env.CHROMIUM_EXE;
  if (env) return env;
  const home = process.env.HOME || "";
  const cands = [
    _nodePathMod.join(home, "Library/Caches/ms-playwright/chromium_headless_shell-1208/chrome-headless-shell-mac-arm64/chrome-headless-shell"),
    _nodePathMod.join(home, ".cache/ms-playwright/chromium_headless_shell-1208/chrome-headless-shell-linux64/chrome-headless-shell"),
  ];
  for (const c of cands) { try { if (_fsMod.existsSync(c)) return c; } catch (e) {} }
  // 兜底：探测本机 playwright 缓存（macOS 默认位置，仅在存在时使用）
  try {
    const _pathMod = require("path");
    const cacheRoot = _nodePathMod.join(home, "Library", "Caches", "ms-playwright");
    if (_fsMod.existsSync(cacheRoot)) {
      for (const dir of _fsMod.readdirSync(cacheRoot)) {
        if (!dir.startsWith("chromium_headless_shell")) continue;
        const cand = _pathMod.join(cacheRoot, dir, "chrome-headless-shell-mac-arm64", "chrome-headless-shell");
        if (_fsMod.existsSync(cand)) return cand;
      }
    }
  } catch (e) {}
  return "";  // 未命中：调用方 launch 会报可诊断错误
}
const EXE = _resolve_exe();

function parseArgs(argv) {
  const a = {};
  for (let i = 2; i < argv.length; i++) {
    const k = argv[i];
    if (k.startsWith("--")) {
      // 审查修复（M）：旗标无值/下一项也是旗标时曾吞掉后续键。布尔旗标置 "1"
      const v = argv[i + 1];
      if (i + 1 < argv.length && v !== undefined && !v.startsWith("--")) { a[k.slice(2)] = v; i++; }
      else a[k.slice(2)] = "1";
    }
  }
  return a;
}

// 为元素生成稳定 CSS 选择器（id > class > tag:nth-child）
function cssPath(el) {
  const parts = [];
  let node = el;
  while (node && node.nodeType === 1) {
    const tag = node.tagName.toLowerCase();
    if (tag === "html" || tag === "body") break;
    let seg = tag;
    if (node.id) { seg = tag + "#" + node.id; }
    else if (node.className && typeof node.className === "string") {
      const cls = node.className.split(/\s+/).filter(Boolean).slice(0, 2).join(".");
      if (cls) seg = tag + "." + cls;
    }
    const parent = node.parentElement;
    if (parent) {
      const same = Array.from(parent.children).filter(c => c.tagName.toLowerCase() === tag);
      if (same.length > 1) seg += ":nth-child(" + (same.indexOf(node) + 1) + ")";
    }
    parts.unshift(seg);
    node = parent;
  }
  return parts.join(" > ");
}

let browser = null, ctx = null, page = null;

async function getPage() {
  if (!page || page.isClosed()) {
    // 审查八轮（H）：曾回退 ctx.pages()[0]——CDP 模式下 page 被关后静默改绑
    // 用户正在用的标签页继续操作。captcha_bridge 实证纪律：CDP 模式专用 newPage
    page = await ctx.newPage();
  }
  return page;
}

async function snapshot(maxLinks, maxText) {
  const p = await getPage();
  return p.evaluate(({ maxLinks, maxText }) => {
    function cssPath(el) {
      const parts = [];
      let node = el;
      while (node && node.nodeType === 1) {
        const tag = node.tagName.toLowerCase();
        if (tag === "html" || tag === "body") break;
        let seg = tag;
        if (node.id) { seg = tag + "#" + node.id; }
        else if (node.className && typeof node.className === "string") {
          const cls = node.className.split(/\s+/).filter(Boolean).slice(0, 2).join(".");
          if (cls) seg = tag + "." + cls;
        }
        const parent = node.parentElement;
        if (parent) {
          const same = Array.from(parent.children).filter(c => c.tagName.toLowerCase() === tag);
          if (same.length > 1) seg += ":nth-child(" + (same.indexOf(node) + 1) + ")";
        }
        parts.unshift(seg);
        node = parent;
      }
      return parts.join(" > ");
    }
    const clean = (s) => (s || "").replace(/\s+/g, " ").trim();
    const links = [], buttons = [], inputs = [];
    document.querySelectorAll("a[href]").forEach(a => {
      const t = clean(a.innerText).slice(0, 60);
      const h = a.getAttribute("href") || "";
      if (!t || h.startsWith("javascript:")) return;
      if (links.length >= maxLinks) return;
      links.push({ t, h: h.slice(0, 200), sel: cssPath(a) });
    });
    document.querySelectorAll("button, [role='button'], input[type='submit'], .btn, a.btn").forEach(b => {
      const t = clean(b.innerText || b.value || b.getAttribute("aria-label")).slice(0, 50);
      if (!t) return;
      if (buttons.length >= 20) return;
      buttons.push({ t, sel: cssPath(b) });
    });
    document.querySelectorAll("input:not([type=hidden]), textarea, select").forEach(i => {
      if (inputs.length >= 12) return;
      inputs.push({
        ph: clean(i.getAttribute("placeholder") || i.name || i.getAttribute("aria-label")).slice(0, 50),
        sel: cssPath(i)
      });
    });
    return {
      url: location.href,
      title: document.title,
      text: (document.body ? document.body.innerText : "").replace(/\s+/g, " ").slice(0, maxText),
      links, buttons, inputs
    };
  }, { maxLinks, maxText }).catch(e => ({ error: String(e && e.message || e) }));
}

async function handle(op) {
  const p = await getPage();
  switch (op.op) {
    case "goto": {
      await p.goto(op.url, { timeout: 45000, waitUntil: "domcontentloaded" }).catch(e => {
        const em = String(e && e.message || e);
        if (!/ERR_ABORTED|Timeout|net::/.test(em)) {
          throw new Error("goto: " + em.slice(0,150));
        }
      });
      await sleep(1800);
      return { ok: true, url: p.url() };
    }
    case "snapshot":
      return await snapshot(parseInt(op.max_links || "30"), parseInt(op.max_text || "1500"));
    case "click": {
      const el = await p.$(op.selector);
      if (!el) throw new Error("找不到元素: " + op.selector);
      await el.scrollIntoViewIfNeeded().catch(()=>{});
      await el.click({ timeout: 10000 }).catch(async () => { await p.click(op.selector, { timeout: 8000 }).catch(e => { throw new Error("click: " + (e.message||e).slice(0,120)); }); });
      await sleep(1000);
      return { ok: true, url: p.url() };
    }
    case "type": {
      const el = await p.$(op.selector);
      if (!el) throw new Error("找不到输入框: " + op.selector);
      await el.click({ timeout: 8000 }).catch(()=>{});
      await el.fill(String(op.text || "")).catch(async () => { await p.type(op.selector, String(op.text||""), { delay: 10 }); });
      return { ok: true };
    }
    case "press":
      await p.keyboard.press(op.key || "Enter");
      await sleep(800);
      return { ok: true };
    case "scroll": {
      await p.evaluate((dir) => {
        if (dir === "bottom") window.scrollTo(0, document.body.scrollHeight);
        else if (dir === "up") window.scrollBy(0, -1200);
        else window.scrollBy(0, 1200);
      }, op.dir || "down");
      await sleep(900);
      return { ok: true };
    }
    case "wait":
      await sleep(parseInt(op.ms || "1000"));
      return { ok: true };
    case "html": {
      const h = await p.content();
      // 清掉 script/style 再截断
      const txt = h.replace(/<script[\s\S]*?<\/script>/gi, " ").replace(/<style[\s\S]*?<\/style>/gi, " ")
                   .replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
      return { ok: true, url: p.url(), text: txt.slice(0, parseInt(op.max_chars || "8000")) };
    }
    case "close":
      if (browser) await browser.close().catch(()=>{});
      return { ok: true, closed: true };
    default:
      throw new Error("未知指令: " + op.op);
  }
}

async function main() {
  const args = parseArgs(process.argv);
  const cdp = args.cdp || "";
  if (cdp) {
    try {
      browser = await chromium.connectOverCDP(cdp);
      ctx = browser.contexts()[0] || await browser.newContext();
      // 审查八轮（H）：曾绑定 ctx.pages()[0]——用户当前标签页被 goto/click/type
      // 直接操作（登录态/表单现场被毁）。captcha_bridge 已实证该事故并改为专用
      // newPage，本桥未同步该纪律。CDP 模式一律专用标签页
      page = await ctx.newPage();
      out({ type: "ready", mode: "cdp", url: page.url() });
    } catch (e) {
      out({ type: "error", message: "CDP 连接失败: " + (e.message||e).slice(0,120) });
      process.exit(1);
    }
  } else {
    const headless = String(args.headless || "1") !== "0";
    try {
      browser = await chromium.launch({ headless, executablePath: EXE, args: ["--no-sandbox", "--ignore-certificate-errors"] });
    } catch (e) {
      // 无该 chromium 版本时回退 playwright 默认
      try { browser = await chromium.launch({ headless, args: ["--no-sandbox", "--ignore-certificate-errors"] }); }
      catch (e2) { out({ type: "error", message: "浏览器启动失败: " + (e2.message||e2).slice(0,120) }); process.exit(1); }
    }
    ctx = await browser.newContext({ viewport: { width: 1366, height: 900 }, locale: "zh-CN",
      userAgent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36" });
    page = await ctx.newPage();
    out({ type: "ready", mode: "headless" });
  }

  const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
  // 串行队列：指令必须一个一个执行（并发会导致 close 抢先关闭浏览器）
  let chain = Promise.resolve();
  let queued = 0;  // OCR R131（H）：chain 无上界——Python 侧停止发指令但桥还在
                   // 慢慢执行时队列无限堆积。超 64 条即拒绝新指令（提示忙）
  rl.on("line", (line) => {
    line = line.trim();
    if (!line) return;
    if (queued >= 64) {
      out({ type: "error", message: "指令队列已满（64）——请等待既有指令完成或重启桥" });
      return;
    }
    queued++;
    chain = chain.then(async () => {
      queued--;
      let op = {};
      try { op = JSON.parse(line); } catch (e) { out({ type: "error", message: "指令非 JSON" }); return; }
      try {
        const r = await handle(op);
        if (op.op === "close") {
          // R27 修复：先吐结果再退出——此前直接 exit(0)，Python 侧 close()
          // 等 5 秒超时后只能 SIGKILL（优雅清理路径成死代码）
          out({ type: "result", op: "close", ...r });
          process.exit(0);
        }
        out({ type: "result", op: op.op, ...r });
      } catch (e) {
        out({ type: "error", op: op.op, message: String(e && e.message || e).slice(0, 300) });
      }
    });
  });
  rl.on("close", async () => {
    await chain.catch(() => {});   // EOF 也等队列执行完再退
    try { if (browser) await browser.close(); } catch(e){}
    process.exit(0);
  });
}

main().catch(e => { out({ type: "error", message: String(e && e.message || e) }); process.exit(1); });
