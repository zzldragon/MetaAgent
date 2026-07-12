"""Verify the While loop node.

A While node lowers to the SAME condition table the If/Else node uses —
[(guard, body), (else, exit)] — so run_graph loops the body while the guard holds
and takes the exit when it fails. Also checks analyze() catches a missing
condition / body / exit, and warns when the body never routes back.
"""

import importlib.util
import os
import shutil
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import graph_codegen  # noqa: E402
from app_config import GENERATED_DIR  # noqa: E402
from graph_model import Graph  # noqa: E402

LLM = {"provider": "siliconflow", "model": "deepseek-ai/DeepSeek-V4-Flash",
       "api_key": "sk-test", "base_url": "https://api.siliconflow.cn/v1"}


def _build():
    """Start -> Loop(while attempts<3) -> Work -> Bump(attempts+1) -> back to Loop;
    Loop -> Report when the guard fails. Returns (graph, loop_node)."""
    g = Graph()
    g.recursion_limit = 50
    g.state_schema = [{"name": "attempts", "type": "int", "reducer": "overwrite",
                       "default": 0, "description": "loop counter"}]
    start = g.new_node("agent", 0, 0); start.name = "Start"
    ls = g.new_node("llm", 0, 0); ls.props.update(LLM); g.add_edge(ls.id, start.id)
    loop = g.new_node("while", 0, 0); loop.name = "Loop"
    loop.props["condition"] = "attempts < 3"; loop.props["body"] = "Work"
    work = g.new_node("agent", 0, 0); work.name = "Work"
    lw = g.new_node("llm", 0, 0); lw.props.update(LLM); g.add_edge(lw.id, work.id)
    bump = g.new_node("setstate", 0, 0); bump.name = "Bump"
    bump.props["assignments"] = [{"field": "attempts", "value": "=attempts + 1"}]
    report = g.new_node("agent", 0, 0); report.name = "Report"
    lr = g.new_node("llm", 0, 0); lr.props.update(LLM); g.add_edge(lr.id, report.id)
    g.add_edge(start.id, loop.id)      # Start -> Loop
    g.add_edge(loop.id, work.id)       # Loop -> Work (body)
    g.add_edge(loop.id, report.id)     # Loop -> Report (exit)
    g.add_edge(work.id, bump.id)       # Work -> Bump
    g.add_edge(bump.id, loop.id)       # Bump -> Loop (back-edge through a setstate)
    return g, loop


# 1. analyzes clean — graph mode, no errors, no warnings (the body loops back
#    through the Set-State, which the reachability check accepts).
g, loop = _build()
info = graph_codegen.analyze(g)
assert not info["errors"], info["errors"]
assert info["mode"] == "graph", info["mode"]
assert not info["warnings"], info["warnings"]
print("ok 1: while graph analyzes clean (mode=graph, no errors/warnings)")

# 2. lowers to a condition table the runtime already understands.
out = os.path.join(GENERATED_DIR, "verify_while")
if os.path.exists(out):
    shutil.rmtree(out)
out = graph_codegen.generate_from_graph(g, "verify_while", gui=False)
spec = importlib.util.spec_from_file_location("verify_while_agent",
                                              os.path.join(out, "agent.py"))
mod = importlib.util.module_from_spec(spec)
sys.path.insert(0, out); os.chdir(out)
spec.loader.exec_module(mod)
os.chdir(BASE)
assert mod.STAGE_KINDS["Loop"] == "while", mod.STAGE_KINDS
assert mod.CONDITIONS["Loop"] == [("attempts < 3", "Work"), (None, "Report")], \
    mod.CONDITIONS["Loop"]
print("ok 2: While lowers to STAGE_KINDS=while + table [(guard,body),(else,exit)]")

# 3. run_graph loops the body while the guard holds, then exits (no network: the
#    agent LLM is stubbed; the loop math is deterministic via the Set-State).
mod._call_one = lambda a, c, s, m: (f"[{a}]", [])
lines = []
res = mod.run("go", emit=lambda s: lines.append(str(s)))
log = "\n".join(lines)
assert log.count("Loop -> Work") == 3, log.count("Loop -> Work")
assert log.count("Loop -> Report") == 1, log
assert "[Report]" in res, res
print("ok 3: run_graph loops the body 3x then exits (attempts 0 -> 3)")


# 4. analyze catches a malformed While: no condition / no body / no exit.
def _errors_after(mutate):
    g2, lp = _build()
    mutate(g2, lp)
    return graph_codegen.analyze(g2)["errors"]


assert any("no loop condition" in e for e in
           _errors_after(lambda g, l: l.props.update(condition=""))), "missing condition"
assert any("no loop body" in e for e in
           _errors_after(lambda g, l: l.props.update(body=""))), "missing body"


def _drop_exit(g, l):
    g.edges = [e for e in g.edges
               if not (e.src == l.id and g.nodes[e.dst].name == "Report")]


assert any("needs an exit" in e for e in _errors_after(_drop_exit)), "missing exit"
print("ok 4: analyze errors on missing condition / body / exit")

# 5. warns when the body can never route back to the While node (runs once).
g5, lp5 = _build()
g5.edges = [e for e in g5.edges
            if not (g5.nodes[e.src].name == "Bump" and e.dst == lp5.id)]
assert any("never links back" in w for w in graph_codegen.analyze(g5)["warnings"]), \
    "no-loop-back warning missing"
print("ok 5: analyze warns when the body never links back")

# 6. per-loop max_iterations caps the loop BEFORE the condition would (Phase 4);
#    a blank cap emits no config key (byte-identical).
import json  # noqa: E402
assert "while_max_iterations" not in json.load(
    open(os.path.join(out, "config.json"), encoding="utf-8")), "blank cap must emit nothing"
g6, loop6 = _build()
loop6.props["max_iterations"] = 1              # cap at 1 though attempts<3 allows 3
out6 = graph_codegen.generate_from_graph(g6, "verify_while_cap", gui=False)
cfg6 = json.load(open(os.path.join(out6, "config.json"), encoding="utf-8"))
assert cfg6.get("while_max_iterations", {}).get("Loop") == 1, cfg6.get("while_max_iterations")
spec6 = importlib.util.spec_from_file_location("verify_while_cap_agent",
                                               os.path.join(out6, "agent.py"))
mod6 = importlib.util.module_from_spec(spec6)
sys.path.insert(0, out6); os.chdir(out6)
spec6.loader.exec_module(mod6); os.chdir(BASE)
mod6._call_one = lambda a, c, s, m: (f"[{a}]", [])
lines6 = []
res6 = mod6.run("go", emit=lambda s: lines6.append(str(s)))
log6 = "\n".join(lines6)
assert log6.count("Loop -> Work") == 1, log6.count("Loop -> Work")
assert "max_iterations=1" in log6, log6
assert "[Report]" in res6, res6
shutil.rmtree(out6, ignore_errors=True)
print("ok 6: while max_iterations caps the loop; blank emits no config key")

print("ALL WHILE-NODE CHECKS PASSED")
