import json
import os
import re
import sys
import tempfile
import argparse
import concurrent.futures
import subprocess
from typing import List, Dict, Any, Optional
from pathlib import Path

# 添加项目根目录到Python路径
if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 定义类别映射
def normalize_gt_category(category: str) -> str:
    category_map = {
        "Security Vulnerability": "security",
        "Performance Issue": "performance",
        "Code Smell": "smell",
        "Potential Bug": "bug",
        # 添加小写格式的映射
        "security": "security",
        "performance": "performance",
        "smell": "smell",
        "bug": "bug",
    }
    return category_map.get(category, category.lower() if category else "bug")

def normalize_pred_category(issue: Dict[str, Any]) -> str:
    cat = issue.get("category")
    if isinstance(cat, str) and cat.strip() in (
        "Security Vulnerability",
        "Performance Issue",
        "Code Smell",
        "Potential Bug",
    ):
        return normalize_gt_category(cat.strip())
    
    # 处理Pylint的错误码
    violated_rule = issue.get("violated_rule", "").strip()
    if violated_rule:
        # Pylint错误码到类别的映射
        pylint_category_map = {
            # 安全相关
            "eval-used": "security",
            "exec-used": "security",
            "subprocess-run-check": "security",
            "subprocess-popen-preexec-fn": "security",
            "subprocess-shell-true": "security",
            "sql-injection": "security",
            "os-urandom": "security",
            "random": "security",
            "hard-coded-password": "security",
            "hard-coded-tmpdir": "security",
            "broad-except": "security",
            "bare-except": "security",
            "logging-not-lazy": "security",
            "logging-format-interpolation": "security",
            "w0108": "security",  # 相似的安全问题
            
            # 性能相关
            "consider-using-generator": "performance",
            "consider-using-dict-items": "performance",
            "consider-using-set-comprehension": "performance",
            "consider-using-f-string": "performance",
            "consider-using-join": "performance",
            "use-dict-literal": "performance",
            "use-set-literal": "performance",
            "use-list-comprehension": "performance",
            "chained-comparison": "performance",
            "unnecessary-lambda": "performance",
            "consider-using-enumerate": "performance",
            "consider-using-ternary": "performance",
            "consider-using-with": "performance",
            "consider-using-dict-comprehension": "performance",
            "consider-using-tuple-unpacking": "performance",
            "consider-using-in": "performance",
            "consider-using-maxsplit-arg": "performance",
            "consider-using-pop": "performance",
            "consider-using-sys-exit": "performance",
            "use-with-open": "performance",
            "use-getitem-for-deletion": "performance",
            
            # 代码异味
            "unused-import": "smell",
            "unused-variable": "smell",
            "unused-argument": "smell",
            "unused-wildcard-import": "smell",
            "wildcard-import": "smell",
            "invalid-name": "smell",
            "missing-docstring": "smell",
            "missing-final-newline": "smell",
            "trailing-whitespace": "smell",
            "bad-continuation": "smell",
            "bad-indentation": "smell",
            "bad-whitespace": "smell",
            "line-too-long": "smell",
            "too-few-public-methods": "smell",
            "too-many-public-methods": "smell",
            "too-many-arguments": "smell",
            "too-many-locals": "smell",
            "too-many-branches": "smell",
            "too-many-statements": "smell",
            "too-many-nested-blocks": "smell",
            "too-many-function-args": "smell",
            "too-many-return-statements": "smell",
            "too-many-instance-attributes": "smell",
            "too-many-ancestors": "smell",
            "too-many-boolean-expressions": "smell",
            "too-many-lines": "smell",
            "duplicate-code": "smell",
            "similarities": "smell",
            "c-extension-no-member": "smell",
            "missing-class-docstring": "smell",
            "missing-function-docstring": "smell",
            "missing-module-docstring": "smell",
            "multiple-statements": "smell",
            "no-else-return": "smell",
            "no-self-use": "smell",
            "protected-access": "smell",
            "redefined-outer-name": "smell",
            "star-args": "smell",
            "too-many-try-statements": "smell",
            "unidiomatic-typecheck": "smell",
            "unnecessary-pass": "smell",
            "unused-private-member": "smell",
            "useless-object-inheritance": "smell",
            "wrong-import-order": "smell",
            "wrong-import-position": "smell",
            
            # 潜在bug
            "no-member": "bug",
            "undefined-variable": "bug",
            "attribute-error": "bug",
            "name-error": "bug",
            "type-error": "bug",
            "value-error": "bug",
            "key-error": "bug",
            "index-error": "bug",
            "zero-division-error": "bug",
            "import-error": "bug",
            "module-not-found-error": "bug",
            "syntax-error": "bug",
            "indentation-error": "bug",
            "taberror": "bug",
            "unbound-local-error": "bug",
            "referenced-before-assignment": "bug",
            "assignment-from-none": "bug",
            "comparison-with-callable": "bug",
            "comparison-with-itself": "bug",
            "comparison-with-none": "bug",
            "constant-comparison": "bug",
            "div-by-zero": "bug",
            "dangerous-default-value": "bug",
            "deprecated-module": "bug",
            "deprecated-method": "bug",
            "deprecated-class": "bug",
            "deprecated-decorator": "bug",
            "deprecated-lambda": "bug",
            "deprecated-property": "bug",
            "deprecated-string-function": "bug",
            "deprecated-variable": "bug",
            "duplicate-argument-name": "bug",
            "format-needs-mapping": "bug",
            "format-string-without-interpolation": "bug",
            "invalid-all-format": "bug",
            "invalid-envvar-default": "bug",
            "invalid-escape-sequence": "bug",
            "invalid-str-returned": "bug",
            "missing-kwargs": "bug",
            "misplaced-comparison-constant": "bug",
            "not-callable": "bug",
            "no-value-for-parameter": "bug",
            "positional-arg-before-keyword-arg": "bug",
            "redundant-keyword-arg": "bug",
            "redefined-argument-from-local": "bug",
            "return-in-init": "bug",
            "return-outside-function": "bug",
            "shadowed-import": "bug",
            "superfluous-parens": "bug",
            "unexpected-keyword-arg": "bug",
            "unreachable": "bug",
            "unrecognized-option": "bug",
            "used-before-assignment": "bug",
            "abstract-method": "bug",
            "arguments-differ": "bug",
            "bad-super-call": "bug",
            "class-cannot-be-instantiated": "bug",
            "duplicate-except": "bug",
            "eval-used": "bug",  # 也可以认为是安全问题
            "exec-used": "bug",  # 也可以认为是安全问题
            "function-redefined": "bug",
            "invalid-length-returned": "bug",
            "invalid-metaclass": "bug",
            "invalid-meta-object": "bug",
            "no-init": "bug",
            "non-iterator-returned": "bug",
            "not-an-iterable": "bug",
            "not-context-manager": "bug",
            "overlapping-except": "bug",
            "parameter-unpacking": "bug",
            "self-cls-assignment": "bug",
            "signature-differs": "bug",
            "superfluous-parens": "bug",
            "unexpected-keyword-arg": "bug",
            "unpacking-non-sequence": "bug",
            "used-before-assignment": "bug",
            "wildcard-import": "bug",  # 也可以认为是代码异味
            "wrong-import-order": "bug",  # 也可以认为是代码异味
        }
        
        # 查找映射
        if violated_rule in pylint_category_map:
            return pylint_category_map[violated_rule]
    
    # 原始的文本匹配逻辑
    text = " ".join(
        [
            str(issue.get("dimension") or ""),
            str(issue.get("violated_rule") or ""),
            str(issue.get("risk_description") or ""),
            str(issue.get("fix_suggestion") or ""),
        ]
    ).lower()
    if any(k in text for k in ["security", "vulnerability", "注入", "xss", "反序列化", "敏感", "漏洞"]):
        return "security"
    if any(k in text for k in ["performance", "性能", "低效", "复杂度"]):
        return "performance"
    if any(k in text for k in ["pep8", "命名", "unused", "未使用", "规范", "code smell", "smell", "资源泄漏"]):
        return "smell"
    return "bug"

def run_pylint(code: str) -> List[Dict[str, Any]]:
    tmp_path: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as f:
            f.write(code)
            tmp_path = f.name

        # 使用subprocess运行pylint
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pylint",
                "--output-format=json",
                "--score=n",
                "--reports=n",
                tmp_path,
            ],
            capture_output=True,
            text=True,
            timeout=30
        )
        
        out = result.stdout.strip()
        if not out:
            return []
        
        parsed = json.loads(out)
        if not isinstance(parsed, list):
            return []
        
        preds: List[Dict[str, Any]] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            preds.append(
                {
                    "dimension": "pylint",
                    "line_number": str(item.get("line") or ""),
                    "violated_rule": str(item.get("symbol") or ""),
                    "risk_level": str(item.get("type") or ""),
                    "risk_description": str(item.get("message") or ""),
                    "fix_suggestion": "",
                }
            )
        return preds
    except subprocess.TimeoutExpired:
        print(f"Pylint运行超时")
        return []
    except Exception as e:
        print(f"Pylint运行错误: {e}")
        return []
    finally:
        if tmp_path:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                pass


def run_bandit(code: str) -> List[Dict[str, Any]]:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(code)
        tmp_path = f.name
    try:
        result = subprocess.run(
            [sys.executable, "-m", "bandit", "-q", "-f", "json", tmp_path],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode not in (0, 1):
            return []
        out = (result.stdout or "").strip()
        if not out:
            return []
        parsed = json.loads(out)
        results = parsed.get("results") if isinstance(parsed, dict) else None
        if not isinstance(results, list):
            return []
        preds: List[Dict[str, Any]] = []
        for it in results:
            if not isinstance(it, dict):
                continue
            preds.append(
                {
                    "dimension": "bandit",
                    "line_number": str(it.get("line_number") or ""),
                    "violated_rule": str(it.get("test_id") or ""),
                    "risk_level": str(it.get("issue_severity") or ""),
                    "risk_description": str(it.get("issue_text") or ""),
                    "fix_suggestion": "",
                }
            )
        return preds
    except subprocess.TimeoutExpired:
        print("Bandit运行超时")
        return []
    except Exception as e:
        print(f"Bandit运行错误: {e}")
        return []
    finally:
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except Exception:
            pass


def parse_ints(value: Any) -> List[int]:
    if value is None:
        return []
    if isinstance(value, int):
        return [value]
    s = str(value)
    nums = re.findall(r"\d+", s)
    out: List[int] = []
    for n in nums:
        try:
            out.append(int(n))
        except Exception:
            continue
    return out


def run_llm_base(code: str) -> Dict[str, Any]:
    try:
        from experiments.llm_client import chat
    except Exception as e:
        print(f"run_llm_base 导入失败：{e}")
        return {"structured_ok": False, "review_summary": "import_failed", "issues": []}
    
    try:
        # 简化的 Prompt：只要求结构化输出，不影响后续数据分析
        prompt = """请审查下面的代码，找出所有问题。

请严格按照以下 JSON 格式输出（不要输出任何额外文本）：
{
  "review_summary": "一句话总结",
  "issues": [
    {
      "category": "Security Vulnerability 或 Performance Issue 或 Code Smell 或 Potential Bug",
      "line_number": 行号（数字）,
      "risk_description": "问题描述",
      "fix_suggestion": "修复建议"
    }
  ]
}

注意：
- category 必须是以下四个之一：Security Vulnerability, Performance Issue, Code Smell, Potential Bug
- 如果没有问题，issues 数组为空

代码：
"""
        prompt += f"```python\n{code}\n```"
        
        messages = [
            {"role": "system", "content": "你是一个 Python 代码审查助手，帮助开发者识别代码中的问题。"},
            {"role": "user", "content": prompt},
        ]
        
        result = chat(messages, temperature=0.2)
        
        # 验证结构化输出
        structured_ok = isinstance(result, dict) and isinstance(result.get("issues"), list) and "review_summary" in result
        if not structured_ok:
            return {"structured_ok": False, "review_summary": str((result or {}).get("review_summary") or ""), "issues": []}
        
        # 转换输出格式以匹配评估脚本
        issues = []
        for issue in result["issues"]:
            # 确保 category 是四大类之一
            category = issue.get("category", "")
            if category not in ["Security Vulnerability", "Performance Issue", "Code Smell", "Potential Bug"]:
                # 尝试映射常见变体
                category_mapping = {
                    "Security": "Security Vulnerability",
                    "Performance": "Performance Issue",
                    "Bug": "Potential Bug",
                    "Smell": "Code Smell",
                    "Code Smell": "Code Smell",
                    "Potential Bug": "Potential Bug",
                    "Security Vulnerability": "Security Vulnerability",
                    "Performance Issue": "Performance Issue",
                }
                category = category_mapping.get(category, "Code Smell")  # 默认归类为 Code Smell
            
            issues.append({
                "dimension": "llm_base",
                "category": category,
                "line_number": str(issue.get("line_number", "")),
                "violated_rule": issue.get("subtype", "") or issue.get("violated_rule", ""),
                "risk_level": issue.get("severity", "") or issue.get("risk_level", ""),
                "risk_description": issue.get("risk_description", ""),
                "fix_suggestion": issue.get("fix_suggestion", ""),
            })
        
        return {"structured_ok": True, "review_summary": str(result.get("review_summary") or ""), "issues": issues}
    except Exception as e:
        print(f"run_llm_base 执行失败：{e}")
        import traceback
        traceback.print_exc()
        return {"structured_ok": False, "review_summary": "request_failed", "issues": []}


def run_system(code: str) -> Dict[str, Any]:
    try:
        from core.reviewer import code_review
    except Exception as e:
        print(f"run_system导入失败: {e}")
        return {"structured_ok": False, "review_summary": "import_failed", "issues": []}
    
    try:
        result = code_review(code)
        
        structured_ok = isinstance(result, dict) and isinstance(result.get("issues"), list) and "review_summary" in result
        if not structured_ok:
            return {"structured_ok": False, "review_summary": str((result or {}).get("review_summary") or ""), "issues": []}
        
        issues = []
        for issue in result["issues"]:
            issues.append({
                "dimension": "system",
                "category": issue.get("category", "") or issue.get("dimension", ""),
                "line_number": str(issue.get("line_number", "")),
                "violated_rule": issue.get("violated_rule", "") or issue.get("subtype", ""),
                "risk_level": issue.get("severity", "") or issue.get("risk_level", ""),
                "risk_description": issue.get("risk_description", ""),
                "fix_suggestion": issue.get("fix_suggestion", ""),
            })
        
        return {"structured_ok": True, "review_summary": str(result.get("review_summary") or ""), "issues": issues}
    except Exception as e:
        print(f"run_system执行失败: {e}")
        import traceback
        traceback.print_exc()
        return {"structured_ok": False, "review_summary": "request_failed", "issues": []}

def evaluate_sample(sample: Dict[str, Any], system: str, require_line: bool) -> Dict[str, Any]:
    try:
        code = sample.get("code", "")
        issues = sample.get("issues", [])
        structured_ok = True
        
        # 运行对应系统的检测
        if system == "pylint":
            predicted_issues = run_pylint(code) + run_bandit(code)
        elif system == "llm_base":
            review = run_llm_base(code)
            predicted_issues = (review or {}).get("issues") or []
            structured_ok = bool((review or {}).get("structured_ok"))
        elif system == "system":
            review = run_system(code)
            predicted_issues = (review or {}).get("issues") or []
            structured_ok = bool((review or {}).get("structured_ok"))
        else:
            predicted_issues = []
            structured_ok = False
        
        # 标准化预测结果的类别
        for pred in predicted_issues:
            pred["category"] = normalize_pred_category(pred)
        
        # 标准化真实标注的类别
        ground_truth = []
        for issue in issues:
            gt_issue = issue.copy()
            gt_issue["category"] = normalize_gt_category(gt_issue.get("category", ""))
            loc = gt_issue.get("location") or {}
            ln_start = int(loc.get("line_start") or gt_issue.get("line_number") or 1)
            ln_end = int(loc.get("line_end") or ln_start)
            gt_issue["line_start"] = ln_start
            gt_issue["line_end"] = ln_end
            ground_truth.append(gt_issue)
        
        # 计算TP, FP, FN
        tp = 0
        fp = 0
        fn = 0
        
        # 简单的匹配逻辑：基于类别和行号
        matched_preds = set()
        matched_gt = set()
        by_type_counts: Dict[str, Dict[str, int]] = {}
        
        for j, gt in enumerate(ground_truth):
            gt_cat = gt.get("category")
            by_type_counts.setdefault(gt_cat, {"tp": 0, "fp": 0, "fn": 0, "count": 0})
            by_type_counts[gt_cat]["count"] += 1
            for i, pred in enumerate(predicted_issues):
                if i in matched_preds:
                    continue
                if pred.get("category") != gt_cat:
                    continue
                if not require_line:
                    tp += 1
                    by_type_counts[gt_cat]["tp"] += 1
                    matched_preds.add(i)
                    matched_gt.add(j)
                    break
                pred_lns = parse_ints(pred.get("line_number"))
                if not pred_lns:
                    continue
                start = max(1, int(gt.get("line_start") or 1) - 1)
                end = int(gt.get("line_end") or start) + 1
                if any(start <= ln <= end for ln in pred_lns):
                    tp += 1
                    by_type_counts[gt_cat]["tp"] += 1
                    matched_preds.add(i)
                    matched_gt.add(j)
                    break
        
        # 计算FP和FN
        fp = len(predicted_issues) - len(matched_preds)
        fn = len(ground_truth) - len(matched_gt)

        for pred in predicted_issues:
            pred_cat = pred.get("category")
            by_type_counts.setdefault(pred_cat, {"tp": 0, "fp": 0, "fn": 0, "count": 0})

        for cat, row in by_type_counts.items():
            pred_count = sum(1 for p in predicted_issues if p.get("category") == cat)
            gt_count = row["count"]
            cat_tp = row["tp"]
            row["fp"] = max(0, pred_count - cat_tp)
            row["fn"] = max(0, gt_count - cat_tp)
        
        return {
            "id": sample.get("id", ""),
            "system": system,
            "predicted_issues": predicted_issues,
            "ground_truth": ground_truth,
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "structured_ok": structured_ok,
            "by_type_counts": by_type_counts,
        }
    except Exception as e:
        print(f"样本处理错误 {sample.get('id', 'unknown')}: {e}")
        import traceback
        traceback.print_exc()
        return {
            "id": sample.get("id", ""),
            "system": system,
            "predicted_issues": [],
            "ground_truth": [],
            "tp": 0,
            "fp": 0,
            "fn": 0,
            "structured_ok": False,
            "by_type_counts": {},
        }

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default="dataset/test.json", help="数据集路径（默认纯净 Python 测试集）")
    parser.add_argument("--out-dir", default="reports/exp1", help="输出目录")
    parser.add_argument("--require-line", action="store_true")
    parser.add_argument("--engine", choices=["local", "remote"], default="remote")
    parser.add_argument("--strict-static", action="store_true")
    parser.add_argument("--max-samples", type=int, default=0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--systems", default="pylint,llm_base,system", help="要运行的系统")
    args = parser.parse_args()
    
    if args.strict_static:
        missing: List[str] = []
        try:
            import pylint  # noqa: F401
        except Exception:
            missing.append("pylint")
        try:
            import bandit  # noqa: F401
        except Exception:
            missing.append("bandit")
        if missing:
            raise SystemExit(f"Missing static tools in current interpreter ({sys.executable}): {', '.join(missing)}")

    # 读取数据集
    with open(args.dataset, "r", encoding="utf-8") as f:
        data = json.load(f)

    if args.max_samples and args.max_samples > 0:
        data = data[: args.max_samples]
    
    # 创建输出目录
    os.makedirs(args.out_dir, exist_ok=True)
    
    # 解析要运行的系统
    systems = [s.strip() for s in args.systems.split(",")]
    
    # 处理样本
    all_results = []
    total_tasks = len(data) * len(systems)

    workers = int(args.workers or 0)
    if workers <= 0:
        workers = min(4, os.cpu_count() or 4)
    print(f"使用 {workers} 个线程进行并发处理")

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = []
        for sample in data:
            for system in systems:
                futures.append(executor.submit(evaluate_sample, sample, system, args.require_line))
        
        completed = 0
        for future in concurrent.futures.as_completed(futures):
            try:
                result = future.result()
                all_results.append(result)
                completed += 1
                if completed % 10 == 0:
                    print(f"处理进度: {completed}/{total_tasks}")
            except Exception as e:
                print(f"任务执行错误: {e}")
                completed += 1
    
    # 计算统计结果
    summary = {}
    for system in systems:
        system_results = [r for r in all_results if r["system"] == system]
        tp = sum(r["tp"] for r in system_results)
        fp = sum(r["fp"] for r in system_results)
        fn = sum(r["fn"] for r in system_results)
        
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
        
        # 按类型统计
        by_type = {}
        agg: Dict[str, Dict[str, int]] = {}
        for r in system_results:
            for cat, row in (r.get("by_type_counts") or {}).items():
                agg.setdefault(cat, {"tp": 0, "fp": 0, "fn": 0, "count": 0})
                agg[cat]["tp"] += int(row.get("tp") or 0)
                agg[cat]["fp"] += int(row.get("fp") or 0)
                agg[cat]["fn"] += int(row.get("fn") or 0)
                agg[cat]["count"] += int(row.get("count") or 0)

        for cat, row in agg.items():
            cat_tp = row["tp"]
            cat_fp = row["fp"]
            cat_fn = row["fn"]
            cat_precision = cat_tp / (cat_tp + cat_fp) if (cat_tp + cat_fp) > 0 else 0.0
            cat_recall = cat_tp / (cat_tp + cat_fn) if (cat_tp + cat_fn) > 0 else 0.0
            cat_f1 = 2 * cat_precision * cat_recall / (cat_precision + cat_recall) if (cat_precision + cat_recall) > 0 else 0.0
            
            by_type[cat] = {
                "precision": cat_precision,
                "recall": cat_recall,
                "f1": cat_f1,
                "count": row["count"],
            }
        
        structured_ok_count = sum(1 for r in system_results if r.get("structured_ok"))
        structured_output_rate = structured_ok_count / len(system_results) if system_results else 0.0
        summary[system] = {
            "samples": len(system_results),
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "structured_output_rate": structured_output_rate,
            "by_type": by_type,
        }
    
    # 保存结果
    report = {
        "require_line": bool(args.require_line),
        "summary": summary,
        "details": all_results,
    }
    
    with open(os.path.join(args.out_dir, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    
    # 打印结果
    print(json.dumps(summary, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
