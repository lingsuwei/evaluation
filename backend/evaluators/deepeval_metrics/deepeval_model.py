# -*- coding: utf-8 -*-
"""DeepEval 模型构建的共享适配——按 provider/base_url 选择 DeepEval 内置模型类，统一接 OpenAI 兼容端点。

源: agent_eval.integrations.models.deepeval_model（零改动移植）。
"""
from __future__ import annotations

import os
import sys


def make_console_safe() -> None:
    """让 stdout/stderr 对无法编码的字符（DeepEval 输出的 emoji）改为替换而非崩溃。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="replace")
        except Exception:  # noqa: BLE001 - 流不可重配时静默跳过
            pass


def resolve_provider(base_url: str, explicit: str | None) -> str:
    """决定 provider：显式优先，否则按 base_url 自动判断（纯函数）。"""
    if explicit:
        return explicit.strip().lower()
    url = (base_url or "").lower()
    if "deepseek" in url:
        return "deepseek"
    if any(key in url for key in ("dashscope", "aliyun", "qwen")):
        return "local"  # Qwen/DashScope 为 OpenAI 兼容，走通用 LocalModel
    if "openai.com" in url:
        return "openai"
    if "11434" in url or "ollama" in url:
        return "ollama"
    return "local"  # 默认：任意 OpenAI 兼容端点（vLLM/OneAPI/本地网关等）


def _default_timeout() -> float:
    """评测模型单次调用超时(秒)：优先环境变量 EVAL_LLM_TIMEOUT_SECONDS，默认 300。"""
    try:
        return float(os.getenv("EVAL_LLM_TIMEOUT_SECONDS", "") or 300.0)
    except ValueError:
        return 300.0


def build_deepeval_model(
    *,
    model: str,
    base_url: str,
    api_key: str | None,
    temperature: float = 0.0,
    provider: str | None = None,
    timeout: float | None = None,
):
    """根据配置构建一个 DeepEval 模型实例（懒加载 deepeval）。

    timeout：评测模型单次调用超时(秒)；不传则取 EVAL_LLM_TIMEOUT_SECONDS(默认 300)。
    """
    if timeout is None:
        timeout = _default_timeout()
    os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "YES")  # 关闭遥测
    make_console_safe()  # DeepEval 会向控制台打 emoji，GBK 控制台需容错
    resolved = resolve_provider(base_url, provider)
    if resolved == "deepseek":
        from deepeval.models import DeepSeekModel

        return DeepSeekModel(model=model, api_key=api_key, temperature=temperature)
    if resolved == "openai":
        from deepeval.models import GPTModel

        return GPTModel(model=model, api_key=api_key, base_url=base_url, temperature=temperature)
    if resolved == "ollama":
        from deepeval.models import OllamaModel

        return OllamaModel(model=model, base_url=base_url, temperature=temperature)
    from deepeval.models import LocalModel

    # timeout 经 kwargs 透传到底层 OpenAI 客户端,延长慢模型的单次调用容忍
    return LocalModel(model=model, api_key=api_key, base_url=base_url, temperature=temperature, timeout=timeout)
