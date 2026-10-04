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


# ---------------------------------------------------------------------------
# R20 契约静态检查：配置键/工具注册/开关键三处一致性（副本漂移防线）
# ---------------------------------------------------------------------------

def _read(rel: str) -> str:
    try:
        return (ROOT / rel).read_text(encoding="utf-8")
    except OSError:
        return ""


def check_mcp_tool_registry():
    """MCP：TOOLS（对外 schema）与 TOOL_IMPLS（分发）名字必须一一对应。

    历史上新增工具只改一处会让 tools/list 与调用面分叉（agent 拿到
    未注册工具名 → -32602）。"""
    import re as _re
    src = _read("universal_scraper/mcp_server.py")
    if not src:
        return
    tools_block = src.split("TOOLS: List[Dict[str, Any]] = [", 1)
    impls_block = src.split("TOOL_IMPLS = {", 1)
    if len(tools_block) < 2 or len(impls_block) < 2:
        FAIL.append("mcp_server.py: 找不到 TOOLS/TOOL_IMPLS 定义（结构变更需同步本检查）")
        return
    tools_names = set(_re.findall(r'"name":\s*"([a-z_]+)"', tools_block[1]))
    impl_keys = set(_re.findall(r'"([a-z_]+)":\s*tool_', impls_block[1]))
    if tools_names != impl_keys:
        FAIL.append(f"mcp_server: TOOLS 与 TOOL_IMPLS 不一致："
                    f"仅声明 {sorted(tools_names - impl_keys)}；"
                    f"仅注册 {sorted(impl_keys - tools_names)}")


def check_switch_key_parity():
    """开关键三处一致：config 校验集合 == 各消费端读取的键。

    - autothrottle：config.py（v2/v3 各一处）== core._AUTOTHROTTLE_KEYS
    - stealth_opts：config.py（bool/str/list 三组）== browser_pw 读取键
      == browser_common.cjs parseStealthOpts 读取键"""
    import re as _re
    cfg = _read("universal_scraper/config.py")
    core_src = _read("universal_scraper/core.py")
    pw = _read("universal_scraper/browser_pw.py")
    js = _read("scripts/browser_common.cjs")

    # --- autothrottle ---
    core_keys = set(_re.findall(r'"([a-z_]+)"',
                                core_src.split("_AUTOTHROTTLE_KEYS = {", 1)[1].split("}", 1)[0]))
    for i, seg in enumerate(cfg.split('_at_allowed = {')[1:], 1):
        cfg_keys = set(_re.findall(r'"([a-z_]+)"', seg.split("}", 1)[0]))
        if cfg_keys != core_keys:
            FAIL.append(f"autothrottle 键不一致（config 第 {i} 处 vs core）："
                        f"{sorted(cfg_keys ^ core_keys)}")

    # --- stealth_opts ---
    so_segs = cfg.split("_SO_BOOLS = {")
    if len(so_segs) >= 2:
        cfg_so = set()
        for name, close in (("_SO_BOOLS", "}"), ("_SO_STRS", "}"), ("_SO_LISTS", "}")):
            if f"{name} = {{" in cfg:
                cfg_so |= set(_re.findall(r'"([a-z_]+)"',
                                          cfg.split(f"{name} = {{", 1)[1].split(close, 1)[0]))
        pw_so = set(_re.findall(r'o\.get\("([a-z_]+)"', pw))
        # 提取 parseStealthOpts 函数体：到下一个顶层函数声明为止
        # （曾用 "return out;" 截断——函数内 JSON 守卫就有早返回，段为空导致全部误报）
        if "function parseStealthOpts" in js:
            _body = js.split("function parseStealthOpts", 1)[1].split("\nfunction ", 1)[0]
            js_so = set(_re.findall(r"\bo\.([a-z_]+)\b", _body))
        else:
            js_so = set()
        js_so = {k for k in js_so if k not in ("ctx", "args", "blocked", "blockAds")}
        if cfg_so != pw_so:
            FAIL.append(f"stealth_opts 键不一致（config vs browser_pw）：{sorted(cfg_so ^ pw_so)}")
        if cfg_so != js_so:
            WARN.append(f"stealth_opts 键不一致（config vs JS 桥）：{sorted(cfg_so ^ js_so)}")


def check_config_key_contract():
    """引擎顶层消费键 ⊆ 配置已知键（消费了但校验器不认 = 拼错静默忽略的前置条件）。"""
    import re as _re
    cfg = _read("universal_scraper/config.py")
    known = set()
    if "_KNOWN_KEYS = {" in cfg:
        known |= set(_re.findall(r'"([a-zA-Z_]+)"',
                                 cfg.split("_KNOWN_KEYS = {", 1)[1].split("}", 1)[0]))
    # validate/validate_task 的容器守卫与显式字段名（用引号字面量近似收集）
    for key in ("name", "source", "pagination", "pipeline", "detail", "record", "storage",
                "anti_bot", "output", "queue", "rules", "parsers", "middleware",
                "incremental", "download", "start_urls", "sitemap", "iterate",
                "capture", "base_dir", "description", "vars", "pipelines"):
        known.add(key)
    consumed = set()
    for rel in ("universal_scraper/engine.py", "universal_scraper/engine_v3.py"):
        src = _read(rel)
        consumed |= set(_re.findall(r'(?<!\.)\bconfig\.get\("([a-zA-Z_]+)"', src))
        consumed |= set(_re.findall(r'self\.config\.get\("([a-zA-Z_]+)"', src))
    unknown = consumed - known
    if unknown:
        FAIL.append(f"引擎消费了未登记配置键：{sorted(unknown)}（同步 config 已知键，防拼错静默）")


def check_cli_flags_wired():
    """CLI 死参数普查（R22）：add_argument 声明了但全文件无人读取 = 静默 no-op。

    用户传了 --sheet 却什么都没发生，是"假成功"族最典型的一种。豁免：
    - help 明写"（默认…）"的（如 cdp --list-tabs"列出所有标签页（默认）"）
    - 经 vars(args) 别名（a = vars(args) 后 a.get("x")）读取的"""
    import ast as _ast
    import re as _re
    src_path = ROOT / "universal_scraper" / "cli.py"
    try:
        src = src_path.read_text(encoding="utf-8")
        tree = _ast.parse(src)
    except (OSError, SyntaxError) as e:
        FAIL.append(f"cli.py 无法解析: {e}")
        return
    subs = {}
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Assign) and isinstance(node.value, _ast.Call):
            f = node.value.func
            if isinstance(f, _ast.Attribute) and f.attr == "add_parser":
                try:
                    name = _ast.literal_eval(node.value.args[0])
                except Exception:
                    continue
                for t in node.targets:
                    if isinstance(t, _ast.Name):
                        subs[t.id] = name
    aliases = {t.id for node in _ast.walk(tree)
               if isinstance(node, _ast.Assign) and isinstance(node.value, _ast.Call)
               and isinstance(node.value.func, _ast.Name) and node.value.func.id == "vars"
               for t in node.targets if isinstance(t, _ast.Name)}
    declared = {}
    for node in _ast.walk(tree):
        if not (isinstance(node, _ast.Call) and isinstance(node.func, _ast.Attribute)
                and node.func.attr == "add_argument" and isinstance(node.func.value, _ast.Name)):
            continue
        v = node.func.value.id
        if v not in subs:
            continue
        flags = [a.value for a in node.args if isinstance(a, _ast.Constant)]
        dest, helpt = None, ""
        for kw in node.keywords:
            if kw.arg == "dest":
                dest = _ast.literal_eval(kw.value)
            if kw.arg == "help" and isinstance(kw.value, _ast.Constant):
                helpt = str(kw.value.value)
        if dest is None:
            longs = [f for f in flags if f.startswith("--")]
            dest = (longs[0] if longs else flags[0]).lstrip("-").replace("-", "_")
        declared.setdefault(subs[v], {})[dest] = (flags, helpt)
    read = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Attribute) and isinstance(node.value, _ast.Name) \
                and node.value.id == "args":
            read.add(node.attr)
        if isinstance(node, _ast.Call):
            f = node.func
            if isinstance(f, _ast.Name) and f.id == "getattr" and node.args \
                    and isinstance(node.args[0], _ast.Name) and node.args[0].id == "args" \
                    and len(node.args) > 1 and isinstance(node.args[1], _ast.Constant):
                read.add(str(node.args[1].value))
            if isinstance(f, _ast.Attribute) and f.attr in ("get", "pop") and node.args \
                    and isinstance(node.args[0], _ast.Constant) \
                    and isinstance(f.value, _ast.Name) and f.value.id in aliases:
                read.add(str(node.args[0].value))
    for sub, dests in sorted(declared.items()):
        for dest, (flags, helpt) in sorted(dests.items()):
            if dest in read:
                continue
            if _re.search(r"（默认|默认）", helpt):
                continue          # 文档明写"默认行为"，显式传参属冗余而非谎言
            FAIL.append(f"cli.py {sub}: 参数 {flags} 声明后无人读取（静默 no-op）")


def check_recipe_refs_resolve():
    """配方编号引用必须真实存在（R22）：代码/文档里写"配方 R47"而 recipes.md 无 R47 =
    对 agent 说了假话（会去翻一个不存在的配方）。"""
    import re as _re
    recipes = _read("references/recipes.md")
    if not recipes:
        return
    defined = set(_re.findall(r"配方\s*\*{0,2}R(\d+)", recipes)) | \
        set(_re.findall(r"^#+\s*\**R(\d+)", recipes, _re.M))
    targets = list((ROOT / "universal_scraper").glob("*.py")) + \
        [ROOT / "SKILL.md"] + list((ROOT / "references").glob("*.md"))
    for f in targets:
        try:
            t = f.read_text(encoding="utf-8")
        except OSError:
            continue
        for m in _re.finditer(r"配方\s*\*{0,2}R(\d+)", t):
            if m.group(1) not in defined:
                FAIL.append(f"{f.name}: 引用了不存在的配方 R{m.group(1)}")


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
    # R20 契约静态检查（与逐文件 AST 检查并列）
    check_mcp_tool_registry()
    check_switch_key_parity()
    check_config_key_contract()
    # R22 陈述真实性检查（"技能说的话必须是真的"）
    check_cli_flags_wired()
    check_recipe_refs_resolve()
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
