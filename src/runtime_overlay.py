"""Runtime overlay state — a LangGraph-Studio-style live view of a run.

Pure, wx-free: it consumes the structured trace records the generated agents
emit (the same dicts written to traces/*.jsonl and handed to a live sink via
`set_trace_sink`) and turns them into per-node status the canvas can paint.

A node (an agent / worker-pool / router stage, keyed by its canvas name) moves
through:  idle -> running -> done   (or -> error on failure).

The class is deliberately decoupled from how events arrive: the designer feeds
it live records on the GUI thread; tests feed it synthetic sequences.
"""

from __future__ import annotations

# status values, in rough lifecycle order
IDLE, RUNNING, DONE, ERROR = "idle", "running", "done", "error"

# canvas colors per status (border + dot); idle draws no overlay
STATUS_COLOR = {
    RUNNING: "#FFA000",   # amber
    DONE: "#2E7D32",      # green
    ERROR: "#C62828",     # red
}


class RuntimeOverlay:
    """Accumulates run state for a fixed set of stage node names."""

    def __init__(self, node_names):
        self.nodes = {
            name: {"status": IDLE, "step": 0, "tool_calls": 0,
                   "last_tool": "", "route": "", "note": "", "output": ""}
            for name in node_names
        }
        self.active = None          # name of the node currently running
        self._concurrent = set()    # branch nodes of an in-flight fan-out (if any):
                                    # while non-empty the single-cursor edge/finish
                                    # inference is suspended (siblings run at once)
        self.edges = set()          # (src_name, dst_name) transitions taken
        self.active_edge = None     # the most recent transition (flowing now)
        self.finished = False
        self.result = ""
        self.error = ""
        self.events = []            # capped log of (kind, node, detail)
        self.last = ""              # one-line description of the latest event
        self.state = {}             # latest full shared-state snapshot (empty
                                    # for graphs with no declared state)
        self.usage = {}             # per-agent token/tool tally (from run_end)
        self.retries = 0            # run-level retry / failover counts
        self.failovers = 0

    # ── queries (used by the canvas painter) ────────────────────────────────
    def status_of(self, name):
        n = self.nodes.get(name)
        return n["status"] if n else IDLE

    def badge(self, name):
        """Short status text drawn on the node, e.g. '▶ 2' / '✓' / '✗'."""
        n = self.nodes.get(name)
        if not n:
            return ""
        st = n["status"]
        if st == RUNNING:
            return f"▶ {n['step']}" if n["step"] else "▶"
        if st == DONE:
            return "✓"
        if st == ERROR:
            return "✗"
        return ""

    def is_edge_active(self, src, dst):
        """The transition currently flowing (drawn brightest)."""
        return self.active_edge == (src, dst)

    def is_edge_traversed(self, src, dst):
        """Any transition taken so far this run."""
        return (src, dst) in self.edges

    def detail(self, name):
        """Multi-field human summary for a tooltip / side panel."""
        n = self.nodes.get(name)
        if not n:
            return ""
        bits = [f"status: {n['status']}"]
        if n["step"]:
            bits.append(f"step {n['step']}")
        if n["tool_calls"]:
            bits.append(f"{n['tool_calls']} tool call(s)"
                        + (f" (last: {n['last_tool']})" if n["last_tool"] else ""))
        if n["route"]:
            bits.append(f"route → {n['route']}")
        if n["note"]:
            bits.append(n["note"])
        return " · ".join(bits)

    # ── event intake ────────────────────────────────────────────────────────
    def _node_name(self, rec):
        return rec.get("agent") or rec.get("router")

    def _ensure(self, name):
        if name and name not in self.nodes:
            self.nodes[name] = {"status": IDLE, "step": 0, "tool_calls": 0,
                                "last_tool": "", "route": "", "note": "",
                                "output": ""}
        return self.nodes.get(name)

    def _set_active(self, name):
        # During a fan-out (concurrent branches in flight) the single-cursor
        # inference below is INVALID: sibling branches run at the same time, so a
        # new active node does NOT mean the previous one finished, and there is no
        # edge between two concurrent branches. The real fan-out edges come from
        # the fanout/join records; each branch node finishes on its own stage_end.
        # So while a fan-out is open, just track the most-recent active node.
        if self._concurrent:
            self.active = name
            return
        # a node becoming active implies the previously-active one finished,
        # and the transition between them is an edge that just lit up
        if self.active and name and self.active != name:
            prev = self.nodes.get(self.active)
            if prev and prev["status"] == RUNNING:
                prev["status"] = DONE
            self.active_edge = (self.active, name)
            self.edges.add((self.active, name))
        self.active = name

    def consume(self, rec):
        """Update state from one trace record. Returns self for chaining."""
        kind = rec.get("kind", "")
        name = self._node_name(rec)
        if kind == "run_start":
            for n in self.nodes.values():
                n.update(status=IDLE, step=0, tool_calls=0, last_tool="",
                         route="", note="", output="")
            self.active = None
            self._concurrent = set()
            self.edges = set()
            self.active_edge = None
            self.finished = False
            self.result = ""
            self.error = ""
            self.state = {}
            self.usage = {}
            self.retries = 0
            self.failovers = 0
            self.last = "run started"
        elif kind == "stage_start":
            n = self._ensure(name)
            if n:
                n["status"] = RUNNING
            self._set_active(name)
            self.last = f"{name}: started"
        elif kind == "stage_end":
            n = self._ensure(name)
            if n and n["status"] != ERROR:
                n["status"] = DONE
                n["output"] = str(rec.get("output", ""))[:400]
            self.last = f"{name}: done"
        elif kind == "llm_step":
            n = self._ensure(name)
            if n:
                n["status"] = RUNNING
                n["step"] = rec.get("step", n["step"])
            self._set_active(name)
            self.last = f"{name}: step {rec.get('step', '')}"
        elif kind == "tool_call":
            n = self._ensure(name)
            if n:
                n["status"] = RUNNING
                n["tool_calls"] += 1
                n["last_tool"] = rec.get("tool", "")
            self._set_active(name)
            self.last = f"{name}: tool {rec.get('tool', '')}"
        elif kind == "tool_result":
            self.last = f"{name}: tool result"
        elif kind == "fanout":
            # a fan-out control node: its branches run CONCURRENTLY until the join.
            # Enter concurrent mode (suspends single-cursor inference) and light the
            # edge from the fan-out node to each branch entry. The branch nodes then
            # each light/finish independently via their own stage_start/stage_end.
            # (a fan-out node is a control node, not a stage — deliberately NOT
            # _ensure'd, so it never shows a badge or a row in the usage summary.)
            # light the incoming edge (the sequential stage that fed this fan-out)
            if self.active and name and self.active != name and not self._concurrent:
                self.edges.add((self.active, name))
            branches = [b for b in (rec.get("branches") or []) if b]
            for b in branches:
                self._ensure(b)
                if name:
                    self.edges.add((name, b))
            self._concurrent = set(branches)
            self.active_edge = (name, branches[0]) if (name and branches) else None
            self.active = name
            self.last = f"{name}: fan-out → {len(branches)} branch(es)"
        elif kind == "join":
            # the barrier: every branch has finished. Leave concurrent mode, mark any
            # branch still shown RUNNING as done (defensive), and light each
            # branch→join edge. (For a multi-node branch the join's true predecessor
            # is the branch TAIL, not its entry; v1 branches are single-agent so
            # entry==tail — the tail case is a minor future refinement.)
            # (the join is a control node — not _ensure'd, same as the fan-out.)
            for b in self._concurrent:
                nb = self.nodes.get(b)
                if nb and nb["status"] == RUNNING:
                    nb["status"] = DONE
                self.edges.add((b, name))
                self.active_edge = (b, name)
            n = len(rec.get("branches") or []) or len(self._concurrent)
            self._concurrent = set()
            self.active = name          # the join becomes the cursor; next stage edges from it
            snap = rec.get("state")
            if isinstance(snap, dict):
                self.state = dict(snap)
            self.last = f"{name}: join ({n} branch(es) merged)"
        elif kind == "route":
            n = self._ensure(name)
            if n:
                n["route"] = rec.get("choice", "")
            self.last = f"{name} → {rec.get('choice', '')}"
        elif kind == "state":
            n = self._ensure(name)
            upd = rec.get("updates", {}) or {}
            summary = ", ".join(f"{k}={v}" for k, v in upd.items())
            if n and summary:
                n["note"] = ("state: " + summary)[:120]
            snap = rec.get("state")
            if isinstance(snap, dict):
                self.state = dict(snap)          # full live shared-state snapshot
            self.last = f"{name}: state {summary}".rstrip()
        elif kind == "condition":
            n = self._ensure(name)
            if n:
                n["route"] = rec.get("choice", "")
            self.last = f"{name} → {rec.get('choice', '')}"
        elif kind in ("retry", "failover"):
            n = self._ensure(name)
            if kind == "retry":
                self.retries += 1
                note = f"retry: {rec.get('error', '')[:60]}"
            else:
                self.failovers += 1
                note = f"failover → {rec.get('next_model', '')}"
            if n:
                n["note"] = note
            self.last = f"{name}: {kind}"
        elif kind == "run_error":
            # injected by the debug runner when run() raises
            target = name or self.active
            n = self._ensure(target) if target else None
            if n:
                n["status"] = ERROR
                n["note"] = str(rec.get("error", ""))[:120]
            self.error = str(rec.get("error", ""))
            self.finished = True
            self.last = f"error: {self.error[:80]}"
        elif kind == "run_end":
            if self.active:
                cur = self.nodes.get(self.active)
                if cur and cur["status"] == RUNNING:
                    cur["status"] = DONE
            self.active_edge = None          # nothing flowing once finished
            self.finished = True
            self.result = str(rec.get("result", ""))
            u = rec.get("usage")             # per-agent token/tool tally
            if isinstance(u, dict):
                self.usage = u
            self.last = "run finished"

        if kind not in ("run_start",):
            self.events.append((kind, name, self.last))
            if len(self.events) > 200:
                self.events = self.events[-200:]
        return self

    # ── usage / cost attribution (LangSmith/Vellum-style per-node breakdown) ──
    def summary(self, prices=None):
        """Aggregate per-agent usage + run totals from the consumed trace, for a
        cost/usage-attribution view. Tokens/tool-calls come from the run_end
        `usage` tally; llm steps from the event stream; retries/failovers from
        the run. If `prices` ({"input_per_1m", "output_per_1m"}) is given, also
        attributes a per-agent and total cost (a saved trace alone carries no
        prices, so cost is opt-in)."""
        usage = self.usage if isinstance(self.usage, dict) else {}
        per_agent = {}

        def _int(v):
            try:
                return int(v or 0)
            except (TypeError, ValueError):
                return 0

        def _row(u, node):
            # tolerate a malformed usage value (non-dict / non-int tokens) from a
            # hand-edited or externally-produced trace — never raise.
            u = u if isinstance(u, dict) else {}
            node = node or {}
            return {
                "input_tokens": _int(u.get("input_tokens", 0)),
                "output_tokens": _int(u.get("output_tokens", 0)),
                "tool_calls": _int(u.get("tool_calls", node.get("tool_calls", 0))),
                # NOTE: node["step"] is the LAST step number — exact for a
                # sequential react loop, but an undercount for a worker pool
                # (N concurrent workers share one agent name); tokens/tool_calls
                # stay correct (from the lock-aggregated run_end usage).
                "llm_steps": _int(node.get("step", 0)),
                "status": node.get("status", IDLE),
            }

        for name, node in self.nodes.items():
            per_agent[name] = _row(usage.get(name), node)
        for name, u in usage.items():            # agents only seen in run_end
            if name not in per_agent and isinstance(u, dict):
                per_agent[name] = _row(u, None)
                per_agent[name]["status"] = DONE

        if prices:
            pin = float(prices.get("input_per_1m", 0) or 0)
            pout = float(prices.get("output_per_1m", 0) or 0)
            for a in per_agent.values():
                a["cost_usd"] = round(a["input_tokens"] / 1e6 * pin
                                      + a["output_tokens"] / 1e6 * pout, 6)

        totals = {
            "input_tokens": sum(a["input_tokens"] for a in per_agent.values()),
            "output_tokens": sum(a["output_tokens"] for a in per_agent.values()),
            "tool_calls": sum(a["tool_calls"] for a in per_agent.values()),
            "llm_steps": sum(a["llm_steps"] for a in per_agent.values()),
            "retries": self.retries,
            "failovers": self.failovers,
            "agents_run": sum(1 for a in per_agent.values()
                              if a["llm_steps"] or a["status"] in (RUNNING, DONE, ERROR)),
            "errored": bool(self.error),
            "finished": self.finished,
        }
        if prices:
            totals["cost_usd"] = round(
                sum(a.get("cost_usd", 0) for a in per_agent.values()), 6)
        return {"per_agent": per_agent, "totals": totals}


def summarize_trace(records, prices=None):
    """Aggregate a list of trace records (e.g. a saved traces/*.jsonl) into a
    per-agent usage/cost summary. A thin convenience over RuntimeOverlay for
    callers that only have records and no live overlay."""
    ov = RuntimeOverlay([])
    for rec in records:
        if isinstance(rec, dict):
            ov.consume(rec)
    return ov.summary(prices)
