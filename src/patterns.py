"""Agent-pattern registry: each pattern is a graph topology (agent roles +
links) plus a runtime mode. Both the form designer and the canvas build
graphs from these presets; graph_codegen generates code that follows the
pattern's mode ("chain" pipelines, or the "supervisor" delegation loop).

Add a new pattern by adding an entry here — the designers pick it up
automatically.
"""

from __future__ import annotations

from graph_model import DEFAULT_BUDGETS, Graph

PATTERNS = {
    "react": {
        "label": "ReAct (single agent)",
        "description": "One agent with tools in a reason+act loop.",
        "agents": [("agent", "single")],
        "links": [],
    },
    "planner_executor": {
        "label": "Planner–Executor",
        "description": "Planner breaks the task into steps; executor runs them.",
        "agents": [("planner", "planner"), ("executor", "worker")],
        "links": [("planner", "executor")],
    },
    "planner_executor_critic": {
        "label": "Planner–Executor–Critic (revise loop)",
        "description": "Adds a critic that can send the work back for revision.",
        "agents": [("planner", "planner"), ("executor", "worker"),
                   ("critic", "critic")],
        "links": [("planner", "executor"), ("executor", "critic"),
                  ("critic", "planner")],
    },
    "supervisor_worker": {
        "label": "Supervisor–Worker (delegation loop)",
        "description": "Supervisor delegates one instruction at a time "
                       "(NEXT/DONE protocol) and reviews each result.",
        "agents": [("supervisor", "supervisor"), ("worker", "worker")],
        "links": [("supervisor", "worker")],
    },
    "orchestrator": {
        "label": "Orchestrator (autonomous, spawns sub-agents)",
        "description": "Orchestrator spawns isolated sub-agents in parallel via "
                       "the spawn_subagent tool; each has its own tools and "
                       "returns only its result.",
        "agents": [("orchestrator", "orchestrator"),
                   ("writer", "worker"), ("reader", "worker")],
        "links": [("orchestrator", "writer"), ("orchestrator", "reader")],
    },
}

# Tools attach to agents with these roles when building a preset.
TOOL_ROLES = {"single", "worker"}

# Per-role sampling temperature for the classic presets' LLMs. These are
# execution / reasoning agents, not creative writers, so they run cooler than the
# provider default (~0.7): deterministic where a decision must be reproducible
# (supervisor delegation), low where consistency and reliable tool use matter.
ROLE_TEMPERATURE = {
    "single": "0.3",        # general ReAct agent — a little flexibility
    "planner": "0.2",       # consistent, well-structured plans
    "worker": "0.2",        # reliable step execution + tool use
    "critic": "0.2",        # rigorous, repeatable judgement
    "supervisor": "0",      # deterministic delegation (NEXT/DONE)
    "orchestrator": "0.2",  # steady coordination + synthesis
}

# Per-role budget OVERRIDES, merged over the caller's budgets / DEFAULT_BUDGETS.
# Coordinating roles fan out (orchestrator → parallel sub-agents) or loop over
# many rounds (supervisor → up to MAX_SUPERVISOR_ROUNDS), so they need more
# iteration and wall-clock head-room than a single-shot agent.
_ROLE_BUDGETS = {
    "orchestrator": {"max_iterations": 16, "max_tool_calls": 30,
                     "max_wall_clock_s": 180},
    "supervisor": {"max_wall_clock_s": 120},
}


def build_pattern_graph(pattern_id: str, llm_props: dict,
                        tool_files: list | None = None,
                        budgets: dict | None = None) -> Graph:
    """Build a canvas Graph for a pattern preset — a COMPLETE pattern: every agent
    comes with its own Prompt node (its role's system-prompt template), LLM node,
    and a Tools node for tool-eligible roles.

    llm_props: provider/model/api_key/base_url applied to every agent's LLM.
    tool_files: tool library files, linked to worker/single-role agents.
    budgets: applied to every agent (defaults otherwise).

    Row layout per agent (left → right): prompt | tool | agent | llm.
    """
    # Local import keeps patterns import-light and avoids any import cycle.
    from graph_codegen import role_template

    spec = PATTERNS[pattern_id]

    # Rich presets (CRAG / Self-RAG / Adaptive-RAG) can't be expressed as a flat
    # list of (name, role) + links — they need RAG nodes, per-agent capability
    # toggles and hand-written prompts. Such a preset supplies a `builder`
    # callable that constructs the whole Graph itself.
    builder = spec.get("builder")
    if builder is not None:
        return builder(llm_props, tool_files, budgets)

    g = Graph()
    agents = {}

    # Lay the rows out CENTERED on the origin (rows straddle y=0, columns straddle
    # x=0) instead of anchored at the top-left. The canvas centers the view on the
    # origin when a preset is inserted, so the pattern lands in the middle with
    # room to pan/drag in every direction — not jammed against a top-left corner.
    n = len(spec["agents"])
    col = {"prompt": -424, "tool": -234, "agent": -44, "llm": 256}
    prompt_overrides = spec.get("prompts", {})

    for i, (name, role) in enumerate(spec["agents"]):
        y = int((i - (n - 1) / 2) * 150)
        agent = g.new_node("agent", col["agent"], y)
        agent.name = name
        agent.props["role"] = role
        # Base budgets from the caller (or defaults), then per-role head-room for
        # coordinating roles that fan out / loop over many rounds.
        base_budget = dict(budgets or DEFAULT_BUDGETS)
        base_budget.update(_ROLE_BUDGETS.get(role, {}))
        for key in DEFAULT_BUDGETS:
            agent.props[key] = base_budget[key]
        agents[name] = agent

        llm = g.new_node("llm", col["llm"], y)
        llm.name = f"llm_{name}"
        llm.props.update(llm_props)
        # Purposeful per-role temperature — cooler than the provider default so
        # these execution/decision agents are consistent and reliable at tool use.
        _temp = ROLE_TEMPERATURE.get(role)
        if _temp is not None:
            llm.props["temperature"] = _temp
        g.add_edge(llm.id, agent.id)

        # Each agent gets its own Prompt node, pre-filled with a pattern-specific
        # prompt when the preset provides one, else its role's system-prompt
        # template — so the pattern's prompts are VISIBLE and editable on the
        # canvas instead of only being resolved implicitly from the role at
        # generation time. The prompt's role matches the agent's role.
        prompt = g.new_node("prompt", col["prompt"], y)
        prompt.name = f"prompt_{name}"
        prompt.props["role"] = role
        prompt.props["text"] = prompt_overrides.get(name) or role_template(role)
        g.add_edge(prompt.id, agent.id)

        # Tools attach PER tool-eligible agent (not one shared node), so sibling
        # sub-agents — e.g. an orchestrator's writer & reader — each start with
        # their own Tools node and can be given different tools. Presets with a
        # single tool-eligible agent are unchanged (just one node, as before).
        if tool_files and role in TOOL_ROLES:
            tools = g.new_node("tool", col["tool"], y)
            tools.name = f"tools_{name}"
            tools.props["files"] = list(tool_files)
            g.add_edge(tools.id, agent.id)

    for src, dst in spec["links"]:
        g.add_edge(agents[src].id, agents[dst].id)
    return g


# ---------------------------------------------------------------------------
# Retrieval presets (CRAG / Self-RAG / Adaptive-RAG)
# ---------------------------------------------------------------------------
# These are ready-made, multi-node retrieval graphs for users who want a proven
# RAG strategy without hand-configuring the retrieval toggles. Each ships with
# carefully written prompts and the right capability flags already switched on;
# the ONE thing the user must still do is point the retrieval node(s) at their
# own documents folder (double-click the RAG node → set "Docs folder").
#
# The retrieval features they rely on (per-passage grading, query self-correction,
# groundedness self-check, adaptive routing, web fallback) are all built into the
# runtime and are engaged purely by these node props — no extra library beyond
# `ddgs` for the web-search fallback (only the CRAG / Adaptive presets use it).

# Shared column x-positions so every generated node lines up in tidy columns
# (prompt | rag | agent | llm), rows stacked by y.
_COLS = {"prompt": -460, "rag": -234, "agent": -44, "llm": 300}

# A knowledge base's tool description is shown to the LLM as the search tool's doc
# string, so it must tell the model WHEN to reach for it. Users should tailor this
# to their corpus, but a sensible default keeps the preset usable out of the box.
_KB_DESC = ("Local document knowledge base. Search it for facts, definitions, "
            "policies, figures and any domain-specific details contained in the "
            "user's uploaded documents.")

# The `single` role's template (system_prompt_template.txt) is a fill-in-the-blank
# SCAFFOLD ("Describe what this agent is an expert at, e.g. ..."), which is right
# for the form designer but wrong to ship verbatim in a one-click preset. So the
# ReAct preset overrides it with a real, working think→act→observe prompt.
_REACT_PROMPT = """You are {agent_name}, a capable assistant that solves tasks by reasoning and using tools.

Work in a think -> act -> observe loop:
1. THINK briefly about what the user needs and which tool (if any) will help.
2. ACT by calling ONE tool at a time with precise arguments — never invent tool names or arguments.
3. OBSERVE the result. If it returns an [ERROR], explain the problem and adjust — don't repeat the same failing call or guess around it.
Repeat until you have what you need, then give a clear, concise final answer.

Rules:
- Use only the tools you are given; if the task can't be done with them, say so plainly.
- Ground answers in tool results — never fabricate data, file contents, numbers or citations.
- Stop calling tools once you can answer; don't keep searching past a confident response."""

# Attach the ReAct prompt to the react preset (keyed by agent name). The other
# classic presets keep their role templates, which are already pattern-aware.
PATTERNS["react"]["prompts"] = {"agent": _REACT_PROMPT}

_CRAG_PROMPT = """You are a meticulous research assistant. You answer questions with a corrective retrieval-augmented workflow, and you NEVER fabricate facts.

For every question:
1. RETRIEVE — Search the knowledge base with focused keywords drawn from the question. The retriever automatically grades each passage for relevance and, if nothing relevant comes back, rewrites the query and retries. Read what it returns before answering.
2. FALL BACK — If, after retrieval, the knowledge base still has nothing relevant, use the `web_search` tool to find the answer on the open web. Treat the local documents as the primary source and the web as a corrective fallback.
3. ANSWER — Compose the answer ONLY from the retrieved passages and/or web results. Cite every non-trivial claim with its source (document file name, or web URL). If neither the documents nor the web support an answer, say so plainly and explain what is missing — do not guess.

Be concise and specific. Quote figures, names and dates exactly as they appear in the sources."""

_SELF_RAG_PROMPT = """You are a careful, self-reflective research assistant. You retrieve evidence, answer strictly from it, and critique your own answer before finalising.

For every question:
1. RETRIEVE — Search the knowledge base with focused keywords. Retrieved passages are automatically graded for relevance; irrelevant ones are discarded. If you need more, refine the keywords and search again.
2. ANSWER — Write the answer using ONLY the relevant passages. Attribute every claim to its source (document file name). If the passages do not contain enough to answer, say "I don't know based on the available documents" rather than filling the gap with assumptions.
3. SELF-CHECK — Your final answer is automatically verified for (a) being fully grounded in the retrieved passages and (b) actually addressing the question. If it falls short you will be asked to revise, so make sure BEFORE you finalise that every sentence is supported by a cited passage and that nothing in the question is left unanswered.

Prefer a short, fully-supported answer over a long, partly-speculative one."""

_ADAPTIVE_ROUTER_PROMPT = """You are a query router for an adaptive retrieval system. Your job is to send each question down the cheapest route that can still answer it correctly.

Decide among three options and act:
- KNOWLEDGE BASE — If the question is about the internal / uploaded documents, or domain-specific facts likely to live in them, hand off to `knowledge_base`.
- WEB — If the question needs external, general, or recent / real-time information that would not be in the internal documents, hand off to `web_research`.
- DIRECT — If it is a greeting, small talk, a clarification, or something you can answer correctly and confidently from your own general knowledge WITHOUT looking anything up, just answer it yourself, briefly.

Route document and factual look-ups — do not answer those from memory. Choose exactly one route per question. When genuinely unsure between the knowledge base and the web, try the knowledge base first."""

_ADAPTIVE_KB_PROMPT = """You answer questions from the internal document knowledge base. Search it with focused keywords, then answer using ONLY the retrieved passages, citing each source document by file name. Retrieved passages are graded for relevance automatically. If the documents do not cover the question, say so clearly rather than guessing."""

_ADAPTIVE_WEB_PROMPT = """You answer questions using web search. Search for the specific facts the question needs, then answer from the results, citing the source URLs. Prefer authoritative and recent sources, cross-check when claims conflict, and if the web does not yield a clear answer, say so rather than guessing."""

# Retrieval-quality defaults shared by every preset's knowledge base. All three
# are offline-SAFE — recursive chunking + overlap keep facts whole across
# boundaries (no external dependency), and hybrid retrieval fuses keyword (BM25)
# with local semantic embeddings via RRF, degrading cleanly to plain BM25 (with a
# surfaced "semantic search unavailable" note) when the embedding model/library
# isn't present, so it never hangs or hard-fails offline. These matter because the
# stock defaults (bm25 + fixed 800-char cuts + zero overlap) are the weakest link
# in retrieval quality — exactly what these presets exist to get right.
_RAG_QUALITY = dict(
    chunk_strategy="recursive",    # split on paragraph/sentence boundaries, not blind char cuts
    chunk_overlap=120,             # ~15% overlap so a fact spanning a boundary survives
    retrieval_algorithm="hybrid",  # BM25 + local dense (bge-small) fused by RRF; falls back to BM25
)

# Retrieval agents run EXTRA loops the stock single-shot budgets don't anticipate
# — corrective re-retrieval, groundedness regeneration, web fallback — so give
# them more iteration and wall-clock head-room.
_RAG_BUDGETS = {**DEFAULT_BUDGETS, "max_iterations": 12, "max_wall_clock_s": 120}


def _unit(g: Graph, name: str, role: str, y: int, llm_props: dict,
          budgets: dict | None, prompt_text: str, llm_temperature=None,
          **agent_props):
    """Create one agent 'unit' at row y — agent + its LLM + its Prompt node
    (pre-filled with `prompt_text`) — wired up, and return the agent node.
    Extra agent capability flags are passed as keyword args (e.g. web_search=True).
    `llm_temperature` (a string, since the LLM node stores it as text) overrides
    the sampling temperature — 0 for a deterministic router, low for faithful
    grounded answering; left None means the provider default.
    """
    a = g.new_node("agent", _COLS["agent"], y)
    a.name = name
    a.props["role"] = role
    for key in DEFAULT_BUDGETS:
        a.props[key] = (budgets or DEFAULT_BUDGETS)[key]
    a.props.update(agent_props)

    llm = g.new_node("llm", _COLS["llm"], y)
    llm.name = f"llm_{name}"
    llm.props.update(llm_props)
    if llm_temperature is not None:
        llm.props["temperature"] = llm_temperature
    g.add_edge(llm.id, a.id)

    prompt = g.new_node("prompt", _COLS["prompt"], y)
    prompt.name = f"prompt_{name}"
    prompt.props["role"] = role
    prompt.props["text"] = prompt_text
    g.add_edge(prompt.id, a.id)
    return a


def _rag(g: Graph, agent, name: str, y: int, description: str, **rag_props):
    """Attach a knowledge-base (RAG) node at row y to `agent`. docs_dir is left
    EMPTY on purpose — the user points it at their own documents folder after
    inserting the preset (analyze() flags the empty folder as the one to-do)."""
    r = g.new_node("rag", _COLS["rag"], y)
    r.name = name
    r.props["description"] = description
    r.props.update(rag_props)
    g.add_edge(r.id, agent.id)
    return r


def _build_crag(llm_props: dict, tool_files, budgets) -> Graph:
    """Corrective RAG: retrieve → grade → self-correct query → web fallback."""
    g = Graph()
    budgets = budgets or _RAG_BUDGETS
    a = _unit(g, "researcher", "single", 0, llm_props, budgets, _CRAG_PROMPT,
              llm_temperature="0.2", web_search=True)
    _rag(g, a, "knowledge", 0, _KB_DESC,
         grade_docs=True, corrective=True, corrective_max_rewrites=2,
         **_RAG_QUALITY)
    return g


def _build_self_rag(llm_props: dict, tool_files, budgets) -> Graph:
    """Self-RAG: retrieve → grade → answer → groundedness self-check + revise."""
    g = Graph()
    budgets = budgets or _RAG_BUDGETS
    a = _unit(g, "self_rag", "single", 0, llm_props, budgets, _SELF_RAG_PROMPT,
              llm_temperature="0.2", groundedness_check=True, max_regen=2)
    _rag(g, a, "knowledge", 0, _KB_DESC, grade_docs=True, **_RAG_QUALITY)
    return g


def _build_adaptive_rag(llm_props: dict, tool_files, budgets) -> Graph:
    """Adaptive RAG: a router sends each query to the knowledge base, the web,
    or answers trivial ones directly (route_self + quick_response)."""
    g = Graph()
    budgets = budgets or _RAG_BUDGETS
    # temperature=0 → the routing decision is deterministic (a given question
    # always takes the same branch), which is what you want from a classifier.
    router = _unit(g, "router", "planner", 0, llm_props, budgets,
                   _ADAPTIVE_ROUTER_PROMPT, llm_temperature="0",
                   route_self=True, quick_response=True)
    kb = _unit(g, "knowledge_base", "single", -260, llm_props, budgets,
               _ADAPTIVE_KB_PROMPT, llm_temperature="0.2")
    _rag(g, kb, "documents", -260, _KB_DESC, grade_docs=True, **_RAG_QUALITY)
    web = _unit(g, "web_research", "single", 260, llm_props, budgets,
                _ADAPTIVE_WEB_PROMPT, llm_temperature="0.2", web_search=True)
    g.add_edge(router.id, kb.id)
    g.add_edge(router.id, web.id)
    return g


_MR_COORD_PROMPT = """You are the coordinator. In one or two sentences, frame the task for a team of specialist workers that run IN PARALLEL, then hand off. Do NOT solve the task yourself — just set it up."""

_MR_WORKER_PROMPT = """You are parallel worker #{n} of {total}. The other workers run at the SAME TIME, each covering a different angle of the task. Analyze the input from YOUR angle only and produce a focused, self-contained result — go deep, don't try to cover everything. (Edit this prompt to give each worker its own specialty.)"""

_MR_REDUCE_PROMPT = """You are the reducer. Your input is the concatenated results of the parallel workers. Synthesize them into ONE coherent answer: merge overlapping points, resolve conflicts, drop duplication, and note which worker contributed what. Do not re-run their analysis — combine it."""


def _build_map_reduce(llm_props: dict, tool_files, budgets) -> Graph:
    """Map-reduce / parallel-analysis: a coordinator frames the task, N specialist
    workers run CONCURRENTLY (a fan-out), then a reducer synthesizes their results
    (join, merge=concat). The generalization of the TradingAgents analyst team; unlike
    a Worker Pool (which splits one task across identical workers), each worker here is
    a distinct, separately-promptable agent. Tools attach per worker when provided."""
    g = Graph()
    budgets = budgets or dict(DEFAULT_BUDGETS)
    n = 3
    coord = _unit(g, "coordinator", "single", -(n // 2 + 1) * 150, llm_props, budgets,
                  _MR_COORD_PROMPT, llm_temperature="0.2")
    fo = g.new_node("fanout", _COLS["agent"], -(n // 2) * 150 - 60)
    fo.name = "fanout"
    jn = g.new_node("join", _COLS["agent"], (n // 2) * 150 + 60)
    jn.name = "join"
    jn.props["merge"] = "concat"
    g.add_edge(coord.id, fo.id)
    for i in range(n):
        y = int((i - (n - 1) / 2) * 150)
        w = _unit(g, f"worker_{i + 1}", "worker", y, llm_props, budgets,
                  _MR_WORKER_PROMPT.format(n=i + 1, total=n), llm_temperature="0.2")
        if tool_files:                       # each worker gets its OWN tools node
            t = g.new_node("tool", _COLS["rag"], y)
            t.name = f"tools_worker_{i + 1}"
            t.props["files"] = list(tool_files)
            g.add_edge(t.id, w.id)
        g.add_edge(fo.id, w.id)              # fan-out branch
        g.add_edge(w.id, jn.id)              # reconverge at the join
    reducer = _unit(g, "reducer", "single", (n // 2 + 1) * 150, llm_props, budgets,
                    _MR_REDUCE_PROMPT, llm_temperature="0.3")
    g.add_edge(jn.id, reducer.id)
    return g


_HA_INTAKE_PROMPT = """You are a customer-support agent. Read the customer's ticket and DRAFT a concise, empathetic reply that resolves their issue. Output ONLY the draft reply text — a human will review it before anything is sent."""

_HA_SEND_PROMPT = """The drafted reply was APPROVED by a human reviewer. Finalize it: fix any typos, add a professional sign-off, and output the final message to send to the customer."""

_HA_REVISER_PROMPT = """A reviewer sent the draft back for changes. Using the reviewer's edits/notes in the input, produce an IMPROVED draft that addresses the feedback. It goes back to the reviewer for another look."""

_HA_ESCALATE_PROMPT = """This ticket needs a specialist. Prepare an ESCALATION hand-off: summarize the ticket, the current draft, and exactly why it needs billing / legal / engineering. Output a clear note for the specialist."""

_HA_REVIEW_PROMPT = """Review the drafted reply and choose the next step:
  send     - approve and send it to the customer
  reviser  - send back for changes (edit the draft above to give guidance)
  escalate - hand off to a specialist
  reject   - close the ticket without replying"""


def _build_human_approval(llm_props: dict, tool_files, budgets) -> Graph:
    """Human-in-the-loop APPROVAL routing: an agent drafts a reply, then a ROUTE-mode
    HITL lets a HUMAN pick the next step — send it, send it back for revision (which
    loops back for another review), escalate to a specialist, or reject (End). The
    human-driven mirror of a Router; shows a revise loop, an End branch, and a safe
    default_route ('escalate') so unattended/timeout runs never auto-send."""
    g = Graph()
    budgets = budgets or dict(DEFAULT_BUDGETS)
    intake = _unit(g, "intake", "single", -300, llm_props, budgets,
                   _HA_INTAKE_PROMPT, llm_temperature="0.3")
    review = g.new_node("hitl", _COLS["agent"], -180)
    review.name = "human_review"
    review.props["prompt"] = _HA_REVIEW_PROMPT
    review.props["default_route"] = "escalate"     # unattended-safe: never auto-send
    send = _unit(g, "send", "single", -60, llm_props, budgets,
                 _HA_SEND_PROMPT, llm_temperature="0.2")
    reviser = _unit(g, "reviser", "single", 60, llm_props, budgets,
                    _HA_REVISER_PROMPT, llm_temperature="0.3")
    escalate = _unit(g, "escalate", "single", 180, llm_props, budgets,
                     _HA_ESCALATE_PROMPT, llm_temperature="0.2")
    reject = g.new_node("end", _COLS["agent"], 300)
    reject.name = "reject"
    g.add_edge(intake.id, review.id)
    g.add_edge(review.id, send.id)            # branch order = the reviewer's options
    g.add_edge(review.id, reviser.id)
    g.add_edge(review.id, escalate.id)
    g.add_edge(review.id, reject.id)
    g.add_edge(reviser.id, review.id)         # revise LOOPS BACK for another review
    return g


_VOTE_FRAMER_PROMPT = """Restate the task clearly for several INDEPENDENT solvers who will each attempt it separately (a self-consistency ensemble). Do NOT solve it yourself — just frame it precisely."""

_VOTE_SOLVER_PROMPT = """Solve the task independently and carefully: show your reasoning, then end with a clear FINAL ANSWER on its own line. You are ONE of several independent attempts running IN PARALLEL — do NOT coordinate; just give your best solution. (For a majority vote to work, keep the final answer in a consistent, minimal form.)"""

_VOTE_JUDGE_PROMPT = """You are given several INDEPENDENT answers to the SAME task (a voting / self-consistency ensemble), separated by '---'. Find the CONSENSUS: the answer the majority agree on. Output that final answer and note how strong the agreement was (e.g. 3/3, 2/3). If they genuinely disagree, pick the best-justified answer and briefly say why. Do not just concatenate them — decide."""


def _build_voting(llm_props: dict, tool_files, budgets) -> Graph:
    """Voting / self-consistency (best-of-N): a framer sets up the task, N INDEPENDENT
    solvers attempt it CONCURRENTLY (same prompt, diverse sampling), then a judge picks
    the CONSENSUS answer at the join. Unlike map-reduce (DISTINCT workers → synthesis),
    the solvers are IDENTICAL and the aggregation is majority/agreement — it reduces
    one-shot mistakes. Tools attach per solver when provided."""
    g = Graph()
    budgets = budgets or dict(DEFAULT_BUDGETS)
    n = 3
    framer = _unit(g, "framer", "single", -(n // 2 + 1) * 150, llm_props, budgets,
                   _VOTE_FRAMER_PROMPT, llm_temperature="0.2")
    fo = g.new_node("fanout", _COLS["agent"], -(n // 2) * 150 - 60)
    fo.name = "fanout"
    jn = g.new_node("join", _COLS["agent"], (n // 2) * 150 + 60)
    jn.name = "join"
    jn.props["merge"] = "concat"     # the judge reads all N; set 'vote' if solvers emit a bare label
    g.add_edge(framer.id, fo.id)
    for i in range(n):
        y = int((i - (n - 1) / 2) * 150)
        s = _unit(g, f"solver_{i + 1}", "single", y, llm_props, budgets,
                  _VOTE_SOLVER_PROMPT, llm_temperature="0.8")   # high temp = diverse paths
        if tool_files:                       # each solver gets its OWN tools node
            t = g.new_node("tool", _COLS["rag"], y)
            t.name = f"tools_solver_{i + 1}"
            t.props["files"] = list(tool_files)
            g.add_edge(t.id, s.id)
        g.add_edge(fo.id, s.id)              # fan-out branch (all solve the SAME input)
        g.add_edge(s.id, jn.id)              # reconverge at the join
    judge = _unit(g, "judge", "single", (n // 2 + 1) * 150, llm_props, budgets,
                  _VOTE_JUDGE_PROMPT, llm_temperature="0.2")
    g.add_edge(jn.id, judge.id)
    return g


PATTERNS.update({
    "human_approval": {
        "label": "Human approval (routing HITL)",
        "description": "An agent drafts a reply, then a route-mode HITL lets a HUMAN "
                       "pick the next step: send it, send it back for revision (loops "
                       "back for another review), escalate to a specialist, or reject "
                       "(End). Demonstrates a human-driven branch (the human mirror of "
                       "a Router), a revise loop, an End branch, and a safe default "
                       "('escalate') for unattended runs. After inserting: edit each "
                       "agent's prompt for your own workflow.",
        "builder": _build_human_approval,
    },
    "map_reduce": {
        "label": "Map-reduce (parallel workers)",
        "description": "A coordinator frames the task, N specialist workers run "
                       "CONCURRENTLY (a fan-out), and a reducer synthesizes their "
                       "results (join). Unlike a Worker Pool (identical workers over "
                       "split subtasks), each worker is a distinct agent you prompt "
                       "separately. After inserting: edit each worker's prompt to give "
                       "it a specialty; add/remove workers by wiring fan-out -> agent "
                       "-> join.",
        "builder": _build_map_reduce,
    },
    "voting": {
        "label": "Voting / self-consistency (best-of-N)",
        "description": "A framer sets up the task, N INDEPENDENT solvers attempt it "
                       "CONCURRENTLY (same prompt, diverse sampling), and a judge picks "
                       "the CONSENSUS answer at the join — reducing one-shot mistakes by "
                       "agreement. Unlike map-reduce (distinct workers, synthesized), the "
                       "solvers are identical and aggregated by majority. After inserting: "
                       "change the solver count by wiring fan-out -> agent -> join, or set "
                       "the Join's merge to 'vote' if the solvers output a bare label/number.",
        "builder": _build_voting,
    },
    "crag": {
        "label": "Corrective RAG (CRAG)",
        "description": "One agent that retrieves from your documents, self-grades "
                       "the passages and rewrites the query when results are weak, "
                       "then falls back to web search when the documents don't "
                       "cover the question — always citing sources. After "
                       "inserting: double-click the 'knowledge' node and set your "
                       "documents folder; web fallback needs `pip install ddgs`.",
        "builder": _build_crag,
    },
    "self_rag": {
        "label": "Self-RAG (self-checking)",
        "description": "One agent that retrieves from your documents, answers only "
                       "from relevance-graded passages, then auto-verifies its own "
                       "answer is grounded and on-topic — revising up to twice if "
                       "not. After inserting: double-click the 'knowledge' node and "
                       "set your documents folder.",
        "builder": _build_self_rag,
    },
    "adaptive_rag": {
        "label": "Adaptive RAG (router)",
        "description": "A router classifies each question and sends it to the "
                       "knowledge-base agent (your documents), the web-research "
                       "agent (live web), or answers trivial ones itself. After "
                       "inserting: set the 'documents' node's folder; web needs "
                       "`pip install ddgs`.",
        "builder": _build_adaptive_rag,
    },
})
