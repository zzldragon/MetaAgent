"""Eval harness for ppbb — Pillar 1: no eval = no agent.

Usage:
    python run_evals.py [path/to/evalset.jsonl] [--floor 0.6]

This is a thin CLI over agent.run_evals(): it runs every eval set defined by
the canvas Eval node(s) (config.json -> "evals") and/or evals/evalset.jsonl,
grading each case with a typed grader: the legacy expected_output (substring) /
expected_regex / judge, OR a {"type", "value", ...} grader (equals, contains,
not_contains, contains_all/any, starts_with, ends_with, regex/not_regex, is_json,
json_has_keys, numeric, similar, length, judge), OR a list of "checks" combined
by "match": "all" (default) | "any". A linked Eval node tests one agent in
isolation; a stand-alone Eval node tests the whole pipeline. Copy
evals/evalset.example.jsonl to evals/evalset.jsonl and grow it
(+5 cases per week, drawn from real failures).
"""

import json
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import agent  # noqa: E402


def main():
    args = list(sys.argv[1:])
    floor = 0.0
    if "--floor" in args:
        i = args.index("--floor")
        floor = float(args[i + 1])
        del args[i:i + 2]
    # An explicit jsonl path overrides the configured/edited sets: run just
    # that file as one set against the whole pipeline.
    if args and os.path.isfile(args[0]):
        with open(args[0], encoding="utf-8") as f:
            cases = [json.loads(line) for line in f if line.strip()]
        agent.EVAL_SETS[:] = [{"name": os.path.basename(args[0]),
                               "target": None, "cases": cases}]

    results = agent.run_evals(emit=print)
    if not results:
        sys.exit(0)
    passed = sum(r["passed"] for r in results)
    total = sum(r["total"] for r in results)
    score = passed / total if total else 0.0
    print(f"\noverall: {passed}/{total} = {score:.2f} (floor {floor})")
    sys.exit(0 if score >= floor else 1)


if __name__ == "__main__":
    main()
