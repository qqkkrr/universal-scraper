#!/usr/bin/env node
// 通用常驻捕获守护进程（实战反馈五#1/#2 收编，源自 xhs_damo_task/xhs_launch.cjs）：
// patchright 持久上下文（登录态 profile 复用）+ 库层代理 + 指定域 XHR 持续捕获落 JSONL。
// 所有签名型站点（小红书/淘宝/抖音）通用：页面自己算签名，守护进程只负责"在场捕获"。
//
// 用法:
//   node capture_daemon.cjs --hosts a.com,b.com [--capture out.jsonl] [--profile dir]
//        [--proxy http://127.0.0.1:7897] [--cdp 9222] [--headful]（默认有头，登录/滑块人工在环）
// 退出: kill -TERM <pid>（优雅关闭）
// 状态: <capture>.status.json 每次捕获后刷新（CLI status 子命令读取）
const path = require('path');
const fs = require('fs');
const os = require('os');

function arg(name, def) {
  const i = process.argv.indexOf('--' + name);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : def;
}
const flag = (name) => process.argv.includes('--' + name);

const HOSTS = String(arg('hosts', '')).split(',').map(s => s.trim()).filter(Boolean);
const CAPTURE_PATH = path.resolve(String(arg('capture', 'capture.jsonl')));
const PROFILE_DIR = path.resolve(String(arg('profile', path.join(os.homedir(), '.universal-scraper', 'daemon_profile'))));
const PROXY = String(arg('proxy', ''));
const CDP_PORT = Number(arg('cdp', 9222));
const HEADLESS = flag('headless');

if (!HOSTS.length) {
  console.error('usage: capture_daemon.cjs --hosts a.com,b.com [--capture out.jsonl] [--profile dir] [--proxy URL] [--cdp 9222]');
  process.exit(2);
}

let chromium;
try {
  ({ chromium } = require('patchright'));
} catch (e) {
  ({ chromium } = require('playwright'));
}

fs.mkdirSync(PROFILE_DIR, { recursive: true });
fs.mkdirSync(path.dirname(CAPTURE_PATH), { recursive: true });

function hostMatches(url) {
  try {
    const u = new URL(url);
    if (u.protocol !== 'https:' && u.protocol !== 'http:') return false;
    return HOSTS.some((d) => u.hostname === d || u.hostname.endsWith('.' + d));
  } catch (e) {
    return false;
  }
}

function safeBody(buf) {
  try {
    if (!buf) return null;
    if (buf.length > 2 * 1024 * 1024) return { truncated: true, len: buf.length };
    return buf.toString('utf8');
  } catch (e) {
    return null;
  }
}

let _lastStatusWrite = 0;
function writeStatus(extra, force) {
  // 审查六轮（L1）：每响应一写曾非原子+高频——tmp+rename 原子化 + 500ms 节流
  const now = Date.now();
  if (!force && now - _lastStatusWrite < 500) return;
  _lastStatusWrite = now;
  try {
    const target = CAPTURE_PATH + '.status.json';
    const tmp = target + '.tmp';
    fs.writeFileSync(tmp, JSON.stringify({
      pid: process.pid, hosts: HOSTS, capture: CAPTURE_PATH,
      profile: PROFILE_DIR, proxy: PROXY || null, cdp: CDP_PORT,
      captured: CAPTURED, updated: new Date().toISOString(), ...extra,
    }));
    fs.renameSync(tmp, target);
  } catch (e) { /* 状态文件失败不影响捕获主路 */ }
}

let CAPTURED = 0;

async function main() {
  const launchOpts = {
    headless: HEADLESS,
    locale: 'zh-CN',
    timezoneId: 'Asia/Shanghai',
    viewport: { width: 1440, height: 900 },
    args: [
      '--remote-debugging-port=' + CDP_PORT,
      '--no-first-run',
      '--no-default-browser-check',
      '--disable-session-crashed-bubble',
      '--hide-crash-restore-bubble',
    ],
  };
  if (PROXY) launchOpts.proxy = { server: PROXY };  // 库层代理：Chrome --proxy-server 被无视的站只能这样换出口
  const ctx = await chromium.launchPersistentContext(PROFILE_DIR, launchOpts);

  const wirePage = (page) => {
    page.on('response', async (resp) => {
      const url = resp.url();
      if (!hostMatches(url)) return;
      const req = resp.request();
      let body = null;
      try {
        body = safeBody(await resp.body());
      } catch (e) {
        body = null;
      }
      const rec = {
        ts: new Date().toISOString(),
        url,
        status: resp.status(),
        method: req.method(),
        post_data: req.postData() || null,
        body,
      };
      CAPTURED += 1;
      fs.appendFile(CAPTURE_PATH, JSON.stringify(rec) + '\n', () => {});
      writeStatus({});
    });
  };

  ctx.pages().forEach(wirePage);
  ctx.on('page', (p) => wirePage(p));

  writeStatus({}, true);
  console.log('LAUNCHED profile=' + PROFILE_DIR + (PROXY ? ' proxy=' + PROXY : '') + ' cdp=' + CDP_PORT);
  console.log('CAPTURE=' + CAPTURE_PATH + ' HOSTS=' + HOSTS.join(','));

  let closing = false;
  const shutdown = async (sig) => {
    if (closing) return;
    closing = true;
    console.log('shutting down on ' + sig);
    try { writeStatus({ stopping: true }, true); } catch (e) {}
    try { await ctx.close(); } catch (e) {}
    process.exit(0);
  };
  process.on('SIGTERM', () => shutdown('SIGTERM'));
  process.on('SIGINT', () => shutdown('SIGINT'));
  ctx.on('close', () => {
    console.log('context closed');
    process.exit(0);
  });
  setInterval(() => {}, 1 << 30);
}

main().catch((e) => {
  console.error('LAUNCH_FAIL ' + (e && e.message ? e.message : String(e)));
  process.exit(1);
});
