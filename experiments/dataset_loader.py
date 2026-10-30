"""实验数据集加载器。

读取 ``dataset/*.json``（原 ``dataset_temp_1/``）：纯 Python 样本，带 GT 行号，
格式为 ``[{id, language, code, fixed_code, issues[{category, subtype, severity, location}]}]``。

``dataset/legacy/`` 存放不参与默认实验的混合语言样本，
需要时用 ``--dataset dataset/legacy/test.json`` 显式指定。

数据集文件可能被中途截断（大文件手工拼接导致），所以下面的解析带两级修复策略：
先按标准 JSON 解析；失败且只要 Python 样本时，截到第一个非 Python 样本之前再解析。
"""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


@dataclass(frozen=True)
class GtIssue:
    category: str
    subtype: str
    severity: str
    description: str
    line_start: int
    line_end: int


@dataclass(frozen=True)
class Sample:
    sample_id: str
    language: str
    code: str
    fixed_code: Optional[str]
    issues: List[GtIssue]


def _try_load_json_array(text: str) -> Optional[List[Dict[str, Any]]]:
    try:
        data = json.loads(text)
        return data if isinstance(data, list) else None
    except Exception:
        return None


def _cut_before_language(text: str, language_value: str) -> Optional[str]:
    m = re.search(r'"language"\s*:\s*"' + re.escape(language_value) + r'"', text)
    if not m:
        return None
    cut = text.rfind("}", 0, m.start())
    if cut == -1:
        return None
    prefix = text[: cut + 1].rstrip()
    if prefix.endswith(","):
        prefix = prefix[:-1]
    return prefix + "\n]"


def _parse_items(text: str, preferred_language: Optional[str]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    stats: Dict[str, Any] = {"strategy": "json", "total_items": 0, "kept_items": 0, "dropped_items": 0}
    data = _try_load_json_array(text)
    if data is not None:
        stats["total_items"] = len(data)
        return data, stats

    if preferred_language and preferred_language.lower() == "python":
        repaired = _cut_before_language(text, "Java")
        if repaired:
            data = _try_load_json_array(repaired)
            if data is not None:
                stats["strategy"] = "cut_before_java"
                stats["total_items"] = len(data)
                return data, stats

    raise json.JSONDecodeError(
        "数据集 JSON 非法，且无法用当前修复策略恢复（可先截断到最后一个完整对象）",
        text,
        0,
    )


def _parse_issues(raw: Any, fallback_category: str = "") -> List[GtIssue]:
    """把不同 schema 的标注统一成 ``GtIssue`` 列表。"""
    issues: List[GtIssue] = []
    if not isinstance(raw, list):
        return issues
    for item in raw:
        if not isinstance(item, dict):
            continue
        location = item.get("location") or {}
        line_start = location.get("line_start", item.get("line_start", item.get("line", 0)))
        line_end = location.get("line_end", item.get("line_end", line_start))
        try:
            line_start = int(line_start or 0)
            line_end = int(line_end or line_start or 0)
        except (TypeError, ValueError):
            line_start, line_end = 0, 0
        issues.append(
            GtIssue(
                category=str(item.get("category") or item.get("bug_category") or fallback_category).strip(),
                subtype=str(item.get("subtype") or item.get("sub_category") or item.get("category") or "").strip(),
                severity=str(item.get("severity") or item.get("risk_level") or "").strip(),
                description=str(item.get("description") or item.get("risk_description") or "").strip(),
                line_start=line_start,
                line_end=line_end,
            )
        )
    return issues


def _normalize_item(item: Any) -> Optional[Sample]:
    """把一条原始样本归一到 ``Sample``；无法识别时返回 ``None``。

    支持三种历史 schema：

    1. ``{id, language, code, fixed_code, issues:[{category, subtype, location}]}``（当前）
    2. ``{code_id, language, code:{buggy, fixed}, issue:{category, sub_category, line}}``（BugSinPy）
    3. ``{id, buggy_code, fixed_code, bug_category, bug_sub_category}``（扁平转换）
    """
    if not isinstance(item, dict):
        return None

    raw_code = item.get("code")
    fixed_code = item.get("fixed_code")
    if isinstance(raw_code, dict):
        fixed_code = raw_code.get("fixed") or fixed_code
        raw_code = raw_code.get("buggy") or raw_code.get("original") or ""
    code = raw_code if isinstance(raw_code, str) else ""
    if not code:
        code = str(item.get("buggy_code") or "")
    if not code:
        return None

    issues_raw = item.get("issues")
    if isinstance(issues_raw, list) and issues_raw:
        issues = _parse_issues(issues_raw, str(item.get("bug_category") or ""))
    elif isinstance(item.get("issue"), dict):
        issues = _parse_issues([item["issue"]], str(item.get("bug_category") or ""))
    elif item.get("bug_category"):
        issues = _parse_issues(
            [
                {
                    "category": item.get("bug_category"),
                    "sub_category": item.get("bug_sub_category"),
                    "description": item.get("description"),
                    "line": item.get("line") or item.get("line_start"),
                }
            ],
            str(item.get("bug_category") or ""),
        )
    else:
        issues = []

    sample_id = str(item.get("id") or item.get("code_id") or item.get("sample_id") or "").strip()
    return Sample(
        sample_id=sample_id or f"sample-{abs(hash(code)) % 10**8}",
        language=str(item.get("language") or "Python").strip(),
        code=code,
        fixed_code=fixed_code if isinstance(fixed_code, str) else None,
        issues=issues,
    )


def load_dataset(
    path: Path,
    language: Optional[str] = "python",
    max_samples: int = 0,
    seed: int = 42,
) -> Tuple[List[Sample], Dict[str, Any]]:
    """加载数据集，返回 (样本列表, 统计信息)。"""
    text = path.read_text(encoding="utf-8", errors="ignore")
    items, parse_stats = _parse_items(text, preferred_language=language)

    total_items = len(items)
    kept_items = 0
    dropped_items = 0

    samples: List[Sample] = []
    for item in items:
        sample = _normalize_item(item)
        if sample is None:
            dropped_items += 1
            continue
        lang = sample.language
        if language and lang.lower() != language.lower():
            dropped_items += 1
            continue
        kept_items += 1
        samples.append(sample)

    rnd = random.Random(seed)
    rnd.shuffle(samples)
    if max_samples and max_samples > 0:
        samples = samples[:max_samples]

    if isinstance(parse_stats, dict):
        parse_stats["total_items"] = int(parse_stats.get("total_items") or total_items)
        parse_stats["kept_items"] = int(kept_items)
        parse_stats["dropped_items"] = int(dropped_items)

    stats: Dict[str, Any] = {
        "path": str(path),
        "language": language,
        "samples": len(samples),
        "parse": parse_stats,
    }
    return samples, stats


#: 别名：外部脚本可能仍按 ``load_dataset_temp`` 调用
load_dataset_temp = load_dataset
