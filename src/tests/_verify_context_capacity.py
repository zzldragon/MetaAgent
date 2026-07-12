"""Verify capacity-based context compaction (replaces the max_input_tokens hard-
stop). Covers the generated agents AND the coding agent — all offline (stubbed LLM):

  (a) an LLM node's context_capacity flows into the generated CONFIG;
  (b) _compact_messages keeps msg[0] (task) + last 10, shrinks, never orphans a tool;
  (c) the ENTRY agent compacts near capacity and context_usage() reports it;
  (d) a WORKER (non-entry) never compacts even with capacity set;
  (e) no capacity set -> neither compaction nor a hard-stop (long run completes);
  (f) the coding agent compacts to capacity (keep system + recent), 0 = off.
"""
import importlib.util
import os
import shutil
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import graph_codegen
from graph_model import Graph

LLM = {"provider": "siliconflow", "model": "deepseek-ai/DeepSeek-V4-Flash",
       "api_key": "", "base_url": "https://api.siliconflow.cn/v1"}

# A throwaway tool that returns a large block of text, so a run can accumulate
# enough context to cross the capacity trigger. Written into tools/ for the
# generator to inline, then removed in the finally block.
_BIGTOOL = os.path.join(BASE, "tools", "ctx_bigtool.py")
with open(_BIGTOOL, "w", encoding="utf-8") as _f:
    _f.write('from tool_registry import tool\n\n\n'
             '@tool\ndef ctx_bigtool(n: int = 1) -> str:\n'
             '    """Return a large block of filler text (compaction test)."""\n'
             '    return "x" * 1500\n')


def _gen(graph, name):
    out = graph_codegen.generate_from_graph(graph, name)
    spec = importlib.util.spec_from_file_location(name + "_agent",
                                                  os.path.join(out, "agent.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return out, mod


def _single(capacity, with_tool=True):
    g = Graph()
    a = g.new_node("agent", 0, 0); a.name = "agent"; a.props["role"] = "single"
    a.props["max_iterations"] = 12
    lm = g.new_node("llm", 0, 0); lm.props.update(LLM)
    lm.props["context_capacity"] = capacity
    g.add_edge(lm.id, a.id)
    if with_tool:
        t = g.new_node("tool", 0, 0); t.props["files"] = ["ctx_bigtool.py"]
        g.add_edge(t.id, a.id)
    return g


_outs = []
try:
    # (a) capacity threads into the generated config
    out, mod = _gen(_single(128000, with_tool=False), "ctxcap_thread")
    _outs.append(out)
    assert mod.CONFIG["llms"]["agent"][0]["context_capacity"] == 128000
    print("ok a: context_capacity flows into generated CONFIG")

    # (b) _compact_messages: keep msg[0] + last 10, shrink, no orphan 'tool' tail
    out, mod = _gen(_single(4000, with_tool=False), "ctxcap_unit")
    _outs.append(out)
    mod._call_one = lambda an, cfg, system, msgs: ("MID-SUMMARY", []) \
        if system == mod._SUMMARIZE_SYS else ("?", [])
    messages = [{"role": "user", "content": "THE-TASK"}]
    for i in range(12):                       # 12 assistant+tool pairs (older turns)
        messages.append({"role": "assistant", "content": f"step {i}",
                         "tool_calls": [{"id": f"c{i}", "name": "t", "args": {}}]})
        messages.append({"role": "tool", "tool_call_id": f"c{i}",
                         "content": "RESULT " + "y" * 50})
    before = len(messages)
    tail10 = messages[-10:]
    assert mod._compact_messages("agent", messages) == "compacted"
    assert len(messages) < before                       # shrank
    # summary is FOLDED INTO the task (messages[0]) — not a separate user turn
    assert messages[0]["content"].startswith("THE-TASK")
    assert "MID-SUMMARY" in messages[0]["content"]
    assert "compacted" in messages[0]["content"]
    assert messages[-10:] == tail10                     # recent 10 preserved verbatim
    assert messages[1].get("role") != "tool"            # tail doesn't start orphaned
    # valid alternation: no two consecutive user messages (Anthropic-safe)
    assert not any(messages[i].get("role") == "user" and
                   messages[i + 1].get("role") == "user"
                   for i in range(len(messages) - 1))
    print("ok b: _compact_messages folds summary into the task, keeps last 10")

    # (c) ENTRY agent compacts near capacity; context_usage() reports it
    out, mod = _gen(_single(500, with_tool=True), "ctxcap_entry")
    _outs.append(out)
    calls = {"compact": 0}
    _orig = mod._compact_messages
    mod._compact_messages = lambda an, m: (calls.__setitem__("compact", calls["compact"] + 1)
                                           or _orig(an, m))
    state = {"n": 0}

    def stub_entry(an, cfg, system, msgs):
        if system == mod._SUMMARIZE_SYS:
            return ("S", [])
        state["n"] += 1
        if state["n"] <= 6:                   # accumulate big tool results
            return ("", [{"id": f"c{state['n']}", "name": "ctx_bigtool", "args": {}}])
        return ("FINAL", [])
    mod._call_one = stub_entry
    res = mod.run("do work", emit=lambda s: None)
    assert "FINAL" in res, res
    assert calls["compact"] > 0, "entry agent should have compacted near capacity"
    cu = mod.context_usage()
    assert cu["capacity"] == 500 and cu["tokens"] > 0, cu
    print(f"ok c: ENTRY compacts near capacity; context_usage={cu}")

    # (d) a WORKER (non-entry) never compacts, even with capacity on its LLM
    g = Graph()
    pl = g.new_node("agent", 0, 0); pl.name = "planner"; pl.props["role"] = "planner"
    lp = g.new_node("llm", 0, 0); lp.props.update(LLM); g.add_edge(lp.id, pl.id)
    ex = g.new_node("agent", 0, 0); ex.name = "executor"; ex.props["role"] = "worker"
    ex.props["max_iterations"] = 12
    le = g.new_node("llm", 0, 0); le.props.update(LLM); le.props["context_capacity"] = 500
    g.add_edge(le.id, ex.id)
    te = g.new_node("tool", 0, 0); te.props["files"] = ["ctx_bigtool.py"]
    g.add_edge(te.id, ex.id)
    g.add_edge(pl.id, ex.id)
    out, mod = _gen(g, "ctxcap_worker")
    _outs.append(out)
    assert mod.ENTRY == "planner"
    assert mod.CONFIG["llms"]["executor"][0]["context_capacity"] == 500
    wcalls = {"compact": 0}
    mod._compact_messages = lambda an, m: wcalls.__setitem__("compact", wcalls["compact"] + 1)
    state = {"n": 0}

    def stub_worker(an, cfg, system, msgs):
        if system == mod._SUMMARIZE_SYS:
            return ("S", [])
        state["n"] += 1
        if state["n"] <= 6:
            return ("", [{"id": f"c{state['n']}", "name": "ctx_bigtool", "args": {}}])
        return ("WORKER DONE", [])
    mod._call_one = stub_worker
    mod._CANCEL.clear()
    res = mod.react("executor", "do it", emit=lambda s: None)
    assert "WORKER DONE" in res, res
    assert wcalls["compact"] == 0, "a worker (non-entry) must never compact"
    print("ok d: worker (non-entry) never compacts despite capacity")

    # (e) no capacity -> no compaction AND no hard-stop (the long run completes)
    out, mod = _gen(_single(0, with_tool=True), "ctxcap_nocap")
    _outs.append(out)
    ncalls = {"compact": 0}
    mod._compact_messages = lambda an, m: ncalls.__setitem__("compact", ncalls["compact"] + 1)
    state = {"n": 0}

    def stub_nocap(an, cfg, system, msgs):
        state["n"] += 1
        if state["n"] <= 6:
            return ("", [{"id": f"c{state['n']}", "name": "ctx_bigtool", "args": {}}])
        return ("FINAL", [])
    mod._call_one = stub_nocap
    res = mod.run("do work", emit=lambda s: None)
    assert "FINAL" in res and "[budget]" not in res, res
    assert ncalls["compact"] == 0, "no capacity set -> no compaction"
    cu = mod.context_usage()                  # size still tracked (GUI shows it)
    assert cu["tokens"] > 0 and cu["capacity"] == 0, cu
    print("ok e: no capacity -> no compaction/stop, but size still reported")

    # (f) coding agent: capacity-based compaction (keep system + recent), 0 = off
    import coding_agent
    _cad = tempfile.mkdtemp(prefix="ca_iso_")      # isolate session storage
    coding_agent.HISTORY_PATH = os.path.join(_cad, "chat_history.json")
    coding_agent.SUMMARY_PATH = os.path.join(_cad, "chat_summary.txt")
    coding_agent.SESSIONS_DIR = os.path.join(_cad, "sessions")
    ca = coding_agent.CodingAgent()
    ca._summarize_lines = lambda lines: "CODING-SUMMARY"   # no network
    msgs = [{"role": "system", "content": "SYSTEM PROMPT"}]
    for i in range(14):
        msgs.append({"role": "assistant", "content": f"a{i}",
                     "tool_calls": [{"id": f"c{i}",
                                     "function": {"name": "save_tool"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "z" * 200})
    n_before = len(msgs)
    ca._compact_to_capacity(msgs, capacity=500)
    assert len(msgs) < n_before
    # summary folded INTO the system prompt; system text preserved, not orphaned
    assert msgs[0]["role"] == "system" and msgs[0]["content"].startswith("SYSTEM PROMPT")
    assert "CODING-SUMMARY" in msgs[0]["content"]
    assert msgs[1].get("role") != "tool"
    assert ca.context_capacity == 500 and ca.last_context_tokens > 0
    # capacity 0 -> no compaction, but the size is still tracked (GUI shows it)
    msgs2 = [{"role": "system", "content": "S"}] + [{"role": "user", "content": "u"}] * 40
    n2 = len(msgs2)
    ca._compact_to_capacity(msgs2, capacity=0)
    assert len(msgs2) == n2, "capacity 0 must not compact"
    assert ca.context_capacity == 0 and ca.last_context_tokens > 0
    print("ok f: coding agent compacts to capacity (system kept); 0 = off but sized")

    print("\nALL CONTEXT-CAPACITY CHECKS PASSED")
finally:
    for o in _outs:
        shutil.rmtree(o, ignore_errors=True)
    try:
        os.remove(_BIGTOOL)
    except OSError:
        pass
