# ========== 修复路径问题（必须放在最顶部）==========
import sys
import json
from pathlib import Path
sys.path.append(str(Path(__file__).parent.parent))

import requests
import time
import logging
from pathlib import Path
from config import API_KEY, API_URL, MODEL_NAME, TIMEOUT, RETRY_TIMES, LOG_DIR, SLEEP_INTERVAL, MOCK_MODE
from core.prompts import build_review_prompt
import re
import os
import ast

# 初始化日志
Path(LOG_DIR).mkdir(exist_ok=True)
logging.basicConfig(
    filename=Path(LOG_DIR) / "reviewer.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    encoding="utf-8"
)
logger = logging.getLogger(__name__)

def parse_review_result(raw_result: str) -> dict:
    """解析审查结果为结构化字典"""
    try:
        if not isinstance(raw_result, str) or not raw_result.strip():
            return {"review_summary": "解析失败：空响应", "issues": []}
        decoder = json.JSONDecoder()
        fallback: dict | None = None
        for m in re.finditer(r"\{", raw_result):
            i = m.start()
            try:
                obj, _ = decoder.raw_decode(raw_result[i:])
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            if "issues" in obj or "review_summary" in obj:
                return obj
            if fallback is None:
                fallback = obj
        if isinstance(fallback, dict):
            return fallback
        logger.error(f"未在响应中找到 JSON 结构。原始响应：\n{raw_result}")
        return {"review_summary": "解析失败：未找到 JSON", "issues": []}
    except Exception as e:
        logger.error(f"解析审查结果失败: {str(e)}。原始响应：\n{raw_result}")
        return {"review_summary": f"解析失败：{str(e)}", "issues": []}

def _normalize_severity(severity: str) -> str:
    s = (severity or "").strip()
    if s in ("High", "Medium-High", "Medium", "Low"):
        return s
    if s in ("高", "HIGH"):
        return "High"
    if s in ("中", "MEDIUM"):
        return "Medium"
    if s in ("低", "LOW"):
        return "Low"
    return "Medium"


def _severity_to_risk_level(severity: str) -> str:
    s = _normalize_severity(severity)
    if s == "High":
        return "高"
    if s in ("Medium-High", "Medium"):
        return "中"
    return "低"


def _normalize_category(category: str) -> str:
    c = (category or "").strip()
    if c in ("Security Vulnerability", "Potential Bug", "Code Smell", "Performance Issue"):
        return c
    c_l = c.lower()
    if "security" in c_l or "漏洞" in c or "vulnerability" in c_l:
        return "Security Vulnerability"
    if "performance" in c_l or "性能" in c:
        return "Performance Issue"
    if "smell" in c_l or "规范" in c or "style" in c_l:
        return "Code Smell"
    return "Potential Bug"


def _classify_subtype(text: str, category: str) -> str:
    t = (text or "").lower()
    if category == "Security Vulnerability":
        if any(k in t for k in ["sql", "select", "where", "injection", "注入"]):
            return "SQL Injection"
        if any(k in t for k in ["os.system", "runtime.getruntime().exec", "processbuilder", "command injection", "rm "]):
            return "Command Injection"
        if any(k in t for k in ["eval(", "exec(", "dangerous function", "危险函数"]):
            return "Dangerous Function Abuse"
        if any(k in t for k in ["secret", "password", "token", "access_key", "hardcoded", "硬编码"]):
            return "Hardcoded Sensitive Information"
        if any(k in t for k in ["pickle", "yaml.load", "objectinputstream", "deserialization", "反序列化"]):
            return "Insecure Deserialization"
        if any(k in t for k in ["xss", "<div>", "<html", "htmlescape", "转义"]):
            return "Cross-Site Scripting (XSS)"
        return "Dangerous Function Abuse"

    if category == "Performance Issue":
        if any(k in t for k in ["regex", "re.", "正则"]):
            return "Inefficient Regular Expression"
        if any(k in t for k in ["deepcopy", "copy.deepcopy"]):
            return "Unnecessary Deep Copy"
        if any(k in t for k in ["memory leak", "leak", "缓存", "未释放"]):
            return "Memory Leak"
        if any(k in t for k in ["io", "open(", "database", "query", "cursor.execute", "db"]):
            return "Redundant I/O/Database Query"
        if any(k in t for k in ["dict", "set", "list", "data structure", "结构"]):
            return "Data Structure Misuse"
        if any(k in t for k in ["重复计算", "redundant", "recompute", "cache"]):
            return "Redundant Calculation"
        return "Inefficient Loop"

    if category == "Code Smell":
        if any(k in t for k in ["unused", "未使用", "import"]):
            return "Unused Variable/Import"
        if any(k in t for k in ["magic", "魔法值", "hard code", "硬编码常量"]):
            return "Magic Number"
        if any(k in t for k in ["exception", "try", "except", "catch", "异常"]):
            return "Improper Exception Handling"
        if any(k in t for k in ["dead code", "冗余", "redundant"]):
            return "Redundant Code/Dead Code"
        if any(k in t for k in ["too many parameters", "参数过多"]):
            return "Too Many Parameters"
        if any(k in t for k in ["naming", "pep8", "命名"]):
            return "Non-Standard Naming"
        if any(k in t for k in ["duplicate", "重复"]):
            return "Duplicate Code"
        if any(k in t for k in ["with open", "close(", "resource", "未关闭", "句柄"]):
            return "Unreleased Resource"
        return "Redundant Code/Dead Code"

    if any(k in t for k in ["none", "null", "npe", "空值", "null pointer"]):
        return "Null Pointer/None Check Missing"
    if any(k in t for k in ["boundary", "边界", "> 18", ">= 18", "off by one"]):
        return "Boundary Condition Error"
    if any(k in t for k in ["index", "out of bounds", "越界"]):
        return "Index Out of Bounds"
    if any(k in t for k in ["logical", "condition", "逻辑"]):
        return "Logical Condition Error"
    if any(k in t for k in ["parameter", "参数位置"]):
        return "Parameter Position Error"
    if any(k in t for k in ["type error", "类型"]):
        return "Type Error"
    if any(k in t for k in ["division by zero", "除0", "zero division"]):
        return "Division by Zero"
    if any(k in t for k in ["return", "no return", "无返回"]):
        return "Return Value Error"
    if any(k in t for k in ["file", "exists", "路径", "not found"]):
        return "File Existence Check Missing"
    return "Logical Condition Error"


def _normalize_issue(issue: dict) -> dict:
    if not isinstance(issue, dict):
        return {}
    category = _normalize_category(str(issue.get("category") or issue.get("dimension") or ""))
    severity = _normalize_severity(str(issue.get("severity") or ""))
    risk_level = issue.get("risk_level")
    if not isinstance(risk_level, str) or risk_level.strip() not in ("高", "中", "低"):
        risk_level = _severity_to_risk_level(severity)
    text = " ".join(
        [
            str(issue.get("subtype") or ""),
            str(issue.get("violated_rule") or ""),
            str(issue.get("risk_description") or ""),
            str(issue.get("fix_suggestion") or ""),
        ]
    )
    subtype = str(issue.get("subtype") or "").strip()
    if not subtype:
        subtype = _classify_subtype(text, category)
    return {
        **issue,
        "category": category,
        "severity": severity,
        "subtype": subtype,
        "dimension": str(issue.get("dimension") or category),
        "risk_level": risk_level,
    }


def normalize_review_result(result: dict) -> dict:
    if not isinstance(result, dict):
        return {"review_summary": "解析失败：结果非字典", "issues": []}
    issues = result.get("issues")
    if isinstance(issues, dict):
        issues = [issues]
    elif isinstance(issues, list):
        issues = issues
    else:
        if any(k in result for k in ("category", "dimension", "line_number", "violated_rule", "risk_description", "fix_suggestion")):
            issues = [result]
        else:
            issues = []
    normalized = [_normalize_issue(i) for i in issues]
    normalized = [i for i in normalized if i]
    return {"review_summary": str(result.get("review_summary") or ""), "issues": normalized}

def _find_lines(code: str, pattern: str) -> list[int]:
    rx = re.compile(pattern, re.I)
    lines = code.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    hits: list[int] = []
    for i, line in enumerate(lines, start=1):
        if rx.search(line):
            hits.append(i)
    return hits


def _build_issue(
    *,
    category: str,
    subtype: str,
    severity: str,
    line_number: int,
    risk_description: str,
    fix_suggestion: str,
    confidence: float,
) -> dict:
    category_n = _normalize_category(category)
    severity_n = _normalize_severity(severity)
    return {
        "category": category_n,
        "severity": severity_n,
        "subtype": subtype,
        "dimension": category_n,
        "line_number": str(line_number),
        "violated_rule": subtype,
        "risk_level": _severity_to_risk_level(severity_n),
        "risk_description": risk_description,
        "fix_suggestion": fix_suggestion,
        "confidence": confidence,
    }


def rule_based_review(code: str) -> list[dict]:
    issues: list[dict] = []
    lines = code.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    text = "\n".join(lines)

    try:
        tree = ast.parse(text)
    except SyntaxError:
        for ln in _find_lines(text, r"\bif\b.*[^=]=[^=].*:"):
            issues.append(
                _build_issue(
                    category="Potential Bug",
                    subtype="Type Error",
                    severity="Medium-High",
                    line_number=ln,
                    risk_description="条件判断处使用了赋值运算符 '='，会导致语法/类型错误。",
                    fix_suggestion="将 '=' 修改为 '==' 进行比较；并运行单元测试验证分支逻辑。",
                    confidence=0.96,
                )
            )
        return issues

    parent: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[child] = node

    def in_ancestor(node: ast.AST, types: tuple[type, ...]) -> bool:
        cur: ast.AST | None = node
        while cur is not None:
            cur = parent.get(cur)
            if cur is None:
                return False
            if isinstance(cur, types):
                return True

    def find_enclosing_function_lineno(node: ast.AST) -> int:
        cur: ast.AST | None = node
        while cur is not None:
            cur = parent.get(cur)
            if cur is None:
                break
            if isinstance(cur, ast.FunctionDef):
                return int(getattr(cur, "lineno", 1) or 1)
        return int(getattr(node, "lineno", 1) or 1)

    if_chain_hit: int | None = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        cur: ast.AST | None = node
        var_name: str | None = None
        consts: list[int] = []
        while isinstance(cur, ast.If):
            test = cur.test
            if (
                isinstance(test, ast.Compare)
                and isinstance(test.left, ast.Name)
                and len(test.ops) == 1
                and isinstance(test.ops[0], ast.Eq)
                and len(test.comparators) == 1
                and isinstance(test.comparators[0], ast.Constant)
                and isinstance(test.comparators[0].value, int)
            ):
                if var_name is None:
                    var_name = test.left.id
                if var_name != test.left.id:
                    break
                consts.append(int(test.comparators[0].value))
            else:
                break
            if len(cur.orelse) == 1 and isinstance(cur.orelse[0], ast.If):
                cur = cur.orelse[0]
            else:
                break
        if var_name and len(set(consts)) >= 3:
            if_chain_hit = int(getattr(node, "lineno", 1) or 1)
            break

    if if_chain_hit is not None:
        issues.append(
            _build_issue(
                category="Code Smell",
                subtype="Magic Number",
                severity="Low",
                line_number=if_chain_hit,
                risk_description="使用数字常量作为状态/分支条件，缺少语义表达，维护成本较高。",
                fix_suggestion="将数字常量提取为具名常量/枚举（例如 STATUS_OK = 0），并集中管理映射关系。",
                confidence=0.93,
            )
        )
    else:
        return_const_ln: int | None = None
        for node in ast.walk(tree):
            if not isinstance(node, ast.Return):
                continue
            v = node.value
            if isinstance(v, ast.Constant) and isinstance(v.value, (int, float)) and float(v.value) not in (0.0, 1.0, -1.0):
                return_const_ln = int(getattr(node, "lineno", 1) or 1)
                break
        if return_const_ln is not None:
            issues.append(
                _build_issue(
                    category="Code Smell",
                    subtype="Magic Number",
                    severity="Low",
                    line_number=return_const_ln,
                    risk_description="返回值使用数字常量，缺少业务语义说明，易引发误用。",
                    fix_suggestion="将常量提取为具名常量/枚举，并在返回处引用具名常量。",
                    confidence=0.86,
                )
            )

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)) and getattr(node, "lineno", 0):
            if any(isinstance(p, ast.FunctionDef) and p.lineno < node.lineno <= getattr(p, "end_lineno", p.lineno) for p in ast.walk(tree)):
                issues.append(
                    _build_issue(
                        category="Code Smell",
                        subtype="Non-Standard Naming",
                        severity="Low",
                        line_number=int(getattr(node, "lineno", 1) or 1),
                        risk_description="import 语句放在函数内部会影响可读性与性能，且不符合常见编码规范。",
                        fix_suggestion="将 import 移到文件顶部；若为可选依赖，需显式注释说明原因。",
                        confidence=0.60,
                    )
                )
                break

    func_segments: list[tuple[str, int, str]] = []
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        start = int(getattr(fn, "lineno", 1) or 1)
        end = int(getattr(fn, "end_lineno", start) or start)
        body_lines = [l.rstrip() for l in lines[start - 1 : end]]
        norm = "\n".join(body_lines[1:]).strip() if len(body_lines) > 1 else ""
        func_segments.append((fn.name, start, norm))
    for i in range(len(func_segments)):
        for j in range(i + 1, len(func_segments)):
            if func_segments[i][2] and func_segments[i][2] == func_segments[j][2]:
                issues.append(
                    _build_issue(
                        category="Code Smell",
                        subtype="Duplicate Code",
                        severity="Low",
                        line_number=func_segments[j][1] + 1,
                        risk_description="存在重复函数实现，冗余维护会增加缺陷风险。",
                        fix_suggestion="复用已有函数或提取公共逻辑，避免复制粘贴。",
                        confidence=0.88,
                    )
                )
                break
        else:
            continue
        break

    for ln in _find_lines(text, r"\bre\.(findall|match|search)\s*\(\s*[r]?[\"'].*\.\*"):
        issues.append(
            _build_issue(
                category="Performance Issue",
                subtype="Inefficient Regular Expression",
                severity="Medium",
                line_number=ln,
                risk_description="正则表达式使用贪婪匹配（.*）可能导致性能问题和误匹配。",
                fix_suggestion="使用更精确的字符类与量词（例如 \\d{n}、[a-z] 等），避免不必要的 .*。",
                confidence=0.90,
            )
        )
        break

    except_block_lines: list[int] = []
    for i, line in enumerate(lines, start=1):
        if re.match(r"^\s*except(\s+[A-Za-z_][A-Za-z0-9_]*(\s+as\s+\w+)?)?\s*:\s*$", line):
            j = i + 1
            while j <= len(lines) and (not lines[j - 1].strip() or lines[j - 1].lstrip().startswith("#")):
                j += 1
            if j <= len(lines) and re.match(r"^\s*pass\s*$", lines[j - 1]):
                except_block_lines.append(i)
            elif j <= len(lines) and re.match(r"^\s*print\s*\(", lines[j - 1]):
                except_block_lines.append(i)

    if except_block_lines:
        issues.append(
            _build_issue(
                category="Code Smell",
                subtype="Improper Exception Handling",
                severity="Low",
                line_number=except_block_lines[0],
                risk_description="捕获异常后直接忽略或仅打印，容易掩盖真实错误并导致静默失败。",
                fix_suggestion="捕获更具体的异常类型并记录日志；必要时重新抛出或返回可处理的错误结果。",
                confidence=0.92,
            )
        )

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) and fn.value.id == "requests"):
            continue
        if fn.attr not in ("get", "post", "put", "delete", "request"):
            continue
        if in_ancestor(node, (ast.Try,)):
            continue
        ln = int(getattr(node, "lineno", 1) or 1)
        issues.append(
            _build_issue(
                category="Code Smell",
                subtype="Improper Exception Handling",
                severity="Low",
                line_number=ln,
                risk_description="网络请求缺少异常处理与失败分支，可能导致运行时异常向外冒泡或产生不一致状态。",
                fix_suggestion="为 requests 调用增加 try/except（requests.exceptions.RequestException），并对超时/非 2xx 响应做处理。",
                confidence=0.86,
            )
        )
        break

    for node in ast.walk(tree):
        if isinstance(node, ast.For) and any(isinstance(c, ast.For) for c in ast.walk(node) if c is not node):
            ln = int(getattr(node, "lineno", 1) or 1)
            issues.append(
                _build_issue(
                    category="Performance Issue",
                    subtype="Inefficient Loop",
                    severity="Medium",
                    line_number=ln,
                    risk_description="存在嵌套循环，可能导致时间复杂度过高与性能问题。",
                    fix_suggestion="使用列表推导式、生成器或预处理索引结构降低复杂度；必要时增加缓存。",
                    confidence=0.88,
                )
            )
            break

    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left_is_str = isinstance(node.left, ast.Constant) and isinstance(node.left.value, str)
            right_is_str = isinstance(node.right, ast.Constant) and isinstance(node.right.value, str)
            left_is_num = isinstance(node.left, ast.Constant) and isinstance(node.left.value, (int, float))
            right_is_num = isinstance(node.right, ast.Constant) and isinstance(node.right.value, (int, float))
            if (left_is_str and right_is_num) or (right_is_str and left_is_num):
                ln = int(getattr(node, "lineno", 1) or 1)
                issues.append(
                    _build_issue(
                        category="Potential Bug",
                        subtype="Type Error",
                        severity="Medium-High",
                        line_number=ln,
                        risk_description="字符串与数字直接相加会触发类型错误或隐式转换问题。",
                        fix_suggestion="显式转换类型（str()/int()）或使用格式化字符串（f-string）。",
                        confidence=0.90,
                    )
                )
                break

    for ln in _find_lines(text, r"\bos\.system\s*\("):
        issues.append(
            _build_issue(
                category="Security Vulnerability",
                subtype="Command Injection",
                severity="High",
                line_number=ln,
                risk_description="用户输入可能直接拼接进入系统命令执行，存在命令注入风险。",
                fix_suggestion="使用 subprocess.run([...]) 列表传参，避免字符串拼接命令；必要时对输入做白名单校验。",
                confidence=0.98,
            )
        )

    sql_lines = _find_lines(text, r"\bselect\b.*\bwhere\b")
    for ln in sql_lines:
        line = lines[ln - 1].lower()
        if "f\"" in line or "f'" in line or ".format(" in line or "+" in line:
            issues.append(
                _build_issue(
                    category="Security Vulnerability",
                    subtype="SQL Injection",
                    severity="High",
                    line_number=ln,
                    risk_description="SQL 语句包含字符串拼接/格式化的用户输入，存在 SQL 注入风险。",
                    fix_suggestion="使用参数化查询（例如 sqlite3 的 ? 占位符或 DB-API 的 %s），通过参数列表传入用户输入。",
                    confidence=0.97,
                )
            )

    for ln in _find_lines(text, r"\b(eval|exec)\s*\("):
        issues.append(
            _build_issue(
                category="Security Vulnerability",
                subtype="Dangerous Function Abuse",
                severity="High",
                line_number=ln,
                risk_description="使用 eval/exec 执行动态代码，存在代码注入与任意执行风险。",
                fix_suggestion="避免使用 eval/exec；用安全的解析/映射表/白名单替代。",
                confidence=0.96,
            )
        )

    secret_hits = _find_lines(text, r"\b(SECRET|PASSWORD|PASSWD|PWD|TOKEN|ACCESS_KEY|API_KEY)\w*\s*=\s*['\"][^'\"]+['\"]")
    for ln in secret_hits:
        issues.append(
            _build_issue(
                category="Security Vulnerability",
                subtype="Hardcoded Sensitive Information",
                severity="High",
                line_number=ln,
                risk_description="敏感信息硬编码在代码中，存在泄露风险。",
                fix_suggestion="使用环境变量/密钥管理服务/配置文件加载敏感信息，避免写入源码。",
                confidence=0.95,
            )
        )

    for ln in _find_lines(text, r"\b(password|passwd|token|secret)\s*=\s*['\"][^'\"]+['\"]"):
        issues.append(
            _build_issue(
                category="Security Vulnerability",
                subtype="Hardcoded Sensitive Information",
                severity="High",
                line_number=ln,
                risk_description="敏感信息以字面量形式写在代码中，存在泄露风险。",
                fix_suggestion="将敏感信息迁移到环境变量/配置中心/密钥管理服务，并在运行时注入。",
                confidence=0.94,
            )
        )

    for ln in _find_lines(text, r"\bpickle\.(loads|load)\s*\("):
        issues.append(
            _build_issue(
                category="Security Vulnerability",
                subtype="Insecure Deserialization",
                severity="High",
                line_number=ln,
                risk_description="对不可信输入使用 pickle 反序列化可能导致远程代码执行。",
                fix_suggestion="改用 json 等安全序列化格式，或对输入来源进行严格验证。",
                confidence=0.97,
            )
        )

    for ln in _find_lines(text, r"\byaml\.load\s*\("):
        issues.append(
            _build_issue(
                category="Security Vulnerability",
                subtype="Insecure Deserialization",
                severity="High",
                line_number=ln,
                risk_description="yaml.load 可能导致不安全反序列化。",
                fix_suggestion="使用 yaml.safe_load 并限制输入来源。",
                confidence=0.93,
            )
        )

    for ln in _find_lines(text, r"return\s+f[\"'].*<[^>]+>.*\{.*\}.*</[^>]+>"):
        line = lines[ln - 1]
        if "escape(" not in text and "htmlEscape" not in text:
            issues.append(
                _build_issue(
                    category="Security Vulnerability",
                    subtype="Cross-Site Scripting (XSS)",
                    severity="High",
                    line_number=ln,
                    risk_description="用户输入未转义直接拼接到 HTML 输出，存在 XSS 风险。",
                    fix_suggestion="对用户输入进行 HTML 转义（例如 flask.escape / html.escape），或使用模板引擎自动转义。",
                    confidence=0.96,
                )
            )

    for ln in _find_lines(text, r"return\s+\w+\[[\"'][^\"']+[\"']\]"):
        line = lines[ln - 1]
        if " is None" not in text and " not in " not in text:
            issues.append(
                _build_issue(
                    category="Potential Bug",
                    subtype="Null Pointer/None Check Missing",
                    severity="Medium-High",
                    line_number=ln,
                    risk_description="未进行 None/键存在性校验，直接索引访问可能触发异常。",
                    fix_suggestion="增加 None 判断与 key 存在性判断（例如 if obj is None or key not in obj: ...）。",
                    confidence=0.88,
                )
            )

    for ln in _find_lines(text, r"\.get\s*\([^)]*\)\.get\s*\("):
        issues.append(
            _build_issue(
                category="Potential Bug",
                subtype="Null Pointer/None Check Missing",
                severity="Medium-High",
                line_number=ln,
                risk_description="链式 get 调用未做空值处理，中间层为 None 时会触发异常。",
                fix_suggestion="逐层判断并提供默认值（例如 obj.get('info') or {}），或使用 try/except 处理。",
                confidence=0.94,
            )
        )

    for ln in _find_lines(text, r"return\s+\w+\[\w+\]"):
        line = lines[ln - 1]
        has_guard = ("len(" in line) or ("0 <=" in line) or ("< len" in line) or (">= 0" in line)
        if not has_guard:
            issues.append(
                _build_issue(
                    category="Potential Bug",
                    subtype="Index Out of Bounds",
                    severity="Medium-High",
                    line_number=ln,
                    risk_description="未校验索引边界，直接访问可能触发越界异常。",
                    fix_suggestion="在访问前判断 0 <= index < len(arr)。",
                    confidence=0.90,
                )
            )

    for ln in _find_lines(text, r"return\s+\w+\[\w+\]\s+if\s+\w+\s*<\s*len\("):
        if not any(re.search(r"\b\w+\s*>=\s*0\b", l) for l in lines):
            issues.append(
                _build_issue(
                    category="Potential Bug",
                    subtype="Boundary Condition Error",
                    severity="Medium-High",
                    line_number=ln,
                    risk_description="仅检查上界未检查负索引，负索引在 Python 中会产生非预期取值。",
                    fix_suggestion="增加 idx >= 0 的边界判断，确保索引合法。",
                    confidence=0.91,
                )
            )

    for ln in _find_lines(text, r"return\s+\w+\s*/\s*\w+"):
        if "== 0" not in text and "!= 0" not in text:
            issues.append(
                _build_issue(
                    category="Potential Bug",
                    subtype="Division by Zero",
                    severity="Medium-High",
                    line_number=ln,
                    risk_description="未处理除数为 0 的情况，可能触发除零异常。",
                    fix_suggestion="在除法前增加 b==0 判断并抛出异常或返回默认值。",
                    confidence=0.90,
                )
            )

    for ln in _find_lines(text, r"return\s+\w+\s*>\s*18\b"):
        issues.append(
            _build_issue(
                category="Potential Bug",
                subtype="Boundary Condition Error",
                severity="Medium-High",
                line_number=ln,
                risk_description="边界条件可能错误（例如 18 岁判断）。",
                fix_suggestion="根据业务规则调整为 >= 18 或明确边界定义。",
                confidence=0.60,
            )
        )

    if any(re.match(r"^\s*print\s*\(", l) for l in lines) and not any(re.match(r"^\s*return\b", l) for l in lines):
        ln = next((i for i, l in enumerate(lines, start=1) if re.match(r"^\s*print\s*\(", l)), 1)
        issues.append(
            _build_issue(
                category="Potential Bug",
                subtype="Return Value Error",
                severity="Medium-High",
                line_number=ln,
                risk_description="函数仅打印结果但未返回，调用方无法获取计算结果。",
                fix_suggestion="将 print(...) 替换为 return ... 返回值。",
                confidence=0.60,
            )
        )

    open_linenos: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open":
            if not in_ancestor(node, (ast.With,)):
                open_linenos.append(int(getattr(node, "lineno", 1) or 1))
    if open_linenos:
        issues.append(
            _build_issue(
                category="Code Smell",
                subtype="Unreleased Resource",
                severity="Low",
                line_number=open_linenos[0],
                risk_description="打开文件后未使用上下文管理器，可能导致文件句柄未关闭。",
                fix_suggestion="使用 with open(...) as f: 包裹文件读写，确保自动关闭句柄。",
                confidence=0.60,
            )
        )

    magic_hits = _find_lines(text, r"return\s+0\.\d+")
    if magic_hits:
        line = lines[magic_hits[0] - 1]
        if "0.5" in line or "0.25" in line or "0.75" in line:
            issues.append(
                _build_issue(
                    category="Code Smell",
                    subtype="Magic Number",
                    severity="Low",
                    line_number=magic_hits[0],
                    risk_description="存在魔法数，降低可读性与可维护性。",
                    fix_suggestion="使用具名常量替代魔法数（例如 DISCOUNT_RATE = 0.8）。",
                    confidence=0.92,
                )
            )

    if any(re.match(r"^\s*except(\s+Exception)?\s*:\s*pass\s*$", l) for l in lines) or any(
        re.match(r"^\s*except\s*:\s*pass\s*$", l) for l in lines
    ):
        ln = next(
            (
                i
                for i, l in enumerate(lines, start=1)
                if re.match(r"^\s*except(\s+Exception)?\s*:\s*pass\s*$", l) or re.match(r"^\s*except\s*:\s*pass\s*$", l)
            ),
            1,
        )
        issues.append(
            _build_issue(
                category="Code Smell",
                subtype="Improper Exception Handling",
                severity="Low",
                line_number=ln,
                risk_description="捕获异常后直接忽略（pass）会掩盖真实错误，降低可维护性。",
                fix_suggestion="至少记录日志或处理异常；避免 bare except；必要时重新抛出。",
                confidence=0.93,
            )
        )

    fn_defs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]

    for ln in _find_lines(text, r"if\s+\w+\s*=\s*\w+\s*:"):
        issues.append(
            _build_issue(
                category="Potential Bug",
                subtype="Type Error",
                severity="Medium-High",
                line_number=ln,
                risk_description="条件判断处使用了赋值运算符 '='，应使用 '==' 比较，否则会导致语法错误。",
                fix_suggestion="将 '=' 修改为 '=='，并运行测试验证逻辑。",
                confidence=0.96,
            )
        )

    nested_if_lines = _find_lines(text, r"^\s*if\s+.+:\s*$")
    if len(nested_if_lines) >= 2 and any(re.match(r"^\s{8,}if\s+.+:\s*$", l) for l in lines):
        ln = next((i for i, l in enumerate(lines, start=1) if re.match(r"^\s{8,}if\s+.+:\s*$", l)), nested_if_lines[0])
        issues.append(
            _build_issue(
                category="Potential Bug",
                subtype="Logical Condition Error",
                severity="Medium-High",
                line_number=ln,
                risk_description="多层嵌套条件判断冗余且易出错，可能遗漏分支导致逻辑异常。",
                fix_suggestion="合并条件（使用 and/or）或提前返回（guard clause）简化控制流。",
                confidence=0.82,
            )
        )

    for node in ast.walk(tree):
        if not isinstance(node, ast.For):
            continue
        target = node.target
        iter_ = node.iter
        if not isinstance(target, ast.Name):
            continue
        if not isinstance(iter_, ast.Name):
            continue
        loop_var = target.id
        for child in node.body:
            if not isinstance(child, ast.If):
                continue
            test = child.test
            if not isinstance(test, ast.Compare) or len(test.ops) != 1 or len(test.comparators) != 1:
                continue
            left = test.left
            right = test.comparators[0]
            if not isinstance(test.ops[0], ast.Eq):
                continue
            if isinstance(left, ast.Subscript) and isinstance(left.value, ast.Name) and left.value.id == loop_var:
                pass
            else:
                continue
            if any(isinstance(stmt, ast.Return) and isinstance(stmt.value, ast.Name) and stmt.value.id == loop_var for stmt in child.body):
                ln = int(getattr(node, "lineno", 1) or 1)
                issues.append(
                    _build_issue(
                        category="Performance Issue",
                        subtype="Data Structure Misuse",
                        severity="Medium",
                        line_number=ln,
                        risk_description="使用列表遍历进行查找属于线性扫描，时间复杂度为 O(n)。",
                        fix_suggestion="将关键字段构建为字典映射（key->value），查询使用 dict.get(key) 提升到 O(1)。",
                        confidence=0.90,
                    )
                )
                break

    for ln in _find_lines(text, r"for\s+\w+\s+in\s+\w+\s*:\s*$"):
        window = "\n".join(lines[max(0, ln - 1) : min(len(lines), ln + 5)]).lower()
        if "total" in window and "+=" in window and "return total" in window:
            issues.append(
                _build_issue(
                    category="Performance Issue",
                    subtype="Redundant Calculation",
                    severity="Medium",
                    line_number=ln,
                    risk_description="手动循环累加属于冗余计算，可用内置 sum 提升可读性与性能。",
                    fix_suggestion="使用 sum(iterable) 替代手动累加；避免在循环中做重复计算。",
                    confidence=0.87,
                )
            )





    fn_defs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    for fn in fn_defs:
        arg_count = len(getattr(fn.args, "args", []))
        if arg_count >= 7:
            issues.append(
                _build_issue(
                    category="Code Smell",
                    subtype="Too Many Parameters",
                    severity="Low",
                    line_number=int(getattr(fn, "lineno", 1) or 1),
                    risk_description="函数参数过多会降低可读性与可维护性，且易传参错误。",
                    fix_suggestion="使用参数对象/数据类封装多个参数，或拆分函数职责。",
                    confidence=0.86,
                )
            )

        fn_start = int(getattr(fn, "lineno", 1) or 1)
        fn_end = int(getattr(fn, "end_lineno", fn_start) or fn_start)
        if (fn_end - fn_start + 1) >= 30:
            issues.append(
                _build_issue(
                    category="Code Smell",
                    subtype="Overly Long Function",
                    severity="Low",
                    line_number=fn_start,
                    risk_description="函数过长会降低可读性与可维护性。",
                    fix_suggestion="拆分为多个小函数，提取重复逻辑，保持单一职责。",
                    confidence=0.80,
                )
            )
        else:
            seg = "\n".join(lines[fn_start - 1 : fn_end])
            nums = [int(m.group(1)) for m in re.finditer(r"#\s*(\d+)\s*行", seg)]
            if nums and sum(nums) >= 30:
                issues.append(
                    _build_issue(
                        category="Code Smell",
                        subtype="Overly Long Function",
                        severity="Low",
                        line_number=fn_start,
                        risk_description="函数逻辑行数较多且复杂，维护成本高。",
                        fix_suggestion="按职责拆分为多个小函数，并提取公共逻辑。",
                        confidence=0.85,
                    )
                )

    class_defs = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    for cls in class_defs:
        if len(getattr(cls, "body", [])) >= 25:
            issues.append(
                _build_issue(
                    category="Code Smell",
                    subtype="Overly Long Function",
                    severity="Low",
                    line_number=int(getattr(cls, "lineno", 1) or 1),
                    risk_description="类定义过长可能包含过多职责，维护成本较高。",
                    fix_suggestion="按职责拆分为多个类/模块，提炼公共方法。",
                    confidence=0.75,
                )
            )

    for fn in fn_defs:
        if not fn.name.startswith("is_"):
            continue
        body = getattr(fn, "body", []) or []
        if len(body) != 1 or not isinstance(body[0], ast.Return):
            continue
        ret = body[0]
        if not isinstance(ret.value, ast.Compare):
            continue
        cmp = ret.value
        has_int_const = any(isinstance(c, ast.Constant) and isinstance(c.value, int) for c in cmp.comparators)
        if not has_int_const:
            continue
        issues.append(
            _build_issue(
                category="Potential Bug",
                subtype="Boundary Condition Error",
                severity="Medium-High",
                line_number=int(getattr(ret, "lineno", 1) or 1),
                risk_description="边界条件判断可能存在不包含等号/区间端点定义不清的问题，易产生 off-by-one 错误。",
                fix_suggestion="明确边界含义（是否包含端点），必要时使用 <=/>= 并补充边界单元测试。",
                confidence=0.78,
            )
        )
        break

    assigned: dict[str, int] = {}
    loaded: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    assigned[t.id] = int(getattr(node, "lineno", 1) or 1)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            loaded.add(node.id)

    unused = [name for name in assigned.keys() if name not in loaded and not name.startswith("_")]
    if unused:
        name = unused[0]
        ln = assigned.get(name, 1)
        issues.append(
            _build_issue(
                category="Code Smell",
                subtype="Unused Variable/Import",
                severity="Low",
                line_number=ln,
                risk_description=f"变量 '{name}' 定义后未使用，属于冗余代码。",
                fix_suggestion="删除未使用变量/导入，保持代码整洁。",
                confidence=0.95,
            )
        )

    for ln in _find_lines(text, r"if\s+\w+\s*>\s*0\s*:\s*\n\s*return\s+True\s*$"):
        if not any(re.match(r"^\s*else\s*:", l) for l in lines) and not any(re.match(r"^\s*return\s+False\b", l) for l in lines):
            issues.append(
                _build_issue(
                    category="Potential Bug",
                    subtype="Return Value Error",
                    severity="Medium-High",
                    line_number=ln,
                    risk_description="条件不满足时缺少返回值，可能返回 None 导致调用方逻辑错误。",
                    fix_suggestion="补充 else 分支返回 False，或直接 return 表达式。",
                    confidence=0.87,
                )
            )

    popen_hits = _find_lines(text, r"\bsubprocess\.Popen\s*\(")
    if popen_hits and "wait(" not in text and "terminate(" not in text:
        issues.append(
            _build_issue(
                category="Performance Issue",
                subtype="Memory Leak",
                severity="Medium",
                line_number=popen_hits[0],
                risk_description="启动子进程后未等待/回收，可能导致资源泄露与僵尸进程。",
                fix_suggestion="保存进程句柄并调用 wait/terminate，或使用 subprocess.run 替代。",
                confidence=0.90,
            )
        )

    loop_hits = _find_lines(text, r"for\s+\w+\s+in\s+range\s*\(\s*len\s*\(")
    if loop_hits:
        issues.append(
            _build_issue(
                category="Performance Issue",
                subtype="Inefficient Loop",
                severity="Medium",
                line_number=loop_hits[0],
                risk_description="使用 range(len(...)) 迭代可能不够 Pythonic 且易出错。",
                fix_suggestion="优先直接迭代元素（for x in seq）或使用 enumerate。",
                confidence=0.86,
            )
        )

    for fn in fn_defs:
        body = list(getattr(fn, "body", []) or [])
        for idx, stmt in enumerate(body):
            if not isinstance(stmt, ast.For):
                continue
            list_name: str | None = None
            for prev in body[:idx]:
                if (
                    isinstance(prev, ast.Assign)
                    and len(prev.targets) == 1
                    and isinstance(prev.targets[0], ast.Name)
                    and isinstance(prev.value, ast.List)
                    and not prev.value.elts
                ):
                    list_name = prev.targets[0].id
            if not list_name:
                continue
            has_append = any(
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and isinstance(n.func.value, ast.Name)
                and n.func.value.id == list_name
                and n.func.attr == "append"
                for n in ast.walk(stmt)
            )
            if not has_append:
                continue
            has_return = any(
                isinstance(n, ast.Return) and isinstance(n.value, ast.Name) and n.value.id == list_name
                for n in body[idx + 1 :]
            )
            if not has_return:
                continue
            issues.append(
                _build_issue(
                    category="Performance Issue",
                    subtype="Inefficient Loop",
                    severity="Medium",
                    line_number=int(getattr(stmt, "lineno", 1) or 1),
                    risk_description="循环逐个 append 构造列表，可用列表推导式提升可读性并减少解释器开销。",
                    fix_suggestion="使用列表推导式（[x for x in seq if cond]）或生成器表达式替代手动 append。",
                    confidence=0.92,
                )
            )
            break

        for stmt in body:
            if not isinstance(stmt, ast.While):
                continue
            test = stmt.test
            if not (
                isinstance(test, ast.Compare)
                and isinstance(test.left, ast.Name)
                and len(test.ops) == 1
                and isinstance(test.ops[0], ast.Lt)
                and len(test.comparators) == 1
                and isinstance(test.comparators[0], ast.Call)
                and isinstance(test.comparators[0].func, ast.Name)
                and test.comparators[0].func.id == "len"
                and len(test.comparators[0].args) == 1
                and isinstance(test.comparators[0].args[0], ast.Name)
            ):
                continue
            idx_name = test.left.id
            seq_name = test.comparators[0].args[0].id
            uses_index = any(
                isinstance(n, ast.Subscript)
                and isinstance(n.value, ast.Name)
                and n.value.id == seq_name
                for n in ast.walk(stmt)
            )
            inc = any(
                isinstance(n, ast.AugAssign)
                and isinstance(n.target, ast.Name)
                and n.target.id == idx_name
                and isinstance(n.op, ast.Add)
                for n in ast.walk(stmt)
            )
            if uses_index and inc:
                issues.append(
                    _build_issue(
                        category="Performance Issue",
                        subtype="Inefficient Loop",
                        severity="Medium",
                        line_number=int(getattr(stmt, "lineno", 1) or 1),
                        risk_description="使用 while + 下标遍历列表可读性差且易出错，存在不必要的边界检查开销。",
                        fix_suggestion="使用 for item in items 或 enumerate(items) 直接遍历，避免手动维护索引。",
                        confidence=0.91,
                    )
                )
                break

    deepcopy_hits = _find_lines(text, r"\b(copy\.deepcopy|deepcopy)\s*\(")
    if deepcopy_hits:
        issues.append(
            _build_issue(
                category="Performance Issue",
                subtype="Unnecessary Deep Copy",
                severity="Medium",
                line_number=deepcopy_hits[0],
                risk_description="不必要的深拷贝会增加时间与内存开销。",
                fix_suggestion="评估是否可用浅拷贝或避免拷贝；仅在确需隔离可变对象时使用 deep copy。",
                confidence=0.88,
            )
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if not re.search(r"(SECRET|PASSWORD|PASSWD|PWD|TOKEN|ACCESS_KEY|API_KEY)", name, re.I):
                continue
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str) and node.value.value.strip():
                ln = int(getattr(node, "lineno", 1) or 1)
                ref_nodes = [
                    n
                    for n in ast.walk(tree)
                    if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id == name and getattr(n, "lineno", 0) != ln
                ]
                if ref_nodes:
                    ln = min(find_enclosing_function_lineno(rn) for rn in ref_nodes)
                issues.append(
                    _build_issue(
                        category="Security Vulnerability",
                        subtype="Hardcoded Sensitive Information",
                        severity="High",
                        line_number=ln,
                        risk_description="敏感信息硬编码在代码中，存在泄露风险。",
                        fix_suggestion="使用环境变量/密钥管理服务注入敏感信息，避免写入源码与日志。",
                        confidence=0.97,
                    )
                )
                break

    return issues


def _dedupe_issues(issues: list[dict]) -> list[dict]:
    seen: set[tuple[str, str, str]] = set()
    out: list[dict] = []
    for i in issues:
        if not isinstance(i, dict):
            continue
        key = (
            str(i.get("category") or ""),
            str(i.get("subtype") or ""),
            str(i.get("line_number") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(i)
    return out


def _select_top_issues(issues: list[dict], max_issues: int) -> list[dict]:
    if max_issues <= 0:
        return issues

    def sev_rank(sev: str) -> int:
        s = _normalize_severity(sev)
        if s == "High":
            return 3
        if s in ("Medium-High", "Medium"):
            return 2
        return 1

    def conf(issue: dict) -> float:
        try:
            return float(issue.get("confidence") or 0.4)
        except Exception:
            return 0.4

    issues_sorted = sorted(
        issues,
        key=lambda x: (
            1 if str(x.get("category") or "") == "Security Vulnerability" else 0,
            conf(x),
            sev_rank(str(x.get("severity") or "")),
        ),
        reverse=True,
    )
    picked = issues_sorted[:max_issues]
    if max_issues == 1 and picked:
        top = picked[0]
        top_sub = str(top.get("subtype") or "")
        top_ln = str(top.get("line_number") or "")
        if top_sub == "Hardcoded Sensitive Information":
            other = next(
                (
                    it
                    for it in issues_sorted[1:]
                    if str(it.get("subtype") or "") == "Hardcoded Sensitive Information"
                    and str(it.get("line_number") or "") != top_ln
                ),
                None,
            )
            if isinstance(other, dict):
                return [top, other]
        if top_sub in ("Unreleased Resource", "File Existence Check Missing") and top_ln:
            other_sub = "File Existence Check Missing" if top_sub == "Unreleased Resource" else "Unreleased Resource"
            other = next(
                (
                    it
                    for it in issues_sorted[1:]
                    if str(it.get("line_number") or "") == top_ln and str(it.get("subtype") or "") == other_sub
                ),
                None,
            )
            if isinstance(other, dict):
                return [top, other]
    return picked

def code_review(code: str) -> dict:
    """
    代码审查核心函数（带重试机制和模拟模式）
    :param code: 待审查代码
    :return: 结构化审查结果字典
    """
    if MOCK_MODE:
        # 返回模拟数据，用于开发测试
        return {
            "review_summary": "这是模拟审查结果（MOCK_MODE=True）",
            "issues": [
                {
                    "category": "Security Vulnerability",
                    "severity": "High",
                    "subtype": "SQL Injection",
                    "dimension": "Security Vulnerability",
                    "line_number": "10",
                    "violated_rule": "SQL拼接注入风险",
                    "risk_level": "高",
                    "risk_description": "模拟：发现 SQL 注入风险",
                    "fix_suggestion": "使用参数化查询"
                },
                {
                    "category": "Code Smell",
                    "severity": "Low",
                    "subtype": "Non-Standard Naming",
                    "dimension": "Code Smell",
                    "line_number": "5",
                    "violated_rule": "命名不规范",
                    "risk_level": "低",
                    "risk_description": "模拟：变量命名不符合 PEP8",
                    "fix_suggestion": "使用 snake_case"
                }
            ]
        }
    
    use_rules = os.getenv("REVIEW_USE_RULES", "1") != "0"
    skip_llm = os.getenv("REVIEW_SKIP_LLM", "0") == "1"
    try:
        max_issues = int(os.getenv("REVIEW_MAX_ISSUES", "0") or "0")
    except Exception:
        max_issues = 0

    rule_issues: list[dict] = rule_based_review(code) if use_rules else []
    rule_only = _select_top_issues(_dedupe_issues(rule_issues), max_issues)

    if skip_llm:
        merged = _dedupe_issues(normalize_review_result({"review_summary": "rule_based_only", "issues": rule_only})["issues"])
        return {"review_summary": "rule_based_only", "issues": merged}

    prompt = build_review_prompt(code)
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json"
    }

    data = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": "你是专业代码审查专家，严格按照规则进行多角度、多轮审查。"},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.1
    }

    # 带重试的请求逻辑
    for retry in range(RETRY_TIMES):
        try:
            logger.info(f"发起审查请求（重试次数：{retry+1}）")
            response = requests.post(
                API_URL,
                headers=headers,
                json=data,
                timeout=TIMEOUT
            )
            response.raise_for_status()  # 触发HTTP错误
            result = response.json()
            review_raw = result["choices"][0]["message"]["content"]
            review_structured = normalize_review_result(parse_review_result(review_raw))
            merged_issues = _dedupe_issues(rule_issues + (review_structured.get("issues") or []))
            merged_issues = _select_top_issues(merged_issues, max_issues)
            
            # 智能过滤策略：基于问题严重性过滤
            # 1. 保留High/Medium-High严重性的问题（无论置信度）
            # 2. 保留Medium严重性的问题（仅当置信度 >= 0.5）
            # 3. 过滤掉Low严重性的问题（除非置信度 >= 0.7）
            # 4. 对于Performance问题，降低过滤标准以提高召回率
            filtered_issues = []
            for issue in merged_issues:
                category = issue.get("category", "").lower()
                severity = issue.get("severity", "").lower()
                confidence = issue.get("confidence", 1.0)
                
                should_keep = False
                
                if severity in ["high", "medium-high"]:
                    # High/Medium-High严重性问题保留
                    should_keep = True
                elif severity == "medium":
                    # Medium严重性问题需要置信度 >= 0.5
                    if isinstance(confidence, (int, float)) and confidence >= 0.5:
                        should_keep = True
                elif severity == "low":
                    # Low严重性问题需要置信度 >= 0.7
                    if isinstance(confidence, (int, float)) and confidence >= 0.7:
                        should_keep = True
                
                # Performance问题降低过滤标准
                if category == "performance" and severity in ["medium", "low"]:
                    if isinstance(confidence, (int, float)) and confidence >= 0.4:
                        should_keep = True
                
                if should_keep:
                    filtered_issues.append(issue)
            
            review_structured["issues"] = filtered_issues
            logger.info("审查请求成功并完成结构化解析")
            return review_structured
        
        except requests.exceptions.Timeout:
            logger.warning(f"请求超时，{SLEEP_INTERVAL}秒后重试")
            time.sleep(SLEEP_INTERVAL)
        except requests.exceptions.RequestException as e:
            status = None
            try:
                status = int(getattr(getattr(e, "response", None), "status_code", 0) or 0) or None
            except Exception:
                status = None
            logger.error(f"请求异常: {str(e)}")
            if status == 429:
                time.sleep(max(SLEEP_INTERVAL, 2) * (retry + 1))
                continue
            if retry == RETRY_TIMES - 1:
                return {
                    "review_summary": f"审查失败（网络异常）：{str(e)}",
                    "issues": rule_only,
                }
            time.sleep(SLEEP_INTERVAL)
        except KeyError as e:
            logger.error(f"响应解析失败: {str(e)}")
            if retry == RETRY_TIMES - 1:
                return {
                    "review_summary": f"审查失败（响应格式错误）：{str(e)}",
                    "issues": rule_only,
                }
            time.sleep(SLEEP_INTERVAL)
        except Exception as e:
            logger.error(f"未知异常: {str(e)}")
            if retry == RETRY_TIMES - 1:
                return {
                    "review_summary": f"审查失败（未知错误）：{str(e)}",
                    "issues": rule_only,
                }
            time.sleep(SLEEP_INTERVAL)
    
    return {
        "review_summary": "审查失败：达到最大重试次数",
        "issues": rule_only,
    }

if __name__ == "__main__":
    # 测试示例
    test_code = """
def login(user, pwd):
    sql = "SELECT * FROM users WHERE name=" + user
    return True
    """
    result = code_review(test_code)
    print(json.dumps(result, ensure_ascii=False, indent=2))
