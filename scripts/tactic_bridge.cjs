#!/usr/bin/env node
/**
 * 通用战术桥（一键自动精配用）
 *
 * 模式（--mode）：
 *   detect     渲染目标页并返回诊断（Vue/链接/PDF/大图/表格/登录墙），供 AI 决策战术
 *   click_ids  点击 item_css 捕获跳转 URL 中的 id（如 schId-123），输出条目
 *   links      提取 href 匹配 regex 的链接（标题+href）
 *   pdfs       提取 href/src 匹配的 PDF/附件 URL
 *   images     提取 src 匹配的大图 URL（排除 logo/icon）
 *
 * 通用流程：--seed 先访问种子页种 Cookie（过 WAF）→ --target 访问目标页 → 执行模式
 * 输出 JSONL：{"type":"meta"|"detect"|"item"|"link"|"pdf"|"image"|"error"|"done", ...}
 */
let chromium = null;
const _path = require("node:path");
const _PROJECT_NM = _path.join(__dirname, "..", "node_modules");
const _cands = [_path.join(_PROJECT_NM, "patchright"), _path.join(_PROJECT_NM, "playwright"), "patchright", "playwright"];
for (const _c of _cands) {
  try { chromium = require(_c).chromium; break; } catch (e) { /* 下一个 */ }
}
if (!chromium) throw new Error("找不到 playwright/patchright");

const os = require("node:os");
const HOME = process.env.HOME || os.homedir() || "/tmp";
const EXE = process.env.PW_EXECUTABLE ||
  `${HOME}/Library/Caches/ms-playwright/chromium_headless_shell-1208/chrome-headless-shell-mac-arm64/chrome-headless-shell`;

const ARGS = {};
const _argv = process.argv.slice(2);
for (let i = 0; i < _argv.length; i++) {
  const a = _argv[i];
  const m = a.match(/^--([^=]+)=(.*)$/);
  if (m) ARGS[m[1]] = m[2];
  else if (a.startsWith("--")) {
    const key = a.slice(2);
    const nxt = _argv[i + 1];
    if (nxt !== undefined && !nxt.startsWith("--")) { ARGS[key] = nxt; i++; }
    else ARGS[key] = "1";
  }
}
const MODE = ARGS.mode || "detect";
const SEED = ARGS.seed || "";
const TARGET = ARGS.target || "";
const ITEM_CSS = ARGS.item_css || ".sch-item";
const HREF_REGEX = ARGS.href_regex || "";
const MAX = parseInt(ARGS.max || "0", 10) || 500;
const SETTLE = parseInt(ARGS.settle || "800", 10);

const sleep = (ms) => new Promise(r => setTimeout(r, ms));
function emit(obj) { process.stdout.write(JSON.stringify(obj) + "\n"); }
function log(msg) { process.stderr.write("[tactic_bridge] " + msg + "\n"); }

async function goto(page, url) {
  const r = await page.goto(url, { timeout: 60000, waitUntil: "domcontentloaded" })
    .catch(e => { log("goto warn: " + String(e.message).slice(0, 100)); return null; });
  await sleep(SETTLE);
  for (let i = 0; i < 6; i++) { await page.mouse.wheel(0, 1500).catch(() => {}); await sleep(250); }
  return r;
}

async function detect(page) {
  return page.evaluate(() => {
    const html = document.documentElement ? document.documentElement.outerHTML : "";
    const text = (document.body ? document.body.innerText.replace(/\s+/g, " ") : "").trim();
    const links = [...document.querySelectorAll("a")].map(a => a.href || "").filter(h => h.startsWith("http"));
    const pdfs = [...document.querySelectorAll("a[href*='.pdf' i], a[href*='.doc' i], a[href*='.xls' i], iframe[src*='.pdf'], iframe[fileurl]")]
      .map(a => a.href || a.getAttribute("src") || a.getAttribute("fileurl") || "").filter(Boolean);
    const imgs = [...document.querySelectorAll("img")].map(i => i.src || i.getAttribute("data-src") || "")
      .filter(s => s && !/logo|icon|qrcode|wechat|avatar/i.test(s) && !s.startsWith("data:"));
    const tables = [...document.querySelectorAll("table")].map(t => (t.innerText || "").replace(/\s+/g, " ").slice(0, 200));
    const hasVue = !!document.querySelector("#app") || html.includes("__vue__") || html.includes("new Vue");
    const loginText = /登录|注册|验证码|滑块|扫码|安全验证|antibot/i.test(text.slice(0, 800));
    const clicks = [...document.querySelectorAll("[onclick]")].map(e => (e.getAttribute("onclick") || "")).filter(o => /window\.open|location\.href/.test(o)).slice(0, 5);
    return {
      title: document.title || "", textLen: text.length, textHead: text.slice(0, 600),
      linkCount: links.length, pdfLinks: pdfs.slice(0, 20), bigImages: imgs.slice(0, 20),
      tables: tables.slice(0, 3), hasVue, loginText, clicks,
      status: "ok"
    };
  });
}

(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: EXE, args: ["--no-sandbox", "--ignore-certificate-errors"] });
  try {
    const ctx = await browser.newContext({
      userAgent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 Safari/605.1.15",
      locale: "zh-CN", viewport: { width: 1440, height: 900 }
    });
    const page = await ctx.newPage();
    if (SEED) {
      log("seed: " + SEED);
      await page.goto(SEED, { timeout: 60000, waitUntil: "domcontentloaded" }).catch(() => {});
      await sleep(2200);
    }
    if (!TARGET) { emit({ type: "error", message: "缺少 --target" }); emit({ type: "done" }); return; }
    log("target: " + TARGET);
    const resp = await goto(page, TARGET);

    if (MODE === "detect") {
      const d = await detect(page);
      d.httpStatus = resp ? resp.status() : null;
      emit({ type: "detect", data: d });
    } else if (MODE === "links") {
      const re = HREF_REGEX ? new RegExp(HREF_REGEX) : null;
      const items = await page.evaluate((reStr) => {
        const re = reStr ? new RegExp(reStr) : null;
        const out = [];
        const seen = new Set();
        document.querySelectorAll("a").forEach(a => {
          const href = a.href || "";
          const t = (a.innerText || "").replace(/\s+/g, " ").trim();
          if (!href || !t || !href.startsWith("http")) return;
          if (re && !re.test(href)) return;
          if (seen.has(href)) return;
          seen.add(href);
          out.push({ title: t.slice(0, 120), href });
        });
        return out;
      }, HREF_REGEX);
      for (const it of items.slice(0, MAX)) emit({ type: "link", ...it });
    } else if (MODE === "pdfs" || MODE === "images") {
      const d = await detect(page);
      const list = MODE === "pdfs" ? d.pdfLinks : d.bigImages;
      for (const u of list.slice(0, MAX)) emit({ type: MODE === "pdfs" ? "pdf" : "image", url: u });
    } else if (MODE === "click_ids") {
      // 点击每个 item_css 元素，捕获 window.open 弹出的 URL（含 id）
      const items = await page.$$(ITEM_CSS);
      const results = [];
      const seen = new Set();
      let seq = 0;
      for (let i = 0; i < items.length && results.length < MAX; i++) {
        // 只认“本次点击后新增”的页面，避免跨轮错位
        const before = ctx.pages().map(p => p);
        try { await items[i].dispatchEvent("click", { bubbles: true }).catch(() => {}); } catch (e) { /* 忽略 */ }
        await sleep(320);
        let popupUrl = null;
        for (const p of ctx.pages()) {
          if (!before.includes(p)) { popupUrl = p.url(); p.close().catch(() => {}); break; }
        }
        if (!popupUrl) await sleep(320);
        if (popupUrl && !seen.has(popupUrl)) {
          seen.add(popupUrl);
          const txt = await items[i].innerText().catch(() => "");
          const text = (txt || "").replace(/\s+/g, " ").trim();
          const idMatch = popupUrl.match(/(?:schId|infoId|orgId|itemId|pid|fid|id)[-=]([0-9A-Za-z_-]{1,40})/i)
            || popupUrl.match(/[?&/](?:id|Id)[=]([0-9A-Za-z_-]{1,40})/);
          emit({ type: "item", seq: ++seq, text: text.slice(0, 200), url: popupUrl, id: idMatch ? idMatch[1] : "" });
          results.push({ url: popupUrl });
        }
      }
    }
    emit({ type: "done" });
  } finally {
    await browser.close().catch(() => {});
  }
})().catch(e => { emit({ type: "error", message: String(e && e.stack || e).slice(0, 600) }); emit({ type: "done" }); });
