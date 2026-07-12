"""Verify per-session isolation + CONCURRENCY (Phase 2e).

run(session_id=…) forks an isolated run state (own history, usage, cancel, HITL)
so simultaneous web/gateway users run concurrently without sharing anything; each
session's conversation is persisted independently. A run with NO session_id uses
the default single active session (GUI/CLI) — byte-identical to before. Offline
seam: generate an agent, import it, stub `_call_one`.
"""

import importlib.util
import os
import shutil
import sys
import threading
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import graph_codegen
from graph_model import Graph

LLM = {"provider": "siliconflow", "model": "deepseek-ai/DeepSeek-V4-Flash",
       "api_key": "", "base_url": "https://api.siliconflow.cn/v1"}


def _agent_graph():
    g = Graph()
    a = g.new_node("agent", 0, 0); a.name = "agent"; a.props["role"] = "single"
    lm = g.new_node("llm", 0, 0); lm.props.update(LLM); g.add_edge(lm.id, a.id)
    return g


def _texts(rec):
    return [h["content"] for h in (rec.get("history") or [])]


_quiet = lambda s: None
out = graph_codegen.generate_from_graph(_agent_graph(), "sess_iso")
try:
    spec = importlib.util.spec_from_file_location("sess_iso_agent",
                                                  os.path.join(out, "agent.py"))
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    mod._call_one = lambda agent_name, cfg, system, messages: ("ok", [])

    # ── 1. A forked session persists its own history; default untouched ──────
    base_hist = list(mod.HISTORY)
    mod.run("hello from A", emit=_quiet, session_id="sessA")
    mod.run("hello from B", emit=_quiet, session_id="sessB")
    assert _texts(mod._read_session("sessA")) == ["hello from A", "ok"], mod._read_session("sessA")
    assert _texts(mod._read_session("sessB")) == ["hello from B", "ok"]
    assert "hello from A" not in str(mod._read_session("sessB"))
    # the default (GUI/CLI) session was NOT disturbed by the forked web runs
    assert list(mod.HISTORY) == base_hist, "forked run leaked into the default session"
    print("1. forked sessions persist isolated history; default untouched ok")

    # ── 2. A session accumulates across turns (reloads its own prior context) ─
    mod.run("more from A", emit=_quiet, session_id="sessA")
    assert _texts(mod._read_session("sessA")) == ["hello from A", "ok", "more from A", "ok"]
    seen = {}
    mod._call_one = lambda agent_name, cfg, system, messages: (
        seen.__setitem__("q", messages[-1]["content"]), ("ok", []))[1]
    mod.run("q3", emit=_quiet, session_id="sessA")
    assert "hello from A" in seen["q"] and "hello from B" not in seen["q"], seen["q"]
    mod._call_one = lambda agent_name, cfg, system, messages: ("ok", [])
    print("2. a session reloads its own prior turns as context; no cross-session bleed ok")

    # ── 3. No session_id → the default active session (today's behaviour) ────
    active = mod.current_session()
    mod.run("plain turn", emit=_quiet)
    assert mod.current_session() == active, "blank session_id must not switch active"
    assert "plain turn" in [h["content"] for h in mod.HISTORY], mod.HISTORY
    print("3. blank session_id uses the default session ok")

    # ── 4. Colon-bearing gateway ids (illegal Windows filename) round-trip ───
    cid = "dingtalk:conv42:staffZ"
    mod.run("via gateway", emit=_quiet, session_id=cid)
    assert _texts(mod._read_session(cid)) == ["via gateway", "ok"], mod._read_session(cid)
    assert any(s["id"] == cid for s in mod.list_sessions())
    print("4. colon-bearing gateway id escapes + round-trips ok")

    # ── 5. TRUE CONCURRENCY: many sessions run at once, no cross-contamination ─
    N, NAP = 8, 0.2                          # sleep dominates so overlap is visible
    def _slow(agent_name, cfg, system, messages):
        time.sleep(NAP)                      # I/O-bound: releases the GIL, so
        return ("done", [])                  #   concurrent runs' naps overlap
    mod._call_one = _slow

    # Wall-clock overlap is load-sensitive (GIL scheduling + whatever else the box is
    # doing), so a single sample is flaky; retry a few times and pass if ANY attempt
    # shows clear overlap. Isolation (the real invariant) is asserted EVERY attempt.
    _elapsed = None
    for _attempt in range(4):
        ids = [f"cc-{_attempt}-{i}" for i in range(N)]   # fresh ids -> clean history each try
        errs = []

        def _worker(sid):
            try:
                mod.run(f"task for {sid}", emit=_quiet, session_id=sid)
            except Exception as e:           # noqa: BLE001
                errs.append((sid, repr(e)))

        ts = [threading.Thread(target=_worker, args=(s,)) for s in ids]
        t0 = time.time()
        for t in ts: t.start()
        for t in ts: t.join()
        _elapsed = time.time() - t0
        assert not errs, errs
        for sid in ids:                      # ISOLATION: each session kept ONLY its turn
            h = _texts(mod._read_session(sid))
            assert h == [f"task for {sid}", "done"], (sid, h)
        if _elapsed < N * NAP * 0.6:         # the naps overlapped -> well under serial
            break
    # CONCURRENCY: overlapped (well under the serial floor N×NAP) on at least one try.
    assert _elapsed < N * NAP * 0.6, \
        f"runs did not overlap after retries (last {_elapsed:.2f}s vs serial {N*NAP:.2f}s)"
    print(f"5. {N} concurrent sessions isolated + overlapped "
          f"({_elapsed:.2f}s vs {N*NAP:.2f}s serial) ok")

    # ── 5b. Per-run HITL lock: a blocking confirm in one session must NOT block
    #        another session's confirm (would, if the lock were process-global) ──
    mod._call_one = lambda agent_name, cfg, system, messages: ("ok", [])
    mod.CONFIG["hitl_confirm"] = True
    mod.CONFIG["high_risk_tools"] = ["danger"]
    gate = threading.Event()
    order = []

    def _blocking(tool, args):
        order.append("A-start"); gate.wait(3.0); order.append("A-done")
        return {"decision": "allow"}

    def _quick(tool, args):
        order.append("B-done"); return {"decision": "allow"}

    stA = mod._RunState.fresh("sidA"); stA.confirm["fn"] = _blocking
    stB = mod._RunState.fresh("sidB"); stB.confirm["fn"] = _quick

    def _in(st, fn):
        mod._CURRENT_RUN.set(st); fn()

    tA = threading.Thread(target=_in, args=(stA, lambda: mod.confirm_tool("danger", {})))
    tB = threading.Thread(target=_in, args=(stB, lambda: mod.confirm_tool("danger", {})))
    tA.start()
    while "A-start" not in order:              # ensure A holds ITS lock first
        time.sleep(0.005)
    tB.start(); tB.join(3.0)
    assert "B-done" in order and "A-done" not in order, \
        f"session B's confirm was blocked by A's — lock is not per-run: {order}"
    gate.set(); tA.join(3.0)
    print("5b. per-run HITL lock: one session's pending confirm doesn't block another ok")

    # ── 5c. Workspace skills are per-run (no cross-session prompt leak) ───────
    stC = mod._RunState.fresh("sidC")
    stC.ws_skills[:] = [{"name": "cskill", "description": "C-only workspace skill"}]
    _leak = {}

    def _chk_c():
        mod._CURRENT_RUN.set(stC)
        _leak["c"] = "cskill" in mod.skills_block(mod.ENTRY)

    tc = threading.Thread(target=_chk_c); tc.start(); tc.join(3.0)
    assert _leak.get("c"), "session C's own workspace skill missing from its prompt"
    assert "cskill" not in mod.skills_block(mod.ENTRY), \
        "session C's workspace skill leaked into the default session's prompt"
    print("5c. workspace skills are per-run (no cross-session leak) ok")

    # ── 6. Generated server.py wires per-connection session_id ───────────────
    gw = _agent_graph()
    _wsn = gw.new_node("webserver", 0, 0); _wsn.name = "srv"
    _entry = next(n for n in gw.nodes.values() if n.kind == "agent")
    gw.add_edge(_entry.id, _wsn.id)
    out2 = graph_codegen.generate_from_graph(gw, "sess_ws", gui=False)
    try:
        src = open(os.path.join(out2, "server.py"), encoding="utf-8").read()
        assert "conn_sid" in src and "session_id=session_id" in src
        print("6. generated server.py wires per-connection session_id ok")
    finally:
        shutil.rmtree(out2, ignore_errors=True)
finally:
    shutil.rmtree(out, ignore_errors=True)

print("\nALL SESSION-ISOLATION CHECKS PASSED")
