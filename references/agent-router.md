# Agent 路由页（先读这一页，再决定读哪本长文档）

> 目标：让 AI Agent 用最少 token 找到正确动线。详细教程在 SKILL.md，配方在
> recipes.md，反爬在 anti-block-playbook.md；本页只给路由与硬判据。

## 0. 先做合规预检

~~~bash
PYTHONPATH="$SKILL_DIR" python3 -m universal_scraper.cli compliance \
  --config <配置.json> --purpose "<用途>" --scale <预计条数> [--authorized]
~~~

- red：停止（签名逆向/设备指纹/滑块破解/非公开数据/未授权敏感个人信息）。
- amber：可做，但必须最小必要、脱敏、写明目的/合法性基础/保存期限。
- green：公开、非个人数据，按正常限速执行。
- **可跳过**：公开采购公告、公开价格等明显 green 的任务，在开工记录里写明
  “公开非个人数据，合规预检 green/跳过”即可；涉及登录态、个人信息、非公开数据
  时必须跑，不能跳过。

> 工程检查清单，不构成法律意见；最终由用户/法务确认。

## 1. 判型 → 动线

| 目标 | 动线 |
|---|---|
| 静态 HTML / JSON API | run --config（v2 线性）或 fetch |
| JS 壳 / SPA | fetch --browser → snapshot 落盘 → 离线解析 |
| 多页递归 / 详情发现 | run --task（v3 队列） |
| 登录墙 | 用户本人登录 + open-debug-chrome.sh + CDP；不自动化登录/验证码 |
| 强反爬/签名站 | 先查 recipes 的取数边界；落在红线内直接拒单 |
| 长跑/断点 | run --task --resume；run --config --resume 只跳过已完成迭代 |
| 改规则不重打站 | run --config --replay（预生成缓存）或 snapshot |
| 结果复核 | verify --file（支持任意 list[dict]，多列表用 --data-key） |

## 2. 页面快照 + 离线解析（Mode C 推荐）

~~~bash
us snapshot "<url1>" "<url2>" --out outputs/snap_xxx --interval 2
# 产出 page_0001.json（状态/哈希/元数据）+ page_0001.html + page_0001.md
~~~

解析放 Python 侧离线跑：改 5 轮正则、修 3 个字段口径 = 0 额外请求。
需要浏览器渲染时加 --browser。

## 3. 运行环境坑（一次说清）

- Python 后台跑加 -u；Node 没有 -u，用 console.log 直接写文件。
- 任务目录里的自写 Node 脚本解析不到 skill 的依赖：显式设
  NODE_PATH="$SKILL_DIR/node_modules"；doctor 已加解析检查。
- run --engine v4 是默认统一入口；排障用 --engine legacy。

## 4. 登录边界（写死）

允许：用户本人扫码/输入密码完成登录；Agent 只复用登录态读取页面。
不允许：自动填密码、代过验证码/滑块、盗用/买卖 Cookie、批量养号。

## 5. 交付纪律

- 先 3 条样本确认字段 → 再全量；
- 抽样复核 ≥10 条（不足 10 条则全量）；
- 字段级对账 + 算术自检（条数/合计/百分比）；
- 写明采集时间、来源、口径、缺口、未获取字段及原因；
- 个人信息脱敏/聚合，交付层不保留原始昵称+ID+IP 组合。
