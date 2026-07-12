"""Verify the cross-run rolling summary (and thus the injected context) stays
BOUNDED no matter how many times it is compacted. Previously the summary had no
cap and history_context injected it in full, so a long-running / verbose session
could grow the summary until it crowded out the context window. Now compact_history
and history_context both hard-cap it (CONFIG['summary_max_chars'], default 4000)."""

import importlib.util
import json
import os
import py_compile
import shutil
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import graph_codegen as gc
import graph_model as gm


def _load():
    g = gm.Graph(); a = g.new_node("agent", 0, 0); a.name = "A"
    l = g.new_node("llm", -200, 0)
    l.props.update(provider="siliconflow", model="deepseek-ai/DeepSeek-V4-Flash",
                   api_key="sk-x", base_url="https://api.siliconflow.cn/v1")
    g.add_edge(l.id, a.id)
    out = gc.generate_from_graph(g, "vctxbound", gui=False)
    py_compile.compile(os.path.join(out, "agent.py"), doraise=True)
    cfg = json.load(open(os.path.join(out, "config.json"), encoding="utf-8"))
    assert cfg["summary_max_chars"] == 4000, cfg.get("summary_max_chars")
    sp = importlib.util.spec_from_file_location("vcb", os.path.join(out, "agent.py"))
    m = importlib.util.module_from_spec(sp); sp.loader.exec_module(m)
    m.save_summary = lambda: None            # keep the test off disk
    m.save_history = lambda: None
    return m, out


m, out = _load()
CAP = m._summary_cap()
assert CAP == 4000

# 1. An ADVERSARIAL summarizer that tries to grow the summary +5000 chars every
#    fold can NEVER grow it past the cap — across many runs.
def _bad_summarize(prior, turns):
    return (prior or "") + " " + "X" * 5000

worst_summary = worst_ctx = 0
for cycle in range(40):
    for _ in range(25):                       # push HISTORY over COMPACT_AT (20)
        m.HISTORY.append({"role": "user", "content": "q%d" % cycle})
        m.HISTORY.append({"role": "assistant", "content": "a%d" % cycle})
    m.compact_history(_bad_summarize)
    worst_summary = max(worst_summary, len(m.SUMMARY))
    worst_ctx = max(worst_ctx, len(m.history_context()))
    assert len(m.SUMMARY) <= CAP, ("summary grew past cap", len(m.SUMMARY))
    assert len(m.history_context()) <= CAP + 4000 + 400, \
        ("injected context unbounded", len(m.history_context()))
assert worst_summary <= CAP
print("1. 40 adversarial folds: summary<=%d, injected context<=%d — bounded ok"
      % (worst_summary, worst_ctx))

# 2. A loaded/legacy oversized summary is capped when injected (not just when folded)
m.SUMMARY = "Y" * 20000
assert len(m.history_context()) <= CAP + 4000 + 400
assert len(m._cap_summary(m.SUMMARY)) <= CAP
print("2. oversized loaded summary capped on inject ok")

# 3. A normal short summary is left untouched (no needless truncation)
m.SUMMARY = "the user is migrating a CSV pipeline; chose pandas; TODO: add tests"
assert m._cap_summary(m.SUMMARY) == m.SUMMARY
print("3. short summary passes through unchanged ok")

# 4. CONFIG['summary_max_chars'] lowers/raises the cap at runtime
m.CONFIG["summary_max_chars"] = 500
assert m._summary_cap() == 500 and len(m._cap_summary("Z" * 3000)) <= 500
print("4. CONFIG summary_max_chars override honored ok")

shutil.rmtree(out, ignore_errors=True)
print("\nALL BOUNDED-CONTEXT CHECKS PASSED")
