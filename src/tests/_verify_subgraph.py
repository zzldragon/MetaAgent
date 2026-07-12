"""Verify the Subgraph / Call-Graph node: a `subgraph` node embeds another graph
(props['graph_json']) and is FLATTENED into the parent at generation time — child
nodes namespaced and spliced in place (parent → child entry; child exit → parent
successor; child End becomes a pass-through). Covers structure, nesting, the
recursion guard, byte-identical no-op when unused, and an end-to-end generated run.
"""
import importlib.util
import os
import sys
import tempfile

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


# ── 1. no subgraph node → returned unchanged (byte-identical downstream) ──────────
_plain = gm.Graph(); _a = _agent(_plain, "solo")
assert gm.expand_subgraphs(_plain) is _plain, "no-subgraph graphs must pass through"
print("ok 1: graphs without a subgraph node are returned unchanged")

# ── 2. flatten: parent(start → subgraph(child) → finish → end) ────────────────────
child = gm.Graph(); cw = _agent(child, "worker")
cend = child.new_node("end", 0, 0); cend.name = "cend"; child.add_edge(cw.id, cend.id)
child.state_schema.append({"name": "child_field", "type": "str",
                           "reducer": "overwrite", "default": ""})
cj = child.to_dict()

p = gm.Graph(); s = _agent(p, "start"); f = _agent(p, "finish")
sg = p.new_node("subgraph", 0, 0); sg.name = "comp"
sg.props = {"graph_name": "Child", "graph_json": cj}
pend = p.new_node("end", 0, 0); pend.name = "pend"
p.add_edge(s.id, sg.id); p.add_edge(sg.id, f.id); p.add_edge(f.id, pend.id)

flat = gm.expand_subgraphs(p)
names = {n.name for n in flat.nodes.values()}
assert not any(n.kind == "subgraph" for n in flat.nodes.values()), "subgraph must be gone"
assert "comp/worker" in names, "child agent namespaced into the parent"
assert [n.name for n in flat.nodes.values() if n.kind == "end"] == ["pend"], "child End dropped"
_e = {(flat.nodes[e.src].name, flat.nodes[e.dst].name) for e in flat.edges}
assert ("start", "comp/worker") in _e and ("comp/worker", "finish") in _e, _e
assert "child_field" in {ff["name"] for ff in flat.state_schema}, "child state merged"
print("ok 2: subgraph flattened — namespaced, spliced (parent→child→succ), state merged")

# ── 3. nested subgraph (child itself embeds a grandchild) flattens fully ──────────
gchild = gm.Graph(); gw = _agent(gchild, "leaf")
ge = gchild.new_node("end", 0, 0); gchild.add_edge(gw.id, ge.id)
mid = gm.Graph(); ms = _agent(mid, "mid")
msg = mid.new_node("subgraph", 0, 0); msg.name = "inner"
msg.props = {"graph_name": "Leaf", "graph_json": gchild.to_dict()}
me = mid.new_node("end", 0, 0); mid.add_edge(ms.id, msg.id); mid.add_edge(msg.id, me.id)
top = gm.Graph(); ts = _agent(top, "top")
tsg = top.new_node("subgraph", 0, 0); tsg.name = "outer"
tsg.props = {"graph_name": "Mid", "graph_json": mid.to_dict()}
te = top.new_node("end", 0, 0); top.add_edge(ts.id, tsg.id); top.add_edge(tsg.id, te.id)
nflat = gm.expand_subgraphs(top)
nn = {n.name for n in nflat.nodes.values()}
assert not any(n.kind == "subgraph" for n in nflat.nodes.values())
assert any("leaf" in x for x in nn), "grandchild leaf must be flattened in: %s" % nn
print("ok 3: nested subgraphs flatten fully (grandchild included)")

# ── 4. recursion guard: a child that re-includes the same graph_name raises ───────
rec_child = gm.Graph(); rw = _agent(rec_child, "w")
rsg = rec_child.new_node("subgraph", 0, 0); rsg.name = "self"
rsg.props = {"graph_name": "Loop", "graph_json": {"nodes": [{"id": "x", "kind": "agent",
             "name": "x", "x": 0, "y": 0, "props": {}}], "edges": []}}
re_ = rec_child.new_node("end", 0, 0); rec_child.add_edge(rw.id, rsg.id)
rp = gm.Graph(); ra = _agent(rp, "a")
rpsg = rp.new_node("subgraph", 0, 0); rpsg.name = "Loop"       # same name it re-includes
rpsg.props = {"graph_name": "Loop", "graph_json": rec_child.to_dict()}
rp.add_edge(ra.id, rpsg.id)
try:
    gm.expand_subgraphs(rp)
    raise AssertionError("recursive include should raise")
except ValueError as e:
    assert "ecursive" in str(e), e
print("ok 4: a recursive subgraph include is rejected")

# ── 5. analyze surfaces an empty/invalid embed as an error ────────────────────────
bad = gm.Graph(); ba = _agent(bad, "a")
bsg = bad.new_node("subgraph", 0, 0); bsg.name = "empty"; bsg.props = {"graph_json": {}}
bad.add_edge(ba.id, bsg.id)
assert gc.analyze(bad)["errors"], "an empty subgraph embed must be an analyze error"
print("ok 5: an empty subgraph embed is reported by analyze()")

# ── 6. end-to-end: generate + run — the child stage runs inside the parent ────────
out = gc.generate_from_graph(p, "verify_subgraph_e2e", gui=False)
spec = importlib.util.spec_from_file_location("vsg", os.path.join(out, "agent.py"))
m = importlib.util.module_from_spec(spec)
sys.path.insert(0, out); os.chdir(out); spec.loader.exec_module(m); os.chdir(BASE)
assert m.PIPELINE == ["start", "comp/worker", "finish"], m.PIPELINE
ran = []
m._call_one = lambda agent, cfg, system, messages: (ran.append(agent) or ("r-%s" % agent, []))
res = m.run("go", emit=lambda s: None)
assert ran == ["start", "comp/worker", "finish"], ran
assert res == "r-finish", res
import shutil  # noqa: E402
shutil.rmtree(out, ignore_errors=True)
print("ok 6: generated agent runs start → comp/worker → finish (child stage executed)")

print("ALL SUBGRAPH CHECKS PASSED")
