#!/usr/bin/env node
/**
 * gaokao.chsi.com.cn 阳光高考「招生章程」浏览器桥
 *
 * 站点特性（已实测）：
 *  - 全站阿里云 WAF（HTTP/headless 直连一律 412），必须先访问首页种 Cookie（acw_tc 等）
 *  - 招生章程列表是 Vue SPA：学校行 .sch-item，点击 → window.open(/zsgs/zhangcheng/listZszc--schId-<orgId>.dhtml)
 *  - orgId 是站内 id（如北大=1），只能通过点击捕获，不在 DOM 上
 *
 * 本桥：首页种 Cookie → 列表页 → 逐个点击 .sch-item 捕获 schId → 输出 JSONL
 *   {"type":"meta","total":N}
 *   {"type":"school","name":"北京大学","schId":"1","province":"北京","dept":"教育部","tags":["本科","双一流"],...}
 *   {"type":"done"}
 */
let chromium = null;
const _path = require("node:path");
// 优先项目 node_modules 的 patchright（本项目桥的默认实现，能过 WAF 的自动化检测）
const _PROJECT_NM = _path.join(__dirname, "..", "node_modules");
const _cands = [
  _path.join(_PROJECT_NM, "patchright"),
  _path.join(_PROJECT_NM, "playwright"),
  "patchright", "playwright"
];
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
const LIST_URL = ARGS.list_url || "https://gaokao.chsi.com.cn/zsgs/zhangcheng/listVerifedZszc--method-index,lb-1.dhtml";
const MAX = parseInt(ARGS.max || "0", 10) || 200;
const SETTLE = parseInt(ARGS.settle || "800", 10);

const sleep = (ms) => new Promise(r => setTimeout(r, ms));
function emit(obj) { process.stdout.write(JSON.stringify(obj) + "\n"); }
function log(msg) { process.stderr.write("[zszc_bridge] " + msg + "\n"); }

(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: EXE, args: ["--no-sandbox", "--ignore-certificate-errors"] });
  try {
    const ctx = await browser.newContext({
      userAgent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 Safari/605.1.15",
      locale: "zh-CN",
      viewport: { width: 1440, height: 900 }
    });
    const page = await ctx.newPage();
    // 1) 首页种 Cookie（过 WAF）
    log("open home (cookie)");
    await page.goto("https://gaokao.chsi.com.cn/", { timeout: 60000, waitUntil: "domcontentloaded" }).catch(e => log("home warn: " + String(e.message).slice(0, 80)));
    await sleep(2500);
    // 2) 列表页
    log("open list: " + LIST_URL);
    const resp = await page.goto(LIST_URL, { timeout: 60000, waitUntil: "domcontentloaded" }).catch(e => log("list warn: " + String(e.message).slice(0, 80)));
    await sleep(SETTLE);
    for (let i = 0; i < 6; i++) { await page.mouse.wheel(0, 1500).catch(() => {}); await sleep(250); }
    // 3) 收集学校 DOM 信息
    const schools = await page.evaluate(() => {
      const out = [];
      document.querySelectorAll(".sch-item").forEach(item => {
        const nameEl = item.querySelector(".sch-title .name");
        const depEl = item.querySelector(".sch-department");
        const tagEls = item.querySelectorAll(".sch-level-tag");
        const name = nameEl ? nameEl.innerText.replace(/\s+/g, "").trim() : "";
        const depText = depEl ? depEl.innerText.replace(/\s+/g, " ").trim() : "";
        const province = (depText.match(/^(.+?)\|/) || [])[1] ? depText.split("|")[0].replace("北京 ", "北京").trim() : (depText.split("|")[0] || "").trim();
        const dept = (depText.match(/主管部门：(.+)/) || [])[1] || "";
        const tags = [...tagEls].map(t => t.innerText.replace(/\s+/g, "").trim());
        out.push({ name, province, dept, tags, _item: item });
      });
      return out.map(x => ({ name: x.name, province: x.province, dept: x.dept, tags: x.tags }));
    });
    // 4) 逐个点击捕获 schId
    const results = [];
    const items = await page.$$(".sch-item");
    const seen = new Set();
    let seq = 0;
    for (let i = 0; i < items.length && results.length < MAX; i++) {
      let popupUrl = null;
      const pHandler = (p) => { popupUrl = p.url(); p.close().catch(() => {}); };
      ctx.on("page", pHandler);
      try {
        await items[i].dispatchEvent("click", { bubbles: true }).catch(() => {});
      } catch (e) { /* 忽略 */ }
      await sleep(260);
      ctx.off("page", pHandler);
      const m = (popupUrl || "").match(/schId-(\d+)\.dhtml/);
      const schId = m ? m[1] : "";
      if (schId && !seen.has(schId)) {
        seen.add(schId);
        const info = schools[i] || {};
        results.push({ ...info, schId });
        emit({ type: "school", seq: ++seq, name: info.name || "", schId, province: info.province || "", dept: info.dept || "", tags: info.tags || [] });
      }
    }
    emit({ type: "meta", total: results.length });
    emit({ type: "done" });
  } finally {
    await browser.close().catch(() => {});
  }
})().catch(e => { emit({ type: "error", message: String(e && e.stack || e).slice(0, 600) }); emit({ type: "done" }); });
