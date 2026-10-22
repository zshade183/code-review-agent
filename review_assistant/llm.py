"""模型接入工厂。

三种 provider：

- ``deepseek``：``ChatDeepSeek``（需 ``pip install langchain-deepseek``）
- ``openai``：``ChatOpenAI``，任何 OpenAI 兼容网关（火山方舟 / DashScope / vLLM 都能用）
- ``mock``：离线假模型（``mock_llm.py``），不联网、不花钱就能把整张图跑通

配置读取顺序：先看本包专用的 ``LLM_*`` 变量，再回落到项目根目录 ``config.py``
的 ``API_KEY / API_URL / MODEL_NAME / MOCK_MODE``，让 CLI、API 与实验脚本共用同一份 .env。
"""

from __future__ import annotations

import json
import os
from typing import Optional

from langchain_core.language_models.chat_models import BaseChatModel

from agent import settings


def _base_url(raw: str) -> str:
    """把完整的 chat/completions 地址裁剪成 ChatOpenAI 需要的 base_url。"""
    url = (raw or "").strip().rstrip("/")
    for suffix in ("/chat/completions", "/completions"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
            break
    return url.rstrip("/")


def _extra_body() -> dict:
    raw = (os.getenv("LLM_EXTRA_BODY") or "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM_EXTRA_BODY 必须是 JSON 对象：{exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("LLM_EXTRA_BODY 必须是 JSON 对象")
    return payload


def resolve_provider(provider: Optional[str] = None) -> str:
    """决定使用哪个 provider。"""
    explicit = (provider or os.getenv("LLM_PROVIDER") or "").strip().lower()
    if explicit:
        return explicit
    if settings.MOCK_MODE:
        return "mock"
    if (os.getenv("LLM_API_KEY") or settings.API_KEY) and (os.getenv("LLM_MODEL") or settings.MODEL_NAME):
        return "openai"
    return "mock"


def create_llm(provider: Optional[str] = None, temperature: Optional[float] = None) -> BaseChatModel:
    """按 provider 造一个聊天模型。"""
    name = resolve_provider(provider)
    temp = settings.TEMPERATURE if temperature is None else float(temperature)

    if name == "mock":
        from review_assistant.mock_llm import MockChatModel

        return MockChatModel()

    api_key = (os.getenv("LLM_API_KEY") or settings.API_KEY or "").strip()
    model = (os.getenv("LLM_MODEL") or settings.MODEL_NAME or "").strip()
    if not api_key or not model:
        raise ValueError(
            "缺少模型配置：请在 .env 中设置 LLM_API_KEY / LLM_MODEL，"
            "或沿用旧的 API_KEY / MODEL_NAME，或用 --mock 走离线模型。"
        )

    options = {
        "model": model,
        "api_key": api_key,
        "temperature": temp,
        "timeout": 60,
        "max_retries": 1,
    }

    base_url = _base_url(os.getenv("LLM_BASE_URL") or settings.API_URL)
    if base_url:
        options["base_url"] = base_url

    extra = _extra_body()
    if extra:
        options["extra_body"] = extra

    if name == "deepseek":
        try:
            from langchain_deepseek import ChatDeepSeek
        except ImportError as exc:  # pragma: no cover - 可选依赖
            raise ValueError("使用 deepseek provider 需要先安装 langchain-deepseek") from exc
        return ChatDeepSeek(**options)

    if name == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(**options)

    raise ValueError(f"暂未接入的模型供应商：{name}")


def describe_backend(provider: Optional[str] = None) -> str:
    """给 CLI 打印一行人类可读的后端说明。"""
    name = resolve_provider(provider)
    if name == "mock":
        return "mock（离线假模型，不调用真实接口）"
    model = (os.getenv("LLM_MODEL") or settings.MODEL_NAME or "?")
    base = _base_url(os.getenv("LLM_BASE_URL") or settings.API_URL)
    return f"{name} / {model} @ {base}"
