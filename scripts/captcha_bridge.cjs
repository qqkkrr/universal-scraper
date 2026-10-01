#!/usr/bin/env node
/**
 * 验证码人机协同桥（gsxt 战训沉淀，2026-09）：CDP 附加调试 Chrome（真指纹/真登录态/
 * 用户可亲眼看窗口并人工点码），长驻进程 + 工作目录文件协议——agent 不用持有管道即可驱动。
 *
 * 用法:
 *   node scripts/captcha_bridge.cjs --dir <workdir> [--cdp 9222] [--own 1] [--url <初始URL>]
 *   --cdp 9222      附加到调试 Chrome（首选：人工在环 = 用户直接在窗口里点码）
 *   --own 1         CDP 不可用时改启动自带 Chromium（headful 便于人工点码）
 *
 * 文件协议（workdir 内）:
 *   status.json       心跳（每 2s）: {"alive":true,"ts":...,"url":...,"pid":...,"mode":"cdp|own"}
 *   cmd.json          指令: {"id":N,"op":"goto|html|shot|click_xy|click_css|fill|press|eval|wait|cookies|close",...}
 *   last_result.json  应答: {"id":N,"ok":true,"data":{...}} | {"id":N,"ok":false,"error":"..."}
 *   stop              触摸此文件 → 优雅退出
 *
 * op 明细:
 *   goto      {url, waitMs?}                       导航（domcontentloaded）
 *   html      {}                                   渲染后 HTML → data.html
 *   shot      {out?}                               截图 → data.file（默认 shot.png）
 *   click_xy  {points:[[x,y],...], delayMs?}       按坐标依次点击（验证码解题输出）
 *   click_css {selector, index?}                   按选择器点击
 *   fill      {selector, text}                     填输入框
 *   press     {key}                                按键（如 Enter）
 *   eval      {js}                                 页面内执行 → data.result
 *   wait      {selector, timeoutMs?}               等选择器出现（用于"验证码已过"判定）
 *   cookies   {}                                   当前页可访问的全部 cookie → data.cookies
 *   close     {}                                   关闭并退出
 *
 * 设计要点（战训）：人工在环不是"暂停等输入"，而是——桥只负责导航/截图/执行点击，
 * 验证码本身由用户在真实窗口里点（或 OCR 算出坐标后 click_xy 自动点）；
 * agent 用 wait 轮询"验证码已过"的页面特征即可。
 */
const fs = require("node:fs");
const path = require("node:path");

function arg(n, d) { const i = process.argv.indexOf("--" + n); return i >= 0 ? process.argv[i + 1] : d; }
const DIR = path.resolve(arg("dir", process.cwd()));
// 先建目录再进依赖守卫：守卫的墓碑写盘必须能落地（否则 30s 盲等）
fs.mkdirSync(DIR, { recursive: true });
const P = (f) => path.join(DIR, f);
const writeJson = (f, o) => { try {
  // R100 修复（P2）：cookies op 会把登录 Cookie 数组写进 last_result.json——0600
  // 审查修复（H）：非原子写曾留半截 JSON（读取方轮询到中间态）——tmp+rename
  const _tmp = P(f) + ".tmp";
  fs.writeFileSync(_tmp, JSON.stringify(o), { mode: 0o600 });
  fs.renameSync(_tmp, P(f));
  fs.chmodSync(P(f), 0o600);  // R18b 同款：治愈遗留 0644
} catch {} };

// browser_common 依赖 node_modules/playwright：缺失时必须立刻写墓碑退出
// （否则 30s 心跳盲等，用户只看到"无响应"三字，白查半小时）
let CHROMIUM_EXE, loadChromium, sleep;
try {
  const common = require("./browser_common.cjs");
  CHROMIUM_EXE = common.CHROMIUM_EXE; loadChromium = common.loadChromium; sleep = common.sleep;
} catch (e) {
  writeJson("status.json", { alive: false, error: `浏览器依赖缺失（${String(e && e.message || e).slice(0, 160)}）——`
      + `在插件根目录安装: npm install playwright && npx playwright install chromium；或使用 Full 版` });
  process.exit(1);
}

// 启动参数优先读 workdir/boot.json（文件协议的一部分：Python 侧进程参数表
// 保持纯字面量，扩展项全部走文件）—— {"cdp":9222,"own":false,"url":"..."}
const boot = (() => { try { return JSON.parse(fs.readFileSync(P("boot.json"), "utf-8")); } catch { return {}; } })();
const CDP_PORT = parseInt(arg("cdp", String(boot.cdp ?? 9222)), 10);
const OWN = (arg("own", boot.own ? "1" : "0")) === "1";
const INIT_URL = arg("url", boot.url || null);

const readJson = (f, d) => { try { return JSON.parse(fs.readFileSync(P(f), "utf-8")); } catch { return d; } };

let browser = null, page = null, mode = "", shouldExit = false;

function heartbeat() {
  // 独立定时器心跳（审查 P0 修复）：长操作（goto 90s/wait 30s）会阻塞主循环，
  // 顺序写心跳会让客户端把"活着但忙"误判为"已停止"。Node 定时器在 await
  // 间隙照样触发，page.url() 是同步读取安全。
  try {
    writeJson("status.json", { alive: true, ts: Date.now(), mode,
                               url: page ? page.url() : "", pid: process.pid });
  } catch {}
}

async function attach() {
  const chromium = loadChromium();
  // 首选 CDP 附加调试 Chrome：真指纹 + 真登录态 + 用户可见窗口（人工在环的正路）。
  // 必须新建【专用标签页】——复用 pages()[0] 会污染用户正在用的页面，
  // 还会把遗留 tab 的旧 DOM 当成新页面（E2E 实证事故）
  try {
    browser = await chromium.connectOverCDP(`http://127.0.0.1:${CDP_PORT}`);
    mode = "cdp";
    const ctx = browser.contexts()[0];
    page = ctx ? await ctx.newPage() : await (await browser.newContext()).newPage();
    return;
  } catch (e) {
    if (!OWN) throw new Error(`CDP 附加失败（${String(e).slice(0, 120)}）——请先运行 open-debug-chrome.sh，或加 --own 1 改用自带浏览器`);
  }
  // 自带 headful Chromium：窗口可见，人工点码仍可行
  browser = await chromium.launch({
    headless: false,
    executablePath: CHROMIUM_EXE,
    args: ["--no-sandbox", "--disable-blink-features=AutomationControlled"],
  });
  mode = "own";
  page = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
}

async function runOp(c) {
  switch (c.op) {
    case "goto":
      await page.goto(c.url, { timeout: 90000, waitUntil: "domcontentloaded" });
      if (c.waitMs) await sleep(c.waitMs);
      return { url: page.url() };
    case "html":
      return { html: await page.evaluate(() => document.documentElement.outerHTML) };
    case "shot": {
      const f = P(c.out || "shot.png");
      if (c.selector) {
        // 元素截图（验证码主图）：全页截图会让 OCR 把题面文字也框进去，
        // 系统性点错——审查 P2 修复
        const loc = page.locator(c.selector);
        await loc.first().screenshot({ path: f, timeout: 15000 });
      } else {
        await page.screenshot({ path: f, fullPage: c.full === 1 });
      }
      return { file: f };
    }
    case "click_xy": {
      const pts = c.points || [];
      const delay = parseInt(c.delayMs || "600", 10);
      for (const [x, y] of pts) {
        await page.mouse.click(Number(x), Number(y));
        if (delay) await sleep(delay);
      }
      return { clicked: pts.length };
    }
    case "click_css": {
      const loc = page.locator(c.selector);
      const idx = parseInt(c.index || "0", 10);
      await loc.nth(idx).click({ timeout: 15000 });
      return { clicked: c.selector };
    }
    case "fill":
      await page.fill(c.selector, String(c.text ?? ""), { timeout: 15000 });
      return { filled: c.selector };
    case "press":
      await page.keyboard.press(c.key || "Enter");
      return { pressed: c.key || "Enter" };
    case "eval":
      return { result: await page.evaluate(c.js) };
    case "wait": {
      try {
        await page.waitForSelector(c.selector, { timeout: parseInt(c.timeoutMs || "30000", 10) });
        return { appeared: true };
      } catch {
        return { appeared: false };
      }
    }
    case "cookies":
      return { cookies: await page.context().cookies() };
    case "close":
      // 不在 runOp 内 process.exit——那会跳过主循环的退出清理（审查 P3 修复）；
      // 置旗标由主循环统一走清理路径
      shouldExit = true;
      return { closed: true };
    default:
      throw new Error(`未知 op: ${c.op}`);
  }
}

async function main() {
  fs.mkdirSync(DIR, { recursive: true });
  await attach();
  // attach 成功即上心跳：INIT_URL 导航可能很慢，--start 必须先如实报告"浏览器已起"
  heartbeat();
  const hbTimer = setInterval(heartbeat, 2000);
  if (INIT_URL) await page.goto(INIT_URL, { timeout: 90000, waitUntil: "domcontentloaded" }).catch(() => {});
  let lastId = 0;
  // 审查八轮（M）：桥被杀/崩溃后重启曾 lastId 归零——workdir 残留的 cmd.json
  // （id > 0）被当新指令重放（提交类操作双击/重复导航），且覆盖 last_result。
  // 启动即消费掉残留指令（记 id + 删文件），客户端重启会话自会写新 id
  {
    const _pre = readJson("cmd.json", null);
    if (_pre && _pre.op) {
      lastId = Number(_pre.id) || 0;
      try { fs.unlinkSync(P("cmd.json")); } catch (e) {}
    }
  }
  const t0 = Date.now();
  while (!shouldExit) {
    if (fs.existsSync(P("stop"))) break;
    heartbeat();
    // 审查修复（H）：page 崩溃/关闭时 page.url() 抛错曾炸出主循环——桥静默死亡
    let _url = "";
    try { _url = page.url(); } catch (e) {}
    writeJson("status.json", { alive: true, ts: Date.now(), mode, url: _url,
                               pid: process.pid, uptime_sec: Math.round((Date.now() - t0) / 1000) });
    const c = readJson("cmd.json", null);
    if (c && c.op && Number(c.id) > lastId) {
      lastId = Number(c.id);
      try {
        const data = await runOp(c);
        writeJson("last_result.json", { id: lastId, ok: true, data });
      } catch (e) {
        writeJson("last_result.json", { id: lastId, ok: false, error: String((e && e.message) || e).slice(0, 300) });
      }
    }
    await sleep(400);
  }
  clearInterval(hbTimer);
  // 退出清理：状态墓碑 + stop 文件（客户端 is_running 立即可判）。
  // cdp 模式只关自己开的专用 tab，绝不 close 用户的整个 Chrome
  try {
    if (mode === "cdp") await page.close();
    else await browser.close();
  } catch {}
  fs.rmSync(P("status.json"), { force: true });
  fs.rmSync(P("stop"), { force: true });
}

main().then(() => process.exit(0)).catch((e) => {
  writeJson("status.json", { alive: false, error: String((e && e.message) || e).slice(0, 300) });
  console.error(String((e && e.message) || e));
  process.exit(1);
});
