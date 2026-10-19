# LearSpider（爬虫百战成神）全自动刷题

## 背景与诚实说明
- [LearnSpider](https://github.com/cpython666/LearnSpider) 是开源爬虫刷题靶场（Django+DRF），
  作者线上站（stardream.vip / learnspider.vip）**已下线**（2026-08-07 探测均不可达）。
- 仓库最新数据 `learn_spider-2025-02-27.sql` 含 **57 道题**：
  - **27 道**：有页面 + 官方答案，可完整运行与验证
  - **22 道**：response_path 对应页面模板缺失（仅线上版有）
  - **8 道**：无 URL（序言/公网跳转类）
- 因此**不存在可获取的"100 题"完整题库**；本工具对仓库中所有可运行的题目全部通过。

## 结果
| 指标 | 数值 |
|---|---|
| 可运行且有官方答案 | 27 |
| **全部通过（官方校验）** | **27/27 = 100%** |
| 页面缺失 / 无 URL | 30（数据源本身缺失） |

覆盖题型：请求/编码/UA、表格求和与键值表、跨行表格、分页（数字与中文页码、翻页表格）、
动态网页(Fetch/XHR)、雪碧图、SVG、JSON 解析、重定向状态码、cookie 链、问卷、知识题等。

## 本地部署（开箱即用）
工具包内已含 `challenges/learnspider/`（LearnSpider 源码，Django+DRF 已装进工具 vendor）：
```bash
cd challenges/learnspider
export PYTHONPATH=<工具根>/vendor
python3 manage.py migrate          # 已含 db.sqlite3（题库已导入）
python3 manage.py runserver 127.0.0.1:8001 --noreload
```

## 全自动刷题
```bash
python3 tests/learnspider_solver.py              # 全部 57 题
python3 tests/learnspider_solver.py 46 49 100    # 指定题目
```
流程：启动 Django → 逐题 HTTP/浏览器侦察 → 规则库(计数/求和/请回答/翻页/接口次数/重定向状态码…)
→ LLM(千问)自动求解（失败自动带错误反馈重试）→ 提交 `/api/check-answer/` 官方校验 → 报告
`challenges/learnspider_results.json`。

## 机制
- 答题判定：POST `/api/check-answer/` `{question_title, answer}`，服务器按页面 title 匹配题库比对。
- 刷题器自动：读 sqlite 题库 → 构造 URL → 侦察（urllib + browser_agent）→ 求解 → 校验。
