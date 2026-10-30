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
  <p>确定性编排的多 Agent 协作：共享证据 · 领域专家并行 · 独立验证 · 规则裁决 · 人工确认后修复</p>
  <span class="pill">Evidence Collector</span>
  <span class="pill">四领域 Reviewer</span>
  <span class="pill">Critic / Verifier 并行</span>
  <span class="pill">确定性裁决</span>
  <span class="pill">Review / Fix 分离</span>
</div>
""",
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------------------
# 后端访问
# ---------------------------------------------------------------------------


def api_get(host: str, path: str, timeout: int = 3) -> Tuple[Optional[Dict[str, Any]], str]:
    """GET 请求；返回 (数据, 错误信息)。错误信息为空表示成功。"""
    try:
        response = requests.get(f"{host}{path}", timeout=timeout)
        response.raise_for_status()
        return response.json(), ""
    except requests.exceptions.RequestException as exc:
        return None, str(exc)
    except ValueError as exc:  # JSON 解析失败
        return None, f"响应不是合法 JSON：{exc}"


def api_post(host: str, path: str, payload: Dict[str, Any], timeout: int = REQUEST_TIMEOUT) -> Tuple[Optional[Dict[str, Any]], str]:
    """POST JSON 请求；返回 (数据, 错误信息)。"""
    try:
        response = requests.post(f"{host}{path}", json=payload, timeout=timeout)
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
    with requests.post(f"{host}{path}", json=payload, stream=True, timeout=STREAM_TIMEOUT) as response:
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
        ["auto（按证据选择领域，推荐）", "fast（单 Agent，最快）", "full（四领域 + 复核）"],
        help="多 Agent 是默认能力，但不必每次请求都无条件调用全部角色："
             "fast 适合编辑器实时检查，auto 是默认策略，full 用于 PR / CI / 重要代码。",
    )
    strategy = {
        "auto（按证据选择领域，推荐）": "auto",
        "fast（单 Agent，最快）": "fast",
        "full（四领域 + 复核）": "full",
    }[strategy_label]
    use_memory = st.toggle(
        "启用长期记忆信号", value=True,
        help="历史误报只在裁决后降低置信度并单独分组，不会在独立验证前删除候选",
    )
    project = st.text_input("项目标识", value="default", help="用于隔离不同项目的记忆")
    stream_steps = st.toggle("实时显示 Agent 步骤", value=True, help="通过 SSE 边跑边看各角色工具调用")

    with st.expander("使用说明"):
        st.markdown(
            "1. 粘贴代码或上传 .py 文件\n"
            "2. 在「代码审查」「代码修复」两个标签页分别提交\n"
            "3. 审查结果里每条结论都能展开看证据、也能标为误报"
        )

# ---------------------------------------------------------------------------
# 输入区
# ---------------------------------------------------------------------------

code = ""
with st.container(border=True):
    st.markdown("#### 📂 待处理代码")
    input_type = st.radio(
        "输入方式",
        ["直接输入", "上传文件(.py)"],
        horizontal=True,
        label_visibility="collapsed",
    )

    if input_type == "直接输入":
        code = st.text_area(
            "代码内容",
            height=280,
            placeholder="def calculate(a, b):\n    return a + b * 10  # 存在魔法数字问题",
            label_visibility="collapsed",
        )
    else:
        uploaded_file = st.file_uploader("上传 Python 代码文件", type=["py"])
        if uploaded_file is not None:
            try:
                code = uploaded_file.read().decode(ENCODING, errors="replace")
                st.success(f"文件读取成功：{uploaded_file.name}（{len(code.splitlines())} 行）")
            except Exception as exc:  # noqa: BLE001
                st.error(f"文件读取失败：{exc}")
                logger.error("文件上传失败: %s", exc)

    if code.strip():
        st.session_state["code"] = code

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
tab_review, tab_fix = st.tabs(["🔍 代码审查", "🛠️ 代码修复"])

with tab_review:
    left, right = st.columns([1, 4])
    trigger = left.button("开始审查", type="primary", width="stretch")
    right.caption(
        "确定性编排：Evidence Collector → 领域 Reviewer 并行 → Candidate Merger → "
        "Critic / Verifier 并行 → 规则裁决 → 记忆调整。"
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
            }
            logger.info("用户触发审查：strategy=%s stream=%s", strategy, stream_steps)
            result: Optional[Dict[str, Any]] = None
            if stream_steps:
                with st.status("Agent 正在工作…", expanded=True) as status_box:
                    steps: List[Dict[str, Any]] = []
                    placeholder = st.empty()
                    try:
                        for event, data in iter_sse(api_host, "/agent/review/stream", payload):
                            if event == "step":
                                steps.append(data)
                                placeholder.markdown(render_steps(steps))
                            elif event == "result":
                                result = data
                            elif event == "error":
                                raise RuntimeError(data.get("message") or "后端返回错误")
                        status_box.update(
                            label=f"完成（{len(steps)} 步）", state="complete", expanded=False
                        )
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

    review_result = st.session_state.get("review_result")
    if review_result:
        st.markdown("### 📝 审查结果")
        stats = review_result.get("stats") or {}
        coverage = review_result.get("coverage") or {}
        plan_info = review_result.get("plan") or {}
        st.markdown(
            f"**策略**：`{review_result.get('strategy')}` ｜ "
            f"**领域**：{('、'.join(plan_info.get('reviewer_roles') or []) or '单 Agent')} ｜ "
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
        render_report(review_result.get("report") or {}, api_host, project, code)
        with st.expander("🔎 各角色轨迹"):
            st.caption(
                f"tokens={stats.get('tokens')}｜耗时={round((stats.get('elapsed_ms') or 0) / 1000, 2)}s｜"
                f"critic={json.dumps(stats.get('critic') or {}, ensure_ascii=False)}"
            )
            for role, trace in (review_result.get("traces") or {}).items():
                st.markdown(
                    f"- `{role}` stop={trace.get('stop_reason')} steps={trace.get('steps')} "
                    f"tokens={trace.get('usage', {}).get('total_tokens')} "
                    f"tools={json.dumps(trace.get('tool_calls') or {}, ensure_ascii=False)}"
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

with tab_fix:
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
        option_map = {}
        for issue in review_issues:
            candidate_id = issue.get("candidate_id") or ""
            option_map[candidate_id] = (
                f"{issue.get('risk_level')}｜{issue.get('category')} / {issue.get('subtype')}"
                f"｜行 {issue.get('line_number')}｜{issue.get('adjudication') or 'unadjudicated'}"
            )
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
            st.session_state["fix_plan"] = plan_result

        fix_plan = st.session_state.get("fix_plan")
        if fix_plan:
            plan = fix_plan.get("plan") or {}
            st.markdown("### 🧾 待确认修复计划")
            if plan.get("empty"):
                st.warning("没有选中可修复的问题。")
            else:
                for issue in plan.get("selected") or []:
                    st.markdown(
                        f"- `{issue.get('candidate_id')}` [{issue.get('severity')}] "
                        f"{issue.get('category')} / {issue.get('subtype')} @行 {issue.get('line_number')}"
                    )
                if plan.get("excluded"):
                    st.caption(f"已跳过：{plan.get('excluded')}")
                if st.button("确认并执行修复", type="primary"):
                    with st.spinner("Fix Agent 正在修复并验证…"):
                        fix_result, error = api_post(
                            api_host,
                            "/agent/fix",
                            {
                                "code": code,
                                "issues": review_issues,
                                "candidate_ids": plan.get("candidate_ids") or [],
                                "include_memory_suspected": True,
                                "strategy": review_result.get("strategy") or "auto",
                                "test_code": test_code,
                                "confirm": True,
                            },
                        )
                    if error:
                        st.error(f"修复失败：{error}")
                        logger.error("修复失败: %s", error)
                    st.session_state["fix_result"] = fix_result

    fix_result = st.session_state.get("fix_result")
    if fix_result:
        result = fix_result.get("result") or {}
        st.markdown("### 🛠️ 修复结果")
        st.markdown(f"**说明**：{result.get('optimization_summary') or '（无）'}")
        st.caption(
            f"接口兼容={result.get('signature_match')}｜有改动={result.get('changed')}｜"
            f"回退原代码={result.get('used_fallback')}｜尝试次数={result.get('attempts')}"
        )
        render_verification(result)
        if result.get("patch"):
            with st.expander("改动补丁（unified diff）", expanded=True):
                st.code(result["patch"], language="diff")
        st.code(result.get("optimized_code") or "（无）", language="python")
        st.download_button(
            "📥 下载修复后代码",
            data=result.get("optimized_code") or "",
            file_name="optimized_code.py",
            mime="text/plain",
        )
        render_trace(fix_result.get("trace") or {})
    else:
        st.markdown(
            '<div class="empty-card">先审查、再选择候选、确认计划，最后才启动 Fix Agent。'
            "填写验收测试可以额外跑一遍行为测试。</div>",
            unsafe_allow_html=True,
        )

st.markdown(
    '<div class="app-footer">多 Agent 代码审查 · 共享证据 / 领域协作 / 独立验证 / 规则裁决 · '
    'Review 与 Fix 分离 · 接口文档 <code>/docs</code></div>',
    unsafe_allow_html=True,
)
