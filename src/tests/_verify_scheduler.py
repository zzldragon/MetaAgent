"""Verify the SCHEDULE node.
B: multiple independent cron jobs over ONE agent — linking N schedule nodes to the entry
agent emits scheduler.py, which runs each as a CONCURRENT job on its own timer + isolated
session (task / every_seconds / offset_seconds / max_runs).
A: separate schedules driving SEPARATE agents — a schedule linked to a non-entry agent
starts the run at THAT agent (config records the target; core.run(entry=…) slices the run).
Proves analyze rules, that scheduler.py + config['schedules'] (a list, each with a target)
are emitted + compile, a schedule-free graph emits nothing, that RUNNING it fires every job
then stops cleanly, and that the entry override targets the right agent. Offline: the
wall-clock budget is tripped (-1) / the model call is stubbed so no real LLM/network call."""
import datetime
import json
import os
import py_compile
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import graph_codegen
from graph_model import DEFAULT_BUDGETS, Graph

LLM = dict(provider="siliconflow", model="deepseek-ai/DeepSeek-V4-Flash",
           api_key="", base_url="https://api.siliconflow.cn/v1")


def _graph(scheds, link=True):
    """scheds: list of dicts (name/every/offset/task/max_runs). Builds entry agent +
    one schedule node per spec, all linked to the entry (unless link=False)."""
    g = Graph()
    llm = g.new_node("llm", 300, 0); llm.name = "m"; llm.props.update(LLM)
    a = g.new_node("agent", 0, 0); a.name = "worker"; a.props["role"] = "single"
    for k in DEFAULT_BUDGETS:
        a.props[k] = DEFAULT_BUDGETS[k]
    a.props["max_wall_clock_s"] = -1          # trip the budget -> no real LLM call (offline)
    g.add_edge(llm.id, a.id)
    nodes = []
    for i, spec in enumerate(scheds):
        s = g.new_node("schedule", -200, i * 120); s.name = spec["name"]
        s.props.update(mode=spec.get("mode", "interval"),
                       every_seconds=spec.get("every", 1),
                       offset_seconds=spec.get("offset", 0),
                       initial_task=spec.get("task", "do the thing"),
                       max_runs=spec.get("max_runs", 1), run_at_start=True,
                       at=spec.get("at", ""), start_at=spec.get("start_at", ""))
        if link:
            g.add_edge(s.id, a.id)
        nodes.append(s)
    return g, a, nodes


# ── 1. analyze: MULTIPLE schedules are allowed (B); each validated ───────────
# 'errors' runs on the interval; 'news' fires at an ABSOLUTE timestamp (a couple of
# seconds out — past by the time the subprocess finishes importing, so it fires ASAP).
_SOON = (datetime.datetime.now() + datetime.timedelta(seconds=2)).strftime("%Y-%m-%d %H:%M:%S")
g, a, ns = _graph([{"name": "errors", "every": 1, "task": "summarize errors"},
                   {"name": "news", "mode": "once", "start_at": _SOON, "task": "check news"}])
info = graph_codegen.analyze(g)
assert not info["errors"], info["errors"]          # two schedules is NOT an error anymore
print("schedule analyze ok: multiple schedule nodes are allowed (concurrent jobs)")

gu = _graph([{"name": "cron"}], link=False)[0]     # unlinked -> WARNING, not error
iu = graph_codegen.analyze(gu)
assert not iu["errors"], iu["errors"]
assert any("isn't linked" in w for w in iu["warnings"]), iu["warnings"]

gbad = _graph([{"name": "cron", "every": 0}])[0]   # every < 1 -> error
assert any("every_seconds" in e for e in graph_codegen.analyze(gbad)["errors"])

# strategy modes: the chosen mode's field is validated; the others are ignored
gok = _graph([{"name": "daily", "mode": "daily", "at": "09:30"},
              {"name": "once", "mode": "once", "start_at": "2030-01-02 03:04:05"}])[0]
assert not graph_codegen.analyze(gok)["errors"], graph_codegen.analyze(gok)["errors"]
gat = _graph([{"name": "x", "mode": "daily", "at": "25:99"}])[0]
assert any("daily time" in e for e in graph_codegen.analyze(gat)["errors"])
gsa = _graph([{"name": "x", "mode": "once", "start_at": "not-a-date"}])[0]
assert any("invalid time" in e for e in graph_codegen.analyze(gsa)["errors"])
gmiss = _graph([{"name": "x", "mode": "daily", "at": ""}])[0]   # daily w/ no time -> error
assert any("needs a time" in e for e in graph_codegen.analyze(gmiss)["errors"])
# a bad every_seconds is IGNORED when the mode isn't interval (no false error)
gign = _graph([{"name": "x", "mode": "daily", "at": "09:00", "every": 0}])[0]
assert not graph_codegen.analyze(gign)["errors"], graph_codegen.analyze(gign)["errors"]
print("schedule analyze ok: per-strategy validation (daily/once/interval), others ignored")

# ── 2. generate -> scheduler.py + config['schedules'] (a LIST, one per node) ─
out = graph_codegen.generate_from_graph(g, "verify_schedule", gui=False)
sp = os.path.join(out, "scheduler.py")
assert os.path.isfile(sp)
py_compile.compile(sp, doraise=True)
cfg = json.load(open(os.path.join(out, "config.json"), encoding="utf-8"))
jobs = cfg["schedules"]
assert isinstance(jobs, list) and len(jobs) == 2, jobs
by_name = {j["name"]: j for j in jobs}
assert by_name["errors"]["mode"] == "interval" and by_name["errors"]["every_seconds"] == 1
assert by_name["news"]["mode"] == "once" and by_name["news"]["start_at"] == _SOON, by_name["news"]
assert "offset_seconds" in by_name["errors"] and "at" in by_name["news"]  # all timing keys present
src = open(sp, encoding="utf-8").read()
assert "import agent as core" in src and 'CONFIG.get("schedules"' in src
assert "threading.Thread" in src and "core.run(" in src, "one concurrent thread per job"
print("schedule codegen ok: scheduler.py + config['schedules'] list emitted, compiles")

gp = Graph()                                       # no schedule node -> no scheduler.py
lp = gp.new_node("llm", 0, 0); lp.name = "m"; lp.props.update(LLM)
ap = gp.new_node("agent", 0, 0); ap.name = "worker"
for k in DEFAULT_BUDGETS:
    ap.props[k] = DEFAULT_BUDGETS[k]
gp.add_edge(lp.id, ap.id)
out0 = graph_codegen.generate_from_graph(gp, "verify_schedule_none", gui=False)
assert not os.path.isfile(os.path.join(out0, "scheduler.py")), "no schedule -> no scheduler.py"
outp = graph_codegen.generate_from_graph(g, "verify_schedule_pkg", gui=False,
                                         code_style="package")
py_compile.compile(os.path.join(outp, "scheduler.py"), doraise=True)
print("schedule codegen ok: none->no file; package code style compiles")

# ── 3. RUN it: BOTH jobs fire once, concurrently, then a clean stop (offline) ─
_env = dict(os.environ, PYTHONPATH=out)            # so `import agent` resolves
proc = subprocess.run([sys.executable, sp], cwd=out, env=_env,
                      capture_output=True, text=True, timeout=180)
assert proc.returncode == 0, (proc.returncode, proc.stdout[-600:], proc.stderr[-600:])
o = proc.stdout
assert "[errors] run #1" in o and "[news] run #1" in o, o[-800:]      # both jobs ran
assert "[errors] stopped after 1 run" in o and "[news] stopped after 1 run" in o, o[-800:]
assert "all jobs stopped" in o, o[-400:]
print("schedule run ok: two concurrent jobs each fired once, then stopped cleanly")

# ── 4. A: separate schedules driving SEPARATE agents in one graph ────────────
# planner -> writer (a 2-stage pipeline). S_plan drives the entry (planner) = the
# whole chain; S_write drives writer alone. Proves (a) config records each job's
# target agent, and (b) core.run(entry=<name>) actually starts the run there.
import importlib.util

ga = Graph()
la = ga.new_node("llm", 300, 0); la.name = "m1"; la.props.update(LLM)
lb = ga.new_node("llm", 300, 200); lb.name = "m2"; lb.props.update(LLM)
planner = ga.new_node("agent", 0, 0); planner.name = "planner"; planner.props["role"] = "first"
writer = ga.new_node("agent", 0, 200); writer.name = "writer"; writer.props["role"] = "last"
for ag in (planner, writer):
    for k in DEFAULT_BUDGETS:
        ag.props[k] = DEFAULT_BUDGETS[k]
ga.add_edge(la.id, planner.id)
ga.add_edge(lb.id, writer.id)
ga.add_edge(planner.id, writer.id)          # planner -> writer pipeline
sp1 = ga.new_node("schedule", -200, 0); sp1.name = "s_plan"
sp1.props.update(every_seconds=1, initial_task="plan", max_runs=1, run_at_start=True)
sw1 = ga.new_node("schedule", -200, 200); sw1.name = "s_write"
sw1.props.update(every_seconds=1, initial_task="write", max_runs=1, run_at_start=True)
ga.add_edge(sp1.id, planner.id)             # drives the entry -> whole graph (B)
ga.add_edge(sw1.id, writer.id)              # drives a DIFFERENT agent (A)

assert not graph_codegen.analyze(ga)["errors"], graph_codegen.analyze(ga)["errors"]
outa = graph_codegen.generate_from_graph(ga, "verify_schedule_multi", gui=False)
cfga = json.load(open(os.path.join(outa, "config.json"), encoding="utf-8"))
tgt = {j["name"]: j.get("target") for j in cfga["schedules"]}
assert tgt == {"s_plan": "planner", "s_write": "writer"}, tgt
sa = open(os.path.join(outa, "scheduler.py"), encoding="utf-8").read()
assert "entry=entry" in sa and 'spec.get("target")' in sa, "scheduler must pass entry=target"
print("A codegen ok: each schedule records its target agent; scheduler passes entry")

# runtime entry override: load the generated agent, stub the model to log which
# agents ran, and prove entry=<name> slices the pipeline to start there.
spec = importlib.util.spec_from_file_location("sched_multi_agent",
                                              os.path.join(outa, "agent.py"))
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
assert mod.PIPELINE == ["planner", "writer"], mod.PIPELINE
ran = []
mod._call_one = lambda agent_name, cfg, system, messages: (
    ran.append(agent_name) or f"done by {agent_name}", [])

mod.clear_history(); ran.clear()
mod.run("go", emit=lambda s: None)                       # default entry = whole chain
assert ran == ["planner", "writer"], ran

mod.clear_history(); ran.clear()
mod.run("go", emit=lambda s: None, entry="writer")       # A: start at writer only
assert ran == ["writer"], ran

mod.clear_history(); ran.clear()
mod.run("go", emit=lambda s: None, entry="planner")      # entry == graph entry == whole chain
assert ran == ["planner", "writer"], ran
print("A runtime ok: core.run(entry=…) starts the run at the targeted agent")

print("\nALL SCHEDULER CHECKS PASSED")
