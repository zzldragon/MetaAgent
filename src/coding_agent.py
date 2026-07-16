"""The built-in coding agent: writes Python tools in MetaAgent's tool-registry
style (a lightweight, langchain-free @tool decorator).

Memory: the full conversation is kept in chat_history.json so the user can
close the app and keep adjusting tools in a later session (Spec.md "basic
memory system"). Only the most recent turns are sent to the LLM.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import threading
import time
import uuid

from app_config import HISTORY_PATH, TOOLS_DIR, load_config
from llm_client import CANCELLED, LLMClient

CODING_SYSTEM = """You are MetaAgent's built-in coding agent — a senior Python AI-agent \
engineer who is good at PySide6/Qt, matplotlib, pandas and numpy.

Your main job: when the user asks for a tool, write it as a Python function in
MetaAgent's lightweight tool-registry style:

```python
from tool_registry import tool

@tool(risk="safe")
def tool_name(arg: str, count: int = 10) -> str:
    \"\"\"One-line description of what the tool does (an LLM will read this).

    Args:
        arg: what it means.
        count: what it means.
    \"\"\"
    ...
```

Rules:
- One complete, runnable tool per ```python block — no placeholders, no TODOs.
- Type-hint every argument; return a str (agents consume text observations).
- The first docstring line must say what the tool does; it becomes the tool
  description in the agent's prompt.
- Catch exceptions inside the tool and return "[ERROR] ..." strings instead of
  raising, so the calling agent can recover.
- Declare the tool's risk on the decorator so generated agents gate it for human
  review correctly: use @tool(risk="high") for ANYTHING with side effects —
  writes/creates/deletes files, sends/posts over the network, executes commands,
  mutates external state — and @tool(risk="safe") for read-only tools (read,
  fetch, compute, format). This is authoritative over the agent's name-based
  guess, so a read-only tool named "update_*" won't needlessly prompt and a
  destructive tool with an innocuous name still will.
- Prefer the standard library; if you need pandas/numpy/etc., say so above the
  code block.
- When the user asks to adjust a previous tool, output the full revised tool,
  not a diff.

You manage the MetaAgent tool library through native function calls:
- list_tools(): see what is in the library
- read_tool(name): read a tool's current source before modifying it
- save_tool(name, code): save a finished tool into the library

When the user asks for a new tool, present the code in a ```python block and
ask whether to save it; call save_tool once they confirm (or immediately if
they ask you to save). When asked to change an existing tool, call read_tool
first, then save the full revised version.

You may also answer general agent-engineering questions, concisely.

Language: match the user's language in everything you generate for them — tool
docstrings/descriptions and your chat replies. If the user writes in Chinese,
respond and write the generated text in Chinese; Japanese -> Japanese, French ->
French; otherwise default to English. (Keep code identifiers and Python keywords
in English regardless.)
"""

MAX_TOOL_ROUNDS = 6

TOOL_DEFS = [
    {"type": "function", "function": {
        "name": "list_tools",
        "description": "List the tool files currently in the library.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "read_tool",
        "description": "Read the source code of a tool in the library.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string",
                     "description": "Tool name (file name without .py)"}},
            "required": ["name"]},
    }},
    {"type": "function", "function": {
        "name": "save_tool",
        "description": "Create or overwrite a tool in the library. The code "
                       "must be one complete tool_registry-style @tool function.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"},
            "code": {"type": "string"}},
            "required": ["name", "code"]},
    }},
]


def _safe_name(name: str) -> str:
    if name.endswith(".py"):
        name = name[:-3]
    return re.sub(r"\W+", "_", name).strip("_")


def _list_tools() -> str:
    if not os.path.isdir(TOOLS_DIR):
        return "(library is empty)"
    files = sorted(f for f in os.listdir(TOOLS_DIR) if f.endswith(".py"))
    return "\n".join(files) if files else "(library is empty)"


def _read_tool(name: str) -> str:
    path = os.path.join(TOOLS_DIR, f"{_safe_name(name)}.py")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return (f"[ERROR] No such tool: {_safe_name(name)}.py — "
                "call list_tools to see the library.")


def _save_tool(name: str, code: str) -> str:
    safe = _safe_name(name)
    if not safe:
        return "[ERROR] Invalid tool name."
    try:
        compile(code, f"{safe}.py", "exec")
    except SyntaxError as e:
        return f"[ERROR] The code has a syntax error, fix and retry: {e}"
    os.makedirs(TOOLS_DIR, exist_ok=True)
    path = os.path.join(TOOLS_DIR, f"{safe}.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(code if code.endswith("\n") else code + "\n")
    return f"Saved to {path}"


LOCAL_TOOLS = {
    "list_tools": _list_tools,
    "read_tool": _read_tool,
    "save_tool": _save_tool,
}

# ── HITL: tools that modify disk require confirmation (Pillar 9) ────────────
HIGH_RISK_TOOLS = {"save_tool"}


def _default_confirm(tool_name: str, args: dict) -> bool:
    """Console fallback; the GUI installs a review dialog instead."""
    preview = json.dumps(args, ensure_ascii=False)[:300]
    try:
        return input(f"Allow {tool_name}({preview})? [y/N]: ") \
            .strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


_CONFIRM = {"fn": _default_confirm}


def set_confirm_handler(fn) -> None:
    """fn(tool_name, args) -> bool. The Qt Tool Generator installs a review dialog."""
    _CONFIRM["fn"] = fn


def reset_confirm_handler() -> None:
    """Restore the default (console) confirm handler. The GUI calls this when its
    Tool Generator window is torn down, so a bound method of a destroyed window
    never lingers in this process-global slot."""
    _CONFIRM["fn"] = _default_confirm


def confirm_tool(tool_name: str, args: dict, high_risk=HIGH_RISK_TOOLS) -> bool:
    if tool_name not in high_risk:
        return True
    if not load_config().get("hitl_confirm", True):
        return True
    return bool(_CONFIRM["fn"](tool_name, args))

# How many recent messages are sent to the LLM each turn.
CONTEXT_WINDOW_MSGS = 40
# Cap on what we persist to disk.
HISTORY_CAP = 400

# Capacity-based in-request compaction (config "context_capacity", 0 = off). As a
# request nears the model's window, the OLDER messages are folded into one summary,
# keeping the system prompt + the most recent KEEP_RECENT_CTX messages.
KEEP_RECENT_CTX = 10           # most recent messages kept verbatim when compacting
COMPACT_TRIGGER = 0.85         # compact once estimate exceeds this fraction of usable
OUTPUT_RESERVE = 4096          # headroom (tokens) reserved for the model's reply.
# (Generated agents subtract their per-agent max_output_tokens here; the coding
#  agent has no per-agent budget, so a fixed reserve is intentional.)

# Rolling-summary compaction: turns older than the recent window are folded into
# a running SUMMARY (one LLM call) instead of being dropped, so a long session
# keeps earlier context instead of silently truncating it.
SUMMARY_PATH = os.path.join(os.path.dirname(HISTORY_PATH), "chat_summary.txt")
# Named, recoverable conversations: one JSON per session under sessions/.
SESSIONS_DIR = os.path.join(os.path.dirname(HISTORY_PATH), "sessions")
KEEP_RECENT_MSGS = 20      # turns kept verbatim after a compaction
COMPACT_AT_MSGS = 40       # compact once history grows past this
SUMMARIZE_SYS = (
    "You compress conversation history for an AI coding agent. Merge the prior "
    "summary (if any) with the older turns below into ONE concise summary that "
    "preserves facts, decisions, tool/file names, and unresolved threads needed "
    "to continue the work. Output only the summary, with no preamble.")


def _est_tokens(messages: list[dict]) -> int:
    """Rough token estimate (chars // 3) over message content, for the
    context-size guard. Mirrors the generated agents' text-only estimate."""
    return sum(len(str(m.get("content") or "")) for m in messages) // 3


def _compact_cut(messages: list[dict]) -> int:
    """Index where the kept recent tail should start when compacting: the last
    KEEP_RECENT_CTX messages, moved forward so the tail never begins with an orphan
    'tool' result (whose owning assistant turn would be summarized away). Returns a
    cut in (1, len) or <=1 if there's nothing worth compacting."""
    if len(messages) <= KEEP_RECENT_CTX + 1:
        return 1
    cut = len(messages) - KEEP_RECENT_CTX
    while cut < len(messages) and messages[cut].get("role") == "tool":
        cut += 1
    return cut if 1 < cut < len(messages) else 1


CODE_BLOCK_RE = re.compile(r"```(?:python)?\s*\n(.*?)```", re.DOTALL)
DEF_NAME_RE = re.compile(r"def\s+(\w+)\s*\(")


def _atomic_write(path: str, text: str) -> None:
    """Write via a temp file + os.replace so a crash mid-write can't leave a
    truncated active.txt / session JSON behind. On Windows os.replace transiently
    fails with PermissionError (ERROR_ACCESS_DENIED) when an AV/search-indexer
    momentarily holds the destination — retry briefly, then fall back to an
    in-place write (these files are small and self-healing)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    for i in range(5):
        try:
            os.replace(tmp, path)
            return
        except OSError:
            if i == 4:
                break
            time.sleep(0.05 * (i + 1))
    try:                                  # last resort: direct (non-atomic) write
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


class CodingAgent:
    def __init__(self, *, system_prompt=None, local_tools=None, tool_defs=None,
                 high_risk=None, storage_dir=None, max_tool_rounds=None):
        # Injectable so a second agent (e.g. the graph Designer) reuses this chat/
        # session core with its OWN brain, tools and ISOLATED storage. Defaults =
        # the coding agent's globals, so `CodingAgent()` is byte-for-byte as before.
        # max_tool_rounds caps the agentic tool-call loop per turn: None => the
        # module default (MAX_TOOL_ROUNDS); <= 0 => UNLIMITED (run until the model
        # stops calling tools or the user cancels).
        self._max_tool_rounds = (MAX_TOOL_ROUNDS if max_tool_rounds is None
                                 else int(max_tool_rounds))
        self._system = system_prompt if system_prompt is not None else CODING_SYSTEM
        self._local_tools = local_tools if local_tools is not None else LOCAL_TOOLS
        self._tool_defs = tool_defs if tool_defs is not None else TOOL_DEFS
        self._high_risk = high_risk if high_risk is not None else HIGH_RISK_TOOLS
        # storage: separate dir => separate history/summary/sessions (no sharing)
        if storage_dir:
            os.makedirs(storage_dir, exist_ok=True)
            self._history_path = os.path.join(storage_dir, "chat_history.json")
            self._summary_path = os.path.join(storage_dir, "chat_summary.txt")
            self._sessions_dir = os.path.join(storage_dir, "sessions")
        else:
            self._history_path, self._summary_path = HISTORY_PATH, SUMMARY_PATH
            self._sessions_dir = SESSIONS_DIR
        # serialize session-file writes (send worker thread) vs clear/new (GUI
        # thread); created first because _load_active() may persist a migration.
        self._io_lock = threading.RLock()
        self.history: list[dict] = []
        self.summary: str = ""             # rolling summary of old turns
        self.session_id: str = ""
        self._load_active()                # populates session_id, history, summary
        self.usage = {"input_tokens": 0, "output_tokens": 0}  # session totals
        self._cancel = threading.Event()
        self._active_client = None       # current send()'s client, for cancel()
        self.context_capacity = 0        # model window (config); 0 = no control
        self.last_context_tokens = 0     # last request's input estimate (for the GUI)

    def cancel(self) -> None:
        """Stop the in-flight send() ASAP. Thread-safe: sets the flag AND force-
        closes the active LLM stream so a blocked read (e.g. before the first
        token) aborts at once instead of running to completion."""
        self._cancel.set()
        c = self._active_client
        if c is not None:
            c.cancel()

    # ── config / client ───────────────────────────────────────────────────
    def _client(self) -> LLMClient:
        cfg = load_config()
        return LLMClient(
            api_key=cfg["api_key"],
            base_url=cfg["base_url"],
            model=cfg["model"],
            request_timeout_s=cfg["request_timeout_s"],   # always merged from DEFAULTS
            proxy=cfg.get("proxy") or None,               # config proxy, else env / direct
        )

    @staticmethod
    def has_api_key() -> bool:
        return bool(str(load_config().get("api_key") or "").strip())

    # ── sessions (named, recoverable conversations) ─────────────────────────
    @staticmethod
    def _new_session_id() -> str:
        return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]

    def _session_path(self, sid: str) -> str:
        return os.path.join(self._sessions_dir, sid + ".json")

    def _active_id_path(self) -> str:
        return os.path.join(self._sessions_dir, "active.txt")

    def _read_session(self, sid: str) -> dict:
        try:
            with open(self._session_path(sid), encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _all_sessions(self) -> list[dict]:
        """Saved session records, newest-updated first."""
        try:
            names = [n for n in os.listdir(self._sessions_dir) if n.endswith(".json")]
        except OSError:
            names = []
        recs = [self._read_session(n[:-5]) for n in names]
        recs = [r for r in recs if r.get("id")]
        recs.sort(key=lambda r: r.get("updated", ""), reverse=True)
        return recs

    def _write_active_id(self) -> None:
        try:
            _atomic_write(self._active_id_path(), self.session_id)
        except OSError:
            pass

    def _session_title(self) -> str:
        for h in self.history:
            if h.get("role") == "user":
                t = " ".join(str(h.get("content", "")).split())
                return (t[:50] + "…") if len(t) > 50 else (t or "(new session)")
        return "(new session)"

    def _load_active(self) -> None:
        """Load the active session into history/summary: the saved active id, else
        the most recent session, else a migrated legacy chat_history.json, else a
        fresh empty session. Sets self.session_id."""
        sid = ""
        try:
            with open(self._active_id_path(), encoding="utf-8") as f:
                sid = f.read().strip()
        except OSError:
            sid = ""
        if not (sid and os.path.isfile(self._session_path(sid))):
            recents = self._all_sessions()
            sid = recents[0]["id"] if recents else ""
        if sid and os.path.isfile(self._session_path(sid)):
            rec = self._read_session(sid)
            self.session_id = sid
            self.history = rec.get("history") if isinstance(rec.get("history"), list) else []
            self.summary = rec.get("summary") or ""
            return
        # migrate a pre-sessions conversation, if present
        try:
            with open(self._history_path, encoding="utf-8") as f:
                legacy = json.load(f)
        except (OSError, json.JSONDecodeError):
            legacy = []
        if isinstance(legacy, list) and legacy:
            try:
                with open(self._summary_path, encoding="utf-8") as f:
                    self.summary = f.read()
            except OSError:
                self.summary = ""
            self.history = legacy
            self.session_id = self._new_session_id()
            self._save_session()
            for p in (self._history_path, self._summary_path):  # mark legacy migrated
                try:
                    os.replace(p, p + ".migrated")
                except OSError:
                    pass
            return
        self.session_id = self._new_session_id()      # fresh (no file until 1st turn)

    def _save_session(self) -> None:
        """Persist the active session (cap to HISTORY_CAP; skip empty ones). Locked
        so a concurrent clear_memory can't be raced (clear empties history first, so
        a write that loses the race simply finds nothing to save)."""
        with self._io_lock:
            if not self.session_id:
                return
            self.history = self.history[-HISTORY_CAP:]
            if not self.history and not self.summary:
                return
            now = time.strftime("%Y-%m-%d %H:%M:%S")
            created = self._read_session(self.session_id).get("created", now)
            rec = {"id": self.session_id, "title": self._session_title(),
                   "created": created, "updated": now,
                   "history": self.history, "summary": self.summary}
            _atomic_write(self._session_path(self.session_id),
                          json.dumps(rec, indent=2, ensure_ascii=False))
            self._write_active_id()

    # save_history / save_summary persist the active session (call sites unchanged)
    def _save_history(self) -> None:
        self._save_session()

    def _save_summary(self) -> None:
        self._save_session()

    def clear_memory(self) -> None:
        """Wipe the active session's content (it stays active, now empty)."""
        with self._io_lock:
            self.history = []
            self.summary = ""
            try:
                os.remove(self._session_path(self.session_id))   # empty -> prune file
            except OSError:
                pass

    def clear_all_sessions(self) -> int:
        """Delete ALL saved sessions (full history wipe) and start a fresh empty
        session. Returns how many stored sessions were removed."""
        with self._io_lock:
            removed = 0
            for r in self._all_sessions():
                try:
                    os.remove(self._session_path(r["id"]))
                    removed += 1
                except OSError:
                    pass
            self.history = []
            self.summary = ""
            self.session_id = self._new_session_id()
            self._write_active_id()
            return removed

    def current_session(self) -> str:
        return self.session_id

    def list_sessions(self) -> list[dict]:
        """Recent sessions, newest first: [{id,title,updated,turns,active}].
        Includes the active session even before its first turn."""
        active, seen, out = self.session_id, set(), []
        for r in self._all_sessions():
            out.append({"id": r["id"], "title": r.get("title") or "(session)",
                        "updated": r.get("updated", ""),
                        "turns": len(r.get("history") or []),
                        "active": r["id"] == active})
            seen.add(r["id"])
        if active and active not in seen:
            out.insert(0, {"id": active, "title": self._session_title(),
                           "updated": "", "turns": len(self.history), "active": True})
        return out

    def new_session(self) -> str:
        """Start a fresh session. The current one is saved continuously, so nothing
        is lost; an empty current session leaves no file behind."""
        with self._io_lock:
            self._save_session()
            self.history = []
            self.summary = ""
            self.session_id = self._new_session_id()
            self._write_active_id()
            return self.session_id

    def load_session(self, sid: str) -> bool:
        """Recover a saved session as the active conversation. False if absent."""
        rec = self._read_session(sid)
        if not rec.get("id"):
            return False
        with self._io_lock:
            self._save_session()                      # persist the one we're leaving
            self.history = rec.get("history") if isinstance(rec.get("history"), list) else []
            self.summary = rec.get("summary") or ""
            self.session_id = sid
            self._write_active_id()
        return True

    # ── compaction (fold old turns into a running summary) ──────────────────
    def _summarize_overflow(self, prior: str, turns: list[dict]) -> str:
        """Fold older turns into a running summary via one LLM call. Returns the
        new summary, or "" on failure (the caller then skips compaction)."""
        lines = [f"{'User' if h.get('role') == 'user' else 'Agent'}: "
                 f"{h.get('content', '')}" for h in turns]
        user = ((f"Prior summary:\n{prior}\n\n" if prior else "")
                + "Older turns to fold in:\n" + "\n".join(lines))
        try:
            client = self._client()
            msg = client.chat([{"role": "system", "content": SUMMARIZE_SYS},
                               {"role": "user", "content": user}])
            u = getattr(client, "last_usage", None)
            if u is not None:
                self.usage["input_tokens"] += getattr(u, "prompt_tokens", 0) or 0
                self.usage["output_tokens"] += getattr(u, "completion_tokens", 0) or 0
            return (getattr(msg, "content", "") or "").strip()
        except Exception:
            return ""

    def _maybe_compact(self, emit=lambda s: None) -> None:
        """When history outgrows COMPACT_AT_MSGS, fold the overflow into the
        summary and keep only the most recent turns. Best-effort: on a failed
        summary call, leave history intact (HISTORY_CAP is the hard backstop)."""
        if len(self.history) <= COMPACT_AT_MSGS:
            return
        old = self.history[:-KEEP_RECENT_MSGS]
        new_summary = self._summarize_overflow(self.summary, old)
        if not new_summary:
            return                          # summarization failed; retry next turn
        self.summary = new_summary
        self.history = self.history[-KEEP_RECENT_MSGS:]
        self._save_summary()
        self._save_history()
        emit(f"[memory] folded {len(old)} older turn(s) into the summary")

    def _compact_to_capacity(self, messages: list[dict], capacity: int,
                             emit=lambda s: None) -> None:
        """Capacity-based, per-request compaction (Cursor/Claude-style). When the
        estimated input nears `capacity`, fold the OLDER messages into one summary,
        keeping messages[0] (the system prompt) and the most recent KEEP_RECENT_CTX.
        Best-effort: if the summary LLM call fails, drop the middle instead (still
        fits). capacity <= 0 disables it entirely. Tracks last_context_tokens."""
        self.context_capacity = capacity
        est = _est_tokens(messages)
        self.last_context_tokens = est
        if capacity <= 0:
            return
        usable = max(capacity - OUTPUT_RESERVE, capacity // 2)
        if est <= usable * COMPACT_TRIGGER:
            return
        cut = _compact_cut(messages)
        if cut <= 1:
            return
        middle = messages[1:cut]
        lines = []
        for m in middle:
            role = m.get("role", "?")
            text = str(m.get("content") or "").strip()
            if m.get("tool_calls"):
                names = ", ".join(
                    tc.get("function", {}).get("name", "?") for tc in m["tool_calls"])
                text = (text + " [called: " + names + "]").strip()
            if text:
                lines.append(role + ": " + text)
        summary = self._summarize_lines(lines)
        # Fold the summary INTO the system prompt (messages[0]); never insert a new
        # user turn — that could break user/assistant alternation. Then drop the
        # now-summarized middle.
        if summary and messages and messages[0].get("role") == "system":
            messages[0] = {**messages[0],
                           "content": str(messages[0].get("content") or "")
                           + "\n\n## Compacted earlier conversation\n" + summary}
        del messages[1:cut]
        self.last_context_tokens = _est_tokens(messages)
        emit(f"[context compacted: ~{est} -> ~{self.last_context_tokens} tok "
             f"(capacity {capacity})]" if summary else
             f"[context TRIMMED: summary failed, dropped oldest turns "
             f"(~{est} -> ~{self.last_context_tokens} tok)]")

    def _summarize_lines(self, lines: list) -> str:
        """One LLM call to compress pre-formatted older turns into a summary; ""
        on failure. A direct call (not _summarize_overflow) so the tool-call names
        captured in `lines` are preserved verbatim."""
        if not lines:
            return ""
        try:
            client = self._client()
            msg = client.chat([{"role": "system", "content": SUMMARIZE_SYS},
                               {"role": "user", "content":
                                "Older turns to compact:\n" + "\n".join(lines)}])
            u = getattr(client, "last_usage", None)
            if u is not None:
                self.usage["input_tokens"] += getattr(u, "prompt_tokens", 0) or 0
                self.usage["output_tokens"] += getattr(u, "completion_tokens", 0) or 0
            return (getattr(msg, "content", "") or "").strip()
        except Exception:
            return ""

    # ── chat (native function-calling loop) ───────────────────────────────
    def send(self, user_text: str, emit=None) -> str:
        """Send one user turn; runs the agentic loop (the model may call
        list_tools / read_tool / save_tool) and returns the final reply.
        `emit` receives live tool-call traces. Blocking."""
        emit = emit or (lambda s: None)
        self._cancel.clear()
        self._maybe_compact(emit)        # fold overflow into the running summary
        self.history.append({"role": "user", "content": user_text})
        sys_content = self._system
        if self.summary:
            sys_content += "\n\n## Summary of earlier conversation\n" + self.summary
        messages = [{"role": "system", "content": sys_content}]
        messages += self.history[-CONTEXT_WINDOW_MSGS:]
        capacity = int(load_config().get("context_capacity", 0) or 0)
        self._compact_to_capacity(messages, capacity, emit)   # fold old turns to fit
        client = self._client()
        self._active_client = client     # so cancel() can force-close its stream
        reply = "(stopped after too many tool rounds)"
        completed = False        # only a real final answer is persisted
        in0, out0 = self.usage["input_tokens"], self.usage["output_tokens"]

        # <= 0 => unlimited rounds (the loop still exits when the model stops
        # calling tools, or when the user cancels); else a fixed cap.
        rounds = (itertools.count() if self._max_tool_rounds <= 0
                  else range(self._max_tool_rounds))
        for _ in rounds:
            if self._cancel.is_set():
                reply = "[cancelled] stopped by the user"
                break
            # Tool results accumulate across rounds; recompact toward capacity
            # before each call (no hard stop). capacity 0 = no control.
            self._compact_to_capacity(messages, capacity, emit)
            msg = client.chat(messages, tools=self._tool_defs,
                               should_cancel=self._cancel.is_set)
            if msg is CANCELLED:           # Stop pressed mid-response
                reply = "[cancelled] stopped by the user"
                break
            u = getattr(client, "last_usage", None)
            if u is not None:
                self.usage["input_tokens"] += getattr(u, "prompt_tokens", 0) or 0
                self.usage["output_tokens"] += getattr(u, "completion_tokens", 0) or 0
            calls = list(msg.tool_calls or [])
            entry = {"role": "assistant", "content": msg.content or ""}
            if calls:
                entry["tool_calls"] = [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name,
                                  "arguments": tc.function.arguments or "{}"}}
                    for tc in calls]
            messages.append(entry)
            if not calls:
                reply = msg.content or ""
                completed = True
                break

            for tc in calls:
                if self._cancel.is_set():    # Stop pressed between tool calls
                    break                    # outer-loop check ends the run next
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                emit(f"[tool] {tc.function.name}("
                     f"{json.dumps(args, ensure_ascii=False)[:120]})")
                fn = self._local_tools.get(tc.function.name)
                if fn is None:
                    result = f"[ERROR] Unknown tool '{tc.function.name}'."
                elif not confirm_tool(tc.function.name, args, self._high_risk):
                    result = ("[denied] The user rejected this tool call. "
                              "Do not retry unless they ask.")
                else:
                    try:
                        result = fn(**args)
                    except TypeError as e:
                        result = f"[ERROR] Bad arguments: {e}"
                emit(f"[tool result] {str(result)[:200]}")
                messages.append({"role": "tool", "tool_call_id": tc.id,
                                 "content": str(result)})

        self._active_client = None       # send finished; nothing to cancel now
        # Persist only a completed exchange. A cancelled / budget-capped /
        # round-exhausted run rolls back the user turn so its synthetic reply
        # ("[cancelled]" / "[budget]" / "(stopped ...)") never re-enters the
        # next turn's context.
        if completed:
            self.history.append({"role": "assistant", "content": reply})
        else:
            self.history.pop()       # remove the user turn appended above
        self._save_history()
        di = self.usage["input_tokens"] - in0
        do = self.usage["output_tokens"] - out0
        note = "" if (di or do) else "  (provider didn't report token usage)"
        cap, tok = self.context_capacity, self.last_context_tokens
        if cap:
            ctx_str = "  |  context: ~{:.1f}k / {}k".format(tok / 1000, cap // 1000)
        elif tok:
            ctx_str = "  |  context: ~{:.1f}k".format(tok / 1000)
        else:
            ctx_str = ""
        emit(f"[usage] this run: {di} in + {do} out tokens  |  session: "
             f"{self.usage['input_tokens']} in + "
             f"{self.usage['output_tokens']} out{note}{ctx_str}")
        return reply

    # ── tool extraction ───────────────────────────────────────────────────
    def extract_tools_from_last_reply(self) -> list[tuple[str, str]]:
        """Return [(tool_name, source_code)] from the last assistant message."""
        for msg in reversed(self.history):
            if msg["role"] == "assistant":
                tools = []
                for code in CODE_BLOCK_RE.findall(msg["content"]):
                    m = DEF_NAME_RE.search(code)
                    if m:
                        tools.append((m.group(1), code.strip() + "\n"))
                return tools
        return []

    @staticmethod
    def save_tool(name: str, code: str) -> str:
        """Write a tool into the tools/ library (the GUI "Save Tool(s)" path).

        Delegates to the same validated writer as the LLM-driven path
        (``_save_tool``): name sanitizing, a syntax check that rejects broken
        code, and a guaranteed trailing newline. Returns ``"Saved to <path>"``
        on success or ``"[ERROR] ..."`` on failure — callers must check the
        prefix and not assume the file was written.
        """
        return _save_tool(name, code)
