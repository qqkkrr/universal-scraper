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
const EXE = process.env.PW_EXECUTABLE || "/Users/kairanqin/Library/Caches/ms-playwright/chromium_headless_shell-1208/chrome-headless-shell-mac-arm64/chrome-headless-shell";

function parseArgs(argv) {
  const a = {};
  for (let i = 2; i < argv.length; i++) {
    const k = argv[i];
    if (k.startsWith("--")) a[k.slice(2)] = argv[i + 1];
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
    page = ctx.pages()[0] || await ctx.newPage();
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
      page = ctx.pages()[0] || await ctx.newPage();
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
  rl.on("line", (line) => {
    line = line.trim();
    if (!line) return;
    chain = chain.then(async () => {
      let op = {};
      try { op = JSON.parse(line); } catch (e) { out({ type: "error", message: "指令非 JSON" }); return; }
      try {
        const r = await handle(op);
        if (op.op === "close") { process.exit(0); }
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
