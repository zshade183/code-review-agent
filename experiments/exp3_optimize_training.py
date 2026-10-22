from __future__ import annotations

import ast
import time
import warnings
from typing import Any, Dict, List, Tuple

import requests

from config import API_KEY, API_URL, MODEL_NAME, TIMEOUT, RETRY_TIMES, SLEEP_INTERVAL, MOCK_MODE
from core.optimizer import compare_code_signature
from core.prompts import build_dataset_optimize_prompt, build_optimize_prompt

warnings.filterwarnings("ignore", category=SyntaxWarning)


def _extract_code(text: str) -> str:
    if not isinstance(text, str):
        return ""
    content = text.strip()
    if "```python" in content:
        content = content.split("```python", 1)[1].split("```", 1)[0].strip()
    elif "```" in content:
        content = content.split("```", 1)[1].split("```", 1)[0].strip()
    return content.strip()


def _syntax_ok(code: str) -> bool:
    try:
        ast.parse(code)
        return True
    except SyntaxError:
        return False


def _build_issues_str(issues: List[Dict[str, Any]]) -> str:
    lines: List[str] = []
    for idx, issue in enumerate(issues):
        if not isinstance(issue, dict):
            continue
        dim = issue.get("dimension") or issue.get("category") or ""
        ln = issue.get("line_number") or ""
        desc = issue.get("risk_description") or issue.get("description") or ""
        fix = issue.get("fix_suggestion") or issue.get("fix") or "无"
        lines.append(f"{idx+1}. 【{dim}】行{ln}：{desc}。建议：{fix}")
    return "\n".join(lines)


def _call_llm(messages: List[Dict[str, str]], temperature: float = 0.1) -> Tuple[str, Dict[str, Any]]:
    if MOCK_MODE:
        return "", {"api_ok": True, "error": "", "used_fallback": False}

    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"

    payload = {"model": MODEL_NAME, "messages": messages, "temperature": float(temperature)}
    last_error = ""
    for retry in range(RETRY_TIMES):
        try:
            resp = requests.post(API_URL, headers=headers, json=payload, timeout=TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            return str(content or ""), {"api_ok": True, "error": "", "used_fallback": False}
        except Exception as e:
            last_error = str(e)
            if retry == RETRY_TIMES - 1:
                break
            time.sleep(SLEEP_INTERVAL)
    return "", {"api_ok": False, "error": last_error, "used_fallback": True}


def optimized_code_optimize(code: str, review_issues: Dict[str, Any]) -> Dict[str, Any]:
    enforce_signature = bool(review_issues.get("enforce_signature", True))
    fixed_code = review_issues.get("fixed_code")

    if MOCK_MODE:
        out = str(fixed_code or "").strip() or str(code or "")
        return {
            "optimized_code": out,
            "signature_match": True,
            "optimization_summary": "mock_optimize",
            "api_ok": True,
            "used_fallback": False,
            "changed": out.strip() != (code or "").strip(),
        }

    if not MODEL_NAME:
        return {
            "optimized_code": code,
            "signature_match": True,
            "optimization_summary": "optimize_failed: MODEL_NAME not set",
            "api_ok": False,
            "used_fallback": True,
            "changed": False,
        }

    issues = review_issues.get("issues", []) or []
    issues_str = _build_issues_str(issues if isinstance(issues, list) else [])
    prompt = build_optimize_prompt(code, issues_str) if enforce_signature else build_dataset_optimize_prompt(code, issues_str)

    raw, meta = _call_llm(
        [
            {"role": "system", "content": "你是专业代码优化助手，输出必须只包含优化后的完整代码。"},
            {"role": "user", "content": prompt},
        ],
        temperature=0.1,
    )
    extracted = _extract_code(raw)
    if not extracted:
        return {
            "optimized_code": code,
            "signature_match": True,
            "optimization_summary": f"optimize_failed: {meta.get('error','')}",
            "api_ok": bool(meta.get("api_ok", False)),
            "used_fallback": True,
            "changed": False,
        }

    if not _syntax_ok(extracted):
        return {
            "optimized_code": code,
            "signature_match": True,
            "optimization_summary": "optimize_failed: syntax_error",
            "api_ok": bool(meta.get("api_ok", True)),
            "used_fallback": True,
            "changed": False,
        }

    signature_match = compare_code_signature(code, extracted) if enforce_signature else True
    if enforce_signature and not signature_match:
        return {
            "optimized_code": code,
            "signature_match": False,
            "optimization_summary": "optimize_failed: signature_mismatch",
            "api_ok": bool(meta.get("api_ok", True)),
            "used_fallback": True,
            "changed": False,
        }

    return {
        "optimized_code": extracted,
        "signature_match": bool(signature_match),
        "optimization_summary": "optimize_ok",
        "api_ok": bool(meta.get("api_ok", True)),
        "used_fallback": False,
        "changed": extracted.strip() != (code or "").strip(),
    }

