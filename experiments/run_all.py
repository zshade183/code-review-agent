"""一键跑完全部实验并出图。

用法::

    python experiments/run_all.py                        # 本地规则引擎基线（零成本）
    python experiments/run_all.py --mode remote           # 走真实模型
    python experiments/run_all.py --only exp2a exp2b      # 只跑指定实验
    python experiments/run_all.py --max-samples 30        # 每个实验取多少样本

产物统一落在 ``--out-root``（默认 ``reports/paper``）下::

    reports/paper/exp1/                    report.json + 图
    reports/paper/exp2/exp2a_ablation/     ...
    reports/paper/exp2/exp2b_interaction/  ...
    reports/paper/exp3/                    report.json + 图

跨模型对比图（exp1_comparison_plot.py / exp3_comparison_plot.py）不在本脚本内：
它们要读取多个模型各自跑出来的 ``reports/exp1-*/report.json``，属于汇总步骤，
详见 experiments/README.md。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Dict, List

EXPERIMENTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = EXPERIMENTS_DIR.parent

ALL_EXPERIMENTS = ("exp1", "exp2a", "exp2b", "exp3")


def run(cmd: List[str]) -> None:
    """执行一个子步骤（工作目录固定为项目根目录）。"""
    printable = " ".join(str(part) for part in cmd)
    print(f"\n$ {printable}", flush=True)
    proc = subprocess.run(cmd, cwd=PROJECT_ROOT)
    if proc.returncode != 0:
        raise SystemExit(f"步骤失败（退出码 {proc.returncode}）：{printable}")


def script(name: str) -> str:
    """返回相对项目根目录的脚本路径（与调用时的 cwd 无关）。"""
    return str(Path("experiments") / name)


def main() -> int:
    parser = argparse.ArgumentParser(description="运行全部对比实验并生成图表")
    parser.add_argument("--dataset", default="dataset/eval.json", help="数据集路径")
    parser.add_argument("--mode", choices=["local", "remote"], default="local",
                        help="local=只用本地规则引擎（零成本）；remote=调用真实模型")
    parser.add_argument("--out-root", default="reports/paper", help="结果输出根目录")
    parser.add_argument("--max-samples", type=int, default=30, help="每个实验取多少样本")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--only", nargs="*", choices=ALL_EXPERIMENTS, default=list(ALL_EXPERIMENTS),
                        help="只跑指定实验，默认全部")
    parser.add_argument("--skip-plots", action="store_true", help="只跑实验、不出图")
    args = parser.parse_args()

    dataset = Path(args.dataset)
    if not dataset.exists():
        raise SystemExit(f"数据集不存在：{dataset}（相对项目根目录 {PROJECT_ROOT}）")

    py = sys.executable
    out_root = Path(args.out_root)
    selected = set(args.only)

    dirs: Dict[str, Path] = {
        "exp1": out_root / "exp1",
        "exp2a": out_root / "exp2" / "exp2a_ablation",
        "exp2b": out_root / "exp2" / "exp2b_interaction",
        "exp3": out_root / "exp3",
    }
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)

    if "exp1" in selected:
        run([
            py, script("exp1_recognition.py"),
            "--dataset", str(dataset),
            "--out-dir", str(dirs["exp1"]),
            "--engine", args.mode,
            "--require-line",
            "--strict-static",
        ])
        if not args.skip_plots:
            run([py, script("exp1_plot.py"),
                 "--report", str(dirs["exp1"] / "report.json"),
                 "--out-dir", str(dirs["exp1"])])

    for key, module in (("exp2a", "exp2a_component_ablation.py"), ("exp2b", "exp2b_interaction_mode.py")):
        if key not in selected:
            continue
        run([
            py, script(module),
            "--dataset", str(dataset),
            "--out-dir", str(dirs[key]),
            "--max-samples", str(args.max_samples),
            "--seed", str(args.seed),
            "--require-line",
            "--details-level", "lite",
        ])
        if not args.skip_plots:
            plot = "exp2a_plot.py" if key == "exp2a" else "exp2b_plot.py"
            run([py, script(plot),
                 "--report", str(dirs[key] / "report.json"),
                 "--out-dir", str(dirs[key] / "plots")])

    if "exp3" in selected:
        run([
            py, script("exp3_optimize.py"),
            "--dataset", str(dataset),
            "--out-dir", str(dirs["exp3"]),
            "--max-samples", str(args.max_samples),
            "--seed", str(args.seed),
        ])
        if not args.skip_plots:
            run([py, script("exp3_plot.py"),
                 "--report", str(dirs["exp3"] / "report.json"),
                 "--out-dir", str(dirs["exp3"])])

    print("\n===== 完成 =====")
    for key in ALL_EXPERIMENTS:
        if key in selected:
            print(f"  {key}: {dirs[key]}")
    if args.mode == "local":
        print("\n提示：本次为本地规则引擎基线（未调用模型）。接真实模型请加 --mode remote。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
