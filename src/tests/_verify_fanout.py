"""Verify the fan-out + join control nodes (Phase 1: SEQUENTIAL execution — correct
fan-in semantics before the concurrent phase). A fanout runs its N branches and
reconverges at a paired join; each branch's state writes merge (disjoint fields +
an accumulate reducer); the join's successor sees all of them; analyze validates the
fanout<->join pairing; a fan-out-free graph stays byte-identical (empty tables). Offline."""
import importlib.util
import os
import py_compile
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import graph_codegen
from graph_model import Graph, DEFAULT_BUDGETS

LLM = dict(provider="siliconflow", model="deepseek-ai/DeepSeek-V4-Flash",
           api_key="", base_url="https://api.siliconflow.cn/v1")


def _agent(g, name, y, reads, writes):
    a = g.new_node("agent", 0, y); a.name = name; a.props["role"] = "single"
    a.props["reads"] = reads; a.props["writes"] = writes
    for k in DEFAULT_BUDGETS:
        a.props[k] = DEFAULT_BUDGETS[k]
    return a


# dispatch -> fanout -> {A,B,C} -> join -> tail
g = Graph()
g.state_schema = [
    {"name": "ra", "type": "str", "reducer": "overwrite", "default": "", "description": "A's report"},
    {"name": "rb", "type": "str", "reducer": "overwrite", "default": "", "description": "B's report"},
    {"name": "rc", "type": "str", "reducer": "overwrite", "default": "", "description": "C's report"},
    {"name": "log", "type": "str", "reducer": "append", "default": "", "description": "shared accumulate log"},
]
llm = g.new_node("llm", 400, 0); llm.name = "m"; llm.props.update(LLM)
llm.props["max_retries"] = "0"     # fail fast (no retry/backoff) for the fault-isolation case
dispatch = _agent(g, "dispatch", -160, ["user_input"], [])
A = _agent(g, "A", -80, ["user_input"], ["ra", "log"])
B = _agent(g, "B", 0, ["user_input"], ["rb", "log"])
C = _agent(g, "C", 80, ["user_input"], ["rc", "log"])
tail = _agent(g, "tail", 240, ["ra", "rb", "rc", "log"], [])
for n in (dispatch, A, B, C, tail):
    g.add_edge(llm.id, n.id)
fo = g.new_node("fanout", 0, -120); fo.name = "fo"
jn = g.new_node("join", 0, 160); jn.name = "jn"
g.add_edge(dispatch.id, fo.id)
g.add_edge(fo.id, A.id); g.add_edge(fo.id, B.id); g.add_edge(fo.id, C.id)  # order = branches
g.add_edge(A.id, jn.id); g.add_edge(B.id, jn.id); g.add_edge(C.id, jn.id)
g.add_edge(jn.id, tail.id)

info = graph_codegen.analyze(g)
assert not info["errors"], info["errors"]
assert info["mode"] == "graph", info["mode"]
assert not info.get("warnings"), info["warnings"]      # single-writer fields + append = clean
print("fanout graph ok: analyze clean, graph mode, fanout<->join paired")

out = graph_codegen.generate_from_graph(g, "verify_fanout", gui=False)
py_compile.compile(os.path.join(out, "agent.py"), doraise=True)
spec = importlib.util.spec_from_file_location("vfo", os.path.join(out, "agent.py"))
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)

# emitted topology tables
assert mod.FANOUT.get("fo", {}).get("join") == "jn", mod.FANOUT
assert mod.FANOUT["fo"]["branches"] == ["A", "B", "C"], mod.FANOUT["fo"]["branches"]
assert mod.JOIN.get("jn", {}).get("fanout") == "fo", mod.JOIN
assert mod.STAGE_KINDS["fo"] == "fanout" and mod.STAGE_KINDS["jn"] == "join"
print("fanout codegen ok: FANOUT/JOIN tables + stage kinds emitted")

# the 'vote' join merge = majority / self-consistency (ties -> first branch; matches
# on stripped text). Powers the voting preset when solvers emit a bare label.
assert mod._join_merge(["APPROVE", "REJECT", "APPROVE"], "vote") == "APPROVE"
assert mod._join_merge(["X", "Y"], "vote") == "X"              # 1-1-... tie -> first branch
assert mod._join_merge([" A ", "A"], "vote").strip() == "A"    # majority on stripped text
assert mod._join_merge([], "vote") == ""                       # empty is safe
print("vote merge ok: majority branch output, ties -> first branch")

# run: branches execute CONCURRENTLY; disjoint fields + a SHARED accumulate field
# both merge with no lost updates; the join's successor sees everyone.
import threading
import time
ORDER, SEEN = [], {}
CONC = {"cur": 0, "max": 0}
_clk = threading.Lock()
FIELD = {"A": "ra", "B": "rb", "C": "rc"}
REP = {"A": "repA", "B": "repB", "C": "repC"}       # disjoint per-branch fields
LOG = {"A": "logA", "B": "logB", "C": "logC"}       # ONE shared 'log' field (race test)
def stub(agent_name, cfg, system, messages):
    ORDER.append(agent_name)
    SEEN[agent_name] = system + "\n".join(
        m.get("content", "") for m in messages if isinstance(m.get("content"), str))
    if agent_name in FIELD:
        with _clk:
            CONC["cur"] += 1; CONC["max"] = max(CONC["max"], CONC["cur"])
        time.sleep(0.15)                            # hold to force real overlap
        with _clk:
            CONC["cur"] -= 1
        blk = f'\n\n```state\n{FIELD[agent_name]} = "{REP[agent_name]}"\nlog = "{LOG[agent_name]}"\n```'
        return (f"[{agent_name}]" + blk, [])
    return (f"[{agent_name}] done", [])
mod._call_one = stub
mod.clear_history()
result = mod.run_graph("go", emit=lambda s: None)

assert ORDER[0] == "dispatch" and ORDER[-1] == "tail", ORDER
assert set(ORDER[1:-1]) == {"A", "B", "C"}, ("all branches must run", ORDER)
assert CONC["max"] >= 2, ("branches must run CONCURRENTLY; max overlap=", CONC["max"])
tctx = SEEN["tail"]
for v in ("repA", "repB", "repC"):                  # disjoint fields: all merged
    assert v in tctx, ("missing disjoint-field write " + v)
for v in ("logA", "logB", "logC"):                  # shared accumulate field: NO lost updates
    assert v in tctx, ("LOST UPDATE on the shared 'log' field: " + v)
# built-in 'agents' append-list must record EVERY branch's visit (join must fold each
# branch against a FROZEN base, not a growing alias, or later branches get dropped)
_final = mod._rs().rec.get("state") or {}
_agents = _final.get("agents") or []
assert {"A", "B", "C"}.issubset(set(_agents)), \
    ("built-in 'agents' lost branch visits (join base-diff bug?): " + str(_agents))
assert result.startswith("[tail]"), result
print(f"fanout run ok: branches ran CONCURRENTLY (overlap {CONC['max']}), "
      "disjoint + shared-accumulate state merged with no lost updates")

# fault isolation: one branch failing -> [ERROR] for it, the others + tail complete
ORDER.clear(); SEEN.clear()
def stub_fault(agent_name, cfg, system, messages):
    ORDER.append(agent_name)
    SEEN[agent_name] = system + "\n".join(
        m.get("content", "") for m in messages if isinstance(m.get("content"), str))
    if agent_name == "B":
        raise RuntimeError("boom")
    if agent_name in FIELD:
        return (f'[{agent_name}]\n\n```state\n{FIELD[agent_name]} = "{REP[agent_name]}"\n```', [])
    return (f"[{agent_name}] done", [])
mod._call_one = stub_fault
mod.clear_history()
res_f = mod.run_graph("go", emit=lambda s: None)
assert res_f.startswith("[tail]"), ("run must complete despite a failing branch", res_f)
assert "repA" in SEEN["tail"] and "repC" in SEEN["tail"], "A and C must still merge"
print("fanout fault-isolation ok: a failing branch is isolated; run completes")

# a fan-out-free graph emits EMPTY tables (byte-identity preserved)
g2 = Graph()
l2 = g2.new_node("llm", 0, 0); l2.props.update(LLM)
s2 = g2.new_node("agent", 0, 0); s2.name = "solo"; g2.add_edge(l2.id, s2.id)
o2 = graph_codegen.generate_from_graph(g2, "verify_fanout_none", gui=False)
sp2 = importlib.util.spec_from_file_location("vf2", os.path.join(o2, "agent.py"))
m2 = importlib.util.module_from_spec(sp2); sp2.loader.exec_module(m2)
assert m2.FANOUT == {} and m2.JOIN == {}, (m2.FANOUT, m2.JOIN)
print("no-fanout graph ok: FANOUT/JOIN tables empty (byte-identical when unused)")

# analyze validation
def err_of(build):
    gg = Graph(); build(gg)
    return graph_codegen.analyze(gg)["errors"]

def _mini(gg, branches, reconverge):
    """dispatch -> fanout -> N agents; each agent -> (join or a dead-end end)."""
    gg.state_schema = []
    lm = gg.new_node("llm", 0, 0); lm.props.update(LLM)
    d = _agent(gg, "d", 0, [], []); gg.add_edge(lm.id, d.id)
    f = gg.new_node("fanout", 0, 0); f.name = "f"
    j = gg.new_node("join", 0, 0); j.name = "j"
    gg.add_edge(d.id, f.id)
    ends = []
    for i in range(branches):
        ai = _agent(gg, f"a{i}", i * 40, [], []); gg.add_edge(lm.id, ai.id)
        gg.add_edge(f.id, ai.id)
        if reconverge[i]:
            gg.add_edge(ai.id, j.id)
        else:                                   # send to its own End (no reconverge)
            e = gg.new_node("end", 0, i * 40); e.name = f"e{i}"; gg.add_edge(ai.id, e.id)
            ends.append(e)
    t = _agent(gg, "t", 500, [], []); gg.add_edge(lm.id, t.id); gg.add_edge(j.id, t.id)

# 1 branch -> error (needs >=2)
assert any("at least 2 branches" in e for e in
           err_of(lambda gg: _mini(gg, 1, [True]))), "1-branch fanout must error"
# a branch that doesn't reconverge at the join -> error
assert any("reconverge" in e or "never reaches a join" in e for e in
           err_of(lambda gg: _mini(gg, 2, [True, False]))), "non-reconverging branch must error"
print("fanout analyze ok: <2 branches + non-reconverging branch both rejected")

# ── review-hardening: reject malformed regions that would double-run / pollute ──
def _shared(gg):                # two branches share an intermediate stage
    lm = gg.new_node("llm", 0, 0); lm.props.update(LLM)
    d = _agent(gg, "d", 0, [], []); gg.add_edge(lm.id, d.id)
    f = gg.new_node("fanout", 0, 0); f.name = "f"
    j = gg.new_node("join", 0, 0); j.name = "j"
    b1 = _agent(gg, "b1", 0, [], []); b2 = _agent(gg, "b2", 0, [], [])
    b3 = _agent(gg, "b3", 0, [], []); m = _agent(gg, "m", 0, [], [])
    for a in (b1, b2, b3, m):
        gg.add_edge(lm.id, a.id)
    gg.add_edge(d.id, f.id)
    gg.add_edge(f.id, b1.id); gg.add_edge(f.id, b2.id); gg.add_edge(f.id, b3.id)
    gg.add_edge(b1.id, j.id)
    gg.add_edge(b2.id, m.id); gg.add_edge(b3.id, m.id); gg.add_edge(m.id, j.id)  # shared m
    t = _agent(gg, "t", 0, [], []); gg.add_edge(lm.id, t.id); gg.add_edge(j.id, t.id)

def _direct(gg):                # a direct fanout -> join edge (phantom branch)
    lm = gg.new_node("llm", 0, 0); lm.props.update(LLM)
    d = _agent(gg, "d", 0, [], []); gg.add_edge(lm.id, d.id)
    f = gg.new_node("fanout", 0, 0); f.name = "f"
    j = gg.new_node("join", 0, 0); j.name = "j"
    b1 = _agent(gg, "b1", 0, [], []); gg.add_edge(lm.id, b1.id)
    gg.add_edge(d.id, f.id); gg.add_edge(f.id, b1.id); gg.add_edge(b1.id, j.id)
    gg.add_edge(f.id, j.id)                                   # direct fanout->join
    t = _agent(gg, "t", 0, [], []); gg.add_edge(lm.id, t.id); gg.add_edge(j.id, t.id)

def _stray(gg):                 # a stray edge reaches the join (not a branch tail)
    lm = gg.new_node("llm", 0, 0); lm.props.update(LLM)
    r = _agent(gg, "r", 0, [], []); r.props["role"] = "router"   # a router may branch
    x = _agent(gg, "x", 0, [], [])
    f = gg.new_node("fanout", 0, 0); f.name = "f"
    j = gg.new_node("join", 0, 0); j.name = "j"
    b1 = _agent(gg, "b1", 0, [], []); b2 = _agent(gg, "b2", 0, [], [])
    for a in (r, x, b1, b2):
        gg.add_edge(lm.id, a.id)
    gg.add_edge(r.id, f.id); gg.add_edge(r.id, x.id)
    gg.add_edge(f.id, b1.id); gg.add_edge(f.id, b2.id)
    gg.add_edge(b1.id, j.id); gg.add_edge(b2.id, j.id)
    gg.add_edge(x.id, j.id)                                   # stray edge into the join
    t = _agent(gg, "t", 0, [], []); gg.add_edge(lm.id, t.id); gg.add_edge(j.id, t.id)

assert any("shared by more than one branch" in e for e in err_of(_shared)), \
    "branches sharing a stage must error (would double-run + double-count)"
assert any("straight to the join" in e for e in err_of(_direct)), \
    "a direct fanout->join edge must error"
assert any("reached only by fan-out" in e for e in err_of(_stray)), \
    "a stray edge into the join must error"
print("fanout analyze hardening ok: non-disjoint / phantom-branch / stray-into-join rejected")

# ── Phase 5: Set-State inside a branch + overlapping accumulate (add) merge ──
g5 = Graph()
g5.state_schema = [{"name": "total", "type": "int", "reducer": "add",
                    "default": 0, "description": "sum across branches"}]
l5 = g5.new_node("llm", 400, 0); l5.props.update(LLM); l5.props["max_retries"] = "0"
d5 = _agent(g5, "d", -100, ["user_input"], [])
a1 = _agent(g5, "a1", -40, ["user_input"], [])
b1 = _agent(g5, "b1", 40, ["user_input"], [])
t5 = _agent(g5, "t", 200, ["total"], [])
for n in (d5, a1, b1, t5):
    g5.add_edge(l5.id, n.id)
fo5 = g5.new_node("fanout", 0, -80); fo5.name = "fo"
jn5 = g5.new_node("join", 0, 120); jn5.name = "jn"
sA = g5.new_node("setstate", 0, 0); sA.name = "sA"
sA.props["assignments"] = [{"field": "total", "value": "5"}]   # literal delta; add reducer sums
sB = g5.new_node("setstate", 0, 0); sB.name = "sB"
sB.props["assignments"] = [{"field": "total", "value": "5"}]
g5.add_edge(d5.id, fo5.id)
g5.add_edge(fo5.id, a1.id); g5.add_edge(fo5.id, b1.id)
g5.add_edge(a1.id, sA.id); g5.add_edge(sA.id, jn5.id)      # branch A: a1 -> Set-State -> join
g5.add_edge(b1.id, sB.id); g5.add_edge(sB.id, jn5.id)      # branch B: b1 -> Set-State -> join
g5.add_edge(jn5.id, t5.id)
assert not graph_codegen.analyze(g5)["errors"], graph_codegen.analyze(g5)["errors"]
out5 = graph_codegen.generate_from_graph(g5, "verify_fanout_setstate", gui=False)
sp5 = importlib.util.spec_from_file_location("vf5", os.path.join(out5, "agent.py"))
m5 = importlib.util.module_from_spec(sp5); sp5.loader.exec_module(m5)
m5._call_one = lambda name, cfg, sys_, msgs: (f"[{name}]", [])   # setstate does the writing
m5.clear_history()
m5.run_graph("go", emit=lambda s: None)
_fs = (m5._rs().rec.get("state") or {})
assert _fs.get("total") == 10, ("overlapping 'add' via Set-State in each branch must "
                                "sum to 10, got " + str(_fs.get("total")))
print("fanout phase5 ok: Set-State inside branches + overlapping 'add' merged to 10")

# ≥2 branches writing the SAME overwrite field -> analyze ERROR (nondeterministic)
def _ovwrite(gg):
    gg.state_schema = [{"name": "pick", "type": "str", "reducer": "overwrite",
                        "default": "", "description": "clobbered"}]
    lm = gg.new_node("llm", 0, 0); lm.props.update(LLM)
    d = _agent(gg, "d", 0, [], [])
    a = _agent(gg, "a", 0, ["user_input"], ["pick"])
    b = _agent(gg, "b", 0, ["user_input"], ["pick"])
    for n in (d, a, b):
        gg.add_edge(lm.id, n.id)
    f = gg.new_node("fanout", 0, 0); f.name = "f"
    j = gg.new_node("join", 0, 0); j.name = "j"
    gg.add_edge(d.id, f.id); gg.add_edge(f.id, a.id); gg.add_edge(f.id, b.id)
    gg.add_edge(a.id, j.id); gg.add_edge(b.id, j.id)
    t = _agent(gg, "t", 0, [], []); gg.add_edge(lm.id, t.id); gg.add_edge(j.id, t.id)
assert any("nondeterministic" in e and "overwrite" in e for e in err_of(_ovwrite)), \
    "concurrent branches writing one overwrite field must ERROR"
print("fanout phase5 ok: concurrent overwrite of one field across branches rejected")

print("\nALL FANOUT CHECKS PASSED")
