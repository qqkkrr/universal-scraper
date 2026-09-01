#!/usr/bin/env node
const { loadChromium, sleep } = require("./browser_common.cjs");
(async () => {
  const chromium = loadChromium();
  const browser = await chromium.connectOverCDP("http://127.0.0.1:9222");
  let page = null;
  for (const c of browser.contexts()) for (const p of c.pages()) if (p.url().includes("cx.cnca.cn")) page = p;
  if (!page) { console.log("无 cx 页面"); process.exit(1); }
  const logs = [];
  page.on("console", m => { if (m.type()==="error"||m.type()==="warning") logs.push(m.type()+": "+m.text().slice(0,150)); });
  page.on("pageerror", e => logs.push("pageerror: "+String(e).slice(0,200)));
  // 重新点一次查询（当前条件已填）
  await page.evaluate(() => {
    const btns=[...document.querySelectorAll("button, .btn")];
    const q=btns.find(b=>(b.textContent||"").trim().includes("询"));
    if(q) q.click();
  });
  await sleep(6000);
  const st = await page.evaluate(() => {
    const out = {};
    // 极验脚本是否加载
    out.gtScripts = [...document.querySelectorAll("script[src]")].map(s=>s.src).filter(s=>/geetest|gcaptcha/i.test(s));
    // 极验容器详情
    const gt=document.querySelector("[class*=geetest_captcha]");
    if (gt) {
      const r=gt.getBoundingClientRect();
      out.gtRect=[Math.round(r.x),Math.round(r.y),Math.round(r.width),Math.round(r.height)];
      out.gtChildCount=gt.children.length;
      out.gtHtml=gt.innerHTML.slice(0,200);
    }
    // 全局 captcha 对象
    out.hasCaptcha = typeof window.captcha;
    out.captchaKeys = (window.captcha && typeof window.captcha==="object") ? Object.keys(window.captcha).slice(0,20) : [];
    // 极验 iframe
    out.frames=[...document.querySelectorAll("iframe")].map(f=>(f.src||f.id).slice(0,90));
    return out;
  });
  console.log("== console 错误 =="); logs.slice(-10).forEach(l=>console.log(l));
  console.log("== 状态 ==", JSON.stringify(st,null,1));
  await browser.close();
})().catch(e=>{console.log("ERR",e.message.slice(0,400));process.exit(1);});
