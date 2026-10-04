#!/usr/bin/env python3
"""审查十七轮（R17，清最后备案 + 回归自查）修复的回归测试。

覆盖：
- fetchers：fetch --capture 空壳豁免（capture/capture_all 两拼写+组合）+ capture_file 事件可见
- scripts/browser_generic.cjs：detail 循环接 handleCaptcha（源级）
- auto：run_with_config 升级种类全集 + _maybe_import_session 接线（源级）
- monitor：watch_once 跨进程 flock（源级）
"""
import inspect
import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL))


def test_capture_empty_shell_exempt_both_spellings():
    from universal_scraper.modules.fetchers import BrowserFetcher
    from universal_scraper.protocols import Response, Request

    def mk(config):
        bf = BrowserFetcher(config, {}, {"session_dir": "/tmp/us_r17_sess",
                                         "empty_shell_check": True})
        bf._fetch_interactive = lambda req: Response(
            request=req, status=200, text="<html><body>x</body></html>", url=req.url)
        return bf

    assert mk({"capture": True}).fetch(Request(url="http://x/")).status == 200
    assert mk({"capture_all": True}).fetch(Request(url="http://x/")).status == 200


def test_generic_bridge_detail_captcha_wired():
    src = (SKILL / "scripts" / "browser_generic.cjs").read_text(encoding="utf-8")
    # detail 循环接 handleCaptcha（修前详情页验证码静默存 HTML）
    assert "detail 循环曾不调 handleCaptcha" in src
    # Python 侧 captcha 事件不再静默
    py = (SKILL / "universal_scraper" / "fetchers.py").read_text(encoding="utf-8")
    assert '详情页验证码' in py


def test_run_with_config_upgrade_kinds_full():
    from universal_scraper import auto as A
    src = inspect.getsource(A)
    # 升级种类全集（修前 run_with_config 只认 waf）
    assert '_upgrade_kinds = ("waf", "cloudflare", "verify", "captcha", "anti_bot",' in src
    # _maybe_import_session_on_auth_wall 在 run_with_config 路径接线
    assert "_maybe_import_session_on_auth_wall(config, _reason, log)" in src


def test_monitor_watch_flocked():
    from universal_scraper import monitor as M
    src = inspect.getsource(M.watch_once)
    assert "flock" in src and "watch.lock" in src
    # 内部实现被拆为 _watch_once_locked
    assert hasattr(M, "_watch_once_locked")
