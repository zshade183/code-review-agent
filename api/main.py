"""FastAPI 服务层。

多 Agent 主线下的端点：

- ``POST /review`` / ``POST /optimize``：扁平响应，供 VSCode 插件与 Web 前端直接调用
- ``POST /agent/review``：多 Agent 审查（策略 / 覆盖度 / 裁决 / 轨迹）
- ``POST /agent/review/stream``：SSE 流式推送各角色的每一步（带 role 标记）
- ``POST /agent/fix``：独立修复管线；``confirm=false`` 时只返回待确认计划（人工确认）
- ``POST /agent/optimize``：兼容入口（审查 + 修复）
- ``POST /agent/feedback``：记录人工反馈，沉淀为长期记忆
- ``GET  /agent/trace/{run_id}``、``GET /agent/stats``、``GET/POST /agent/memory*``

两类扁平端点内部走同一条多 Agent 管线（默认 ``auto`` 策略）。
"""

from __future__ import annotations

import json
import logging
import queue
import secrets
import sys
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

sys.path.append(str(Path(__file__).parent.parent))

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

import agent.settings as agent_settings
from agent.llm import LLMClient
from agent.memory import MemoryStore, get_shared_memory
from agent.pipelines import FixPipeline, ReviewPipeline
from agent.schema import normalize_strategy
from agent.trace import TraceStore, iter_run_lines
from agent.workflow_store import WorkflowStore
from config import LOG_DIR, MOCK_MODE

Path(LOG_DIR).mkdir(exist_ok=True)
logging.basicConfig(
    filename=Path(LOG_DIR) / "api.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    encoding="utf-8",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="智能代码审查 Agent API（多 Agent 主线）")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_rate_windows: Dict[str, deque] = defaultdict(deque)
_rate_lock = threading.Lock()


@app.middleware("http")
async def service_guard(request: Request, call_next):  # noqa: ANN001
    """Optional API-key authentication and per-client fixed-window rate limiting."""
    expected = agent_settings.API_KEY_AUTH
    if expected and request.url.path != "/":
        supplied = request.headers.get("x-api-key", "")
        authorization = request.headers.get("authorization", "")
        if not supplied and authorization.lower().startswith("bearer "):
            supplied = authorization[7:].strip()
        if not secrets.compare_digest(supplied, expected):
            return JSONResponse(status_code=401, content={"detail": "无效或缺失的服务 API Key"})

    limit = agent_settings.API_RATE_LIMIT_PER_MINUTE
    if limit > 0 and request.url.path != "/":
        client = request.client.host if request.client else "unknown"
        key = f"{client}:{request.url.path}"
        now = time.monotonic()
        with _rate_lock:
            window = _rate_windows[key]
            while window and now - window[0] >= 60.0:
                window.popleft()
            if len(window) >= limit:
                return JSONResponse(status_code=429, content={"detail": "请求过于频繁，请稍后重试"})
            window.append(now)
    return await call_next(request)

# ---------------------------------------------------------------------------
# 单例缓存：代码库索引与证据缓存构建代价较高，不能每个请求重建一次
# ---------------------------------------------------------------------------

_agent_cache: Dict[str, Any] = {}
_cache_lock = threading.Lock()
_memory_store: Optional[MemoryStore] = None
_workflow_store: Optional[WorkflowStore] = None


def _get_memory() -> MemoryStore:
    global _memory_store
    if _memory_store is None:
        _memory_store = get_shared_memory()
    return _memory_store


def _get_workflows() -> WorkflowStore:
    global _workflow_store
    if _workflow_store is None:
        _workflow_store = WorkflowStore()
    return _workflow_store


def _get_pipeline(
    codebase_root: Optional[str], use_memory: bool = True, project: str = "default"
) -> ReviewPipeline:
    # project 纳入缓存键，避免并发请求通过可变 project 串读其它项目记忆。
    key = f"pipeline|{codebase_root or ''}|{use_memory}|{project}"
    with _cache_lock:
        pipeline = _agent_cache.get(key)
        if pipeline is None:
            pipeline = ReviewPipeline(
                codebase_root=codebase_root,
                project=project,
                use_memory=use_memory,
            )
            _agent_cache[key] = pipeline
        return pipeline


def _get_fixer(codebase_root: Optional[str]) -> FixPipeline:
    key = f"fixer|{codebase_root or ''}"
    with _cache_lock:
        fixer = _agent_cache.get(key)
        if fixer is None:
            fixer = FixPipeline(client=LLMClient(), codebase_root=codebase_root)
            _agent_cache[key] = fixer
        return fixer


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------


class CodePayload(BaseModel):
    code: str


class ReviewRequest(BaseModel):
    code: str = Field(..., description="待审查代码")
    file_path: Optional[str] = Field(default=None, description="文件路径，仅用于提示词展示")
    codebase_root: Optional[str] = Field(default=None, description="代码库根目录，启用检索工具")
    use_memory: bool = Field(default=True, description="是否启用长期记忆（裁决后降权，不删除）")
    strategy: str = Field(
        default="",
        description="审查档位：fast 仅初审；auto 风险候选复核；full 全候选复核",
    )
    mode: str = Field(
        default="",
        description="[已废弃] single=fast；multi=full。请改用 strategy",
    )
    project: str = Field(default="default", description="项目标识，用于记忆隔离")
    session_id: str = Field(default="", description="可选会话标识，用于串联有界短期记忆")
    extra_context: str = Field(default="", max_length=6000, description="本轮审查要求或补充上下文")


class FollowUpRequest(BaseModel):
    session_id: str = Field(min_length=1)
    question: str = Field(min_length=1, max_length=6000)
    project: str = "default"


@app.post("/agent/followup")
def agent_followup(request: FollowUpRequest) -> Dict[str, Any]:
    """回答同一会话中对已有审查的追问；不执行代码修改。"""
    try:
        return _get_pipeline(None, project=request.project).follow_up(request.question, request.session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("追问失败：%s", exc, exc_info=True)
        raise HTTPException(status_code=502, detail="追问模型调用失败，请稍后重试") from exc


class OptimizeRequest(BaseModel):
    code: str
    file_path: Optional[str] = None
    codebase_root: Optional[str] = None
    test_code: str = Field(default="", description="可选：验收测试代码，用于沙箱验证")
    strategy: str = Field(default="auto", description="先跑哪档审查来生成修复清单")
    issues: list = Field(default_factory=list, description="可选：直接传入的问题清单")
    candidate_ids: list = Field(default_factory=list, description="可选：只修这些候选")


class FixRequest(BaseModel):
    code: str = ""
    review_id: str = Field(default="", description="持久化审查快照 ID；可不重复提交代码和问题")
    fix_id: str = Field(default="", description="待确认修复计划 ID；confirm=true 时从检查点恢复")
    file_path: Optional[str] = None
    codebase_root: Optional[str] = None
    test_code: str = ""
    strategy: str = "auto"
    candidate_ids: list = Field(default_factory=list)
    issues: list = Field(default_factory=list)
    include_memory_suspected: bool = False
    confirm: bool = Field(
        default=False, description="false=只返回修复计划等待人工确认；true=执行 Fix Agent"
    )


class FeedbackRequest(BaseModel):
    code: str
    project: str = "default"
    accepted: bool = Field(..., description="该结论是否被人工采纳")
    issue: Dict[str, Any] = Field(..., description="被反馈的问题对象（与报告中的 issue 结构一致）")
    note: str = ""


class EntityMemoryRequest(BaseModel):
    project: str = "default"
    entity_type: str = Field(..., description="实体类型，例如 project、file、component")
    name: str = Field(..., description="实体的稳定名称")
    facts: Dict[str, Any] = Field(default_factory=dict, description="结构化事实")
    source: str = "manual"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


def _extract_code(code: Optional[str], payload: Optional[CodePayload]) -> str:
    if code is not None and code.strip():
        return code
    if payload is not None and payload.code.strip():
        return payload.code
    return ""


def _resolve_strategy(request: ReviewRequest) -> str:
    """兼容历史 mode 字段：single -> fast，multi -> full。"""
    if request.strategy.strip():
        return normalize_strategy(request.strategy).value
    if request.mode.strip():
        return normalize_strategy(request.mode).value
    return "auto"


def _legacy_mode(strategy: str) -> str:
    return "single" if strategy == "fast" else "multi"


# ---------------------------------------------------------------------------
# 基础端点
# ---------------------------------------------------------------------------


@app.get("/")
def home() -> Dict[str, Any]:
    return {
        "status": "ok",
        "msg": "多 Agent 代码审查服务运行正常",
        "mock_mode": MOCK_MODE,
        "default_strategy": agent_settings.DEFAULT_STRATEGY,
        "endpoints": [
            "POST /review",
            "POST /optimize",
            "POST /agent/review",
            "POST /agent/review/stream",
            "POST /agent/fix",
            "POST /agent/optimize",
            "POST /agent/feedback",
            "GET /agent/workflows/{workflow_id}",
            "POST /agent/memory/entity",
            "DELETE /agent/memory/session/{session_id}",
            "GET /agent/trace/{run_id}",
            "GET /agent/stats",
            "GET /agent/memory",
        ],
    }


# ---------------------------------------------------------------------------
# 扁平端点（固定响应结构，供插件与前端调用）
# ---------------------------------------------------------------------------


@app.post("/review")
def review(
    code: Optional[str] = None,
    payload: Optional[CodePayload] = Body(None),
    strategy: str = Query(default="auto", description="fast / auto / full"),
) -> Dict[str, Any]:
    """代码审查（扁平响应结构，内部走多 Agent 主线）。"""
    code_value = _extract_code(code, payload)
    if not code_value:
        raise HTTPException(status_code=400, detail="代码不能为空")
    try:
        pipeline = _get_pipeline(agent_settings.CODEBASE_ROOT or None, use_memory=True)
        result = pipeline.review(code_value, strategy=strategy)
        return {
            "result": result.report.to_legacy(),
            "strategy": result.strategy.value,
        }
    except Exception as exc:  # noqa: BLE001
        logger.error("审查失败：%s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"审查失败：{exc}") from exc


@app.post("/optimize")
def optimize(
    code: Optional[str] = None,
    payload: Optional[CodePayload] = Body(None),
    strategy: str = Query(default="auto"),
) -> Dict[str, Any]:
    """代码优化（审查 + 修复管线，返回优化后的代码）。"""
    code_value = _extract_code(code, payload)
    if not code_value:
        raise HTTPException(status_code=400, detail="代码不能为空")
    try:
        pipeline = _get_pipeline(agent_settings.CODEBASE_ROOT or None, use_memory=False)
        review_result = pipeline.review(code_value, strategy=strategy)
        fixer = _get_fixer(agent_settings.CODEBASE_ROOT or None)
        plan = fixer.plan(code_value, review_result.report, include_memory_suspected=True)
        result, _outcome = fixer.fix(code_value, plan)
        return {"result": result.optimized_code, "strategy": review_result.strategy.value}
    except Exception as exc:  # noqa: BLE001
        logger.error("代码优化失败：%s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"优化失败：{exc}") from exc


# ---------------------------------------------------------------------------
# Agent 端点
# ---------------------------------------------------------------------------


@app.post("/agent/review")
def agent_review(request: ReviewRequest) -> Dict[str, Any]:
    """多 Agent 审查：报告 + 策略 + 覆盖度 + 裁决 + 各角色轨迹。"""
    if not request.code.strip():
        raise HTTPException(status_code=400, detail="代码不能为空")

    strategy = _resolve_strategy(request)
    try:
        pipeline = _get_pipeline(
            request.codebase_root,
            use_memory=request.use_memory,
            project=request.project,
        )
        result = pipeline.review(
            request.code,
            file_path=request.file_path or "",
            strategy=strategy,
            session_id=request.session_id,
            extra_context=request.extra_context,
        )
        payload = result.to_dict(include_traces=True)
        # 向后兼容：旧客户端读 mode / trace
        payload["mode"] = _legacy_mode(result.strategy.value)
        payload["trace"] = {"engine": "langgraph", "events": result.events}
        review_id = _get_workflows().save(
            "review",
            request.code,
            payload,
            status="reviewed",
        )
        payload["review_id"] = review_id
        return payload
    except Exception as exc:  # noqa: BLE001
        logger.error("Agent 审查失败：%s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Agent 审查失败：{exc}") from exc


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.post("/agent/review/stream")
def agent_review_stream(request: ReviewRequest) -> StreamingResponse:
    """SSE 流式审查：推送各角色的每一步（事件里带 role），最后推送完整结果。"""
    if not request.code.strip():
        raise HTTPException(status_code=400, detail="代码不能为空")

    strategy = _resolve_strategy(request)

    def event_source() -> Iterator[str]:
        events: "queue.Queue[Optional[tuple]]" = queue.Queue()
        box: Dict[str, Any] = {}

        def on_event(role: str, step: Any) -> None:
            events.put((role, step))

        def worker() -> None:
            try:
                pipeline = _get_pipeline(
                    request.codebase_root,
                    use_memory=request.use_memory,
                    project=request.project,
                )
                result = pipeline.review(
                    request.code,
                    file_path=request.file_path or "",
                    strategy=strategy,
                    on_event=on_event,
                    session_id=request.session_id,
                    extra_context=request.extra_context,
                )
                payload = result.to_dict(include_traces=True)
                payload["mode"] = _legacy_mode(result.strategy.value)
                payload["review_id"] = _get_workflows().save(
                    "review", request.code, payload, status="reviewed"
                )
                box["payload"] = payload
            except Exception as exc:  # noqa: BLE001
                logger.error("流式审查失败：%s", exc, exc_info=True)
                box["error"] = str(exc)
            finally:
                events.put(None)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

        yield _sse("start", {"strategy": strategy, "mock_mode": MOCK_MODE})
        try:
            while True:
                item = events.get()
                if item is None:
                    break
                role, step = item
                event = step if isinstance(step, dict) else {"detail": str(step)}
                payload = {"role": role, **event}
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


def _fix_payload(
    request: FixRequest, confirm: bool
) -> Dict[str, Any]:
    workflows = _get_workflows()
    code = request.code
    review_id = request.review_id
    selected_ids = list(request.candidate_ids)
    checkpoint = workflows.get(request.fix_id) if request.fix_id else None
    if request.fix_id and (checkpoint is None or checkpoint["kind"] != "fix"):
        raise HTTPException(status_code=404, detail="修复计划不存在")
    if checkpoint is not None:
        if request.code.strip() and request.code != checkpoint["code"]:
            raise HTTPException(
                status_code=409,
                detail="待修复代码已变化，旧修复计划不能直接执行；请重新审查并生成计划",
            )
        if checkpoint["status"] == "completed" and confirm:
            execution = checkpoint["payload"].get("execution")
            if isinstance(execution, dict):
                return execution
        code = checkpoint["code"]
        saved = checkpoint["payload"]
        report = saved.get("report") or {}
        review_payload = saved.get("review") or {"report": report}
        selected_ids = list(saved.get("candidate_ids") or [])
        review_id = checkpoint.get("parent_id") or review_id
    elif review_id:
        review_checkpoint = workflows.get(review_id)
        if review_checkpoint is None or review_checkpoint["kind"] != "review":
            raise HTTPException(status_code=404, detail="审查快照不存在")
        if code.strip() and code != review_checkpoint["code"]:
            raise HTTPException(
                status_code=409,
                detail="待修复代码与审查快照不一致；请重新审查",
            )
        code = code or review_checkpoint["code"]
        review_payload = review_checkpoint["payload"]
        report = review_payload.get("report") or {}
    else:
        report = None
        review_payload = {}

    if not code.strip():
        raise HTTPException(status_code=400, detail="代码不能为空；请提交 code、review_id 或 fix_id")
    fixer = _get_fixer(request.codebase_root)
    if checkpoint is not None or review_id:
        pass
    elif request.issues:
        # 调用方已经带着审查结果来修复（用户已确认选中的问题），不再重复跑审查，
        # 避免「Review + Fix」两次多 Agent 全量调用的成本翻倍。
        report = {"review_summary": "用户选中的候选", "issues": request.issues}
        review_payload = {"report": report, "strategy": request.strategy}
    else:
        pipeline = _get_pipeline(
            request.codebase_root, use_memory=False, project="default"
        )
        review_result = pipeline.review(
            code, file_path=request.file_path or "", strategy=request.strategy
        )
        report = review_result.report
        review_payload = review_result.to_dict(include_traces=False)
        review_id = workflows.save("review", code, review_payload, status="reviewed")
    plan = fixer.plan(
        code,
        report,
        candidate_ids=selected_ids or None,
        include_memory_suspected=request.include_memory_suspected,
    )
    payload: Dict[str, Any] = {
        "review": review_payload,
        "plan": plan.to_dict(),
        "confirm": confirm,
        "review_id": review_id,
    }
    if not confirm:
        serializable_report = report.to_legacy() if hasattr(report, "to_legacy") else report
        fix_id = workflows.save(
            "fix",
            code,
            {
                "report": serializable_report,
                "review": review_payload,
                "candidate_ids": list(plan.candidate_ids),
            },
            status="awaiting_confirmation",
            parent_id=review_id,
        )
        payload["fix_id"] = fix_id
        payload["message"] = "这是待人工确认的修复计划；确认后以 confirm=true 再次调用才会执行 Fix Agent。"
        return payload
    result, outcome = fixer.fix(code, plan, test_code=request.test_code)
    payload.update(
        {
            "result": result.to_legacy(),
            "verification": result.verification.to_dict(),
            "trace": outcome.run.to_trace_dict(),
        }
    )
    if request.fix_id:
        saved_payload = dict(checkpoint["payload"] if checkpoint is not None else {})
        saved_payload["execution"] = payload
        workflows.update_status(request.fix_id, "completed", saved_payload)
    return payload


@app.post("/agent/fix")
def agent_fix(request: FixRequest) -> Dict[str, Any]:
    """独立修复管线：默认只返回计划（人工确认），confirm=true 才真正执行。"""
    if not request.code.strip() and not request.review_id and not request.fix_id:
        raise HTTPException(status_code=400, detail="代码、review_id、fix_id 至少提供一个")
    try:
        return _fix_payload(request, confirm=request.confirm)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("Agent 修复失败：%s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Agent 修复失败：{exc}") from exc


@app.post("/agent/optimize")
def agent_optimize(request: OptimizeRequest) -> Dict[str, Any]:
    """兼容入口：审查 + 修复（等价于 /agent/fix + confirm=true）。"""
    if not request.code.strip():
        raise HTTPException(status_code=400, detail="代码不能为空")
    try:
        fix_request = FixRequest(
            code=request.code,
            file_path=request.file_path,
            codebase_root=request.codebase_root,
            test_code=request.test_code,
            strategy=request.strategy,
            candidate_ids=request.candidate_ids,
            issues=request.issues,
            include_memory_suspected=True,
            confirm=True,
        )
        payload = _fix_payload(fix_request, confirm=True)
        payload.pop("review", None)
        return payload
    except Exception as exc:  # noqa: BLE001
        logger.error("Agent 优化失败：%s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Agent 优化失败：{exc}") from exc


@app.get("/agent/workflows/{workflow_id}")
def agent_workflow(workflow_id: str) -> Dict[str, Any]:
    """读取持久化审查/修复检查点；源码不在响应中回显。"""
    item = _get_workflows().get(workflow_id)
    if item is None:
        raise HTTPException(status_code=404, detail="工作流检查点不存在")
    return {
        "id": item["id"],
        "kind": item["kind"],
        "status": item["status"],
        "parent_id": item["parent_id"],
        "payload": item["payload"],
        "created_at": item["created_at"],
        "updated_at": item["updated_at"],
    }


@app.post("/agent/feedback")
def agent_feedback(request: FeedbackRequest) -> Dict[str, Any]:
    """记录人工反馈，沉淀为长期记忆（下次审查在裁决后降低同类结论置信度）。"""
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
        "candidate_id": issue.ensure_candidate_id(),
        "memory_stats": _get_memory().stats(),
    }


@app.get("/agent/trace/{run_id}")
def agent_trace(run_id: str) -> Dict[str, Any]:
    """取回某次角色运行的持久化轨迹（JSONL 逐行解析）。"""
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


@app.post("/agent/memory/entity")
def agent_memory_entity(request: EntityMemoryRequest) -> Dict[str, Any]:
    """新增或更新一条结构化实体事实。"""
    try:
        record = _get_memory().upsert_entity(
            project=request.project,
            entity_type=request.entity_type,
            name=request.name,
            facts=request.facts,
            source=request.source,
            confidence=request.confidence,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "record": record.to_dict()}


@app.delete("/agent/memory/session/{session_id}")
def agent_memory_clear_session(session_id: str, project: str = "default") -> Dict[str, Any]:
    """显式清除某个会话的短期记忆与当前感知。"""
    removed = _get_memory().clear_session(session_id, project=project)
    return {"ok": True, "removed": removed}


@app.get("/agent/memory")
def agent_memory(project: Optional[str] = None, session_id: str = "") -> Dict[str, Any]:
    """查看持久记忆、短期会话摘要与当前感知元数据。"""
    memory = _get_memory()
    memory.ensure_loaded()
    records = memory.records
    if project:
        records = [record for record in records if record.project == project]
    response: Dict[str, Any] = {
        "stats": memory.stats(),
        "records": [record.to_dict() for record in records[-50:]],
    }
    if session_id:
        selected_project = project or "default"
        response["short_term"] = [
            record.to_dict() for record in memory.session_turns(session_id, selected_project)
        ]
        perception = memory.current_perception(selected_project, session_id)
        # 原始源码只保存在进程内，不通过管理接口回显；仅返回可审计元数据。
        response["perception"] = perception.payload if perception else None
    return response


if __name__ == "__main__":
    import uvicorn

    # 注意：这里刻意不用 emoji。Windows 控制台默认 GBK 编码，输出 emoji 会抛
    # UnicodeEncodeError 导致服务无法启动。
    print("[start] Agent API 服务：http://127.0.0.1:8000")
    print("[docs ] 接口文档：http://127.0.0.1:8000/docs")
    if MOCK_MODE:
        print("[warn ] 当前运行在 MOCK_MODE（不调用真实模型）")
    uvicorn.run(app, host="127.0.0.1", port=8000)
