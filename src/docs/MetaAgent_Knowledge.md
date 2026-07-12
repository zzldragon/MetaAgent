<!-- GENERATED FILE — do not edit by hand.
     This is the canvas-core knowledge the built-in Estimation / design-assistant
     agent reasons from. It is rendered from design_assistant.knowledge_prompt(),
     which is DERIVED from the live code registries (KIND_META, ROLE_DEFAULT_PROMPTS,
     PATTERNS, ALLOWED_EDGES, ...) plus the curated semantics in design_assistant.py.
     Per the maintenance rule (docs/MetaAgent_UserGuide + design_assistant.py docstring),
     any canvas change must update that knowledge; then regenerate this file with:
         python -c "import design_assistant as d; open('docs/MetaAgent_Knowledge.md','w',encoding='utf-8').write(d.knowledge_prompt())"
     (or re-run tools/gen scratch). -->

# MetaAgent canvas — reference (auto-generated)

## Core design
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
runtime/ package) whose engine is a ReAct loop.

## Node kinds

### Agents (pipeline stages)
- `agent` (Agent) — A ReAct agent: an LLM in a reason→act loop that can call tools and produce a text answer. The unit of work; one pipeline stage.
- `workerpool` (Worker Pool) — An agent that fans out over a list of subtasks at runtime, running workers in parallel.
- `router` (Router) — An agent that classifies the input with an LLM and routes it to exactly ONE successor agent; may have several outgoing agent links. It forwards the same text (no data contract).

### Resources (feed an agent)
- `llm` (LLM) — The chat model + provider config for an agent (provider/model/api_key/base_url/temperature). Several LLMs on one agent form a failover chain.
- `tool` (Tools) — A library of Python functions the agent may call; each function's docstring tells the agent when to use it. The same tool file linked twice to one agent is an error.
- `skill` (Skills) — Progressive-disclosure instructions: name+description are always shown; the body loads on demand (or via a /slash command).
- `prompt` (Prompt) — The agent's system-prompt text (its persona). Singleton — at most one per agent; if absent the agent uses its role's default template.
- `rag` (RAG) — A retrieval knowledge base (a documents folder) exposed to the agent as a retrieval tool; several RAG nodes = several knowledge bases.
- `memory` (Memory) — A persistent cross-run memory store linked to an agent: gives it remember(content, tags) and recall(query) tools backed by a JSON store + BM25 retrieval, so it learns across runs (Reflexion-style).
- `webserver` (WebServer) — Makes generation emit a web server that serves the agent. At most one per graph.
- `mcp` (MCP) — An MCP client connecting the agent to an external tool server (stdio or http). Several MCP nodes = several servers.
- `eval` (Eval) — Offline test cases + graders for one agent; links to a single agent.
- `gui` (GUI) — Makes generation emit a PySide6 desktop GUI; link it to the entry agent.
- `schedule` (Schedule) — Makes generation emit scheduler.py — an ambient runner that calls an agent on an interval/daily/once (no user prompt). Link it to the entry agent to drive the whole graph, or link separate schedules to separate agents to drive each one on its own timer.
- `subgraph` (Subgraph) — Embeds another whole graph as one reusable step: its incoming edge feeds the child's entry, and the child's End (or last stage) continues to this node's successor. The child (nodes/tools/prompts/LLMs) is stored INLINE on the node and flattened in at generation time, so the parent stays self-contained; the child's shared-state fields merge into the parent's. Nested subgraphs flatten first; a recursive include is rejected.

### Control-flow
- `condition` (If/Else) — If/Else: routes to one of several branches by evaluating a predicate over SHARED STATE (not over the upstream text).
- `while` (While) — A loop guard: runs its body while a state predicate holds, else takes the exit link.
- `foreach` (For-Each) — Map-over-list: runs its body ONCE PER ITEM of a shared-state list field, the items in PARALLEL on isolated state forks (a dynamic fan-out). Each item is passed to the body both as its input and (when set) written to an item field; the body links BACK to the For-Each node, and the OTHER outgoing link is the exit, taken once after all items finish. Optionally collects each item's output into a result list field and merges the outputs (concat/first/last/state_only/vote).
- `setstate` (Set State) — Writes shared-state fields (deterministic assignments/expressions). Exactly one outgoing link.
- `guardrail` (Guardrail) — An inline content gate that redacts or blocks the content flowing through it.
- `end` (End) — A terminal sink (no outgoing links) that finishes the run early, returning whatever output reached it — handy on an If/Else else-branch or a While exit.
- `fanout` (Fan-out) — Runs its 2+ branches CONCURRENTLY (real threads), then reconverges at the paired Join; each branch is an independent agent chain that writes its own shared-state fields.
- `join` (Join) — The barrier that reconverges a Fan-out's branches: their shared-state writes merge via each field's reducer and the branch outputs combine by its merge policy (concat/first/last/state_only).

### Flow
- `hitl` (HITL) — A human-in-the-loop checkpoint. With ONE outgoing link it's a review gate (approve/edit/reject), spliced out and applied before the downstream agent. With 2+ outgoing links it's a human-driven BRANCH (route mode): the reviewer picks which successor runs next — the human mirror of a Router.

## Agent roles (runtime behaviour)
- `single` — A general ReAct agent (the default) — runs alone or as one chain stage.
- `planner` — Breaks the task into a numbered plan for the next agent; does not execute. May 'self-route' (pick its own successor) instead of a Router.
- `worker` — Executes the task (and the plan, if given) thoroughly and reports. Tool-eligible.
- `critic` — Reviews the previous agent's output; can send it back by starting its reply with 'REVISE:' (a bounded revise loop).
- `supervisor` — Delegates one instruction at a time to its workers (NEXT/DONE protocol), reviewing each result. Entry-only; workers are leaves.
- `orchestrator` — Autonomous coordinator: spawns isolated sub-agents in parallel via spawn_subagent; each runs with only its own tools and returns just its result. Entry-only; sub-agents are linked leaves.

## Edges (what a link means)
- resource → agent: an llm/tool/skill/prompt/rag/mcp node feeds capabilities into an agent; it is NOT a pipeline stage.
- agent → agent: a data handoff — the upstream agent's text OUTPUT becomes the downstream agent's INPUT. May carry a data CONTRACT (fields the producer outputs = the consumer expects): advisory prompt tails by default, or ENFORCED (the producer's output is JSON-validated and retried).
- agent → condition/while/setstate/guardrail/end: control-flow edges. condition/while pick a branch by reading shared STATE; the payload passes through unchanged.
- router → agents: the router picks ONE branch at runtime; contracts don't apply.
- eval → agent, gui → agent: attach evaluation / a desktop GUI to one agent (usually the entry).

## Validity rules
- Every agent needs at least one linked LLM.
- A plain agent or worker-pool may have at most ONE outgoing agent link. Only a router, a supervisor, an orchestrator, or a self-routing planner may branch to several agents.
- The orchestrator and supervisor roles are allowed only on the ENTRY agent; their sub-agents/workers must be plain leaf agents.
- A router must have at least one outgoing agent link.
- prompt and gui are singleton inputs: at most one of each per agent.
- The same tool file linked twice to one agent is an error.
- condition/while/foreach/setstate require shared-state fields; a condition needs branches, a while needs a guard+body+exit, a foreach needs an 'over' list field plus a body that links back and an exit, an end has no outgoing links.
- A hitl node sits between exactly one upstream and one downstream node, and cannot connect to a router or directly to another hitl.
- Data contracts apply only to agent→agent edges (not router/condition/resource).
- The entry is the agent with no incoming agent link (or a single planner); an ambiguous entry is an error.

## Shared state
- Field types: `str`, `int`, `float`, `bool`, `list`, `dict`
- A schema field is {name, type, reducer, default, description}; reserved built-ins: `tool_calls`, `agents`.

## Patterns (starting topologies)
- `react` — ReAct (single agent): One agent with tools in a reason+act loop.
    - agents: agent(single)
    - links: (no agent links)
- `planner_executor` — Planner–Executor: Planner breaks the task into steps; executor runs them.
    - agents: planner(planner), executor(worker)
    - links: planner→executor
- `planner_executor_critic` — Planner–Executor–Critic (revise loop): Adds a critic that can send the work back for revision.
    - agents: planner(planner), executor(worker), critic(critic)
    - links: planner→executor ; executor→critic ; critic→planner
- `supervisor_worker` — Supervisor–Worker (delegation loop): Supervisor delegates one instruction at a time (NEXT/DONE protocol) and reviews each result.
    - agents: supervisor(supervisor), worker(worker)
    - links: supervisor→worker
- `orchestrator` — Orchestrator (autonomous, spawns sub-agents): Orchestrator spawns isolated sub-agents in parallel via the spawn_subagent tool; each has its own tools and returns only its result.
    - agents: orchestrator(orchestrator), writer(worker), reader(worker)
    - links: orchestrator→writer ; orchestrator→reader
- `human_approval` — Human approval (routing HITL): An agent drafts a reply, then a route-mode HITL lets a HUMAN pick the next step: send it, send it back for revision (loops back for another review), escalate to a specialist, or reject (End). Demonstrates a human-driven branch (the human mirror of a Router), a revise loop, an End branch, and a safe default ('escalate') for unattended runs. After inserting: edit each agent's prompt for your own workflow.
- `map_reduce` — Map-reduce (parallel workers): A coordinator frames the task, N specialist workers run CONCURRENTLY (a fan-out), and a reducer synthesizes their results (join). Unlike a Worker Pool (identical workers over split subtasks), each worker is a distinct agent you prompt separately. After inserting: edit each worker's prompt to give it a specialty; add/remove workers by wiring fan-out -> agent -> join.
- `voting` — Voting / self-consistency (best-of-N): A framer sets up the task, N INDEPENDENT solvers attempt it CONCURRENTLY (same prompt, diverse sampling), and a judge picks the CONSENSUS answer at the join — reducing one-shot mistakes by agreement. Unlike map-reduce (distinct workers, synthesized), the solvers are identical and aggregated by majority. After inserting: change the solver count by wiring fan-out -> agent -> join, or set the Join's merge to 'vote' if the solvers output a bare label/number.
- `crag` — Corrective RAG (CRAG): One agent that retrieves from your documents, self-grades the passages and rewrites the query when results are weak, then falls back to web search when the documents don't cover the question — always citing sources. After inserting: double-click the 'knowledge' node and set your documents folder; web fallback needs `pip install ddgs`.
- `self_rag` — Self-RAG (self-checking): One agent that retrieves from your documents, answers only from relevance-graded passages, then auto-verifies its own answer is grounded and on-topic — revising up to twice if not. After inserting: double-click the 'knowledge' node and set your documents folder.
- `adaptive_rag` — Adaptive RAG (router): A router classifies each question and sends it to the knowledge-base agent (your documents), the web-research agent (live web), or answers trivial ones itself. After inserting: set the 'documents' node's folder; web needs `pip install ddgs`.

## Link-kind reference
- Resource kinds (→ agent): `eval`, `gui`, `llm`, `mcp`, `memory`, `prompt`, `rag`, `schedule`, `skill`, `tool`
- Agent-stage kinds (may have agent successors): `agent`, `workerpool`, `router`
- Control-flow kinds: `condition`, `while`, `foreach`, `setstate`, `guardrail`, `end`, `fanout`, `join`
- Singleton inputs (≤1 per agent): `gui`, `prompt`

## Default per-agent budgets
- max_iterations=0, max_tool_calls=0, max_output_tokens=0, max_wall_clock_s=0

## Available tool libraries
- `base64_decode.py`, `base64_encode.py`, `cn_market_tools.py`, `coding_agent_tools.py`, `coding_edit_tools.py`, `coding_read_tools.py`, `coding_shell_tools.py`, `create_math_pdf.py`, `csv_column_means.py`, `data_analysis_tools.py`, `load_csv.py`, `picturebook_check_tools.py`, `picturebook_illustrate_tools.py`, `picturebook_pdf_tools.py`, `picturebook_tools.py`, `research_tools.py`, `topic_memory_tools.py`, `trading_agents_tools.py`, `trading_memory_tools.py`, `us_market_tools.py`, `wechat_analytics_tools.py`, `wechat_publish_tools.py`, `wechat_rss_tools.py`, `write_human_words_to_file.py`, `writing_insights_tools.py`
