#!/usr/bin/env node
/**
 * 单页浏览器桥（source.pool=false 后备）：加载一个 URL，返回渲染后 HTML。
 * 支持 js_pre / wait / scroll / actions / stealth / remove_overlays。
 * 输出: {"type":"html","file":"/tmp/xx.html","url":"..."} | {"type":"error","message":"..."}
 */
const fs = require("node:fs");
const path = require("node:path");
const { CHROMIUM_EXE, loadChromium, sleep, runActions, applyStealth, dismissOverlays, parseProxy, waitCloudflare } = require("./browser_common.cjs");
const out = (o) => console.log(JSON.stringify(o));
function arg(n, d) { const i = process.argv.indexOf("--" + n); return i >= 0 ? process.argv[i + 1] : d; }

async function main() {
  const url = arg("url");
  const outFile = arg("out");
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
  try {
    browser = await loadChromium().launch({ headless, executablePath: CHROMIUM_EXE, args: ["--no-sandbox", "--ignore-certificate-errors", "--disable-blink-features=AutomationControlled"] });
    const ctxOpts = storageState && fs.existsSync(storageState) ? { storageState } : {};
    ctxOpts.viewport = { width: 1440, height: 900 };
    if (proxy) ctxOpts.proxy = proxy;
    const context = await browser.newContext(ctxOpts);
    if (stealth) await applyStealth(context);
    const page = await context.newPage();
    await page.goto(url, { timeout: 90000, waitUntil: "domcontentloaded" });
    await waitCloudflare(page, context).catch(() => {});
    if (jsPre) await page.evaluate(jsPre);
    if (removeOverlays) await dismissOverlays(page);
    if (actionsJson) await runActions(page, JSON.parse(actionsJson));
    if (waitSel) await page.waitForSelector(waitSel, { timeout: 30000 }).catch(() => {});
    for (let s = 0; s < scrollCount; s++) {
      if (stopRequested()) { out({ type: "stopped", url }); process.exit(0); }
      await page.evaluate(() => { const _h = document.documentElement ? document.documentElement.scrollHeight : (document.body ? document.body.scrollHeight : 0); window.scrollTo(0, _h); window.dispatchEvent(new Event("scroll")); });
      await sleep(scrollWait);
    }
    if (stopRequested()) { out({ type: "stopped", url }); process.exit(0); }
    const html = await page.evaluate(() => document.documentElement.outerHTML);
    fs.writeFileSync(outFile, html);
    out({ type: "html", file: outFile, url: page.url(), bytes: html.length });
  } catch (e) {
    out({ type: "error", message: String((e && e.message) || e) });
    process.exit(1);
  } finally {
    if (browser) await browser.close();
  }
}
main();
