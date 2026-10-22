"""全局配置。

**密钥一律从环境变量 / .env 读取，绝不写进源码。**
任何写进源码的密钥都等同于已泄露，必须改为通过环境变量注入。

参考 ``.env.example`` 创建本地 ``.env``：

.. code-block:: ini

    API_KEY=你的密钥
    API_URL=https://ark.cn-beijing.volces.com/api/v3/chat/completions
    MODEL_NAME=你的模型endpoint-id
    MOCK_MODE=false
"""

import logging
import os

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 模型接入配置（全部来自环境变量）
# ---------------------------------------------------------------------------

#: 同时接受 ARK_API_KEY 作为别名
API_KEY = (os.getenv("API_KEY") or os.getenv("ARK_API_KEY") or "").strip()

API_URL = os.getenv("API_URL") or "https://ark.cn-beijing.volces.com/api/v3/chat/completions"

#: 模型 endpoint / 模型名。请在 .env 中配置，例如火山方舟的 ep-xxxxxxxx 形式 ID。
MODEL_NAME = (os.getenv("MODEL_NAME") or "").strip()

# ---------------------------------------------------------------------------
# 路径配置
# ---------------------------------------------------------------------------

REPORT_DIR = "reports"
LOG_DIR = "logs"
TEMP_DIR = "temp"

# ---------------------------------------------------------------------------
# 调用配置
# ---------------------------------------------------------------------------

TIMEOUT = 60
RETRY_TIMES = 3
SLEEP_INTERVAL = 0.5
ENCODING = "utf-8"

#: 模拟模式：不调用真实模型，使用 agent/mock.py 的离线后端，
#: 便于在没有密钥或不想消耗额度时跑通全流程。
MOCK_MODE = (os.getenv("MOCK_MODE") or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def missing_credentials() -> list[str]:
    """返回缺失的必需配置项，便于启动时给出明确提示而不是让请求失败。"""
    missing: list[str] = []
    if not MOCK_MODE:
        if not API_KEY:
            missing.append("API_KEY")
        if not MODEL_NAME:
            missing.append("MODEL_NAME")
    return missing


if missing_credentials() and not MOCK_MODE:
    # 这里只提示，不抛异常：导入 config 的场景很多（例如跑离线 rules 评测），
    # 真正需要调用模型时 LLMClient 会给出明确的报错。
    logger.warning(
        "缺少模型配置 %s。请在 .env 中补齐，或设置 MOCK_MODE=true 使用离线模拟后端。",
        missing_credentials(),
    )
