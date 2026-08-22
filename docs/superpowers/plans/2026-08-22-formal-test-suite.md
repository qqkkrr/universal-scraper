# 正式测试套件 Implementation Plan

> **For agentic workers:** 按本计划逐任务执行，TDD（先写测试→看红→最小实现→看绿→提交）。

**Goal:** 为万能爬虫工具建立可重复运行的正式 pytest 测试套件，覆盖核心模块 + 关键安全回归。

**Architecture:** 纯单元测试（零网络），本地 fixture（HTML/JSON/配置样本）驱动；生产代码只做必要的 bug 修复（由测试红→绿驱动），不重构。

**Tech Stack:** pytest 9.x + 标准库（unittest.mock / pathlib / subprocess）。

**Spec:** 本计划（依据：用户宗旨「不管是大佬还是小白，天下没有难爬的虫」+ 补测试建议）

## Global Constraints
- 测试必须离线可跑（不访问外网），除标注 integration 的用例
- 不改生产行为；若测试暴露 bug，先写测试确认红，再最小修复
- 测试文件放 tests/test_*.py，用 `sys.path` 指向项目根（与现有 test_report.py 一致）
- 每完成一个文件：跑一次该文件 pytest，绿了再继续

---
## 任务清单
- [ ] T1 基础设施：tests/conftest.py（项目根入 sys.path + 常用 fixture）
- [ ] T2 tests/test_config.py：source.type 白名单/scrapling 放行/未知类型报错/rules 必填
- [ ] T3 tests/test_parsers.py：HTML row_css+fields 解析、JSON records_path+path、字段映射
- [ ] T4 tests/test_pipelines.py：filter(non_empty/contains/between/dedup)
- [ ] T5 tests/test_solutions.py：classify_failure 登录/验证码/0条/404/「10条不算0条」回归
- [ ] T6 tests/test_security.py：reports 路径穿越封堵 / api/report 文件名净化 / diagnose task_dir 限定
- [ ] T7 tests/test_fetchers.py：ScraplingFetcher 未安装降级；HttpFetcher 本地 mock 请求
- [ ] T8 tests/test_auto_guide.py：human_guide/diagnose_failure 规则兜底（无 LLM）
- [ ] T9 tests/test_frontend_js.py：webui/index.html 的 <script> JS 语法可解析
- [ ] T10 全量跑：python3 -m pytest tests/ -q；修到全绿；提交
