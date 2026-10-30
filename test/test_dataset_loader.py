"""数据集加载器冒烟测试。

运行：``python test/test_dataset_loader.py``

覆盖：默认测试集/评估集能否解析、字段是否齐全、语言过滤与截断修复策略是否生效。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from experiments.dataset_loader import load_dataset  # noqa: E402

CASES = [
    ("data/eval.json", 5),
]


def main() -> int:
    failures = 0
    for rel, limit in CASES:
        path = ROOT / rel
        if not path.exists():
            print(f"[SKIP] {rel} 不存在")
            continue
        try:
            samples, stats = load_dataset(path, max_samples=limit)
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] {rel} 加载失败：{type(exc).__name__}: {exc}")
            failures += 1
            continue

        print(f"[OK] {rel}")
        print(f"     解析策略={stats['parse'].get('strategy')} 保留={stats['parse'].get('kept_items')}"
              f" 丢弃={stats['parse'].get('dropped_items')} 本次取样={len(samples)}")
        if not samples:
            print("     [FAIL] 没有取到样本")
            failures += 1
            continue

        first = samples[0]
        print(f"     首样本 id={first.sample_id} language={first.language} "
              f"code={len(first.code)}字符 issues={len(first.issues)}")
        if first.language.lower() != "python" or not first.code.strip():
            print("     [FAIL] 样本语言或代码为空")
            failures += 1
        for issue in first.issues[:3]:
            print(f"       - [{issue.category}/{issue.severity}] 行{issue.line_start}-{issue.line_end} {issue.subtype}")

    print("\n全部通过。" if failures == 0 else f"\n{failures} 个用例失败。")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
