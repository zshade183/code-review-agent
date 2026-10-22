"""web/app.py 的无头端到端测试。

流程：临时起一个 MOCK_MODE 的后端 → 用 Streamlit 官方 AppTest 加载前端脚本 →
填入后端地址 → 点击「开始审查」→ 断言页面没有异常且渲染出了问题表。

运行：``python test/test_web_app.py``

说明：这是本项目里唯一需要「前后端一起跑」的测试，因此它会自己拉起后端再关掉，
不依赖你已经手动启动了服务。
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 必须在导入 config / api.main 之前设置，否则拿到的是真实模型配置
os.environ["MOCK_MODE"] = "true"
os.environ.setdefault("PYTHONIOENCODING", "utf-8")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def start_backend(port: int):
    """在线程里跑 uvicorn，返回 (server, thread)。"""
    import uvicorn

    from api.main import app

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return server, thread


def wait_for_backend(port: int, timeout: float = 25.0) -> bool:
    import requests

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if requests.get(f"http://127.0.0.1:{port}/", timeout=1).status_code == 200:
                return True
        except Exception:  # noqa: BLE001 - 启动期间连不上是正常的
            pass
        time.sleep(0.3)
    return False


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f" -> {detail}" if detail else ""))
    if not ok:
        raise AssertionError(label)


def main() -> int:
    from streamlit.testing.v1 import AppTest

    port = free_port()
    server, _thread = start_backend(port)
    try:
        check("后端已启动（MOCK 模式）", wait_for_backend(port), f"http://127.0.0.1:{port}")

        app = AppTest.from_file(str(ROOT / "web" / "app.py"), default_timeout=90)
        app.run()
        check("前端首次渲染无异常", not app.exception, str(app.exception[0].value if app.exception else ""))
        check("侧边栏存在 API 地址输入框", len(app.sidebar.text_input) >= 1)

        app.sidebar.text_input[0].set_value(f"http://127.0.0.1:{port}")
        app.run()
        check("切换后端地址后仍无异常", not app.exception)

        # 填入待审查代码
        app.text_area[0].set_value(
            "import os\n\n\ndef run(cmd):\n    return os.system(cmd)\n\n\ndef average(items):\n"
            "    total = 0\n    for item in items:\n        total += item\n    return total / len(items)\n"
        )
        app.run()

        buttons = app.button
        check("页面上有可点击的按钮", len(buttons) >= 1, f"buttons={len(buttons)}")
        buttons[0].click()
        app.run()

        check("点击「开始审查」后无异常", not app.exception, str(app.exception[0].value if app.exception else ""))

        result = app.session_state.get("review_result") or {}
        report = result.get("report") or {}
        issues = report.get("issues") or []
        check("拿到结构化审查报告", bool(report), f"issues={len(issues)}")
        check("报告里至少有一条带证据的结论", bool(issues) and all(i.get("evidence") for i in issues))
        check("页面渲染出审查结果区", any("审查结果" in str(m.value) for m in app.markdown))
        return 0
    finally:
        server.should_exit = True
        time.sleep(1.0)


if __name__ == "__main__":
    raise SystemExit(main())
