#!/usr/bin/env bash
# In-loop objective evidence for task 1 (shown to the evaluator; not a reward by itself).
set -u
test -f intervals.py || { echo "missing intervals.py"; exit 2; }
test -f test_intervals.py || { echo "missing test_intervals.py"; exit 2; }
test -f NOTES.md || echo "warning: missing NOTES.md"
python3 -m unittest -q 2>&1 | tail -5
python3 - <<'PY'
from intervals import merge_intervals as m
cases = [([], []), ([[3, 4], [1, 2]], [[1, 2], [3, 4]]), ([[1, 2], [2, 3]], [[1, 3]]), ([[1, 10], [2, 3]], [[1, 10]]), ([[5, 5]], [[5, 5]])]
bad = [(i, m(i), o) for i, o in cases if m(i) != o]
try:
    m([[3, 1]]); bad.append(("start>end", "no error", "ValueError"))
except ValueError:
    pass
print("hidden checks:", "PASS" if not bad else f"FAIL {bad}")
raise SystemExit(1 if bad else 0)
PY
