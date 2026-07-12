"""Verify Phase-3 Extra Settings in the GENERATED runtime (no network): guardrail
node custom patterns/keywords/max-length, tool return_direct / error-policy / risk
override / description override, and HITL decisions + unattended timeout. Also that
a graph with none of these knobs emits NO new config keys (byte-identical)."""

import importlib.util
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import graph_codegen
from graph_model import Graph

LLM = dict(provider="siliconflow", model="deepseek-ai/DeepSeek-V4-Flash", api_key="sk")
TOOLFILE = "load_csv.py"
FUNCS = graph_codegen._tool_names(TOOLFILE)
FUNC = FUNCS[0]


def _gen(tag, agent_props=None, tool_props=None):
    g = Graph()
    a = g.new_node("agent", 0, 0); a.name = "agent"
    a.props.update(agent_props or {})
    llm = g.new_node("llm", 0, 0); llm.props.update(LLM)
    g.add_edge(llm.id, a.id)
    if tool_props is not None:
        t = g.new_node("tool", -200, 80)
        t.props["files"] = [TOOLFILE]
        t.props["tool_props"] = tool_props
        g.add_edge(t.id, a.id)
    out = graph_codegen.generate_from_graph(g, tag, gui=False)
    spec = importlib.util.spec_from_file_location(tag + "_agent",
                                                  os.path.join(out, "agent.py"))
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod, out


# ── 1. guardrail node: custom patterns / keywords / max-length ───────────────
mod, _ = _gen("p3_base")
ga = mod.guardrail_node_apply
new, blocked, _ = ga({"checks": {"secret": False}, "patterns": [r"SECRET-\d+"]},
                     "x SECRET-42 y")
assert "[REDACTED:custom]" in new and not blocked, new
new, blocked, _ = ga({"checks": {"secret": False}, "keywords": ["banana"]},
                     "I like BANANA bread")
assert "[REDACTED:custom]" in new, new                    # case-insensitive
_, blocked, _ = ga({"checks": {"secret": False}, "keywords": ["x"], "on_trip": "block"},
                   "has x")
assert blocked, "on_trip=block must block a custom hit"
new, _, _ = ga({"checks": {"secret": False}, "max_length": 5}, "0123456789")
assert new.startswith("01234") and "truncated" in new, new
# empty keyword must NOT redact everything
new, _, _ = ga({"checks": {"secret": False}, "keywords": ["", "  "]}, "hello world")
assert new == "hello world", new
print("1. guardrail node custom patterns/keywords/max-length ok")

# blank guardrail node stays byte-identical (only checks + on_trip)
gm_blank = Graph(); gn = gm_blank.new_node("guardrail", 0, 0); gn.name = "g"
assert set(graph_codegen._guardrail_node_cfg(gn)) == {"checks", "on_trip"}
print("1b. blank guardrail node cfg = {checks, on_trip} (byte-identical) ok")

# ── 2. tool return_direct: the tool output IS the final answer ───────────────
mod, _ = _gen("p3_rd", tool_props={FUNC: {"return_direct": True}})
mod.TOOLS[FUNC] = lambda **k: "DIRECT RESULT"
calls = {"n": 0}
def _rd_stub(agent_name, cfg, system, messages):
    calls["n"] += 1
    return "", [{"id": "1", "name": FUNC, "args": {}}]   # always request the tool
mod._call_one = _rd_stub; mod.clear_history()
res = mod.run("go", emit=lambda *_a: None)
assert res == "DIRECT RESULT", res
assert calls["n"] == 1, ("return_direct must short-circuit — no 2nd LLM call", calls)
print("2. tool return_direct short-circuits with the tool output ok")

# ── 3. tool error policy: retry then succeed ────────────────────────────────
mod, _ = _gen("p3_retry", tool_props={FUNC: {"error_mode": "retry", "error_retries": 2}})
tcalls = {"n": 0}
def _flaky(**k):
    tcalls["n"] += 1
    if tcalls["n"] < 3:
        raise RuntimeError("transient")
    return "TOOL OK"
mod.TOOLS[FUNC] = _flaky
step = {"n": 0}
def _retry_stub(agent_name, cfg, system, messages):
    step["n"] += 1
    if step["n"] == 1:
        return "", [{"id": "1", "name": FUNC, "args": {}}]
    return "done", []
mod._call_one = _retry_stub; mod.clear_history()
mod.run("go", emit=lambda *_a: None)
assert tcalls["n"] == 3, ("tool should be retried to success", tcalls)
print("3. tool error policy 'retry' recovers after transient failures ok")

# ── 4. tool error policy 'raise' aborts (ToolAborted, not retried by stage) ──
mod, _ = _gen("p3_raise", tool_props={FUNC: {"error_mode": "raise"}})
mod.TOOLS[FUNC] = lambda **k: (_ for _ in ()).throw(RuntimeError("boom"))
mod._call_one = lambda a, c, s, m: ("", [{"id": "1", "name": FUNC, "args": {}}])
mod.clear_history()
res = mod.run("go", emit=lambda *_a: None)
assert "[error]" in res.lower() or "abort" in res.lower(), res   # surfaced, not swallowed
print("4. tool error policy 'raise' aborts the run ok")

# ── 5. risk override + description override reach config / tool_schema ───────
mod, o = _gen("p3_riskdesc",
              tool_props={FUNC: {"risk": "high", "description": "custom help"}})
cfg = json.load(open(os.path.join(o, "config.json"), encoding="utf-8"))
assert FUNC in cfg["high_risk_tools"], cfg["high_risk_tools"]
assert mod.tool_schema(FUNC)["description"] == "custom help"
assert mod.is_high_risk(FUNC) is True
print("5. tool risk override + description override applied ok")

# ── 6. blank tool node: no new config keys (byte-identical) ──────────────────
mod, o = _gen("p3_toolblank", tool_props={})
cfg = json.load(open(os.path.join(o, "config.json"), encoding="utf-8"))
for k in ("return_direct_tools", "tool_error_policy", "tool_descriptions"):
    assert k not in cfg, f"{k} must be absent when no tool_props set"
print("6. blank tool node emits no new config keys ok")

# ── 7. HITL: unattended timeout auto-decides; decisions coerced ─────────────
import time as _t
mod, _ = _gen("p3_hitl")
mod.set_review_handler(lambda p, c: _t.sleep(5) or {"decision": "reject"})  # slow
r = mod.human_review("ok?", "content", timeout=1, on_timeout="approve")
assert r["decision"] == "approve", r                       # timed out -> auto approve
# _human_gate coerces an out-of-set decision to approve
mod.set_review_handler(lambda p, c: {"decision": "reject", "feedback": "no"})
out = mod._human_gate({"prompt": "ok?", "decisions": ["approve"]}, "PAYLOAD",
                      emit=lambda *_a: None)
assert out == "PAYLOAD", ("reject not allowed -> coerced to approve", out)
print("7. HITL timeout auto-decide + decisions coercion ok")

print("\nALL PHASE-3 CHECKS PASSED")
