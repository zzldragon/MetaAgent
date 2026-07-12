"""Verify the two new time-budget features:
  * per-agent `on_budget` policy — continue (default) / stop / retry — for when a
    stage returns a [budget] cap note.
  * run-level `max_run_wall_clock_s` deadline — stops the WHOLE run (not mid-call).
"""
import importlib.util
import os
import sys
import tempfile
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import graph_codegen as gc  # noqa: E402
import graph_model as gm  # noqa: E402

LLM = dict(provider="siliconflow", model="x", api_key="k",
           base_url="https://api.siliconflow.cn/v1")


def _agent(g, name):
    a = g.new_node("agent", 0, 0); a.name = name
    lm = g.new_node("llm", 0, 0); lm.props.update(LLM); g.add_edge(lm.id, a.id)
    return a


# chain: A -> B -> end
g = gm.Graph()
a = _agent(g, "A"); b = _agent(g, "B")
end = g.new_node("end", 0, 0)
g.add_edge(a.id, b.id); g.add_edge(b.id, end.id)
# default budgets/on_budget round-trip through generation
assert "on_budget" in gm.default_props("agent")

out = gc.generate_from_graph(g, "verify_budget_policy", gui=False)
spec = importlib.util.spec_from_file_location("vbp", os.path.join(out, "agent.py"))
m = importlib.util.module_from_spec(spec)
sys.path.insert(0, out); os.chdir(out); spec.loader.exec_module(m); os.chdir(BASE)
assert m.PATTERN_MODE in ("chain", "graph"), m.PATTERN_MODE  # helper covers both runners
_REAL_REACT = m.react                  # save before any stubbing (restored for test 4)
_BUDGET = "[budget] Wall-clock limit exceeded."


def _react_stub(ran):
    def fn(agent, question, emit=print):
        ran.append(agent)
        return _BUDGET if agent == "A" else "B-output"
    return fn


# 1. default 'continue' — A hits a budget, B still runs and its output is final
m.AGENTS["A"].pop("on_budget", None)
ran = []; m.react = _react_stub(ran)
res = m.run("go", emit=lambda s: None)
assert "B" in ran and res == "B-output", (ran, res)
print("ok 1: on_budget=continue (default) — the [budget] note flows downstream, B runs")

# 2. 'stop' — A hits a budget, the WHOLE run ends there, B never runs
m.AGENTS["A"]["on_budget"] = "stop"
ran = []; m.react = _react_stub(ran)
res = m.run("go", emit=lambda s: None)
assert ran == ["A"] and res.startswith("[budget]"), (ran, res)
print("ok 2: on_budget=stop — the run stops at the capped stage; downstream skipped")

# 3. 'retry' with stage_retries=1 — A re-runs once (fresh budget) then stops
m.AGENTS["A"]["on_budget"] = "retry"; m.AGENTS["A"]["stage_retries"] = 1
ran = []; m.react = _react_stub(ran)
res = m.run("go", emit=lambda s: None)
assert ran == ["A", "A"] and res.startswith("[budget]"), (ran, res)   # initial + 1 retry
print("ok 3: on_budget=retry — the stage re-runs stage_retries times, then stops")

# 4. run-level deadline — stops the whole run (via the REAL react, at a step boundary)
m.AGENTS["A"].pop("on_budget", None); m.AGENTS["A"]["stage_retries"] = 0
m.react = _REAL_REACT                               # restore the real react loop
m.CONFIG["max_run_wall_clock_s"] = 1
calls = []


def _call_one_stub(agent, cfg, system, messages):
    calls.append(agent)
    if agent == "A":
        m._rs().rec["run_start"] -= 9999           # simulate the deadline passing
        return "A-done", []
    return "B-done", []


m._call_one = _call_one_stub
res = m.run("go", emit=lambda s: None)
assert res.startswith("[budget] Run wall-clock"), res
assert "B" not in calls, calls                     # deadline stops the run before B
print("ok 4: max_run_wall_clock_s — the whole run stops at the next step boundary")

import shutil  # noqa: E402
shutil.rmtree(out, ignore_errors=True)
print("ALL BUDGET-POLICY CHECKS PASSED")
