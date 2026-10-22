"""Agent 运行时配置。

统一从环境变量读取，便于在不改代码的前提下切换模型 / 预算 / 沙箱策略。
密钥相关配置仍然复用项目根目录的 ``config.py``（该文件已支持 .env）。
"""

from __future__ import annotations

import os

# 统一出口：其它模块通过 ``settings.API_KEY`` 等访问，避免各自去 import config。
# 因此这里是**有意**的再导出，并非未使用导入。
# pylint: disable=unused-import
from config import API_KEY, API_URL, MODEL_NAME, MOCK_MODE

AGENT_DIR = "reports/agent"
TRACE_DIR = "traces"
MEMORY_PATH = "memory/agent_memory.jsonl"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


# ---------- Agent Loop 预算 ----------
MAX_STEPS = _env_int("AGENT_MAX_STEPS", 10)
MAX_TOOL_CALLS = _env_int("AGENT_MAX_TOOL_CALLS", 14)
MAX_PROMPT_TOKENS = _env_int("AGENT_MAX_PROMPT_TOKENS", 120_000)
MAX_WALL_CLOCK_S = _env_float("AGENT_MAX_WALL_CLOCK_S", 300.0)
REPEAT_ACTION_LIMIT = _env_int("AGENT_REPEAT_ACTION_LIMIT", 2)
STRUCTURED_RETRY_LIMIT = _env_int("AGENT_STRUCTURED_RETRY_LIMIT", 2)
TEMPERATURE = _env_float("AGENT_TEMPERATURE", 0.1)

#: 当模型不支持原生 function calling 时，是否降级为文本 ReAct 协议
TEXT_REACT_FALLBACK = _env_bool("AGENT_TEXT_REACT_FALLBACK", True)

# ---------- 工具开关 ----------
ENABLE_PYLINT = _env_bool("AGENT_ENABLE_PYLINT", True)
ENABLE_BANDIT = _env_bool("AGENT_ENABLE_BANDIT", True)
ENABLE_SANDBOX = _env_bool("AGENT_ENABLE_SANDBOX", True)
SANDBOX_TIMEOUT_S = _env_float("AGENT_SANDBOX_TIMEOUT_S", 10.0)
SANDBOX_MAX_OUTPUT_CHARS = _env_int("AGENT_SANDBOX_MAX_OUTPUT_CHARS", 8000)

#: 允许 Agent 索引的代码库根目录（代码库检索工具的工作范围，留空则禁用该工具）
CODEBASE_ROOT = os.getenv("AGENT_CODEBASE_ROOT", "").strip()

# ---------- 模型定价（美元 / 1000 tokens） ----------
# 说明：默认 0 表示「不计算成本」，请按实际采购价格填写，避免出现编造的数字。
PRICE_PROMPT_PER_1K = _env_float("AGENT_PRICE_PROMPT_PER_1K", 0.0)
PRICE_COMPLETION_PER_1K = _env_float("AGENT_PRICE_COMPLETION_PER_1K", 0.0)
