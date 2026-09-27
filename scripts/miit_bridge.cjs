#!/usr/bin/env node
/**
 * miit.gov.cn 工信部「APP侵害用户权益专项整治行动-通知公告」浏览器桥
 *
 * 列表页是 JS 渲染壳（HTTP 直抓只有 5KB 导航），必须用真实浏览器渲染；
 * 详情页正文只有「详见附件」，App 名单在详情页内嵌 PDF 阅读器 iframe 的
 * fileurl（PDF 直链）里。本桥：
 *   1) 渲染列表页，滚动加载，提取 标题/链接/日期
 *   2) 逐个打开详情页，提取 iframe fileurl（PDF 直链）
 * 结果以 JSONL 流式输出到 stdout：
 *   {"type":"meta","total":N}
 *   {"type":"article", ...}
 *   {"type":"error","message":"..."}
 *   {"type":"done"}
 */
let chromium = null;
const _path = require("node:path");
const _cands = (process.env.NODE_PATH || "").split(":").filter(Boolean)
  .map((p) => _path.join(p, "playwright")).concat(["patchright", "playwright"]);
for (const _c of _cands) {
  try { chromium = require(_c).chromium; break; } catch (e) { /* 下一个 */ }
}
if (!chromium) throw new Error("找不到 playwright/patchright");

const fs = require("node:fs");
const os = require("node:os");
const HOME = process.env.HOME || os.homedir() || "/tmp";
const EXE = process.env.PW_EXECUTABLE || (() => {
  // 深测二轮：playwright 缓存版本升级后硬编码 1208 失效——扫任意版本号取最新
  try {
    const root = `${HOME}/Library/Caches/ms-playwright`;
    const names = fs.readdirSync(root).filter((d) => d.startsWith("chromium_headless_shell-")).sort().reverse();
    for (const d of names) {
      const p = `${root}/${d}/chrome-headless-shell-mac-arm64/chrome-headless-shell`;
      if (fs.existsSync(p)) return p;
    }
  } catch (e) {}
  return `${HOME}/Library/Caches/ms-playwright/chromium_headless_shell-1208/chrome-headless-shell-mac-arm64/chrome-headless-shell`;
})();

const ARGS = {};
const _argv = process.argv.slice(2);
for (let i = 0; i < _argv.length; i++) {
  const a = _argv[i];
  const m = a.match(/^--([^=]+)=(.*)$/);
  if (m) {
    ARGS[m[1]] = m[2];
  } else if (a.startsWith("--")) {
    const key = a.slice(2);
    const nxt = _argv[i + 1];
    if (nxt !== undefined && !nxt.startsWith("--")) { ARGS[key] = nxt; i++; }
    else ARGS[key] = "1";
  }
}
const LIST_URL = ARGS.list_url || "https://www.miit.gov.cn/jgsj/xgj/APPqhyhqyzxzzxd/tzgg/";
const MAX_ARTICLES = parseInt(ARGS.max_articles || "0", 10) || 100;
const SETTLE = parseInt(ARGS.settle || "1200", 10);
const DEADLINE = parseInt(ARGS.deadlineMs || "360000", 10);

const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const started = Date.now();
// R98 修复（P2）：硬退出兜底（tactic/zszc 同款）——unref 允许正常完成时自然
// 退出；仅当 await 卡死且有活跃 handle（浏览器连接）时定时器才触发强制退出
const _hardExit = setTimeout(() => {
  log("deadline+5s 强制退出（有 await 卡死）");
  process.exit(2);
}, DEADLINE + 5000);
_hardExit.unref();

function emit(obj) { process.stdout.write(JSON.stringify(obj) + "\n"); }
function log(msg) { process.stderr.write("[miit_bridge] " + msg + "\n"); }

async function scrollToBottom(page) {
  // 审查修复（M）：固定 12 轮曾无收敛检测——短页白滚，长页 12 轮不够。
  // 改为"高度不再变化即停"，上限 20 轮兜底
  let lastH = 0;
  for (let i = 0; i < 20; i++) {
    await page.mouse.wheel(0, 1600).catch(() => {});
    await sleep(300);
    const h = await page.evaluate(() => document.documentElement ? document.documentElement.scrollHeight : 0).catch(() => 0);
    if (h > 0 && h === lastH) break;
    lastH = h;
  }
}

async function extractList(page) {
  const items = await page.evaluate(() => {
    const seen = new Set();
    const out = [];
    // 常见列表容器：ul.li_list / .news_list / .list 等，逐一兼容
    const containers = [
      ...document.querySelectorAll("ul.li_list li, ul.news_list li, .news_list li, .list li, .zwgk_list li, .border_list li")
    ];
    for (const li of containers) {
      const a = li.querySelector("a[href*='/art/']") || li.querySelector("a");
      if (!a) continue;
      const href = a.getAttribute("href") || "";
      if (!href.includes("/art/") || seen.has(href)) continue;
      seen.add(href);
      const title = (a.innerText || a.getAttribute("title") || "").replace(/\s+/g, " ").trim();
      const dateEl = li.querySelector("span.date, .time, em, span:last-child");
      const date = (dateEl ? dateEl.innerText : "").replace(/\s+/g, "").trim();
      out.push({ title, href, date });
    }
    return out;
  });
  return items;
}

async function extractArticle(page) {
  const info = await page.evaluate(() => {
    const ifr = document.querySelector("iframe[fileurl], ul.fileArry iframe, iframe[src*='pdfjs']");
    let pdfUrl = "";
    let viewerUrl = "";
    if (ifr) {
      pdfUrl = ifr.getAttribute("fileurl") || "";
      viewerUrl = ifr.getAttribute("src") || "";
      const m = viewerUrl.match(/[?&]file=([^&]+)/);
      if (!pdfUrl && m) pdfUrl = decodeURIComponent(m[1]);
    }
    const title = document.title || "";
    const body = document.body ? document.body.innerText.replace(/\s+/g, " ") : "";
    const date = (body.match(/(20\d{2})年(\d{1,2})月(\d{1,2})日/) || [])[0] || "";
    return { pdfUrl, viewerUrl, title, date };
  });
  return info;
}

(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: EXE, args: ["--no-sandbox", "--ignore-certificate-errors"] });
  try {
    const ctx = await browser.newContext({
      userAgent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 Safari/605.1.15",
      locale: "zh-CN",
      viewport: { width: 1440, height: 900 }
    });
    const page = await ctx.newPage();
    log("open: " + LIST_URL);
    await page.goto(LIST_URL, { timeout: 60000, waitUntil: "domcontentloaded" }).catch(e => log("warn: " + String(e.message).slice(0, 120)));
    await sleep(SETTLE);
    await scrollToBottom(page);
    // 单篇详情模式：页面直接带 PDF 阅读器
    const isArticle = await page.evaluate(() => !!document.querySelector("iframe[fileurl], iframe[src*='pdfjs']"));
    if (isArticle) {
      const det = await extractArticle(page);
      const title = (await page.title()) || "";
      emit({ type: "meta", total: 1 });
      emit({ type: "article", index: 1, title, href: LIST_URL, date: det.date, pdfUrl: det.pdfUrl, viewerUrl: det.viewerUrl });
      emit({ type: "done" });
      return;
    }
    let items = await extractList(page);
    if (items.length === 0) {
      // 兜底：整页找 art 链接
      items = await page.evaluate(() => {
        const seen = new Set(); const out = [];
        document.querySelectorAll("a[href*='/art/']").forEach(a => {
          const href = a.getAttribute("href");
          const t = (a.innerText || "").replace(/\s+/g, " ").trim();
          if (t && !seen.has(href)) { seen.add(href); out.push({ title: t, href, date: "" }); }
        });
        return out;
      });
    }
    if (items.length === 0) {
      emit({ type: "error", message: "列表页 0 条（可能被 WAF/验证码拦截或栏目结构变化）" });
      emit({ type: "done" });
      return;
    }
    emit({ type: "meta", total: items.length });
    const picked = items.slice(0, MAX_ARTICLES);
    for (let i = 0; i < picked.length; i++) {
      if (Date.now() - started > DEADLINE) { log("deadline hit"); break; }
      const it = picked[i];
      const url = it.href.startsWith("http") ? it.href : "https://www.miit.gov.cn" + it.href;
      try {
        await page.goto(url, { timeout: 60000, waitUntil: "domcontentloaded" }).catch(e => log("art warn: " + String(e.message).slice(0, 120)));
        await sleep(Math.max(600, SETTLE / 3));
        await scrollToBottom(page);
        const det = await extractArticle(page);
        emit({ type: "article", index: i + 1, title: it.title || det.title, href: url, date: it.date || det.date, pdfUrl: det.pdfUrl, viewerUrl: det.viewerUrl });
      } catch (e) {
        log("article error: " + String(e.message).slice(0, 160));
        emit({ type: "article", index: i + 1, title: it.title, href: url, date: it.date, pdfUrl: "", viewerUrl: "", error: String(e.message).slice(0, 200) });
      }
      if (i < picked.length - 1) await sleep(400);
    }
    emit({ type: "done" });
  } finally {
    await browser.close().catch(() => {});
  }
})().catch(e => { emit({ type: "error", message: String(e && e.stack || e).slice(0, 500) }); emit({ type: "done" }); });
