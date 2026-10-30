"""实验 1：基线工具、单次模型调用与完整 Agent 的问题识别效果对比。

实现集中在 :mod:`experiments.agent_eval`，保留此文件名作为项目原有实验 1 的稳定入口。
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from experiments.agent_eval import main


if __name__ == "__main__":
    main()
