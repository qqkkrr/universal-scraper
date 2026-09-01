#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""登录态浏览器直驱引擎：附着用户已登录的调试 Chrome（9222 端口），
在该浏览器的真实会话中执行任务——所有需要登录的网站不再是障碍。

原理：Playwright connectOverCDP → 用户浏览器 → 打开目标页 → 等待渲染 → 提取。
用户的 Cookie/Token/指纹全部天然继承，零配置零试错。

用法:
  from universal_scraper.browser_drive import browser_drive_crawl
  result = browser_drive_crawl(page, "https://www.douyin.com/search/厨房神器",
                                scroll_rounds=10, max_items=50)
"""
import time


def check_chrome_alive(port: int = 9222) -> bool:
    """调试 Chrome 是否在运行。"""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def browser_drive_crawl(page, url: str, scroll_rounds: int = 10,
                        max_items: int = 200, wait_ms: int = 3000,
                        log=None) -> dict:
    """在已登录的浏览器中打开目标页 → 滚动加载 → 提取所有可见的问答/列表项。

    Args:
        page: Playwright Page（已附着到调试 Chrome）
        url: 目标页面 URL
        scroll_rounds: 最大滚动轮数（每轮等 2s）
        max_items: 最大条数
        wait_ms: 每轮滚动后等待时间（ms）

    Returns:
        {title, url, items: [{text, href}], html_len, screenshots: []}
    """
    def _log(m):
        if log:
            log(m)

    _log(f"🖥️ 浏览器直驱: {url[:80]}")
    page.goto(url, timeout=45000, wait_until="domcontentloaded")
    time.sleep(wait_ms / 1000)

    # 滚动加载更多内容
    for i in range(scroll_rounds):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(2)
        _log(f"  滚动 {i+1}/{scroll_rounds}…")

    # 提取所有可见的问答/列表项
    items = page.evaluate("""
      () => {
        const out = [];
        // 策略1: 常见列表选择器
        const selectors = [
          '.m_feed_item',           // 通用 feed
          '[class*="article"]',      // 文章
          '[class*="video-item"]',   // 视频卡片
          '[class*="search-result"]', // 搜索结果
          '[class*="list-item"]',    // 通用列表
          'article',                  // HTML5 article
          '.rank_box .m_feed_item',  // e互动问答
          '[data-e2e="scroll-list"] > div', // 抖音
        ];
        const seen = new Set();
        for (const sel of selectors) {
          document.querySelectorAll(sel).forEach(el => {
            const text = (el.innerText || '').trim();
            if (text.length < 20 || seen.has(text)) return;
            seen.add(text);
            const link = el.querySelector('a[href]');
            const dateEl = el.innerText.match(/20\\d{2}[-年]\\d{1,2}[-月]\\d{1,2}/);
            out.push({
              text: text.slice(0, 500),
              href: link ? link.href : '',
              date: dateEl ? dateEl[0] : ''
            });
          });
        }
        return out;
      }
    """)

    title = page.title()
    final_url = page.url
    html_len = len(page.content())

    _log(f"  ✅ 提取 {len(items)} 条 | 页面: {title[:40]}")

    return {
        "title": title,
        "url": final_url,
        "items": items[:max_items],
        "html_len": html_len,
    }


def browser_drive_crawl_full(page, url: str, scroll_rounds: int = 10,
                              log=None) -> dict:
    """完整版：提取 + 返回页面全文（供后续 A/B/C 分类和关键词统计）。"""
    result = browser_drive_crawl(page, url, scroll_rounds, log=log)
    # 追加全文供关键词统计
    try:
        full_text = page.inner_text("body")
        result["full_text"] = full_text[:50000]
    except Exception:
        pass
    return result
