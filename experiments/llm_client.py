from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

import requests
import re

from config import API_KEY, API_URL, MODEL_NAME, TIMEOUT, RETRY_TIMES, SLEEP_INTERVAL, MOCK_MODE


def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    if not isinstance(text, str) or not text:
        return None
    decoder = json.JSONDecoder()
    fallback: Optional[Dict[str, Any]] = None
    for m in re.finditer(r"\{", text):
        i = m.start()
        try:
            obj, _ = decoder.raw_decode(text[i:])
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        if "issues" in obj or "review_summary" in obj:
            return obj
        if fallback is None:
            fallback = obj
    return fallback


def chat(messages: List[Dict[str, str]], temperature: float = 0.2) -> Dict[str, Any]:
    if MOCK_MODE:
        # 从消息中提取代码
        code = ""
        for msg in messages:
            if msg.get("role") == "user":
                content = msg.get("content", "")
                # 提取代码块
                if "```python" in content:
                    code = content.split("```python")[1].split("```")[0].strip()
                elif "```" in content:
                    code = content.split("```")[1].split("```")[0].strip()
                break
        
        # 根据代码生成模拟问题
        issues = []
        if code:
            lines = code.split("\n")
            num_lines = len(lines)
            
            # 根据代码长度生成1-3个模拟问题
            num_issues = min(3, max(1, num_lines // 10))
            
            issue_types = [
                ("Security Vulnerability", "SQL Injection", "SQL拼接注入风险", "使用参数化查询", "高"),
                ("Performance Issue", "Inefficient Loop", "循环效率低", "使用列表推导式", "中"),
                ("Code Smell", "Non-Standard Naming", "命名不规范", "使用 snake_case", "低"),
                ("Potential Bug", "Missing Error Handling", "缺少错误处理", "添加 try-except", "中"),
            ]
            
            for i in range(num_issues):
                category, subtype, risk_desc, fix_suggestion, severity = issue_types[i % len(issue_types)]
                divisor = num_issues + 1
                line_num = min(num_lines, max(1, (i + 1) * (num_lines // divisor)))
                
                issues.append({
                    "category": category,
                    "dimension": category,
                    "subtype": subtype,
                    "line_number": str(line_num),
                    "violated_rule": subtype,
                    "risk_level": severity,
                    "risk_description": f"模拟：发现{risk_desc}",
                    "fix_suggestion": fix_suggestion,
                    "severity": severity,
                })
        
        return {
            "review_summary": f"这是模拟审查结果（MOCK_MODE=True），发现 {len(issues)} 个问题",
            "issues": issues
        }

    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"

    payload = {"model": MODEL_NAME, "messages": messages, "temperature": temperature}

    last_error: str = ""
    for retry in range(RETRY_TIMES):
        try:
            resp = requests.post(API_URL, headers=headers, json=payload, timeout=TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            obj = _extract_json_object(content)
            if obj is None:
                return {"review_summary": "parse_failed", "issues": []}
            return obj
        except Exception as e:
            last_error = str(e)
            if retry == RETRY_TIMES - 1:
                break
            time.sleep(SLEEP_INTERVAL)

    return {"review_summary": f"request_failed: {last_error}", "issues": []}
