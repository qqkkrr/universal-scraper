# Agent Quick Reference · universal-scraper

> 给自主 AI agent 的单页速查。人类小白请回读 SKILL.md（面向小白的引导教程）。

## 命令模板

```bash
PYTHONPATH="${SKILL_DIR}" python3 -m universal_scraper.cli <cmd> [args]
```

## 判型 → 命令速查

| 目标页特征 | 命令 | 备注 |
|---|---|---|
| 静态 HTML，数据在源码里 | `fetch <url>` | 出 markdown，`--json` 出信封结构 |
| JS 渲染壳 | `fetch <url> --browser` | playwright 桥接，CDP 降级 |
| JSON API（已知端点） | `run --config`（http_json） | 或直接 curl_cffi |
| 接口未知（SPA） | `fetch <url> --browser` + capture_all | 找 POST 体→改 http_json |
| 列表+详情 | `run --config`（browser + detail） | 详情字段清洗放 `detail.post_pipeline`（`pipeline` 跑在详情**之前**） |
| **快手评论** | 浏览器内 `fetch('/graphql')` + `operationName: commentListQuery` + 只用 `pcursorV2` 翻页；直连必 `Need captcha`。定位账号用 `/rest/v/search/user|feed`。配方 R47 |
| **猫眼短评+评分** | 直连 `m.maoyan.com/apollo/apolloapi/review/v2/comments.json` | 无签名无登录，limit≤20 + ts 游标；评分读移动端 SSR 明文字段（绕开字体反爬）。配方 R46 |
| **GraphQL 站冷启动** | introspection 被禁时靠**校验错误反推 schema**（`Did you mean` / `Cannot query field`）；`operationName` 是白名单须抄页面的；游标 V1/V2 **不能混用**。配方 R48（不限平台） |
| **评论任务读法** | 评论类任务**先读 R45 通用骨架**（热度口径/游标翻页/展开回复/请求量估算），再查站配方：R46 猫眼 / R40 小红书 / R41 知乎 / **R47 快手** |
| PDF 下载 | `pdf --download 清单.json --out 目录` | %PDF 校验+断点 |
| PDF 表格 | `pdf --tables x.pdf` | pdfplumber |
| 批量队列 | `batch --queue q.json next/claim/done/fail/nodata/retry/status` | |
| 有 curl 要复现 | `curl2config --cmd "curl '…'"` | 只解析不执行；凭据/不支持项会提醒；产物先 `--dry-run` |
| **调规则不重打站点** | `run --config x.json --replay` | R20：只读本地响应缓存、**绝不发网络**；未命中=结构化失败（先正常跑一次生成缓存） |
| 站点改版选择器失效 | 自动（无需操作） | R20：行/字段级**元素指纹找回**（只在 0 行/空值时触发，找回值须过非空验证门；`source.adaptive:false` 可关） |
| 长跑想按域自适应限速 | `anti_bot.autothrottle: {"cap": 30}` | 分域桶（A 站被封不拖慢 B 站）；`Retry-After` 计入惩罚；robots Crawl-delay 作下限 |
| 反检测细粒度 | `anti_bot.stealth_opts: {...}` | `hide_canvas/block_webrtc/allow_webgl/dns_over_https/timezone/locale/block_ads/blocked_domains/extra_flags` |
| Agent 一次抓多个 URL | MCP `bulk_scrape{urls:[…]}`（≤30） | 逐条隔离失败 + 间隔；`screenshot{url}` 落盘截图 |
| **接口看不懂形态** | playbook **第九章**（GraphQL/protobuf-gRPC/WebSocket） | 乱码=protobuf 明文非加密；`wss://` 帧流用 `browser_agent`（内置帧采集）；GraphQL=R48 |
| **先查表再动手** | 配方表 + 第九章 + `sites` 端点登录标注 | 配方写"已下架"的接口不要实测确认；写"免登录"的不要先登录（R21 铁律 №6） |
| 页面内容进 LLM 前 | 自动净化 | R20：隐藏元素/注释/零宽字符剥离 + 主内容收窄（`mcp extract` 默认净化，`raw:true` 放行） |

## 失败 → 处置

| 症状 | 判定 | 动作 |
|---|---|---|
| 0 条 + 页面 200 | JS 壳 | → fetch --browser |
| fetch 正文极短但原文 HTML 很大且含内联数据 | SSR/内联 JSON（**非壳页**） | → `fetch --raw --out p.html` 看结构；按 `record.fields` 写配置，**别升浏览器** |
| 0 条 + 页面 403/421 | IP 封/配额 | → budget 已自动记；换 IP 或冷却 |
| 0 条 + 空数据 | nodata | → WebSearch 核验 → batch nodata |
| 连续 3 次网络失败 | 出口变化 | → doctor.py 复查 |
| 字段完整率 < 0.9 | 部分成功 | → verify --dir 看细节 |

## fetch --json 输出契约

```json
{"url": "...", "status": 200, "text": "<页面markdown，内嵌JSON则为转义字符串>"}
```

text 内含 JSON 时先找到 JSON 起始位置再解析，不要把信封当数据。

## capture_all.json 输出契约

```json
[{"url": "...", "method": "POST", "post_data": "...", "request_headers": {...}, "json": {...}}]
```

## 运行时行为

- HTTP 客户端默认 curl_cffi + chrome 指纹 + verify=True（TLS 证书校验，anti.verify=false 可关）+ 3 次重试
- 403/421/52x 自动记入域名封锁台账（budget --list 查）
- 连续 3 次网络失败自动提示 doctor 复查
- strategy=none + 空 records_path → 自动识别常见键(records/items/list/results/data)
- source.single_record=true → 整响应体作为一条记录（GraphQL 类）

## 配额管理

- `budget --mark 域名 --hours 24`：手动记账
- `budget --check 域名`：查冷却状态（exit 0=可访问, 2=冷却中）
- `budget --list`：全台账

## 自写采集代码的安全编码口径（Mimosa 钩子预适配）

本环境的安全钩子会拦截以下写法——**写采集代码时直接用右侧口径**，别照搬网上教程：
- 写文件：用 `pathlib` + `write_text()`，不用 `open(f"{var}.txt", "w")`（f-string 路径拼接触发拦截）
- 登录态/Cookie 落盘：`os.open(..., 0o600)` + `os.chmod(path, 0o600)`（0644 = 凭据泄露）
- 出站请求：仅 http/https 且 host 走白名单/已知域；拒绝 localhost/私网/保留地址
- 哈希用途非安全场景（缓存键/临时名）：`hashlib.sha256`（md5 会被告警）

## 知识引用

- 反爬升级阶梯 / 配额四分类 / 三分叉 → `references/anti-block-playbook.md`
- 配方 R1~R42（R34=B站四通道战法；R35-R42：汽车之家/12306/双色球/统计局/存档替代/小红书/知乎/第三方每日存档仓库）→ `references/recipes.md`
- 配置字段 → `references/spec-schema.md`
- 交易所索引 → `references/data-sources-exchanges.md`
- 证据 schema → `references/evidence-schema.md`
