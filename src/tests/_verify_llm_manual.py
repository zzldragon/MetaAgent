"""Verify authoritative manual LLM selection (roadmap item 2 / §6.2):
an agent with 2+ linked LLMs can be set to 'manual' mode so ONLY the selected
LLM is used (no failover), vs the default 'fallback' chain. Covers canvas props
-> config.json, the runtime get/set_llm_mode persistence, and the llm() dispatch
behavior (manual raises on the selected model's failure; fallback recovers)."""

import importlib.util
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import graph_codegen
from graph_model import Graph

A = {"provider": "siliconflow", "model": "model-a",
     "api_key": "sk-test", "base_url": "https://api.siliconflow.cn/v1"}
B = {"provider": "siliconflow", "model": "model-b",
     "api_key": "sk-test", "base_url": "https://api.siliconflow.cn/v1"}


def _gen(name, manual):
    """One agent, two linked LLMs (A primary, B fallback), optional manual mode."""
    g = Graph()
    a = g.new_node("agent", 0, 0); a.name = "agent"
    if manual:
        a.props["llm_mode"] = "manual"
    la = g.new_node("llm", 0, 0); la.props.update(A)
    lb = g.new_node("llm", 0, 0); lb.props.update(B)
    g.add_edge(la.id, a.id)          # first link = primary
    g.add_edge(lb.id, a.id)          # second link = fallback
    tool = g.new_node("tool", 0, 0); tool.props["files"] = ["load_csv.py"]
    g.add_edge(tool.id, a.id)
    out = graph_codegen.generate_from_graph(g, name, gui=False)
    # Hermetic: a prior run persists llm_choice.json / llm_mode.json (set_llm_*),
    # and regeneration does NOT clean them — so the module would import with a
    # stale selection/mode and the manual-mode assertions below would flip on
    # re-run. Start every run from defaults.
    for _fn in ("llm_choice.json", "llm_mode.json"):
        _p = os.path.join(out, _fn)
        if os.path.exists(_p):
            os.remove(_p)
    spec = importlib.util.spec_from_file_location(name + "_agent",
                                                  os.path.join(out, "agent.py"))
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    cfg = json.load(open(os.path.join(out, "config.json"), encoding="utf-8"))
    return mod, cfg, out


# ── 1. canvas props -> config.json: only 'manual' is emitted ────────────────
mod_m, cfg_m, out_m = _gen("demo_llm_manual", manual=True)
assert cfg_m.get("llm_modes") == {"agent": "manual"}, cfg_m.get("llm_modes")
mod_f, cfg_f, out_f = _gen("demo_llm_fallback", manual=False)
assert "llm_modes" not in cfg_f, "fallback is the default — don't emit it"
print("ok 1: manual mode reaches config.json; fallback is the default (omitted)")

# ── 2. runtime get/set_llm_mode + persistence to llm_mode.json ──────────────
assert mod_m.get_llm_mode("agent") == "manual"
assert mod_f.get_llm_mode("agent") == "fallback"
mod_f.set_llm_mode("agent", "manual")
assert mod_f.get_llm_mode("agent") == "manual"
saved = json.load(open(os.path.join(out_f, "llm_mode.json"), encoding="utf-8"))
assert saved.get("agent") == "manual", saved
try:
    mod_f.set_llm_mode("agent", "bogus")
    raise AssertionError("invalid mode must raise")
except ValueError:
    pass
mod_f.set_llm_mode("agent", "fallback")          # restore for the next check
print("ok 2: get/set_llm_mode round-trips, persists, rejects invalid values")


# ── 3. llm() dispatch: manual raises on the selected model; fallback recovers ─
def make_stub(mod):
    """_call_with_retry that fails for model-a and succeeds for model-b."""
    def stub(agent_name, cfg, system, messages, emit=print):
        if cfg["model"] == "model-a":
            raise RuntimeError("model-a is down")
        return ("answer-from-B", [])
    mod._call_with_retry = stub


# fallback mode: A fails -> fails over to B -> returns B's answer
make_stub(mod_f)
assert mod_f.get_llm_choice("agent") == 0, "primary (A) should be selected"
text, calls = mod_f.llm("agent", "sys", [{"role": "user", "content": "hi"}],
                        emit=lambda s: None)
assert text == "answer-from-B", text
print("ok 3a: fallback mode recovers by failing over to the second LLM")

# manual mode: only A is tried -> its failure surfaces, NO failover to B
make_stub(mod_m)
assert mod_m.get_llm_mode("agent") == "manual"
try:
    mod_m.llm("agent", "sys", [{"role": "user", "content": "hi"}],
              emit=lambda s: None)
    raise AssertionError("manual mode must NOT fail over")
except RuntimeError as e:
    assert "model-a" in str(e), e
print("ok 3b: manual mode surfaces the selected model's error (no failover)")

# manual mode honors the active choice: pick B -> B is used exclusively
mod_m.set_llm_choice("agent", 1)
text2, _ = mod_m.llm("agent", "sys", [{"role": "user", "content": "hi"}],
                     emit=lambda s: None)
assert text2 == "answer-from-B", text2
print("ok 3c: manual mode uses exactly the selected LLM")

print("\nLLM MANUAL-MODE CHECKS PASSED")
