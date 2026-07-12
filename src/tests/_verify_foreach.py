"""Verify the For-Each (map-over-list) node.

A For-Each node runs its `body` successor ONCE PER ITEM of a shared-state list
field (`over`), the items in PARALLEL on isolated state forks (a dynamic fan-out),
then takes the exit link once. The item is passed to the body BOTH as its input AND
(when `item_var` is set) into that state field; `result_field` collects each item's
output. Checks emission (the FOREACH table + STAGE_KINDS), the runtime (parallel
per-item execution, item delivery, result collection, merge), analyze() error cases,
the parallel-overwrite warning, and the byte-identical (no-foreach) case.
"""

import importlib.util
import os
import shutil
import sys
import threading

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import graph_codegen  # noqa: E402
from graph_model import Graph  # noqa: E402

LLM = {"provider": "siliconflow", "model": "deepseek-ai/DeepSeek-V4-Flash",
       "api_key": "sk-test", "base_url": "https://api.siliconflow.cn/v1"}


def _agent(g, name):
    a = g.new_node("agent", 0, 0); a.name = name
    lm = g.new_node("llm", 0, 0); lm.props.update(LLM); g.add_edge(lm.id, a.id)
    return a


def _build():
    """Seed[writes items] -> Loop(over=items) -> Worker -> back to Loop;
    Loop -> Report (exit). Worker reads item_var `cur`; outputs collected in `results`.
    Returns (graph, loop_node)."""
    g = Graph()
    g.recursion_limit = 80
    g.state_schema = [
        {"name": "items", "type": "list", "reducer": "extend", "default": [],
         "description": "work items"},
        {"name": "results", "type": "list", "reducer": "append", "default": [],
         "description": "collected outputs"},
        {"name": "cur", "type": "str", "reducer": "overwrite", "default": "",
         "description": "current item"}]
    seed = _agent(g, "Seed"); seed.props["writes"] = ["items"]
    loop = g.new_node("foreach", 0, 0); loop.name = "Loop"
    loop.props.update(over="items", body="Worker", item_var="cur",
                      result_field="results", merge="concat", max_parallel=0)
    worker = _agent(g, "Worker"); worker.props["reads"] = ["cur"]
    report = _agent(g, "Report")
    g.add_edge(seed.id, loop.id)       # Seed -> Loop
    g.add_edge(loop.id, worker.id)     # Loop -> Worker (body)
    g.add_edge(worker.id, loop.id)     # Worker -> Loop (links back)
    g.add_edge(loop.id, report.id)     # Loop -> Report (exit)
    return g, loop


def _load(g, name):
    out = graph_codegen.generate_from_graph(g, name, gui=False)
    spec = importlib.util.spec_from_file_location(name, os.path.join(out, "agent.py"))
    m = importlib.util.module_from_spec(spec)
    sys.path.insert(0, out); os.chdir(out)
    spec.loader.exec_module(m)
    os.chdir(BASE)
    return m, out


# ── 1. analyze + emission ────────────────────────────────────────────────────
g, loop = _build()
info = graph_codegen.analyze(g)
assert not info["errors"], info["errors"]
assert info["mode"] == "graph", info["mode"]
print("ok 1: a For-Each graph analyzes clean and resolves to graph mode")

m, out = _load(g, "verify_foreach_1")
assert m.STAGE_KINDS["Loop"] == "foreach", m.STAGE_KINDS.get("Loop")
fe = m.FOREACH["Loop"]
assert fe["over"] == "items" and fe["body"] == "Worker" and fe["exit"] == "Report", fe
assert fe["item_var"] == "cur" and fe["result_field"] == "results", fe
assert m.SUCCESSORS["Loop"] == ["Worker", "Report"], m.SUCCESSORS["Loop"]
print("ok 2: FOREACH table + STAGE_KINDS emitted (over/body/exit/item_var/result_field)")


# ── 2. runtime: parallel per-item execution + item delivery + collection ──────
_overlap = {"cur": 0, "max": 0}
_lock = threading.Lock()


def _stub(agent, cfg, system, messages):
    if agent == "Seed":
        return '```state\nitems = ["apple", "banana", "cherry", "date"]\n```', []
    if agent == "Worker":
        with _lock:
            _overlap["cur"] += 1
            _overlap["max"] = max(_overlap["max"], _overlap["cur"])
        import time; time.sleep(0.05)                 # widen the concurrency window
        q = messages[-1]["content"] if messages else ""
        tag = next((t for t in ("apple", "banana", "cherry", "date") if t in q), "?")
        with _lock:
            _overlap["cur"] -= 1
        return f"did:{tag}", []
    return "REPORT-DONE", []


m._call_one = _stub
res = m.run("go", emit=lambda s: None)
state = m._rs().rec.get("state", {})
ran = state.get("agents", []).count("Worker")
assert ran == 4, f"Worker ran {ran} times, expected 4"
assert sorted(state.get("results", [])) == ["did:apple", "did:banana",
                                            "did:cherry", "did:date"], state.get("results")
assert _overlap["max"] >= 2, f"no parallelism observed (max overlap {_overlap['max']})"
assert res == "REPORT-DONE", res            # the exit stage runs once after the map
print("ok 3: body ran once per item, in PARALLEL, with the item delivered + collected")


# ── 3. empty list -> body skipped, exit still runs ───────────────────────────
def _stub_empty(agent, cfg, system, messages):
    if agent == "Seed":
        return "no items today", []          # writes nothing -> items stays []
    if agent == "Worker":
        return "SHOULD-NOT-RUN", []
    return "REPORT-DONE", []


m._call_one = _stub_empty
res = m.run("go", emit=lambda s: None)
assert m._rs().rec.get("state", {}).get("agents", []).count("Worker") == 0
assert res == "REPORT-DONE", res
print("ok 4: an empty 'over' list skips the body and still runs the exit once")
shutil.rmtree(out, ignore_errors=True)


# ── 4. analyze error cases ───────────────────────────────────────────────────
def _errs(mut):
    g2, loop2 = _build()
    mut(g2, loop2)
    return " ".join(graph_codegen.analyze(g2)["errors"])


e = _errs(lambda g, l: l.props.update(over=""))
assert "no list to iterate" in e, e
e = _errs(lambda g, l: l.props.update(over="not_a_field"))
assert "unknown state field" in e, e
e = _errs(lambda g, l: l.props.update(item_var="ghost"))
assert "item variable 'ghost'" in e, e
e = _errs(lambda g, l: l.props.update(result_field="ghost"))
assert "result field 'ghost'" in e, e
e = _errs(lambda g, l: l.props.update(merge="bogus"))
assert "invalid merge" in e, e
print("ok 5: analyze rejects empty/unknown 'over', unknown item_var/result_field, bad merge")


# body that never links back to the For-Each node
def _no_loopback(g, l):
    # drop Worker -> Loop, add Worker -> Report so the body flows into the exit
    g.edges = [ed for ed in g.edges
               if not (g.nodes[ed.src].name == "Worker" and g.nodes[ed.dst].name == "Loop")]
    g.add_edge(next(n.id for n in g.nodes.values() if n.name == "Worker"),
               next(n.id for n in g.nodes.values() if n.name == "Report"))


e = _errs(_no_loopback)
assert "back to the For-Each node" in e, e
print("ok 6: analyze rejects a body that doesn't link back to the For-Each node")


# ── 5. parallel-overwrite warning ────────────────────────────────────────────
g3, loop3 = _build()
g3.state_schema.append({"name": "summary", "type": "str", "reducer": "overwrite",
                        "default": "", "description": "clobbered by parallel items"})
next(n for n in g3.nodes.values() if n.name == "Worker").props["writes"] = ["summary"]
w = " ".join(graph_codegen.analyze(g3)["warnings"])
assert "clobber" in w and "summary" in w, w
print("ok 7: analyze warns when the body writes an 'overwrite' field (parallel clobber)")


# ── 6. no For-Each -> empty table (byte-identical topology) ───────────────────
g4 = Graph()
a = _agent(g4, "solo")
m4, out4 = _load(g4, "verify_foreach_none")
assert m4.FOREACH == {}, m4.FOREACH
shutil.rmtree(out4, ignore_errors=True)
print("ok 8: a graph with no For-Each emits FOREACH == {} (byte-identical)")

print("ALL FOREACH CHECKS PASSED")
