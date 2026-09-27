// 单篇笔记采集驱动（收编自 xhs_damo_task 实战，实战反馈五#1）。
// 与原版差异仅一处：capture 路径经 CAPTURE_PATH 环境变量注入（配合通用守护进程）。
// 单篇笔记采集 v2：导航笔记页 + 滚动 .note-scroller 加载评论到 ≥50 顶层 + 展开回复
// 用法: node collect_note.cjs <note_id> <xsec_token> <out_prefix>
// 详情与评论均由守护进程捕获（SSR HTML + comment/page + sub/page）；本脚本只做驱动与计数
const fs = require('fs');
const { chromium } = require('patchright');

const CDP = 'http://127.0.0.1:9222';
const CAP = process.env.CAPTURE_PATH || __dirname + '/capture.jsonl';
const [, , noteId, token, outPrefix] = process.argv;
if (!noteId || !outPrefix) {
  console.error('usage: collect_note.cjs <note_id> <xsec_token> <out_prefix>');
  process.exit(2);
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function fileSize() {
  try { return fs.statSync(CAP).size; } catch (e) { return 0; }
}

// 从 offset 到文件尾的增量内容
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

async function main() {
  const browser = await chromium.connectOverCDP(CDP);
  const ctx = browser.contexts()[0];
  const page = ctx.pages().length ? ctx.pages()[ctx.pages().length - 1] : await ctx.newPage();
  const url = `https://www.xiaohongshu.com/explore/${noteId}?xsec_token=${encodeURIComponent(token)}&xsec_source=pc_search`;
  const out = { noteId, url, steps: [], t0: Date.now() };
  const cpNeedle = 'comment/page?note_id=' + noteId + '&';
  try {
    const capBase = fileSize();
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 45000 });
    await sleep(3500);

    // 滚动评论区：.note-scroller 为主，全局兜底；以捕获的 comment/page 增量为收敛依据
    let lastPages = -1, stable = 0;
    const targetPages = 8; // 8×10=80 ≥ 50 顶层，留余量；cursor 不前进时稳定退出
    let pages = 0;
    for (let r = 0; r < 26; r++) {
      await page.evaluate(() => {
        const el = document.querySelector('.note-scroller');
        if (el) {
          el.scrollTop = el.scrollHeight; // 跳到真实底部，强制触发懒加载
          el.scrollBy(0, 600);
        } else {
          window.scrollTo(0, document.body.scrollHeight);
        }
      }).catch(() => {});
      await sleep(1600);
      // 预算内的"展开N条回复"点击
      await page.evaluate(() => {
        const btns = [...document.querySelectorAll('div,span')].filter(
          (e) => e.children.length === 0 && /^展开/.test((e.innerText || '').trim()) && (e.innerText || '').length < 16
        );
        for (const b of btns.slice(0, 3)) b.click();
      }).catch(() => {});
      await sleep(900);
      pages = tailFrom(capBase).split(cpNeedle).length - 1;
      if (pages >= targetPages) { out.steps.push('pages:' + pages + '@r' + r); break; }
      if (pages === lastPages) { stable++; if (stable >= 4) { out.steps.push('stable@' + pages + 'p,r' + r); break; } }
      else stable = 0;
      lastPages = pages;
    }
    out.commentPagesCaptured = pages;
    out.subPagesCaptured = tailFrom(capBase).split('comment/sub/page?note_id=' + noteId + '&').length - 1;
    out.ms = Date.now() - out.t0;
    fs.writeFileSync(outPrefix + '.meta.json', JSON.stringify(out, null, 1));
    console.log('OK ' + JSON.stringify(out));
  } catch (e) {
    out.error = e && e.message ? e.message : String(e);
    try { fs.writeFileSync(outPrefix + '.meta.json', JSON.stringify(out, null, 1)); } catch (e2) {}
    console.error('FAIL ' + out.error);
  } finally {
    await browser.close();
  }
}

main().catch((e) => { console.error('FATAL ' + (e && e.message)); process.exit(1); });
