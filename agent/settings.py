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
MEMORY_PATH = os.getenv("AGENT_MEMORY_PATH", "memory/agent_memory.db")
WORKFLOW_DB_PATH = os.getenv("AGENT_WORKFLOW_DB_PATH", "memory/agent_workflows.db")


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


# ---------- API 生产防护（默认关闭，保持本地开发兼容） ----------
API_KEY_AUTH = os.getenv("AGENT_SERVICE_API_KEY", "").strip()
API_RATE_LIMIT_PER_MINUTE = _env_int("AGENT_RATE_LIMIT_PER_MINUTE", 0)


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

# ---------- 多 Agent 主线 ----------
#: 默认审查策略：fast=单 Agent；auto=按证据选择领域 Reviewer；full=四领域 + Critic + Verifier
DEFAULT_STRATEGY = (os.getenv("AGENT_DEFAULT_STRATEGY") or "auto").strip().lower()
#: 单个领域 Reviewer 的超时（秒）。超时不算整体失败，只降低覆盖率。
REVIEWER_TIMEOUT_S = _env_float("AGENT_REVIEWER_TIMEOUT_S", 180.0)
#: 领域 Reviewer 失败后的重试次数（仅对异常生效，超时不重试）
REVIEWER_RETRIES = _env_int("AGENT_REVIEWER_RETRIES", 1)
#: 选择性 Critic 的超时（秒）
CRITIC_TIMEOUT_S = _env_float("AGENT_CRITIC_TIMEOUT_S", 120.0)
#: 独立 Verifier 允许的最长执行时间（秒）
VERIFIER_TIMEOUT_S = _env_float("AGENT_VERIFIER_TIMEOUT_S", 90.0)
#: 自动策略下最多启动多少个领域 Reviewer
AUTO_MAX_REVIEWERS = _env_int("AGENT_AUTO_MAX_REVIEWERS", 4)
#: 最终裁决中「无独立佐证但可确认」的置信度下限
ADJUDICATION_CONFIRM_CONFIDENCE = _env_float("AGENT_ADJUDICATION_CONFIRM_CONFIDENCE", 0.65)
#: 进入选择性 Critic 的判据：置信度低于该值即复核
CRITIC_LOW_CONFIDENCE = _env_float("AGENT_CRITIC_LOW_CONFIDENCE", 0.7)
#: 进入选择性 Critic 的判据：严重性不低于该值即复核
CRITIC_FORCE_REVIEW_SEVERITIES = tuple(
    item.strip().upper()
    for item in (os.getenv("AGENT_CRITIC_FORCE_REVIEW_SEVERITIES") or "HIGH,MEDIUM-HIGH").split(",")
    if item.strip()
)

# ---------- 工具开关 ----------
ENABLE_PYLINT = _env_bool("AGENT_ENABLE_PYLINT", True)
ENABLE_BANDIT = _env_bool("AGENT_ENABLE_BANDIT", True)
ENABLE_SANDBOX = _env_bool("AGENT_ENABLE_SANDBOX", True)
SANDBOX_TIMEOUT_S = _env_float("AGENT_SANDBOX_TIMEOUT_S", 10.0)
SANDBOX_MAX_OUTPUT_CHARS = _env_int("AGENT_SANDBOX_MAX_OUTPUT_CHARS", 8000)
SANDBOX_BACKEND = (os.getenv("AGENT_SANDBOX_BACKEND") or "subprocess").strip().lower()
SANDBOX_DOCKER_IMAGE = (os.getenv("AGENT_SANDBOX_DOCKER_IMAGE") or "python:3.11-alpine").strip()

#: 允许 Agent 索引的代码库根目录（代码库检索工具的工作范围，留空则禁用该工具）
CODEBASE_ROOT = os.getenv("AGENT_CODEBASE_ROOT", "").strip()

# ---------- 模型定价（美元 / 1000 tokens） ----------
# 说明：默认 0 表示「不计算成本」，请按实际采购价格填写，避免出现编造的数字。
PRICE_PROMPT_PER_1K = _env_float("AGENT_PRICE_PROMPT_PER_1K", 0.0)
PRICE_COMPLETION_PER_1K = _env_float("AGENT_PRICE_COMPLETION_PER_1K", 0.0)
