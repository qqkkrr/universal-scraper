# 配置文件参考（task.json，v2 格式，`run --config` 使用）

> 你（agent）负责写这个文件，用户永远不需要看它。
> 拿不准就先 `python3 -m universal_scraper.cli scaffold --type <类型> --name <任务名> --out <路径>` 生成模板再改。
> 四种内建模板：`http_json` / `http_html` / `browser` / `browser_script`。

## 顶层结构

```json
{
  "name": "任务名",
  "vars": { "keyword": "关键词" },
  "source":   { "...": "按类型见下" },
  "pagination": { "...": "翻页策略" },
  "record":   { "fields": { "输出列名": {"from": "来源字段"} } },
  "detail":   { "enabled": false },
  "pipeline": [],
  "output":   { "dir": "", "base_name": "", "formats": ["json","csv","xlsx"] },
  "anti_bot": { "min_interval": 1.0, "max_retries": 3, "http_backend": "requests" }
}
```

`vars` 里的变量用 `{{keyword}}` 语法在 URL/参数里引用。

## source 四种类型

**http_html** — 页面里直接有数据（L0/L1）：

```json
{
  "type": "http_html",
  "url": "https://example.com/list",
  "row_css": "div.item",
  "fields": {
    "标题": {"css": "h3 a"},
    "链接": {"css": "h3 a", "attr": "href"},
    "价格": {"css": "span.price"},
    "xpath取值": {"xpath": "//td[2]"}
  }
}
```

URL 里可以用 `vars` 定义的变量（如 `?q={{keyword}}`）；**页码不要写进 URL**——
翻页由 pagination 策略负责（查询参数自动追加，或跟随"下一页"链接）。

**内嵌 JSON 页面**（SSR 把数据放在 `<script>` 里，如股吧的 `window.article_list`）：
不用 CSS 选择器，直接声明变量引用即可拿到结构化记录（module 作用域的
`const/let/var X` 也能识别）：

```json
"embedded_json": "window.article_list"
```

或变量值是字典时用 `path` 下钻：`{"var": "window.g_data", "path": "list"}`。
只支持 JSON 兼容字面量（容忍尾逗号）；提取到的每条记录走 `record.fields` 映射。

**http_json** — 接口返回 JSON：

```json
{
  "type": "http_json",
  "method": "GET",
  "url": "https://api.example.com/search?q={{keyword}}&page={{page}}",
  "headers": {"Referer": "https://example.com"}
}
```

⚠️ **必须**在 pagination 里写 `records_path`（JSON 里列表数据所在的点路径，如
`"data.list"`），否则抓到响应也提不出记录（page +0）。`record.fields` 的 `from`
支持 jpath 点路径取嵌套值：`{"UP主": {"from": "owner.name"}}`。

**browser** — JS 渲染/交互（L2/L3）：

```json
{
  "type": "browser",
  "url": "https://example.com",
  "pool": true,
  "stealth": true,
  "remove_overlays": true,
  "wait": {"selector": "#list", "timeout": 20000},
  "row_css": "li.item",
  "fields": {"标题": {"css": "span.t"}},
  "pagination": {"type": "click", "selector": "a.next", "wait_ms": 1500},
  "cdp": "http://127.0.0.1:9222",
  "scroll_count": 5,
  "scroll_wait_ms": 2000,
  "capture": true,
  "record_from": "capture_all",
  "headless": true,
  "login": {"user_css": "#username", "pass_css": "#password", "submit_css": "#go"},
  "slider": {},
  "captcha": {}
}
```

要点：
- `cdp` 存在时附加用户已登录的调试 Chrome（L3），不再新开浏览器。
- `scroll_count`/`scroll_wait_ms`：滚动加载型列表。
- `capture` + `record_from`: `"capture"`（按声明模式）或 `"capture_all"`（录下全部 JSON
  响应，每条记录 `{_api_url, data}`，事后由你挑字段）——数据藏在接口里时用。
- `headless: false` + 人工关卡：验证码/滑块由用户手动过，`login_timeout_ms` 内等他完成。

**browser_script** — 专用桥接脚本（财新/招投标/淘宝等，`scripts/*.cjs`）：

```json
{
  "type": "browser_script",
  "bridge": "${SKILL_DIR}/scripts/ggzy_bridge.cjs",
  "bridge_params": {"keyword": "{{keyword}}"}
}
```

## pagination（外层，http 类）

四种合法策略：`page_param` / `offset` / `next_url` / `none`。

```json
{"strategy": "page_param", "page_param": "page", "start": 1, "max_pages": 10,
 "total_path": "data.total", "records_path": "data.records"}
```

- `page_param`：把页码追加为查询参数（`?page=2`）。`records_path`/`total_path`
  仅 http_json 用（点分隔路径）。
- `start`：**起始页号**（定向分页/历史回溯的关键）。`{"start": 1161, "max_pages": 1200}`
  从第 1161 页抓到第 1200 页。HTTP 路径直接生效；browser 路径通过深链 URL 配合
  （URL 本身指向起始页），桥从该页号开始计数。
- `offset`：`{"strategy":"offset","offset_param":"offset","limit":20,"max_pages":50}`，
  按 `(页-1)*limit` 递增。
- `next_url`：路径式翻页（`/page/2/`）用这个——解析每页"下一页"链接并跟随，
  相对链接自动补全：
  `{"strategy":"next_url","next_selector":"li.next a","max_pages":50}`
  （也可用 `next_xpath`）。
- `none`：只抓一页。
- browser 型翻页写在 `source.pagination`：
  `{"type": "click"|"js"|"none", "selector": "...", "js": "...", "wait_ms": 1500}`，
  最大页数仍读外层 `pagination.max_pages`。

## record.fields

`{"输出列名": {"from": "来源字段名"}}`。来源字段来自 source.fields 的键、JSON 的键
或 embedded_json 的记录键。

## pipeline（记录清洗与过滤——按日期/条件筛选就在这里）

`pipeline` 是记录级处理步骤数组，抓完每页就执行。五种步骤：

```json
[
  {"type": "filter", "field": "post_publish_time", "op": "regex", "pattern": "^2025-09-01"},
  {"type": "filter", "field": "阅读", "op": "between", "min": 1000},
  {"type": "dedup", "key": ["标题", "作者"]},
  {"type": "rename", "mapping": {"cmt": "评论数"}},
  {"type": "cast", "field": "阅读", "to": "int"},
  {"type": "add", "field": "来源", "value": "guba"}
]
```

- `filter` 五种操作符：`contains`（默认）/ `eq` / `regex`（`pattern`）/ `not_contains` /
  `between`（数值区间，`min`/`max`，自动去千分位逗号）。**日期筛选用 regex 前缀匹配
  即可**（`^2025-09-01` 匹配当天全部时间戳）。
- `dedup`：按 `key`（单字段或字段数组）去重。
- `rename`：`mapping` 字典批量改列名；`cast`：`to` 取 int/float/str（自动去千分位）；
  `add`：`field`+`value` 加常量列。
- `transform`：字段变换，`op` 取 `unix_to_datetime`（秒/毫秒时间戳自适应转
  `fmt` 格式，默认 `%Y-%m-%d %H:%M:%S`）/ `upper` / `lower`：
  `{"type":"transform","field":"pubdate","op":"unix_to_datetime"}`。
- `template`：用已有字段拼新字段，`tmpl` 里 `{字段名}` 占位：
  `{"type":"template","field":"视频链接","tmpl":"https://www.bilibili.com/video/{bvid}"}`。
- **仅 v3 任务包（`run --task`）支持的类型**：`parse_date`、`split`、`default`、
  `download`、`validate`、`dedup_content`——这些在 `run --config` 执行器中未实现，
  validate 会给出警告且运行时跳过。词表唯一来源见 `universal_scraper/contract.py`。

## detail（列表 → 详情两级）

```json
{
  "enabled": true,
  "url_field": "url",
  "extract": [{"name": "正文", "css": "div.content"}],
  "max_pages": 0,
  "concurrency": 2,
  "interval": 0.5,
  "url_transform": [{"type": "prefix", "value": "https://example.com"}],
  "filters": []
}
```

相对链接用 `url_transform` 补前缀；`max_pages: 0` 表示全部。

`extract` 每项支持五种 `type`（配 `limit` 可抓列表字段，多条换行连接）：

```json
[
  {"name": "正文",   "type": "css_text",  "selector": "div.article"},
  {"name": "makers", "type": "css_attr",  "selector": "a[href^='/@']", "attr": "href"},
  {"name": "页面链接", "type": "css_html", "selector": "div.meta"},
  {"name": "标题",   "type": "xpath_text", "xpath": "//h1"},
  {"name": "发布时间", "type": "json",     "path": "meta.publish_time"}
]
```

`limit: 0`（默认）表示全部匹配；注意选择器口径——列表选择器太宽会把无关元素
（如 upvoter/评论者）混进来。

## anti_bot 常用键

| 键 | 作用 |
|---|---|
| `min_interval` | 请求最小间隔秒（礼貌起点 1.0） |
| `max_retries` | 重试次数 |
| `http_backend` | `requests` / `curl_cffi`（指纹伪装时用它） |
| `impersonate` | curl_cffi 伪装目标，如 `chrome` |
| `proxy` / `proxies` / `proxies_file` / `proxy_mode` | 代理 |
| `timeout` | 请求超时秒 |
| `headers` / `rotate_ua` | 请求头 / UA 轮换 |
| `cookies` / `cookie_mode` / `cookie_domain` | Cookie 直抓。**dict 或串均可**：`{"SESSID": "x"}` 或 `"SESSID=x; OTHER=y"`（`cookies` 命令导出的串可直接粘贴） |
| `respect_robots` | 尊重 robots.txt |
| `captcha` / `session_dir` / `session_name` | 验证码与登录态存放 |

## output

`dir` 给绝对路径（用户指定或默认 `~/Desktop/<任务名>/`）；
`formats` 至少 `["json","csv","xlsx"]`。

## 验证与试跑

```bash
PYTHONPATH="${SKILL_DIR}" python3 -m universal_scraper.cli validate --config <路径>
PYTHONPATH="${SKILL_DIR}" python3 -m universal_scraper.cli run --config <路径> --limit 5
```

先 `validate` 再 `--limit 5` 出样本，是第三幕的标准动作。
