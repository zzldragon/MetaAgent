"""Verify the built-in read-only `user_input` shared-state field: it's present on
every graph, seeded once from the run's task, readable by agents (opt-in) and by
conditions (so routing can branch on the original request), and write-protected
against both agent writes and setstate assignments."""

import importlib.util
import os
import shutil
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import graph_codegen
import graph_model as gm

LLM = {"provider": "siliconflow", "model": "deepseek-ai/DeepSeek-V4-Flash",
       "api_key": "sk-test", "base_url": "https://api.siliconflow.cn/v1"}


def _llm(g, a):
    n = g.new_node("llm", a.x - 200, a.y)
    n.props.update(LLM)
    assert g.add_edge(n.id, a.id) is None


def _load(g, name):
    out = graph_codegen.generate_from_graph(g, name, gui=False)
    spec = importlib.util.spec_from_file_location(name + "_a", os.path.join(out, "agent.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m, out


# 1. user_input is a reserved built-in str field, present on EVERY graph.
assert "user_input" in gm.RESERVED_STATE_NAMES
f = next(x for x in gm.state_fields(gm.Graph()) if x["name"] == "user_input")
assert f["builtin"] and f["type"] == "str" and f["reducer"] == "overwrite"
# a user can't redeclare it — a same-named user field is dropped by state_fields
g0 = gm.Graph()
g0.state_schema = [{"name": "user_input", "type": "int", "reducer": "overwrite",
                    "default": 5, "description": "mine"}]
ui = [x for x in gm.state_fields(g0) if x["name"] == "user_input"]
assert len(ui) == 1 and ui[0]["builtin"] and ui[0]["type"] == "str", ui
print("1. user_input is a built-in, present everywhere, not user-redeclarable")

# 2. run() seeds state['user_input'] = the task; a reader agent sees it in-prompt.
g = gm.Graph()
p = g.new_node("agent", 0, 0); p.name = "P"; _llm(g, p)
w = g.new_node("agent", 300, 0); w.name = "W"; _llm(g, w)
assert g.add_edge(p.id, w.id) is None
p.props["reads"] = ["user_input"]
m, out = _load(g, "demo_user_input_read")
assert "user_input" in {x["name"] for x in m.STATE_SCHEMA}
assert m._new_state("Q-SEED")["user_input"] == "Q-SEED"
assert m._new_state()["user_input"] == ""
seen = {}
m._call_one = lambda name, cfg, system, msgs: (seen.__setitem__(name, system), ("ok", []))[1]
m.clear_history()
m.run("ORIGINAL-REQUEST-XYZ", emit=lambda s: None)
assert m._RUN["state"]["user_input"] == "ORIGINAL-REQUEST-XYZ", m._RUN["state"]
assert "user_input" in seen["P"], "a reader agent's prompt must document user_input"
shutil.rmtree(out, ignore_errors=True)
print("2. run() seeds user_input=task; reader agent sees it")

# 3. a Condition can branch on user_input (read access across the whole graph).
g = gm.Graph()
t = g.new_node("agent", 0, 0); t.name = "triage"; _llm(g, t)
c = g.new_node("condition", 250, 0); c.name = "route"
fast = g.new_node("agent", 500, -80); fast.name = "fast"; _llm(g, fast)
slow = g.new_node("agent", 500, 80); slow.name = "slow"; _llm(g, slow)
assert g.add_edge(t.id, c.id) is None
assert g.add_edge(c.id, fast.id) is None
assert g.add_edge(c.id, slow.id) is None
c.props["branches"] = [{"expr": "'urgent' in user_input", "to": "fast"},
                       {"expr": "", "to": "slow"}]
m, out = _load(g, "demo_user_input_cond")
assert m.PATTERN_MODE == "graph"
visited = []
m._call_one = lambda name, cfg, system, msgs: (visited.append(name), ("done", []))[1]
m.clear_history(); visited.clear()
m.run("this is urgent please help", emit=lambda s: None)
assert "fast" in visited and "slow" not in visited, visited
m.clear_history(); visited.clear()
m.run("just a normal question", emit=lambda s: None)
assert "slow" in visited and "fast" not in visited, visited
shutil.rmtree(out, ignore_errors=True)
print("3. a Condition routes on user_input (urgent -> fast, else -> slow)")

# 4. read-only: neither an agent write nor a setstate assignment can change it.
g = gm.Graph()
a = g.new_node("agent", 0, 0); a.name = "A"; _llm(g, a)
a.props["writes"] = ["user_input"]                      # attempt to make it writable
ss = g.new_node("setstate", 250, 0); ss.name = "clobber"
ss.props["assignments"] = [{"field": "user_input", "value": "hacked"}]
b = g.new_node("agent", 500, 0); b.name = "B"; _llm(g, b)
assert g.add_edge(a.id, ss.id) is None
assert g.add_edge(ss.id, b.id) is None
info = graph_codegen.analyze(g)
assert not info["errors"], info["errors"]
# the setstate's reserved assignment is flagged as ignored
assert any("user_input" in w and "ignored" in w.lower() for w in info["warnings"]), info["warnings"]
m, out = _load(g, "demo_user_input_ro")
# emitted setstate table must not contain user_input (reserved names are dropped)
assert "user_input" not in repr(m.CONDITIONS.get("clobber", [])), m.CONDITIONS.get("clobber")
m._call_one = lambda name, cfg, system, msgs: ("ok", [])
m.clear_history()
m.run("PROTECTED-INPUT", emit=lambda s: None)
assert m._RUN["state"]["user_input"] == "PROTECTED-INPUT", "user_input must be immutable"
shutil.rmtree(out, ignore_errors=True)
print("4. user_input is read-only (agent write + setstate assignment both blocked)")

print("\nALL USER_INPUT CHECKS PASSED")
