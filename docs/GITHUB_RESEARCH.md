# GitHub 权威爬虫方案研究笔记（第三十一轮）

来源：2026-08 调研 GitHub 上 2024–2026 年活跃/权威的爬虫与反爬项目，逐项对照
本工具已有能力，落地可实施部分。仓库与要点如下。

## 研究清单

| 项目 | 关键能力 | 本工具落地点 |
|---|---|---|
| apify/crawlee（TS+Python） | SessionPool、block-detection、指纹生成、自适应并发、代理配置、RequestQueue | `session.py` 会话池、`detect_block`、已有 queue/自适应并发 |
| omkarcloud/botasaurus | AntiDetectDriver、Cloudflare 绕过、Capsolver 集成、代理轮换 | CDP 直连 + CF 自动点击 + 既有 2captcha/nopecha |
| lexiforest/curl_cffi | TLS/JA3/HTTP2 指纹伪装（requests 兼容） | `sites.fetch_html`/`fetch_bytes` 接入 chrome impersonate |
| art3m4ik3/cloudflare-solver | Turnstile 自动点击 + 真人化 + cf_clearance | `browser_generic.cjs` 自动点击复选框 |
| NanmiCoder/MediaCrawler | 小红书/抖音/快手/B站 评论、CDP 模式、签名仓库 | CDP 直连模式（真实浏览器附着） |
| g1879/DrissionPage | 浏览器+requests 统一、反检测、CDP | CDP 模式、`us cookies` 登录态复用 |
| 934050259/BrowserCluster | Playwright+DrissionPage 双引擎、Cookie 池、代理池、任务调度 | 会话池 + 代理池 + 浏览器桥 |
| unclecode/crawl4ai | LLM 抽取、markdown、magic-mode stealth | 已有 LLM 兜底抽取 + stealth 脚本 |
| firecrawl/firecrawl | scrape/crawl/batch、actions 动作链、markdown | 已有 fetch/crawl 命令 + actions 动作链 |
| trafilatura | 高质量正文提取、编码探测 | `smart_decode` 编码探测思路 |
| FlareSolverr / cloudscraper | Cloudflare 5秒盾绕过 | CF 自动点击 + 人机结合 |
| Egida/scrapoxy | 超级代理聚合/自动轮换 | 已有 ProxyPool（可对接任意代理源） |
| DsTansice/aggregator | 免费代理池自动构建 | 未来可对接（未集成） |

## 落地总结

- 编码：charset_normalizer + 候选打分，修复 GBK/错标乱码（实测新浪 GBK 零乱码）。
- 封禁：`detect_block` 识别 CF/验证/登录/验证码/限流/403/429 → 会话池自动换 UA/代理 →
  AI 自修复提示升级浏览器/CDP。
- 浏览器：CDP 直连真实浏览器（最强）、Turnstile 自动点击、stealth 指纹增强
  （navigator.vendor/platform/appVersion/userAgent/chrome.loadTimes）。
- 会话：HTTP 会话池（域名 Cookie/UA/代理，封禁自动轮换）；浏览器登录态导出 Cookie 串。
- 下载：`download` 流水线通用文件下载；`journal` 期刊全文批量下载（magtech 通用）。

## 未落地/未来方向

- camoufox（Firefox 反指纹内核，~700MB）：可作为可选浏览器引擎，对抗极强指纹检测。
- 免费代理池自动构建（aggregator）：对接后可自动扩充代理。
- Playwright 双引擎（DrissionPage）：复杂 SPA 可再加一路取数器。
