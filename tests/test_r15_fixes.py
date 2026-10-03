#!/usr/bin/env python3
"""审查十五轮（R15，实战反馈猫眼）修复的回归测试。

覆盖：
- modules/fetchers：capture 模式下空壳不抛 RateLimitedError（bug #1 根因之一）
- quick：capture 复制后 stdout 显式打印路径（bug #1 的可见性）
- SKILL.md：任务书对照表 / R45 路由行 / macOS timeout 注记（反馈 #2/#3/#4）
- Lite 同步：recipes/spec-schema 与 Full 字节一致（含用户 R46）
"""
import inspect
import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL))


def test_capture_mode_empty_shell_exempt():
    from universal_scraper.modules.fetchers import BrowserFetcher
    src = inspect.getsource(BrowserFetcher.fetch)
    # 修前：空壳检测无条件抛 RateLimitedError——capture 模式（空壳恰是主场景）
    # 的桥捕获从未执行，--capture 参数拿不到文件
    assert 'and not self.config.get("capture")' in src


def test_quick_capture_path_printed():
    from universal_scraper import quick as Q
    src = inspect.getsource(Q)
    # 复制点显式打印绝对路径（修前只在日志侧、PC 端静默）
    assert src.count("capture 已保存到:") >= 2
    assert "capture_error" in src


def test_skillmd_taskbook_vs_site_table():
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    # 反馈 #2：任务书 vs 源站能力对照表
    assert "任务书 vs 源站能力对照" in text
    assert "实际可翻深度" in text
    # 反馈 #4：路由表的评论类任务 → R45 指引
    assert "先读配方 R45" in text
    # 反馈 #3：macOS timeout 注记
    assert "macOS 没有 `timeout` 命令" in text


def test_lite_recipes_in_sync_with_r46():
    full = (SKILL / "references" / "recipes.md").read_text(encoding="utf-8")
    lite = (SKILL.parent / "universal-scraper-lite" / "references" / "recipes.md").read_text(encoding="utf-8")
    assert "R46 · 猫眼电影" in full                     # 用户沉淀的猫眼配方在 Full
    assert full == lite                                 # 知识层同步纪律：字节一致
    schema_full = (SKILL / "references" / "spec-schema.md").read_text(encoding="utf-8")
    schema_lite = (SKILL.parent / "universal-scraper-lite" / "references" / "spec-schema.md").read_text(encoding="utf-8")
    assert schema_full == schema_lite
