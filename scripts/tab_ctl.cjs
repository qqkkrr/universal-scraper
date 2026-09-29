// Tab 观察工具（通用 CDP 控制器）：connectOverCDP 控制任意已打开的调试 Chrome
// 实战反馈七收编（拼多多/知乎两役手搓两次后决定一等公民化）
// 补充命令：tap <selector>（模拟点击）、text <selector>（取文本）
// 用法: node tab_ctl.cjs <cmd> [args]
//   goto <url> [waitMs]   导航当前页并等待
//   eval <jsFile|- "js">  在当前页执行 JS 并打印 JSON 结果
//   shot <file>           截图
//   cookies [domain]      导出 cookie（domain 过滤）
//   tabs                  列出页面
//   info                  当前页 URL/标题
const fs = require('fs');
const { chromium } = require('patchright');

const CDP = 'http://127.0.0.1:9222';

async function pickPage(ctx) {
  const pages = ctx.pages();
  if (pages.length) return pages[pages.length - 1];
  return await ctx.newPage();
}

async function main() {
  const [cmd, ...rest] = process.argv.slice(2);
  if (!cmd) {
    console.error('usage: tab_ctl.cjs goto|eval|shot|cookies|tabs|info ...');
    process.exit(2);
  }
  const browser = await chromium.connectOverCDP(CDP);
  const ctx = browser.contexts()[0];
  if (!ctx) {
    console.error('NO_CONTEXT');
    process.exit(1);
  }
  const page = await pickPage(ctx);

  if (cmd === 'tabs') {
    console.log(JSON.stringify(ctx.pages().map((p) => ({ url: p.url() })), null, 1));
  } else if (cmd === 'info') {
    console.log(JSON.stringify({ url: page.url(), title: await page.title() }));
  } else if (cmd === 'goto') {
    const url = rest[0];
    const waitMs = Number(rest[1] || 0);
    // 仅放行 http/https 及已知域，拒绝 localhost/私网语义上的绕行目标
    const u = new URL(url);
    if (!/^https?:$/.test(u.protocol)) throw new Error('bad protocol');
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 45000 });
    if (waitMs) await page.waitForTimeout(waitMs);
    console.log(JSON.stringify({ url: page.url(), title: await page.title() }));
  } else if (cmd === 'shot') {
    await page.screenshot({ path: rest[0], fullPage: false });
    console.log('saved ' + rest[0]);
  } else if (cmd === 'cookies') {
    const domain = rest[0] || '';
    const all = await ctx.cookies();
    const out = domain ? all.filter((c) => c.domain.includes(domain)) : all;
    console.log(JSON.stringify(out));
  } else if (cmd === 'eval') {
    const arg = rest[0];
    let code = arg;
    if (arg === '-') code = fs.readFileSync(0, 'utf8');
    else if (fs.existsSync(arg)) code = fs.readFileSync(arg, 'utf8');
    const result = await page.evaluate(code);
    console.log(JSON.stringify(result));
  } else {
    console.error('unknown cmd ' + cmd);
    process.exit(2);
  }
  await browser.close(); // 仅断开 CDP 连接，不关浏览器
}

main().catch((e) => {
  console.error('CTL_FAIL ' + (e && e.message ? e.message : String(e)));
  process.exit(1);
});
