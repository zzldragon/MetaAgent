"""Verify graphs/CodingAgent.mta is a fan-out ORCHESTRATOR of tool-scoped specialists.

The coder is an entry `orchestrator` that spawns three isolated leaf sub-agents:
  explorer (read-only), editor (writes), tester (shell). Asserts:
  * analyze -> mode 'autonomous', 0 errors,
  * SPAWNABLE == {explorer, editor, tester}; spawn_subagent ONLY on the coder,
  * each specialist has EXACTLY its tool slice (read / edit / shell),
  * end-to-end: the coder spawns the editor, and the editor's write_file still goes
    through the HITL confirm handler (so the GUI diff-approve survives) — file is
    written only after approval.
"""
import importlib.util
import os
import shutil
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import graph_codegen  # noqa: E402
import graph_model  # noqa: E402

td = tempfile.mkdtemp()
g, _ = graph_model.load_mta("graphs/CodingAgent.mta", td)

info = graph_codegen.analyze(g)
assert not info["errors"], info["errors"]
assert info.get("mode") == "autonomous", info.get("mode")
print("ok 1: analyze clean, mode == autonomous (orchestrator)")

out = os.path.join(graph_codegen.GENERATED_DIR, "verify_coding_orch") \
    if hasattr(graph_codegen, "GENERATED_DIR") else None
out = graph_codegen.generate_from_graph(g, "verify_coding_orch", gui=False)
spec = importlib.util.spec_from_file_location("co_agent", os.path.join(out, "agent.py"))
m = importlib.util.module_from_spec(spec)
sys.path.insert(0, out); os.chdir(out)
spec.loader.exec_module(m)
os.chdir(BASE)

assert m.ENTRY == "coder", m.ENTRY
assert set(m.SPAWNABLE) == {"explorer", "editor", "tester"}, m.SPAWNABLE
assert "spawn_subagent" in m.AGENTS["coder"]["tools"]
assert not [a for a in m.AGENTS if a != "coder" and "spawn_subagent" in m.AGENTS[a]["tools"]]
print("ok 2: SPAWNABLE == {explorer, editor, tester}; spawn_subagent only on coder")

assert set(m.AGENTS["explorer"]["tools"]) == {"read_file", "list_dir", "glob_search", "grep_search"}
assert set(m.AGENTS["editor"]["tools"]) == {"write_file", "edit_file", "make_dir", "move_path", "delete_path"}
assert set(m.AGENTS["tester"]["tools"]) == {"run_shell"}
print("ok 3: each specialist has exactly its tool slice (read / edit / shell)")

# end-to-end: coder spawns editor; editor's write_file goes through the confirm gate
ws = tempfile.mkdtemp()
if hasattr(m, "set_workspace"):
    m.set_workspace([ws])
m.CONFIG["hitl_confirm"] = True
confirms = []


def _handler(tool, args):
    confirms.append((tool, dict(args)))
    return {"decision": "allow"}


m.set_confirm_handler(_handler)
target = os.path.join(ws, "hello.txt")


def _stub(agent_name, cfg, system, messages):
    if agent_name == "coder":
        if not any(mm.get("role") == "tool" for mm in messages):
            return "", [{"id": "s1", "name": "spawn_subagent",
                         "args": {"name": "editor", "task": "create hello.txt with hi"}}]
        return "FINAL: created the file", []
    if agent_name == "editor":
        if not any(mm.get("role") == "tool" for mm in messages):
            return "", [{"id": "w1", "name": "write_file",
                         "args": {"path": target, "content": "hi"}}]
        return "created hello.txt", []
    return "?", []


m._call_one = _stub
res = m.run("make hello.txt", emit=lambda s: None)
assert res.startswith("FINAL:"), res
assert confirms and confirms[0][0] == "write_file", confirms
assert os.path.isfile(target), "file should be written after approval"
print("ok 4: coder -> spawn editor -> write_file HITL-confirmed (diff-approve preserved) -> file written")

shutil.rmtree(out, ignore_errors=True)
print("ALL CODING-ORCHESTRATOR CHECKS PASSED")
