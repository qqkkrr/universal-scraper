# 反爬升级手册（实战检验版）

> 每一级都要向用户播报一句进展。升级不丢人，卡住不说才丢人。

## 一、判型表（侦察后 30 秒内定级）

| 症状（看 fetch 侦察结果） | 判型 | 起手级 |
|---|---|---|
| 200 且内容齐全在 HTML 里 | 直接可抓 | L0 |
| 页面是 JS 应用（有界面壳、数据靠 XHR 拼，如 EUIPO eSearch） | SPA 应用 | **接口捕获（capture_all）优先**，浏览器只留交互 |
| 返回"Just a moment"/Turnstile 挑战页；headless 也被卡；偶发 ERR_CONNECTION_CLOSED | Cloudflare 类 | 直接 L3：真实调试 Chrome 过一次校验 → 配置 cdp 附加（R16），headless 别硬试 |
| 403 / 412 / 468 / 503，或正常 UA 也被拒 | WAF 指纹拦截 | L1 |
| 200 但 body 极短（<2KB），含 `document.location`、`document.write`、`stoken`、`__js_challenge`、`setTimeout(...location...)` 之类脚本壳 | JS 挑战壳 | L1 → L2 |
| 返回验证码图片 / 滑块 / 点选 | 验证码 | L2 + 识别，失败 L4 |
| 提示登录 / 跳登录页 / 关键字段空且需会员 | 登录墙 | L4（唯一正路） |
| HTML 干净但没数据（列表空） | 数据走接口 | 接口捕获 |
| 前 N 条成功，之后 429/超时/空页 | 限流 | 降速 + L1 |
| 换任何方式都返回同一个短壳（如 gov.cn 1378 字节壳） | 全通道防护 | L5 人工通道 |

## 二、升级阶梯 L0 → L5

**L0 · HTTP 直抓**（默认）：`fetch <url>` / 配置 `type: http_html`。
最快最礼貌，能用它就不升级。

**L1 · 浏览器指纹**：用 curl_cffi 伪装真实 Chrome 的 TLS/HTTP2 指纹。
配置 `type` 换 curl_cffi 通道，或直接 `adaptive` 自适应引擎（自动试到通为止）。

**L2 · 无头浏览器**：真 Chromium 渲染。`fetch --browser`，或配置里
`pagination/wait/scroll_count` 全量支持（见 spec-schema）。
适合 JS 壳、动态列表、无限滚动。

**L3 · 附加用户已登录的 Chrome（CDP）**：
`bash "${SKILL_DIR}/scripts/open-debug-chrome.sh"` 弹独立调试窗口（端口 9222，
独立配置目录，不影响用户日常 Chrome）。用户登录一次后，配置加
`"cdp": "http://127.0.0.1:9222"` 即可在他的登录态里抓。
话术：*"我弹出一个浏览器窗口，请你像平常一样登录一次，好了说一声，之后不用你再管。"*

**L4 · 人工过一次关卡 + 复用**：
- 登录：同 L3。
- 滑块/验证码：配置 `captcha`/`slider` 段自动识别（ddddocr/轨迹模拟）；
  自动识别连续 2 次失败就转人工——弹浏览器让用户滑一下，会话状态保存后复用。
- 登录态导出：`cookies` 命令把浏览器登录态导成 Cookie 串，之后走 L0/L1 轻量直抓。

**L5 · 诚实告知边界**：全通道都不通时（典型：gov.cn 对数据中心 IP 全量返回
1378 字节 JS 壳，真人浏览器才放行），明确告诉用户：
> 这个站对所有自动化访问都关了门，只放行真人浏览器。两个选择：
> ① 你在自己浏览器里打开这个页面，Cmd+S 另存到任务文件夹，我来解析；
> ② 我先把其余部分全部抓完，这几个文件留到最后人工补。

绝不用"抓到了"掩盖没抓到，绝不伪造内容顶数。

## 三、限流与礼貌（预防 > 治疗）

- **请求预算概念**（强验证站点通用规律）：验证类 cookie（滑块/身份核实发的）不是
  永久通行证——它有请求预算，用完（如约 140 次请求）验证重新触发。预算将尽的
  信号：连续请求开始返回验证页。此时回 L4 重新过验证，或全程改用浏览器导航
  （会话内风控更宽松）。
- **HTTP 与浏览器会话连坐**：同一 IP 上 HTTP 批量爆发会把正在正常工作的浏览器
  会话一起封。强风控站点历史采集全程浏览器单线程导航，不用 HTTP 并发。
- 配置里加请求间隔（1～3 秒起），列表页翻页加随机抖动。
- 被限流后：立刻降速 ×2、降并发到 1；连续 3 轮 429 → 换时段再跑。
- 大任务建议 `proxy` 命令建免费代理池（仅用于公开数据、遵守目标站条款）。
- `crawl` 加 `--robots` 尊重站点声明。

## 四、接口捕获（数据不在 HTML 里时）

配置 `capture: true` + `record_from: capture_all`：浏览器方案会顺带录下页面
发出的所有 JSON 响应，落盘 `capture_all.json`。你（agent）读该文件，
找到目标数据的字段路径，回填到配置的 `records_path`/字段映射里再跑。
签名接口（参数带 sign/stoken/token 且你无法推导）→ 不逆向，走 L3 登录态直抓。

⚠️ **capture 只录 run 生命周期内的响应**。SPA 的数据 XHR 往往在页面加载完之后
异步触发（如 EUIPO eSearch），空等只会捕到配置/认证类响应——必须配
`"actions": [{"type": "wait", "ms": 10000}]` 这类等待让 XHR 有时间发出；
需要点击/翻页才出的数据，就把点击写进 actions 再捕获。

## 五、实战档案（真实案例，照方抓药）

| 站点 | 症状 | 有效方案 |
|---|---|---|
| gov.cn 政策文件 | 全通道 1378B JS 壳 | L5：真人浏览器另存 + 你解析 HTML |
| 大众点评 | csec 验证码 + 接口加密 | `dianping` 专用命令：登录 Cookie 直抓搜索列表 |
| 京东搜索/详情 | 登录墙 + h5st 签名 | 红线不碰签名；用豆瓣"在哪儿买"公开聚合价替代 |
| 豆瓣读书 | 无严重反爬 | `sites` 精配直接用 |
| 小红书评论 | SPA 签名接口 | 浏览器方案 + `record_from: capture` 捕获响应 |
| magtech 期刊系统 | 常规 | `journal` 精配批量下载 PDF |
| 抖音话题视频 | 强 JS 壳 + 签名 | 仅浏览器可读部分可见数据；抓不全时如实说明并 L5 |
| EUIPO eSearch plus | 13KB JS 应用壳，直抓无数据；后端 API 基座活跃（/eSearch/api 返回 200） | 先 capture_all 捕获检索/详情接口 → JSON 直抓；浏览器只留交互（配方 R13） |
| 东方财富股吧 | SSR 内嵌 JSON（window.article_list）+ em_capt「身份核实」+ 验证 cookie 有请求预算（约140次/会话） | L3 过一次核实拿 wsc_checkuser_ok cookie → 浏览器导航 + embedded_json 直取（配方 R15）；忌 HTTP 批量（会连坐封浏览器会话） |
| Product Hunt | Cloudflare Turnstile 拦 headless + Next.js 客户端渲染 + 无限滚动；连接重置=风控 | 调试 Chrome 过一次校验 → cdp 附加 → row_css 卡片 + scroll_count 滚动 + detail.extract limit 抓 makers（配方 R16） |
| B站 API | 直接调接口返回 -352 风控码 | 先 GET 一次 www.bilibili.com 主页拿 buvid cookie，把 cookie **串**填进 `anti_bot.cookies`，并**固定 UA**（`"rotate_ua": false`，与预热时一致——UA 漂移会再触发 -352）；带 Referer 调 api.bilibili.com 公开接口（每周必看 `popular/series/one?number=期号`、热门 `popular` 均有现成端点） |

**两条先查表再动手的经验**（能省一个数量级的功夫）：

1. **"某天公布的全部 X"** → 先找官方公报/Gazette/Bulletin（EUIPO Trade Marks
   Bulletin 周刊、商标公告、招标公告）。公报是官方设计给人按期浏览的入口，
   别逐个实体硬查（配方 R14）。
2. **"某实体的程序/状态记录"**（商标异议、案件进展）→ 官方检索页多半是 SPA，
   第一手动作是 capture_all 找详情接口，而不是渲染 UI（配方 R13）。

## 六、0 结果诊断报告（固定结构）

```
⚠️ 这次没抓到数据，原因和方案如下：
· 现象：<一句话，如"所有请求都返回同一段 1378 字节的保护壳页面">
· 已尝试：HTTP 直抓 → 指纹伪装 → 无头浏览器（共 3 级）
· 证据：recon.md / last_page.html 已存在 <任务目录>/，你可以亲自打开看
· 判定：<类型>
· 方案：<下一级动作，或 L5 人工通道指引>
```

要求：诊断前先把证据文件给用户留好（失败轮的 `last_page.html`、
`capture_all.json` 天然保留在任务目录）；禁止只说"失败了"三个字。
