"""FastAPI 服务层。

提供两类端点：

- ``POST /review`` / ``POST /optimize``：扁平响应，供 VSCode 插件与 Web 前端直接调用
- ``POST /agent/review``：结构化报告 + 完整运行轨迹 + 统计
- ``POST /agent/review/stream``：SSE 流式推送 Agent 的每一步（思考 / 工具调用 / 观察）
- ``POST /agent/optimize``：带三级验证报告的优化结果
- ``POST /agent/feedback``：记录人工反馈，沉淀为长期记忆
- ``GET  /agent/trace/{run_id}``：取回某次运行的持久化轨迹
- ``GET  /agent/stats``：多次运行的聚合统计（平均步数 / token / 成本 / 工具分布）

两类端点内部走同一条 Agent 管线：工具取证、证据校验、结构化输出重试与降级。
"""

from __future__ import annotations

import json
import logging
import queue
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

sys.path.append(str(Path(__file__).parent.parent))

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

import agent.settings as agent_settings
from agent.llm import LLMClient
from agent.memory import MemoryStore
from agent.multi_agent import MultiAgentReviewPipeline
from agent.optimizer_agent import OptimizerAgent
from agent.reviewer_agent import ReviewerAgent
from agent.runtime import AgentConfig
from agent.trace import TraceRecorder, TraceStore, iter_run_lines
from config import LOG_DIR, MOCK_MODE

Path(LOG_DIR).mkdir(exist_ok=True)
logging.basicConfig(
    filename=Path(LOG_DIR) / "api.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    encoding="utf-8",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="智能代码审查 Agent API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# 单例缓存：代码库索引构建代价较高，不能每个请求重建一次
# ---------------------------------------------------------------------------

_agent_cache: Dict[str, Any] = {}
_cache_lock = threading.Lock()
_memory_store: Optional[MemoryStore] = None


def _get_memory() -> MemoryStore:
    global _memory_store
    if _memory_store is None:
        _memory_store = MemoryStore()
    return _memory_store


def _get_reviewer(codebase_root: Optional[str], use_memory: bool) -> ReviewerAgent:
    key = f"reviewer|{codebase_root or ''}|{use_memory}"
    with _cache_lock:
        agent = _agent_cache.get(key)
        if agent is None:
            agent = ReviewerAgent(
                client=LLMClient(),
                config=AgentConfig(),
                codebase_root=codebase_root,
                memory=_get_memory() if use_memory else None,
            )
            _agent_cache[key] = agent
        return agent


def _get_optimizer(codebase_root: Optional[str]) -> OptimizerAgent:
    key = f"optimizer|{codebase_root or ''}"
    with _cache_lock:
        agent = _agent_cache.get(key)
        if agent is None:
            agent = OptimizerAgent(client=LLMClient(), config=AgentConfig(), codebase_root=codebase_root)
            _agent_cache[key] = agent
        return agent


def _get_pipeline(codebase_root: Optional[str]) -> MultiAgentReviewPipeline:
    key = f"pipeline|{codebase_root or ''}"
    with _cache_lock:
        pipeline = _agent_cache.get(key)
        if pipeline is None:
            pipeline = MultiAgentReviewPipeline(
                client=LLMClient(), codebase_root=codebase_root, persist_traces=False
            )
            _agent_cache[key] = pipeline
        return pipeline


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------


class CodePayload(BaseModel):
    code: str


class ReviewRequest(BaseModel):
    code: str = Field(..., description="待审查代码")
    file_path: Optional[str] = Field(default=None, description="文件路径，仅用于提示词展示")
    codebase_root: Optional[str] = Field(default=None, description="代码库根目录，启用检索工具")
    use_memory: bool = Field(default=True, description="是否启用长期记忆（历史误报压制）")
    mode: str = Field(default="single", description="single=单 Agent；multi=多智能体协作")
    project: str = Field(default="default", description="项目标识，用于记忆隔离")


class OptimizeRequest(BaseModel):
    code: str
    file_path: Optional[str] = None
    codebase_root: Optional[str] = None
    test_code: str = Field(default="", description="可选：验收测试代码，用于沙箱验证")


class FeedbackRequest(BaseModel):
    code: str
    project: str = "default"
    accepted: bool = Field(..., description="该结论是否被人工采纳")
    issue: Dict[str, Any] = Field(..., description="被反馈的问题对象（与报告中的 issue 结构一致）")
    note: str = ""


def _extract_code(code: Optional[str], payload: Optional[CodePayload]) -> str:
    if code is not None and code.strip():
        return code
    if payload is not None and payload.code.strip():
        return payload.code
    return ""


# ---------------------------------------------------------------------------
# 基础端点
# ---------------------------------------------------------------------------


@app.get("/")
def home() -> Dict[str, Any]:
    return {
        "status": "ok",
        "msg": "Agent 服务运行正常",
        "mock_mode": MOCK_MODE,
        "endpoints": [
            "POST /review",
            "POST /optimize",
            "POST /agent/review",
            "POST /agent/review/stream",
            "POST /agent/optimize",
            "POST /agent/feedback",
            "GET /agent/trace/{run_id}",
            "GET /agent/stats",
        ],
    }


# ---------------------------------------------------------------------------
# 扁平端点（固定响应结构，供插件与前端调用）
# ---------------------------------------------------------------------------


@app.post("/review")
def review(code: Optional[str] = None, payload: Optional[CodePayload] = Body(None)) -> Dict[str, Any]:
    """代码审查（扁平响应结构，内部走 Agent 管线）。"""
    code_value = _extract_code(code, payload)
    if not code_value:
        raise HTTPException(status_code=400, detail="代码不能为空")
    try:
        agent = _get_reviewer(agent_settings.CODEBASE_ROOT or None, use_memory=True)
        report, _outcome = agent.review(code_value)
        return {"result": report.to_legacy()}
    except Exception as exc:  # noqa: BLE001
        logger.error("审查失败：%s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"审查失败：{exc}") from exc


@app.post("/optimize")
def optimize(code: Optional[str] = None, payload: Optional[CodePayload] = Body(None)) -> Dict[str, Any]:
    """代码优化（返回优化后的代码，内部为带验证闭环的 Agent 管线）。"""
    code_value = _extract_code(code, payload)
    if not code_value:
        raise HTTPException(status_code=400, detail="代码不能为空")
    try:
        reviewer = _get_reviewer(agent_settings.CODEBASE_ROOT or None, use_memory=False)
        report, _ = reviewer.review(code_value)
        optimizer = _get_optimizer(agent_settings.CODEBASE_ROOT or None)
        result, _run = optimizer.optimize(code_value, report)
        return {"result": result.optimized_code}
    except Exception as exc:  # noqa: BLE001
        logger.error("代码优化失败：%s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"优化失败：{exc}") from exc


# ---------------------------------------------------------------------------
# Agent 端点
# ---------------------------------------------------------------------------


@app.post("/agent/review")
def agent_review(request: ReviewRequest) -> Dict[str, Any]:
    """Agent 审查：返回报告 + 完整轨迹 + 统计。"""
    if not request.code.strip():
        raise HTTPException(status_code=400, detail="代码不能为空")

    try:
        if request.mode == "multi":
            pipeline = _get_pipeline(request.codebase_root)
            result = pipeline.review(request.code, file_path=request.file_path)
            traces = {
                role: run.to_trace_dict() for role, run in result.runs.items()
            }
            return {
                "report": result.report.to_legacy(),
                "mode": "multi",
                "stats": result.stats,
                "verdicts": [v.model_dump() for v in result.verdicts],
                "corroborations": [
                    {"index": c.index, "corroborated": c.corroborated, "engines": c.engines, "note": c.note}
                    for c in result.corroborations
                ],
                "traces": traces,
            }

        agent = _get_reviewer(request.codebase_root, use_memory=request.use_memory)
        agent.project = request.project
        report, outcome = agent.review(request.code, file_path=request.file_path)
        return {
            "report": report.to_legacy(),
            "mode": "single",
            "trace": outcome.run.to_trace_dict(),
            "stop_reason": outcome.run.stop_reason,
        }
    except Exception as exc:  # noqa: BLE001
        logger.error("Agent 审查失败：%s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Agent 审查失败：{exc}") from exc


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.post("/agent/review/stream")
def agent_review_stream(request: ReviewRequest) -> StreamingResponse:
    """SSE 流式审查：实时推送 Agent 的每一步（思考 / 工具调用 / 观察），最后推送报告。

    Agent 的每一步都通过 ``TraceRecorder.on_event`` 回调产生事件，经线程安全队列
    转交给生成器，因此前端可以看到「正在调用哪个工具、拿到了什么观察」的实时过程，
    而不是干等几十秒。
    """
    if not request.code.strip():
        raise HTTPException(status_code=400, detail="代码不能为空")

    def event_source() -> Iterator[str]:
        events: "queue.Queue[Optional[tuple]]" = queue.Queue()
        box: Dict[str, Any] = {}

        def on_event(step: Any) -> None:
            events.put(("step", step))

        def worker() -> None:
            try:
                if request.mode == "multi":
                    pipeline = _get_pipeline(request.codebase_root)
                    result = pipeline.review(request.code, file_path=request.file_path, on_event=on_event)
                    box["payload"] = {
                        "report": result.report.to_legacy(),
                        "mode": "multi",
                        "stats": result.stats,
                        "traces": {role: run.to_trace_dict() for role, run in result.runs.items()},
                    }
                else:
                    agent = _get_reviewer(request.codebase_root, use_memory=request.use_memory)
                    agent.project = request.project
                    trace = TraceRecorder(task="review", on_event=on_event)
                    report, outcome = agent.review(request.code, file_path=request.file_path, trace=trace)
                    box["payload"] = {
                        "report": report.to_legacy(),
                        "mode": "single",
                        "trace": outcome.run.to_trace_dict(),
                        "stop_reason": outcome.run.stop_reason,
                    }
            except Exception as exc:  # noqa: BLE001
                logger.error("流式审查失败：%s", exc, exc_info=True)
                box["error"] = str(exc)
            finally:
                events.put(None)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

        yield _sse("start", {"mode": request.mode, "mock_mode": MOCK_MODE})
        try:
            while True:
                item = events.get()
                if item is None:
                    break
                _kind, step = item
                payload = {
                    "index": step.index,
                    "kind": step.kind,
                    "brief": step.brief(600),
                    "latency_ms": round(step.latency_ms, 2),
                    "tokens": step.usage.total_tokens,
                    "tool": step.tool_call.name if step.tool_call else None,
                    "ok": step.tool_result.ok if step.tool_result else None,
                }
                yield _sse("step", payload)
        finally:
            thread.join(timeout=1.0)

        if "error" in box:
            yield _sse("error", {"message": box["error"]})
        else:
            yield _sse("result", box.get("payload") or {})
        yield _sse("end", {"ok": "error" not in box})

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@app.post("/agent/optimize")
def agent_optimize(request: OptimizeRequest) -> Dict[str, Any]:
    """Agent 优化：返回优化结果 + 三级验证报告 + 轨迹。"""
    if not request.code.strip():
        raise HTTPException(status_code=400, detail="代码不能为空")
    try:
        reviewer = _get_reviewer(request.codebase_root, use_memory=False)
        report, _ = reviewer.review(request.code, file_path=request.file_path)
        optimizer = _get_optimizer(request.codebase_root)
        result, outcome = optimizer.optimize(request.code, report, test_code=request.test_code)
        return {
            "result": result.to_legacy(),
            "verification": result.verification.to_dict(),
            "trace": outcome.run.to_trace_dict(),
        }
    except Exception as exc:  # noqa: BLE001
        logger.error("Agent 优化失败：%s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Agent 优化失败：{exc}") from exc


@app.post("/agent/feedback")
def agent_feedback(request: FeedbackRequest) -> Dict[str, Any]:
    """记录人工反馈，沉淀为长期记忆（下次审查自动压制同类误报）。"""
    from agent.schema import Issue

    try:
        issue = Issue.model_validate(request.issue)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"issue 结构非法：{exc}") from exc

    record = _get_memory().record_feedback(
        project=request.project,
        issue=issue,
        accepted=request.accepted,
        code=request.code,
        note=request.note,
    )
    return {
        "ok": True,
        "record_id": record.id,
        "kind": record.kind,
        "memory_stats": _get_memory().stats(),
    }


@app.get("/agent/trace/{run_id}")
def agent_trace(run_id: str) -> Dict[str, Any]:
    """取回某次运行的持久化轨迹（JSONL 逐行解析）。"""
    safe_id = Path(run_id).name  # 防目录穿越
    path = Path(agent_settings.TRACE_DIR) / f"{safe_id}.jsonl"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"未找到轨迹 {safe_id}")
    return {"run_id": safe_id, "lines": list(iter_run_lines(path))}


@app.get("/agent/stats")
def agent_stats(limit: int = Query(default=200, ge=1, le=2000)) -> Dict[str, Any]:
    """多次运行的聚合统计：平均步数 / token / 成本 / 工具使用分布 / 停止原因分布。"""
    store = TraceStore()
    return {"trace_dir": agent_settings.TRACE_DIR, **store.summary(limit=limit)}


@app.get("/agent/memory")
def agent_memory(project: Optional[str] = None) -> Dict[str, Any]:
    """查看长期记忆库内容与统计。"""
    memory = _get_memory()
    memory.ensure_loaded()
    records = memory.records
    if project:
        records = [r for r in records if r.project == project]
    return {
        "stats": memory.stats(),
        "records": [r.to_dict() for r in records[-50:]],
    }


if __name__ == "__main__":
    import uvicorn

    # 注意：这里刻意不用 emoji。Windows 控制台默认 GBK 编码，输出 emoji 会抛
    # UnicodeEncodeError 导致服务无法启动。
    print("[start] Agent API 服务：http://127.0.0.1:8000")
    print("[docs ] 接口文档：http://127.0.0.1:8000/docs")
    if MOCK_MODE:
        print("[warn ] 当前运行在 MOCK_MODE（不调用真实模型）")
    uvicorn.run(app, host="127.0.0.1", port=8000)
