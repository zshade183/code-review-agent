#!/bin/bash

# ==============================================================================
# AI Code Review CI/CD Integration Template
# ==============================================================================
# 使用说明:
# 1. 确保已安装 Python 环境及 requirements.txt 中的依赖。
# 2. 将此脚本放入您的 CI 流水线步骤中。
# 3. 配置环境变量 API_KEY (用于真实 AI 审查)。

FILE_TO_REVIEW=$1

if [ -z "$FILE_TO_REVIEW" ]; then
    echo "Usage: ./ci_integrate.sh <file_path>"
    exit 1
fi

echo "--- Starting AI Code Review for: $FILE_TO_REVIEW ---"

# 运行 CLI 工具
# --fail-on-high-risk: 如果发现高风险问题，退出码将为 1，从而中断 CI 流水线
python cli.py "$FILE_TO_REVIEW" --fail-on-high-risk --format text

EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
    echo "--- AI Code Review Passed! ---"
    exit 0
else
    echo "--- AI Code Review Failed (High Risk Issues Found) ---"
    exit 1
fi
