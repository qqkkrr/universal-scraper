#!/usr/bin/env python3
"""验证码人机协同会话（gsxt 战训沉淀，2026-09）：scripts/captcha_bridge.cjs
（CDP 附加真 Chrome + 工作目录文件协议）的 Python 客户端。

为什么是"文件协议"而不是管道：验证码拉锯战以分钟计，agent（或人）随时断开
重连都不能杀死浏览器现场；workdir 里的 cmd.json/last_result.json/status.json
就是崩溃安全的交接面——本类只做纯文件读写，不持有任何进程句柄，崩溃安全。
桥进程的启动/停止走 `cli captcha start/stop`（生命周期归 CLI，协议归本类）。
复刻自 gsxt 实战里手写的 cmd.json/last_result.json/status.json 三件套，
现已内置化——不要再现场重写这 300 行。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
BRIDGE = SCRIPTS_DIR / "captcha_bridge.cjs"


class CaptchaSession:
    """连接/驱动验证码桥（纯文件协议客户端）。所有 op 返回 data 字典；
    失败抛 RuntimeError。用法：
        s = CaptchaSession(workdir)         # 桥已由 `cli captcha start` 启动
        s.wait_alive(30)                    # 等 status.json 上报心跳
        s.goto(url) / s.shot() / s.click_xy([[x, y]]) / s.html() ...
        s.stop()                            # 触摸 stop 文件，桥优雅退出
    """

    def __init__(self, workdir: str | Path):
        self.dir = Path(workdir).resolve()
        # 纪元毫秒作 id 基数（审查 P1 修复）：每个新进程若从 0 起 id，会被
        # 仍存活的桥按"已处理过"静默忽略（文档化用法 = start/solve 跨进程）
        self._next_id = int(time.time() * 1000)

    # ---- 心跳 ----
    def status(self) -> Dict[str, Any]:
        try:
            return json.loads((self.dir / "status.json").read_text(encoding="utf-8"))
        except Exception:
            return {"alive": False}

    def wait_alive(self, timeout: float = 30.0, require_after: float = 0.0) -> Dict[str, Any]:
        """等桥上报心跳；超时抛错（附诊断方向）。

        require_after（epoch 秒，>0 生效）：只接受**该时刻之后**产生的心跳。
        收官十轮（审查，实测复现）：原实现只查 alive 字段——被 SIGKILL 的旧桥
        残留 status.json（alive:true）会让 `cli captcha --start` 立即"成功"返回
        死桥的 mode/url，而真正新起的桥若启动失败已写墓碑退出，cli 里"启动超时
        击杀孤儿"的防线（依赖本函数抛错）永不触发。启动方须传 require_after=启动
        时刻。"""
        t0 = time.time()
        while time.time() - t0 < timeout:
            st = self.status()
            if st.get("alive"):
                if not require_after:
                    return st
                try:
                    ts = float(st.get("ts", 0)) / 1000.0
                except (TypeError, ValueError):
                    ts = 0.0
                if ts >= require_after:
                    return st
                # 心跳比本次启动还早 = 残留文件，继续等新桥心跳（或超时）
            elif st.get("error"):
                raise RuntimeError(f"验证码桥启动失败: {st['error']}")
            time.sleep(0.5)
        raise RuntimeError(f"验证码桥 {timeout:.0f}s 内未上报新心跳——"
                           f"是否已用 `cli captcha start --dir {self.dir}` 启动？")

    # ---- 生命周期（文件语义）----
    def is_running(self) -> bool:
        st = self.status()
        if not st.get("alive"):
            return False
        # ts 非数值（坏文件/半写）时按桥已死处理——不能让 send() 轮询被 float() 炸穿
        try:
            ts = float(st.get("ts", 0))
        except (TypeError, ValueError):
            return False
        # 心跳 15s 未刷新 = 桥已死（独立定时器 2s 周期；容忍 GC/负载抖动）
        return time.time() * 1000 - ts < 15_000

    def stop(self) -> None:
        """触摸 stop 文件（桥 0.4s 轮询内优雅退出：关专用 tab、删墓碑文件），
        并清掉全部交接文件——is_running 立即转 False。

        收官十轮（审查，实测复现）：曾在本方法里挂一个"2 秒后删除 stop"的线程——
        只要有 op 在飞（goto 最长 90s），桥要等 op 结束才在循环顶部检查 stop，
        届时文件已被客户端自己删掉 → 桥收到"已停止"却继续运行（浏览器不关、
        心跳继续刷、is_running 转回 True），且新 --start 被"已有桥在运行"拒绝，
        用户既停不掉也开不起来。桥退出时本来就自删 stop（captcha_bridge.cjs
        退出清理段），故此处不删；残留 stop 由 --start 启动前清理。"""
        try:
            (self.dir / "stop").touch()
        except Exception:
            pass
        for f in ("cmd.json", "last_result.json", "boot.json", "status.json"):
            try:
                (self.dir / f).unlink(missing_ok=True)
            except Exception:
                pass

    # ---- 文件协议 ----
    def send(self, op: str, timeout: float = 60.0, **kw) -> Dict[str, Any]:
        """写 cmd.json → 轮询 last_result.json 直到 id 匹配。"""
        self._next_id += 1
        cid = self._next_id
        # 先清残留应答（审查 P1 修复）：上一会话的 last_result 若 id 恰好
        # 相同会被当成本次应答，把旧页面当新页面
        try:
            (self.dir / "last_result.json").unlink(missing_ok=True)
        except Exception:
            pass
        # OCR R131（M）：**kw 曾排在 id/op 之后——调用方 kw 里带 id/op 会覆盖
        # 协议字段。kw 先展开，协议字段最后落定
        (self.dir / "cmd.json").write_text(
            json.dumps({**kw, "id": cid, "op": op}, ensure_ascii=False), encoding="utf-8")
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                r = json.loads((self.dir / "last_result.json").read_text(encoding="utf-8"))
                # OCR R131（H）：id 为垃圾值（"abc"/None/list）时 int() 抛
                # ValueError/TypeError 未被下方两个 handler 覆盖——单次坏文件
                # 直接炸出轮询循环。宽容解析：解析失败当没准备好，继续等
                try:
                    rid = int(r.get("id", -1))
                except (TypeError, ValueError, AttributeError):
                    # AttributeError：桥写出的合法 JSON 但非 dict（list/标量）——
                    # 同"坏 id"口径视作非本次应答，继续等
                    rid = -2
                if rid == cid:
                    if not r.get("ok"):
                        raise RuntimeError(f"op={op} 失败: {r.get('error', '?')}")
                    return r.get("data") or {}
            except FileNotFoundError:
                pass
            except json.JSONDecodeError:
                pass  # 半写瞬间重试
            if not (self.dir / "status.json").exists() or not self.is_running():
                raise RuntimeError(f"验证码桥已停止（op={op}）——查 status.json")
            time.sleep(0.25)
        raise RuntimeError(f"op={op} 超时（{timeout:.0f}s）——桥无响应？查 status.json")

    # ---- 便捷封装 ----
    def goto(self, url: str, wait_ms: int = 0) -> Dict[str, Any]:
        return self.send("goto", url=url, waitMs=wait_ms)

    def html(self) -> str:
        return str(self.send("html").get("html") or "")

    def shot(self, out: str = "shot.png", full: bool = False,
             selector: str = "") -> str:
        """截图。给 selector 时只截该元素（验证码主图 OCR 必须用元素截图，
        全页截图会让 OCR 把题面文字也框进去点错）。"""
        return str(self.send("shot", out=out, full=1 if full else 0,
                             selector=selector).get("file") or "")

    def click_xy(self, points: List[List[int]], delay_ms: int = 600) -> int:
        return int(self.send("click_xy", points=points, delayMs=delay_ms).get("clicked") or 0)

    def click_css(self, selector: str, index: int = 0) -> None:
        self.send("click_css", selector=selector, index=index)

    def fill(self, selector: str, text: str) -> None:
        self.send("fill", selector=selector, text=text)

    def press(self, key: str = "Enter") -> None:
        self.send("press", key=key)

    def run_js(self, js: str, timeout: float = 60.0) -> Any:
        """在页面内执行 JS（浏览器桥 op；JS 源码由调用方任务代码提供，
        与 page.evaluate 同语义）。"""
        return self.send("eval", timeout=timeout, js=js).get("result")

    def wait(self, selector: str, timeout_ms: int = 30000) -> bool:
        return bool(self.send("wait", selector=selector, timeoutMs=timeout_ms).get("appeared"))

    def cookies(self) -> List[Dict[str, Any]]:
        return list(self.send("cookies").get("cookies") or [])
