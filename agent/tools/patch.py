"""补丁类工具。

让 Agent 以 **patch（最小改动）** 而不是「重写整个文件」的方式交付修复方案。

这一点在代码审查场景里非常关键：让模型直接重写整个文件，它会顺手改格式、改命名、
删注释，产生大量与本次问题无关的 diff，既难以 review 也破坏了「最小修改」约束。
改为「生成 patch → 校验 patch 只触及问题行 → 应用」后，产出变得可审计。
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from agent.schema import ToolResult
from agent.tools.base import Tool

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", re.M)


@dataclass
class PatchStats:
    added: int
    removed: int
    hunks: int
    total_lines: int

    @property
    def change_ratio(self) -> float:
        return (self.added + self.removed) / self.total_lines if self.total_lines else 0.0


def make_unified_diff(original: str, modified: str, filename: str = "snippet.py", context: int = 3) -> str:
    """生成标准 unified diff。"""
    original_lines = original.splitlines(keepends=True)
    modified_lines = modified.splitlines(keepends=True)
    diff = difflib.unified_diff(
        original_lines,
        modified_lines,
        fromfile=f"a/{filename}",
        tofile=f"b/{filename}",
        n=context,
    )
    return "".join(diff)


def patch_stats(patch_text: str, total_lines: int) -> PatchStats:
    added = 0
    removed = 0
    hunks = 0
    for line in patch_text.splitlines():
        if line.startswith("@@ "):
            hunks += 1
        elif line.startswith("+++") or line.startswith("---"):
            continue
        elif line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return PatchStats(added=added, removed=removed, hunks=hunks, total_lines=total_lines)


def parse_hunks(patch_text: str) -> List[Dict[str, Any]]:
    """解析 unified diff 的 hunk 列表。"""
    hunks: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    for raw in patch_text.splitlines():
        match = _HUNK_RE.match(raw)
        if match:
            if current is not None:
                hunks.append(current)
            current = {
                "orig_start": int(match.group(1)),
                "orig_count": int(match.group(2) or 1),
                "new_start": int(match.group(3)),
                "new_count": int(match.group(4) or 1),
                "lines": [],
            }
            continue
        if current is None:
            continue
        if raw.startswith("\\"):
            continue
        if raw[:1] in (" ", "+", "-") or raw == "":
            current["lines"].append(raw if raw else " ")
    if current is not None:
        hunks.append(current)
    return hunks


class PatchApplyError(RuntimeError):
    """补丁无法应用。"""


def apply_unified_diff(original: str, patch_text: str) -> str:
    """应用 unified diff，上下文不匹配时抛错（不会静默产出错误结果）。"""
    original_lines = original.splitlines(keepends=True)
    hunks = parse_hunks(patch_text)
    if not hunks:
        raise PatchApplyError("补丁中未解析到任何 hunk")

    result: List[str] = []
    cursor = 0
    for hunk_index, hunk in enumerate(hunks):
        target = hunk["orig_start"] - 1
        if target < cursor:
            raise PatchApplyError(f"第 {hunk_index + 1} 个 hunk 起始行 {target + 1} 早于当前位置，补丁顺序异常")
        if target > len(original_lines):
            raise PatchApplyError(f"第 {hunk_index + 1} 个 hunk 起始行 {target + 1} 超出文件长度 {len(original_lines)}")

        result.extend(original_lines[cursor:target])
        cursor = target

        for line in hunk["lines"]:
            tag, content = line[:1], line[1:]
            # 补 hunk 内的换行符
            if content and not content.endswith("\n") and cursor < len(original_lines):
                pass
            if tag == " ":
                if cursor >= len(original_lines) or original_lines[cursor].rstrip("\n") != content.rstrip("\n"):
                    actual = original_lines[cursor].rstrip("\n") if cursor < len(original_lines) else "<文件结束>"
                    raise PatchApplyError(
                        f"第 {hunk_index + 1} 个 hunk 上下文不匹配：期望 '{content.rstrip(chr(10))}'，实际 '{actual}'"
                    )
                result.append(original_lines[cursor])
                cursor += 1
            elif tag == "-":
                if cursor >= len(original_lines) or original_lines[cursor].rstrip("\n") != content.rstrip("\n"):
                    actual = original_lines[cursor].rstrip("\n") if cursor < len(original_lines) else "<文件结束>"
                    raise PatchApplyError(
                        f"第 {hunk_index + 1} 个 hunk 待删除行不匹配：期望 '{content.rstrip(chr(10))}'，实际 '{actual}'"
                    )
                cursor += 1
            elif tag == "+":
                result.append(content + ("\n" if not content.endswith("\n") else ""))

    result.extend(original_lines[cursor:])
    return "".join(result)


class CreatePatchTool(Tool):
    name = "create_patch"
    description = (
        "对比原始代码与修改后代码，生成标准 unified diff 补丁，并给出改动规模统计。"
        "生成修复方案后应调用本工具产出补丁，而不是直接返回整段代码，以便评审时只看改动部分。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "original_code": {"type": "string", "description": "原始代码"},
            "modified_code": {"type": "string", "description": "修改后的代码"},
            "filename": {"type": "string", "description": "文件名，用于 diff 头", "default": "snippet.py"},
        },
        "required": ["original_code", "modified_code"],
    }

    def run(self, original_code: str, modified_code: str, filename: str = "snippet.py") -> ToolResult:
        if original_code.strip() == modified_code.strip():
            payload = {"changed": False, "note": "两份代码完全一致，无需改动。", "patch": "", "stats": {}}
            return ToolResult(tool=self.name, ok=True, content=json.dumps(payload, ensure_ascii=False), data=payload)

        patch = make_unified_diff(original_code, modified_code, filename)
        stats = patch_stats(patch, max(1, len(original_code.split("\n"))))
        payload = {
            "changed": True,
            "patch": patch,
            "stats": {
                "added": stats.added,
                "removed": stats.removed,
                "hunks": stats.hunks,
                "total_lines": stats.total_lines,
                "change_ratio": round(stats.change_ratio, 4),
            },
            "warning": (
                "改动比例超过 40%，已偏离「最小修改」原则，建议确认是否只修改了必要行。"
                if stats.change_ratio > 0.4
                else ""
            ),
        }
        return ToolResult(tool=self.name, ok=True, content=json.dumps(payload, ensure_ascii=False, indent=2), data=payload)


class ApplyPatchTool(Tool):
    name = "apply_patch"
    mutating = True  # 会改写代码产物（以返回新代码的方式，不直接落盘）
    description = (
        "把 unified diff 补丁应用到原始代码上，返回应用后的完整代码。"
        "上下文不匹配时会明确报错。生成补丁后可用本工具确认补丁确实可应用。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "original_code": {"type": "string", "description": "原始代码"},
            "patch": {"type": "string", "description": "unified diff 文本"},
        },
        "required": ["original_code", "patch"],
    }

    def run(self, original_code: str, patch: str) -> ToolResult:
        try:
            applied = apply_unified_diff(original_code, patch)
        except PatchApplyError as exc:
            return ToolResult(
                tool=self.name,
                ok=False,
                error=str(exc),
                content=f"补丁应用失败：{exc}",
                data={"applied": False},
            )
        payload = {"applied": True, "code": applied, "lines": len(applied.split("\n"))}
        return ToolResult(
            tool=self.name,
            ok=True,
            content=json.dumps({"applied": True, "lines": payload["lines"]}, ensure_ascii=False),
            data=payload,
        )
