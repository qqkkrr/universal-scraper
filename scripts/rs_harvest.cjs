#!/usr/bin/env node
/**
 * rs_harvest.cjs —— 瑞数(RiverSecurity)/强防护 SPA 站配置驱动采集器。
 *
 * NMPA 战训（2026-09-10，575/575 全量验证）浓缩成通用配方：
 *   真实 Chrome CDP 过瑞数 → UI 操作触发查询 → 拦截 XHR JSON 响应（不解析 DOM 文本）
 *   → 翻页循环收集 → JSON/CSV 落盘。适用所有"界面是 Vue/Element UI + 数据走
 *   XHR 接口"的 gov 站（药监局/卫健委等瑞数系）。
 *
 * 用法：
 *   node rs_harvest.cjs --config harvest.json
 *   node rs_harvest.cjs --inline '{"cdp":...,"steps":[...]}'
 *
 * 配置字段（全部键均可省略，见 DEFAULTS）：
 *   cdp              CDP 地址，默认 http://127.0.0.1:9222
 *   goto             起始页 URL（复用标签页时可省）
 *   gotoWait         起始页等待 ms（瑞数挑战二次加载），默认 5000
 *   reuseTabPattern  正则串：命中已有标签页 URL 则复用（省重复过挑战、防标签页堆积）
 *   steps[]          UI 操作序列：{click, fill:{selector,text,delay}, press, hover,
 *                    evaluate, wait, index, popup}
 *                    popup:true 表示该点击可能弹新标签页（查询按钮），后续在新页操作
 *   intercept        {urlPattern, listPath, totalPath, pageNumPath}  XHR 拦截
 *   fields           {"中文名": "f0", ...}  行字段映射（getPath 语义）
 *   dedupKeys        去重键数组，默认取 fields 全部键
 *   paginate         {mode:"jumper"|"next", selector, from}  jumper 填页码回车/next 点下一页
 *   maxPages         翻页上限，默认 200
 *   pageDelay        每页间隔 ms（礼貌），默认 1200
 *   outJson/outCsv   输出路径（必填其一，建议都给）
 *   debugShot        出错/无进展时截图路径
 *   expectTotal      校验口径：不给则以接口 totalPath 为准
 */
'use strict';
const fs = require('fs');
const path = require('path');

function arg(name, dflt) {
  const i = process.argv.indexOf('--' + name);
  if (i >= 0 && i + 1 < process.argv.length) return process.argv[i + 1];
  return dflt;
}
function die(msg) { console.error(JSON.stringify({ type: 'error', message: msg })); process.exit(1); }

// playwright 解析：优先 skill 目录 node_modules，再退化到全局
let pw;
try { pw = require('playwright'); }
catch (e) {
  try { pw = require(path.join(__dirname, '..', 'node_modules', 'playwright')); }
  catch (e2) { die('playwright 未安装：先运行 scripts/setup.sh'); }
}

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

function getPath(obj, p) {
  if (p === undefined || p === null || p === '') return undefined;
  try {
    return String(p).split('.').reduce((o, k) => (o == null ? undefined : o[k]), obj);
  } catch (e) { return undefined; }
}

function loadConfig() {
  const cfgFile = arg('config', null);
  const inline = arg('inline', null);
  let raw = null;
  if (cfgFile) raw = fs.readFileSync(cfgFile, 'utf-8');
  else if (inline) raw = inline;
  else die('需要 --config <file> 或 --inline <json>');
  let cfg;
  try { cfg = JSON.parse(raw); } catch (e) { die('配置不是合法 JSON: ' + e.message); }
  if (!cfg || typeof cfg !== 'object') die('配置应为 JSON 对象');
  if (!cfg.outJson && !cfg.outCsv) die('至少要给 outJson 或 outCsv 之一（否则采了没处落盘）');
  if (!Array.isArray(cfg.steps) && !cfg.reuseTabPattern && !cfg.goto)
    die('steps / reuseTabPattern / goto 至少给一个');
  if (!cfg.intercept || !cfg.intercept.urlPattern) die('intercept.urlPattern 必填（要拦截的 XHR 接口特征）');
  // 审查修复（P1）：输出目录不存在曾让成功采集后的落盘 ENOENT 崩掉、全部数据丢失
  for (const p of [cfg.outJson, cfg.outCsv]) {
    if (p) { try { fs.mkdirSync(path.dirname(p), { recursive: true }); } catch (e) {} }
  }
  return cfg;
}

function writeCsv(file, fields, rows) {
  const cols = Object.keys(fields);
  // 审查修复（M）：esc 曾只转义内嵌引号——值里含逗号/换行/回车时 CSV 列错位
  const esc = (v) => '"' + String(v == null ? '' : v).replace(/"/g, '""').replace(/[\r\n]+/g, ' ') + '"';
  const lines = ['\uFEFF' + cols.map(esc).join(',')];
  for (const r of rows) lines.push(cols.map(c => esc(r[c])).join(','));
  fs.writeFileSync(file, lines.join('\n'), 'utf-8');
}

(async () => {
  const cfg = loadConfig();
  const t0 = Date.now();
  const CDP = cfg.cdp || 'http://127.0.0.1:9222';
  const fields = cfg.fields || {};
  const dedupKeys = (Array.isArray(cfg.dedupKeys) && cfg.dedupKeys.length) ? cfg.dedupKeys : Object.keys(fields);
  const maxPages = Math.max(1, parseInt(cfg.maxPages, 10) || 200);
  const pageDelay = Math.max(300, parseInt(cfg.pageDelay, 10) || 1200);

  const browser = await pw.chromium.connectOverCDP(CDP);
  const context = browser.contexts()[0] || await browser.newContext();
  const ownPages = [];      // 本进程创建的标签页——退出前必须关闭（防堆积，NMPA 复盘）
  let reused = false;

  // —— 状态 ——
  const rows = [];
  const seen = new Set();            // 审查修复（P2）：O(n²) some() 换 Set，2 万行级采集不被拖死
  let total = (cfg.expectTotal != null) ? Number(cfg.expectTotal) : null;
  let lastPageNum = null;
  let gotResponse = false;
  let parseFailures = 0;             // 审查修复（P0）：拦截响应解析失败曾静默——弱模型把半程数据当全量交付

  const keyOf = (row) => JSON.stringify(dedupKeys.map(k => row[k]));
  const addRows = (list) => {
    let added = 0;
    for (const it of (list || [])) {
      const row = {};
      for (const [cn, p] of Object.entries(fields)) row[cn] = getPath(it, p);
      const k = keyOf(row);
      if (!seen.has(k)) { seen.add(k); rows.push(row); added++; }
    }
    return added;
  };

  const attachCollector = (page) => {
    page.on('response', async (r) => {
      try {
        if (!r.url().includes(cfg.intercept.urlPattern)) return;
        const ct = String((r.headers() || {})['content-type'] || '');
        if (ct && !ct.includes('json')) return;
        let j;
        try {
          j = await r.json();
        } catch (e) {
          parseFailures++;
          console.log(`  [warn] 拦截响应解析失败（截断/被挑战页替换?）: ${r.url().slice(0, 100)} — ${e.message.slice(0, 80)}`);
          return;
        }
        const list = getPath(j, cfg.intercept.listPath || 'data.list');
        if (!Array.isArray(list)) {
          // 合法 JSON 但不是数据包（如 {"code":500} 错误封套）——同样不能静默
          parseFailures++;
          console.log(`  [warn] 拦截响应无数据列表（listPath=${cfg.intercept.listPath || 'data.list'} 未命中）：${JSON.stringify(j).slice(0, 120)}`);
          return;
        }
        const t = getPath(j, cfg.intercept.totalPath || 'data.total');
        if (t != null) total = Number(t);
        const pn = getPath(j, cfg.intercept.pageNumPath || 'data.pageNum');
        if (pn != null) lastPageNum = Number(pn);
        const added = addRows(list);
        gotResponse = true;
        console.log(`  <- page ${lastPageNum != null ? lastPageNum : '?'}: +${added}, total ${rows.length}/${total}`);
      } catch (e) { /* 收集器自身异常不外抛（保住页面会话），计数可见 */ parseFailures++; }
    });
  };

  // —— 统一出口：先清理再退出；close 挂死 6s 硬退（与 browser_generic 同款） ——
  let finished = false;
  async function finish(code) {
    if (finished) return;
    finished = true;
    const hard = setTimeout(() => { try { process.exit(code); } catch (e) {} }, 6000);
    if (hard.unref) hard.unref();
    for (const p of ownPages) {
      try { await Promise.race([p.close(), new Promise(r => setTimeout(r, 1500))]); } catch (e) {}
    }
    try { await Promise.race([browser.close(), new Promise(r => setTimeout(r, 4000))]); } catch (e) {}
    clearTimeout(hard);
    process.exit(code);
  }
  const bail = async (msg) => {
    console.error(JSON.stringify({ type: 'error', message: msg }));
    if (cfg.debugShot && resultPage) { try { await resultPage.screenshot({ path: cfg.debugShot }); } catch (e) {} }
    try { dump(true); } catch (e) { console.error(JSON.stringify({ type: 'error', message: '部分结果落盘也失败: ' + e.message })); }
    await finish(1);
  };

  // —— 落盘（部分结果也要落：NMPA 战训"崩溃不丢已抓数据"） ——
  const dump = (partial) => {
    if (cfg.outJson) {
      fs.writeFileSync(cfg.outJson, JSON.stringify({
        source: cfg.goto || cfg.reuseTabPattern || '', partial: !!partial,
        total_official: total, scraped: rows.length,
        parse_failures: parseFailures,
        stop_reason: stopReason,
        scraped_at: new Date().toISOString(), rows,
      }, null, 1), 'utf-8');
    }
    if (cfg.outCsv && Object.keys(fields).length) writeCsv(cfg.outCsv, fields, rows);
  };
  let stopReason = 'not_started';   // 审查修复（P0）：partial 口径由此推导，不再想当然

  let resultPage = null;
  try {
    // 复用已有标签页（防瑞数挑战反复触发 + 防标签页堆积）
    if (cfg.reuseTabPattern) {
      let re = null;
      try { re = new RegExp(cfg.reuseTabPattern); } catch (e) { throw new Error(`reuseTabPattern 非法正则: ${e.message}`); }
      const hit = context.pages().find(p => { try { return re.test(p.url()); } catch (e) { return false; } });
      if (hit) {
        resultPage = hit; reused = true;
        console.log(`[reuse] 复用已有标签页: ${hit.url().slice(0, 90)}`);
        if (Array.isArray(cfg.steps) && cfg.steps.length)
          console.log('[reuse] 跳过 steps（复用标签页不重放 UI 流程）');
      }
    }

    // 走 UI 流程（无复用时）
    let page = null;
    if (!resultPage) {
      if (!cfg.goto) throw new Error('没有命中 reuseTabPattern 的标签页，且未给 goto——无法开始');
      page = await context.newPage();
      ownPages.push(page);
      attachCollector(page);                       // 弹窗场景：第一步的 XHR 也可能在本页
      console.log(`[goto] ${cfg.goto}`);
      await page.goto(cfg.goto, { waitUntil: 'domcontentloaded', timeout: 60000 });
      await sleep(Math.max(0, parseInt(cfg.gotoWait, 10) || 5000));

      for (let i = 0; i < cfg.steps.length; i++) {
        const st = cfg.steps[i];
        const idx = (st.index != null) ? parseInt(st.index, 10) : 0;
        const host = resultPage || page;
        // popup 监听必须在点击前挂载（NMPA 战训：先 waitForEvent 再 click，
        // 反过来挂会漏掉已弹出的新标签页）
        const popupP = st.popup
          ? context.waitForEvent('page', { timeout: 20000 }).catch(() => null)
          : null;
        if (st.click) {
          await host.locator(st.click).nth(idx).click({ force: true, timeout: 20000 });
        } else if (st.fill) {
          const inp = host.locator(st.fill.selector).nth(idx);
          await inp.click({ timeout: 15000 });
          await inp.type(String(st.fill.text == null ? '' : st.fill.text),
                         { delay: Math.max(0, parseInt(st.fill.delay, 10) || 60) });
        } else if (st.press) {
          await host.locator(st.press.selector).nth(idx).press(String(st.press.key || 'Enter'));
        } else if (st.hover) {
          await host.locator(st.hover).nth(idx).hover({ timeout: 15000 });
        } else if (st.evaluate) {
          await host.evaluate(st.evaluate);
        }
        // 触发类点击可能弹新标签页（查询按钮）→ 等待并切换 resultPage
        if (popupP) {
          const popup = await popupP;
          if (popup) {
            attachCollector(popup);                // NMPA 战训：结果在新标签页，拦截器必须跟着挂
            ownPages.push(popup);
            resultPage = popup;
            await popup.bringToFront().catch(() => {});
          } else {
            console.log('  [warn] popup:true 但 20s 内没有新标签页——继续用当前页');
          }
        }
        if (st.wait) await sleep(parseInt(st.wait, 10));
      }
      resultPage = resultPage || page;
    } else {
      attachCollector(resultPage);
    }
    await resultPage.bringToFront().catch(() => {});
    console.log(`[page] ${resultPage.url().slice(0, 90)}`);

    // —— 翻页收集 ——
    const pag = cfg.paginate || { mode: 'jumper' };
    const jumpSel = pag.selector || '.el-pagination__jump input';
    const jump = async (n) => {
      gotResponse = false;
      const inp = resultPage.locator(jumpSel).first();
      await inp.fill(String(n), { timeout: 10000 });
      await inp.press('Enter');
      for (let i = 0; i < 24 && !gotResponse; i++) await sleep(500);   // 最多等 12s
      await sleep(pageDelay);
      return gotResponse;
    };
    const next = async () => {
      gotResponse = false;
      const btn = resultPage.locator(pag.selector || '.btn-next').first();
      // 审查修复（P1）：探测失败 ≠ 已禁用——曾把选择器写错映射成"到头了"假完成
      const state = await btn.evaluate(el => (el.classList.contains('disabled') || el.disabled) ? 'disabled' : 'ok')
                             .catch(e => { console.log('  [warn] next 按钮探测失败: ' + String(e.message).slice(0, 80)); return 'error'; });
      if (state !== 'ok') return false;
      await btn.click({ force: true, timeout: 10000 });
      for (let i = 0; i < 24 && !gotResponse; i++) await sleep(500);
      await sleep(pageDelay);
      return gotResponse;
    };

    // 先跳到 from 页（jumper 用于把 UI 拉回第 1 页/继续第 N 页；next 顺序前进）
    let noProgress = 0;
    stopReason = 'max_pages';
    // 审查修复（L）：from 曾在每轮循环重复计算（值恒定）——提升出循环
    const from = Number.isFinite(Number(pag.from)) ? Number(pag.from) : 1;
    for (let p = 1; p <= maxPages; p++) {
      if (total != null && rows.length >= total) { stopReason = 'total_reached'; console.log('[stop] 已达官方总数'); break; }
      const before = rows.length;
      const target = (pag.mode === 'next') ? null : (from + p - 1);
      const ok = target != null ? await jump(target) : await next();
      if (!ok) console.log(`  [warn] 第 ${target != null ? target : p} 轮未收到接口响应，重试一次`);
      if (!ok && target != null) { gotResponse = false; await jump(target); }
      if (rows.length === before) {
        noProgress++;
        if (noProgress >= 3) { stopReason = 'no_progress'; console.log('[stop] 连续 3 轮无新增，结束'); break; }
      } else noProgress = 0;
    }
    await sleep(1000);
    // 审查修复（P0）：partial 不再说谎——只有"到官方总数且零解析失败"才算完整；
    // max_pages 打满上限也可能是截断，同样按 partial 口径
    const complete = (stopReason === 'total_reached') && parseFailures === 0;
    dump(!complete);

    const uniq = seen.size;
    console.log(JSON.stringify({
      type: 'done', scraped: rows.length, total_official: total,
      dedup: uniq, reused_tab: reused, stop_reason: stopReason,
      parse_failures: parseFailures, partial: !complete,
      elapsed_sec: Math.round((Date.now() - t0) / 1000),
      outJson: cfg.outJson || null, outCsv: cfg.outCsv || null,
    }));
    await finish(0);
  } catch (e) {
    await bail(String((e && e.message) || e));
  }
})().catch(e => { console.error(JSON.stringify({ type: 'error', message: String((e && e.message) || e) })); process.exit(1); });
