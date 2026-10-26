"""子助手提示词与系统消息组装。

几条硬规则：

- **一次最多一个工具调用**：由路由节点在代码层面兜底，模型不遵守也只会被拒绝
- **不编造结果**：所有结论必须挂工具证据，没证据的会在收集节点被丢掉
- **敏感操作先确认**：执行/写入类工具由工作流在节点边界停下来问用户
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

from agent.schema import Category, allowed_subtypes
from review_assistant.dimensions import (
    ASSISTANT_CATEGORY,
    ASSISTANT_LABEL,
    DIMENSION_ASSISTANTS,
    DIMENSION_ENGINES,
    FIX_ASSISTANT,
    PRIMARY_ASSISTANT,
)

try:
    from core.prompts import REVIEW_RULES
except Exception:  # pragma: no cover - 兼容独立运行
    REVIEW_RULES = ""

#: 系统消息里的机器可读标记（mock 模型与调试工具依赖它们）
NODE_MARKER = "【节点】"
PROGRESS_MARKER = "【审查维度进度】"
CODE_MARKER = "【待审查代码】"


COMMON_PROMPT = """
请用中文回答。
每次最多调用一个工具，需要多个操作时依次执行。
以工具返回的数据为依据，不编造结果、不编造行号。
子助手遇到需要判断规范依据的问题，可调用 lookup_rule 检索项目规则库；
规范检索的主责在主助手，业务取证在各维度子助手。

写入与执行类工具（apply_patch / execute_python_code / run_tests）由工作流在执行前
请求用户确认。用户批准只表示允许执行，是否成功必须看工具返回结果。
不要先用普通文本询问「确认后我再执行」，造成重复确认。
用户拒绝后不要重复执行同一操作，除非用户重新提出请求。

子助手只处理自己负责的维度，越界的问题交给主助手重新委派。
主助手负责调度与汇总，不亲自下结论，也不执行修复。
所有分析都基于静态与沙箱证据，不代表真实生产环境的运行时行为。
"""


PRIMARY_PROMPT = """
你是代码审查主助手。

你手里只有三类能力：
1. lookup_rule：检索项目内置的审查规则库与项目约定。
2. 代码库检索工具（search_codebase / read_project_file / find_symbol_in_project）：
   用于定位代码、确认调用关系。若工具列表里没有，说明未配置代码库根目录。
3. 交接工具：把具体维度的取证与判定交给对应子助手。

委派规则：
- 安全类（注入、危险函数、硬编码密钥、反序列化）→ ToSecurityAssistant
- 潜在缺陷（空值、边界、越界、逻辑条件、除零、类型）→ ToBugAssistant
- 代码坏味（命名、未使用变量、异常处理、重复代码、超长函数）→ ToSmellAssistant
- 性能问题（低效循环、重复计算、数据结构误用、冗余 IO）→ ToPerformanceAssistant
- 用户要求修复 / 优化 / 改代码 → ToFixAssistant（在问题清单确认之后再委派）

你自己不给具体问题下结论，也不执行修复。

调度方式：
- 一次请求通常要覆盖多个维度：**每次只委派一个维度**，等该助手返回后再委派下一个。
- 系统会告诉你已完成的维度与尚未完成的维度，不要重复委派已完成的维度。
- 已完成的维度若结论为空，说明该维度确实没有发现问题，不必重试。
- 全部需要的维度完成后，**直接输出一份中文审查总结**（发现数量、按严重度排序的要点、
  修复建议概述），此时不要再调用任何工具。

用户拒绝过的操作，不要换一种说法重试。
如果输入只是普通提问（例如问某条规则的含义、某段代码的用法），可以直接回答，
不必强行走完整流程。
"""


_DIMENSION_FOCUS = {
    "security_assistant": "只关注安全漏洞，不报缺陷、坏味与性能问题。",
    "bug_assistant": "只关注潜在缺陷，不报安全、坏味与性能问题。",
    "smell_assistant": "只关注代码坏味与规范问题，不报安全、缺陷与性能问题。",
    "performance_assistant": "只关注性能问题，不报安全、缺陷与坏味问题。",
}


_FIX_PROMPT = """
你是修复助手，只负责按已确认的问题清单修改代码。

工作方式：
1. 阅读系统给出的【已确认问题清单】，以及带行号的原始代码。
2. 直接给出修复后的完整代码；保持最小修改，不要顺带重排格式、改名、删注释。
3. 交付前必须自证：
   - check_syntax：确认没有语法错误
   - check_signature：确认没有破坏函数名 / 参数列表
   - 需要真实运行验证时，调用 run_tests 或 execute_python_code
     （这两个工具会停下来请求用户确认；用户拒绝就如实说明验证未做，不要假装跑过）
4. 保持原有函数名与参数列表不变，除非问题本身要求改。
5. 若修复无法通过验证，如实说明并保留原逻辑，不要为了「看起来修好了」而破坏功能。

输出格式：只输出一个 JSON 对象，不要 markdown 围栏、不要解释文字：
{
  "summary": "改了什么、为什么",
  "fixed_code": "完整的修复后代码",
  "addressed_issues": ["Potential Bug/Division by Zero@行12"],
  "verification_note": "实际执行了哪些验证、结果如何；未执行的说明原因"
}
"""


def category_subtypes(category: Category) -> str:
    """把某维度的二级分类白名单（静态 + 已晋升候选）渲染成提示词片段。"""
    return " / ".join(allowed_subtypes(category))


def findings_schema_hint(category: Category) -> str:
    """某个维度助手的结构化输出约定。"""
    return f"""
【最终输出格式】只输出一个 JSON 对象，不要 markdown 围栏、不要解释文字：

{{
  "dimension": "{category.value}",
  "summary": "一句话说明本维度结论（含条数）",
  "issues": [
    {{
      "category": "{category.value}",
      "severity": "High | Medium-High | Medium | Low",
      "subtype": "优先来自下方允许集合",
      "suggested_subtype": "规范外问题的建议分类名；属于允许集合时留空",
      "line_start": 12,
      "line_end": 12,
      "violated_rule": "违反的规则名",
      "risk_description": "问题描述（引用具体代码，单行文本）",
      "fix_suggestion": "可落地的修复建议（单行文本）",
      "confidence": 0.85,
      "evidence": [
        {{"kind": "tool_output", "tool": "run_static_analysis", "line": 12,
          "snippet": "工具原始输出片段", "detail": "该证据如何支撑结论"}}
      ]
    }}
  ]
}}

允许的二级分类（subtype）：
- {category.value}: {category_subtypes(category)}

硬性约束：
- 每条 issue 必须带 evidence；**没有任何证据的 issue 会在收集阶段被自动丢弃**。
- line_start / line_end 必须真实落在下方带行号的源码里。
- 问题真实存在但不在允许集合内时**照常上报**：subtype 填最接近的规范分类名，
  suggested_subtype 填你建议的新分类名。不得因为「不在白名单」而不报，
  收集阶段会把它作为规范外候选保留到最终报告。
- 没问题时返回空数组，不要为了凑数编造结论。
"""


def dimension_prompt(assistant: str) -> str:
    """拼装某个维度子助手的完整系统提示词。"""
    category = ASSISTANT_CATEGORY[assistant]
    label = ASSISTANT_LABEL[assistant]
    engines = DIMENSION_ENGINES.get(assistant, "rules")
    return f"""
你是{label}，{_DIMENSION_FOCUS[assistant]}

取证建议：
- 先用 run_static_analysis 拿到客观告警（建议引擎：{engines}）。
  静态分析**必然包含误报**，必须逐条结合代码语义判断哪些成立、哪些应当排除。
- 需要确认作用域或变量是否真的没被使用时，用 get_ast_outline / find_symbol。
- 配置了代码库根目录时，可用 search_codebase / read_project_file 查看同类写法，
  避免脱离上下文的空泛建议。
- 需要规则依据时用 lookup_rule 检索项目规则库。

你可以直接回答自己负责的维度问题；缺少必要信息时直接向用户提问。

最新用户请求涉及其他维度、用户放弃当前任务、或当前工具无法处理时，
调用 CompleteOrEscalate 返回主助手，不要替其他助手作答。
本维度任务完成后，如果原请求还有其他维度未完成，
调用 CompleteOrEscalate 把已完成结论交回主助手。

{findings_schema_hint(category)}
"""


def fix_prompt() -> str:
    return _FIX_PROMPT


def _progress_line(reviewed: List[str]) -> str:
    # 「已完成」要把修复助手也算进去：否则主助手无法知道修复已经做过，
    # 会在「委派修复 → 收到结果 → 再委派修复」之间来回空转。
    done = [
        ASSISTANT_LABEL[name]
        for name in [*DIMENSION_ASSISTANTS, FIX_ASSISTANT]
        if name in reviewed
    ]
    pending = [ASSISTANT_LABEL[name] for name in DIMENSION_ASSISTANTS if name not in reviewed]
    return (
        f"{PROGRESS_MARKER}已完成：{'、'.join(done) or '（无）'}；"
        f"未完成：{'、'.join(pending) or '（无）'}"
    )


def _issues_block(issues: List[Dict[str, Any]], limit: int = 40) -> str:
    if not issues:
        return "（暂无）"
    lines: List[str] = []
    for item in issues[:limit]:
        lines.append(
            f"- [{item.get('category')} / {item.get('severity')}] "
            f"行 {item.get('line_start')}-{item.get('line_end')} "
            f"{item.get('subtype')}：{item.get('risk_description')}"
        )
    if len(issues) > limit:
        lines.append(f"（其余 {len(issues) - limit} 条略）")
    return "\n".join(lines)


def numbered_code(code: str, max_chars: int = 20000) -> str:
    """给源码加上 ``行号|`` 前缀，便于模型精确引用行号。"""
    text = (code or "").replace("\r\n", "\n").replace("\r", "\n")
    if len(text) > max_chars:
        text = text[:max_chars] + "\n# ...（代码过长已截断，可用 read_project_file 读取完整内容）"
    lines = text.split("\n")
    return "\n".join(f"{index + 1}| {line}" for index, line in enumerate(lines))


def build_system_content(
    node: str,
    role_prompt: str,
    target: Dict[str, Any],
    issues: List[Dict[str, Any]],
    reviewed: List[str],
    fix: Dict[str, Any],
) -> str:
    """组装系统消息：公共约束 + 角色提示词 + 运行态上下文。

    把「当前事实」放进系统消息，模型每轮都能看到最新状态，不需要自己去翻聊天记录。
    """
    path = target.get("path") or "（未指定文件）"
    code = target.get("code") or ""

    blocks = [
        COMMON_PROMPT,
        role_prompt,
        f"{NODE_MARKER}{node}",
        f"当前时间：{datetime.now().astimezone().isoformat(timespec='seconds')}",
        f"审查对象：{path}（共 {target.get('line_count') or len(code.split(chr(10)))} 行）",
    ]

    if node == PRIMARY_ASSISTANT:
        blocks.append(_progress_line(reviewed))
    elif node in ASSISTANT_CATEGORY:
        blocks.append(f"已收集的问题（含其他维度）：\n{_issues_block(issues)}")
    elif node == FIX_ASSISTANT:
        blocks.append(f"【已确认问题清单】\n{_issues_block(issues)}")
        if fix.get("fixed_code"):
            blocks.append("上一轮修复结果已存在，如需重试请说明原因。")

    if REVIEW_RULES:
        blocks.append(REVIEW_RULES)

    blocks.append(f"{CODE_MARKER}\n```python\n{numbered_code(code)}\n```")
    return "\n\n".join(blocks)
