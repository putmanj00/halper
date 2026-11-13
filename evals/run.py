"""Offline eval for the impact-tier guardrail classifier.

Run from the repo root:  uv run python -m evals.run [path/to/cases.jsonl]
Standard library only. Exits 0 regardless of accuracy.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from halper.guardrail import Tier, classify_tool_call

CASES = Path(__file__).with_name("cases.jsonl")
TIERS = list(Tier)  # READ, LOW_WRITE, HIGH_WRITE, DESTRUCTIVE (ordinal = risk)


def load_cases(path: Path = CASES) -> list[dict]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def summarise(call: dict) -> str:
    text = json.dumps(call["tool_input"], ensure_ascii=False)
    return f"{call['tool_name']} {text}"[:90]


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    cases = load_cases(Path(args[0]) if args else CASES)
    rows = []
    for case in cases:
        expected = Tier[case["expected"].upper().replace("-", "_")]
        actual = classify_tool_call(case["tool_name"], case["tool_input"])
        rows.append((case, expected, actual))

    correct = sum(1 for _, e, a in rows if e == a)
    total = len(rows)
    print(f"Cases: {total}")
    print(f"Accuracy: {correct}/{total} = {correct / total:.1%}")
    print()

    names = [t.name for t in TIERS]
    print("Confusion matrix (rows = expected, cols = actual)")
    print(" " * 13 + "".join(f"{n:>12}" for n in names))
    for exp in TIERS:
        counts = [sum(1 for _, e, a in rows if e == exp and a == act) for act in TIERS]
        print(f"{exp.name:>12} " + "".join(f"{c:>12}" for c in counts))
    print()

    misses = [(c, e, a) for c, e, a in rows if e != a]
    unsafe = [m for m in misses if m[2] < m[1]]  # rated lower risk than label
    over = [m for m in misses if m[2] > m[1]]  # rated higher risk than label
    print(f"Misses: {len(misses)} (unsafe: {len(unsafe)}, over-cautious: {len(over)})")
    for case, exp, act in misses:
        kind = "UNSAFE" if act < exp else "over-cautious"
        print(f"  [{kind}] {case['id']}: expected {exp.name}, actual {act.name}")
        print(f"      input: {summarise(case)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
