"""Streamlit 前端：对接 FastAPI 的多 Agent 审查接口。

- 走 ``/agent/review`` 拿**结构化报告**（问题表 + 证据 + 裁决状态 + 各角色轨迹）；
- 可选走 ``/agent/review/stream``（SSE）实时显示「哪个角色正在调用哪个工具」；
- 支持 fast / auto / full 三种审查策略与覆盖度 / 部分成功展示；
- 每条结论都能一键「标记为误报」，写回后端长期记忆，下次审查在裁决后降权；
- 修复走 ``/agent/fix``：先生成计划等待人工确认，确认后才启动 Fix Agent，
  并在受限沙箱里做「语法 → 接口兼容 → 行为测试」三级验证。

启动::

    python api/main.py                                  # 先起后端
    streamlit run web/app.py                            # 再起前端
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from datetime import datetime
from urllib.parse import quote, urlencode
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

sys.path.append(str(Path(__file__).parent.parent))

import requests
import streamlit as st

from config import ENCODING, LOG_DIR

# 初始化日志
Path(LOG_DIR).mkdir(exist_ok=True)
logging.basicConfig(
    filename=Path(LOG_DIR) / "web_app.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    encoding=ENCODING,
)
logger = logging.getLogger(__name__)

STREAM_TIMEOUT = 600
REQUEST_TIMEOUT = 600

st.set_page_config(
    page_title="智能代码审查 Agent",
    page_icon="🧠",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
<style>
    :root {
        --brand-a: #4C78A8;
        --brand-b: #54A24B;
        --ink: #0f172a;
        --muted: #64748b;
        --line: #e2e8f0;
    }
    .block-container { padding-top: 2rem; max-width: 1180px; }
    .app-hero {
        padding: 0.8rem 0 1.2rem 0;
        border-bottom: 1px solid var(--line);
        margin-bottom: 1.3rem;
    }
    .app-hero h1 {
        font-size: 2.05rem;
        line-height: 1.2;
        margin: 0 0 0.3rem 0;
        letter-spacing: -0.02em;
        color: var(--ink);
    }
    .app-hero .accent {
        background: linear-gradient(90deg, var(--brand-a) 0%, var(--brand-b) 100%);
        -webkit-background-clip: text;
        background-clip: text;
        color: transparent;
    }
    .app-hero p { color: var(--muted); margin: 0; font-size: 0.95rem; }
    .pill {
        display: inline-block;
        margin: 0.7rem 0.4rem 0 0;
        padding: 0.16rem 0.62rem;
        border-radius: 999px;
        font-size: 0.76rem;
        color: #334155;
        background: #eef2f7;
        border: 1px solid var(--line);
    }
    .metric-card {
        background: #fff;
        border: 1px solid var(--line);
        border-radius: 14px;
        padding: 0.85rem 1rem 0.9rem 1rem;
        box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
        min-height: 88px;
    }
    .metric-card .metric-label { color: var(--muted); font-size: 0.78rem; }
    .metric-card .metric-value {
        color: var(--ink); font-size: 1.55rem; font-weight: 650;
        line-height: 1.9rem; margin: 0.15rem 0 0 0;
    }
    .metric-card .metric-hint { color: var(--muted); font-size: 0.72rem; }
    .metric-card.danger .metric-value { color: #b42318; }
    .metric-card.ok .metric-value { color: #15803d; }
    .sev {
        display: inline-block; padding: 0.1rem 0.5rem; border-radius: 999px;
        font-size: 0.74rem; font-weight: 600; border: 1px solid transparent;
    }
    .sev-high { background: #fee4e2; color: #b42318; border-color: #fecdca; }
    .sev-mid { background: #fef0c7; color: #b54708; border-color: #fedf89; }
    .sev-low { background: #eef2f7; color: #475569; border-color: var(--line); }
    .issue-meta { color: var(--muted); font-size: 0.82rem; }
    .issue-desc { margin: 0.45rem 0 0.65rem 0; color: var(--ink); }
    .empty-card {
        text-align: center; padding: 2.2rem 1rem; border: 1px dashed var(--line);
        border-radius: 14px; color: var(--muted); background: #fafcff;
    }
    .stTextArea textarea, textarea {
        font-family: 'JetBrains Mono', 'Consolas', 'Monaco', monospace;
        font-size: 13.5px;
    }
    section[data-testid="stSidebar"] {
        background: #f8fafc;
        border-right: 1px solid var(--line);
    }
    div[data-testid="stExpander"] { border-radius: 12px; border: 1px solid var(--line); }
    [data-testid="stToolbar"] { display: none; }
    #MainMenu { visibility: hidden; }
    .app-footer {
        color: var(--muted); font-size: 0.78rem; text-align: center;
        padding: 1.2rem 0 0.4rem 0;
    }
</style>
""",
    unsafe_allow_html=True,
)

st.markdown(
    """
<div class="app-hero">
  <h1>🧠 智能<span class="accent">代码审查</span> Agent</h1>
  <p>确定性编排的 Agent 协作：综合审查 · 独立复核 · 证据追踪 · 规则裁决 · 人工确认后修复</p>
  <span class="pill">Review Agent</span>
  <span class="pill">Verification Agent</span>
  <span class="pill">LangGraph 状态汇合</span>
  <span class="pill">确定性裁决</span>
  <span class="pill">Review / Fix 分离</span>
</div>
""",
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------------------
# 后端访问
# ---------------------------------------------------------------------------


def api_headers() -> Dict[str, str]:
    key = st.session_state.get("service_api_key", "")
    return {"x-api-key": key} if key else {}


def api_get(host: str, path: str, timeout: int = 3) -> Tuple[Optional[Dict[str, Any]], str]:
    """GET 请求；返回 (数据, 错误信息)。错误信息为空表示成功。"""
    try:
        response = requests.get(f"{host}{path}", timeout=timeout, headers=api_headers())
        response.raise_for_status()
        return response.json(), ""
    except requests.exceptions.RequestException as exc:
        return None, str(exc)
    except ValueError as exc:  # JSON 解析失败
        return None, f"响应不是合法 JSON：{exc}"


def api_post(host: str, path: str, payload: Dict[str, Any], timeout: int = REQUEST_TIMEOUT) -> Tuple[Optional[Dict[str, Any]], str]:
    """POST JSON 请求；返回 (数据, 错误信息)。"""
    try:
        response = requests.post(f"{host}{path}", json=payload, timeout=timeout, headers=api_headers())
        if response.status_code >= 400:
            detail = ""
            try:
                detail = str(response.json().get("detail") or "")
            except ValueError:
                detail = response.text[:200]
            return None, f"HTTP {response.status_code}：{detail}"
        return response.json(), ""
    except requests.exceptions.RequestException as exc:
        return None, str(exc)
    except ValueError as exc:
        return None, f"响应不是合法 JSON：{exc}"


def iter_sse(host: str, path: str, payload: Dict[str, Any]) -> Iterator[Tuple[str, Dict[str, Any]]]:
    """消费 SSE 流，逐个产出 (事件名, 数据)。"""
    with requests.post(f"{host}{path}", json=payload, stream=True, timeout=STREAM_TIMEOUT, headers=api_headers()) as response:
        if response.status_code >= 400:
            raise RuntimeError(f"HTTP {response.status_code}：{response.text[:200]}")
        event = "message"
        for raw in response.iter_lines(decode_unicode=True):
            if raw is None:
                continue
            line = raw.strip()
            if not line:
                continue
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                body = line[5:].strip()
                try:
                    yield event, json.loads(body)
                except json.JSONDecodeError:
                    yield event, {"raw": body}

# ---------------------------------------------------------------------------
# 侧边栏：后端地址 + 运行参数
# ---------------------------------------------------------------------------

with st.sidebar:
    st.header("⚙️ 运行配置")
    api_host = st.text_input("API 服务地址", value="http://127.0.0.1:8000").rstrip("/")
    st.text_input("服务 API Key（可选）", type="password", key="service_api_key")

    status, status_error = api_get(api_host, "/")
    if status_error:
        st.error("无法连接后端服务，请先运行 `python api/main.py`")
        with st.expander("错误详情"):
            st.code(status_error)
    elif status and status.get("mock_mode"):
        st.warning("后端运行在 MOCK 模式（离线模拟，不调用真实模型）")
        st.caption("想接真实模型：在 .env 里填 `API_KEY` / `MODEL_NAME`，并把 `MOCK_MODE` 设为 false。")
    else:
        st.success("后端连接正常（真实模型已启用）")

    st.markdown("---")
    strategy_label = st.radio(
        "审查策略",
        ["auto（风险候选复核，推荐）", "fast（仅初审，最快）", "full（全部候选复核）"],
        help="fast 适合编辑器实时检查，auto 只复核风险候选，full 用于 PR / CI / 重要代码。",
    )
    strategy = {
        "auto（风险候选复核，推荐）": "auto",
        "fast（仅初审，最快）": "fast",
        "full（全部候选复核）": "full",
    }[strategy_label]
    use_memory = st.toggle(
        "启用会话与长期记忆", value=True,
        help="保存审查会话以支持连续追问，同时参考项目约定与历史误报",
    )
    project = st.text_input("项目标识", value="default", help="用于隔离不同项目的记忆")
    stream_steps = st.toggle("实时显示 Agent 步骤", value=True, help="通过 SSE 边跑边看各角色工具调用")
    with st.expander("项目检索与审查要求"):
        codebase_root = st.text_input("后端代码库目录", help="填写后端机器上可访问的目录，启用跨文件检索")
        file_path = st.text_input("当前文件路径", value="snippet.py")
        extra_context = st.text_area("本轮审查要求", max_chars=6000, placeholder="例如：重点关注 SQL 输入来源")

    with st.expander("使用说明"):
        st.markdown(
            "1. 粘贴代码或上传 .py 文件\n"
            "2. 在「代码审查」「代码修复」两个标签页分别提交\n"
            "3. 审查结果里每条结论都能展开看证据、也能标为误报"
            "\n4. 审查完成后，在「连续追问」中讨论问题和修复建议"
        )

# ---------------------------------------------------------------------------
# 输入区
# ---------------------------------------------------------------------------

scope = (api_host, project)
if st.session_state.get("chat_scope") != scope:
    for key in ("review_result", "fix_plan", "fix_result", "chat_messages", "chat_ready", "reviewed_code", "chat_error", "loaded_checkpoint", "recovered_plan"):
        st.session_state.pop(key, None)
    st.session_state["chat_scope"] = scope
    st.session_state["chat_session_id"] = str(uuid.uuid4())
st.session_state.setdefault("chat_messages", [])
st.session_state.setdefault("chat_ready", False)

# 其它标签页（例如修复结果）请求回填代码时，必须在控件创建前写入 session_state
pending_code = st.session_state.pop("pending_code", None)
if pending_code is not None:
    st.session_state["input_mode"] = "直接输入"
    st.session_state["code"] = pending_code

st.session_state.setdefault("input_mode", "直接输入")

with st.container(border=True):
    st.markdown("#### 📂 待处理代码")
    input_type = st.radio(
        "输入方式",
        ["直接输入", "上传文件(.py)"],
        horizontal=True,
        label_visibility="collapsed",
        key="input_mode",
    )

    code = st.session_state.get("code", "")
    if input_type == "直接输入":
        # 不使用 key，保证切换输入方式后仍能从 session_state["code"] 回填
        typed = st.text_area(
            "代码内容",
            value=code,
            height=280,
            placeholder="def calculate(a, b):\n    return a + b * 10  # 存在魔法数字问题",
            label_visibility="collapsed",
        )
        if typed != code:
            st.session_state["code"] = typed
            code = typed
    else:
        uploaded_file = st.file_uploader("上传 Python 代码文件", type=["py"], key="code_upload")
        if uploaded_file is not None:
            file_tag = f"{uploaded_file.name}:{uploaded_file.size}"
            if st.session_state.get("code_upload_tag") != file_tag:
                try:
                    loaded = uploaded_file.read().decode(ENCODING, errors="replace")
                except Exception as exc:  # noqa: BLE001
                    st.error(f"文件读取失败：{exc}")
                    logger.error("文件上传失败: %s", exc)
                else:
                    st.session_state["code"] = loaded
                    st.session_state["code_upload_tag"] = file_tag
                    code = loaded
            if st.session_state.get("code_upload_tag") == file_tag:
                st.success(
                    f"文件读取成功：{uploaded_file.name}（{len(code.splitlines())} 行），"
                    "可切回「直接输入」继续编辑"
                )

    if code.strip():
        st.caption(f"当前待处理代码：{len(code.splitlines())} 行 · {len(code)} 字符")
    else:
        st.caption("尚未输入代码：可直接粘贴，或切换到「上传文件」选择 .py 文件。")

# ---------------------------------------------------------------------------
# 结果渲染
# ---------------------------------------------------------------------------

SEVERITY_ORDER = ["High", "Medium-High", "Medium", "Low"]

#: 严重度 -> (CSS 类, 图标)
SEVERITY_STYLE = {
    "High": ("sev-high", "🔴"),
    "Medium-High": ("sev-mid", "🟠"),
    "Medium": ("sev-mid", "🟡"),
    "Low": ("sev-low", "⚪"),
}


def severity_badge(severity: str) -> str:
    """把严重度渲染成带颜色的徽章。"""
    css, dot = SEVERITY_STYLE.get(severity, ("sev-low", "⚪"))
    return f'<span class="sev {css}">{dot} {severity}</span>'


def metric_card(label: str, value: str, hint: str = "", tone: str = "") -> str:
    """概览指标卡：圆角、细边框，可选 danger / ok 状态色。"""
    return (
        f'<div class="metric-card {tone}">'
        f'<div class="metric-label">{label}</div>'
        f'<div class="metric-value">{value}</div>'
        f'<div class="metric-hint">{hint}</div>'
        f"</div>"
    )


def render_steps(steps: List[Dict[str, Any]]) -> str:
    """把 SSE 步骤渲染成 Markdown 列表（只保留最近若干步，避免刷屏）。"""
    lines: List[str] = []
    for step in steps[-14:]:
        prefix = f"`#{step.get('index')}` **{step.get('kind') or '?'}**"
        if step.get("role"):
            prefix = f"`[{step['role']}]` " + prefix
        if step.get("tool"):
            prefix += f" `{step['tool']}`"
        lines.append(f"- {prefix} {str(step.get('brief') or '')[:160]}")
    return "\n".join(lines)


def render_trace(trace: Dict[str, Any]) -> None:
    """渲染一次运行的轨迹摘要。"""
    if not trace:
        return
    with st.expander("🔎 运行轨迹（Agent 每一步）"):
        if trace.get("run_id"):
            st.caption(f"运行 ID：{trace['run_id']}（可在“运行统计 → 查询运行轨迹”中按 ID 查看）")
        st.caption(
            f"stop_reason={trace.get('stop_reason')}｜步数={trace.get('steps')}｜"
            f"tokens={trace.get('usage', {}).get('total_tokens')}｜"
            f"耗时={round((trace.get('duration_ms') or 0) / 1000, 2)}s"
        )
        st.caption(f"工具调用分布：{json.dumps(trace.get('tool_calls') or {}, ensure_ascii=False)}")
        timeline = trace.get("timeline") or []
        if timeline:
            st.dataframe(
                [{"步": item.get("index"), "类型": item.get("kind"), "内容": item.get("brief")} for item in timeline],
                width="stretch",
                hide_index=True,
            )


def render_agent_traces(traces: List[Dict[str, Any]]) -> None:
    """渲染审查图里各角色的轨迹（stats.agent_traces）。"""
    if not traces:
        st.info("本次运行没有采集到角色轨迹。")
        return
    for trace in traces:
        tool_calls = trace.get("tool_calls") or []
        st.markdown(
            f"**{trace.get('agent') or 'agent'}** · 步数 {trace.get('steps')} · "
            f"停止原因 `{trace.get('stop_reason') or '—'}` · 工具调用 {len(tool_calls)} 次"
        )
        if not tool_calls:
            st.caption("该角色没有调用工具。")
            continue
        st.dataframe(
            [
                {
                    "步骤": call.get("step"),
                    "工具": call.get("tool"),
                    "参数": json.dumps(call.get("arguments") or {}, ensure_ascii=False)[:200],
                    "结果": "成功" if call.get("ok") else f"失败（{call.get('error_type') or '未知'}）",
                    "重试": call.get("attempts") or 0,
                    "缓存": "命中" if call.get("from_cache") else "—",
                    "耗时(ms)": round(float(call.get("duration_ms") or 0), 1),
                }
                for call in tool_calls
            ],
            width="stretch",
            hide_index=True,
        )


def render_report(report: Dict[str, Any], host: str, project: str, code: str) -> None:
    """渲染结构化审查报告：概览指标 + 可筛选的问题表 + 证据详情 + 误报反馈。"""
    issues: List[Dict[str, Any]] = report.get("issues") or []
    st.markdown(f"**摘要**：{report.get('review_summary') or '（无）'}")

    confidences = [float(i.get("confidence") or 0) for i in issues]
    high = sum(1 for i in issues if i.get("risk_level") == "高")
    unverified = sum(1 for i in issues if i.get("verified") is False)
    avg_conf = f"{sum(confidences) / len(confidences):.2f}" if confidences else "—"
    cards = [
        ("问题总数", str(len(issues)), "来自各审查维度", ""),
        ("高风险", str(high), "需要优先处理", "danger" if high else "ok"),
        ("平均置信度", avg_conf, "0-1，越高越有把握", ""),
        ("证据不足", str(unverified), "已自动降低置信度", "danger" if unverified else "ok"),
    ]
    for column, (label, value, hint, tone) in zip(st.columns(4), cards):
        column.markdown(metric_card(label, value, hint, tone), unsafe_allow_html=True)

    if not issues:
        st.markdown('<div class="empty-card">✅ 没有带证据支撑的问题</div>', unsafe_allow_html=True)
        return

    categories = sorted({str(i.get("category") or "未分类") for i in issues})
    filter_col1, filter_col2 = st.columns([2, 2])
    picked = filter_col1.multiselect("按类别筛选", categories, default=categories)
    severities = [s for s in SEVERITY_ORDER if any(i.get("severity") == s for i in issues)]
    picked_sev = filter_col2.multiselect("按严重度筛选", severities, default=severities)

    shown = [i for i in issues if i.get("category") in picked and i.get("severity") in picked_sev]
    if not shown:
        st.info("当前筛选条件下没有问题。")
        return

    if shown:
        st.dataframe(
            [
                {
                    "类别": i.get("category"),
                    "严重度": i.get("severity"),
                    "行号": i.get("line_number"),
                    "二级分类": i.get("subtype"),
                    "置信度": round(float(i.get("confidence") or 0), 2),
                    "已核实": "是" if i.get("verified") else ("否" if i.get("verified") is False else "未校验"),
                }
                for i in shown
            ],
            width="stretch",
            hide_index=True,
        )

    for idx, issue in enumerate(shown, start=1):
        _, dot = SEVERITY_STYLE.get(str(issue.get("severity")), ("sev-low", "⚪"))
        status = issue.get("adjudication") or "unadjudicated"
        title = (
            f"{dot} {issue.get('severity')} · 行 {issue.get('line_number')} · "
            f"{issue.get('subtype') or issue.get('category')} · {status}"
        )
        with st.expander(title, expanded=False):
            st.markdown(
                f"{severity_badge(str(issue.get('severity')))}"
                f"<span class='issue-meta'>　{issue.get('category')} ｜ 规则："
                f"{issue.get('violated_rule') or '—'} ｜ 置信度：{round(float(issue.get('confidence') or 0), 2)}"
                f" ｜ 裁决：{status} ｜ 来源：{'/'.join(issue.get('sources') or []) or issue.get('produced_by') or '—'}"
                f"</span>",
                unsafe_allow_html=True,
            )
            st.markdown(f"<div class='issue-desc'>{issue.get('risk_description') or ''}</div>", unsafe_allow_html=True)
            st.markdown(f"**修复建议**：{issue.get('fix_suggestion') or '—'}")
            if issue.get("verify_note"):
                st.caption(f"校验说明：{issue['verify_note']}")
            if issue.get("adjudication_reason"):
                st.caption(f"裁决说明：{issue['adjudication_reason']}")
            if issue.get("candidate_id"):
                st.caption(f"候选 ID：`{issue['candidate_id']}`（修复时按此选择）")
            for evidence in issue.get("evidence") or []:
                st.markdown(
                    f"- 证据 `{evidence.get('tool') or evidence.get('kind')}`"
                    f"（行 {evidence.get('line')}）：{evidence.get('detail') or evidence.get('snippet')}"
                )
            if st.button("标记为误报（写入长期记忆）", key=f"fb_{idx}_{issue.get('line_number')}"):
                data, error = api_post(
                    host,
                    "/agent/feedback",
                    {"code": code, "project": project, "accepted": False, "issue": issue, "note": "前端标记误报"},
                    timeout=30,
                )
                if error:
                    st.error(f"反馈失败：{error}")
                else:
                    st.success(f"已记录（{data.get('record_id')}），下次审查会压制同类形态")
            if st.button("确认问题有效", key=f"accept_{idx}_{issue.get('line_number')}"):
                data, error = api_post(host, "/agent/feedback", {
                    "code": code, "project": project, "accepted": True, "issue": issue, "note": "前端确认问题有效",
                }, timeout=30)
                if error:
                    st.error(f"反馈失败：{error}")
                else:
                    st.success("已保存有效问题反馈")


def render_verification(result: Dict[str, Any]) -> None:
    """渲染修复结果的验证情况。"""
    checks = ((result.get("verification") or {}).get("checks")) or []
    if not checks:
        return
    st.markdown("**三级验证**")
    for check in checks:
        mark = "✅" if check.get("passed") else "❌"
        st.markdown(f"- {mark} `{check.get('name')}`：{check.get('detail')}")


# ---------------------------------------------------------------------------
# 主区：审查 / 修复
# ---------------------------------------------------------------------------

st.markdown("---")
tab_review, tab_fix, tab_chat, tab_manage = st.tabs(["🔍 代码审查", "🛠️ 代码修复", "💬 连续追问", "📊 运行与记忆"])

with tab_review:
    left, right = st.columns([1, 4])
    trigger = left.button("开始审查", type="primary", width="stretch")
    right.caption(
        "确定性编排：基础上下文 → Review Agent → 候选校验 → "
        "条件式 Verification Agent → 规则裁决。"
    )

    if trigger:
        if not code.strip():
            st.warning("请先输入或上传代码")
        else:
            payload = {
                "code": code,
                "strategy": strategy,
                "use_memory": use_memory,
                "project": project,
                "session_id": st.session_state["chat_session_id"],
                "file_path": file_path,
                "codebase_root": codebase_root or None,
                "extra_context": extra_context,
            }
            logger.info("用户触发审查：strategy=%s stream=%s", strategy, stream_steps)
            result: Optional[Dict[str, Any]] = None
            if stream_steps:
                with st.status("Agent 正在工作…", expanded=True) as status_box:
                    steps: List[Dict[str, Any]] = []
                    placeholder = st.empty()
                    stream_meta: Dict[str, Any] = {}
                    try:
                        for event, data in iter_sse(api_host, "/agent/review/stream", payload):
                            if event == "start":
                                stream_meta.update(data or {})
                                mode_hint = "（MOCK 模式）" if stream_meta.get("mock_mode") else ""
                                status_box.update(
                                    label=f"Agent 正在工作…策略 {stream_meta.get('strategy') or strategy}{mode_hint}",
                                    state="running",
                                    expanded=True,
                                )
                            elif event == "step":
                                steps.append(data)
                                placeholder.markdown(render_steps(steps))
                            elif event == "result":
                                result = data
                            elif event == "error":
                                raise RuntimeError(data.get("message") or "后端返回错误")
                            elif event == "end" and not data.get("ok"):
                                raise RuntimeError("后端流式执行未成功完成")
                        status_box.update(
                            label=f"完成（{len(steps)} 步）", state="complete", expanded=False
                        )
                        if not result:
                            st.warning("流式响应已结束，但没有收到完整结果；可关闭「实时显示 Agent 步骤」后重试。")
                    except Exception as exc:  # noqa: BLE001 - 前端需要把任何失败展示出来
                        status_box.update(label="执行失败", state="error")
                        st.error(str(exc))
                        logger.error("流式审查失败: %s", exc)
            else:
                with st.spinner("Agent 正在工作…"):
                    result, error = api_post(api_host, "/agent/review", payload)
                if error:
                    st.error(f"审查失败：{error}")
                    logger.error("审查失败: %s", error)
            st.session_state["review_result"] = result
            st.session_state["chat_ready"] = bool(result and use_memory)
            if result:
                st.session_state["reviewed_code"] = code
                st.session_state["chat_messages"] = []
                st.session_state.pop("chat_error", None)
                st.session_state.pop("fix_plan", None)
                st.session_state.pop("fix_result", None)

    review_result = st.session_state.get("review_result")
    if review_result:
        st.markdown("### 📝 审查结果")
        stats = review_result.get("stats") or {}
        coverage = review_result.get("coverage") or {}
        st.markdown(
            f"**策略**：`{review_result.get('strategy')}` ｜ "
            f"**角色**：{('、'.join(stats.get('selected_roles') or []) or 'review')} ｜ "
            f"**部分成功**：{coverage.get('partial')}"
        )
        if coverage.get("incomplete"):
            st.warning(f"以下维度未完成（其余结果仍有效）：{coverage.get('incomplete')}")
        adjudication = stats.get("adjudication") or {}
        columns = st.columns(4)
        for column, (label, key) in zip(
            columns,
            [("已确认", "confirmed"), ("待人工复核", "needs_review"),
             ("记忆存疑", "memory_suspected"), ("已驳回", "rejected")],
        ):
            column.metric(label, adjudication.get(key, 0))
        if review_result.get("review_id") or review_result.get("run_id"):
            st.caption(
                f"审查快照 ID：{review_result.get('review_id') or '—'} ｜ "
                f"运行轨迹 ID：{review_result.get('run_id') or '—'}"
            )
        render_report(review_result.get("report") or {}, api_host, project, st.session_state.get("reviewed_code", code))
        if not ((review_result.get("report") or {}).get("issues") or []):
            # 候选可能因字段校验失败被后端丢弃，把原因直接露出来，避免只看到“0 条问题”
            review_detail = str(((coverage.get("dimensions") or {}).get("review") or {}).get("detail") or "")
            st.warning(
                "本次审查没有留下问题。"
                + (f"审查阶段说明：{review_detail}" if review_detail else "可补充上下文，或换用 full 策略后重试。")
            )
        with st.expander("🔎 各角色轨迹"):
            render_agent_traces(stats.get("agent_traces") or [])
        dimensions = coverage.get("dimensions") or {}
        with st.expander("📋 审查覆盖度"):
            if dimensions:
                st.dataframe(
                    [
                        {
                            "维度/角色": name,
                            "状态": item.get("status"),
                            "问题数": item.get("issues"),
                            "耗时(ms)": round(float(item.get("elapsed_ms") or 0), 1),
                            "说明": item.get("detail"),
                        }
                        for name, item in dimensions.items()
                    ],
                    width="stretch",
                    hide_index=True,
                )
            else:
                st.caption("后端未返回维度覆盖信息。")
            if review_result.get("reviewed_dimensions"):
                st.caption("已审查维度：" + "、".join(review_result["reviewed_dimensions"]))
        with st.expander("🧾 完整阶段事件"):
            st.json(review_result.get("events") or [])
        evidence_count = stats.get("evidence_records")
        st.caption(
            f"耗时 {round((review_result.get('elapsed_ms') or 0) / 1000, 2)}s"
            + (f" ｜ 证据记录 {evidence_count} 条" if evidence_count is not None else "")
        )
        st.download_button(
            "📥 下载审查报告(JSON)",
            data=json.dumps(review_result, ensure_ascii=False, indent=2),
            file_name="code_review_report.json",
            mime="application/json",
        )
    else:
        st.markdown(
            '<div class="empty-card">粘贴或上传代码后点击「开始审查」，'
            "结果会在这里按维度展示，并可展开查看证据与裁决状态。</div>",
            unsafe_allow_html=True,
        )

with tab_chat:
    st.markdown("### 💬 围绕审查结果继续讨论")
    st.caption("可以询问问题原因、风险和修复思路。执行代码修改请使用「代码修复」。")
    if not use_memory:
        st.info("请启用侧边栏的「会话与长期记忆」，然后重新审查代码。")
    elif not st.session_state["chat_ready"]:
        st.info("先完成一次代码审查，即可在这里连续追问。服务重启或清除会话后，请重新审查。")
    if st.session_state.get("reviewed_code") and code != st.session_state["reviewed_code"]:
        st.warning("编辑区代码已变化。当前对话仍基于上次审查的代码；请重新审查以讨论新版本。")
    messages = st.session_state["chat_messages"]
    if not messages and st.session_state["chat_ready"]:
        st.markdown("你可以问：**哪个问题最值得优先修复？** 或 **这个问题为什么会发生？**")
    for message in messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])
    if st.session_state.get("chat_error"):
        st.error(st.session_state["chat_error"])
    question = st.chat_input("输入追问，例如：请解释第一个问题，并给出修复建议", max_chars=6000,
                             disabled=not (use_memory and st.session_state["chat_ready"]))
    if question:
        with st.spinner("正在结合审查结果回答…"):
            reply, error = api_post(api_host, "/agent/followup", {
                "project": project, "session_id": st.session_state["chat_session_id"], "question": question,
            })
        answer = reply.get("answer") if isinstance(reply, dict) else None
        if error or not isinstance(answer, str) or not answer.strip():
            st.session_state["chat_error"] = f"追问失败：{error or '后端未返回有效回答'}。可重新提交问题；会话过期时请重新审查。"
            st.error(st.session_state["chat_error"])
            with st.expander("复制刚才的问题后重试", expanded=True):
                st.code(question, language=None)
        else:
            messages.extend([{"role": "user", "content": question}, {"role": "assistant", "content": answer}])
            st.session_state["chat_messages"] = messages[-40:]
            st.session_state.pop("chat_error", None)
            st.rerun()
    st.caption("会话保存在当前后端进程；服务重启后不会保留。")
    if st.button("清除对话并开始新会话", key="clear_chat"):
        try:
            response = requests.delete(f"{api_host}/agent/memory/session/{st.session_state['chat_session_id']}",
                                       params={"project": project}, timeout=10, headers=api_headers())
            response.raise_for_status()
        except requests.exceptions.RequestException as exc:
            st.error(f"清除失败，原会话仍保留：{exc}")
        else:
            st.session_state["chat_session_id"] = str(uuid.uuid4())
            st.session_state["chat_messages"] = []
            st.session_state["chat_ready"] = False
            st.session_state.pop("chat_error", None)
            st.rerun()

with tab_fix:
    fix_flash = st.session_state.pop("fix_flash", None)
    if fix_flash:
        st.success(fix_flash)
    st.caption(
        "审查与修复是两条独立管线：先在「代码审查」得到候选，人工确认要修哪些，"
        "再交给 Fix Agent；修复必须通过「语法 → 接口兼容 → 行为测试」三级验证，不通过会回退原代码。"
    )
    test_code = st.text_area(
        "验收测试代码（可选，填了会真实跑一遍行为测试）",
        height=120,
        key="test_code",
        placeholder="from module import calculate\n\ndef test_fix():\n    assert calculate(1, 2) == 3",
    )
    review_result = st.session_state.get("review_result") or {}
    review_issues = (review_result.get("report") or {}).get("issues") or []

    if not review_issues:
        st.info("请先在「代码审查」页完成一次审查，修复计划需要基于审查候选生成。")
    else:
        option_map: Dict[str, str] = {}
        missing_candidates = 0
        for issue in review_issues:
            candidate_id = issue.get("candidate_id") or ""
            if not candidate_id:
                missing_candidates += 1
                continue
            option_map[candidate_id] = (
                f"{issue.get('risk_level')}｜{issue.get('category')} / {issue.get('subtype')}"
                f"｜行 {issue.get('line_number')}｜{issue.get('adjudication') or 'unadjudicated'}"
            )
        if missing_candidates:
            st.caption(f"有 {missing_candidates} 条结论缺少候选 ID，无法按候选定向修复，已从选项中忽略。")
        if not option_map:
            st.warning("本次审查结果没有可用的候选 ID，请重新审查后再试。")
        else:
            default_ids = [
                issue.get("candidate_id")
                for issue in review_issues
                if issue.get("candidate_id")
                and (issue.get("adjudication") or "confirmed") in {"confirmed", "needs_review"}
            ][:5]
            selected_ids = st.multiselect(
                "选择要修复的候选（默认选择已确认/待复核的前 5 条）",
                options=list(option_map),
                default=default_ids,
                format_func=lambda candidate_id: option_map.get(candidate_id, candidate_id),
            )
            include_memory_suspected = st.toggle(
                "也修复「记忆存疑」候选", value=False,
                help="记忆存疑默认不自动修复；需要人工显式勾选",
            )
            if st.button("生成修复计划（人工确认）", type="primary"):
                with st.spinner("生成修复计划…"):
                    plan_result, error = api_post(
                        api_host,
                        "/agent/fix",
                        {
                            "code": code,
                            "review_id": review_result.get("review_id") or "",
                            "codebase_root": codebase_root or None,
                            "issues": review_issues,
                            "candidate_ids": selected_ids,
                            "include_memory_suspected": include_memory_suspected,
                            "strategy": review_result.get("strategy") or "auto",
                            "confirm": False,
                        },
                    )
                if error:
                    st.error(f"生成修复计划失败：{error}")
                    logger.error("生成修复计划失败: %s", error)
                else:
                    st.session_state["fix_plan"] = plan_result
                    st.session_state["fix_include_memory_suspected"] = include_memory_suspected

            fix_plan = st.session_state.get("fix_plan")
            if fix_plan:
                plan = fix_plan.get("plan") or {}
                st.markdown("### 🧾 待确认修复计划")
                if fix_plan.get("message"):
                    st.caption(fix_plan["message"])
                if fix_plan.get("fix_id"):
                    st.caption(f"修复计划 ID：`{fix_plan['fix_id']}`（确认后凭此执行）")
                if plan.get("empty"):
                    st.warning("没有选中可修复的问题。")
                else:
                    for issue in plan.get("selected") or []:
                        st.markdown(
                            f"- `{issue.get('candidate_id')}` [{issue.get('severity')}] "
                            f"{issue.get('category')} / {issue.get('subtype')} @行 {issue.get('line_number')}"
                        )
                    if plan.get("excluded"):
                        with st.expander("已跳过的候选与原因"):
                            st.dataframe(
                                [
                                    {
                                        "候选 ID": item.get("candidate_id"),
                                        "原因": item.get("reason"),
                                    }
                                    for item in plan.get("excluded") or []
                                ],
                                width="stretch",
                                hide_index=True,
                            )
                    if st.button("确认并执行修复", type="primary"):
                        with st.spinner("Fix Agent 正在修复并验证…"):
                            fix_result, error = api_post(
                                api_host,
                                "/agent/fix",
                                {
                                    "code": code,
                                    "fix_id": fix_plan.get("fix_id") or "",
                                    "codebase_root": codebase_root or None,
                                    "issues": review_issues,
                                    "candidate_ids": plan.get("candidate_ids") or [],
                                    "include_memory_suspected": st.session_state.get(
                                        "fix_include_memory_suspected", False
                                    ),
                                    "strategy": review_result.get("strategy") or "auto",
                                    "test_code": test_code,
                                    "confirm": True,
                                },
                            )
                        if error:
                            st.error(f"修复失败：{error}")
                            logger.error("修复失败: %s", error)
                        else:
                            st.session_state["fix_result"] = fix_result

    fix_result = st.session_state.get("fix_result")
    if fix_result:
        result = fix_result.get("result") or {}
        verification = fix_result.get("verification") or result.get("verification") or {}
        st.markdown("### 🛠️ 修复结果")
        st.markdown(f"**说明**：{result.get('optimization_summary') or '（无）'}")
        st.caption(
            f"验证通过={verification.get('passed')}｜接口兼容={result.get('signature_match')}｜"
            f"有改动={result.get('changed')}｜回退原代码={result.get('used_fallback')}｜"
            f"尝试次数={result.get('attempts')}"
        )
        render_verification(result)
        if result.get("patch"):
            with st.expander("改动补丁（unified diff）", expanded=True):
                st.code(result["patch"], language="diff")
        st.code(result.get("optimized_code") or "（无）", language="python")
        action_col1, action_col2 = st.columns(2)
        action_col1.download_button(
            "📥 下载修复后代码",
            data=result.get("optimized_code") or "",
            file_name="optimized_code.py",
            mime="text/plain",
        )
        if result.get("optimized_code") and action_col2.button("↩️ 回填到编辑区", key="apply_fix_to_editor"):
            st.session_state["pending_code"] = result["optimized_code"]
            st.rerun()
        render_trace(fix_result.get("trace") or {})
    else:
        st.markdown(
            '<div class="empty-card">先审查、再选择候选、确认计划，最后才启动 Fix Agent。'
            "填写验收测试可以额外跑一遍行为测试。</div>",
            unsafe_allow_html=True,
        )

with tab_manage:
    st.markdown("### 📊 运行统计与项目记忆")
    st.caption("按需查询后端记录。运行统计基于已持久化的轨迹，不代表所有请求都已采集。")
    if st.button("刷新运行统计", key="load_stats"):
        data, error = api_get(api_host, "/agent/stats", timeout=30)
        if error:
            st.error(error)
        else:
            st.session_state["stats_data"] = data
    stats_data = st.session_state.get("stats_data")
    if stats_data:
        runs = int(stats_data.get("runs") or 0)
        if not runs:
            st.info("还没有采集到运行轨迹：完成一次审查或修复后会自动落盘。")
        else:
            cost = float(stats_data.get("avg_cost") or 0)
            cols = st.columns(4)
            cols[0].metric("运行次数", runs)
            cols[1].metric("平均 Token", f"{float(stats_data.get('avg_tokens') or 0):.0f}")
            cols[2].metric("平均耗时", f"{float(stats_data.get('avg_duration_ms') or 0) / 1000:.2f}s")
            cols[3].metric("平均估算费用", "未配置单价" if cost <= 0 else f"${cost:.6f}")
        stop_reasons = stats_data.get("stop_reasons") or {}
        tool_usage = stats_data.get("tool_usage") or {}
        if stop_reasons:
            st.markdown("**停止原因分布**")
            st.dataframe(
                [{"停止原因": name, "次数": count} for name, count in stop_reasons.items()],
                width="stretch",
                hide_index=True,
            )
        if tool_usage:
            st.markdown("**工具调用分布**")
            st.dataframe(
                [{"工具": name, "次数": count} for name, count in tool_usage.items()],
                width="stretch",
                hide_index=True,
            )
        with st.expander("原始统计 JSON"):
            st.json(stats_data)

    if st.button("查看当前项目与会话记忆", key="load_memory"):
        query = urlencode({"project": project, "session_id": st.session_state["chat_session_id"]})
        data, error = api_get(api_host, f"/agent/memory?{query}", timeout=30)
        if error:
            st.error(error)
        else:
            st.session_state["memory_data"] = data
    memory_data = st.session_state.get("memory_data")
    if memory_data:
        memory_stats = memory_data.get("stats") or {}
        cols = st.columns(4)
        cols[0].metric("长期记忆", memory_stats.get("total", 0))
        cols[1].metric("短期会话", memory_stats.get("short_term_sessions", 0))
        cols[2].metric("当前感知", memory_stats.get("perceptions", 0))
        cols[3].metric("存储后端", memory_stats.get("backend") or "—")
        by_kind = memory_stats.get("by_kind") or {}
        if by_kind:
            st.caption("记忆类型分布：" + "、".join(f"{kind} {count}" for kind, count in by_kind.items()))
        records = memory_data.get("records") or []
        if records:
            st.markdown("**长期记忆（最近 50 条）**")
            st.dataframe(
                [
                    {
                        "类型": record.get("kind"),
                        "项目": record.get("project"),
                        "内容": (record.get("text") or "").replace("\n", " ")[:120],
                        "时间": datetime.fromtimestamp(float(record.get("created_at") or 0)).strftime("%Y-%m-%d %H:%M"),
                    }
                    for record in records
                ],
                width="stretch",
                hide_index=True,
            )
        else:
            st.caption("当前项目还没有长期记忆记录。")
        turns = memory_data.get("short_term") or []
        if turns:
            st.markdown("**当前会话短期记忆**")
            st.dataframe(
                [
                    {
                        "角色": (turn.get("payload") or {}).get("role"),
                        "内容": (turn.get("text") or "").replace("\n", " ")[:160],
                        "时间": datetime.fromtimestamp(float(turn.get("created_at") or 0)).strftime("%Y-%m-%d %H:%M"),
                    }
                    for turn in turns
                ],
                width="stretch",
                hide_index=True,
            )
        if memory_data.get("perception"):
            with st.expander("当前感知元数据（不含源码）"):
                st.json(memory_data["perception"])

    with st.expander("新增或更新项目事实"):
        with st.form("entity_memory"):
            entity_type = st.selectbox("事实类型", ["project", "file", "component"])
            entity_name = st.text_input("名称", placeholder="例如：数据库访问层")
            entity_facts = st.text_area("结构化事实（JSON 对象）", value='{"database": "PostgreSQL"}')
            save_entity = st.form_submit_button("保存项目事实")
        if save_entity:
            try:
                facts = json.loads(entity_facts)
                if not entity_name.strip() or not isinstance(facts, dict):
                    raise ValueError("名称不能为空，事实必须是 JSON 对象")
            except ValueError as exc:
                st.error(f"输入错误：{exc}")
            else:
                data, error = api_post(api_host, "/agent/memory/entity", {
                    "project": project, "entity_type": entity_type, "name": entity_name.strip(), "facts": facts,
                }, timeout=30)
                if error:
                    st.error(error)
                else:
                    st.success(f"项目事实已保存（{data.get('record', {}).get('id') or '已更新'}）")
                    st.json(data)

    with st.expander("查询持久化运行轨迹"):
        run_id = st.text_input("运行 ID", key="lookup_run_id")
        if st.button("查询运行轨迹", key="lookup_trace"):
            if not run_id.strip():
                st.warning("请输入运行 ID")
            else:
                data, error = api_get(api_host, f"/agent/trace/{quote(run_id.strip(), safe='')}", timeout=30)
                if error:
                    st.error(error)
                else:
                    lines = data.get("lines") or []
                    meta = next((line for line in lines if line.get("type") == "run_meta"), {})
                    if meta:
                        usage = meta.get("usage") or {}
                        st.caption(
                            f"任务={meta.get('task')}｜停止原因={meta.get('stop_reason')}｜"
                            f"耗时={round(float(meta.get('duration_ms') or 0) / 1000, 2)}s｜"
                            f"Token={usage.get('total_tokens')}｜错误={meta.get('error') or '无'}"
                        )
                        if meta.get("tool_calls"):
                            st.dataframe(
                                [{"工具": name, "次数": count} for name, count in meta["tool_calls"].items()],
                                width="stretch",
                                hide_index=True,
                            )
                    steps = [line for line in lines if line.get("type") == "step"]
                    if steps:
                        st.dataframe(
                            [
                                {
                                    "步": step.get("index"),
                                    "类型": step.get("kind"),
                                    "内容": str(step.get("content") or "")[:160],
                                    "工具": (step.get("tool_call") or {}).get("name"),
                                    "耗时(ms)": round(float(step.get("latency_ms") or 0), 1),
                                    "Token": (step.get("usage") or {}).get("total_tokens"),
                                }
                                for step in steps
                            ],
                            width="stretch",
                            hide_index=True,
                        )
                    else:
                        st.caption("该轨迹没有步骤记录。")
                    with st.expander("原始轨迹行"):
                        st.json(lines)

    with st.expander("恢复审查或修复检查点"):
        checkpoint_id = st.text_input("审查 / 修复 ID", key="checkpoint_id")
        if st.button("读取检查点", key="load_checkpoint"):
            st.session_state.pop("loaded_checkpoint", None)
            st.session_state.pop("recovered_plan", None)
            if not checkpoint_id.strip():
                st.warning("请输入检查点 ID")
            else:
                data, error = api_get(api_host, f"/agent/workflows/{quote(checkpoint_id.strip(), safe='')}", timeout=30)
                if error:
                    st.error(error)
                else:
                    st.session_state["loaded_checkpoint"] = data
        checkpoint = st.session_state.get("loaded_checkpoint")
        if checkpoint:
            st.caption(
                f"类型：{checkpoint.get('kind')} · 状态：{checkpoint.get('status')} · "
                f"父 ID：{checkpoint.get('parent_id') or '—'}"
            )
            with st.expander("检查点内容"):
                st.json(checkpoint.get("payload") or {})
            if checkpoint.get("kind") == "review":
                if st.button("根据此快照生成待确认计划", key="recover_plan"):
                    data, error = api_post(api_host, "/agent/fix", {"review_id": checkpoint["id"], "confirm": False,
                                          "codebase_root": codebase_root or None})
                    if error:
                        st.error(error)
                    else:
                        st.session_state["recovered_plan"] = data
            recovered = st.session_state.get("recovered_plan")
            if recovered:
                plan = recovered.get("plan") or {}
                st.caption(
                    f"计划 ID：{recovered.get('fix_id') or '—'}｜候选 {len(plan.get('candidate_ids') or [])} 条"
                    f"｜跳过 {len(plan.get('excluded') or [])} 条"
                )
                for issue in plan.get("selected") or []:
                    st.markdown(
                        f"- `{issue.get('candidate_id')}` [{issue.get('severity')}] "
                        f"{issue.get('category')} / {issue.get('subtype')} @行 {issue.get('line_number')}"
                    )
                with st.expander("计划原始 JSON"):
                    st.json(recovered)
            recovery_id = checkpoint["id"] if checkpoint.get("kind") == "fix" else (recovered or {}).get("fix_id")
            if recovery_id and checkpoint.get("status") != "completed":
                st.caption("将使用检查点保存的源码，不使用当前编辑区代码。请先核对上方问题与候选。")
                recovery_tests = st.text_area("恢复修复的验收测试（可选）", key="recovery_tests")
                if st.button("确认并执行此检查点修复", key="execute_checkpoint"):
                    with st.spinner("正在恢复修复并验证…"):
                        data, error = api_post(api_host, "/agent/fix", {"fix_id": recovery_id, "confirm": True,
                                              "test_code": recovery_tests, "codebase_root": codebase_root or None})
                    if error:
                        st.error(error)
                    else:
                        st.session_state["fix_result"] = data
                        st.session_state["fix_flash"] = "检查点修复执行完成，已切换到「代码修复」页展示结果与验证详情。"
                        st.rerun()

st.markdown(
    '<div class="app-footer">多 Agent 代码审查 · 共享证据 / 领域协作 / 独立验证 / 规则裁决 · '
    'Review 与 Fix 分离 · 接口文档 <code>/docs</code></div>',
    unsafe_allow_html=True,
)
