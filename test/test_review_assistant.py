"""LangGraph 版审查助手的离线端到端测试。

用 mock 模型 + 脚本化确认器，验证整条链路：
主助手委派 → 四个维度子助手取证 → 结论校验合并 → 修复助手 → 人工确认中断 → 汇总报告。

运行：python test/test_review_assistant.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 离线跑，不需要「缺少模型配置」的提示
logging.getLogger("config").setLevel(logging.ERROR)

import review_assistant.workflow as workflow_module  # noqa: E402
from review_assistant.dimensions import DIMENSION_ASSISTANTS, FIX_ASSISTANT  # noqa: E402
from review_assistant.llm import create_llm  # noqa: E402
from review_assistant.main import make_config, run_turn  # noqa: E402
from review_assistant.toolset import build_toolset  # noqa: E402
from review_assistant.workflow import build_graph  # noqa: E402

TARGET = ROOT / "examples" / "review_target.py"


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {label}" + (f" -> {detail}" if detail else ""))
    if not condition:
        raise AssertionError(label)


def make_graph():
    workflow_module.VERBOSE = False
    toolset = build_toolset(codebase_root=str(ROOT))
    return build_graph(create_llm(provider="mock"), toolset), toolset


def test_review_and_reject(graph):
    config = make_config(
        target_path=str(TARGET),
        thread_id="offline-reject",
        use_memory=False,
    )
    seen: list = []

    def approver(name, _args):
        seen.append(name)
        return "测试脚本：第一次敏感操作拒绝"

    result = run_turn(graph, "请审查这个文件，然后修复发现的问题", config, approver=approver)

    report = result["report"]
    issues = report.get("issues") or []
    reviewed = result["reviewed_dimensions"]

    check("图跑到了汇总节点", bool(report), f"issues={len(issues)}")
    check(
        "四个维度 + 修复都已执行",
        all(name in reviewed for name in [*DIMENSION_ASSISTANTS, FIX_ASSISTANT]),
        str(reviewed),
    )
    check("产出了带证据的结论", bool(issues) and all(i.get("evidence") for i in issues), f"{len(issues)} 条")
    check(
        "每条结论都有校验结论",
        all(i.get("verified") is not None for i in issues),
    )
    check("敏感操作被请求确认", bool(seen), f"工具={seen}")
    check("用户拒绝后工具未执行", any("rejected" in str(_content(m)) for m in _messages(graph, config)))
    check("修复结果带有 harness 校验", bool(result["fix"].get("checks")), str(result["fix"].get("checks")))
    return result


def test_review_and_approve():
    graph, _ = make_graph()
    config = make_config(
        target_path=str(TARGET),
        thread_id="offline-approve",
        use_memory=False,
    )
    approved: list = []

    def approver(name, args):
        approved.append((name, args))
        return None

    result = run_turn(graph, "审查并修复这个文件", config, approver=approver)
    check("敏感操作已被放行", bool(approved), str([name for name, _ in approved]))
    check("修复结果已收集", bool(result["fix"]), str(result["fix"].get("summary")))
    syntax_checks = [c for c in result["fix"].get("checks") or [] if c.get("name") == "syntax"]
    check("修复代码通过语法校验", bool(syntax_checks) and syntax_checks[0]["passed"])
    return result


def _messages(graph, config):
    return graph.get_state(config).values.get("messages") or []


def _content(message) -> str:
    content = getattr(message, "content", "")
    return content if isinstance(content, str) else str(content)


def main() -> int:
    graph, _toolset = make_graph()
    result = test_review_and_reject(graph)

    print("\n--- 结构化报告预览 ---")
    print((result["report"].get("review_summary") or "")[:200])
    for issue in (result["report"].get("issues") or [])[:5]:
        print(f"  - [{issue['category']}] 行 {issue['line_number']} {issue['subtype']}")

    test_review_and_approve()
    print("\n全部离线用例通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
