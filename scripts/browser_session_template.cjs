#!/usr/bin/env node
// 浏览器持久会话采集模板（实战反馈六#2 收编，源自小红书/知乎两役验证的共性层）。
// HTTP 层有 safe_http_template.py，这一层（签名站在场捕获+滚动加载）此前每个任务重写——
// 六个已知坑已固化在内：
//   ① 监听器必须在首次导航【前】挂（挂晚错过链头请求）
//   ② 滚动以"捕获增量收敛"为准，不以滚动次数为准（旧 cursor 消耗型站点滚动无效）
//   ③ 跳底哨兵：scrollTo(scrollHeight) 对 >30k px 长页不够，需连跳+容器感知
//   ④ "展开N条回复"按钮点击预算化（防展开风暴刷爆请求）
//   ⑤礼貌节奏：每轮 sleep 可调，导航间隔独立
//   ⑥ argv 路径不要用 process.argv[0]（那是 node 自身）
//
// 用法（复制到任务目录后改 THREE 处标注 /* [改] */ 即可）:
//   CAPTURE_PATH=out.jsonl SESSION_TEMPLATE_TARGET="https://site/list" \
//   SESSION_TEMPLATE_CONTAINER=".note-scroller" \
//   SESSION_TEMPLATE_EXPAND="^展开" \
//   SESSION_TEMPLATE_TARGET_PAGES=8 \
//   node browser_session_template.cjs
// 需要 patchright/playwright + 已登录的持久 profile（先跑 capture_daemon.cjs）。
const fs = require('fs');
let chromium;
try { ({ chromium } = require('patchright')); } catch (e) { ({ chromium } = require('playwright')); }

// ── 参数（环境变量注入，模板零硬编码）──
const CDP = process.env.SESSION_TEMPLATE_CDP || 'http://127.0.0.1:9222';
const CAP = process.env.CAPTURE_PATH || 'capture.jsonl';
const TARGET = process.env.SESSION_TEMPLATE_TARGET || '';
const CONTAINER = process.env.SESSION_TEMPLATE_CONTAINER || '';   // 滚动容器选择器（空=全局滚动）
const EXPAND_RE = process.env.SESSION_TEMPLATE_EXPAND || '';      // 展开按钮文本正则（空=不点）
const TARGET_PAGES = Number(process.env.SESSION_TEMPLATE_TARGET_PAGES || 8);
const NEEDLE = process.env.SESSION_TEMPLATE_NEEDLE || '';         // 分页请求 URL 特征（收敛依据）
const ROUND_SLEEP = Number(process.env.SESSION_TEMPLATE_ROUND_SLEEP || 1600);
const MAX_ROUNDS = Number(process.env.SESSION_TEMPLATE_MAX_ROUNDS || 26);

if (!TARGET || !NEEDLE) {
  console.error('usage: SESSION_TEMPLATE_TARGET=<url> SESSION_TEMPLATE_NEEDLE=<分页URL特征> node browser_session_template.cjs');
  process.exit(2);
}

function fileSize() {
  try { return fs.statSync(CAP).size; } catch (e) { return 0; }
}
function tailFrom(offset) {
  try {
    const size = fs.statSync(CAP).size;
    if (size <= offset) return '';
    const fd = fs.openSync(CAP, 'r');
    const len = size - offset;
    const buf = Buffer.alloc(len);
    fs.readSync(fd, buf, 0, len, offset);
    fs.closeSync(fd);
    return buf.toString('utf8');
  } catch (e) { return ''; }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  const browser = await chromium.connectOverCDP(CDP);
  const ctx = browser.contexts()[0];
  const page = ctx.pages().length ? ctx.pages()[ctx.pages().length - 1] : await ctx.newPage();
  const out = { target: TARGET, steps: [], t0: Date.now() };
  try {
    const capBase = fileSize();
    await page.goto(TARGET, { waitUntil: 'domcontentloaded', timeout: 45000 });
    await sleep(3500);

    // ②③ 滚动循环：捕获增量收敛 + 跳底哨兵连跳
    let lastPages = -1, stable = 0, pages = 0;
    for (let r = 0; r < MAX_ROUNDS; r++) {
      await page.evaluate((containerSel) => {
        const el = containerSel ? document.querySelector(containerSel) : null;
        if (el) {
          el.scrollTop = el.scrollHeight;   // ③ 跳到真实底部强制触发懒加载
          el.scrollBy(0, 600);
        } else {
          window.scrollTo(0, document.body.scrollHeight);
        }
      }, CONTAINER).catch(() => {});
      await sleep(ROUND_SLEEP);
      // ④ 展开按钮预算化点击（最多 3 个/轮）
      if (EXPAND_RE) {
        await page.evaluate((re) => {
          const rx = new RegExp(re);
          const btns = [...document.querySelectorAll('div,span')].filter(
            (e) => e.children.length === 0 && rx.test((e.innerText || '').trim()) && (e.innerText || '').length < 16
          );
          for (const b of btns.slice(0, 3)) b.click();
        }, EXPAND_RE).catch(() => {});
        await sleep(900);
      }
      pages = tailFrom(capBase).split(NEEDLE).length - 1;
      if (pages >= TARGET_PAGES) { out.steps.push('pages:' + pages + '@r' + r); break; }
      if (pages === lastPages) { stable++; if (stable >= 4) { out.steps.push('stable@' + pages + 'p,r' + r); break; } }
      else stable = 0;
      lastPages = pages;
    }
    out.pagesCaptured = pages;
    out.ms = Date.now() - out.t0;
    const metaPath = process.env.SESSION_TEMPLATE_META || (TARGET.replace(/[^\w.-]+/g, '_') + '.meta.json');
    fs.writeFileSync(metaPath, JSON.stringify(out, null, 1));
    console.log('OK ' + JSON.stringify(out));
  } catch (e) {
    out.error = e && e.message ? e.message : String(e);
    try {
      const metaPath = process.env.SESSION_TEMPLATE_META || 'session_template.meta.json';
      fs.writeFileSync(metaPath, JSON.stringify(out, null, 1));
    } catch (e2) {}
    console.error('FAIL ' + out.error);
    process.exitCode = 1;  // 审查六轮（L5）：失败曾以 exit 0 结束，自动化拿到假绿灯
  } finally {
    await browser.close();  // 仅断开 CDP，不关浏览器
  }
}

main().catch((e) => { console.error('FATAL ' + (e && e.message ? e.message : String(e))); process.exit(1); });
