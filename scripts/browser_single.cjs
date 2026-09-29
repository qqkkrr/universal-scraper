#!/usr/bin/env node
/**
 * 单页浏览器桥（source.pool=false 后备）：加载一个 URL，返回渲染后 HTML。
 * 支持 js_pre / wait / scroll / actions / stealth / remove_overlays。
 * 输出: {"type":"html","file":"/tmp/xx.html","url":"..."} | {"type":"error","message":"..."}
 */
const fs = require("node:fs");
const path = require("node:path");
const { CHROMIUM_EXE, loadChromium, sleep, runActions, applyStealth, dismissOverlays, parseProxy, waitCloudflare, applyResourceBlocking } = require("./browser_common.cjs");
const out = (o) => console.log(JSON.stringify(o));
function arg(n, d) { const i = process.argv.indexOf("--" + n); return i >= 0 ? process.argv[i + 1] : d; }

async function main() {
  const url = arg("url");
  const outFile = arg("out");
  // 审查修复（H）：url/out 缺失曾走到 launch 后才以晦涩错误崩（page.goto expected
  // string / writeFileSync undefined）——入口即校验，错误信息可操作
  if (!url || !/^https?:\/\//.test(url)) {
    out({ type: "error", message: `缺少或非法 --url（http/https）: ${String(url).slice(0, 60)}` });
    process.exit(1);  // 浏览器未启动，直接退出无泄漏
  }
  if (!outFile) {
    out({ type: "error", message: "缺少 --out（HTML 落盘路径）" });
    process.exit(1);
  }
  const headless = arg("headless", "1") !== "0";
  const scrollCount = parseInt(arg("scrollCount", "0"), 10);
  const scrollWait = parseInt(arg("scrollWait", "1500"), 10);
  const waitSel = arg("wait", null);
  const jsPre = arg("js", null);
  const storageState = arg("storageState", null);
  const actionsJson = arg("actions", null);
  const stealth = arg("stealth", "0") === "1";
  const removeOverlays = arg("removeOverlays", "0") === "1";
  const proxy = parseProxy(arg("proxy", null));
  const stopFile = arg("stopFile", null);
  const stopRequested = () => stopFile && fs.existsSync(stopFile);
  let browser = null;
  // 审查修复（P1）：process.exit 同步终止会跳过 finally 的 browser.close() →
  // 无头 Chrome 孤儿（R8 战训同款）。改 flag+return，finally 里清理后再退
  let exitAfter = null;
  try {
    browser = await loadChromium().launch({ headless, executablePath: CHROMIUM_EXE, args: ["--no-sandbox", "--ignore-certificate-errors", "--disable-blink-features=AutomationControlled"] });
    const ctxOpts = storageState && fs.existsSync(storageState) ? { storageState } : {};
    ctxOpts.viewport = { width: 1440, height: 900 };
    if (proxy) ctxOpts.proxy = proxy;
    const context = await browser.newContext(ctxOpts);
    if (stealth) await applyStealth(context);
    await applyResourceBlocking(context);  // Crawlee 对标：font/media 默认阻断（自建 context）
    const page = await context.newPage();
    await page.goto(url, { timeout: 90000, waitUntil: "domcontentloaded" });
    await waitCloudflare(page, context).catch(() => {});
    if (jsPre) await page.evaluate(jsPre);
    if (removeOverlays) await dismissOverlays(page);
    if (actionsJson) await runActions(page, JSON.parse(actionsJson));
    if (waitSel) await page.waitForSelector(waitSel, { timeout: 30000 }).catch(() => {});
    for (let s = 0; s < scrollCount; s++) {
      if (stopRequested()) { out({ type: "stopped", url }); exitAfter = 0; return; }
      await page.evaluate(() => { const _h = document.documentElement ? document.documentElement.scrollHeight : (document.body ? document.body.scrollHeight : 0); window.scrollTo(0, _h); window.dispatchEvent(new Event("scroll")); });
      await sleep(scrollWait);
    }
    if (stopRequested()) { out({ type: "stopped", url }); exitAfter = 0; return; }
    const html = await page.evaluate(() => document.documentElement.outerHTML);
    fs.writeFileSync(outFile, html);
    // OCR R131（L）：html.length 是 UTF-16 码元数——中文页面下虚高。用 Buffer 报字节数
    const bytes = Buffer.byteLength(html, "utf-8");
    out({ type: "html", file: outFile, url: page.url(), bytes });
    // 实战反馈七（拼多多）：SPA 壳检测——浏览器渲染后仍无实质内容的站，
    // 数据靠 JS 接口下发，fetch/markdown 都是垃圾。提示改走 capture 路线。
    const bodyText = await page.evaluate(() => (document.body && document.body.innerText) || "");
    if (bodyText.trim().length < 200 && html.length > 3000) {
      out({ type: "spa_shell",
            message: "⚠️ 浏览器渲染后可见文本极少（" + bodyText.trim().length + "B），"
                   + "疑似 SPA 壳——数据靠 JS 接口下发。建议：capture-daemon 在场捕获"
                   + " 或 jsrecon 找接口（fetch --capture）" });
    }
  } catch (e) {
    out({ type: "error", message: String((e && e.message) || e) });
    exitAfter = 1;
    return;
  } finally {
    // 审查修复（P1）：close 拒绝曾变 unhandledRejection 翻转退出码（html 已发出）
    if (browser) { try { await browser.close(); } catch (e) {} }
    if (exitAfter !== null) process.exit(exitAfter);
  }
}
main().catch((e) => { try { console.error(String((e && e.message) || e)); } catch (_) {} process.exit(1); });
