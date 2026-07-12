"""Offline verification of the canvas layer: graph model, validation,
multi-agent codegen, and the generated pipeline's mechanics (no API calls)."""

import importlib.util
import os
import py_compile
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

for fname in ["graph_model.py", "graph_codegen.py",
              os.path.join("canvas_qt", "designer.py"),
              os.path.join("canvas_qt", "dialogs.py")]:
    py_compile.compile(os.path.join(BASE, fname), doraise=True)
    print(f"compile ok: {fname}")

import graph_codegen
from graph_model import Graph

# 1. Build planner → worker → critic with a revise back-edge
g = Graph()
planner = g.new_node("agent", 0, 0); planner.name = "planner"; planner.props["role"] = "planner"
worker = g.new_node("agent", 0, 0); worker.name = "worker"; worker.props["role"] = "worker"
critic = g.new_node("agent", 0, 0); critic.name = "critic"; critic.props["role"] = "critic"

for agent in (planner, worker, critic):
    llm = g.new_node("llm", 0, 0)
    llm.props.update(api_key="sk-test", model="deepseek-ai/DeepSeek-V4-Flash")
    assert g.add_edge(llm.id, agent.id) is None

# second LLM on the planner = fallback (must be allowed now)
fallback = g.new_node("llm", 0, 0)
fallback.name = "llm_fallback"
fallback.props.update(api_key="sk-test2", model="deepseek-ai/DeepSeek-V3")
assert g.add_edge(fallback.id, planner.id) is None, "second LLM link must be allowed"

tool = g.new_node("tool", 0, 0); tool.props["files"] = ["load_csv.py"]
assert g.add_edge(tool.id, worker.id) is None
prompt = g.new_node("prompt", 0, 0)
prompt.props["role"] = "planner"
prompt.props["text"] = "You plan data-analysis work."
assert g.add_edge(prompt.id, planner.id) is None

assert g.add_edge(planner.id, worker.id) is None
assert g.add_edge(worker.id, critic.id) is None
assert g.add_edge(critic.id, planner.id) is None  # revise loop

# 2. Edge rules
assert g.add_edge(tool.id, llm.id) is not None, "tool->llm must be rejected"
assert g.add_edge(prompt.id, planner.id) is not None, "second prompt must be rejected"
print("edge rules ok")

# 3. Save / load round-trip
g.save("graphs_test.json")
g2 = Graph.load("graphs_test.json")
assert len(g2.nodes) == len(g.nodes) and len(g2.edges) == len(g.edges)
os.remove("graphs_test.json")
print("save/load ok")

# 4. Analysis
info = graph_codegen.analyze(g)
assert not info["errors"], info["errors"]
names = [g.nodes[a].name for a in info["pipeline"]]
print("pipeline:", names, "| revise edge:", info["revise_edge"] is not None)
assert names == ["planner", "worker", "critic"]

# 5. Validation catches a broken graph (agent without LLM)
bad = Graph()
bad.new_node("agent", 0, 0)
errs = graph_codegen.analyze(bad)["errors"]
assert any("LLM" in e for e in errs), errs
print("validation ok:", errs[0])

# 5b. Role templates exist; prompt-role/agent-role mismatch is rejected
assert "{agent_name}" in graph_codegen.role_template("planner")
assert "the worker" in graph_codegen.role_template("worker")
assert "REVISE" in graph_codegen.role_template("critic")
prompt.props["role"] = "critic"  # wrong: linked to the planner agent
errs2 = graph_codegen.analyze(g)["errors"]
assert any("Prompt" in e and "role" in e for e in errs2), errs2
prompt.props["role"] = "planner"
assert not graph_codegen.analyze(g)["errors"]
print("prompt roles ok: templates load, mismatch rejected")

# 6. Generate (with GUI) and compile-check the pipeline agent
out_dir = graph_codegen.generate_from_graph(g, "demo_pipeline", gui=True)
print("generated:", out_dir)
agent_path = os.path.join(out_dir, "agent.py")
py_compile.compile(agent_path, doraise=True)
print("compile ok: generated agent.py")
gui_path = os.path.join(out_dir, "gui.py")
assert os.path.exists(gui_path), "gui.py was not generated"
py_compile.compile(gui_path, doraise=True)
print("compile ok: generated gui.py")
reqs = open(os.path.join(out_dir, "requirements.txt")).read()
assert "PySide6" in reqs, reqs
bat = open(os.path.join(out_dir, "build.bat")).read()
assert "--windowed" in bat and "gui.py" in bat, bat
print("requirements + build.bat carry the GUI")

spec = importlib.util.spec_from_file_location("demo_pipeline_agent", agent_path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
print("import ok; agents:", mod.PIPELINE, "| revise:", mod.REVISE_EDGE)
assert list(mod.AGENTS) == ["planner", "worker", "critic"]
assert mod.AGENTS["worker"]["tools"] == ["load_csv"]
assert mod.AGENTS["planner"]["tools"] == []
assert "REVISE" in mod.AGENTS["critic"]["system"]
print("critic prompt carries the REVISE convention")
# no prompt node on worker → its role template, {agent_name} filled in
assert "You are worker, the worker." in mod.AGENTS["worker"]["system"]
# explicit prompt node on planner → its text wins
assert mod.AGENTS["planner"]["system"].startswith("You plan data-analysis work.")
print("role-template fallback ok")

# 6b. Multi-LLM: config carries the fallback chain in link order
import json as _json
with open(os.path.join(out_dir, "config.json"), encoding="utf-8") as f:
    gen_cfg = _json.load(f)
assert len(gen_cfg["llms"]["planner"]) == 2, gen_cfg["llms"]["planner"]
assert gen_cfg["llms"]["planner"][0]["model"] == "deepseek-ai/DeepSeek-V4-Flash"
assert gen_cfg["llms"]["planner"][1]["model"] == "deepseek-ai/DeepSeek-V3"
assert len(gen_cfg["llms"]["worker"]) == 1
print("config fallback chain ok:",
      [c["model"] for c in gen_cfg["llms"]["planner"]])

# 6c. Failover mechanics: primary fails -> fallback answers
calls = []
def _fake_call(agent_name, cfg, system, messages):
    calls.append(cfg["model"])
    if len(calls) == 1:
        raise RuntimeError("simulated outage")
    return "ok", []
mod._call_one = _fake_call
notes = []
result = mod.llm("planner", "sys", [], emit=notes.append)
assert result == ("ok", []), result
assert calls == ["deepseek-ai/DeepSeek-V4-Flash", "deepseek-ai/DeepSeek-V3"], calls
assert any("failover" in n for n in notes), notes
print("failover ok:", notes[0])

# 6d. LLM switching: selected LLM is tried first; choice persists to disk
assert mod.get_llm_options("planner") == [
    "deepseek-ai/DeepSeek-V4-Flash", "deepseek-ai/DeepSeek-V3"]
assert mod.get_llm_choice("planner") == 0
mod.set_llm_choice("planner", 1)
calls2 = []
def _fake_ok(agent_name, cfg, system, messages):
    calls2.append(cfg["model"])
    return "ok", []
mod._call_one = _fake_ok
mod.llm("planner", "sys", [], emit=lambda s: None)
assert calls2 == ["deepseek-ai/DeepSeek-V3"], calls2
choice_path = os.path.join(out_dir, "llm_choice.json")
assert os.path.exists(choice_path)
with open(choice_path, encoding="utf-8") as f:
    assert _json.load(f)["planner"] == 1
mod.set_llm_choice("planner", 0)  # reset
print("llm switching ok: selected model is tried first, choice persisted")

# 6e. Native function-calling loop: stubbed LLM requests a tool call; the
#     real tool executes; its result returns as a tool message; loop ends.
_fc_rounds = []
def _fake_fc(agent_name, cfg, system, messages):
    if not _fc_rounds:
        _fc_rounds.append(1)
        return "", [{"id": "call_1", "name": "load_csv",
                     "args": {"path": os.path.join("..", "react_agent",
                                                   "sample_data.csv"),
                              "max_rows": 2}}]
    assert messages[-1]["role"] == "tool", messages[-1]
    assert "Columns" in messages[-1]["content"], messages[-1]["content"][:120]
    return "done: 8 rows, 4 columns", []
mod._call_one = _fake_fc
out_text = mod.react("worker", "load the csv", emit=lambda s: None)
assert out_text == "done: 8 rows, 4 columns", out_text
print("native FC loop ok: tool executed, result fed back, loop terminated")

# 6f. Retry with backoff: transient errors retried on the SAME model first
mod.RETRY_BASE_S = 0  # no real sleeping in tests
attempts = []
def _flaky(agent_name, cfg, system, messages):
    attempts.append(cfg["model"])
    if len(attempts) < 3:
        raise TimeoutError("transient blip")
    return "ok", []
mod._call_one = _flaky
notes2 = []
out = mod.llm("worker", "sys", [], emit=notes2.append)
assert out == ("ok", []), out
assert len(attempts) == 3 and len(set(attempts)) == 1, attempts
assert sum("[retry]" in n for n in notes2) == 2, notes2
print("retry ok:", notes2[0])

# non-retryable error -> no retry, straight to failover/raise
attempts2 = []
def _fatal(agent_name, cfg, system, messages):
    attempts2.append(1)
    raise ValueError("bad request")
mod._call_one = _fatal
try:
    mod.llm("worker", "sys", [], emit=lambda s: None)
    raise AssertionError("should have raised")
except RuntimeError as e:
    assert "bad request" in str(e)
assert len(attempts2) == 1, attempts2
print("non-retryable ok: single attempt, immediate raise")

# 6g. HITL: high-risk tool calls require confirmation; denial feeds back
assert mod.is_high_risk("save_file") and mod.is_high_risk("delete_rows")
assert not mod.is_high_risk("load_csv")  # read-only name, not risky
mod.CONFIG["hitl_confirm"] = True
mod.CONFIG["high_risk_tools"] = ["load_csv"]  # opt this tool in for the test
assert mod.is_high_risk("load_csv")

prompts_seen = []
mod.set_confirm_handler(lambda p: prompts_seen.append(p) or False)  # deny
hitl_rounds = []
def _hitl_stub(agent_name, cfg, system, messages):
    if not hitl_rounds:
        hitl_rounds.append(1)
        return "", [{"id": "h1", "name": "load_csv",
                     "args": {"path": "x.csv"}}]
    assert "[denied]" in messages[-1]["content"], messages[-1]
    return "understood, skipping the file", []
mod._call_one = _hitl_stub
out = mod.react("worker", "load x.csv", emit=lambda s: None)
assert out == "understood, skipping the file", out
assert prompts_seen and "load_csv" in prompts_seen[0]
print("hitl deny ok:", prompts_seen[0][:60], "...")

mod.set_confirm_handler(lambda p: True)  # allow -> tool actually runs
hitl_rounds2 = []
def _hitl_stub2(agent_name, cfg, system, messages):
    if not hitl_rounds2:
        hitl_rounds2.append(1)
        return "", [{"id": "h2", "name": "load_csv",
                     "args": {"path": os.path.join("..", "react_agent",
                                                   "sample_data.csv")}}]
    assert "Columns" in messages[-1]["content"], messages[-1]
    return "done", []
mod._call_one = _hitl_stub2
assert mod.react("worker", "load it", emit=lambda s: None) == "done"
mod.CONFIG["high_risk_tools"] = []  # reset
print("hitl allow ok: tool executed after confirmation")

# schema sanity on the pipeline module too
schema = mod.tool_schema("load_csv")
assert schema["parameters"]["properties"]["max_rows"]["type"] == "integer"
assert schema["parameters"]["required"] == ["path"]
print("tool schema ok:", schema["name"])

# 7. Pipeline mechanics offline: zero wall-clock budgets → every react()
#    returns a budget message without any API call, but the full pipeline
#    sequencing still executes end to end.
for spec_a in mod.AGENTS.values():
    spec_a["budgets"]["max_wall_clock_s"] = -1
mod.clear_history()
result = mod.run("test task", emit=lambda s: None)
assert result.startswith("[budget]"), result
print("pipeline mechanics ok:", result)

# 7a. Trace: the run wrote a structured JSONL trace with a consistent trace_id
import glob as _glob
trace_files = sorted(_glob.glob(os.path.join(out_dir, "traces", "*.jsonl")))
assert trace_files, "no trace file written"
with open(trace_files[-1], encoding="utf-8") as f:
    records = [_json.loads(line) for line in f]
kinds = [r["kind"] for r in records]
assert kinds[0] == "run_start" and kinds[-1] == "run_end", kinds
assert records[0]["task"] == "test task"
assert len({r["trace_id"] for r in records}) == 1
assert "usage" in records[-1] and "cost_usd" not in records[-1]  # cost removed
print("trace ok:", os.path.basename(trace_files[-1]), "kinds:", kinds)

# 7b. History: exchange recorded, context flows into the next run, clearable.
#     A budget-capped / cancelled / errored run is intentionally NOT persisted
#     (run() only stores a completed exchange), so drive a normal completing run
#     here with a restored budget and a stubbed LLM.
for spec_a in mod.AGENTS.values():
    spec_a["budgets"]["max_wall_clock_s"] = 60
mod._call_one = lambda agent_name, cfg, system, messages: ("final answer", [])
mod.clear_history()
mod.run("test task", emit=lambda s: None)
assert len(mod.HISTORY) == 2 and mod.HISTORY[0]["content"] == "test task"
assert any(f.endswith(".json")
           for f in os.listdir(os.path.join(out_dir, "sessions")))   # session persisted
ctx = mod.history_context()
assert "Conversation so far" in ctx and "test task" in ctx
mod.run("follow-up", emit=lambda s: None)
assert len(mod.HISTORY) == 4
mod.clear_history()
assert mod.HISTORY == [] and mod.history_context() == ""
print("history ok: stored, context built, cleared")

# 8. Regression: a graph WITHOUT a revise loop (single agent) must generate
#    valid Python — json.dumps(None) once emitted `null` here.
g3 = Graph()
solo = g3.new_node("agent", 0, 0)
solo.name = "solo"
llm3 = g3.new_node("llm", 0, 0)
llm3.props.update(api_key="sk-test", model="deepseek-ai/DeepSeek-V4-Flash")
assert g3.add_edge(llm3.id, solo.id) is None
out3 = graph_codegen.generate_from_graph(g3, "solo_no_loop", gui=False)
py_compile.compile(os.path.join(out3, "agent.py"), doraise=True)
spec3 = importlib.util.spec_from_file_location(
    "solo_agent", os.path.join(out3, "agent.py"))
mod3 = importlib.util.module_from_spec(spec3)
spec3.loader.exec_module(mod3)
assert mod3.REVISE_EDGE is None, mod3.REVISE_EDGE
assert mod3.PIPELINE == ["solo"]
assert "You are solo" in mod3.AGENTS["solo"]["system"]  # single-role template
mod3.AGENTS["solo"]["budgets"]["max_wall_clock_s"] = -1
mod3.clear_history()
assert mod3.run("x", emit=lambda s: None).startswith("[budget]")
mod3.clear_history()
print("no-revise-edge regression ok: REVISE_EDGE is None")

# 9. Workspace: set folders, check prompt context and relative-path resolution
ws_dir = os.path.abspath(os.path.join("..", "react_agent"))
mod3.set_workspace([ws_dir])
assert mod3.get_workspace() == [ws_dir]
ctx = mod3.workspace_context()
assert "# Workspace" in ctx and "sample_data.csv" in ctx, ctx[:300]
resolved = mod3.resolve_workspace_path("sample_data.csv")
assert resolved == os.path.join(ws_dir, "sample_data.csv"), resolved
assert mod3.resolve_workspace_path("no_such_file.xyz") == "no_such_file.xyz"
sys_prompt = mod3.build_system("solo")
assert "# Workspace" in sys_prompt
extra = os.path.abspath(os.path.join("..", "cpp_examples"))
folders = mod3.add_workspace_folder(extra)
assert folders == [ws_dir, extra]
mod3.set_workspace([])  # clean up workspace.json content
assert mod3.workspace_context() == ""
print("workspace ok: context, resolution, add-folder")

# 11. Duplicate-link detection (same content, different nodes)
g5 = Graph()
ag = g5.new_node("agent", 0, 0); ag.name = "dup_agent"
llm_a = g5.new_node("llm", 0, 0)
llm_a.props.update(api_key="k1", model="deepseek-ai/DeepSeek-V4-Flash")
assert g5.add_edge(llm_a.id, ag.id) is None

# 11a. same tool file across two Tools nodes -> blocked at link time
t1 = g5.new_node("tool", 0, 0); t1.props["files"] = ["load_csv.py"]
t2 = g5.new_node("tool", 0, 0); t2.props["files"] = ["load_csv.py"]
assert g5.add_edge(t1.id, ag.id) is None
err_dup = g5.add_edge(t2.id, ag.id)
assert err_dup and "already has tool file" in err_dup, err_dup
print("dup tool blocked at link time:", err_dup[:60], "...")

# 11b. duplicate file configured AFTER linking -> generation error
t3 = g5.new_node("tool", 0, 0)              # empty: link is allowed
assert g5.add_edge(t3.id, ag.id) is None
t3.props["files"] = ["load_csv.py"]         # now it's a duplicate
errs5 = graph_codegen.analyze(g5)["errors"]
assert any("more than once" in e for e in errs5), errs5
g5.remove_node(t3.id)
print("dup tool caught at generation:", [e for e in errs5 if "once" in e][0][:60], "...")

# 11c. identical LLM twice -> link-time warning + generation warning
llm_b = g5.new_node("llm", 0, 0)
llm_b.props.update(api_key="k2", model="deepseek-ai/DeepSeek-V4-Flash")
assert g5.add_edge(llm_b.id, ag.id) is None      # allowed...
warn = g5.link_warning(llm_b.id, ag.id)
assert warn and "identical LLMs" in warn, warn   # ...but warned
info5 = graph_codegen.analyze(g5)
assert any("more than once" in w for w in info5["warnings"]), info5["warnings"]
assert not any("LLM" in e and "once" in e for e in info5["errors"])
print("dup llm warned:", warn[:60], "...")

# 11d. identical skill text across two Skills nodes -> warning
s1 = g5.new_node("skill", 0, 0)
s1.props["skills"] = [{"name": "cite", "text": "Always cite sources."}]
s2 = g5.new_node("skill", 0, 0)
s2.props["skills"] = [{"name": "cite", "text": "Always cite sources."}]
assert g5.add_edge(s1.id, ag.id) is None
assert g5.add_edge(s2.id, ag.id) is None
warn_s = g5.link_warning(s2.id, ag.id)
assert warn_s and "identical text" in warn_s, warn_s
assert any("identical text" in w for w in graph_codegen.analyze(g5)["warnings"])
print("dup skill warned:", warn_s[:60], "...")

# 11e. distinct models on one agent -> no warning (real fallback)
g6 = Graph()
ag6 = g6.new_node("agent", 0, 0)
l1 = g6.new_node("llm", 0, 0); l1.props.update(model="deepseek-ai/DeepSeek-V4-Flash")
l2 = g6.new_node("llm", 0, 0); l2.props.update(model="deepseek-ai/DeepSeek-V3")
g6.add_edge(l1.id, ag6.id); g6.add_edge(l2.id, ag6.id)
assert g6.link_warning(l2.id, ag6.id) is None
print("distinct llm fallback not warned")

# 12. one Tools node aggregates several tool files for an agent
from graph_model import Node, tool_files
g7 = Graph()
ag7 = g7.new_node("agent", 0, 0); ag7.name = "worker"
l7 = g7.new_node("llm", 0, 0); l7.props.update(api_key="sk", model="m")
g7.add_edge(l7.id, ag7.id)
tools7 = g7.new_node("tool", 0, 0); tools7.name = "tools"
tools7.props["files"] = ["load_csv.py", "csv_column_means.py"]
g7.add_edge(tools7.id, ag7.id)
out7 = graph_codegen.generate_from_graph(g7, "demo_tools_node", gui=False)
spec7 = importlib.util.spec_from_file_location(
    "demo_tools_node_agent", os.path.join(out7, "agent.py"))
m7 = importlib.util.module_from_spec(spec7)
spec7.loader.exec_module(m7)
assert "load_csv" in m7.TOOLS and "csv_column_means" in m7.TOOLS, list(m7.TOOLS)
print("Tools node registered both functions:", sorted(m7.TOOLS))

# 12b. a legacy single-'file' tool node still loads (backward compatibility)
legacy = Node(id="tool_x", kind="tool", name="t", x=0, y=0,
              props={"file": "load_csv.py"})
assert tool_files(legacy) == ["load_csv.py"]
print("legacy single-file tool node still resolves")

# 13. GUI node: linking it to the entry agent derives gui.py (no gui= arg).
g8 = Graph()
ag8 = g8.new_node("agent", 0, 0); ag8.name = "planner"
l8 = g8.new_node("llm", 0, 0); l8.props.update(api_key="sk", model="m")
g8.add_edge(l8.id, ag8.id)
gnode = g8.new_node("gui", 0, 0)
g8.add_edge(gnode.id, ag8.id)                       # GUI -> entry agent
out8 = graph_codegen.generate_from_graph(g8, "demo_gui_node")   # gui derived
assert os.path.exists(os.path.join(out8, "gui.py")), "linked GUI node -> gui.py"
reqs8 = open(os.path.join(out8, "requirements.txt"), encoding="utf-8").read()
assert "PySide6" in reqs8, reqs8
print("GUI node linked -> gui.py derived + PySide6 in requirements")

# 13b. no GUI node -> headless (no gui.py); an unlinked GUI node warns + no gui.py
g9 = Graph()
ag9 = g9.new_node("agent", 0, 0); ag9.name = "solo"
l9 = g9.new_node("llm", 0, 0); l9.props.update(api_key="sk", model="m")
g9.add_edge(l9.id, ag9.id)
g9.new_node("gui", 0, 0)                            # present but NOT linked
info9 = graph_codegen.analyze(g9)
assert any("GUI module isn't linked" in w for w in info9["warnings"]), info9["warnings"]
out9 = graph_codegen.generate_from_graph(g9, "demo_gui_unlinked")
assert not os.path.exists(os.path.join(out9, "gui.py")), "unlinked GUI node -> no gui.py"
print("unlinked GUI node -> warning + headless (no gui.py)")

# 13c. multi-agent: gui must derive from the ENTRY agent only, not any agent
g10 = Graph()
a10 = g10.new_node("agent", 0, 0); a10.name = "planner"
b10 = g10.new_node("agent", 0, 0); b10.name = "writer"
for ag in (a10, b10):
    ll = g10.new_node("llm", 0, 0); ll.props.update(api_key="sk", model="m")
    g10.add_edge(ll.id, ag.id)
g10.add_edge(a10.id, b10.id)                       # planner (entry) -> writer
gx = g10.new_node("gui", 0, 0)
g10.add_edge(gx.id, b10.id)                        # GUI -> NON-entry agent
assert any("isn't linked to the entry agent" in w
           for w in graph_codegen.analyze(g10)["warnings"])
out10 = graph_codegen.generate_from_graph(g10, "demo_gui_nonentry")
assert not os.path.exists(os.path.join(out10, "gui.py")), "GUI on non-entry -> no gui.py"
g10.remove_edge(next(e for e in g10.edges if e.src == gx.id))
g10.add_edge(gx.id, a10.id)                        # re-link GUI -> entry
assert not any("isn't linked" in w for w in graph_codegen.analyze(g10)["warnings"])
out10b = graph_codegen.generate_from_graph(g10, "demo_gui_entry")
assert os.path.exists(os.path.join(out10b, "gui.py")), "GUI on entry -> gui.py"
print("gui derivation ok: entry agent only (non-entry link warns + no gui.py)")

# 13d. more than one GUI node -> advisory 'only one' warning
g11 = Graph()
a11 = g11.new_node("agent", 0, 0); a11.name = "solo"
l11 = g11.new_node("llm", 0, 0); l11.props.update(api_key="sk", model="m")
g11.add_edge(l11.id, a11.id)
g11a = g11.new_node("gui", 0, 0); g11.add_edge(g11a.id, a11.id)
g11.new_node("gui", 0, 0)                          # a second, unlinked GUI node
assert any("Only one GUI module" in w for w in graph_codegen.analyze(g11)["warnings"])
print("two GUI nodes -> 'only one' warning")

# 13e. save/load preserves the gui node + its edge; derivation survives
g12 = Graph()
a12 = g12.new_node("agent", 0, 0); a12.name = "solo"
l12 = g12.new_node("llm", 0, 0); l12.props.update(api_key="sk", model="m")
g12.add_edge(l12.id, a12.id)
gg = g12.new_node("gui", 0, 0); g12.add_edge(gg.id, a12.id)
g12.save("gui_roundtrip.json")
g12b = Graph.load("gui_roundtrip.json")
os.remove("gui_roundtrip.json")
assert any(n.kind == "gui" for n in g12b.nodes.values()), "gui node lost on load"
out12 = graph_codegen.generate_from_graph(g12b, "demo_gui_roundtrip")
assert os.path.exists(os.path.join(out12, "gui.py")), "gui derivation must survive save/load"
print("gui node save/load round-trip ok")

print("\nALL CANVAS CHECKS PASSED")
