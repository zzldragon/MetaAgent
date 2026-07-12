"""Verify the built-in set_state tool + the require-writes gate.

set_state: writer agents (non-empty `writes`) get a native tool to record their
shared-state fields — a reliable alternative to the ```state block that doesn't
fight an output-format constraint. require_writes (opt-in per agent): after the
agent runs, if it didn't record a declared writable field, re-prompt it (bounded)
then proceed. Runs the paths (tool + fenced back-compat + allow-list + the gate)."""
import importlib.util
import os
import shutil
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import graph_model as gm
import graph_codegen as gc

LLM = {"provider": "siliconflow", "model": "deepseek-ai/DeepSeek-V4-Flash",
       "api_key": "sk-x", "base_url": "https://api.siliconflow.cn/v1"}


def _llm(g, a):
    n = g.new_node("llm", a.x - 150, a.y + 120); n.props.update(LLM); g.add_edge(n.id, a.id)


def _load(g, name):
    out = gc.generate_from_graph(g, name, gui=False)
    sp = importlib.util.spec_from_file_location(name, os.path.join(out, "agent.py"))
    m = importlib.util.module_from_spec(sp); sp.loader.exec_module(m)
    return m, out


def pbook(require=False):
    """PictureBook shape: author (writes a greeting flag) -> If/Else -> End early
    / else -> illustrator. Exercises writer-agent set_state + the End early-exit."""
    g = gm.Graph()
    g.state_schema = [{"name": "greeting", "type": "bool", "reducer": "overwrite",
                       "default": False, "description": "true if the input is just a greeting"}]
    au = g.new_node("agent", 0, 0); au.name = "author"; au.props["role"] = "planner"
    au.props["writes"] = ["greeting"]; au.props["require_writes"] = require; _llm(g, au)
    c = g.new_node("condition", 250, 0); c.name = "is_greeting"
    end = g.new_node("end", 500, -80); end.name = "early_out"
    il = g.new_node("agent", 500, 80); il.name = "illustrator"; _llm(g, il)
    g.add_edge(au.id, c.id); g.add_edge(c.id, end.id); g.add_edge(c.id, il.id)
    c.props["branches"] = [{"to": "early_out", "expr": "greeting == True"},
                           {"to": "illustrator", "expr": ""}]
    return g


# 1. tool wired to writer only; schema reflects the writable field's type
m, out = _load(pbook(), "vss_pbook")
assert "set_state" in m.AGENTS["author"]["tools"], m.AGENTS["author"]["tools"]
sch = m._set_state_tool_schema("author")
assert sch["name"] == "set_state"
assert sch["parameters"]["properties"]["greeting"]["type"] == "boolean", sch
assert "set_state" not in m.AGENTS["illustrator"]["tools"]      # no writes -> no tool
shutil.rmtree(out, ignore_errors=True)
print("1. set_state wired to writer only; schema exposes greeting:boolean")

# 2. author sets the flag via a TOOL CALL (not a fenced block) -> End early-exit
m, out = _load(pbook(), "vss_tool")
seen = []
def stub(name, cfg, system, messages):
    seen.append(name)
    if name == "author" and seen.count("author") == 1:
        return ("let me check", [{"id": "c1", "name": "set_state", "args": {"greeting": True}}])
    if name == "author":
        return ("Hi! Nice to meet you.", [])           # final: ONLY prose
    return ("[illustrator] drew pages", [])
m._call_one = stub; m.clear_history()
res = m.run("hi", emit=lambda s: None)
assert "illustrator" not in seen and res == "Hi! Nice to meet you.", (seen, repr(res))
shutil.rmtree(out, ignore_errors=True)
print("2. author set greeting via TOOL -> If/Else -> End; illustrator skipped")

# 3. back-compat: the fenced ```state block still works
m, out = _load(pbook(), "vss_fenced")
seen = []
def stub2(name, cfg, system, messages):
    seen.append(name)
    if name == "author":
        return ("Hi there!\n```state\ngreeting = True\n```", [])
    return ("[illustrator] drew pages", [])
m._call_one = stub2; m.clear_history()
res = m.run("hi", emit=lambda s: None)
assert "illustrator" not in seen and res == "Hi there!", (seen, repr(res))
shutil.rmtree(out, ignore_errors=True)
print("3. back-compat: fenced ```state block still routes to End")

# 4. allow-list: set_state ignores a field the agent may not write
m, out = _load(pbook(), "vss_allow")
m._RUN["state"] = m._new_state("x")
r = m._set_state({"greeting": True, "not_a_field": 9}, "author", emit=lambda s: None)
assert m._RUN["state"]["greeting"] is True and "not_a_field" not in m._RUN["state"], m._RUN["state"]
shutil.rmtree(out, ignore_errors=True)
print("4. set_state applies only the declared writes")

# 5. no-state single agent: no set_state tool (clean prompt/toolset)
g = gm.Graph(); a = g.new_node("agent", 0, 0); a.name = "solo"; _llm(g, a)
m, out = _load(g, "vss_solo")
assert "set_state" not in m.AGENTS["solo"]["tools"]
shutil.rmtree(out, ignore_errors=True)
print("5. no-state single agent: no set_state tool")

# 6. require_writes: agent that never writes -> retried (bounded) then proceeds
m, out = _load(pbook(require=True), "vss_req_giveup")
assert m.AGENTS["author"].get("require_writes") is True
lines = []; turns = {"n": 0}
def stub_no(name, cfg, s, ms):
    if name == "author":
        turns["n"] += 1; return ("I won't record anything.", [])
    return ("[illustrator] drew pages", [])
m._call_one = stub_no; m.clear_history()
res = m.run("hi", emit=lambda x: lines.append(str(x)))
log = "\n".join(lines)
assert turns["n"] == 3, turns                          # initial + 2 retries
assert "[require-writes]" in log and "proceeding" in log, log[-200:]
assert "[illustrator]" in res, res                     # proceeded (greeting stayed default)
shutil.rmtree(out, ignore_errors=True)
print("6. require_writes never-writes -> 2 retries then proceeds")

# 7. require_writes satisfied on a retry via set_state -> stop retrying, End early
m, out = _load(pbook(require=True), "vss_req_ok")
seen = []; t = {"n": 0}
def stub_late(name, cfg, s, ms):
    if name == "author":
        t["n"] += 1
        if t["n"] == 1:
            return ("thinking...", [])
        if t["n"] == 2:
            return ("", [{"id": "c1", "name": "set_state", "args": {"greeting": True}}])
        return ("Hi! Just a greeting.", [])
    seen.append(name); return ("[illustrator] drew", [])
m._call_one = stub_late; m.clear_history()
res = m.run("hi", emit=lambda x: None)
assert "illustrator" not in seen and res == "Hi! Just a greeting.", (seen, repr(res))
shutil.rmtree(out, ignore_errors=True)
print("7. require_writes satisfied on retry via set_state -> End early")

# 8. writes UI: require checkbox present with writable fields, round-trips; None w/o state
from PySide6.QtWidgets import QApplication
QApplication.instance() or QApplication([])
from canvas_qt.dialogs import _state_io_controls, _apply_state_io
class _D:  # noqa: E701
    pass
g = pbook(); au = next(n for n in g.nodes.values() if n.name == "author")
d = _D(); box = _state_io_controls(d, au, g)           # keep `box` alive (owns the widgets)
assert d._require_writes is not None
d._require_writes.setChecked(True); _apply_state_io(d, au)
assert au.props["require_writes"] is True
g2 = gm.Graph(); solo = g2.new_node("agent", 0, 0)
d2 = _D(); box2 = _state_io_controls(d2, solo, g2)
assert d2._require_writes is None
print("8. require-writes checkbox: present + round-trips; absent when no state")

print("\nALL SET_STATE + REQUIRE-WRITES CHECKS PASSED")
