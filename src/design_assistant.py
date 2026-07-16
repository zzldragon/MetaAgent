"""Design-assistant foundations: the DETERMINISTIC, always-in-sync layer that a
built-in "design assistant" agent (a sibling of the Tool Generator coding agent)
sits on top of.

The design principle — and the reason this file exists at all — is that the
assistant's KNOWLEDGE must never drift from the code. So nothing here is a
hand-written prose description of "how the canvas works". Every fact is
INTROSPECTED from the single sources of truth that generation itself uses:

  * node kinds        -> graph_model.KIND_META
  * roles             -> graph_codegen.ROLE_DEFAULT_PROMPTS
  * patterns          -> patterns.PATTERNS
  * link rules        -> graph_model.ALLOWED_EDGES / SINGLETON_INPUTS
  * shared-state types-> graph_model.STATE_TYPES
  * default budgets   -> graph_model.DEFAULT_BUDGETS
  * available tools   -> codegen.list_tools()
  * bug/design linter -> graph_codegen.analyze()   (errors + warnings)

Add a new node kind, role, or pattern to the code and its ENUMERABLE facts show
up here for free; `tests/_verify_design_assistant.py` fails fast if that stops
being true. The richer SEMANTICS below (KIND_DESC, ROLE_SEMANTICS, EDGE_SEMANTICS,
CANVAS_RULES, CANVAS_DESIGN) are hand-written and NOT auto-derivable.

RULE — keep this in sync with the canvas (mirror it in the SAME commit). Any
change to the canvas vocabulary or semantics — a node kind (KIND_META), role
(ROLE_DEFAULT_PROMPTS), pattern (PATTERNS), edge rule (ALLOWED_EDGES /
SINGLETON_INPUTS), STATE_TYPES entry, default budgets, or an analyze() validity
rule — MUST update the built-in knowledge here:
  * enumerable facts: introspected above + guarded by the parity test (a new
    kind/role/pattern with no KIND_DESC / ROLE_SEMANTICS entry FAILS the suite);
  * curated semantics: edit by hand — KIND_DESC, ROLE_SEMANTICS, EDGE_SEMANTICS,
    CANVAS_RULES, CANVAS_DESIGN (these are NOT machine-checkable).
Also mirror the change in the user guide's node/link tables (docs/MetaAgent_CuDo.md).

Two public entry points:
  design_knowledge()        -> structured, in-sync knowledge blob (dict)
  knowledge_prompt()        -> the same, rendered as Markdown for a system prompt
  graph_metrics(graph)      -> deterministic stats (agent-call lower bound,
                               budgets, parallelism, redundancy) — the honest
                               "how heavy is this graph" numbers an LLM should
                               INTERPRET rather than invent
  design_review(graph)      -> analyze() (errors/warnings/topology) + graph_metrics
                               bundled: the report the L0 "Design Review" panel
                               renders and the L2 advisor agent reads.
"""

from __future__ import annotations

from graph_model import (
    AGENT_KINDS,
    ALLOWED_EDGES,
    CONTROL_KINDS,
    DEFAULT_BUDGETS,
    FLOW_KINDS,
    KIND_META,
    SINGLETON_INPUTS,
    STATE_TYPES,
)

_BUDGET_KEYS = tuple(DEFAULT_BUDGETS)  # max_iterations / max_tool_calls / ...


# ── kind grouping (derived, so new kinds classify automatically) ─────────────
def _kind_group(kind: str) -> str:
    if kind in AGENT_KINDS:
        return "agent"
    if kind in CONTROL_KINDS:
        return "control"
    if kind == "hitl":
        return "flow"
    return "resource"


def _resource_kinds() -> list[str]:
    """Kinds that link INTO an agent but are not themselves flow nodes
    (llm/tool/skill/prompt/rag/mcp/eval/gui) — derived from ALLOWED_EDGES so it
    tracks whatever resources generation actually accepts."""
    return sorted({s for (s, d) in ALLOWED_EDGES
                   if d in AGENT_KINDS and s not in FLOW_KINDS})


# ── curated core-design knowledge (parity-tested against the registries) ──────
# These describe the SEMANTICS that aren't machine-introspectable (what a kind or
# role DOES at runtime, how the canvas executes). They are hand-written but the
# test suite asserts KIND_DESC/ROLE_SEMANTICS cover every registry entry, so a new
# kind or role can't ship without a description — no silent drift.

CANVAS_DESIGN = """\
A MetaAgent graph is a visual spec compiled into a runnable multi-agent app.
Nodes are AGENTS (the work), RESOURCES that feed them (llm/tool/skill/prompt/rag/
mcp), and CONTROL nodes that route between them (condition/while/setstate/
guardrail/end/hitl). Edges wire them together.

Execution mode is DERIVED by analyze() from the entry agent's role + topology
(never set by hand):
- chain: a linear pipeline of agents; each agent's text output is the next's input.
- graph: branches, loops and control nodes (condition/while/setstate/guardrail/end).
- supervisor: a supervisor entry delegates to worker agents in a NEXT/DONE loop.
- autonomous: an orchestrator entry spawns isolated sub-agents in parallel
  (the built-in spawn_subagent tool).

Data flow: an agent's final TEXT is "carried" to the next stage as its input.
Control nodes route but do NOT reshape the payload; condition/while choose the
branch by reading SHARED STATE, not the text. Resources are not stages — they give
an agent its model, tools, prompt and knowledge.

Shared state: an optional graph-level typed schema (name, type, reducer, default,
description). Agents read/write it via a fenced block; condition/while/setstate
operate on it. Reserved built-in fields: tool_calls, agents.

System prompt: each agent's runtime system prompt = persona (its Prompt node text,
or the role's default template) + a routing tail + skills + tool guidance + any
data-contract tails.

Budgets: every agent has per-run limits (max_iterations, max_tool_calls,
max_output_tokens, max_wall_clock_s).

Generation: analyze() must pass (errors block, warnings are advisory);
generate_from_graph then emits a runnable agent (a single self-contained file, or a
runtime/ package) whose engine is a ReAct loop."""

KIND_DESC = {
    "agent": "A ReAct agent: an LLM in a reason→act loop that can call tools and "
             "produce a text answer. The unit of work; one pipeline stage.",
    "llm": "The chat model + provider config for an agent (provider/model/api_key/"
           "base_url/temperature). Several LLMs on one agent form a failover chain.",
    "tool": "A library of Python functions the agent may call; each function's "
            "docstring tells the agent when to use it. The same tool file linked "
            "twice to one agent is an error.",
    "skill": "Progressive-disclosure instructions: name+description are always "
             "shown; the body loads on demand (or via a /slash command).",
    "prompt": "The agent's system-prompt text (its persona). Singleton — at most "
              "one per agent; if absent the agent uses its role's default template.",
    "rag": "A retrieval knowledge base (a documents folder) exposed to the agent as "
           "a retrieval tool; several RAG nodes = several knowledge bases.",
    "memory": "A persistent cross-run memory store linked to an agent: gives it "
              "remember(content, tags) and recall(query) tools backed by a JSON store "
              "+ BM25 retrieval, so it learns across runs (Reflexion-style).",
    "webserver": "Makes generation emit a web server that serves the agent. At most "
                 "one per graph.",
    "mcp": "An MCP client connecting the agent to an external tool server (stdio or "
           "http). Several MCP nodes = several servers.",
    "workerpool": "An agent that fans out over a list of subtasks at runtime, "
                  "running workers in parallel.",
    "router": "An agent that classifies the input with an LLM and routes it to "
              "exactly ONE successor agent; may have several outgoing agent links. "
              "It forwards the same text (no data contract).",
    "hitl": "A human-in-the-loop checkpoint. With ONE outgoing link it's a review "
            "gate (approve/edit/reject), spliced out and applied before the downstream "
            "agent. With 2+ outgoing links it's a human-driven BRANCH (route mode): the "
            "reviewer picks which successor runs next — the human mirror of a Router.",
    "eval": "Offline test cases + graders for one agent; links to a single agent.",
    "gui": "Makes generation emit a PySide6 desktop GUI; link it to the entry agent.",
    "schedule": "Makes generation emit scheduler.py — an ambient runner that calls an "
                "agent on an interval/daily/once (no user prompt). Link it to the entry "
                "agent to drive the whole graph, or link separate schedules to separate "
                "agents to drive each one on its own timer.",
    "condition": "If/Else: routes to one of several branches by evaluating a "
                 "predicate over SHARED STATE (not over the upstream text).",
    "while": "A loop guard: runs its body while a state predicate holds, else takes "
             "the exit link.",
    "foreach": "Map-over-list: runs its body ONCE PER ITEM of a shared-state list "
               "field, the items in PARALLEL on isolated state forks (a dynamic "
               "fan-out). Each item is passed to the body both as its input and (when "
               "set) written to an item field; the body links BACK to the For-Each "
               "node, and the OTHER outgoing link is the exit, taken once after all "
               "items finish. Optionally collects each item's output into a result "
               "list field and merges the outputs (concat/first/last/state_only/vote).",
    "setstate": "Writes shared-state fields (deterministic assignments/expressions). "
                "Exactly one outgoing link.",
    "guardrail": "An inline content gate that redacts or blocks the content flowing "
                 "through it.",
    "end": "A terminal sink (no outgoing links) that finishes the run early, "
           "returning whatever output reached it — handy on an If/Else else-branch "
           "or a While exit.",
    "fanout": "Runs its 2+ branches CONCURRENTLY (real threads), then reconverges at "
              "the paired Join; each branch is an independent agent chain that writes "
              "its own shared-state fields.",
    "join": "The barrier that reconverges a Fan-out's branches: their shared-state "
            "writes merge via each field's reducer and the branch outputs combine by "
            "its merge policy (concat/first/last/state_only).",
    "subgraph": "Embeds another whole graph as one reusable step: its incoming edge "
                "feeds the child's entry, and the child's End (or last stage) continues "
                "to this node's successor. The child (nodes/tools/prompts/LLMs) is stored "
                "INLINE on the node and flattened in at generation time, so the parent "
                "stays self-contained; the child's shared-state fields merge into the "
                "parent's. Nested subgraphs flatten first; a recursive include is rejected.",
}

ROLE_SEMANTICS = {
    "single": "A general ReAct agent (the default) — runs alone or as one chain stage.",
    "planner": "Breaks the task into a numbered plan for the next agent; does not "
               "execute. May 'self-route' (pick its own successor) instead of a Router.",
    "worker": "Executes the task (and the plan, if given) thoroughly and reports. "
              "Tool-eligible.",
    "critic": "Reviews the previous agent's output; can send it back by starting its "
              "reply with 'REVISE:' (a bounded revise loop).",
    "supervisor": "Delegates one instruction at a time to its workers (NEXT/DONE "
                  "protocol), reviewing each result. Entry-only; workers are leaves.",
    "orchestrator": "Autonomous coordinator: spawns isolated sub-agents in parallel "
                    "via spawn_subagent; each runs with only its own tools and "
                    "returns just its result. Entry-only; sub-agents are linked leaves.",
}

EDGE_SEMANTICS = [
    "resource → agent: an llm/tool/skill/prompt/rag/mcp node feeds capabilities into "
    "an agent; it is NOT a pipeline stage.",
    "agent → agent: a data handoff — the upstream agent's text OUTPUT becomes the "
    "downstream agent's INPUT. May carry a data CONTRACT (fields the producer "
    "outputs = the consumer expects): advisory prompt tails by default, or ENFORCED "
    "(the producer's output is JSON-validated and retried).",
    "agent → condition/while/setstate/guardrail/end: control-flow edges. "
    "condition/while pick a branch by reading shared STATE; the payload passes "
    "through unchanged.",
    "router → agents: the router picks ONE branch at runtime; contracts don't apply.",
    "eval → agent, gui → agent: attach evaluation / a desktop GUI to one agent "
    "(usually the entry).",
]

CANVAS_RULES = [
    "Every agent needs at least one linked LLM.",
    "A plain agent or worker-pool may have at most ONE outgoing agent link. Only a "
    "router, a supervisor, an orchestrator, or a self-routing planner may branch to "
    "several agents.",
    "The orchestrator and supervisor roles are allowed only on the ENTRY agent; "
    "their sub-agents/workers must be plain leaf agents.",
    "A router must have at least one outgoing agent link.",
    "prompt and gui are singleton inputs: at most one of each per agent.",
    "The same tool file linked twice to one agent is an error.",
    "condition/while/foreach/setstate require shared-state fields; a condition needs "
    "branches, a while needs a guard+body+exit, a foreach needs an 'over' list field "
    "plus a body that links back and an exit, an end has no outgoing links.",
    "A hitl node sits between exactly one upstream and one downstream node, and "
    "cannot connect to a router or directly to another hitl.",
    "Data contracts apply only to agent→agent edges (not router/condition/resource).",
    "The entry is the agent with no incoming agent link (or a single planner); an "
    "ambiguous entry is an error.",
]


# ── the knowledge blob (100% introspected + curated semantics) ────────────────
def design_knowledge() -> dict:
    """A machine-readable snapshot of the canvas rules, introspected from the
    live registries so it can never fall out of sync with generation."""
    import graph_codegen as gc
    import patterns as pat
    from codegen import list_tools

    node_kinds = {
        kind: {"label": meta["label"], "group": _kind_group(kind),
               "desc": KIND_DESC.get(kind, "")}       # curated per-kind semantics
        for kind, meta in KIND_META.items()
    }
    roles = dict(gc.ROLE_DEFAULT_PROMPTS)  # role -> its default persona template
    graph_patterns = {
        pid: {"label": spec.get("label", pid),
              "description": spec.get("description", ""),
              # concrete topology so the LLM has exemplars to reason from
              "agents": [list(a) for a in spec.get("agents", [])],
              "links": [list(link) for link in spec.get("links", [])]}
        for pid, spec in pat.PATTERNS.items()
    }
    return {
        "core_design": CANVAS_DESIGN,
        "node_kinds": node_kinds,
        "roles": roles,
        "role_semantics": dict(ROLE_SEMANTICS),        # role -> runtime behaviour
        "patterns": graph_patterns,
        "edge_semantics": list(EDGE_SEMANTICS),
        "link_rules": {
            "resource_to_agent": _resource_kinds(),
            "agent_kinds": list(AGENT_KINDS),
            "control_kinds": list(CONTROL_KINDS),
            "singleton_inputs": sorted(SINGLETON_INPUTS),
            "allowed_edges": sorted(ALLOWED_EDGES),
        },
        "rules": list(CANVAS_RULES),
        "state_types": list(STATE_TYPES),
        "default_budgets": dict(DEFAULT_BUDGETS),
        "available_tools": list_tools(),
    }


def knowledge_prompt() -> str:
    """Render design_knowledge() as the Markdown block to embed in the design
    assistant's system prompt. Because it is derived, editing the code updates
    the assistant's knowledge with zero prose maintenance."""
    k = design_knowledge()
    lines: list[str] = ["# MetaAgent canvas — reference (auto-generated)\n"]

    lines.append("## Core design")
    lines.append(k["core_design"])

    lines.append("\n## Node kinds")
    for group, title in (("agent", "Agents (pipeline stages)"),
                         ("resource", "Resources (feed an agent)"),
                         ("control", "Control-flow"),
                         ("flow", "Flow")):
        items = [(kind, m) for kind, m in k["node_kinds"].items() if m["group"] == group]
        if not items:
            continue
        lines.append(f"\n### {title}")
        for kind, m in items:
            lines.append(f"- `{kind}` ({m['label']}) — {m['desc']}")

    lines.append("\n## Agent roles (runtime behaviour)")
    for role, does in k["role_semantics"].items():
        lines.append(f"- `{role}` — {does}")

    lines.append("\n## Edges (what a link means)")
    for e in k["edge_semantics"]:
        lines.append(f"- {e}")

    lines.append("\n## Validity rules")
    for r in k["rules"]:
        lines.append(f"- {r}")

    lines.append("\n## Shared state")
    lines.append("- Field types: " + ", ".join(f"`{t}`" for t in k["state_types"]))
    lines.append("- A schema field is {name, type, reducer, default, description}; "
                 "reserved built-ins: `tool_calls`, `agents`.")

    lines.append("\n## Patterns (starting topologies)")
    for pid, spec in k["patterns"].items():
        line = f"- `{pid}` — {spec['label']}: {spec['description']}"
        if spec["agents"]:
            agents = ", ".join(f"{n}({r})" for n, r in spec["agents"])
            links = " ; ".join(f"{a}→{b}" for a, b in spec["links"]) or "(no agent links)"
            line += f"\n    - agents: {agents}\n    - links: {links}"
        lines.append(line)

    lr = k["link_rules"]
    lines.append("\n## Link-kind reference")
    lines.append("- Resource kinds (→ agent): " + ", ".join(f"`{r}`" for r in lr["resource_to_agent"]))
    lines.append("- Agent-stage kinds (may have agent successors): "
                 + ", ".join(f"`{a}`" for a in lr["agent_kinds"]))
    lines.append("- Control-flow kinds: " + ", ".join(f"`{c}`" for c in lr["control_kinds"]))
    lines.append("- Singleton inputs (≤1 per agent): "
                 + ", ".join(f"`{s}`" for s in lr["singleton_inputs"]))

    lines.append("\n## Default per-agent budgets")
    lines.append("- " + ", ".join(f"{key}={val}" for key, val in k["default_budgets"].items()))

    lines.append("\n## Available tool libraries")
    tools = k["available_tools"]
    lines.append("- " + (", ".join(f"`{t}`" for t in tools) if tools else "(none in tools/)"))

    return "\n".join(lines) + "\n"


# ── deterministic graph metrics (interpret, don't invent) ────────────────────
def _budget(node) -> dict:
    props = node.props or {}
    out = {}
    for key in _BUDGET_KEYS:
        val = props.get(key, DEFAULT_BUDGETS[key])
        try:
            out[key] = int(val)
        except (TypeError, ValueError):
            out[key] = DEFAULT_BUDGETS[key]
    return out


def graph_metrics(graph) -> dict:
    """Deterministic, LLM-free stats about a graph's size and cost SHAPE. These
    are honest lower bounds and structural counts — loops make real invocation
    counts unbounded, so this reports the static skeleton and flags the
    multipliers, rather than pretending to a single 'efficiency' number.
    """
    import graph_codegen as gc

    info = gc.analyze(graph)
    mode = info.get("mode")
    pipeline_ids = info.get("pipeline") or []

    node_counts: dict[str, int] = {}
    for n in graph.nodes.values():
        node_counts[n.kind] = node_counts.get(n.kind, 0) + 1

    agents = graph.agents()
    agent_ids = {n.id for n in agents}
    pipeline_agent_ids = [i for i in pipeline_ids if i in agent_ids]

    # per-agent + summed budgets (theoretical ceiling for ONE pass)
    per_agent = {}
    totals = {key: 0 for key in _BUDGET_KEYS}
    for n in agents:
        b = _budget(n)
        per_agent[n.name] = b
        for key in _BUDGET_KEYS:
            totals[key] += b[key]

    # loop multipliers that make a single-pass estimate a *lower* bound
    while_count = node_counts.get("while", 0)
    revise_loop = bool(info.get("revise_edge"))
    recursion_limit = int(getattr(graph, "recursion_limit", 0) or 0)

    # parallelism: worker pools fan out; an orchestrator spawns sub-agents
    worker_pools = [n for n in agents if n.kind == "workerpool"]
    orchestrators = [n for n in agents
                     if (n.props or {}).get("role") == "orchestrator"]

    warnings = info.get("warnings") or []
    redundancy = [w for w in warnings
                  if any(t in w.lower() for t in ("duplicate", "identical", "twice",
                                                  "same tool", "same llm", "clobber",
                                                  "overwrite"))]

    return {
        "mode": mode,
        "entry": info.get("entry"),
        "pipeline": [graph.nodes[i].name for i in pipeline_ids if i in graph.nodes],
        "node_counts": node_counts,
        "agent_count": len(agents),
        # honest lower bound: one LLM turn per agent stage on the resolved path,
        # BEFORE any loop/branch multiplier (see loops).
        "min_agent_invocations": len(pipeline_agent_ids) or len(agents),
        "loops": {
            "revise_loop": revise_loop,
            "while_nodes": while_count,
            "recursion_limit": recursion_limit,   # 0 = auto (codegen derives it)
            "note": ("actual invocations exceed min_agent_invocations when a "
                     "revise/while loop or router branch repeats stages"),
        },
        "parallelism": {
            "worker_pools": [n.name for n in worker_pools],
            "orchestrator": bool(orchestrators),
            "note": ("worker pools fan out over subtasks; an orchestrator can "
                     "spawn sub-agents in parallel — both trade tokens for wall-clock"),
        },
        "budgets": {"per_agent": per_agent, "totals": totals},
        "redundancy_warnings": redundancy,
        "issue_counts": {"errors": len(info.get("errors") or []),
                         "warnings": len(warnings)},
    }


def design_review(graph) -> dict:
    """The full deterministic report: analyze()'s correctness findings +
    graph_metrics()'s cost/shape stats. This is what an L0 'Design Review' panel
    renders and what the L2 advisor agent is fed (so it never reasons about the
    canvas from memory)."""
    import graph_codegen as gc

    info = gc.analyze(graph)
    return {
        "errors": info.get("errors") or [],
        "warnings": info.get("warnings") or [],
        "topology": {
            "mode": info.get("mode"),
            "entry": info.get("entry"),
            "pipeline": [graph.nodes[i].name
                         for i in (info.get("pipeline") or []) if i in graph.nodes],
        },
        "metrics": graph_metrics(graph),
    }
