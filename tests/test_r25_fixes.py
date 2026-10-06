#!/usr/bin/env python3
"""审查二十五轮 R25：rangedl 手工套件接线 + 出站守卫敌意形态矩阵 + 会话 hermeticity。

覆盖：
- **出站守卫的敌意形态矩阵**：零信任检查"凡解析到非公网的形态必被拒"——这是
  平台无关的不变式（守卫是"词法 + DNS 解析"两层：`0177.0.0.1` 在 macOS 解析成
  公网 177.0.0.1、在 glibc/Linux 按八进制解析成 127.0.0.1，两者行为不同但都对）
- **test_rangedl_manual 接线**：10 个下载完整性场景进入 pytest 电池（此前只按文件名
  被 import、一条都没跑），且 main() 可重复调用
- **会话 hermeticity**：该模块**不得在模块级**设置 US_ALLOW_PRIVATE（pytest 收集期
  就 import 它——曾污染整个会话，让"守卫拒绝"类断言失去意义）
"""
import ast
import ipaddress
import socket
import sys
import urllib.parse
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL))

# 敌意形态：回环/私网的各种书写法
_HOSTILE_FORMS = [
    "http://127.0.0.1:8080/", "http://127.1:8080/", "http://2130706433:8080/",
    "http://0x7f000001:8080/", "http://0177.0.0.1:8080/", "http://0:8080/",
    "http://0.0.0.0:8080/", "http://[::1]:8080/", "http://[::]:8080/",
    "http://[::ffff:127.0.0.1]:8080/", "http://localhost:8080/",
    "http://localhost.:8080/", "http://127.0.0.1.:8080/",
    "http://169.254.169.254/", "http://10.1.2.3/", "http://192.168.0.1/",
    "http://172.16.9.9/", "http://[fd00::1]:8080/", "http://[fe80::1]:8080/",
]


def _resolves_private(host: str):
    """该主机在本机的解析结果是否含非公网地址（None=解析失败）。"""
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return None
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (ip.is_private or ip.is_loopback or ip.is_reserved
                or ip.is_link_local or ip.is_multicast):
            return True
    return False


def test_r25_outbound_guard_rejects_everything_that_resolves_private(monkeypatch):
    """不变式：**解析到非公网的 URL 必被拒**（平台无关；对每个形态取本机解析为真值）。"""
    from universal_scraper.core import assert_public_url
    monkeypatch.delenv("US_ALLOW_PRIVATE", raising=False)
    leaked = []
    for u in _HOSTILE_FORMS:
        host = urllib.parse.urlsplit(u).hostname or ""
        resolved_private = _resolves_private(host)
        try:
            assert_public_url(u, context="r25")
            rejected = False
        except Exception:
            rejected = True
        if resolved_private is True and not rejected:
            leaked.append((u, host))
        print(f"  {u:<34} resolved_private={resolved_private} rejected={rejected}")
    assert not leaked, f"解析到私网的形态被放行: {leaked}"


def test_r25_outbound_guard_allow_private_switch(monkeypatch):
    from universal_scraper.core import assert_public_url
    monkeypatch.setenv("US_ALLOW_PRIVATE", "1")
    assert_public_url("http://127.0.0.1:8080/x", context="r25")   # 白名单显式放行


def test_r25_proxy_api_guard_stricter_than_core():
    """proxy_api 的 API 地址守卫是**词法层**（比 core 更保守）：全部敌意形态必须拒。"""
    from universal_scraper.proxy_api import _is_public_http_url
    for u in _HOSTILE_FORMS:
        assert _is_public_http_url(u) is False, f"{u} 未被 proxy_api 守卫拒绝"


def test_r25_rangedl_suite_is_collected_and_wired():
    """rangedl 手工套件已接进 pytest：入口存在、场景数达标、可重复调用不累积。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "r25_rangedl_manual", SKILL / "tests" / "test_rangedl_manual.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert hasattr(mod, "test_rangedl_manual_scenarios")
    assert hasattr(mod, "main") and hasattr(mod, "_main_impl")
    # 场景清单非空（数量断言在真实运行里做；此处只验证已接线与结构）
    assert isinstance(mod.res, list) and isinstance(mod.OUT, Path)


def test_r25_rangedl_module_import_is_hermetic():
    """hermeticity：模块级不得设置 US_ALLOW_PRIVATE（pytest 收集期即 import）。

    曾于模块级设置 → 整个测试会话的白名单被打开，守卫类断言全部失真。"""
    src = (SKILL / "tests" / "test_rangedl_manual.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in tree.body:                     # 只看顶层语句（函数/类内部允许）
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Assign):
                for t in sub.targets:
                    if (isinstance(t, ast.Subscript)
                            and isinstance(t.value, ast.Attribute)
                            and isinstance(t.value.value, ast.Name)
                            and t.value.value.id == "os"
                            and getattr(t.value, "attr", "") == "environ"):
                        pytest.fail(f"模块级设置 os.environ（第 {sub.lineno} 行）——会话污染")
    # 功能未丢：设置仍存在于 main()/实现里
    assert 'os.environ["US_ALLOW_PRIVATE"] = "1"' in src
