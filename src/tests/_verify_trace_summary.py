"""Verify the usage/cost-attribution observability layer (roadmap item 7):
RuntimeOverlay.summary() / summarize_trace() roll a trace's records up into a
per-agent token / tool-call / step breakdown + run totals (LangSmith/Vellum-style
per-node attribution), with opt-in cost when prices are supplied. (Replay-from-
JSONL — item 7's other half — is already wired in the designer.)"""

import glob
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

from runtime_overlay import RuntimeOverlay, summarize_trace

# 1. Per-agent attribution from a synthetic single-agent run
recs = [
    {"kind": "run_start", "task": "t"},
    {"kind": "stage_start", "agent": "agent"},
    {"kind": "llm_step", "agent": "agent", "step": 1, "tool_calls": ["load_csv"]},
    {"kind": "tool_call", "agent": "agent", "tool": "load_csv"},
    {"kind": "tool_result", "agent": "agent"},
    {"kind": "llm_step", "agent": "agent", "step": 2, "tool_calls": []},
    {"kind": "stage_end", "agent": "agent", "output": "done"},
    {"kind": "run_end", "result": "done",
     "usage": {"agent": {"input_tokens": 1200, "output_tokens": 340, "tool_calls": 1}}},
]
s = summarize_trace(recs)
a = s["per_agent"]["agent"]
assert a["input_tokens"] == 1200 and a["output_tokens"] == 340, a
assert a["tool_calls"] == 1 and a["llm_steps"] == 2, a
t = s["totals"]
assert t["input_tokens"] == 1200 and t["output_tokens"] == 340, t
assert t["tool_calls"] == 1 and t["llm_steps"] == 2 and t["agents_run"] == 1, t
assert t["finished"] is True and t["errored"] is False
print("ok 1: single-agent per-agent + totals (tokens/tools/steps) attributed")

# 2. Multi-agent attribution + retries/failovers counted
recs2 = [
    {"kind": "run_start", "task": "t"},
    {"kind": "stage_start", "agent": "planner"},
    {"kind": "llm_step", "agent": "planner", "step": 1},
    {"kind": "retry", "agent": "planner", "error": "429"},
    {"kind": "stage_end", "agent": "planner", "output": "plan"},
    {"kind": "stage_start", "agent": "executor"},
    {"kind": "llm_step", "agent": "executor", "step": 1},
    {"kind": "failover", "agent": "executor", "next_model": "backup"},
    {"kind": "llm_step", "agent": "executor", "step": 2},
    {"kind": "stage_end", "agent": "executor", "output": "out"},
    {"kind": "run_end", "result": "out", "usage": {
        "planner": {"input_tokens": 500, "output_tokens": 100, "tool_calls": 0},
        "executor": {"input_tokens": 800, "output_tokens": 250, "tool_calls": 3}}},
]
s2 = summarize_trace(recs2)
assert s2["per_agent"]["planner"]["input_tokens"] == 500
assert s2["per_agent"]["executor"]["tool_calls"] == 3
assert s2["per_agent"]["executor"]["llm_steps"] == 2
assert s2["totals"]["input_tokens"] == 1300 and s2["totals"]["output_tokens"] == 350
assert s2["totals"]["retries"] == 1 and s2["totals"]["failovers"] == 1, s2["totals"]
assert s2["totals"]["agents_run"] == 2
print("ok 2: multi-agent attribution; retries + failovers counted")

# 3. Opt-in cost when prices supplied
s3 = summarize_trace(recs2, prices={"input_per_1m": 0.28, "output_per_1m": 1.10})
# planner: 500/1e6*0.28 + 100/1e6*1.10 = 0.00014 + 0.00011 = 0.00025
assert abs(s3["per_agent"]["planner"]["cost_usd"] - 0.00025) < 1e-9, s3["per_agent"]["planner"]
exec_cost = 800 / 1e6 * 0.28 + 250 / 1e6 * 1.10
assert abs(s3["per_agent"]["executor"]["cost_usd"] - round(exec_cost, 6)) < 1e-9
assert abs(s3["totals"]["cost_usd"] - round(0.00025 + exec_cost, 6)) < 1e-9, s3["totals"]
# without prices, no cost key
assert "cost_usd" not in s2["totals"]
print("ok 3: cost attributed per agent + total only when prices are supplied")

# 4. run_start resets accumulation (a second run in the same overlay)
ov = RuntimeOverlay([])
for r in recs2:
    ov.consume(r)
assert ov.summary()["totals"]["retries"] == 1
for r in recs:                     # a fresh run via run_start must reset
    ov.consume(r)
fresh = ov.summary()["totals"]
assert fresh["retries"] == 0 and fresh["failovers"] == 0, fresh
assert fresh["input_tokens"] == 1200, fresh
print("ok 4: run_start resets usage/retries/failovers for the next run")

# 5. Robustness: empty / garbage / mid-run (no run_end yet) → no crash, no tokens
assert summarize_trace([])["totals"]["input_tokens"] == 0
assert summarize_trace([1, "x", None, {"kind": "bogus"}])["per_agent"] == {}
mid = summarize_trace(recs[:5])          # stopped before run_end
assert mid["totals"]["input_tokens"] == 0          # tokens only arrive at run_end
assert mid["totals"]["llm_steps"] == 1 and mid["totals"]["finished"] is False
print("ok 5: empty/garbage/mid-run handled (tokens only at run_end, no crash)")

# 5b. malformed run_end.usage values must not crash (a hand-edited/corrupt trace):
#     a non-dict usage for a KNOWN node, and non-int token values → coerced to 0.
bad1 = [{"kind": "run_start"}, {"kind": "stage_start", "agent": "agent"},
        {"kind": "llm_step", "agent": "agent", "step": 1},
        {"kind": "run_end", "usage": {"agent": "not-a-dict"}}]
g1 = summarize_trace(bad1)
assert g1["per_agent"]["agent"]["input_tokens"] == 0, g1
bad2 = [{"kind": "run_start"}, {"kind": "stage_start", "agent": "agent"},
        {"kind": "run_end", "usage": {"agent": {"input_tokens": "abc",
                                                 "output_tokens": None}}}]
g2 = summarize_trace(bad2)
assert g2["per_agent"]["agent"]["input_tokens"] == 0
assert g2["per_agent"]["agent"]["output_tokens"] == 0
# a top-level non-dict usage is also safe
assert summarize_trace([{"kind": "run_end", "usage": "garbage"}])["per_agent"] == {}
print("ok 5b: malformed usage (non-dict value, non-int tokens) coerced, no crash")

# 6. Real saved trace files round-trip through summarize_trace without error
real = sorted(glob.glob(os.path.join(BASE, "generated_agents", "*", "traces", "*.jsonl")))
checked = 0
for path in real[:8]:
    with open(path, encoding="utf-8") as f:
        rs = [json.loads(line) for line in f if line.strip()]
    summ = summarize_trace(rs)
    assert "per_agent" in summ and "totals" in summ
    assert summ["totals"]["llm_steps"] >= 0
    checked += 1
assert checked >= 1, "no saved traces found to validate against"
print(f"ok 6: {checked} real saved trace(s) summarized cleanly")

# 7. Offscreen TracePanel renders the usage block from a finished overlay
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication  # noqa: E402
from canvas_qt.trace_panel import TracePanel  # noqa: E402

app = QApplication.instance() or QApplication([])
panel = TracePanel()
ov2 = RuntimeOverlay([])
for r in recs2:
    ov2.consume(r)
block = TracePanel._usage_block(ov2)
assert "Usage by agent" in block and "planner" in block and "executor" in block, block
assert "total:" in block and "1 retries" in block and "1 failovers" in block, block
panel.show_node(None, ov2)                 # run-summary mode renders without error
assert "Usage by agent" in panel.insp_body.toPlainText()
# empty overlay → no usage block (clean)
assert TracePanel._usage_block(RuntimeOverlay([])) == ""
print("ok 7: TracePanel run-summary shows the per-agent usage breakdown")

print("\nTRACE-SUMMARY CHECKS PASSED")
