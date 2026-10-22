"""可观测性：单次 Agent 运行的全链路轨迹与成本统计。

一次审查需要回答「Agent 做了什么、每步花了多少 token、为什么停下来了」。
本模块把这些信息落成结构化 trace，供：

- CLI 实时渲染（能看到 Agent 逐步思考与工具调用）
- 前端 SSE 流式展示
- 离线评测与失败归因（``TraceStore.summary`` 聚合多次运行）
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from agent import settings
from agent.schema import AgentRun, AgentStep, TokenUsage

EventHook = Callable[[AgentStep], None]


def new_run_id(prefix: str = "run") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{prefix}-{stamp}-{uuid.uuid4().hex[:6]}"


def estimate_cost(usage: TokenUsage) -> Dict[str, float]:
    """按 1k token 单价估算成本；单价未配置时返回 0。"""
    prompt_cost = usage.prompt_tokens / 1000.0 * settings.PRICE_PROMPT_PER_1K
    completion_cost = usage.completion_tokens / 1000.0 * settings.PRICE_COMPLETION_PER_1K
    return {
        "prompt_cost": round(prompt_cost, 6),
        "completion_cost": round(completion_cost, 6),
        "total_cost": round(prompt_cost + completion_cost, 6),
    }


class TraceRecorder:
    """收集一次 Agent 运行的轨迹。"""

    def __init__(
        self,
        run_id: Optional[str] = None,
        task: str = "review",
        persist_dir: Optional[str] = None,
        on_event: Optional[EventHook] = None,
    ) -> None:
        self.run_id = run_id or new_run_id(task)
        self.task = task
        self.persist_dir = Path(persist_dir or settings.TRACE_DIR)
        self.steps: List[AgentStep] = []
        self.started_at = time.time()
        self.stop_reason = ""
        self.error = ""
        self._on_event = on_event
        self._step_index = 0

    # ---------- 记录 ----------

    def add_step(
        self,
        kind: str,
        content: str = "",
        tool_call: Any = None,
        tool_result: Any = None,
        usage: Optional[TokenUsage] = None,
        latency_ms: float = 0.0,
    ) -> AgentStep:
        self._step_index += 1
        step = AgentStep(
            index=self._step_index,
            kind=kind,  # type: ignore[arg-type]
            content=content,
            tool_call=tool_call,
            tool_result=tool_result,
            usage=usage or TokenUsage(),
            latency_ms=latency_ms,
            timestamp=time.time(),
        )
        self.steps.append(step)
        if self._on_event is not None:
            try:
                self._on_event(step)
            except Exception:
                # 观测回调失败绝不应该影响主流程
                pass
        return step

    def mark_stop(self, reason: str) -> None:
        self.stop_reason = reason

    def mark_error(self, message: str) -> None:
        self.error = message

    # ---------- 统计 ----------

    @property
    def tool_calls(self) -> List[Any]:
        """本次运行中已经发生的工具调用。"""
        return [s.tool_call for s in self.steps if s.kind == "act" and s.tool_call]

    @property
    def tool_names(self) -> List[str]:
        """本次运行中已经调用过的工具名（用于证据校验）。"""
        return [call.name for call in self.tool_calls]

    def total_usage(self) -> TokenUsage:
        total = TokenUsage()
        for step in self.steps:
            total.accumulate(step.usage)
        return total

    def build(self, report: Any = None, output: Optional[Dict[str, Any]] = None) -> AgentRun:
        steps = list(self.steps)
        return AgentRun(
            run_id=self.run_id,
            task=self.task,
            steps=steps,
            usage=self.total_usage(),
            duration_ms=(time.time() - self.started_at) * 1000.0,
            stop_reason=self.stop_reason,
            report=report,
            output=output,
            error=self.error,
        )

    # ---------- 落盘 ----------

    def persist(self, run: AgentRun) -> Optional[Path]:
        try:
            self.persist_dir.mkdir(parents=True, exist_ok=True)
            path = self.persist_dir / f"{run.run_id}.jsonl"
            with path.open("w", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "type": "run_meta",
                            "run_id": run.run_id,
                            "task": run.task,
                            "started_at": self.started_at,
                            "duration_ms": round(run.duration_ms, 2),
                            "stop_reason": run.stop_reason,
                            "error": run.error,
                            "usage": run.usage.model_dump(),
                            "cost": estimate_cost(run.usage),
                            "tool_calls": run.tool_call_stats(),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                for step in run.steps:
                    fh.write(
                        json.dumps(
                            {"type": "step", **step.model_dump()},
                            ensure_ascii=False,
                            default=str,
                        )
                        + "\n"
                    )
                if run.report is not None:
                    fh.write(
                        json.dumps(
                            {"type": "report", **run.report.to_legacy()},
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
            return path
        except Exception:
            return None

    # ---------- 渲染 ----------

    def render_text(self) -> str:
        run = self.build()
        lines = [
            f"Agent Run {run.run_id}  (task={run.task})",
            f"停止原因: {run.stop_reason or '-'}   耗时: {run.duration_ms / 1000:.2f}s   "
            f"步数: {len(run.steps)}   工具调用: {sum(run.tool_call_stats().values())}",
            f"Token: prompt={run.usage.prompt_tokens} completion={run.usage.completion_tokens} "
            f"total={run.usage.total_tokens}",
            "-" * 78,
        ]
        for step in run.steps:
            mark = {
                "think": "THINK",
                "act": " ACT ",
                "observe": " OBS ",
                "final": "FINAL",
                "error": " ERR ",
            }.get(step.kind, step.kind.upper())
            lines.append(f"[{step.index:02d}][{mark}] {step.brief(200)}")
        return "\n".join(lines)

    def render_html(self, run: Optional[AgentRun] = None) -> str:
        run = run or self.build()
        rows = []
        for step in run.steps:
            css = {
                "think": "think",
                "act": "act",
                "observe": "obs",
                "final": "final",
                "error": "err",
            }.get(step.kind, "think")
            rows.append(
                f'<div class="step {css}"><span class="idx">#{step.index}</span>'
                f'<span class="kind">{step.kind}</span>'
                f'<pre>{_html_escape(step.brief(1200))}</pre>'
                f'<span class="meta">{step.latency_ms:.0f}ms · {step.usage.total_tokens} tok</span></div>'
            )
        cost = estimate_cost(run.usage)
        return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Agent Trace {run.run_id}</title>
<style>
 body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#14161a;color:#e6e6e6;padding:24px}}
 h1{{font-size:18px;margin:0 0 4px}} .sub{{color:#8b949e;font-size:13px;margin-bottom:18px}}
 .step{{border-left:3px solid #30363d;background:#1b1f24;margin:8px 0;padding:10px 12px;border-radius:6px}}
 .step.act{{border-color:#2f81f7}} .step.obs{{border-color:#3fb950}}
 .step.final{{border-color:#d29922}} .step.err{{border-color:#f85149}}
 .idx{{color:#8b949e;margin-right:8px}} .kind{{color:#79c0ff;font-weight:600;margin-right:8px;font-size:12px;text-transform:uppercase}}
 pre{{white-space:pre-wrap;word-break:break-word;margin:6px 0 0;font-size:13px}}
 .meta{{color:#6e7681;font-size:11px}}
</style></head><body>
<h1>Agent Trace · {run.run_id}</h1>
<div class="sub">task={run.task} · 停止原因={run.stop_reason or '-'} ·
耗时 {run.duration_ms / 1000:.2f}s · 步数 {len(run.steps)} ·
token {run.usage.total_tokens} · 估算成本 ${cost['total_cost']}</div>
{''.join(rows)}
</body></html>"""


def _html_escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


class TraceStore:
    """按目录聚合多次运行，用于离线分析与评测。"""

    def __init__(self, trace_dir: Optional[str] = None) -> None:
        self.trace_dir = Path(trace_dir or settings.TRACE_DIR)

    def files(self) -> List[Path]:
        if not self.trace_dir.exists():
            return []
        return sorted(self.trace_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)

    def load_meta(self, path: Path) -> Dict[str, Any]:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "run_meta":
                    return obj
        return {}

    def summary(self, limit: int = 200) -> Dict[str, Any]:
        metas: List[Dict[str, Any]] = []
        for path in self.files()[-limit:]:
            meta = self.load_meta(path)
            if meta:
                metas.append(meta)

        if not metas:
            return {"runs": 0}

        tool_counter: Dict[str, int] = {}
        stop_counter: Dict[str, int] = {}
        total_tokens = 0
        total_cost = 0.0
        total_duration = 0.0
        for meta in metas:
            for name, count in (meta.get("tool_calls") or {}).items():
                tool_counter[name] = tool_counter.get(name, 0) + int(count)
            reason = str(meta.get("stop_reason") or "unknown")
            stop_counter[reason] = stop_counter.get(reason, 0) + 1
            usage = meta.get("usage") or {}
            total_tokens += int(usage.get("total_tokens") or 0)
            total_cost += float((meta.get("cost") or {}).get("total_cost") or 0.0)
            total_duration += float(meta.get("duration_ms") or 0.0)

        n = len(metas)
        return {
            "runs": n,
            "avg_tokens": round(total_tokens / n, 1),
            "avg_cost": round(total_cost / n, 6),
            "avg_duration_ms": round(total_duration / n, 1),
            "stop_reasons": stop_counter,
            "tool_usage": dict(sorted(tool_counter.items(), key=lambda kv: -kv[1])),
        }


def iter_run_lines(path: Path) -> Iterable[Dict[str, Any]]:
    """按行读取一次运行的 jsonl。"""
    with Path(path).open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue
