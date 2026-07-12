"""Verify L2: opt-in answer-groundedness grading + bounded regenerate loop.

An agent with groundedness_check grades its final answer (grounded in the
retrieved context + answers the question) and, if it falls short, feeds back and
regenerates up to max_regen times. Purely additive: with the flag OFF the grader
is NEVER called and react() behaves exactly as before (legacy untouched).
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

g = Graph()
a = g.new_node("agent", 0, 0); a.name = "A"
a.props["groundedness_check"] = True; a.props["max_regen"] = 1
llm = g.new_node("llm", 0, 0); llm.props.update(LLM); g.add_edge(llm.id, a.id)

out = os.path.join(GENERATED_DIR, "verify_grounded")
if os.path.exists(out):
    shutil.rmtree(out)
out = graph_codegen.generate_from_graph(g, "verify_grounded", gui=False)
mod = importlib.util.module_from_spec(
    importlib.util.spec_from_file_location("vg_agent", os.path.join(out, "agent.py")))
sys.path.insert(0, out); os.chdir(out)
mod.__loader__.exec_module(mod)
os.chdir(BASE)

# 1. spec wiring + prompt tail.
assert mod.AGENTS["A"].get("groundedness_check") is True, mod.AGENTS["A"]
assert mod.AGENTS["A"].get("max_regen") == 1, mod.AGENTS["A"]
assert "## Grounded answers" in mod.AGENTS["A"]["system"], "prompt tail missing"
print("ok 1: groundedness_check + max_regen threaded into the spec + prompt tail")

# 2. _grade_answer: context is passed to the grader; OK/reason parsing; fail-soft.
seen = []


def _gok(agent_name, cfg, system, messages):
    seen.append(messages[-1]["content"])
    return ("OK", []) if "you grade an assistant" in system.lower() else ("x", [])


mod._call_one = _gok
ok, why = mod._grade_answer(mod.ENTRY, "Q", "A", "CTXMARKER123")
assert ok and why == "", (ok, why)
assert any("CTXMARKER123" in p for p in seen), "retrieved context not passed to the grader"
mod._call_one = lambda a, c, s, m: ("not grounded: it invented facts.", [])
ok, why = mod._grade_answer(mod.ENTRY, "Q", "A", "")
assert (not ok) and "invented" in why, (ok, why)
# a reason that merely STARTS with "Ok..." must NOT be accepted as grounded.
mod._call_one = lambda a, c, s, m: ("Okay, but the answer misses a key point.", [])
ok, why = mod._grade_answer(mod.ENTRY, "Q", "A", "")
assert (not ok) and "misses" in why, ("'Ok...' reason wrongly accepted", ok, why)
mod._call_one = lambda a, c, s, m: ("OK.", [])   # a genuine OK (trailing punctuation)
assert mod._grade_answer(mod.ENTRY, "Q", "A", "")[0] is True
mod._call_one = lambda a, c, s, m: (_ for _ in ()).throw(RuntimeError("boom"))
assert mod._grade_answer(mod.ENTRY, "Q", "A", "ctx") == (True, ""), "grader must fail soft"
print("ok 2: _grade_answer passes context, parses OK/reason precisely, fails soft")

# 3. react loop: a failing grade triggers ONE bounded regenerate, then accepts.
st = {"n": 0, "grades": 0, "grade_ok": False}


def _stub(agent_name, cfg, system, messages):
    if "you grade an assistant" in system.lower():
        st["grades"] += 1
        return ("OK", []) if st["grade_ok"] else ("not grounded: unsupported claim.", [])
    st["n"] += 1
    return (f"ANSWER_V{st['n']}", [])


mod._call_one = _stub
res = mod.run("do the task", emit=lambda s: None)
assert res == "ANSWER_V2", ("grade failed once -> should regenerate to V2", res, st)
assert st["grades"] == 1 and st["n"] == 2, st       # one grade, one regenerate, then stop
print("ok 3: failing grade regenerates once (bounded by max_regen), then accepts")

# 4. a passing grade accepts the first answer (no needless regenerate).
st.update(n=0, grades=0, grade_ok=True)
assert mod.run("do the task", emit=lambda s: None) == "ANSWER_V1", st
assert st["grades"] == 1 and st["n"] == 1, st
print("ok 4: passing grade accepts the first answer")

# 5. LEGACY UNTOUCHED: with the flag off, the grader is NEVER called and the loop
#    behaves exactly as before (zero extra LLM calls).
mod.AGENTS["A"]["groundedness_check"] = False
st.update(n=0, grades=0, grade_ok=False)
assert mod.run("do the task", emit=lambda s: None) == "ANSWER_V1", st
assert st["grades"] == 0, ("grader must not run when the feature is off", st)
mod.AGENTS["A"]["groundedness_check"] = True
print("ok 5: feature off -> grader never runs, react() unchanged (legacy untouched)")

# 6. enabling the check clamps max_regen to >= 1 (0 would be a no-op with a tail).
g2 = Graph()
a2 = g2.new_node("agent", 0, 0); a2.name = "Z"
a2.props["groundedness_check"] = True; a2.props["max_regen"] = 0
l2 = g2.new_node("llm", 0, 0); l2.props.update(LLM); g2.add_edge(l2.id, a2.id)
out2 = os.path.join(GENERATED_DIR, "verify_grounded0")
if os.path.exists(out2):
    shutil.rmtree(out2)
out2 = graph_codegen.generate_from_graph(g2, "verify_grounded0", gui=False)
m2 = importlib.util.module_from_spec(
    importlib.util.spec_from_file_location("vg0", os.path.join(out2, "agent.py")))
sys.path.insert(0, out2); os.chdir(out2)
m2.__loader__.exec_module(m2)
os.chdir(BASE)
assert m2.AGENTS["Z"].get("max_regen") == 1, ("max_regen=0 should clamp to 1", m2.AGENTS["Z"])
print("ok 6: groundedness_check with max_regen=0 clamps to 1 (no silent no-op)")

print("ALL GROUNDEDNESS CHECKS PASSED")
