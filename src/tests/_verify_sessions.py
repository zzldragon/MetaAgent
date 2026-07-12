"""Verify multi-session support (recent sessions / recover / new session) for BOTH
the generated-agent runtime and the coding agent — offline, no LLM. Covers:
  (a) new_session / list_sessions / load_session round-trip;
  (b) legacy single-conversation history is migrated to a session on first load;
  (c) empty sessions are not persisted (no clutter);
  (d) the active session is always listed (even before its first turn)."""
import importlib.util
import json
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


def _files(d):
    try:
        return sorted(n for n in os.listdir(d) if n.endswith(".json"))
    except OSError:
        return []


# ── 1. generated-agent runtime ─────────────────────────────────────────────
g = Graph()
a = g.new_node("agent", 0, 0); a.name = "agent"; a.props["role"] = "single"
lm = g.new_node("llm", 0, 0); lm.props.update(LLM); g.add_edge(lm.id, a.id)
out = graph_codegen.generate_from_graph(g, "sess_gen")
try:
    # seed a LEGACY conversation before importing (so migration runs on load)
    with open(os.path.join(out, "history.json"), "w", encoding="utf-8") as f:
        json.dump([{"role": "user", "content": "the legacy question"},
                   {"role": "assistant", "content": "the legacy answer"}], f)
    with open(os.path.join(out, "history_summary.txt"), "w", encoding="utf-8") as f:
        f.write("legacy summary")

    spec = importlib.util.spec_from_file_location("sess_gen_agent",
                                                  os.path.join(out, "agent.py"))
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)

    # (b) legacy migrated into one active session
    s = mod.list_sessions()
    assert len(s) == 1 and s[0]["active"] and "legacy question" in s[0]["title"], s
    assert mod.HISTORY[0]["content"] == "the legacy question"
    assert mod.SUMMARY == "legacy summary"
    migrated_id = s[0]["id"]
    # run-once: legacy files are renamed away so a reload can't re-migrate
    assert not os.path.isfile(os.path.join(out, "history.json"))
    assert os.path.isfile(os.path.join(out, "history.json.migrated"))
    print("ok gen-b: legacy conversation migrated once (legacy renamed)")

    # (a/d) new_session -> fresh active, previous still listed
    nid = mod.new_session()
    assert nid != migrated_id and mod.HISTORY == [] and mod.SUMMARY == ""
    s = mod.list_sessions()
    assert {x["id"] for x in s} >= {migrated_id, nid}
    assert any(x["active"] and x["id"] == nid for x in s)
    # (c) the brand-new empty session has NOT been written to disk yet
    assert migrated_id + ".json" in _files(mod.SESSIONS_DIR)
    assert nid + ".json" not in _files(mod.SESSIONS_DIR)
    print("ok gen-a/c/d: new_session is active + empty + unwritten; old one kept")

    # simulate a turn in the new session, then it persists
    mod.HISTORY[:] = [{"role": "user", "content": "brand new chat"},
                      {"role": "assistant", "content": "done"}]
    mod.save_history()
    assert nid + ".json" in _files(mod.SESSIONS_DIR)
    assert any(x["id"] == nid and x["turns"] == 2 and "brand new chat" in x["title"]
               for x in mod.list_sessions())

    # recover the migrated session
    assert mod.load_session(migrated_id) is True
    assert mod.current_session() == migrated_id
    assert mod.HISTORY[0]["content"] == "the legacy question"
    assert mod.load_session("nope-does-not-exist") is False
    print("ok gen: recover restores history; unknown id -> False")
finally:
    shutil.rmtree(out, ignore_errors=True)

# ── 2. coding agent ─────────────────────────────────────────────────────────
import coding_agent as CA

tmp = tempfile.mkdtemp(prefix="ca_sess_")
_orig = (CA.HISTORY_PATH, CA.SUMMARY_PATH, CA.SESSIONS_DIR)
try:
    CA.HISTORY_PATH = os.path.join(tmp, "chat_history.json")
    CA.SUMMARY_PATH = os.path.join(tmp, "chat_summary.txt")
    CA.SESSIONS_DIR = os.path.join(tmp, "sessions")
    # legacy coding-agent conversation
    with open(CA.HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump([{"role": "user", "content": "write a base64 tool"},
                   {"role": "assistant", "content": "here you go"}], f)

    ca = CA.CodingAgent()
    s = ca.list_sessions()
    assert len(s) == 1 and s[0]["active"] and "base64 tool" in s[0]["title"], s
    assert ca.history[0]["content"] == "write a base64 tool"
    migrated = s[0]["id"]
    # run-once: re-constructing reloads the SAME session, never re-migrates
    n_before = len(CA.CodingAgent._all_sessions())
    ca2 = CA.CodingAgent()
    assert ca2.session_id == migrated and len(ca2._all_sessions()) == n_before
    assert not os.path.isfile(CA.HISTORY_PATH)        # legacy renamed away
    print("ok code-b: legacy chat migrated once; reload reuses it")

    nid = ca.new_session()
    assert nid != migrated and ca.history == [] and ca.summary == ""
    assert migrated + ".json" in _files(CA.SESSIONS_DIR)
    assert nid + ".json" not in _files(CA.SESSIONS_DIR)        # empty not written
    # a turn in the new session persists it
    ca.history = [{"role": "user", "content": "now a csv loader"},
                  {"role": "assistant", "content": "ok"}]
    ca._save_session()
    assert nid + ".json" in _files(CA.SESSIONS_DIR)
    assert any(x["id"] == nid and x["turns"] == 2 for x in ca.list_sessions())

    assert ca.load_session(migrated) is True
    assert ca.current_session() == migrated
    assert ca.history[0]["content"] == "write a base64 tool"
    # clear_memory empties + prunes the active session file
    ca.clear_memory()
    assert ca.history == [] and migrated + ".json" not in _files(CA.SESSIONS_DIR)
    assert ca.load_session("ghost") is False
    print("ok code-a/c/d: round-trip, recover, clear prunes, unknown id -> False")
finally:
    CA.HISTORY_PATH, CA.SUMMARY_PATH, CA.SESSIONS_DIR = _orig
    shutil.rmtree(tmp, ignore_errors=True)

print("\nALL SESSION CHECKS PASSED")
