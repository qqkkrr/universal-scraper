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
    # 主模型 key：OPENAI_API_KEY 优先（切 DeepSeek/GPT 等时用），千问 QWEN_API_KEY 兜底
    key = os.environ.get("OPENAI_API_KEY") or os.environ.get("QWEN_API_KEY") or ""
    if key:
        return key
    from pathlib import Path
    zs = Path.home() / ".zshenv"
    if zs.exists():
        m = re.search(r'^export\s+QWEN_API_KEY=["\']?([^"\'\n]+)', zs.read_text(), re.M)
        if m:
            return m.group(1).strip()
    return ""




def _llm_url_guard(url: str) -> str:
    """LLM 端点出站守卫：仅 http/https（拒绝 file:/ftp: 等伪协议读取本地资源）。
    信任边界：base_url 为用户在本机/设置页显式配置的推理端点（含本地 Ollama 等私有
    端点，属产品特性），故不阻断私网/环回地址；但协议白名单与主机非空校验强制执行。"""
    from urllib.parse import urlsplit as _split
    sp = _split(url or "")
    if (sp.scheme or "").lower() not in ("http", "https") or not sp.hostname:
        raise ValueError(f"LLM 端点仅支持 http/https，已拒绝: {str(url)[:60]!r}")
    return url


class LLMClient:
    """主决策模型（纯文本）：配置生成/自修复/抽取，走 LLM_MODEL（默认千问）。
    视觉模型（可选）：看截图/图片验证码，走 VISION_MODEL（如 qwen-vl-max / gpt-4o）。
    两者可完全不同厂商：主模型用 DeepSeek，视觉用千问 qwen-vl，互不冲突。"""

    def __init__(self, model: Optional[str] = None, base_url: Optional[str] = None,
                 api_key: Optional[str] = None, timeout: Optional[int] = None):
        timeout = timeout or int(os.environ.get("LLM_TIMEOUT", "90"))
        self.api_key = api_key or _get_key()
        self.base_url = base_url or os.environ.get(
            "OPENAI_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
        self.model = model or os.environ.get("LLM_MODEL", "qwen3.7-plus")
        self.timeout = timeout
        self.vision_model = os.environ.get("VISION_MODEL") or os.environ.get("LLM_VISION_MODEL") or ""
        self.vision_base_url = os.environ.get("VISION_BASE_URL") or self.base_url
        self.vision_api_key = os.environ.get("VISION_API_KEY") or self.api_key

    def chat(self, messages: List[Dict[str, str]], temperature: float = 0.1,
             retries: int = 2) -> str:
        """带指数退避重试（LLM 一慢/一闪断不应让整个任务死掉）。"""
        import time
        import urllib.request
        body = json.dumps({"model": self.model, "messages": messages, "temperature": temperature}).encode()
        last_err = ""
        for attempt in range(1, retries + 1):
            req = urllib.request.Request(
                self.base_url.rstrip("/") + "/chat/completions", data=body,
                headers={"Authorization": "Bearer " + self.api_key, "Content-Type": "application/json"})
            try:
                # scheme 守卫：仅 http/https。信任边界说明——base_url 是用户本机
                # 配置（含本地 Ollama 等私有端点，属产品特性），故不做私网 IP 过滤，
                # 仅拒绝 file:/ftp: 等伪协议
                _llm_url_guard(req.full_url)
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    data = json.load(r)
                return data["choices"][0]["message"]["content"]
            except Exception as e:
                last_err = str(e)
                if attempt < retries:
                    wait = 2 ** attempt + 1
                    time.sleep(wait)
        raise RuntimeError(f"LLM 调用失败（重试 {retries} 次后）: {last_err}")

    def vision(self, prompt: str, image_url: str, timeout: Optional[int] = None) -> str:
        """视觉问答：传图片 URL（http/data: 均可），用 VISION_MODEL（未配置则回退主模型）。
        用于看截图判断页面结构/验证码等场景。返回文本。"""
        if not self.vision_model:
            # 未配视觉模型：若主模型是千问可回退 qwen-vl-max，否则报错提示
            if "dashscope" in self.base_url:
                self.vision_model = os.environ.get("VISION_MODEL", "qwen-vl-max")
            else:
                raise RuntimeError("未配置视觉模型：设 VISION_MODEL 环境变量（如 qwen-vl-max）")
        import time as _t
        import urllib.request
        body = json.dumps({
            "model": self.vision_model,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": image_url}},
            ]}],
            "temperature": 0.1,
        }).encode()
        to = timeout or self.timeout
        last_err = ""
        for attempt in range(1, 4):
            req = urllib.request.Request(
                self.vision_base_url.rstrip("/") + "/chat/completions", data=body,
                headers={"Authorization": "Bearer " + self.vision_api_key,
                         "Content-Type": "application/json"})
            try:
                _llm_url_guard(req.full_url)
                with urllib.request.urlopen(req, timeout=to) as r:
                    data = json.load(r)
                return data["choices"][0]["message"]["content"]
            except Exception as e:
                last_err = str(e)
                if attempt < 3:
                    _t.sleep(2 ** attempt + 1)
        raise RuntimeError(f"视觉模型调用失败: {last_err}")

    @staticmethod
    def describe() -> dict:
        """当前 LLM 配置摘要（us llm / doctor 用）。"""
        return {
            "主模型": os.environ.get("LLM_MODEL", "qwen3.7-plus"),
            "主接口": os.environ.get("OPENAI_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            "视觉模型": os.environ.get("VISION_MODEL") or os.environ.get("LLM_VISION_MODEL") or "qwen-vl-max(默认)",
            "API Key": ("已配置(" + (_get_key()[:6] + "...") if _get_key() else "未配置"),
        }

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
