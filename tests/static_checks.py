#!/usr/bin/env python3
"""静态缺陷检查（AST 级；收官十五轮新增，CI 自检调用）。

覆盖三类"机器可判"的严重缺陷：
1. **同锁嵌套获取**（自死锁）：`with self._lock:` 直接或经由同函数内嵌再进同一属性锁。
   threading.Lock 不可重入——命中即死锁。这是 v2.0.17 真实踩过的坑（条目写入即挂死）。
2. **裸 except**：`except:`（不带类型）会吞 KeyboardInterrupt/SystemExit——禁止。
3. **可变默认参数**：`def f(x=[])` / `={}` / `=set()`——跨调用共享状态的经典来源。

用法: python3 tests/static_checks.py [路径...]   （默认扫 universal_scraper/）
退出码非 0 = 有失败项。
"""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAIL = []
WARN = []


def _with_lock_attrs(node: ast.With) -> set:
    """返回该 with 语句直接获取的 self.<attr> 集合（形如 with self._lock:）。"""
    out = set()
    for item in node.items:
        ctx = item.context_expr
        if (isinstance(ctx, ast.Attribute) and isinstance(ctx.value, ast.Name)
                and ctx.value.id == "self"):
            out.add(ctx.attr)
    return out


class LockNestChecker(ast.NodeVisitor):
    """跟踪同一函数体内 with self.<attr> 的嵌套栈。"""

    def __init__(self, path: Path):
        self.path = path
        self.stack = []          # 当前嵌套中的锁名
        self.hits = []

    def _scan_block(self, body, active: set):
        for stmt in body:
            self.visit_stmt(stmt, active)

    def visit_stmt(self, stmt, active: set):
        if isinstance(stmt, ast.With):
            got = _with_lock_attrs(stmt)
            dup = got & active
            for name in dup:
                self.hits.append((getattr(stmt, "lineno", 0), name))
            self._scan_block(stmt.body, active | got)
            return
        if isinstance(stmt, (ast.For, ast.While, ast.If, ast.Try)):
            self._scan_block(getattr(stmt, "body", []), active)
            for attr in ("orelse", "finalbody", "handlers"):
                sub = getattr(stmt, attr, None)
                if isinstance(sub, list):
                    for h in sub:
                        if isinstance(h, ast.ExceptHandler):
                            self._scan_block(h.body, active)
                        else:
                            self.visit_stmt(h, active)
            return
        # 其余语句内部可能含函数定义/lambda（新作用域，锁栈重置）
        for child in ast.iter_child_nodes(stmt):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._scan_block(child.body, set())
            else:
                self.visit_stmt(child, active) if isinstance(
                    child, (ast.With, ast.For, ast.While, ast.If, ast.Try)) else None

    def check(self, tree: ast.AST):
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._scan_block(node.body, set())


def check_locks(path: Path, tree: ast.AST):
    c = LockNestChecker(path)
    c.check(tree)
    for lineno, name in c.hits:
        FAIL.append(f"{path}:{lineno}: 同锁嵌套获取（自死锁风险）: with self.{name} 已在 "
                    f"with self.{name} 内")


def check_bare_except(path: Path, tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.type is None:
            WARN.append(f"{path}:{node.lineno}: 裸 except（会吞 KeyboardInterrupt/SystemExit）")


def check_mutable_defaults(path: Path, tree: ast.AST):
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for d in list(node.args.defaults) + [x for x in node.args.kw_defaults if x]:
            bad = (isinstance(d, (ast.List, ast.Dict, ast.Set))
                   or (isinstance(d, ast.Call) and isinstance(d.func, ast.Name)
                       and d.func.id in ("list", "dict", "set")))
            if bad:
                WARN.append(f"{path}:{node.lineno}: 可变默认参数 in {node.name}()")


def main(argv):
    targets = [Path(a) for a in argv] or [ROOT / "universal_scraper"]
    files = []
    for t in targets:
        files.extend(sorted(t.rglob("*.py")) if t.is_dir() else [t])
    n = 0
    for f in files:
        if "__pycache__" in str(f):
            continue
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError as e:
            FAIL.append(f"{f}: 语法错误 {e}")
            continue
        check_locks(f, tree)
        check_bare_except(f, tree)
        check_mutable_defaults(f, tree)
        n += 1
    print(f"== 静态检查（{n} 文件）==")
    for w in WARN[:20]:
        print(f"  ⚠️  {w}")
    if len(WARN) > 20:
        print(f"  …（共 {len(WARN)} 条警告）")
    for x in FAIL:
        print(f"  ❌ {x}")
    print(f"失败 {len(FAIL)} 项 / 警告 {len(WARN)} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
