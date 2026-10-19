#!/usr/bin/env python3
"""模型通道对照测试：同一提示分别请求 官网 DeepSeek / 硅基流动直连 / CC Switch 本地代理。

用途：定位「好。」口头禅 / 低信息量重复到底来自模型部署差异、模型别名还是代理层。
安全约定：所有 API Key 只从环境变量或 CC Switch 本地配置读取，不写入文档/代码，也不打印。
本脚本不会打印任何 Key 片段，只会标记 configured / missing。

用法：
    python3 scripts/model_channel_test.py --endpoint responses --rounds 3
    python3 scripts/model_channel_test.py --endpoint both --rounds 1
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

PROXY_BEARER = "PROXY_MANAGED"
DB_PATH = Path.home() / ".cc-switch" / "cc-switch.db"
CODEX_CONFIG = Path.home() / ".codex" / "config.toml"
SYSTEM = "你是一个直接、简洁的编程助手。不要口头禅，不要复读，不要每步都汇报'好。'。"
USER = "请用一句话说明：爬虫返回 0 条结果时，应该给用户什么？"
MAX_TOKENS = 600


@dataclass
class Channel:
    id: str
    name: str
    base: str
    model: str
    key: str
    available: bool = True
    missing_reason: str = ""


def _provider_config(name: str) -> Dict[str, str]:
    """从 CC Switch 本地数据库读取指定 provider 的 base/model/key 元信息。"""
    base, model, key = "", "", ""
    try:
        con = sqlite3.connect(str(DB_PATH))
        row = con.execute(
            "SELECT settings_config FROM providers WHERE app_type='codex' AND name=? LIMIT 1",
            (name,),
        ).fetchone()
        con.close()
        if not row:
            return {"base": base, "model": model, "key": key}
        cfg = json.loads(row[0])
        text = cfg.get("config", "") or ""
        m = re.search(r'^\s*base_url\s*=\s*"([^"]+)"', text, re.M)
        base = m.group(1).strip() if m else ""
        m = re.search(r'^\s*model\s*=\s*"([^"]+)"', text, re.M)
        model = m.group(1).strip() if m else ""
        key = str((cfg.get("auth") or {}).get("OPENAI_API_KEY", "") or "")
        return {"base": base, "model": model, "key": key}
    except Exception:
        return {"base": base, "model": model, "key": key}


def _current_codex_config() -> Dict[str, str]:
    """读取当前 Codex 实际生效的 model / provider / base_url（只用于展示，不读 Key）。"""
    out = {"model": "", "provider": "", "base_url": "", "wire_api": ""}
    try:
        text = CODEX_CONFIG.read_text(encoding="utf-8")
        m = re.search(r'^model\s*=\s*"([^"]+)"', text, re.M)
        out["model"] = m.group(1) if m else ""
        m = re.search(r'^model_provider\s*=\s*"([^"]+)"', text, re.M)
        out["provider"] = m.group(1) if m else ""
        m = re.search(r'^\s*base_url\s*=\s*"([^"]+)"', text, re.M)
        out["base_url"] = m.group(1) if m else ""
        m = re.search(r'^\s*wire_api\s*=\s*"([^"]+)"', text, re.M)
        out["wire_api"] = m.group(1) if m else ""
    except Exception:
        pass
    return out


def _channels() -> List[Channel]:
    provider = _provider_config("DeepSeek")
    silicon = _provider_config("SiliconFlow")
    codex = _current_codex_config()

    # 官网通道：取自 CC Switch 当前 DeepSeek provider（不要使用硬编码模型名）。
    official = Channel(
        id="deepseek-official",
        name="官网 DeepSeek",
        base=os.environ.get("DEEPSEEK_BASE_URL", provider["base"] or "https://api.deepseek.com").rstrip("/"),
        model=os.environ.get("DEEPSEEK_MODEL", provider["model"] or "deepseek-v4-flash"),
        key=os.environ.get("DEEPSEEK_API_KEY", provider["key"]),
        available=bool(os.environ.get("DEEPSEEK_API_KEY") or provider["key"]),
        missing_reason="环境变量/CC Switch 中没有 DeepSeek API Key",
    )
    # 硅基流动通道：有配置才请求，否则跳过。
    direct = Channel(
        id="siliconflow-direct",
        name="硅基流动直连",
        base=os.environ.get("SILICONFLOW_BASE_URL", silicon["base"] or "https://api.siliconflow.cn/v1").rstrip("/"),
        model=os.environ.get("SILICONFLOW_MODEL", silicon["model"] or "deepseek-ai/DeepSeek-V4-Flash"),
        key=os.environ.get("SILICONFLOW_API_KEY", silicon["key"]),
        available=bool(os.environ.get("SILICONFLOW_API_KEY") or silicon["key"]),
        missing_reason="环境变量/CC Switch 中没有 SiliconFlow API Key",
    )
    # 本地代理通道：使用 Codex 当前实际配置里的模型，而不是硅基流动别名。
    proxy = Channel(
        id="cc-switch-proxy",
        name="CC Switch 本地代理",
        base=os.environ.get("PROXY_BASE_URL", "http://127.0.0.1:15721/v1").rstrip("/"),
        model=os.environ.get("PROXY_MODEL", codex["model"] or "deepseek-v4-flash"),
        key=PROXY_BEARER,
        available=True,
    )
    return [official, direct, proxy]


def _sentence_split(text: str) -> List[str]:
    parts = re.split(r"(?<=[。！？!?；;])|\n+", text)
    return [p.strip() for p in parts if p.strip()]


def _fillers(text: str) -> Dict[str, int]:
    counts = {
        "好。": len(re.findall(r"好(?:的|啊|嘞|吧)?[。！]|嗯{1,}[。！]|收到[。！]", text)),
        "重复句": 0,
        "最大连复": 0,
    }
    sents = _sentence_split(text)
    run = 1
    for a, b in zip(sents, sents[1:]):
        if a == b:
            run += 1
            counts["重复句"] += 1
            counts["最大连复"] = max(counts["最大连复"], run)
        else:
            run = 1
    return counts


def _extract_text(resp: Dict[str, Any], endpoint: str) -> str:
    if endpoint == "responses":
        out = []
        for item in resp.get("output") or []:
            if item.get("type") == "message":
                for c in item.get("content") or []:
                    if c.get("type") == "output_text":
                        out.append(c.get("text", ""))
        return "\n".join(out)
    choices = resp.get("choices") or []
    return (choices[0].get("message", {}).get("content", "") if choices else "")


def _chat_body(ch: Channel) -> Dict[str, Any]:
    return {
        "model": ch.model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": USER},
        ],
        "stream": False,
        "max_tokens": MAX_TOKENS,
    }


def _responses_body(ch: Channel) -> Dict[str, Any]:
    return {
        "model": ch.model,
        "instructions": SYSTEM,
        "input": USER,
        "stream": False,
        "max_output_tokens": MAX_TOKENS,
    }


def _call_once(ch: Channel, endpoint: str) -> Dict[str, Any]:
    url = f"{ch.base}/{'responses' if endpoint == 'responses' else 'chat/completions'}"
    headers = {
        "Authorization": f"Bearer {ch.key}",
        "Content-Type": "application/json",
    }
    body = _responses_body(ch) if endpoint == "responses" else _chat_body(ch)
    t0 = time.monotonic()
    try:
        r = requests.post(url, headers=headers, json=body, timeout=120)
        elapsed = time.monotonic() - t0
        if r.status_code != 200:
            # 不打印上游原始错误里的敏感信息，只保留状态码和类型。
            return {
                "ok": False,
                "status": r.status_code,
                "elapsed": round(elapsed, 2),
                "text": f"HTTP {r.status_code}",
                "error_hint": (r.text or "")[:240].replace("\n", " "),
            }
        data = r.json()
        text = _extract_text(data, endpoint).strip()
        return {"ok": True, "status": 200, "elapsed": round(elapsed, 2), "text": text}
    except Exception as e:
        return {"ok": False, "status": 0, "elapsed": round(time.monotonic() - t0, 2),
                "text": f"{type(e).__name__}: {e}"}


def _proxy_models(base: str) -> List[str]:
    """检测本地代理实际暴露的模型名，用于核对配置名是否被代理接受。"""
    try:
        r = requests.get(f"{base}/models", headers={"Authorization": f"Bearer {PROXY_BEARER}"}, timeout=5)
        if r.status_code != 200:
            return []
        data = r.json()
        names = []
        for m in data.get("models", []):
            for k in ("slug", "model", "id", "name"):
                if m.get(k):
                    names.append(str(m[k]))
                    break
        return names
    except Exception:
        return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", choices=["responses", "chat", "both"], default="both")
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--system", default=SYSTEM)
    ap.add_argument("--user", default=USER)
    args = ap.parse_args()

    globals().update(SYSTEM=args.system, USER=args.user)

    endpoints = ["responses", "chat"] if args.endpoint == "both" else [args.endpoint]
    channels = _channels()
    codex = _current_codex_config()

    print("\n📍 当前 Codex 实际配置（模型通道监测）")
    print(f"  model={codex['model'] or '?'}")
    print(f"  provider={codex['provider'] or '?'}")
    print(f"  base_url={codex['base_url'] or '?'}")
    print(f"  wire_api={codex['wire_api'] or '?'}")
    print(f"\n📡 对照通道（Key 已脱敏，不打印）")
    for ch in channels:
        key_state = "configured" if ch.available else "missing"
        print(f"  - {ch.name}: {ch.base} | model={ch.model} | key={key_state}"
              + ("" if ch.available else f" | {ch.missing_reason}"))

    proxy_base = next((c.base for c in channels if c.id == "cc-switch-proxy"), "")
    if proxy_base:
        models = _proxy_models(proxy_base)
        if models:
            print(f"\n🔎 本地代理实际暴露模型: {', '.join(models)}")

    rows = []
    for ch in channels:
        if not ch.available:
            print(f"\n⏭️ 跳过 {ch.name}（{ch.missing_reason}）")
            continue
        for ep in endpoints:
            for i in range(1, args.rounds + 1):
                res = _call_once(ch, ep)
                metrics = _fillers(res["text"]) if res["ok"] else {"好。": 0, "重复句": 0, "最大连复": 0}
                rows.append({
                    "channel": ch.name,
                    "endpoint": ep,
                    "round": i,
                    "elapsed": res["elapsed"],
                    "status": res["status"],
                    "len": len(res["text"]),
                    **metrics,
                    "preview": res["text"][:240],
                    "error_hint": res.get("error_hint", ""),
                })

    print("\n📊 模型通道对照（同一 System + 同一用户消息）")
    for r in rows:
        flag = []
        if r["好。"]:
            flag.append(f"口头禅×{r['好。']}")
        if r["重复句"]:
            flag.append(f"重复句×{r['重复句']} 连复{r['最大连复']}")
        print(
            f"[{r['channel']}/{r['endpoint']}/R{r['round']}] "
            f"HTTP {r['status']} {r['elapsed']}s len={r['len']} "
            + (" ".join(flag) if flag else "未见明显复读")
        )
        if r["error_hint"]:
            print("  " + r["error_hint"])
        else:
            print("  " + r["preview"].replace("\n", " ⏎ "))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
