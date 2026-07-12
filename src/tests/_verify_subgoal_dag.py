"""Verify structured SubgoalSpec / dependency-aware pool (roadmap item 4).

A planner with `structured_plan` emits a typed {id, subgoal, depends_on} plan in
a ```plan fence; a pool with `dag` runs it as a DAG: independent subgoals in
parallel, dependents wait for and RECEIVE their prerequisites' outputs. Invalid /
absent plans fall back to today's flat parallel pool. No LLM: we monkeypatch
mod._call_one (the same seam _verify_pool.py uses) so react runs network-free."""

import importlib.util
import json
import os
import py_compile
import re
import sys
import threading
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import graph_codegen
from graph_model import Graph

LLM = {"provider": "siliconflow", "model": "deepseek-ai/DeepSeek-V4-Flash",
       "api_key": "sk-test", "base_url": "https://api.siliconflow.cn/v1"}


def _agent(g, name, kind="agent", **props):
    n = g.new_node(kind, 0, 0); n.name = name; n.props.update(props)
    llm = g.new_node("llm", 0, 0); llm.props.update(LLM)
    assert g.add_edge(llm.id, n.id) is None
    return n


# 1. Codegen / spec: opt-in flags + planner tail; plain pool stays flat
g = Graph()
pl = _agent(g, "planner", role="planner", structured_plan=True)
pool = _agent(g, "pool", kind="workerpool", role="worker", max_workers=3, dag_plan=True)
cr = _agent(g, "critic", role="critic")
g.add_edge(pl.id, pool.id); g.add_edge(pool.id, cr.id)
assert not graph_codegen.analyze(g)["errors"], graph_codegen.analyze(g)["errors"]
out_dir = graph_codegen.generate_from_graph(g, "demo_subgoal_dag", gui=False)
py_compile.compile(os.path.join(out_dir, "agent.py"), doraise=True)
spec = importlib.util.spec_from_file_location("demo_subgoal_dag_agent",
                                              os.path.join(out_dir, "agent.py"))
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
assert mod.AGENTS["pool"]["dag"] is True
assert mod.AGENTS["planner"].get("structured_plan") is True
assert "```plan" in mod.AGENTS["planner"]["system"], "planner tail missing"

g2 = Graph()                                  # plain pool (no dag_plan) → no flag
_agent(g2, "p2", role="planner")
pool2 = _agent(g2, "pool2", kind="workerpool", role="worker", max_workers=2)
g2.add_edge(g2.nodes[[n for n in g2.nodes if g2.nodes[n].name == "p2"][0]].id, pool2.id)
out2 = graph_codegen.generate_from_graph(g2, "demo_pool_plain2", gui=False)
m2spec = importlib.util.spec_from_file_location("demo_pool_plain2_agent",
                                                os.path.join(out2, "agent.py"))
mod2 = importlib.util.module_from_spec(m2spec); m2spec.loader.exec_module(mod2)
assert mod2.AGENTS["pool2"].get("dag") is None, "flat pool must not get dag flag"
print("ok 1: dag/structured_plan flags + planner tail emitted; flat pool unflagged")

# 2. _parse_subgoal_spec — tolerant parse + normalize + DAG/validity gate
P = mod._parse_subgoal_spec


def block(subgoals, prose="Here is the plan.\n"):
    return prose + "```plan\n" + json.dumps({"subgoals": subgoals}) + "\n```\n"


good = P(block([{"id": "s1", "subgoal": "do A", "depends_on": []},
                {"id": "s2", "subgoal": "do B", "depends_on": ["s1"]}]))
assert good and [x["id"] for x in good] == ["s1", "s2"]
assert good[1]["depends_on"] == ["s1"]
# bare top-level array (no wrapper), id auto-assign, depends_on missing/str/null
bare = P('[{"subgoal":"x"},{"subgoal":"y","depends_on":"s1"},'
         '{"subgoal":"z","depends_on":null}]')
assert bare and [x["id"] for x in bare] == ["s1", "s2", "s3"]
assert bare[1]["depends_on"] == ["s1"] and bare[2]["depends_on"] == []
# int ids coerced to str; alt key 'task'
alt = P('{"subgoals":[{"id":1,"task":"a","depends_on":[]},'
        '{"id":2,"task":"b","depends_on":[1]}]}')
assert alt and alt[1]["depends_on"] == ["1"], alt
# dangling ref + self-dep dropped; duplicate id repaired
clean = P('{"subgoals":[{"id":"s1","subgoal":"a","depends_on":["zzz","s1"]},'
          '{"id":"s1","subgoal":"b","depends_on":[]}]}')
assert clean and clean[0]["depends_on"] == [] and clean[1]["id"] == "s1_", clean
# rejections → None (caller falls back to flat split)
assert P('{"subgoals":[{"id":"s1","subgoal":"a","depends_on":["s2"]},'
         '{"id":"s2","subgoal":"b","depends_on":["s1"]}]}') is None, "cycle"
assert P('{"subgoals":[{"id":"s1","subgoal":"only one","depends_on":[]}]}') is None, "<2"
assert P("```plan\n{not valid json}\n```") is None, "malformed"
assert P("1. step one\n2. step two\n3. step three") is None, "free text"
assert P("") is None and P(None) is None
print("ok 2: parse handles fence/bare/alt-keys/int-ids/dangling/dup, rejects "
      "cycle/<2/malformed/free-text")

# 2b. quote-aware loose parse: a brace inside a subgoal string (no fence) must
#     not truncate the JSON blob (audit fix: _balanced is string/escape aware)
br = P('[{"id":"s1","subgoal":"tidy the } brace [x]","depends_on":[]},'
       '{"id":"s2","subgoal":"done","depends_on":["s1"]}]')
assert br and [x["id"] for x in br] == ["s1", "s2"], br
assert "}" in br[0]["subgoal"] and "[x]" in br[0]["subgoal"], br
# 2c. a non-JSON fence (```state) BEFORE the ```plan fence must not shadow it
#     (audit fix: try every fence, not just the first)
mixed = ("```state\nscore = 0.6\n```\n"
         + block([{"id": "s1", "subgoal": "a", "depends_on": []},
                  {"id": "s2", "subgoal": "b", "depends_on": ["s1"]}], prose=""))
mp = P(mixed)
assert mp and [x["id"] for x in mp] == ["s1", "s2"], mp
print("ok 2b/2c: brace-in-string parses; a stray ```state fence doesn't shadow "
      "a later ```plan")


# ---- execution helpers: a network-free _call_one that records order + prompts
def make_recorder(sleep=0.0, fail_on=None, cancel_on=None):
    lock = threading.Lock()
    rec = {"order": [], "prompt": {}, "conc": 0, "max": 0}

    def stub(agent_name, cfg, system, messages):
        content = messages[0]["content"]
        sid = re.search(r"Subgoal (\S+) ", content).group(1)
        with lock:
            rec["order"].append(sid)
            rec["prompt"][sid] = content
            rec["conc"] += 1
            rec["max"] = max(rec["max"], rec["conc"])
        if cancel_on == sid:
            mod.request_cancel()
        if sleep:
            time.sleep(sleep)
        mod._track(agent_name, cfg, 10, 5)
        with lock:
            rec["conc"] -= 1
        if fail_on == sid:
            raise RuntimeError("boom")
        return f"OUT[{sid}]", []

    return stub, rec


def reset_usage():
    mod.USAGE["pool"] = {"input_tokens": 0, "output_tokens": 0, "tool_calls": 0}


# 3. Ordering + dependency-output injection (s2 depends on s1)
stub, rec = make_recorder(sleep=0.05)
mod._call_one = stub; reset_usage()
txt = block([{"id": "s1", "subgoal": "do A", "depends_on": []},
             {"id": "s2", "subgoal": "do B", "depends_on": ["s1"]}])
merged = mod.run_stage("pool", txt, emit=lambda s: None)
assert rec["order"].index("s1") < rec["order"].index("s2"), rec["order"]
assert "OUT[s1]" in rec["prompt"]["s2"], "s2 did not receive s1's output"
assert "OUT[s1]" not in rec["prompt"]["s1"], "independent subgoal got injected deps"
assert merged.index("[subgoal s1]") < merged.index("[subgoal s2]"), merged
print("ok 3: dependent runs after its prereq AND receives its output; ordered merge")

# 4. Parallelism: 3 independent subgoals run concurrently (max_workers=3)
stub, rec = make_recorder(sleep=0.15)
mod._call_one = stub; reset_usage()
txt = block([{"id": "a", "subgoal": "A", "depends_on": []},
             {"id": "b", "subgoal": "B", "depends_on": []},
             {"id": "c", "subgoal": "C", "depends_on": []}])
mod.run_stage("pool", txt, emit=lambda s: None)
assert rec["max"] >= 2, f"independents did not run in parallel (max={rec['max']})"
assert mod.USAGE["pool"]["input_tokens"] == 30, mod.USAGE["pool"]   # 3×10, no lost updates
print(f"ok 4: independents ran in parallel (max {rec['max']}); usage summed (30/15)")

# 5. Diamond: s1 → {s2,s3} → s4; s4 sees BOTH s2 and s3; s2,s3 overlap
stub, rec = make_recorder(sleep=0.12)
mod._call_one = stub; reset_usage()
txt = block([{"id": "s1", "subgoal": "root", "depends_on": []},
             {"id": "s2", "subgoal": "left", "depends_on": ["s1"]},
             {"id": "s3", "subgoal": "right", "depends_on": ["s1"]},
             {"id": "s4", "subgoal": "join", "depends_on": ["s2", "s3"]}])
merged = mod.run_stage("pool", txt, emit=lambda s: None)
assert rec["order"][0] == "s1", rec["order"]
assert rec["order"][-1] == "s4", rec["order"]
assert "OUT[s2]" in rec["prompt"]["s4"] and "OUT[s3]" in rec["prompt"]["s4"], "join missing a dep"
order = merged
assert (order.index("[subgoal s1]") < order.index("[subgoal s2]")
        < order.index("[subgoal s3]") < order.index("[subgoal s4]")), "merge order"
print("ok 5: diamond — join waits for both branches and receives both outputs")

# 6. Fault isolation: a failing subgoal becomes an [ERROR]; dependents still run
stub, rec = make_recorder(fail_on="s1")
mod._call_one = stub; reset_usage()
txt = block([{"id": "s1", "subgoal": "boom", "depends_on": []},
             {"id": "s2", "subgoal": "after", "depends_on": ["s1"]}])
merged = mod.run_stage("pool", txt, emit=lambda s: None)
assert "[ERROR] subgoal s1 failed" in merged, merged
assert "s2" in rec["order"], "dependent did not run after a failed prereq"
assert "[ERROR] subgoal s1 failed" in rec["prompt"]["s2"], "s2 should see the error text"
print("ok 6: a failed subgoal is isolated; dependents still run with the error text")

# 7. Fallback: dag=True but planner emitted free text → flat pool path
stub, rec = make_recorder()
mod._call_one = stub
flat = mod.run_stage("pool", "1. alpha\n2. beta\n3. gamma", emit=lambda s: None)
assert "[subtask 1]" in flat and "[subgoal" not in flat, flat   # flat labels, not DAG
print("ok 7: dag pool with no valid plan falls back to flat parallel (no regression)")

# 7b. a structured planner's output into a NON-dag pool: _pool_split_tasks strips
#     the ```plan fence so the numbered steps win, not stringified subgoal dicts
#     (audit fix: a plan fence must not pollute the flat split)
planner_out = ("1. gather data\n2. analyze\n3. report\n"
               + block([{"id": "s1", "subgoal": "x", "depends_on": []},
                        {"id": "s2", "subgoal": "y", "depends_on": ["s1"]}], prose=""))
flat_tasks = mod._pool_split_tasks(planner_out)
assert flat_tasks == ["gather data", "analyze", "report"], flat_tasks
assert not any("subgoal" in t for t in flat_tasks), "plan JSON leaked as subtasks"
# existing flat behavior unchanged (no fence present)
assert mod._pool_split_tasks("1. a\n2. b") == ["a", "b"]
assert mod._pool_split_tasks('["x", "y"]') == ["x", "y"]
print("ok 7b: a plan fence is stripped from the flat split; numbered steps win")

# 7c. analyze() advisory: structured_plan planner + non-dag pool warns; with a
#     dag pool it does not (audit fix)
import graph_codegen as _gc
ga = Graph()
pa = _agent(ga, "planner", role="planner", structured_plan=True)
poola = _agent(ga, "pool", kind="workerpool", role="worker", max_workers=2)
ga.add_edge(pa.id, poola.id)
warns = _gc.analyze(ga)["warnings"]
assert any("Dependency-aware execution" in w for w in warns), warns
poola.props["dag_plan"] = True
warns2 = _gc.analyze(ga)["warnings"]
assert not any("Dependency-aware execution" in w for w in warns2), warns2
print("ok 7c: analyze warns when structured_plan has no dag pool; silent once enabled")

# 8. Cancel propagates out of the DAG dispatcher
stub, rec = make_recorder(cancel_on="s1")
mod._call_one = stub
txt = block([{"id": "s1", "subgoal": "first", "depends_on": []},
             {"id": "s2", "subgoal": "second", "depends_on": ["s1"]}])
try:
    mod.run_stage("pool", txt, emit=lambda s: None)
    raise AssertionError("cancel must propagate RunCancelled")
except mod.RunCancelled:
    pass
finally:
    mod._CANCEL.clear()
assert "s2" not in rec["order"], "a cancelled run must not start the dependent"
print("ok 8: RunCancelled propagates from run_stage; pending dependent never starts")

print("\nALL SUBGOAL-DAG CHECKS PASSED")
