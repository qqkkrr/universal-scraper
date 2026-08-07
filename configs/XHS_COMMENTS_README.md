# 小红书评论区爬取任务（高难度）

## 为什么难（GitHub 大佬研究结论）
- 小红书 Web 端 API 全加密：`x-s`/`x-t`/`x-s-common` 签名，且与 Cookie、Canvas 指纹**强绑定**
- 纯逆向重算签名（submato/xhscrawl、XHS_RS_TOOLS 等方案）会被风控拦截——服务端校验签名生成环境
- **正解（MediaCrawler 60k★ 同款思路）**：登录态浏览器 + 让 SPA 自己调自己的签名接口，我们只拦截响应

## 本任务原理
1. **登录（Level 4 人机结合）**：有头浏览器打开小红书 → 你扫码/手机号登录 → 会话持久化到 `outputs/.session/xhs_comments.json`（之后免登录）
2. **网络捕获**：浏览器打开笔记页 → 自动滚动触发懒加载 → 拦截 `/api/sns/web/v2/comment/page`（主评论）和 `/comment/sub/page`（子评论）的 JSON 响应
3. **提取导出**：comments 字段映射 → 去重 → JSON/CSV/XLSX

## 使用步骤
```bash
# 1) 把笔记 ID 填进配置（或命令行覆盖）
python3 -m universal_scraper.cli run --config configs/xhs_comments.json \
  --var note_id=64xxxxxxxxxxxxxxxxxxxx

# 2) 第一次运行：弹出浏览器，手动登录后回车等待（会话自动保存）
# 3) 之后运行：复用会话，直接抓
python3 -m universal_scraper.cli run --config configs/xhs_comments.json \
  --var note_id=64xxxxxxxxxxxxxxxxxxxx
```

## 合规声明（重要）
- 仅限**你自己的账号**、公开笔记、个人研究/存档用途
- 小红书服务条款禁止未授权爬取；批量高频抓取可能导致**账号风控/封禁**，风险自负
- 数据量控制：本任务单笔记评论，通常 <10MB，符合 ≤100MB 要求；请勿扩大范围
- 本仓库不提供签名破解代码；本方案依赖你自己的真实登录态，属于"浏览器自动化+人工登录"路线
