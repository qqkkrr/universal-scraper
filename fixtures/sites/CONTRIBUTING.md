# 站点模板贡献指南（fixtures）

为 parse 型精配站点提交模板时，**必须附带离线夹具**——没有夹具的模板无法通过 CI。

## 目录结构

```
fixtures/sites/<站点名>/
├── meta.json    # {"site": "注册名", "url": "命中该站的示例 URL"}
├── page.html    # 真实页面快照（脱敏：删除个人信息/登录态）
└── expect.json  # {"min_rows": 1, "must_have_fields": ["标题"],
                 #  "field_contains": {"标题": "某关键词"}}
```

## 贡献流程

1. **抓快照**：`fetch <url> --out page.html`，人工脱敏后放入上面的目录；
2. **写期望**：`expect.json` 至少声明 `min_rows` 与 `must_have_fields`；
3. **自测**：`python3 scripts/fixture_test.py <站点名>` 必须全绿；
4. **提交**：新站点代码（`sites.py` 的 match/parse）+ 夹具目录一起提交。
   `register(..., run=...)` 型站点（需要打网络的）暂不收夹具——请优先
   贡献 parse 型；确实只能 run 型的，在 meta.json 加 `"run_type": true`
   并说明，由维护者人工验收。

## 判定口径

- 夹具用**真实页面结构**（class/层级照抄），不要简化成玩具 HTML——
  选择器对玩具页通过、对真实页失效是最常见的假绿；
- 快照里不能有登录态/个人数据；
- `expect.json` 的 `field_contains` 用页面上稳定出现的文案，别用易变数字。
