"""Functional graph-mode checkpoint/resume test (previously UNTESTED — grep found
zero resume()/save_checkpoint call sites in tests). run_graph snapshots each stage
boundary; a mid-run checkpoint resumes from where it stopped WITHOUT re-running
completed stages; a stale topology-sig is ignored; missing/done checkpoints are
no-ops. This is the Phase-0 safety net locked in BEFORE the parallel fan-out rewrite
of run_graph, so that rework is provably resume-preserving. Offline."""
import copy
import importlib.util
import os
import shutil
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import graph_codegen
from graph_model import Graph, DEFAULT_BUDGETS

LLM = dict(provider="siliconflow", model="deepseek-ai/DeepSeek-V4-Flash",
           api_key="", base_url="https://api.siliconflow.cn/v1")

# a -> b -> c -> End : the End node forces GRAPH mode (run_graph + checkpointing);
# a str+append `steps` field lets us see which stages actually ran.
g = Graph()
g.state_schema = [{"name": "steps", "type": "str", "reducer": "append",
                   "default": "", "description": "execution log"}]
llm = g.new_node("llm", 0, 0); llm.name = "m"; llm.props.update(LLM)


def mk(name, y):
    a = g.new_node("agent", 0, y); a.name = name; a.props["role"] = "single"
    a.props["writes"] = ["steps"]
    for k in DEFAULT_BUDGETS:
        a.props[k] = DEFAULT_BUDGETS[k]
    g.add_edge(llm.id, a.id)
    return a


a, b, c = mk("a", -80), mk("b", 0), mk("c", 80)
end = g.new_node("end", 0, 160); end.name = "done"
g.add_edge(a.id, b.id); g.add_edge(b.id, c.id); g.add_edge(c.id, end.id)
info = graph_codegen.analyze(g)
assert not info["errors"], info["errors"]
assert info["mode"] == "graph", ("need graph mode for checkpointing", info["mode"])
out = graph_codegen.generate_from_graph(g, "verify_graph_checkpoint", gui=False)

spec = importlib.util.spec_from_file_location("vgc", os.path.join(out, "agent.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
assert hasattr(mod, "save_checkpoint") and hasattr(mod, "resume"), "checkpoint code not emitted"
mod.CHECKPOINT_ENABLED = True          # config gate is import-time; force it on for the test
CK = os.path.join(mod.BASE_DIR, "checkpoints")
shutil.rmtree(CK, ignore_errors=True)

ORDER = []
def stub(agent_name, cfg, system, messages):
    ORDER.append(agent_name)
    return (f'[{agent_name}]\n\n```state\nsteps = "{agent_name}"\n```', [])
mod._call_one = stub

TID = "ckpt-test-1"

# 1. a full run snapshots EVERY stage boundary (done=False, right topology_sig) and
#    clears the checkpoint on clean finish.
RECS = []
_real_save = mod.save_checkpoint
mod.save_checkpoint = lambda tid, snap: (RECS.append(copy.deepcopy(snap)),
                                         _real_save(tid, snap))[-1]
res = mod.run_graph("go", emit=lambda s: None, thread_id=TID)
mod.save_checkpoint = _real_save
assert ORDER == ["a", "b", "c"], ORDER
assert [r["node"] for r in RECS] == ["a", "b", "c", "done"], [r["node"] for r in RECS]
assert all(r["done"] is False and r["topology_sig"] == mod._TOPO_SIG for r in RECS)
assert mod.load_checkpoint(TID) is None, "checkpoint must be cleared on clean finish"
print("checkpoint save ok: snapshot per stage boundary + cleared on finish")

# 2. the REAL snapshot run_graph wrote just before 'c' ran = a genuine mid-run/crash
#    point (a,b applied, c not). Resuming from it must re-run ONLY c.
snap_c = next(r for r in RECS if r["node"] == "c")
assert "a" in snap_c["state"]["steps"] and "b" in snap_c["state"]["steps"]
assert "c" not in snap_c["state"]["steps"], "snapshot before c must not contain c"
mod.save_checkpoint(TID, snap_c)
ORDER.clear()
res2 = mod.run_graph("go", emit=lambda s: None, thread_id=TID)
assert ORDER == ["c"], ("resume must re-run ONLY c (a,b rehydrated), got", ORDER)
assert res2.startswith("[c]"), res2
assert mod.load_checkpoint(TID) is None, "checkpoint must clear after resume finishes"
print("checkpoint resume ok: rehydrates at c, skips a/b, completes + clears")

# 2b. the public resume() entrypoint does the same from a crafted mid-run snapshot.
mod.save_checkpoint(TID, snap_c)
ORDER.clear()
res3 = mod.resume(TID, emit=lambda s: None)
assert ORDER == ["c"] and res3.startswith("[c]"), (ORDER, res3)
print("resume() entrypoint ok")

# 3. topology drift: a checkpoint whose topology_sig no longer matches is IGNORED —
#    the run restarts from ENTRY (protects against resuming a changed graph).
stale = copy.deepcopy(snap_c); stale["topology_sig"] = "STALE-SIG"
mod.save_checkpoint(TID, stale)
ORDER.clear()
mod.run_graph("go", emit=lambda s: None, thread_id=TID)
assert ORDER == ["a", "b", "c"], ("stale sig must restart from entry", ORDER)
print("topology-drift ok: stale checkpoint ignored, full re-run from entry")

# 4. nothing-to-resume + already-done checkpoints are no-ops.
mod.clear_checkpoint(TID)
assert "nothing to resume" in mod.resume(TID, emit=lambda s: None)
done_snap = copy.deepcopy(snap_c); done_snap["done"] = True
mod.save_checkpoint(TID, done_snap)
assert "nothing to resume" in mod.resume(TID, emit=lambda s: None)
print("no-op resume ok: missing + done checkpoints are not resumed")

shutil.rmtree(CK, ignore_errors=True)
print("\nALL GRAPH-CHECKPOINT CHECKS PASSED")
