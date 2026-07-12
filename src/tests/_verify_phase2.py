"""Verify Phase-2 Extra Settings in the GENERATED runtime (no network — LLM calls
are stubbed): per-LLM max_retries + tool_choice, agent max_rpm/stage_retries,
max_budget_usd cost cap, and the structured final-answer schema. Also asserts
that a graph leaving all knobs blank emits NO new keys (byte-identical)."""

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


def _gen(tag, agent_props=None, llm_props=None):
    g = Graph()
    a = g.new_node("agent", 0, 0); a.name = "agent"
    a.props.update(agent_props or {})
    llm = g.new_node("llm", 0, 0)
    llm.props.update(LLM); llm.props.update(llm_props or {})
    g.add_edge(llm.id, a.id)
    out = graph_codegen.generate_from_graph(g, tag, gui=False)
    spec = importlib.util.spec_from_file_location(tag + "_agent",
                                                  os.path.join(out, "agent.py"))
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod, out


# ── 1. byte-identical when blank: no new keys in the AGENTS spec ─────────────
mod, out = _gen("p2_blank")
spec = mod.AGENTS["agent"]
for k in ("max_rpm", "stage_retries", "final_schema"):
    assert k not in spec, f"{k} must be absent when unset ({spec})"
assert "max_budget_usd" not in spec.get("budgets", {}), spec["budgets"]
cfg = json.load(open(os.path.join(out, "config.json"), encoding="utf-8"))
for c in cfg["llms"]["agent"]:
    for k in ("max_retries", "tool_choice", "price_in_per_1m", "price_out_per_1m"):
        assert k not in c, f"{k} must be absent when unset ({c})"
print("1. blank knobs emit nothing (byte-identical spec + config) ok")

# ── 2. _tool_choice_kwargs wire shapes (OpenAI vs Anthropic) ─────────────────
tc = mod._tool_choice_kwargs
assert tc({"tool_choice": "any"}, "openai") == "required"
assert tc({"tool_choice": "none"}, "openai") == "none"
assert tc({"tool_choice": "specific", "tool_choice_name": "f"}, "openai") == \
    {"type": "function", "function": {"name": "f"}}
assert tc({"tool_choice": "any"}, "anthropic") == {"type": "any"}
assert tc({"tool_choice": "specific", "tool_choice_name": "f"}, "anthropic") == \
    {"type": "tool", "name": "f"}
assert tc({"tool_choice": "auto"}, "openai") is None          # auto -> omit
assert tc({"tool_choice": "specific"}, "openai") is None      # no name -> omit
print("2. _tool_choice_kwargs translates provider shapes ok")

# ── 3. per-LLM max_retries overrides the framework default ───────────────────
for retries, expect_calls in ((None, 3), (0, 1)):
    props = {"max_retries": str(retries)} if retries is not None else {}
    m, o = _gen(f"p2_retry_{retries}", llm_props=props)
    calls = {"n": 0}

    class _Boom(Exception):                     # class name contains "Timeout"
        pass
    _Boom.__name__ = "FakeTimeout"

    def _stub(agent_name, cfg, system, messages, force_tools=False, _c=calls):
        _c["n"] += 1
        raise _Boom("boom")
    m._call_one = _stub
    m.time.sleep = lambda *_a: None             # don't actually back off
    try:
        m._call_with_retry("agent", m.CONFIG["llms"]["agent"][0], "sys", [])
    except Exception:
        pass
    assert calls["n"] == expect_calls, (retries, calls["n"], expect_calls)
print("3. per-LLM max_retries honored (default=3 calls, 0=1 call) ok")

# ── 4. structured final-answer schema: validate + bounded re-ask + coerce ────
SCHEMA = '{"type":"object","properties":{"n":{"type":"integer"}},"required":["n"]}'
m, o = _gen("p2_schema", agent_props={"final_schema": SCHEMA, "final_schema_retries": 2})
# 4a. the validator subset works on the imported module
ok, why, canon = m._validate_final_schema(json.loads(SCHEMA), 'here you go: {"n": 5}')
assert ok and json.loads(canon) == {"n": 5}, (ok, canon)     # prose stripped
assert not m._validate_final_schema(json.loads(SCHEMA), "no json")[0]
assert not m._validate_final_schema(json.loads(SCHEMA), '{"n": "x"}')[0]   # wrong type
assert not m._validate_final_schema(json.loads(SCHEMA), '{}')[0]           # required
assert m.AGENTS["agent"]["final_schema"]["required"] == ["n"]
assert "## Structured final answer" in m.AGENTS["agent"]["system"]  # persona tail
# 4b. turn 1 prose -> re-ask -> turn 2 valid JSON; returns canonical JSON
seq = ["the answer is five", '{"n": 5}']
calls = {"n": 0}
def _stub_schema(agent_name, cfg, system, messages, force_tools=False):
    i = calls["n"]; calls["n"] += 1
    return seq[min(i, len(seq) - 1)], []
m._call_one = _stub_schema
m.clear_history()
res = m.run("give a number", emit=lambda *_a: None)
assert calls["n"] == 2, calls                 # one re-ask
assert json.loads(res) == {"n": 5}, res       # coerced to clean JSON
print("4. final_schema validates, re-asks once, returns canonical JSON ok")

# ── 5. stage_retries re-runs a stage on a transient error; not on cancel ─────
m, o = _gen("p2_stage", agent_props={"stage_retries": 2})
m.time.sleep = lambda *_a: None
rc = {"n": 0}
def _react_flaky(name, question, emit=print):
    rc["n"] += 1
    if rc["n"] < 3:
        raise RuntimeError("transient")
    return "recovered"
m.react = _react_flaky
assert m.run_stage("agent", "q", emit=lambda *_a: None) == "recovered"
assert rc["n"] == 3, rc                        # 1 + 2 retries
rc["n"] = 0
def _react_cancel(name, question, emit=print):
    rc["n"] += 1
    raise m.RunCancelled("stop")
m.react = _react_cancel
try:
    m.run_stage("agent", "q", emit=lambda *_a: None)
    raise AssertionError("RunCancelled must propagate")
except m.RunCancelled:
    pass
assert rc["n"] == 1, "cancel must NOT be retried"
print("5. stage_retries retries transient errors, never a cancel ok")

# ── 6. max_budget_usd cost cap aborts once estimated cost passes the cap ──────
m, o = _gen("p2_cost",
            agent_props={"max_budget_usd": 0.001},
            llm_props={"price_in_per_1m": "1000", "price_out_per_1m": "1000"})
cfgj = json.load(open(os.path.join(o, "config.json"), encoding="utf-8"))
assert cfgj["llms"]["agent"][0]["price_in_per_1m"] == 1000.0, "prices in config"
def _stub_cost(agent_name, cfg, system, messages, force_tools=False):
    m._track(agent_name, cfg, 100000, 100000)   # ~$0.2 per call -> over $0.001
    return "", [{"id": "1", "name": "nope", "args": {}}]  # a tool call -> loop again
m._call_one = _stub_cost
m.clear_history()
res = m.run("spend", emit=lambda *_a: None)
assert res.startswith("[budget]") and "Cost" in res, res
print("6. max_budget_usd cost cap aborts the run ok")

# ── 7. _rpm_throttle is inert when max_rpm is unset ─────────────────────────
m, o = _gen("p2_rpm")
slept = {"n": 0}
m.time.sleep = lambda *_a: slept.__setitem__("n", slept["n"] + 1)
m._rpm_throttle("agent")                        # max_rpm absent -> no sleep
assert slept["n"] == 0
m.AGENTS["agent"]["max_rpm"] = 60               # 1/sec: first call no wait
m._rpm_throttle("agent"); m._rpm_throttle("agent")
assert slept["n"] >= 1, "second call within the window should throttle"
print("7. _rpm_throttle inert by default, throttles when set ok")

print("\nALL PHASE-2 CHECKS PASSED")
