#!/usr/bin/env python3
"""文档-代码一致性检查（收官十五轮新增）。

用户的 KLEKT 复盘暴露过"文档承诺 ≠ 实现"的漂移（pipeline 顺序陷阱、batch init 之类
的想象命令）。本检查把"文档里写的接口"与"代码里真实存在的接口"对账：

1. 文档提到的 CLI 子命令必须真实存在（SKILL.md / agent-quickref.md / spec-schema.md
   里反引号包住的 `us <cmd>` / `cli <cmd>` 形态）
2. 文档提到的配置键必须在契约表白名单里（contract.py 的键集合 + 已知顶层键）
3. 诊断/方案的处方里引用的命令必须存在（solutions.py 的 PRESCRIPTIONS 文本）

用法: python3 tests/docs_consistency.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(("  ✅ " if cond else "  ❌ ") + name + (f" | {detail}" if detail and not cond else ""))


def cli_commands() -> set:
    """从 cli.py 的 argparse 里抽取全部子命令名。"""
    import io
    import contextlib
    from universal_scraper import cli as cli_mod
    buf = io.StringIO()
    old = sys.argv
    sys.argv = ["universal-scraper", "--help"]
    try:
        with contextlib.redirect_stdout(buf):
            try:
                cli_mod.main()
            except SystemExit:
                pass
    finally:
        sys.argv = old
    help_text = buf.getvalue()
    m = re.search(r"\{([a-z0-9_,\-]+)\}", help_text)
    return set((m.group(1).split(",")) if m else [])


def documented_commands(texts: dict) -> dict:
    """文档里出现的 `us <cmd>` / `cli <cmd>` / `python3 -m universal_scraper.cli <cmd>`。"""
    found = {}
    pat = re.compile(r"(?:\bus\s+|\bcli\s+|universal_scraper\.cli\s+)([a-z][a-z0-9\-]{1,20})\b")
    for name, text in texts.items():
        for m in pat.finditer(text):
            cmd = m.group(1)
            found.setdefault(cmd.upper() if cmd.isupper() else cmd, set()).add(name)
    return found


def main():
    print("== 1) 文档提到的 CLI 子命令必须存在 ==")
    cmds = cli_commands()
    check("抽取到子命令表", len(cmds) > 20, f"n={len(cmds)}")
    docs = {}
    for p in (ROOT / "references").glob("*.md"):
        docs[p.name] = p.read_text(encoding="utf-8", errors="ignore")
    docs["SKILL.md"] = (ROOT / "SKILL.md").read_text(encoding="utf-8", errors="ignore") \
        if (ROOT / "SKILL.md").exists() else ""
    doc_cmds = documented_commands(docs)
    # 文档中出现的非命令词（自然语言/参数名）白名单
    ignore = {"the", "a", "an", "and", "or", "to", "for", "in", "on", "of", "it", "is",
              "you", "we", "this", "that", "run", "test", "help", "url", "dir", "file",
              "out", "json", "all", "now", "here", "add", "use", "see", "cfg", "path",
              "name", "task", "config", "python3", "pip", "git", "cd", "ls", "cat",
              "grep", "node", "npm", "curl", "mkdir", "rm", "cp", "mv", "echo", "export",
              "install", "show", "list", "done", "fail", "retry", "status", "next",
              "claim", "touch", "nodata", "blocked", "batch", "xxx", "xxx.py"}
    unknown = {c: sorted(v) for c, v in doc_cmds.items()
               if c not in cmds and c.lower() not in ignore and not c.isupper()}
    check("文档无幽灵命令", not unknown, f"未实现: {unknown}")

    print("== 2) 文档声明的类型/顶层键必须被契约接受 ==")
    from universal_scraper import contract
    allowed = set()
    for attr in dir(contract):
        if attr.isupper():
            v = getattr(contract, attr)
            if isinstance(v, (set, frozenset, tuple, list)):
                allowed |= {str(x) for x in v}
            elif isinstance(v, dict):
                allowed |= {str(x) for x in v}
    # 权威常量：从 config.py 的 *_TYPES / *_STRATEGIES 集合装配
    from universal_scraper import config as cfg_mod
    for attr in dir(cfg_mod):
        if attr.isupper() and attr.endswith(("_TYPES", "_STRATEGIES")):
            v = getattr(cfg_mod, attr)
            if isinstance(v, (set, frozenset, tuple, list)):
                allowed |= {str(x) for x in v}
    # 非"类型"语义的合法取值（动作步骤/字段格式/pagination 子键等）
    allowed |= {"prefix", "suffix", "none", "auto", "text", "attr", "html", "markdown",
                "article", "table", "json", "template", "selector", "click", "wait",
                "scroll", "screenshot", "type", "write", "fill", "press", "hover",
                "go_back", "reload", "eval", "cookie", "header", "delay", "goto",
                "value", "check", "uncheck", "select_option", "upload"}
    known_top = {"name", "type", "description", "vars", "source", "pagination", "pipeline",
                 "pipelines", "detail", "record", "parsers", "rules", "storage", "output",
                 "iterate", "anti_bot", "incremental", "cookies", "middleware", "schedule",
                 "start_urls", "verify", "login", "notify", "webhook", "retry", "limits",
                 "queue", "vars_file", "tags", "notes", "version", "preset", "extends"}

    # 2a) 所有 json 块里的 "type": "<x>" 必须落在某个已知类型表里
    bad_types = {}
    for name, text in docs.items():
        for block in re.findall(r"```json\n(.*?)```", text, re.S):
            for m in re.finditer(r'"type"\s*:\s*"([a-z_][\w\-]*)"', block):
                t = m.group(1)
                if t not in allowed:
                    bad_types.setdefault(t, set()).add(name)
    check("文档声明的类型均被契约接受", not bad_types,
          f"未识别类型: { {k: sorted(v) for k, v in bad_types.items()} }")

    # 2b) 整配置示例（同时含 name 与 source）的顶层键必须合法
    bad_top = {}
    for name, text in docs.items():
        for block in re.findall(r"```json\n(.*?)```", text, re.S):
            if '"name"' not in block or '"source"' not in block:
                continue
            try:
                import json as _json
                cfg = _json.loads(block)
            except Exception:
                continue  # 截断示例/含注释，跳过
            if not isinstance(cfg, dict):
                continue
            for k in cfg:
                if k not in known_top and k not in allowed:
                    bad_top.setdefault(k, set()).add(name)
    check("整配置示例顶层键合法", not bad_top,
          f"未识别顶层键: { {k: sorted(v) for k, v in bad_top.items()} }")

    # 2c) 文档声明的 CLI 参数抽样核对（对 3 个高频命令逐个对 help 文本）
    print("== 2c) 关键词条抽样核对 ==")
    import contextlib
    import io as _io
    from universal_scraper import cli as cli_mod

    def _helptext(*argv) -> str:
        buf = _io.StringIO()
        old = sys.argv
        sys.argv = ["universal-scraper", *argv, "--help"]
        try:
            with contextlib.redirect_stdout(buf):
                try:
                    cli_mod.main()
                except SystemExit:
                    pass
        finally:
            sys.argv = old
        return buf.getvalue()

    for cmd, must in (("run", "--config"), ("run", "--task"), ("run", "--resume"),
                      ("fetch", "--browser"), ("fetch", "--out"), ("diagnose", "--out"),
                      ("verify", "--dir"), ("report", "--group")):
        ht = _helptext(cmd)
        check(f"`us {cmd} {must}` 存在于 help", must in ht, f"help 缺 {must}: {ht[:120]!r}")

    print("== 3) 方案库处方引用的命令必须存在 ==")
    from universal_scraper import solutions as sol
    src = Path(sol.__file__).read_text(encoding="utf-8")
    presc_cmds = set(re.findall(r"`?(?:us|cli)\s+([a-z][a-z0-9\-]{2,20})", src))
    ghost = {c for c in presc_cmds if c not in cmds and c not in ignore}
    check("处方无幽灵命令", not ghost, f"未实现: {sorted(ghost)}")

    bad = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n文档一致性 {len(RESULTS)} 项，失败 {len(bad)}" + (f": {bad}" if bad else "  ★ 全部通过"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
