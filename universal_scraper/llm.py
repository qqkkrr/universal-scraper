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
    # OCR R131（M）：只拒绝攻击向量（他人预置的符号链接/非属主文件）；
    # 0644 等常见权限不拒——否则大多数用户密钥加载直接失效
    try:
        import os as _os, stat as _stat
        _st = zs.lstat()
        if _stat.S_ISLNK(_st.st_mode) or _st.st_uid != _os.getuid():
            return ""
        # 审查三轮（H）：曾把行内注释一并当 key（"sk-xxx # 注释"→key 带尾巴）。
        # 三分支（双引号/单引号/裸值）——非贪婪+尾锚定的组合在带引号形态会
        # 整体回溯失配（实测 m=None）
        m = (re.search(r'^export\s+QWEN_API_KEY="([^"]*)"', zs.read_text(), re.M)
             or re.search(r"^export\s+QWEN_API_KEY='([^']*)'", zs.read_text(), re.M)
             or re.search(r'^export\s+QWEN_API_KEY=([^"\'\s#]+)', zs.read_text(), re.M))
        if m:
            return (m.group(1) or "").strip()
    except OSError:
        pass
    return ""




class _LLMFormatError(RuntimeError):
    """确定性响应格式错误——不重试，直穿重试循环（审查三轮）。"""


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
        # R95 修复（P2）：无 Key 曾发起空 Bearer 请求并把 401 误报成网络波动——
        # 中心化前置守卫（覆盖 auto/precise_auto/webui 所有 LLM 调用方）
        if not self.api_key:
            raise RuntimeError("未配置 API Key——AI 功能需要先配置（webui 设置页或 `llm --key <key>`）")
        import time
        import urllib.request
        body = json.dumps({"model": self.model, "messages": messages, "temperature": temperature}).encode()
        last_err = ""
        for attempt in range(1, retries + 2):  # retries=N = 首呼 + N 次重试（与 vision()/报错文案一致）
            req = urllib.request.Request(
                self.base_url.rstrip("/") + "/chat/completions", data=body,
                headers={"Authorization": "Bearer " + self.api_key, "Content-Type": "application/json"})
            # scheme 守卫：仅 http/https。信任边界说明——base_url 是用户本机
            # 配置（含本地 Ollama 等私有端点，属产品特性），故不做私网 IP 过滤，
            # 仅拒绝 file:/ftp: 等伪协议。
            # 审查二轮（M）：guard 曾在 try 内——配置错误的伪协议被当网络错误
            # 白白重试满轮。配置类错误应在循环外立即失败
            _llm_url_guard(req.full_url)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    data = json.load(r)
                try:
                    return data["choices"][0]["message"]["content"]
                except (KeyError, IndexError, TypeError) as e:
                    # OCR R131 同口径（vision）：确定性格式错不烧重试轮——
                    # 专用异常直穿重试循环（审查七轮 N30）
                    raise _LLMFormatError(
                        f"LLM 响应格式异常（缺少 choices[0].message.content）: {str(data)[:200]}") from e
            except _LLMFormatError:
                raise
            except urllib.error.HTTPError as e:
                # 收官九轮（审查）：4xx = 请求本身有误（key/参数/格式）——重试
                # 同样的请求必然同样失败，白白烧掉预算和时间
                e.close() if hasattr(e, 'close') else None
                raise RuntimeError(f"LLM HTTP {e.code}（{'认证失败' if e.code in (401,403) else '请求错误'}）"
                                   f"——非瞬时错误，不重试: {e.read().decode('utf-8','ignore')[:200]}") from e
            except Exception as e:
                last_err = str(e)
                if attempt < retries + 1:
                    wait = 2 ** attempt + 1
                    time.sleep(wait)
        raise RuntimeError(f"LLM 调用失败（重试 {retries} 次后）: {last_err}")

    def vision(self, prompt: str, image_url: str, timeout: Optional[int] = None) -> str:
        """视觉问答：传图片 URL（http/data: 均可），用 VISION_MODEL（未配置则回退主模型）。
        用于看截图判断页面结构/验证码等场景。返回文本。"""
        if not self.vision_api_key:
            # 审查二轮（M）：vision 曾缺 key 前置检查——401 被误报成网络波动
            raise RuntimeError("未配置视觉模型 API Key——先在 webui 设置页或 `llm --key` 配置")
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
            _llm_url_guard(req.full_url)  # 审查二轮（M）：同 chat——移出重试 try
            try:
                with urllib.request.urlopen(req, timeout=to) as r:
                    data = json.load(r)
                try:
                    return data["choices"][0]["message"]["content"]
                except (KeyError, IndexError, TypeError) as e:
                    # OCR R131（M）：格式异常曾以裸 KeyError 进重试，报错无从排查。
                    # 审查三轮（M）：格式化 RuntimeError 抛在内层 try 里仍会被外层
                    # except 捕获重试——确定性格式错不该烧满 3 轮。用专用异常直穿
                    raise _LLMFormatError(
                        f"视觉模型响应格式异常（缺少 choices[0].message.content）: {str(data)[:200]}") from e
            except _LLMFormatError:
                raise
            except Exception as e:
                last_err = str(e)
                if attempt < 3:
                    _t.sleep(2 ** attempt + 1)
        raise RuntimeError(f"视觉模型调用失败: {last_err}")

    @staticmethod
    def describe() -> dict:
        """当前 LLM 配置摘要（us llm / doctor 用）。"""
        _k = _get_key()  # OCR R131（L）：曾连续调用两次 _get_key
        return {
            "主模型": os.environ.get("LLM_MODEL", "qwen3.7-plus"),
            "主接口": os.environ.get("OPENAI_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            "视觉模型": os.environ.get("VISION_MODEL") or os.environ.get("LLM_VISION_MODEL") or "qwen-vl-max(默认)",
            "API Key": ("已配置(" + _k[:6] + "...)" if _k else "未配置"),
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
            # 先试最短 {} 片段（防贪婪把首 { 到末 } 之间的噪声一并吞入），
            # 解析失败再回退贪婪匹配（嵌套 JSON 场景保持原有可提取性）
            m = re.search(r"\{.*?\}", raw, re.S)
            if m:
                try:
                    return json.loads(m.group(0))
                except json.JSONDecodeError:
                    pass
            m = re.search(r"\{.*\}", raw, re.S)
            if m:
                return json.loads(m.group(0))
            raise ValueError(f"LLM 未返回合法 JSON: {raw[:200]}")

    def extract_json_model(self, content: str, model_cls,
                           instruction: str = "请从以下内容中提取字段，输出严格 JSON。",
                           max_fix_rounds: int = 2) -> Any:
        """R101 新能力（对标 Crawl4AI 结构化抽取）：pydantic 模型驱动的抽取。

        - model_cls: pydantic.BaseModel 子类 → 自动生成 jsonschema 注入提示词
        - LLM 返回后用模型校验；校验失败把错误清单回喂 LLM 自修复（最多
          max_fix_rounds 轮），仍失败才抛 ValidationError。
        - pydantic 未安装时抛 ImportError（调用方降级到 extract_json）。"""
        try:
            import pydantic
        except ImportError:
            raise ImportError("pydantic 未安装（pip install pydantic）——"
                              "或降级用 extract_json(dict schema)")
        if not (isinstance(model_cls, type) and issubclass(model_cls, pydantic.BaseModel)):
            raise TypeError("model_cls 必须是 pydantic.BaseModel 子类")
        schema_str = json.dumps(model_cls.model_json_schema(), ensure_ascii=False)
        prompt = (f"{instruction}\n只输出 JSON 对象，不要任何解释。"
                  f"字段结构（jsonschema）:\n{schema_str}\n\n内容:\n{content[:8000]}")
        last_err = ""
        for attempt in range(1 + max_fix_rounds):
            raw = self.chat([
                {"role": "system", "content": "你是专业的数据抽取引擎，只输出合法 JSON。"},
                {"role": "user", "content": prompt if not last_err else
                    (prompt + f"\n\n上一次输出未通过校验，错误：{last_err}\n请修正后重新输出完整 JSON。")},
            ])
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                # OCR R131（M）：与 extract_json 同口径——先试最短 {} 片段（防贪婪
                # 吞入噪声），失败再回退贪婪匹配（嵌套 JSON 保持可提取性）
                data = None
                m = re.search(r"\{.*?\}", raw, re.S)
                if m:
                    try:
                        data = json.loads(m.group(0))
                    except json.JSONDecodeError:
                        data = None
                if data is None:
                    m = re.search(r"\{.*\}", raw, re.S)
                    if not m:
                        last_err = f"非 JSON 输出: {raw[:200]}"
                        continue
                    try:
                        data = json.loads(m.group(0))
                    except json.JSONDecodeError as e2:
                        # R103 修复（P2）：大括号内有内容但非法时曾绕过修复循环裸抛
                        last_err = f"截取片段仍非合法 JSON: {e2}"
                        continue
            try:
                return model_cls.model_validate(data)
            except pydantic.ValidationError as e:
                last_err = "; ".join(f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}"
                                     for err in e.errors()[:8])
        raise ValueError(f"LLM 抽取 {max_fix_rounds + 1} 轮仍未通过模型校验: {last_err}")
