"""Agent 评测 runner。

用法::

    # 离线基线：只用静态分析工具，不调用 LLM（零成本，可先验证数据集与指标口径）
    python experiments/agent_eval.py --mode rules --dataset data/benchmark/dataset.jsonl

    # 单 Agent（带记忆与证据闭环）
    python experiments/agent_eval.py --mode single --max-samples 50 --max-workers 4

    # 多智能体协作
    python experiments/agent_eval.py --mode multi --max-samples 20

    # 查看历史轨迹的聚合统计
    python cli.py stats

与旧实验的关系：``--mode rules`` 复用了与 ``exp1_recognition.py`` 相同的匹配口径与
同一批数据集，因此新旧结果可以直接对比（旧实验的 ``static`` 基线 ≈ 这里的 ``rules``）。
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.llm import LLMClient
from agent.schema import ReviewReport
from agent.tools.static_analysis import run_bandit, run_pylint, run_rules
from agent.trace import TraceRecorder, estimate_cost
from experiments.agent_metrics import aggregate, evaluate_report, format_summary, gt_has_lines


class ThreadLocalLLM:
    """每线程一个 LLMClient。

    ``requests.Session`` 并非严格线程安全，且并发下共用连接池容易互相干扰；
    这里按线程持有独立客户端，避免加锁串行化的同时保证正确性。
    """

    def __init__(self) -> None:
        self._local = threading.local()

    @property
    def client(self) -> LLMClient:
        client = getattr(self._local, "client", None)
        if client is None:
            client = LLMClient()
            self._local.client = client
        return client

    def chat(self, *args: Any, **kwargs: Any) -> Any:
        return self.client.chat(*args, **kwargs)

    def chat_stream(self, *args: Any, **kwargs: Any) -> Any:
        return self.client.chat_stream(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.client, name)


def _rules_only_report(code: str) -> ReviewReport:
    """离线基线：只用静态分析（规则引擎 + pylint + bandit），不调用 LLM。"""
    issues = run_rules(code) + run_pylint(code) + run_bandit(code)
    deduped: Dict[tuple, Any] = {}
    for issue in issues:
        key = (issue.category.value, issue.subtype, issue.line_start)
        if key not in deduped or issue.confidence > deduped[key].confidence:
            deduped[key] = issue
    kept = sorted(deduped.values(), key=lambda i: i.line_start)
    return ReviewReport(review_summary=f"rules-only: {len(kept)} issues", issues=kept)


def _build_agents(args: argparse.Namespace):
    """构建 Agent 工厂。

    注册表（含代码库索引）在线程池外一次性构建，避免多线程各建一份、重复扫描仓库；
    LLM 客户端则按线程持有（见 ``ThreadLocalLLM``）。
    """
    from agent.memory import MemoryStore
    from agent.multi_agent import MultiAgentReviewPipeline
    from agent.reviewer_agent import ReviewerAgent
    from agent.tools import build_reviewer_registry

    registry = build_reviewer_registry(args.codebase_root)
    client = ThreadLocalLLM()
    memory = None if args.no_memory else MemoryStore()

    if args.mode == "multi":
        return MultiAgentReviewPipeline(
            client=client, codebase_root=args.codebase_root, persist_traces=args.persist_traces
        )
    return ReviewerAgent(
        client=client,
        registry=registry,
        codebase_root=args.codebase_root,
        memory=memory,
        project=args.project,
    )


def _run_one(agent: Any, sample: EvalSample, args: argparse.Namespace) -> Dict[str, Any]:
    ground_truth = list(sample.issues)
    match_level = args.match_level
    try:
        if args.mode == "rules":
            report = _rules_only_report(sample.code)
            return evaluate_report(
                report, ground_truth, run=None, sample_id=sample.sample_id, match_level=match_level
            )

        if args.mode == "multi":
            result = agent.review(sample.code, file_path=f"{sample.sample_id}.py")
            return {
                **evaluate_report(
                    result.report,
                    ground_truth,
                    run=None,
                    sample_id=sample.sample_id,
                    match_level=match_level,
                ),
                **{f"multi_{k}": v for k, v in result.stats.items()},
            }

        trace = TraceRecorder(task="review") if args.persist_traces else None
        report, outcome = agent.review(sample.code, trace=trace)
        row = evaluate_report(
            report,
            ground_truth,
            run=outcome.run,
            sample_id=sample.sample_id,
            match_level=match_level,
        )
        if args.persist_traces and trace is not None:
            trace.persist(outcome.run)
        return row
    except Exception as exc:  # noqa: BLE001 - 单个样本失败不应中断整个评测
        return {
            "id": sample.sample_id,
            "error": f"{type(exc).__name__}: {exc}",
            "match_level": match_level,
            "tp": 0,
            "fp": 0,
            "fn": len(ground_truth),
            "precision": 0.0,
            "recall": 0.0,
            "f1": 0.0,
            "task_success": False,
            "stop_reason": "exception",
        }


@dataclass
class EvalSample:
    """评测样本（与具体数据 schema 解耦）。

    项目里存在四种数据形态，本模块统一归一化成该结构：

    1. ``{id, language, code, fixed_code, issues[{category,subtype,severity,description,location}]}``
    —— ``dataset/*.json``、``data/eval_dataset*.json``，**带行号**
    2. ``{code_id, language, code:{buggy,fixed}, issue:{category,sub_category,line,...}}``
       —— ``data/processed/**/*.jsonl``，单问题、**通常无行号**
    3. ``{id, buggy_code, fixed_code, bug_category, bug_sub_category}``
       —— ``final_dataset.json`` / ``combined_dataset.json``，**无行号、无描述**
    4. ``{id, language, code, issues[...]}`` 的 JSONL 变体

    行号缺失会直接影响评测口径，因此 ``issues`` 里的 ``line_start`` 允许为 0
    （表示未知），由 ``agent_metrics.gt_has_lines`` 判定后自动降级为类别级匹配。
    """

    sample_id: str
    code: str
    fixed_code: Optional[str]
    issues: List[Dict[str, Any]]
    language: str = "python"


def _issue_dict(
    category: Any,
    subtype: Any = "",
    severity: Any = "",
    description: Any = "",
    line_start: Any = 0,
    line_end: Any = 0,
) -> Dict[str, Any]:
    try:
        start = int(line_start or 0)
    except (TypeError, ValueError):
        start = 0
    try:
        end = int(line_end or start)
    except (TypeError, ValueError):
        end = start
    return {
        "category": str(category or "").strip(),
        "subtype": str(subtype or "").strip(),
        "severity": str(severity or "").strip(),
        "description": str(description or "").strip(),
        "line_start": start,
        "line_end": max(start, end),
    }


def _normalize_item(item: Dict[str, Any]) -> Optional[EvalSample]:
    """把任意 schema 的样本对象归一化为 EvalSample。无法识别时返回 None。"""
    language = str(item.get("language") or "python").strip()

    # ---- 形态 2：code 是 {buggy, fixed} 的嵌套结构 ----
    code_field = item.get("code")
    if isinstance(code_field, dict):
        code = str(code_field.get("buggy") or "")
        fixed = str(code_field.get("fixed") or "") or None
        raw_issue = item.get("issue") or {}
        issues: List[Dict[str, Any]] = []
        if isinstance(raw_issue, dict) and raw_issue:
            issues.append(
                _issue_dict(
                    category=raw_issue.get("category") or raw_issue.get("type"),
                    subtype=raw_issue.get("sub_category") or raw_issue.get("category"),
                    severity=raw_issue.get("severity"),
                    description=raw_issue.get("description"),
                    line_start=raw_issue.get("line") or 0,
                )
            )
        if not code:
            return None
        return EvalSample(
            sample_id=str(item.get("code_id") or item.get("id") or ""),
            code=code,
            fixed_code=fixed,
            issues=issues,
            language=language,
        )

    # ---- 形态 3：buggy_code / fixed_code + bug_category ----
    if item.get("buggy_code") is not None:
        code = str(item.get("buggy_code") or "")
        if not code:
            return None
        return EvalSample(
            sample_id=str(item.get("id") or item.get("bug_id") or ""),
            code=code,
            fixed_code=str(item.get("fixed_code") or "") or None,
            issues=[
                _issue_dict(
                    category=item.get("bug_category"),
                    subtype=item.get("bug_sub_category"),
                    description=item.get("bug_category"),
                )
            ]
            if item.get("bug_category")
            else [],
            language=language,
        )

    # ---- 形态 1 / 4：code 是字符串 + issues 数组 ----
    if isinstance(code_field, str):
        code = code_field
        if not code:
            return None
        issues = []
        for raw in item.get("issues") or []:
            if not isinstance(raw, dict):
                continue
            location = raw.get("location") or {}
            issues.append(
                _issue_dict(
                    category=raw.get("category") or raw.get("dimension"),
                    subtype=raw.get("subtype"),
                    severity=raw.get("severity"),
                    description=raw.get("description"),
                    line_start=location.get("line_start") or raw.get("line_start") or 0,
                    line_end=location.get("line_end") or raw.get("line_end") or 0,
                )
            )
        return EvalSample(
            sample_id=str(item.get("id") or item.get("code_id") or ""),
            code=code,
            fixed_code=str(item.get("fixed_code") or "") or None,
            issues=issues,
            language=language,
        )

    return None


def _load_samples(path: Path, language: str, max_samples: int) -> tuple[List[EvalSample], Dict[str, Any]]:
    """加载数据集（自动识别 JSON 数组 / JSONL 与多种 schema）。"""
    items: List[Dict[str, Any]] = []
    stats: Dict[str, Any] = {"path": str(path), "format": path.suffix.lower().lstrip(".")}

    if path.suffix.lower() == ".jsonl":
        skipped_lines = 0
        with path.open("r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    skipped_lines += 1
                    continue
                if isinstance(obj, dict):
                    items.append(obj)
        stats["skipped_lines"] = skipped_lines
    else:
        text = path.read_text(encoding="utf-8", errors="ignore")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
    # 与 dataset_loader 一致：允许截断到最后一个完整对象
            data = None
            end = text.rfind("}")
            if end != -1:
                try:
                    data = json.loads(text[: end + 1] + "]")
                except json.JSONDecodeError:
                    data = None
            if data is None:
                raise
        items = data if isinstance(data, list) else data.get("samples") or []

    samples: List[EvalSample] = []
    unparsed = 0
    dropped_lang = 0
    for item in items:
        if not isinstance(item, dict):
            unparsed += 1
            continue
        sample = _normalize_item(item)
        if sample is None:
            unparsed += 1
            continue
        if language and sample.language and sample.language.lower() != language.lower():
            dropped_lang += 1
            continue
        samples.append(sample)

    random.Random(42).shuffle(samples)
    if max_samples and max_samples > 0:
        samples = samples[:max_samples]
    stats.update(
        {
            "total_items": len(items),
            "kept": len(samples),
            "unparsed": unparsed,
            "dropped_by_language": dropped_lang,
            "with_line_info": sum(
                1 for s in samples if s.issues and gt_has_lines(s.issues)
            ),
        }
    )
    return samples, stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent 评测 runner")
    parser.add_argument("--dataset", default="data/benchmark/dataset.jsonl", help="数据集路径")
    parser.add_argument("--mode", choices=["rules", "single", "multi"], default="rules")
    parser.add_argument("--max-samples", type=int, default=0, help="0 表示全部")
    parser.add_argument("--max-workers", type=int, default=4)
    parser.add_argument("--language", default="python")
    parser.add_argument("--out", default="", help="结果输出路径，默认 reports/agent/<mode>/report.json")
    parser.add_argument("--codebase-root", default="")
    parser.add_argument("--project", default="default")
    parser.add_argument("--no-memory", action="store_true")
    parser.add_argument(
        "--match-level",
        choices=["auto", "line", "category"],
        default="auto",
        help="匹配口径。auto=按标注是否含行号自动选择；category 级匹配更宽松，两者不可直接比较",
    )
    parser.add_argument("--persist-traces", action="store_true", help="把每次运行的轨迹落盘")
    args = parser.parse_args()

    dataset_path = Path(args.dataset)
    if not dataset_path.exists():
        print(f"数据集不存在：{dataset_path}", file=sys.stderr)
        return 2

    samples, load_stats = _load_samples(dataset_path, args.language, args.max_samples)
    if not samples:
        print("未加载到样本。", file=sys.stderr)
        return 2

    print(f"数据集：{dataset_path}  样本数：{len(samples)}  模式：{args.mode}")
    if load_stats.get("with_line_info", 0) == 0:
        print(
            "注意：该数据集不含行号标注（with_line_info=0），将退化为"
            "「问题类别级」匹配（match_level=category）。此类指标比行号级宽松，"
            "不可与 exp1 的 line 级结果直接比较。"
        )
    if args.mode != "rules" and len(samples) > 5 and args.max_workers <= 1:
        print("提示：单线程运行会较慢，可用 --max-workers 提高并发。")

    agent = _build_agents(args)
    rows: List[Dict[str, Any]] = []
    started = time.time()

    if args.max_workers <= 1 or args.mode == "rules":
        iterator = samples
        for sample in iterator:
            rows.append(_run_one(agent, sample, args))
            print(f"  ... {len(rows)}/{len(samples)}", end="\r")
    else:
        executor = ThreadPoolExecutor(max_workers=args.max_workers)
        futures = {executor.submit(_run_one, agent, sample, args): sample for sample in samples}
        for future in as_completed(futures):
            rows.append(future.result())
            print(f"  ... {len(rows)}/{len(samples)}", end="\r")
        executor.shutdown(wait=True)
    print()

    elapsed = time.time() - started
    summary = aggregate(rows)
    summary["config"] = {
        "dataset": str(dataset_path),
        "mode": args.mode,
        "samples": len(samples),
        "max_workers": args.max_workers,
        "wall_clock_s": round(elapsed, 2),
        "codebase_root": args.codebase_root or None,
        "memory_enabled": not args.no_memory and args.mode == "single",
        "dataset_stats": load_stats,
    }

    out_path = Path(args.out) if args.out else Path("reports") / "agent" / args.mode / "report.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps({"summary": summary, "samples": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print()
    print(format_summary(summary))
    print(f"\n总耗时 {elapsed:.1f}s   结果已保存：{out_path}")
    if args.mode != "rules":
        cost = estimate_cost(_total_usage(rows))
        print(f"累计 token {_total_tokens(rows)}  估算成本 ${cost['total_cost']}")
    return 0


def _total_tokens(rows: Sequence[Dict[str, Any]]) -> int:
    return sum(int(r.get("tokens") or 0) for r in rows)


def _total_usage(rows: Sequence[Dict[str, Any]]):
    from agent.schema import TokenUsage

    return TokenUsage(
        prompt_tokens=sum(int(r.get("prompt_tokens") or 0) for r in rows),
        completion_tokens=sum(int(r.get("completion_tokens") or 0) for r in rows),
        total_tokens=_total_tokens(rows),
    )


if __name__ == "__main__":
    raise SystemExit(main())
