"""Verify the terminal End node: a sink (no outgoing links) that finishes a
graph-mode run early, returning whatever output was carried into it. Covers the
registries + edge rules, analyzer validation, run_graph early-return (incl. the
PictureBook If/Else->End pattern), that End returns the CARRIED value (so a
guardrail's redaction in the path is honored), package code-style, and the
canvas branch-dialog accepting an End target."""
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


def _load(g, name, code_style="single"):
    out = gc.generate_from_graph(g, name, gui=False, code_style=code_style)
    sp = importlib.util.spec_from_file_location(name, os.path.join(out, "agent.py"))
    m = importlib.util.module_from_spec(sp)
    if code_style == "package":
        sys.path.insert(0, out); here = os.getcwd(); os.chdir(out)
        try:
            sp.loader.exec_module(m)
        finally:
            os.chdir(here); sys.path.remove(out)
    else:
        sp.loader.exec_module(m)
    return m, out


# ── 0. registries + edge rules ───────────────────────────────────────────────
import canvas_qt.designer as dz
import canvas_qt.dialogs as dlg
assert "end" in gm.NODE_KINDS and "end" in gm.CONTROL_KINDS
assert gm.KIND_META["end"]["label"] == "End"
assert dz.KIND_SHAPE["end"] == "stadium" and dlg._DIALOGS["end"] is dlg.EndDialog
assert ("agent", "end") in gm.ALLOWED_EDGES and ("condition", "end") in gm.ALLOWED_EDGES
assert ("while", "end") in gm.ALLOWED_EDGES
assert all(("end", b) not in gm.ALLOWED_EDGES for b in gm.FLOW_KINDS)  # true sink
print("0. registries + edge rules: End is a registered terminal sink")

# ── 1. add_edge: End is a sink; router can't feed it; a HITL CAN (route branch) ─
gx = gm.Graph()
xa = gx.new_node("agent", 0, 0); xe = gx.new_node("end", 300, 0)
xr = gx.new_node("router", 150, 150); xh = gx.new_node("hitl", 150, -150)
assert gx.add_edge(xa.id, xe.id) is None                     # agent -> end OK
assert (gx.add_edge(xe.id, xa.id) or "").startswith("Cannot link")   # end -> agent
assert (gx.add_edge(xr.id, xe.id) or "").startswith("Cannot link")   # router -> end
# hitl -> end is now ALLOWED: a route-mode HITL (2+ outgoing) may branch to End
# ("human stops here"). A gate-mode HITL pointing only at End is still caught as
# malformed by graph_codegen._validate_hitl, not by the edge rule.
assert gx.add_edge(xh.id, xe.id) is None                     # hitl -> end OK
assert ("hitl", "end") in gm.ALLOWED_EDGES
print("1. add_edge: agent->End ok; End->agent & router->End refused; hitl->End ok")

# clean graph for the run checks below
g = gm.Graph()
a = g.new_node("agent", 0, 0); a.name = "worker"; _llm(g, a)
e = g.new_node("end", 300, 0); e.name = "done"; g.add_edge(a.id, e.id)

# ── 2. unconditional agent -> End : graph mode + early return of carried ─────
info = gc.analyze(g)
assert not info["errors"] and info["mode"] == "graph", (info["errors"], info["mode"])
m, out = _load(g, "vend_uncond")
assert m.STAGE_KINDS["done"] == "end" and m.SUCCESSORS["done"] == []
m._call_one = lambda n, c, s, ms: ("hi there", [])
assert m.run("x", emit=lambda s: None) == "hi there"
shutil.rmtree(out, ignore_errors=True)
print("2. agent->End: graph mode, lowers to STAGE_KINDS=end, returns carried")

# ── 3. PictureBook: entry -> If/Else -> (greeting) End early / (else) illustrator
def _pbook():
    g = gm.Graph()
    g.state_schema = [{"name": "greeting", "type": "bool", "reducer": "overwrite",
                       "default": True, "description": "input is only a greeting"}]
    au = g.new_node("agent", 0, 0); au.name = "author"; au.props["role"] = "planner"; _llm(g, au)
    c = g.new_node("condition", 250, 0); c.name = "is_greeting"
    en = g.new_node("end", 500, -80); en.name = "early_out"
    il = g.new_node("agent", 500, 80); il.name = "illustrator"; _llm(g, il)
    g.add_edge(au.id, c.id); g.add_edge(c.id, en.id); g.add_edge(c.id, il.id)
    c.props["branches"] = [{"to": "early_out", "expr": "greeting == True"},
                           {"to": "illustrator", "expr": ""}]
    return g

g = _pbook()
assert not gc.analyze(g)["errors"], gc.analyze(g)["errors"]
m, out = _load(g, "vend_pbook")
seen = []
m._call_one = lambda n, c, s, ms: (seen.append(n) or (f"[{n}] hi!", []))
res = m.run("hello", emit=lambda s: None)
assert "illustrator" not in seen and res == "[author] hi!", (seen, res)
shutil.rmtree(out, ignore_errors=True)
print("3. If/Else->End early: illustrator skipped, returns entry output")

# ── 4. End returns the CARRIED value (post-guardrail), not the raw agent result
g = gm.Graph()
a = g.new_node("agent", 0, 0); a.name = "worker"; _llm(g, a)
gr = g.new_node("guardrail", 250, 0); gr.name = "scrub"
en = g.new_node("end", 500, 0); en.name = "stop"
g.add_edge(a.id, gr.id); g.add_edge(gr.id, en.id)
m, out = _load(g, "vend_guard")
m._call_one = lambda n, c, s, ms: ("my secret is XYZ", [])
# guardrail rewrites the carried content in place; End must return THAT, not result
m.guardrail_node_apply = lambda spec, content: ("[REDACTED]", False, "")
assert m.run("x", emit=lambda s: None) == "[REDACTED]", "End must return carried, not result"
shutil.rmtree(out, ignore_errors=True)
print("4. End returns carried (guardrail redaction honored), not the raw result")

# ── 5. analyze: outgoing-from-End = error; orphan End = warning ──────────────
g = gm.Graph()
w = g.new_node("agent", 0, 0); w.name = "w"; _llm(g, w)
en = g.new_node("end", 300, 0); en.name = "E"; g.add_edge(w.id, en.id)
g.edges.append(gm.Edge(en.id, w.id))                    # illegal outgoing edge
assert any("terminal node" in e for e in gc.analyze(g)["errors"])
g2 = gm.Graph()
w2 = g2.new_node("agent", 0, 0); w2.name = "w"; _llm(g2, w2)
g2.new_node("end", 300, 0).name = "orphan"              # no incoming
assert any("never be reached" in w for w in gc.analyze(g2)["warnings"])
# a hand-edited agent->hitl->End (bypassing add_edge) must NOT silently drop the
# review gate — _hitl_wiring excludes End, so it flags a malformed hitl instead.
g5 = gm.Graph()
w5 = g5.new_node("agent", 0, 0); w5.name = "w"; _llm(g5, w5)
h5 = g5.new_node("hitl", 150, 0); h5.name = "gate"
en5 = g5.new_node("end", 300, 0); en5.name = "stop"
g5.edges.append(gm.Edge(w5.id, h5.id)); g5.edges.append(gm.Edge(h5.id, en5.id))
assert gc.analyze(g5)["errors"], "hand-edited hitl->End must not analyze clean"
print("5. analyze: outgoing-from-End errors; orphan warns; hitl->End flagged")

# ── 6. package code-style: End works there too (run_graph is in agent.py engine)
m, out = _load(_pbook(), "vend_pkg", code_style="package")
assert m.STAGE_KINDS["early_out"] == "end"
seen = []
# in package mode llm() lives in runtime/_core and calls _core._call_one, so the
# stub MUST patch _core (patching agent._call_one would be ignored) — see the
# code-style memory. run_graph/react live in agent.py and read STAGE_KINDS.
core = sys.modules["runtime._core"]
core._call_one = lambda n, c, s, ms: (seen.append(n) or (f"[{n}] hi!", []))
res = m.run("hello", emit=lambda s: None)
assert "illustrator" not in seen and res == "[author] hi!", (seen, res)
shutil.rmtree(out, ignore_errors=True)
print("6. package code-style: End early-return works in the runtime/ package layout")

# ── 7. canvas: EdgeConditionBranchDialog accepts an End node as a branch target
from PySide6.QtWidgets import QApplication
QApplication.instance() or QApplication([])
from canvas_qt.dialogs import EdgeConditionBranchDialog, EndDialog
g = _pbook()
cond = next(n for n in g.nodes.values() if n.kind == "condition")
end = next(n for n in g.nodes.values() if n.kind == "end")
edge = next(e for e in g.edges if e.src == cond.id and e.dst == end.id)
d = EdgeConditionBranchDialog(None, edge, g)             # opens without error
# EndDialog round-trips the name
en2 = g.new_node("end", 0, 0); en2.name = "old"
ed = EndDialog(None, en2); ed.name.setText("finish"); assert ed.apply() is None
assert en2.name == "finish"
print("7. canvas: condition->End branch dialog opens; EndDialog round-trips name")

print("\nALL END-NODE CHECKS PASSED")
