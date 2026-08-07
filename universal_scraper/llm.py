#!/usr/bin/env python3
"""LLM 智能抽取（crawl4ai/Firecrawl/ScrapeGraphAI 方向）。

用大模型把任意 HTML/文本 转成结构化 JSON，解决"写选择器太费劲"的场景。
默认走千问（DashScope OpenAI 兼容），可配 OPENAI_BASE_URL/OPENAI_API_KEY 走任意兼容服务。
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional


def _get_key() -> str:
    key = os.environ.get("QWEN_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
    if key:
        return key
    from pathlib import Path
    zs = Path.home() / ".zshenv"
    if zs.exists():
        m = re.search(r'^export\s+QWEN_API_KEY=["\']?([^"\'\n]+)', zs.read_text(), re.M)
        if m:
            return m.group(1).strip()
    return ""


class LLMClient:
    def __init__(self, model: Optional[str] = None, base_url: Optional[str] = None,
                 api_key: Optional[str] = None, timeout: int = 90):
        self.api_key = api_key or _get_key()
        self.base_url = base_url or os.environ.get(
            "OPENAI_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
        self.model = model or os.environ.get("LLM_MODEL", "qwen3.7-plus")
        self.timeout = timeout

    def chat(self, messages: List[Dict[str, str]], temperature: float = 0.1) -> str:
        import urllib.request
        body = json.dumps({"model": self.model, "messages": messages, "temperature": temperature}).encode()
        req = urllib.request.Request(
            self.base_url.rstrip("/") + "/chat/completions", data=body,
            headers={"Authorization": "Bearer " + self.api_key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.load(r)
            return data["choices"][0]["message"]["content"]
        except Exception as e:
            raise RuntimeError(f"LLM 调用失败: {e}")

    def extract_json(self, content: str, schema: Dict[str, Any],
                     instruction: str = "请从以下内容中提取字段，输出严格 JSON。") -> Dict[str, Any]:
        """把内容按 schema 提取成 JSON。schema: {"字段名": "字段说明"}。"""
        schema_str = json.dumps(schema, ensure_ascii=False)
        prompt = (
            f"{instruction}\n"
            f"只输出 JSON 对象，不要任何解释。字段定义:\n{schema_str}\n\n"
            f"内容:\n{content[:8000]}"
        )
        raw = self.chat([
            {"role": "system", "content": "你是专业的数据抽取引擎，只输出合法 JSON。"},
            {"role": "user", "content": prompt},
        ])
        # 容错：去掉 ```json 围栏
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", raw, re.S)
            if m:
                return json.loads(m.group(0))
            raise ValueError(f"LLM 未返回合法 JSON: {raw[:200]}")
