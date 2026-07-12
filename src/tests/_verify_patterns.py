"""Verify the pattern registry: every preset builds a valid graph, modes are
detected, and the generated code follows the pattern (incl. the supervisor
delegation loop, exercised offline with a stubbed LLM)."""

import importlib.util
import os
import py_compile
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import graph_codegen
import patterns
from graph_model import DEFAULT_BUDGETS

LLM = {"provider": "siliconflow", "model": "deepseek-ai/DeepSeek-V4-Flash",
       "api_key": "sk-test", "base_url": "https://api.siliconflow.cn/v1"}

# 1. Every pattern preset analyzes cleanly with the expected mode + pipeline
EXPECT = {
    "react": ("chain", ["agent"]),
    "planner_executor": ("chain", ["planner", "executor"]),
    "planner_executor_critic": ("chain", ["planner", "executor", "critic"]),
    "supervisor_worker": ("supervisor", ["supervisor", "worker"]),
    "orchestrator": ("autonomous", ["orchestrator", "writer", "reader"]),
}
# The retrieval presets (CRAG / Self-RAG / Adaptive-RAG) are richer than the
# simple (role, links) schema — they carry RAG nodes, capability toggles and
# hand-written prompts, so they're validated separately in section 9 below.
RAG_PRESETS = {"crag", "self_rag", "adaptive_rag"}
BUILDER_PRESETS = {"map_reduce", "human_approval", "voting"}   # builder-based, validated in 1c–1e
assert set(EXPECT) | RAG_PRESETS | BUILDER_PRESETS == set(patterns.PATTERNS), (
    "untested preset(s): "
    + str(set(patterns.PATTERNS) - set(EXPECT) - RAG_PRESETS - BUILDER_PRESETS))
for pid, (mode, names) in EXPECT.items():
    g = patterns.build_pattern_graph(pid, LLM, tool_files=["load_csv.py"])
    info = graph_codegen.analyze(g)
    assert not info["errors"], (pid, info["errors"])
    assert info["mode"] == mode, (pid, info["mode"])
    got = [g.nodes[a].name for a in info["pipeline"]]
    assert got == names, (pid, got)
    # COMPLETE pattern: every agent has exactly one Prompt node (role-matched, not
    # blank), a purposeful per-role LLM temperature, and per-role budget head-room.
    _overrides = patterns.PATTERNS[pid].get("prompts", {})
    for a in g.agents():
        role = a.props.get("role")
        prompts = g.inputs_of(a.id, "prompt")
        assert len(prompts) == 1, (pid, a.name, "want exactly one prompt node")
        p = prompts[0]
        assert p.props.get("role") == role, (pid, a.name, "role mismatch")
        assert (p.props.get("text") or "").strip(), (pid, a.name, "empty prompt text")
        # a preset-supplied prompt overrides the role template; else it IS the template
        if a.name in _overrides:
            assert p.props["text"] == _overrides[a.name], (pid, a.name, "override not applied")
            assert "Describe what this agent" not in p.props["text"], (
                pid, a.name, "still shipping the placeholder scaffold")
        else:
            assert p.props["text"] == graph_codegen.role_template(role)
        # purposeful per-role temperature on the agent's LLM
        llm_n = g.inputs_of(a.id, "llm")[0]
        assert llm_n.props.get("temperature") == patterns.ROLE_TEMPERATURE[role], (
            pid, a.name, llm_n.props.get("temperature"))
        # budgets = defaults + any per-role override
        want_bud = {**DEFAULT_BUDGETS, **patterns._ROLE_BUDGETS.get(role, {})}
        for _k, _v in want_bud.items():
            assert a.props[_k] == _v, (pid, a.name, _k, a.props[_k], "want", _v)
    print(f"pattern ok: {pid} -> mode={mode}, pipeline={got}, prompts+temps+budgets ok")

# 1c. Map-reduce preset (builder-based: coordinator -> fan-out -> 3 workers -> join ->
#     reducer) — richer than the (role, links) schema, so validated on its own.
import py_compile as _pyc
_mr = patterns.build_pattern_graph("map_reduce", LLM, tool_files=["load_csv.py"])
_mri = graph_codegen.analyze(_mr)
assert not _mri["errors"], ("map_reduce", _mri["errors"])
assert _mri["mode"] == "graph", ("map_reduce", _mri["mode"])
assert _mr.nodes[_mri["entry"]].name == "coordinator", "coordinator must be the entry"
_mrk = {}
for _n in _mr.nodes.values():
    _mrk[_n.kind] = _mrk.get(_n.kind, 0) + 1
assert _mrk.get("fanout") == 1 and _mrk.get("join") == 1, _mrk
assert _mrk.get("agent") == 5, ("coordinator + 3 workers + reducer", _mrk)
assert _mrk.get("tool") == 3, ("one tools node per worker", _mrk)
_mro = graph_codegen.generate_from_graph(_mr, "verify_pat_map_reduce", gui=False)
_pyc.compile(os.path.join(_mro, "agent.py"), doraise=True)
print("pattern ok: map_reduce -> graph mode, fan-out/join, per-worker tools, compiles")

# 1d. Human-approval preset (builder-based: intake -> route-mode HITL -> send / reviser
#     / escalate / reject(End), reviser loops back) — a human-driven branch.
_ha = patterns.build_pattern_graph("human_approval", LLM)
_hai = graph_codegen.analyze(_ha)
assert not _hai["errors"], ("human_approval", _hai["errors"])
assert _hai["mode"] == "graph", ("human_approval", _hai["mode"])
assert _ha.nodes[_hai["entry"]].name == "intake", "intake must be the entry"
_hak = {}
for _n in _ha.nodes.values():
    _hak[_n.kind] = _hak.get(_n.kind, 0) + 1
assert _hak.get("hitl") == 1 and _hak.get("end") == 1, _hak
_hao = graph_codegen.generate_from_graph(_ha, "verify_pat_human_approval", gui=False)
_pyc.compile(os.path.join(_hao, "agent.py"), doraise=True)
_spec_ha = importlib.util.spec_from_file_location("vpha", os.path.join(_hao, "agent.py"))
_ham = importlib.util.module_from_spec(_spec_ha)
_spec_ha.loader.exec_module(_ham)
assert _ham.STAGE_KINDS["human_review"] == "hitl", _ham.STAGE_KINDS
assert set(_ham.SUCCESSORS["human_review"]) == {"send", "reviser", "escalate", "reject"}, \
    _ham.SUCCESSORS["human_review"]
assert _ham.HITL_NODES["human_review"]["default_route"] == "escalate", _ham.HITL_NODES
print("pattern ok: human_approval -> route-mode HITL (4 branches, End, revise loop), compiles")

# 1e. Voting / self-consistency preset (framer -> fan-out -> N identical solvers ->
#     join -> judge). Builder-based; the solvers are identical (diverse sampling).
_vt = patterns.build_pattern_graph("voting", LLM)
_vti = graph_codegen.analyze(_vt)
assert not _vti["errors"], ("voting", _vti["errors"])
assert _vti["mode"] == "graph", ("voting", _vti["mode"])
assert _vt.nodes[_vti["entry"]].name == "framer", "framer must be the entry"
_vtk = {}
for _n in _vt.nodes.values():
    _vtk[_n.kind] = _vtk.get(_n.kind, 0) + 1
assert _vtk.get("fanout") == 1 and _vtk.get("join") == 1, _vtk
assert _vtk.get("agent") == 5, ("framer + 3 solvers + judge", _vtk)   # 3 solvers
_vto = graph_codegen.generate_from_graph(_vt, "verify_pat_voting", gui=False)
_pyc.compile(os.path.join(_vto, "agent.py"), doraise=True)
print("pattern ok: voting -> fan-out N solvers -> judge, graph mode, compiles")

# 1b. The Prompt node (not just the role fallback) drives the generated system
# prompt: a sentinel edited into a preset's prompt node must reach the output.
g = patterns.build_pattern_graph("react", LLM)
pnode = next(n for n in g.nodes.values() if n.kind == "prompt")
pnode.props["text"] = "SENTINEL-PERSONA-XYZ: you analyze CSV files."
out_dir = graph_codegen.generate_from_graph(g, "demo_prompt_node", gui=False)
spec_pn = importlib.util.spec_from_file_location(
    "demo_prompt_node_agent", os.path.join(out_dir, "agent.py"))
mod_pn = importlib.util.module_from_spec(spec_pn)
spec_pn.loader.exec_module(mod_pn)
entry = mod_pn.PIPELINE[0]
assert "SENTINEL-PERSONA-XYZ" in mod_pn.AGENTS[entry]["system"], mod_pn.AGENTS[entry]["system"][:200]
print("prompt-node drives generated system prompt ok")

# 2. Revise edge exists only where the pattern has a critic loop
g = patterns.build_pattern_graph("planner_executor_critic", LLM)
assert graph_codegen.analyze(g)["revise_edge"] is not None
g = patterns.build_pattern_graph("planner_executor", LLM)
assert graph_codegen.analyze(g)["revise_edge"] is None
print("revise edges ok")

# 3. Supervisor validation: workers must not have outgoing agent links
g = patterns.build_pattern_graph("supervisor_worker", LLM)
wrk = next(n for n in g.agents() if n.props["role"] == "worker")
extra = g.new_node("agent", 0, 0)
extra.name = "extra"
llm_x = g.new_node("llm", 0, 0)
llm_x.props.update(LLM)
assert g.add_edge(llm_x.id, extra.id) is None
assert g.add_edge(wrk.id, extra.id) is None
errs = graph_codegen.analyze(g)["errors"]
assert any("must not have outgoing" in e for e in errs), errs
print("supervisor validation ok")

# 4. Generate the supervisor agent; run the delegation loop offline
g = patterns.build_pattern_graph("supervisor_worker", LLM,
                                 tool_files=["load_csv.py"])
out_dir = graph_codegen.generate_from_graph(g, "demo_supervisor", gui=True)
py_compile.compile(os.path.join(out_dir, "agent.py"), doraise=True)
py_compile.compile(os.path.join(out_dir, "gui.py"), doraise=True)
spec = importlib.util.spec_from_file_location(
    "demo_supervisor_agent", os.path.join(out_dir, "agent.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
assert mod.PATTERN_MODE == "supervisor"
assert mod.PIPELINE == ["supervisor", "worker"]
assert "NEXT" in mod.AGENTS["supervisor"]["system"]   # supervisor template
assert mod.AGENTS["worker"]["tools"] == ["load_csv"]

seq = {"n": 0}
def fake(agent_name, cfg, system, messages):
    seq["n"] += 1
    if seq["n"] == 1:
        assert agent_name == "supervisor"
        return ("NEXT: compute the answer", [])
    if seq["n"] == 2:
        assert agent_name == "worker"
        assert "compute the answer" in messages[0]["content"]
        return ("the answer is 42", [])
    assert agent_name == "supervisor"
    assert "the answer is 42" in messages[0]["content"]
    return ("DONE: 42", [])
mod._call_one = fake
mod.clear_history()
traces = []
result = mod.run("what is the answer?", emit=traces.append)
assert result == "42", result
assert seq["n"] == 3, seq
assert any("Supervisor round 1 done" in t for t in traces), traces
mod.clear_history()
print("supervisor runtime ok: NEXT -> worker -> DONE -> '42'")

# 5. Planner–Executor via the same path (the form designer's route)
out2 = graph_codegen.generate_from_graph(
    patterns.build_pattern_graph("planner_executor", LLM,
                                 tool_files=["load_csv.py"]),
    "demo_pe", gui=False)
py_compile.compile(os.path.join(out2, "agent.py"), doraise=True)
spec2 = importlib.util.spec_from_file_location(
    "demo_pe_agent", os.path.join(out2, "agent.py"))
mod2 = importlib.util.module_from_spec(spec2)
spec2.loader.exec_module(mod2)
assert mod2.PATTERN_MODE == "chain"
assert mod2.PIPELINE == ["planner", "executor"]
assert mod2.AGENTS["executor"]["tools"] == ["load_csv"]
assert mod2.AGENTS["planner"]["tools"] == []
assert not os.path.exists(os.path.join(out2, "gui.py"))
print("planner-executor ok: chain mode, tools on the executor only")

# 6. Eval harness: generated run_evals.py + agent.run_evals() grade the agent
#    over the evalset (no Eval node here → falls back to evals/evalset.example).
assert os.path.isfile(os.path.join(out2, "run_evals.py"))
assert os.path.isfile(os.path.join(out2, "evals", "evalset.example.jsonl"))
spec_ev = importlib.util.spec_from_file_location(
    "demo_pe_run_evals", os.path.join(out2, "run_evals.py"))
run_evals = importlib.util.module_from_spec(spec_ev)
spec_ev.loader.exec_module(run_evals)

def _score():
    results = run_evals.agent.run_evals(emit=lambda s: None)
    p = sum(r["passed"] for r in results)
    t = sum(r["total"] for r in results)
    return p, t, (p / t if t else 0.0)

def _router(agent_name, cfg, system, messages):
    content = messages[0]["content"]
    if "2 + 3" in content:
        return "5", []
    if "no_such_file" in content:
        return "That file was not found in the workspace.", []
    if "JSON object" in content:                      # json grader case
        return '{"name": "Ada", "age": 36}', []
    return "hello world", []
run_evals.agent._call_one = _router
passed, total, score = _score()
# the example evalset exercises every grader family (contains/numeric/regex/
# is_json+json_has_keys/not_contains); the stub answers each correctly.
assert (passed, total, score) == (5, 5, 1.0), (passed, total, score)
print(f"eval harness ok: {passed}/{total} = {score:.2f}")

# grading also fails correctly: a wrong answer per case (gibberish fails
# contains/numeric/regex/json; the safety case fails only if the text DOES say
# "i cannot", so answer that there specifically).
def _wrong(agent_name, cfg, system, messages):
    if "Summarize" in messages[0]["content"]:
        return "i cannot comply", []
    return "banana", []
run_evals.agent._call_one = _wrong
passed, total, score = _score()
assert passed == 0 and total == 5, (passed, total)
print("eval harness ok: failing agent scores 0")

# 7. Runtime pattern-switch dispatcher: /mode routes to the right runner and
#    swaps that mode's topology globals around it (proves the mechanism that a
#    multi-pattern app uses to switch PEC/ReAct/Supervisor at runtime).
dm = run_evals.agent
_orig_pipeline = list(dm.PIPELINE)
hits = []
dm.run_pipeline = lambda q, e=print, entry=None: (hits.append(("pipeline", list(dm.PIPELINE))) or "P")
dm.run_supervisor = lambda q, e=print: (hits.append(("supervisor", list(dm.PIPELINE))) or "S")
dm.run_graph = lambda q, e=print, thread_id=None, entry=None: (hits.append(("graph", dm.ENTRY)) or "G")
dm.MODES = {
    "chain": {"runner": "pipeline", "globals": {"PIPELINE": ["planner", "executor"]}},
    "boss":  {"runner": "supervisor", "globals": {"PIPELINE": ["sup", "w1"]}},
    "flow":  {"runner": "graph", "globals": {"ENTRY": "g0"}},
}
dm.DEFAULT_LABEL = "chain"
dm.ACTIVE_MODE = None
dm.clear_history()
dm.run("hi", emit=lambda s: None)
assert hits[-1] == ("pipeline", ["planner", "executor"]), hits
dm.run("/mode boss delegate the work", emit=lambda s: None)
assert hits[-1] == ("supervisor", ["sup", "w1"]), hits      # mode globals swapped in
assert dm.PIPELINE == _orig_pipeline, "topology globals restored after dispatch"
dm.run("/mode flow walk it", emit=lambda s: None)
assert hits[-1] == ("graph", "g0"), hits
dm.run("again", emit=lambda s: None)                        # no token -> sticky
assert hits[-1][0] == "graph", "ACTIVE_MODE sticky across turns"
_n = len(hits)
out = dm.run("/mode", emit=lambda s: None)                  # bare /mode: report only
assert len(hits) == _n and "available" in out, out
dm.run("/mode bogus do x", emit=lambda s: None)             # unknown keeps current
assert hits[-1][0] == "graph", "unknown mode keeps current mode"
dm.MODES = {}; dm.ACTIVE_MODE = None
print("pattern-switch dispatcher ok: /mode routes + swaps globals + sticky + reports")

# 8. Multi-pattern GENERATION: tag 3 sub-pipelines (react/pec/supervisor) in one
#    graph -> one app whose end-user switches the runner via /mode. Proves the
#    codegen per-component compile end-to-end.
import patterns as _pat
from graph_model import Graph as _G
_LLM = {"provider": "siliconflow", "model": "deepseek-ai/DeepSeek-V4-Flash",
        "api_key": "sk-x", "base_url": "https://api.siliconflow.cn/v1"}
_comb = _G()
def _merge(sub):
    idm = {}
    for nid, node in sub.nodes.items():
        nn = _comb.new_node(node.kind, node.x, node.y); nn.name = node.name
        nn.props = dict(node.props); idm[nid] = nn.id
    for e in sub.edges:
        assert _comb.add_edge(idm[e.src], idm[e.dst]) is None
for _pid in ("react", "planner_executor_critic", "supervisor_worker"):
    _merge(_pat.build_pattern_graph(_pid, _LLM))
_bn = {n.name: n for n in _comb.nodes.values()}
_bn["agent"].props["mode_label"] = "react"
_bn["planner"].props["mode_label"] = "pec"
_bn["supervisor"].props["mode_label"] = "supervisor"
_out = graph_codegen.generate_from_graph(_comb, "demo_multipattern", gui=False)
_spec = importlib.util.spec_from_file_location("demo_mp", os.path.join(_out, "agent.py"))
_mp = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_mp)
assert set(_mp.MODES) == {"react", "pec", "supervisor"}, _mp.MODES
assert _mp.DEFAULT_LABEL == "react"
assert _mp.MODES["pec"]["runner"] == "pipeline"
assert _mp.MODES["pec"]["globals"]["REVISE_EDGE"] == ["critic", "planner"]
assert _mp.MODES["supervisor"]["runner"] == "supervisor"
assert set(_mp.AGENTS) == {"agent", "planner", "executor", "critic",
                           "supervisor", "worker"}, sorted(_mp.AGENTS)
_seen = []
def _mpstub(an, cfg, sysm, msgs):
    _seen.append(an)
    return ("DONE: ok", []) if an == "supervisor" else (f"[{an}]", [])
_mp._call_one = _mpstub
def _mprun(t):
    _seen.clear(); _mp.clear_history(); return _mp.run(t, emit=lambda s: None)
_mprun("/mode react hi"); assert _seen == ["agent"], _seen
_mprun("/mode pec go"); assert _seen == ["planner", "executor", "critic"], _seen
_res = _mprun("/mode supervisor ship")
assert "supervisor" in _seen and _res == "ok", (_seen, _res)   # real supervisor DONE-parse
print("multi-pattern app ok: /mode switches react/pec/supervisor to real runners")

# 9. Retrieval presets (CRAG / Self-RAG / Adaptive-RAG). Each is a ready-made
#    multi-node RAG graph: carefully-written prompts + capability toggles already
#    switched on. The ONLY thing left for the user is the docs folder — so a fresh
#    preset (empty docs_dir) must flag exactly that, and once a folder is set it
#    must analyze cleanly (expected mode), generate and compile.
import tempfile as _tf
_docs = _tf.mkdtemp(prefix="preset_docs_")
with open(os.path.join(_docs, "policy.txt"), "w", encoding="utf-8") as _f:
    _f.write("Refunds are processed within 14 days of the request.\n")

# 9a. A fresh preset points the user at the one thing to configure: the folder.
for _pid in RAG_PRESETS:
    _g = patterns.build_pattern_graph(_pid, LLM)
    _errs = graph_codegen.analyze(_g)["errors"]
    assert _errs and all("no docs folder" in e for e in _errs), (_pid, _errs)

# 9b. Expected topology, activating props, custom prompts, mode, and codegen.
# Every RAG node also carries the shared retrieval-quality config (recursive
# chunking + overlap + hybrid retrieval) — locked here so it can't silently
# revert to the weak bm25/fixed-cut defaults these presets exist to avoid.
_RQ = {"chunk_strategy": "recursive", "chunk_overlap": 120,
       "retrieval_algorithm": "hybrid"}
RAG_EXPECT = {
    "crag": {
        "mode": "chain",
        "agent_props": {"researcher": {"role": "single", "web_search": True}},
        "rag_props": {"knowledge": {"grade_docs": True, "corrective": True,
                                    "corrective_max_rewrites": 2, **_RQ}},
        "llm_temp": {"researcher": "0.2"},
        "prompt_snip": "corrective retrieval-augmented workflow",
    },
    "self_rag": {
        "mode": "chain",
        "agent_props": {"self_rag": {"role": "single", "groundedness_check": True,
                                     "max_regen": 2}},
        "rag_props": {"knowledge": {"grade_docs": True, **_RQ}},
        "llm_temp": {"self_rag": "0.2"},
        "prompt_snip": "self-reflective research assistant",
    },
    "adaptive_rag": {
        "mode": "graph",
        "agent_props": {
            "router": {"role": "planner", "route_self": True, "quick_response": True},
            "web_research": {"web_search": True},
            "knowledge_base": {"role": "single"},
        },
        "rag_props": {"documents": {"grade_docs": True, **_RQ}},
        "llm_temp": {"router": "0", "knowledge_base": "0.2", "web_research": "0.2"},
        "prompt_snip": "query router for an adaptive retrieval system",
    },
}
for _pid, _exp in RAG_EXPECT.items():
    _g = patterns.build_pattern_graph(_pid, LLM)
    _by_name = {n.name: n for n in _g.nodes.values()}
    # activating props landed on the right nodes
    for _name, _want in _exp["agent_props"].items():
        _p = _by_name[_name].props
        for _k, _v in _want.items():
            assert _p.get(_k) == _v, (_pid, _name, _k, _p.get(_k), "want", _v)
    for _name, _want in _exp["rag_props"].items():
        _p = _by_name[_name].props
        for _k, _v in _want.items():
            assert _p.get(_k) == _v, (_pid, _name, _k, _p.get(_k), "want", _v)
    # every agent gets the tuned budgets (head-room for retry/regen/web loops)
    for _a in _g.agents():
        assert _a.props["max_iterations"] == 12 and _a.props["max_wall_clock_s"] == 120, (
            _pid, _a.name, "budgets not tuned")
    # each agent's LLM node carries its purposeful sampling temperature
    for _name, _t in _exp["llm_temp"].items():
        _lp = _by_name["llm_" + _name].props
        assert _lp.get("temperature") == _t, (_pid, _name, _lp.get("temperature"), "want", _t)
    # custom (non-role-template) prompt text is on every agent's prompt node
    for _a in _g.agents():
        _pr = _g.inputs_of(_a.id, "prompt")
        assert len(_pr) == 1 and (_pr[0].props.get("text") or "").strip(), (_pid, _a.name)
        assert _pr[0].props["text"] != graph_codegen.role_template(_a.props["role"]), (
            _pid, _a.name, "prompt should be custom, not the role template")
    # point every RAG node at the docs folder, then analyze + generate + compile
    for _n in _g.nodes.values():
        if _n.kind == "rag":
            _n.props["docs_dir"] = _docs
    _info = graph_codegen.analyze(_g)
    assert not _info["errors"], (_pid, _info["errors"])
    assert _info["mode"] == _exp["mode"], (_pid, _info["mode"], "want", _exp["mode"])
    _out = graph_codegen.generate_from_graph(_g, "demo_" + _pid, gui=False)
    py_compile.compile(os.path.join(_out, "agent.py"), doraise=True)
    _src = open(os.path.join(_out, "agent.py"), encoding="utf-8").read()
    assert _exp["prompt_snip"] in _src, (_pid, "prompt not in generated source")
    print(f"rag preset ok: {_pid} -> mode={_info['mode']}, toggles+prompts wired, compiles")

print("\nALL PATTERN CHECKS PASSED")
