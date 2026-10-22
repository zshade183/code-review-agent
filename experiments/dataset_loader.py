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
        lang = str(item.get("language") or "").strip()
        if language and lang.lower() != language.lower():
            dropped_items += 1
            continue
        kept_items += 1
        issues: List[GtIssue] = []
        for it in item.get("issues", []) or []:
            if not isinstance(it, dict):
                continue
            loc = it.get("location") or {}
            issues.append(
                GtIssue(
                    category=str(it.get("category") or "").strip(),
                    subtype=str(it.get("subtype") or "").strip(),
                    severity=str(it.get("severity") or "").strip(),
                    description=str(it.get("description") or "").strip(),
                    line_start=int(loc.get("line_start") or 1),
                    line_end=int(loc.get("line_end") or int(loc.get("line_start") or 1)),
                )
            )
        samples.append(
            Sample(
                sample_id=str(item.get("id") or "").strip(),
                language=lang,
                code=str(item.get("code") or ""),
                fixed_code=str(item.get("fixed_code") or "") if item.get("fixed_code") is not None else None,
                issues=issues,
            )
        )

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
