#!/usr/bin/env python3
"""任务包脚手架：生成标准任务目录（配置 + 可改模块模板）。"""
from __future__ import annotations

import json
from pathlib import Path

TASK_CONFIG_TEMPLATE = {
    "name": "__NAME__",
    "vars": {},
    "start_urls": ["https://example.com/list"],
    "queue": {"max_depth": 5, "max_requests": 500, "max_concurrency": 4},
    "rules": [
        {"match": "regex", "pattern": "/list", "parser": "list", "follow": True},
        {"match": "regex", "pattern": "/detail/\\d+", "parser": "detail", "follow": False}
    ],
    "parsers": {
        "list": {
            "type": "html",
            "row_css": "tr.item",
            "fields": {"title": {"css": "td.t"}, "url": {"css": "a", "attr": "href"}},
            "extract_links": {"allow": "/detail/\\d+", "deny": None}
        },
        "detail": {
            "type": "html",
            "fields": {"body": {"css": ".content", "limit": 5000}}
        }
    },
    "pipelines": [
        {"type": "filter", "field": "title", "op": "non_empty"},
        {"type": "dedup", "key": "url"}
    ],
    "storage": {"type": "jsonl", "name": "__NAME__"},
    "output": {"dir": "outputs", "base_name": "__NAME__"},
    "anti_bot": {"min_interval": 1.0, "max_retries": 3, "http_backend": "requests"}
}

PARSER_TEMPLATE = '''#!/usr/bin/env python3
"""任务自定义解析器（按需修改这个文件即可适配任务）。

只需要实现 parse()：输入 Response，输出 ParseResult(items, requests)。
- items: 提取到的数据（交给 pipeline 和 storage）
- requests: 需要继续抓的链接（交给 RequestQueue 递归）
"""
import re
from universal_scraper.protocols import BaseParser, ParseResult, ParseContext, Response, Request


class Parser(BaseParser):
    name = "detail"  # 对应 config.json 里 rules[].parser 的名字

    def parse(self, resp: Response, ctx: ParseContext) -> ParseResult:
        # 示例：从 HTML 提取标题和正文
        title = ""
        m = re.search(r"<h1[^>]*>(.*?)</h1>", resp.text, re.S)
        if m:
            title = re.sub(r"<[^>]+>", "", m.group(1)).strip()

        # 返回数据条目
        return ParseResult(
            items=[{"title": title, "url": resp.url}],
            requests=[],
        )
'''

TASK_README = '''# {name} 任务包

## 结构
- `config.json`：声明式配置（入口/规则/解析器/流水线/存储/反爬）
- `modules/parser.py`：**自定义解析器（按任务改这个）**

## 适配新任务的步骤（只需改一个模块）
1. 改 `config.json`：`start_urls`（入口）、`rules`（URL→解析器）、`parsers`（选择器）
2. 复杂解析：写 `modules/parser.py`（继承 BaseParser）
3. 特殊取数：写 `modules/fetcher.py`；特殊存储：写 `modules/storage.py`

## 运行
```bash
python3 -m universal_scraper.cli run --task {path}
```
'''


def scaffold_task(name: str, out: Path) -> Path:
    # OCR R131（H）：`suffix == ""` 曾把带点目录名（v1.0/my.project）误判成文件
    # 路径——scaffold 建出错误嵌套。目录存在按目录；仅已知配置后缀按文件。
    # 审查二轮（M）：out 是已存在的普通文件（非配置后缀）时 root=out，
    # 下方 root/"config.json" 抛 NotADirectoryError——按目录的父级兜底
    if out.is_file():
        root = out.parent / out.stem
    else:
        root = out if (out.is_dir() or out.suffix not in (".json", ".md", ".txt")) else out.parent / out.stem
    if (root / "config.json").exists():
        # OCR R131（M）：已存在任务曾被静默整目录覆盖
        raise FileExistsError(f"任务已存在，拒绝覆盖: {root}/config.json（换名字或先删除）")
    # 审查修复：只查 config.json 曾漏掉 parser.py/README.md——out 指向一个恰好
    # 有 README.md 的普通目录时，模板会静默覆盖用户文件。写入前全量检查
    for _t in ("modules/parser.py", "README.md"):
        if (root / _t).exists():
            raise FileExistsError(f"拒绝覆盖已存在文件: {root}/{_t}（换名字或先删除）")
    (root / "modules").mkdir(parents=True, exist_ok=True)
    cfg = json.loads(json.dumps(TASK_CONFIG_TEMPLATE))
    cfg["name"] = name
    cfg["storage"]["name"] = name
    cfg["output"]["base_name"] = name
    (root / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (root / "modules" / "parser.py").write_text(PARSER_TEMPLATE, encoding="utf-8")
    (root / "README.md").write_text(TASK_README.format(name=name, path=root), encoding="utf-8")
    return root
