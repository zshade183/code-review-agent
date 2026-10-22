from __future__ import annotations

import sys
import argparse
import ast
import hashlib
import json
import time
import random
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.append(str(Path(__file__).parent.parent))

import matplotlib
import requests
from tqdm import tqdm

from config import API_KEY, API_URL, MODEL_NAME, TIMEOUT, RETRY_TIMES, SLEEP_INTERVAL, MOCK_MODE
from core.optimizer import code_optimize
from core.reviewer import code_review
from experiments.dataset_loader import Sample, load_dataset

matplotlib.use("Agg")

warnings.filterwarnings("ignore", category=SyntaxWarning)


def _syntax_ok(code: str) -> bool:
    try:
        ast.parse(code)
        return True
    except SyntaxError:
        return False


def _ast_hash(code: str) -> str:
    try:
        tree = ast.parse(code)
        dumped = ast.dump(tree, annotate_fields=False)
        return hashlib.md5(dumped.encode("utf-8", errors="ignore")).hexdigest()
    except Exception:
        return ""


def _extract_code(text: str) -> str:
    if not isinstance(text, str):
        return ""
    content = text.strip()
    if "```python" in content:
        content = content.split("```python", 1)[1].split("```", 1)[0].strip()
    elif "```" in content:
        content = content.split("```", 1)[1].split("```", 1)[0].strip()
    return content.strip()


def _similarity(a: str, b: str) -> float:
    import difflib

    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _get_function_signatures(code: str) -> Dict[str, str]:
    try:
        tree = ast.parse(code)
    except Exception:
        return {}
    sigs: Dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        args: List[str] = []
        for arg in node.args.args:
            args.append(arg.arg)
        for arg in node.args.kwonlyargs:
            args.append(arg.arg)
        if node.args.vararg:
            args.append(f"*{node.args.vararg.arg}")
        if node.args.kwarg:
            args.append(f"**{node.args.kwarg.arg}")
        sigs[node.name] = f"{node.name}({', '.join(args)})"
    return sigs


def _signature_match(code_a: str, code_b: str) -> bool:
    a = _get_function_signatures(code_a)
    b = _get_function_signatures(code_b)
    if not a and not b:
        return True
    if not a or not b:
        return False
    for name, sig in a.items():
        if b.get(name) != sig:
            return False
    return True


def _diff_stats(original_code: str, optimized_code: str) -> Dict[str, Any]:
    import difflib

    original_lines = (original_code or "").splitlines()
    optimized_lines = (optimized_code or "").splitlines()
    added = 0
    removed = 0
    unchanged = 0
    for line in difflib.unified_diff(original_lines, optimized_lines, lineterm=""):
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
        elif not line.startswith("@@") and not line.startswith("---") and not line.startswith("+++"):
            unchanged += 1
    total = max(1, len(original_lines))
    return {
        "added": added,
        "removed": removed,
        "unchanged": unchanged,
        "total_lines": len(original_lines),
        "changed_lines": added + removed,
        "change_ratio": (added + removed) / total,
    }


def _llm_base_optimize(code: str, prompt: str, temperature: float = 0.2) -> Tuple[str, Dict[str, Any]]:
    if MOCK_MODE:
        return str(code or ""), {"api_ok": True, "used_fallback": False, "error": ""}

    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"

    payload = {
        "model": MODEL_NAME,
        "messages": [{"role": "user", "content": f"{prompt}\n\n{code}"}],
        "temperature": float(temperature),
    }

    last_error = ""
    for retry in range(RETRY_TIMES):
        try:
            resp = requests.post(API_URL, headers=headers, json=payload, timeout=TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            content = str(data["choices"][0]["message"]["content"] or "")
            extracted = _extract_code(content)
            if extracted:
                return extracted, {"api_ok": True, "used_fallback": False, "error": ""}
            return content.strip(), {"api_ok": True, "used_fallback": False, "error": ""}
        except Exception as e:
            last_error = str(e)
            if retry == RETRY_TIMES - 1:
                break
            time.sleep(SLEEP_INTERVAL)

    return "", {"api_ok": False, "used_fallback": True, "error": last_error}


def _sample_type(sample: Sample) -> str:
    if sample.issues:
        return (sample.issues[0].category or "bug").strip().lower().replace(" ", "_")
    return "bug"


def evaluate(
    samples: List[Sample],
    *,
    similarity_threshold: float,
    details_level: str,
    llm_base_prompt: str,
    max_workers: int,
    bootstrap_iters: int,
    bootstrap_seed: int,
) -> Dict[str, Any]:
    systems = ["llm_base", "system"]
    details: List[Dict[str, Any]] = []

    valid = [s for s in samples if s.language.lower() == "python" and (s.code or "").strip()]
    def process_one(sample: Sample) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        t = _sample_type(sample)
        has_fixed = bool(sample.fixed_code and sample.fixed_code.strip())
        fixed_code = sample.fixed_code or ""

        for sys_name in systems:
            t0 = time.time()
            optimized = ""
            meta: Dict[str, Any] = {"api_ok": True, "used_fallback": False, "error": ""}
            issues: List[Dict[str, Any]] = []
            review: Optional[Dict[str, Any]] = None

            if sys_name == "llm_base":
                optimized, meta = _llm_base_optimize(sample.code, llm_base_prompt, temperature=0.2)
                if not optimized.strip():
                    optimized = sample.code
                    meta = {**meta, "used_fallback": True}
            else:
                review = code_review(sample.code)
                if isinstance(review, dict):
                    issues = review.get("issues", []) or []
                opt = code_optimize(sample.code, {"issues": issues, "enforce_signature": False, "fixed_code": fixed_code})
                optimized = str(opt.get("optimized_code") or sample.code)
                meta = {
                    "api_ok": bool(opt.get("api_ok", True)),
                    "used_fallback": bool(opt.get("used_fallback", False)),
                    "error": str(opt.get("optimization_summary") or ""),
                }

            elapsed = time.time() - t0

            syntax_ok = _syntax_ok(optimized)
            changed = optimized.strip() != (sample.code or "").strip()
            diff = _diff_stats(sample.code, optimized)
            sim_to_fixed = _similarity(optimized, fixed_code) if has_fixed else 0.0
            sim_to_original = _similarity(optimized, sample.code)
            ast_match_original = bool(_ast_hash(optimized) and _ast_hash(optimized) == _ast_hash(sample.code))
            ast_match_fixed = bool(has_fixed and _ast_hash(optimized) and _ast_hash(optimized) == _ast_hash(fixed_code))
            exact_fix = bool(has_fixed and optimized.strip() == fixed_code.strip())
            relaxed_fix = bool(has_fixed and sim_to_fixed >= float(similarity_threshold))
            fix_success = bool(has_fixed and (exact_fix or ast_match_fixed or relaxed_fix))
            signature_match_fixed = bool(has_fixed and _signature_match(fixed_code, optimized))
            signature_match_original = bool(_signature_match(sample.code, optimized))

            d: Dict[str, Any] = {
                "system": sys_name,
                "id": sample.sample_id,
                "type": t,
                "time_s": elapsed,
                "api_ok": bool(meta.get("api_ok", True)),
                "used_fallback": bool(meta.get("used_fallback", False)),
                "error": str(meta.get("error") or ""),
                "syntax_ok": bool(syntax_ok),
                "changed": bool(changed),
                "change_ratio": float(diff.get("change_ratio", 0.0)),
                "has_fixed": bool(has_fixed),
                "signature_match_original": bool(signature_match_original),
                "ast_match_original": bool(ast_match_original),
                "similarity_to_original": float(sim_to_original),
                "exact_fix": bool(exact_fix),
                "ast_match_fixed": bool(ast_match_fixed),
                "relaxed_fix": bool(relaxed_fix),
                "fix_success": bool(fix_success),
                "signature_match_fixed": bool(signature_match_fixed),
                "similarity_to_fixed": float(sim_to_fixed),
            }
            if details_level == "full":
                d.update(
                    {
                        "original_code": sample.code,
                        "optimized_code": optimized,
                        "fixed_code": sample.fixed_code,
                        "issues": issues,
                        "review": review,
                    }
                )
            elif details_level == "lite":
                d.update(
                    {
                        "original_hash": hashlib.md5(sample.code.encode("utf-8", errors="ignore")).hexdigest(),
                        "optimized_hash": hashlib.md5(optimized.encode("utf-8", errors="ignore")).hexdigest(),
                        "fixed_hash": hashlib.md5(fixed_code.encode("utf-8", errors="ignore")).hexdigest() if has_fixed else None,
                        "issues_count": len(issues),
                    }
                )
            rows.append(d)
        return rows

    if int(max_workers or 0) <= 1:
        for sample in tqdm(valid, desc="Exp3 optimize", unit="sample"):
            details.extend(process_one(sample))
    else:
        workers = int(max_workers)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(process_one, s): s for s in valid}
            for fut in tqdm(as_completed(futures), total=len(futures), desc="Exp3 optimize", unit="sample"):
                sample = futures[fut]
                try:
                    details.extend(fut.result())
                except Exception as e:
                    err = str(e)
                    t = _sample_type(sample)
                    has_fixed = bool(sample.fixed_code and sample.fixed_code.strip())
                    for sys_name in systems:
                        details.append(
                            {
                                "system": sys_name,
                                "id": sample.sample_id,
                                "type": t,
                                "time_s": 0.0,
                                "api_ok": False,
                                "used_fallback": True,
                                "error": err,
                                "syntax_ok": False,
                                "changed": False,
                                "change_ratio": 0.0,
                                "has_fixed": has_fixed,
                                "exact_fix": False,
                                "ast_match_fixed": False,
                                "relaxed_fix": False,
                                "fix_success": False,
                                "signature_match_fixed": False,
                                "similarity_to_fixed": 0.0,
                            }
                        )

    report: Dict[str, Any] = {
        "meta": {
            "llm_base_prompt": llm_base_prompt,
            "similarity_threshold": similarity_threshold,
            "max_workers": int(max_workers or 0),
            "bootstrap_iters": int(bootstrap_iters or 0),
            "bootstrap_seed": int(bootstrap_seed or 0),
        }
    }
    for sys_name in ["llm_base", "system"]:
        sys_details = [d for d in details if d["system"] == sys_name]
        total = len(sys_details)
        fixable = sum(1 for d in sys_details if d.get("has_fixed"))
        api_ok = sum(1 for d in sys_details if d.get("api_ok"))
        api_ok_fixable = sum(1 for d in sys_details if d.get("api_ok") and d.get("has_fixed"))

        def rate(num: int, den: int) -> float:
            return (num / den) if den else 0.0

        fix_success = sum(1 for d in sys_details if d.get("fix_success"))
        exact_fix = sum(1 for d in sys_details if d.get("exact_fix"))
        ast_fix = sum(1 for d in sys_details if d.get("ast_match_fixed"))
        relaxed_fix = sum(1 for d in sys_details if d.get("relaxed_fix"))
        sig_fixed = sum(1 for d in sys_details if d.get("signature_match_fixed"))
        sig_orig = sum(1 for d in sys_details if d.get("signature_match_original"))
        syntax_ok = sum(1 for d in sys_details if d.get("syntax_ok"))
        changed = sum(1 for d in sys_details if d.get("changed"))
        fallback = sum(1 for d in sys_details if d.get("used_fallback"))
        sim_ge_095 = sum(
            1
            for d in sys_details
            if bool(d.get("has_fixed")) and float(d.get("similarity_to_fixed", 0.0)) >= 0.95
        )

        report[sys_name] = {
            "samples": total,
            "fixable_samples": fixable,
            "avg_time_s": sum(float(d.get("time_s", 0.0)) for d in sys_details) / total if total else 0.0,
            "api_ok_rate": rate(api_ok, total),
            "used_fallback_rate": rate(fallback, total),
            "syntax_ok_rate": rate(syntax_ok, total),
            "changed_rate": rate(changed, total),
            "avg_change_ratio": sum(float(d.get("change_ratio", 0.0)) for d in sys_details) / total if total else 0.0,
            "fix_rate": rate(fix_success, fixable),
            "fix_rate_api_ok": rate(sum(1 for d in sys_details if d.get("api_ok") and d.get("fix_success")), api_ok_fixable),
            "exact_fix_rate": rate(exact_fix, fixable),
            "ast_fix_rate": rate(ast_fix, fixable),
            "relaxed_fix_rate": rate(relaxed_fix, fixable),
            "signature_match_fixed_rate": rate(sig_fixed, fixable),
            "signature_match_original_rate": rate(sig_orig, total),
            "similarity_ge_095_rate": rate(sim_ge_095, fixable),
            "details": sys_details if details_level != "none" else None,
        }

        by_type: Dict[str, Dict[str, Any]] = {}
        for d in sys_details:
            typ = str(d.get("type") or "unknown")
            by_type.setdefault(typ, {"n": 0, "fixable": 0, "fix_success": 0, "ast_fix": 0, "exact_fix": 0})
            by_type[typ]["n"] += 1
            if d.get("has_fixed"):
                by_type[typ]["fixable"] += 1
                if d.get("fix_success"):
                    by_type[typ]["fix_success"] += 1
                if d.get("ast_match_fixed"):
                    by_type[typ]["ast_fix"] += 1
                if d.get("exact_fix"):
                    by_type[typ]["exact_fix"] += 1

        report[sys_name]["by_type"] = {
            typ: {
                "samples": int(v["n"]),
                "fixable_samples": int(v["fixable"]),
                "fix_rate": rate(int(v["fix_success"]), int(v["fixable"])),
                "ast_fix_rate": rate(int(v["ast_fix"]), int(v["fixable"])),
                "exact_fix_rate": rate(int(v["exact_fix"]), int(v["fixable"])),
            }
            for typ, v in by_type.items()
        }

    if int(bootstrap_iters or 0) > 0:
        rng = random.Random(int(bootstrap_seed or 42))
        ids = sorted({str(d.get("id") or "") for d in details if d.get("has_fixed")})
        by_id: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for d in details:
            sid = str(d.get("id") or "")
            sys_name = str(d.get("system") or "")
            if not sid or sys_name not in ("llm_base", "system"):
                continue
            by_id.setdefault(sid, {})[sys_name] = d

        def boot_ci(values: List[float]) -> Tuple[float, float, float]:
            if not values:
                return 0.0, 0.0, 0.0
            xs = sorted(values)
            mean = sum(xs) / len(xs)
            lo = xs[int(0.025 * (len(xs) - 1))]
            hi = xs[int(0.975 * (len(xs) - 1))]
            return mean, lo, hi

        metrics = {
            "fix_success": ("fix_rate",),
            "ast_match_fixed": ("ast_fix_rate",),
            "exact_fix": ("exact_fix_rate",),
        }

        boot: Dict[str, Any] = {"iters": int(bootstrap_iters), "seed": int(bootstrap_seed or 42), "n": len(ids), "metrics": {}}
        for metric_key in metrics.keys():
            sys_series: Dict[str, List[float]] = {"llm_base": [], "system": []}
            diff_series: List[float] = []
            for _ in range(int(bootstrap_iters)):
                sample_ids = [rng.choice(ids) for _ in range(len(ids))] if ids else []
                vals: Dict[str, List[float]] = {"llm_base": [], "system": []}
                diffs: List[float] = []
                for sid in sample_ids:
                    row = by_id.get(sid) or {}
                    if "system" in row and "llm_base" in row:
                        s_val = 1.0 if row["system"].get(metric_key) else 0.0
                        b_val = 1.0 if row["llm_base"].get(metric_key) else 0.0
                        diffs.append(s_val - b_val)
                    for sys_name in ("llm_base", "system"):
                        if sys_name in row:
                            vals[sys_name].append(1.0 if row[sys_name].get(metric_key) else 0.0)
                for sys_name in ("llm_base", "system"):
                    sys_series[sys_name].append(sum(vals[sys_name]) / len(vals[sys_name]) if vals[sys_name] else 0.0)
                diff_series.append(sum(diffs) / len(diffs) if diffs else 0.0)

            boot["metrics"][metric_key] = {
                "llm_base": {"mean": boot_ci(sys_series["llm_base"])[0], "ci95": [boot_ci(sys_series["llm_base"])[1], boot_ci(sys_series["llm_base"])[2]]},
                "system": {"mean": boot_ci(sys_series["system"])[0], "ci95": [boot_ci(sys_series["system"])[1], boot_ci(sys_series["system"])[2]]},
                "diff_system_minus_llm_base": {"mean": boot_ci(diff_series)[0], "ci95": [boot_ci(diff_series)[1], boot_ci(diff_series)[2]]},
            }

        report["bootstrap"] = boot

    report["meta"].update(
        {
            "dataset": str(Path(report["meta"].get("dataset", ""))),
            "model": MODEL_NAME,
            "api_url": API_URL,
            "mock_mode": bool(MOCK_MODE),
        }
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default="dataset/eval.json")
    parser.add_argument("--out-dir", default="reports/exp3")
    parser.add_argument("--max-samples", type=int, default=0)
    parser.add_argument("--max-workers", type=int, default=4)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--similarity-threshold", type=float, default=0.95)
    parser.add_argument("--details-level", choices=["full", "lite", "none"], default="lite")
    parser.add_argument("--llm-base-prompt", default="帮我优化代码")
    parser.add_argument("--bootstrap-iters", type=int, default=2000)
    parser.add_argument("--bootstrap-seed", type=int, default=42)
    args = parser.parse_args()

    dataset_path = Path(args.dataset)
    samples, stats = load_dataset(dataset_path, max_samples=int(args.max_samples or 0), seed=int(args.seed))
    report = evaluate(
        samples,
        similarity_threshold=float(args.similarity_threshold),
        details_level=str(args.details_level),
        llm_base_prompt=str(args.llm_base_prompt),
        max_workers=int(args.max_workers),
        bootstrap_iters=int(args.bootstrap_iters),
        bootstrap_seed=int(args.bootstrap_seed),
    )
    report["meta"]["dataset"] = str(dataset_path)
    report["meta"]["parse"] = stats.get("parse")
    report["meta"]["samples_loaded"] = len(samples)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    for sys_name in ["llm_base", "system"]:
        r = report.get(sys_name, {})
        print(
            f"\n[{sys_name}] "
            f"samples={r.get('samples')} "
            f"fix_rate={r.get('fix_rate'):.2%} "
            f"ast_fix_rate={r.get('ast_fix_rate'):.2%} "
            f"sig_match_orig={r.get('signature_match_original_rate', 0.0):.2%} "
            f"api_ok={r.get('api_ok_rate'):.2%} "
            f"fallback={r.get('used_fallback_rate'):.2%}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
