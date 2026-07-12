# MetaAgent — Engineering Guide

*Audience: software engineers (and AI assistants) working ON MetaAgent itself.*
*Consolidates the former Spec.md, AutonomousPatternDesign.md, improvement-plan.md,
remainedRequirements.md, metaagent-progress-gap.md, issue_fix.md, and TESTING.md.*

---

## 1. Overview & product vision

MetaAgent is a **PySide6/Qt desktop app** with two surfaces:

1. **Coding agent** (`canvas_qt/tool_generator.py` + `coding_agent.py`) — chat with an
   LLM that writes Python `@tool` files into the shared `tools/` library.
2. **Visual canvas** (`canvas_qt/designer.py`) — draw an agent (or a team) as a node
   graph, then **Generate Code** into a self-contained Python folder.

**The big idea: canvas → standalone Python, with ZERO LangChain at runtime.** Tool
files are inlined verbatim, the ReAct loop is hand-written, and LLMs are called
through the `openai`/`anthropic` SDKs directly. Default backend: **DeepSeek-V4-Flash
via SiliconFlow** (OpenAI-compatible). Generated agents depend on ~`openai` +
`anthropic` (plus whatever the tools import).

Launch path: `main.py` → `QApplication` → `canvas_qt/welcome.py` (the launcher) →
the designer / tool generator (lazily imported so startup stays fast).

*Origin (from the original Spec):* the first brief was a wxPython app; it has since
been fully ported to Qt. The founding requirements — pick LLM + key, pick a pattern,
name agent roles, link modules like a graph, per-agent budgets, compile to a
standalone exe — are all implemented.

---

## 2. Current software architecture

### 2.1 App structure & component map

| File | Role |
|---|---|
| `main.py` | Entry point → QApplication → welcome launcher |
| `canvas_qt/welcome.py` | Launcher (new/open project); `run_on_gui_thread` (Qt's `wx.CallAfter`) |
| `canvas_qt/designer.py` | The canvas: nodes/edges, menus, generate/run/debug/chat/estimate |
| `canvas_qt/dialogs.py` | Per-node config dialogs; `make_dialog_resizable`; edge/contract dialogs |
| `canvas_qt/tool_generator.py` | Coding-agent window (worker thread, sessions, HITL confirm bridge) |
| `canvas_qt/estimation_ui.py` | Estimation stream window + Fix-with-AI loop (see §2.11) |
| `canvas_qt/trace_panel.py` | Live run-trace panel + per-module inspector |
| `coding_agent.py` | `CodingAgent`: chat loop, sessions, memory compaction, cancel, HITL |
| `graph_model.py` | `Graph`/`Node`/`Edge`; `KIND_META`, `ALLOWED_EDGES`, `STATE_TYPES`, `DEFAULT_BUDGETS`; `expand_subgraphs`; `analyze` helpers |
| `graph_codegen.py` | N-agent code generation (`analyze()`, `generate_from_graph`, `_build_agent_specs`) |
| `graph_codegen_templates.py` | The runtime template blocks (`@*_CODE@`), ReAct loop, run_graph/pipeline/supervisor/autonomous |
| `codegen.py` | 1-node ReAct shim over `graph_codegen`; `list_tools()` |
| `patterns.py` | `PATTERNS` preset registry; `build_pattern_graph` |
| `llm_client.py` | Thin OpenAI-compatible client (lazy `openai` import, streaming, cancel) |
| `app_config.py` | `config.json` load/save (self-healing), `TOOLS_DIR`, recents |
| `design_assistant.py` | Estimation knowledge (`design_knowledge`/`knowledge_prompt`) + `graph_metrics`/`design_review` |
| `estimation.py` | Estimators, LLM-judge harness, AI fixes (`propose_fixes`/`apply_fix`) |
| `code_view.py` | "Check Code" attribution: `code_for_node()` → generated files + the node's character spans (anchor search, non-invasive) |
| `canvas_qt/code_view_ui.py` | "Check Code" viewer (`CodeViewWindow`/`show_code_view`): read-only, resizable, span highlights via `setExtraSelections` |
| `runner.py` | Runs generated GUI agents (Qt-native; frozen-interpreter resolution) |
| `runtime/*.py` | Source fragments inlined into generated apps (storage, hitl, guardrails, eval, pool, skills, …) |
| `tools/*.py` | The shared `@tool` library |
| `generated_agents/*` | **Outputs/fixtures, not source of truth** — edit templates and regenerate |

### 2.2 LLM backend & `config.json`

- **Provider-neutral, OpenAI-compatible.** The Tool Generator + Estimation key lives in
  `config["api_key"]` with `base_url` + `model` — point them at any provider (default:
  DeepSeek-V4-Flash via SiliconFlow; also OpenAI/DeepSeek/local; Anthropic path supported).
  `base_url` must be the API root (NOT include `/chat/completions`).
- **Key naming (migrated):** the host key is `api_key`. It was historically the
  SiliconFlow-specific `deepseek_api_key`; `app_config._migrate` carries a legacy value
  over to `api_key` and drops the old field on next load (one-time, transparent).
- `load_config()` (`app_config.py`) reads `config.json` **fresh on every call** and
  self-heals by merging `DEFAULTS` — so a settings change takes effect on the next
  call with no restart. Price knobs + `request_timeout_s` (default 120s) live here.

### 2.3 The two codegen engines

- `codegen.generate_agent` — a 1-node ReAct shim (tests/simple cases).
- `graph_codegen.generate_from_graph` — the real engine: `analyze()` derives
  `PATTERN_MODE ∈ {chain, graph, supervisor, autonomous}` from the entry role +
  topology, then emits `agent.py` (+ `config.json`, `requirements.txt`, `build.bat`,
  README; optionally `gui.py`/`server.py`).
- **Two injection conventions (do not break):** substitute with `str.replace` on
  `@MARKER@` tokens, **not** `str.format` (tool code contains `{}`); embed Python
  literals with **`repr()`**, **not** `json.dumps` (needs `True`/`None`, not
  `true`/`null`). Tool files are inlined with their LangChain imports stripped;
  `requirements.txt` is scanned from imports.
- **Code style:** `code_style="single"` (one self-contained `agent.py`, the Ctrl+G
  default) or `"package"` (a `runtime/` package + a thin `agent.py`). Same behavior.

### 2.4 Generated-agent runtime

- Provider abstraction blocks (`LLM_BLOCK_OPENAI` / `_ANTHROPIC`); an **ordered LLM
  list** per agent = primary + fallbacks (`MAX_LLM_RETRIES=2`, `RETRY_BASE_S=0.5`).
- **Budgets** (`DEFAULT_BUDGETS`): all default to **0 = UNLIMITED** (`max_iterations`,
  `max_tool_calls`, `max_output_tokens`, `max_wall_clock_s`) — demos run to completion;
  the designer opts into a real cap. Runtime: iterations `or 1_000_000` backstop, wall-clock
  check guarded by truthiness (so `-1` still trips for offline tests), tool-call cap guarded
  by truthiness; `max_output_tokens=0` → OpenAI `max_tokens=None` (provider default) /
  Anthropic `8000` (it requires the param). (+ `context_capacity` for entry-only context
  compaction; historical `max_input_tokens`→`context_capacity`, `max_cost_usd` removed.)
  Token estimate ≈ `chars//3`. `_verify_budgets.py`.
- **Budget scope & policy (4 layers).** The `DEFAULT_BUDGETS` above are **per-stage** and
  **soft** — checked between ReAct steps, returning a `"[budget] …"` sentinel, never mid-call.
  A single call is bounded by the LLM's `request_timeout_s` (raises → failover). Per-agent
  **`on_budget`** (`continue`|`stop`|`retry`, default `continue`, emitted only when ≠continue)
  decides what a capped stage does: `continue` flows the note downstream (legacy); `stop` ends
  the run; `retry` re-runs the stage up to `stage_retries` with a fresh budget then stops — via
  `_run_stage_budgeted()`, used by both `run_graph` and `run_pipeline`. A **run-wide** deadline
  `graph.run_wall_clock_s` → config `max_run_wall_clock_s` raises **`RunDeadline`** (a
  `RunCancelled` subclass, so fan-out/branches abort and the top-level handler returns its
  `[budget]` message) at the next step/hop boundary; `_run_deadline_check()` runs in the
  `react()` step loop and the `run_graph` hop loop, `run_start` set in `_run_inner`/`resume()`.
  The hard ceiling is `RECURSION_LIMIT` (hops → `GraphRecursionError`); user Stop = `RunCancelled`.
  All default to old behavior → byte-identical when unset. `_verify_budget_policy.py`.
- JSONL traces (`traces/*.jsonl`), history persistence + rolling-summary compaction,
  vision, streaming, cooperative cancel. Engine constants: `MAX_REVISE_ROUNDS=2`,
  `MAX_SUPERVISOR_ROUNDS=6`, `MAX_TOOL_ROUNDS=6`, `CONTEXT_WINDOW_MSGS=40`,
  `HISTORY_CAP=400`.
- **Transient-error policy** (`_retryable`): retry-then-failover fires only when the
  exception's `status_code ∈ {408,409,429,500,502,503,504,529}` or its class name
  contains `Timeout`/`Connection`/`RateLimit`/`Overloaded`/`InternalServer`; anything
  else raises immediately. Backoff = `RETRY_BASE_S * 2**attempt * (1+random())`
  (exp + jitter) up to `MAX_LLM_RETRIES`.
- **Per-agent LLM mode** (`get_llm_mode`/`set_llm_mode`, `_VALID_LLM_MODES=("fallback",
  "manual")`, persisted as `config["llm_modes"]` + a live `llm_mode.json`): `fallback`
  (default) advances through the linked LLMs in link order after per-LLM retry; `manual`
  uses ONLY the selected LLM — still retries transient errors but never fails over, so
  its error is authoritative. Codegen writes only the `manual` agents. Runtime-switchable
  from the GUI **LLM** menu's checkable *Lock to selected (no fallback)*. (`_verify_llm_manual`.)
- **Usage/cost attribution** (`runtime_overlay.py`): `RuntimeOverlay.summary(prices=None)`
  + `summarize_trace(records, prices=None)` roll a trace into a per-agent breakdown
  (in/out tokens, `tool_calls`, `llm_steps`, `status`) + run totals (`retries`,
  `failovers`, `agents_run`) from the lock-aggregated `run_end.usage`. **Cost is opt-in**
  — computed only when per-LLM prices are passed (a saved trace carries none). Surfaced in
  `trace_panel._usage_block` for live + replay. Caveat: `llm_steps` undercounts a worker
  pool (N workers share one name); tokens/tool-calls stay exact.
- **Fan-out-aware overlay** (`runtime_overlay.py`): the state machine is single-cursor for
  sequential walks (a new active node finishes the previous + lights the edge), but a
  `fanout` trace record opens **concurrent mode** (`_concurrent` = branch set): while it is
  open the single-cursor finish/edge inference is suspended, so interleaved branch records
  from the worker threads leave every branch RUNNING at once (no sibling cross-finish, no
  phantom sibling edge); branches finish on their own `stage_end`; the `join` record closes
  concurrent mode and lights each branch→join edge. Fan-out/join are control nodes — never
  `_ensure`'d, so they draw no badge and no usage row. (`_verify_runtime_overlay` §5c synthetic
  + §7 real interleaved trace.) v1 limitation: a multi-node branch's join edge is drawn from
  its entry, not its tail (shipped branches are single-agent, so entry==tail).

### 2.5 Canvas model, edge semantics & prompt resolution

- `ALLOWED_EDGES` gates connections; `SINGLETON_INPUTS = {prompt, gui}` (≤1 per
  agent; RAG/LLM/MCP are multi). Only a **router / supervisor / orchestrator /
  self-routing planner** may fan out to several agents; a plain agent/pool has ≤1
  outgoing agent link.
- **Prompt resolution order:** linked Prompt node → role template file →
  `ROLE_DEFAULT_PROMPTS`. The composed system prompt = persona + route tail + skills
  + tool guidance + contract tails; single-sourced by `_build_agent_specs` /
  `_compose_system_prompt` (also used by `system_prompts_for_graph` = "Dump System
  Prompts").
- **Link (edge) contracts:** an agent→agent edge can carry a data-handoff contract
  (`edge.props["contract"] = [{name,type,description}]`), advisory by default,
  optionally **enforced** (`contract_enforce` → `CONTRACTS_OUT`; the producer's
  output is JSON-validated + retried, `contract_max_retries`, default 2). Eligibility:
  `src ∈ (agent, workerpool)` and `dst ∈ AGENT_KINDS`. HITL splice preserves the
  contract (incl. enforce flags) across a review gate.

### 2.6 Execution patterns & role taxonomy

- **chain** — linear pipeline; each agent's text output is the next's input.
- **graph** — branches/loops/control nodes (`run_graph`, single-cursor walk;
  condition/while route on **shared state**, not the payload).
- **supervisor** — a supervisor entry delegates to workers via a text `NEXT:`/`DONE:`
  loop (sequential).
- **autonomous / orchestrator** — an orchestrator entry spawns **isolated, parallel**
  sub-agents via the built-in `spawn_subagent` tool (enum-constrained to its linked
  leaf agents; `parallel_tools` on; each spawn = a fresh `react(name, task)` returning
  only its final string). Entry-only; no nested orchestration in v1; no new node/link
  type — sub-agents are plain Agent leaves. Isolation: own context, tools, usage,
  failure; runaway guards (spawn/concurrency/depth caps, shared budget).
- **Role taxonomy:** `single` (general), `planner` (plans; `route_self` = planner-only,
  ≥2 successors, flips to graph mode), `worker` (executes; tool-eligible), `critic`
  (can `REVISE:` in a bounded loop), `supervisor` (NEXT/DONE), `orchestrator`
  (spawn_subagent). `router` is a node kind, not a role.
- **Branch selection = a `route_to(agent)` tool, not `response_format`.** Routers and
  self-routing planners pick a branch via a built-in enum-constrained `route_to` tool
  (`_route_tool_schema`; enum = successor names, `__none__` when `quick_response`), NOT
  JSON-schema structured output (weakly enforced on DeepSeek/SiliconFlow and it clashes
  with the planner being a ReAct agent). `_extract_route` resolves most-reliable-first:
  `route_to` call → a final `ROUTE: <name>` text line → longest-substring match →
  first-successor fallback. A `route_self` planner records its pick in the module global
  `_ROUTE["choice"]`, strips the `ROUTE:` line, and passes the plan on with no extra
  round-trip.
- **A Prompt node linked to a Router is a silent no-op.** `_persona()` for a router
  returns its own `instructions` (else `DEFAULT_ROUTER_PROMPT`) and never reads a linked
  prompt (`prompt→router` is a legal edge but ignored). `route()` describes each branch as
  *name + first persona line + [tools]*, so branch names / first lines are load-bearing;
  an ambiguous reply falls back to the first successor (order matters).
- **Delegation tuning (autonomous).** `spawn_subagent` is a plain tool, so the LLM decides
  delegate-vs-answer — tune against over-delegation (spawning trivial work) and
  under-delegation (never spawning). Strongest lever = **tool surface**: the preset is a
  *pure planner* (`patterns.TOOL_ROLES` excludes `orchestrator`) so the orchestrator gets
  only `spawn_subagent` and Tool nodes link to the sub-agents; linking a tool to the
  orchestrator makes a *hybrid* (`graph_codegen` appends `spawn_subagent` to its tools). No
  floor forces spawning; fan-out is bounded by the orchestrator's `_ROLE_BUDGETS`
  `max_tool_calls`/`max_iterations` (there is no `max_spawns` key).
- **Autonomous isolation invariants (runtime).** A spawn suppresses the sub-agent's token
  stream (`_spawn_subagent` nulls `_TOK.on_token` around the nested `react()`, restored in
  `finally`) — the parent chat sees only `[spawn]`/`[spawn done]` trace lines + `spawn`/
  `spawn_result` events, never sub-agent tokens. Run-level singletons stay entry-scoped:
  staged vision images are consume-once (`_take_run_images()`, so the entry takes them
  before any spawn) and context compaction runs only for the entry; sub-agents lack
  `route_to` and autonomous mode never reads `_ROUTE`.
- **Worker-pool DAG (opt-in).** A `planner` with `structured_plan` appends a typed
  ```plan``` fence (`{id, subgoal, depends_on}`); a downstream Worker Pool with `dag_plan`
  runs it dependency-aware via `_run_pool_dag` — independents parallel up to `max_workers`,
  each dependent waits for and is fed its prereqs' outputs; per-subgoal fault isolation.
  Tolerant parse (`_parse_subgoal_spec`) + Kahn acyclicity; any absent/invalid/cyclic plan
  falls back to the flat `run_pool` split (`pool_spec_fallback` trace), so default behavior
  is unchanged. `analyze()` warns if `structured_plan` is on with no downstream `dag_plan`
  pool.

### 2.7 HITL gating (all scopes)

- **Coding agent:** confirms `save_tool` only; Deny default; module-global swappable
  `_CONFIRM` handler (Qt installs a blocking Event+signal bridge).
- **Generated agents:** keyword `HIGH_RISK_MARKERS` gate risky tools; a **1-outgoing**
  canvas `hitl` node is spliced out (`_splice_hitl`) and applied as a review gate before
  the downstream agent (`_human_gate`).
- **Routing HITL (human-driven branch):** a `hitl` node with **2+ outgoing** edges is NOT
  spliced — it survives as a runtime stage (`STAGE_KINDS[name]=='hitl'`, branch set in
  `SUCCESSORS`, prompt/timeout/default in the `HITL_NODES` table) and `run_graph` dispatches
  it via `_human_route`, the human mirror of `route()`: it calls `human_review(..., choices=
  SUCCESSORS[name])` and maps the reviewer's pick to a successor (default_route on timeout /
  out-of-set). Route-mode detection = "a hitl node survived the splice" (`_is_hitl_route` =
  2+ flow-successors incl. End). `flow_successors`/entry-count/graph-mode all treat a route
  HITL like a control node; `human_review`/review handlers gained an optional `choices` arg
  (GUI branch-buttons, web numbered picker, console menu) — a legacy 2-arg handler still
  works (gate mode byte-identical; route mode falls back to the default branch). Verified by
  `_verify_hitl_route.py`.
- **Risk classification precedence** (`is_high_risk`): `config.high_risk_tools` (always
  confirm) → `config.safe_tools` (never) → the `HIGH_RISK_MARKERS` name-substring heuristic
  (`high` wins if a tool is in both). The two lists are auto-populated at codegen by
  `graph_codegen._tool_risk`, which AST-parses each tool's own `@tool(risk="high"|"safe")`
  declaration (authoritative over the name guess) and leaves them editable in config.json.
  `is_parallel_safe` follows the same classification. This fixes both the false-positive
  (`update_dashboard` wrongly prompting) and false-negative (`refresh_cache` silently
  bypassing HITL) cases of the old name-only heuristic.
- **Web/WebSocket:** a real **bidirectional** HITL round-trip — `server.py` sends
  `hitl_confirm`/`hitl_review` and the worker thread blocks until the browser replies
  with `hitl_response` (see `_hitl_request`/`_resolve_pending`). `AUTO_ALLOW` is only
  the between-runs / no-client-connected default, not the in-run behavior.
- **Headless (no UI):** auto-deny / auto-approve as configured.

### 2.7b Multi-user sessions & concurrency (generated runtime)

The generated runtime supports **concurrent, per-session-isolated** runs so one `server.py`
serves many users at once. The whole per-run state lives on a `_RunState` object held in a
`contextvars.ContextVar` **`_CURRENT_RUN`**; `_rs()` returns it. A process-global
**`_DEFAULT_RUN`** wraps the module globals, so the GUI/CLI/single-session path (and any code
not entered through `run()`) is **byte-for-byte the old behaviour** and `mod.HISTORY`/`USAGE`/…
still reflect it.
- **Fork:** `run(session_id=…)` forks `_RunState.fresh(sid)` into the ContextVar (else runs on
  the default), registers it in `_ACTIVE_RUNS[sid]`, and resets in `finally`. Pool workers
  inherit it via `ex.submit(contextvars.copy_context().run, work, i)` — a **fresh copy per
  submit** (one `Context` can't be `.run()` on two threads at once). `asyncio.to_thread`
  (parallel tools) copies context itself.
- **Migrated off module globals → `_rs().X`:** cancel/stream, `_RUN` (rec), cost, usage, route,
  ctx, images (`runtime/image.py`), HITL confirm/review/allow **+ the HITL lock**
  (`runtime/hitl.py`, console fallback), trace (`runtime/trace.py`), workspace skills
  (`runtime/skills.py`). Conversation history is a per-session `_MEM` registry in
  `runtime/history.py` (LRU, `config['sessions_in_memory']`≈200; source of truth is `_STORE`).
- **Server (`SERVER_TEMPLATE`):** the single `_RUN_LOCK` is gone → a **per-session
  `asyncio.Lock`** (`_session_lock(sid)`: own turns serialize, different sessions concurrent).
  HITL handlers are passed **into** `core.run(on_confirm=, on_review=)` (never a global
  `set_confirm_handler` — that races); `_HITL_PENDING` slots carry an `owner` so
  `_resolve_pending(owner)` and the `hitl_response` handler are session-scoped;
  `request_cancel(session_id=…)` Stops only that user's run. Per-session `workspace-<sid>.json`
  (falls back to the shared `workspace.json`).
- **Package code-style gotcha:** `_rs()`/`_CURRENT_RUN`/`_DEFAULT_RUN` and every wrapped global
  must co-locate in `runtime/_core.py` (feature modules only `from ._core import *`); HITL/skills
  keep their own module state and reach the run via `_rs()`.
- **Deliberate process-globals (not per-session):** `LLM_CHOICE`/`LLM_MODE`, `_clients`,
  `_RPM_LAST`, `_MCP` (start guarded by `_MCP_START_LOCK`), `_TRACE_SINK` (designer overlay),
  `ACTIVE_MODE` (multi-pattern `/mode` runs serialize via `_MODE_LOCK`). Hardened after a
  4-lens adversarial concurrency review (cross-session HITL resolution/starvation/ownership +
  workspace-skills leak, all fixed; verified by the per-run-lock / skills-isolation /
  8-session-overlap checks in `_verify_session_isolation.py`).

### 2.8 Feature inventory

Nodes: agent, workerpool, router, llm, tool, skill, prompt, rag, memory, mcp, hitl,
condition, while, foreach, setstate, guardrail, end, fanout, join, webserver, gui, schedule,
eval, subgraph (**24 kinds** — keep in sync, see the User Guide's maintenance rule).
**For-Each** = a control node that maps a body sub-flow over a runtime list: `run_graph`'s
`foreach` arm reads `state[over]` and runs `body` once per item in parallel, reusing the
fan-out engine (`ThreadPoolExecutor` + `_run_branch` with `stop_at`=the For-Each node,
`_apply_state` capture/replay, `_merge_branch_builtins`, `_join_merge`); each item is passed
as the body's input AND (when `item_var` set) into that state field; `result_field` collects
outputs. Emitted as the `FOREACH` table (`_foreach_region` bounds the body), byte-identical
when absent — `_verify_foreach.py`. **Subgraph** =
a composition node embedding a whole child graph inline (`props.graph_json`); `expand_subgraphs`
flattens it at analyze/generate time (child names namespaced `sub/name`, End→pass-through, state
merged into the parent), so it adds no runner and generates byte-identically when absent —
`_verify_subgraph.py`. **Memory** = a resource
node giving its agent persistent `remember`/`recall` tools (JSON store + reused RAG BM25) for
cross-run (Reflexion-style) learning — `runtime/memory.py`. **Schedule** = an emitter node
(like gui/webserver) that writes `scheduler.py` — an ambient runner calling `agent.run(task)`
on an interval (config-driven; `write_scheduler`/`SCHEDULER_TEMPLATE`). MULTIPLE schedule
nodes → `config["schedules"]` list → one concurrent thread per job (own task/period/offset/
session, reusing session isolation). Per-agent scheduling + webhook triggers = future. **Fan-out/Join** = true concurrent
branch execution (ThreadPoolExecutor + per-branch forked state; reducer-merged fan-in). RAG (BM25 default; configurable chunk/embed/
retrieval/MMR/rerank; free local embedding fallback; memory/chroma/faiss/qdrant stores —
qdrant embedded on-disk or remote). MCP
(stdio + streamable_http; multiple servers). Vision. Parallel tools. LLM node options
(temperature/top_p/response_format text|json_object|json_schema with vendor-aware
support; Gemini provider; per-LLM timeout). Shared state (typed schema + reducers).
Guardrails (per-agent hooks + the inline guardrail node). Skills (progressive
disclosure + `/slash`). Eval graders (15-grader registry). Runtime debug overlay.
Multi-pattern runtime switching (`mode_label` → `/mode`).

- **Extra Settings (per-node, opt-in; blank = byte-identical).** Collapsible groups
  expose advanced knobs, emitted only when set. LLM node: sampling extras (fold into
  the per-LLM `extra` dict merged by `_sampling_kwargs`), `max_retries` (overrides
  `MAX_LLM_RETRIES`), `tool_choice` (first-turn only, via `_TOK.force_tools` thread-local
  + `_tool_choice_kwargs`; never changes `_call_one`'s signature), `price_in/out_per_1m`.
  Agent: `mode_label`, `max_rpm` (`_rpm_throttle`), `stage_retries` (wraps `run_stage`,
  excludes cancel/human-stop/loop-guard), `max_budget_usd` (`_RUN_COST` accrued in
  `_track`, checked in react()), `final_schema` (+retries → `_validate_final_schema`,
  a JSON-Schema subset, coerces the final answer). Eval node: full 15-grader registry
  (`type`/`checks`/`not`). Tool node (`tool_props` per function, aggregated app-wide):
  `return_direct` (react short-circuit), error policy return|retry|raise ('raise' →
  `ToolAborted`, excluded from stage_retries), risk override (→ high_risk_tools/safe_tools),
  description override (→ `tool_schema`). HITL node/agent: `decisions` (coerced in
  `_human_gate`) + `timeout`/`on_timeout` (`human_review._call_with_timeout`, review-gate
  only). Guardrail node: custom `patterns`/`keywords`/`max_length` (`_gr_node_patterns`).
  RAG node: `score_threshold` (gated to dense/cross-encoder), `metadata_filter` (source
  glob, `_rag_source_match`), `multi_query`+`multi_query_n` (`_rag_multi_queries`, RRF-fused)
  — runtime/rag.py. MCP node: `allow_tools`/`deny_tools`, `connect_timeout`/`call_timeout`,
  `env` (stdio, merged w/ os.environ), `headers` (http) — runtime/mcp.py `_mcp_srv`. While:
  per-loop `max_iterations` (`CONFIG["while_max_iterations"]` + `_wcounts`). Router:
  `default_route` (threaded through `_match_route`/`_extract_route`) + routing-LLM override
  (prepended to `CONFIG["llms"][router]`). WebServer (SERVER_TEMPLATE): `auto_allow_tools`,
  `tls_cert`/`tls_key` (wss), `allowed_origins` (CORS), `max_connections`. GUI node:
  `custom_gui` — a user-authored single-file `gui.py` SOURCE emitted verbatim in place
  of the built-in window (with `@AGENT_NAME@` substituted); drives the agent via
  `import agent` / `agent.run(...)`. `analyze()` blocks on a syntax error and warns when
  the source never imports/runs the agent; the dialog's Choose/Clear buttons + status
  label wrap `_validate_custom_gui`. `codegen.write_gui(out_dir, name, custom_src="")` /
  `generate_from_graph` derive it from the entry GUI node before `_splice_hitl`; blank =
  the standard window (byte-identical). All wired in `graph_codegen` + the runtime
  templates; tested by `_verify_phase2..5.py` / `_verify_while.py` + `test_canvas_dialogs.py`.
- **Resource-node naming.** Tool / Prompt / Skill node names are canvas-only labels —
  codegen emits the artifact (tool `.py` / prompt text / skill items), not the name, so
  renaming them doesn't change output. **MCP is the exception:** `_mcp_server_config` writes
  `{"id", "name"}` into `config['mcp_servers']` and `runtime/mcp.py:_mcp_label` prints the
  friendly name (not the id `mcp_3`) in `[mcp] …` log lines (the id stays the attachment /
  session key). **RAG is also name-sensitive:** with 2+ RAG nodes `rag_tool_name()` →
  `search_<slug(name)>` and the name is the KB key in `config['rag']` (a lone RAG keeps the
  fixed `search_docs`).

### 2.9 Packaging & distribution

- **PyInstaller in a clean venv** (avoids 200MB exes from a fat env).
- `runner.py` resolves the interpreter frozen-vs-source (`_python_exe`,
  `missing_modules`) so generated GUIs launch either way.

### 2.10 The `generated_agents/` gallery

~40+ near-identical example apps used as a feature/regression corpus. **They are
outputs, not source** — never hand-edit them; change the templates
(`codegen.py`/`graph_codegen.py`/`graph_codegen_templates.py`) and regenerate.

### 2.11 Estimation / design-assistant (recent)

Deterministic-first design review + optional LLM judgment, all read-only except the
opt-in Fix loop. `design_assistant.py`: `analyze()`-backed `design_review` +
`graph_metrics` + the **derived-from-registries** `design_knowledge`/
`knowledge_prompt` (the estimation agent's knowledge; see doc ③). `estimation.py`:
`estimate_graph/prompts/tools/all`, a grounded one-shot LLM-judge harness (`judge`,
degrades on no-key/parse/cancel/network), and the **Fix-with-AI loop** — batched per
agent (prompts) + per function (tool docstrings), HITL-confirmed, `analyze()`/
`py_compile` self-check with auto-revert, then re-estimate and repeat
(`MAX_FIX_ROUNDS=5`). The built-in knowledge MUST stay in sync with the canvas (rule
in the User Guide + `design_assistant.py` docstring; enumerable parts guarded by
`tests/_verify_design_assistant.py`).

---

## 3. Coding rules & conventions

- **Codegen substitution:** `str.replace` on `@MARKER@` (never `str.format`);
  `repr()` for embedded literals (never `json.dumps`). A renamed/typo'd `@MARKER@`
  must fail at codegen (guarded by `test_marker_guard`).
- **Templates are the source; `generated_agents/` are outputs.** Generator fixes go in
  `codegen.py` / `graph_codegen.py` / `graph_codegen_templates.py` — never edit a
  generated app.
- **Runtime invariants** (proven by past bugs — keep them true):
  - Never persist an aborted run — `[cancelled]`/`[budget]`/`[error]`/HITL-reject must
    NOT enter chat history.
  - Bound in-memory history like disk (`HISTORY_CAP`); when trimming, never orphan a
    `tool_calls`↔`tool` message pair.
  - Rolling-summary compaction lives in the system prompt / `history_summary.txt`.
  - **Prompt-cache-stable system prefix:** `build_system()` emits a byte-stable prefix
    (persona + skills + guidance + `_UNTRUSTED_CLAUSE`) and appends the ONLY per-call
    volatile part LAST (`workspace_context()`'s `os.listdir`); shared-state values
    (`_state_preamble`) and history live in the USER turn, never the system prompt. Never
    leak volatile content into the prefix or you bust provider prompt caching (guarded by
    `tests/_verify_prompt_cache.py`: no-workspace prompt is a strict byte prefix of the
    with-workspace one across a workspace mutation).
  - **Instant-Stop / single streaming thread:** `request_cancel()` sets `_CANCEL` **and**
    force-closes the one `_ACTIVE_STREAM`, so a blocked pre-first-token read aborts at once
    — safe only because ONLY the main run thread streams (`_TOK.on_token` is thread-local;
    pool/DAG/spawn workers, the compactor and eval graders run non-streaming). Companion:
    `_call_with_retry()` returns `("", [])` on any exception while `_CANCEL` is set (no
    retry, no failover), so the force-close error is never retried. Mirrored in the coding
    agent's `LLMClient` (`_active_stream` + `cancel()`).
- **Estimation/knowledge sync rule** (see §2.11).
- **Keep startup light:** heavy imports (`openai`, codegen backend) are lazy /
  background-prewarmed; don't add eager heavy imports to the launcher path.
- **Fragment inlining contract:** each `runtime/*.py` fragment's disk text must equal
  the loaded `*_CODE` constant (guarded by `test_runtime_fragments`).

---

## 4. Known bugs, limitations & technical debt

### 4.1 ⚠ LIVE security action — credential hygiene
`config.json` has historically shipped a **real API key** (`api_key`, formerly
`deepseek_api_key`, `sk-…`). Highest priority. Concrete remediation:

- **Rotate provider-side first** — the `sk-…` is *live*; blanking the file does **not**
  invalidate it. Regenerate the key in your provider's console, then put the new one in your
  local (git-ignored) `config.json`.
- **Blank the field** — set `"api_key": ""`. This matches `app_config.DEFAULTS`, so
  `load_config()`'s `{**DEFAULTS, **cfg}` merge self-heals and the app still starts.
- **Add a `.gitignore`** (the repo has none; a `git init` would stage the key + ~3.3k trace
  files):
  ```gitignore
  config.json
  recent_projects.json
  chat_history.json
  chat_summary.txt
  *.migrated
  __pycache__/
  .ruff_cache/
  .pytest_cache/
  generated_agents/*/traces/
  ```
- **Ship a committed `config.example.json`** template (mirror of `DEFAULTS`, empty key):
  ```json
  {"api_key": "", "model": "deepseek-ai/DeepSeek-V4-Flash",
   "base_url": "https://api.siliconflow.cn/v1", "hitl_confirm": true,
   "request_timeout_s": 120, "proxy": "", "context_capacity": 0, "theme": "dark"}
  ```

### 4.2 Open correctness bug — two divergent `save_tool` paths
The LLM path (`_save_tool`) validates (`_safe_name` + `compile()` + trailing newline);
the GUI "Save Tool(s)" button (`CodingAgent.save_tool`) writes **verbatim** (no
sanitize/syntax check). Fix = a shared `_write_validated_tool` helper.

### 4.3 Naming debt — RESOLVED
The host key was historically the SiliconFlow-specific `deepseek_api_key`; it is now the
provider-neutral `api_key`, with a one-time back-compat migration in `app_config._migrate`.

### 4.4 Concurrency debt — per-agent cost budget race
The per-agent budget check reads `USAGE` **without the lock** under concurrency
(orchestrator spawn + worker-pool parallel paths) → may overshoot slightly.

### 4.5 Supervisor + worker-pool degrades to one worker per instruction
Supervisor mode dispatches one instruction at a time; a worker pool under it doesn't
batch-dispatch. Needs batch-dispatch to parallelize.

### 4.6 Cancellation limit
A **pre-first-token** stall can't be interrupted except via the force-close-active-
stream mechanism; bounded by `request_timeout_s` (default 120s).

### 4.7 Stability findings not caught by the suite
(See MEMORY `metaagent-stability-findings` — status may have changed.) Historically:
a dangling edge (missing endpoint) and an agent NAME containing `"""` (docstring
injection). Verify against current `analyze()`/`from_dict` before acting.

### 4.8 Known test flake
`_verify_web_hitl_confirm.py` — a websocket timing race, unrelated to codegen.

### 4.9 Doc/test hygiene notes (this consolidation)
- The old User Guide said "17 node kinds" while `KIND_META` has 18 (`guardrail`) — a
  drift the new maintenance rule targets; fix in the User Guide.
- Test dir is `tests/` (plural). `test_canvas_qt.py` was split into
  `tests/canvas/test_canvas_*.py` (+ a subdir `conftest.py`).

---

## 5. Future direction & roadmap

### 5.1 Positioning & guiding principle
Defensible niche: a **desktop-native visual builder that compiles to a standalone
exe**, with hard budget enforcement, coding-agent tool auto-generation, MCP-first, and
a model-agnostic router (DeepSeek as opt-in). Out of scope: community size,
hosting/multi-tenancy, integration-catalog breadth. **Guiding rule: add by
demonstrated need, not because a pattern sounds cool.**

### 5.2 Near-term follow-ups
- Web/console **model switcher**; coding-agent **model list**; eval-linked trace view.
- **Granular / diff-style HITL** review; websocket **interrupt/steering** beyond Stop;
  explicit per-file context selection; a cheap "explore" sub-agent.
- Estimation: extend Fix ops beyond prompts/docstrings (safe structural graph edits);
  the full `CodingAgent`-injectability refactor for a conversational design assistant.

### 5.3 Worth-later
Long-term **memory node** (profile/lessons/episodes, reusing RAG BM25); an **eval CI
gate** (GitHub Action / pre-commit); model tiering.

**Custom GUI in the GUI node** — the *single-file* path is **done** (Phase 5; see
§2.8 Extra Settings). A GUI node's `props["custom_gui"]` carries a user-authored `gui.py`
source that `codegen.write_gui`/`generate_from_graph` emit (`@AGENT_NAME@`-substituted) in
place of `GUI_TEMPLATE`; `GUIDialog` has Choose/Clear buttons + a status label backed by
`_validate_custom_gui`; `analyze()` blocks on a syntax error and warns if the source never
imports/runs the agent; blank = the built-in window (byte-identical). The front-end drives
the agent through the generated `agent.py` `core` API — `core.run(task, emit, on_token,
images)` plus a read↔write pair per configurable parameter, each setter persisting a sidecar
JSON so the *next* run picks it up (`get_llm_options`/`set_llm_choice`→`llm_choice.json`;
`rag_enabled`/`set_rag_enabled`; `skills_for`/`add_skill`; HITL `set_confirm_handler`/
`set_review_handler`; RAG/MCP accessors are feature-gated — validation must not require them).
Prototyped end-to-end in `prototype/custom_gui/`; contract in `prototype/custom_gui/CONTRACT.md`.
- *Deferred (multi-file):* a single string breaks once the GUI spans sibling `.py` files —
  store a `{relpath: source}` map or a base64 zip, write all files next to `agent.py` (keep
  `gui.py` as the launched entry), guard reserved names (`agent.py`, `config.json`,
  `requirements.txt`, `build.bat`, `run_evals.py`, `server.py`, sidecar `*.json`), handle
  sub-packages + binary assets. Alt path: Qt Designer `.ui` + `QUiLoader` + a thin generated
  controller.

### 5.4 Autonomous-pattern next phases
Bounded nesting; an opt-in constrained "freeform" sub-agent.

### 5.5 Parallel fan-out/join — deferred next phases
The core fan-out/join feature is DONE (see §2.4 fan-out-aware overlay, §5.6 restraint list;
full phase log in `.claude/plans/parallel-fanout-join-v2.md`). Two phases were **deliberately
deferred** after a value/risk review (2026-07-06). Recorded here so a future implementer has
the decision, the trigger to revisit, and an approach sketch. **Do 5b before 3** if either is
picked up.

- **Phase 3 — frontier-aware checkpoint (perf-only; SKIP unless proven needed).**
  *What:* checkpoint *inside* a fan-out (per-branch) so a resume after a mid-fan-out
  stop/crash skips already-completed branches instead of re-running the whole fan-out.
  *Why deferred:* it is **not a correctness fix**. `run_graph` saves the checkpoint at the
  top of each hop, so at the fan-out node it snapshots the **pre-fan-out** state; a resume
  restores that and re-runs the entire fan-out, which forks from the same base and produces
  the identical merge — **no double-counting** (accumulate reducers are not re-applied on top
  of a partial result). So the fan-out is already an **atomic super-step**. Phase 3 only saves
  the cost of re-running the few completed branches, and only when branches are expensive AND
  crashes land mid-fan-out often (rare). *Risk/effort:* HIGH — per-branch checkpoint records,
  tracking completed branches, rehydrating partial branch state + `capture` lists, and merging
  on resume, all through the trickiest concurrency code (the capture-replay merge). *Revisit
  when:* a real workload shows resume cost from re-run branches actually matters. Bad trade
  otherwise.

- **Phase 5b — control flow inside a branch (real expressiveness gap; DEFER).**
  *What:* allow a Router / If-Else (`condition`) / While / nested Fan-out **inside** a branch.
  Today `_fanout_region` (graph_codegen.py) requires each branch to be a **linear** chain of
  `agent`+`setstate` stages with a single successor until the join; `_run_branch`
  (graph_codegen_templates.py) walks that linear chain (`node = succ[0]`). *Why deferred:* no
  shipped graph needs it — every current branch (TradingAgents' 4 analysts, the `map_reduce`
  workers) is a single agent, and branch-internal logic can be done inside the agent via tools.
  *Approach:* make `_run_branch` a **recursive `run_graph`** that takes a `stop_at` boundary
  (the join) and its own per-frame forked state — i.e. unify the two walkers so a branch can
  run routers/conditions/whiles and even a **nested** fan-out. *Risk/effort:* MEDIUM-HIGH —
  nested `ThreadPoolExecutor`s (thread-explosion + a nested concurrency cap), cancel propagation
  through nested pools, nested capture-replay + reducer merge at each inner join, and recursion
  limits. Relax the `_fanout_region` linearity check in lockstep. *Revisit when:* a user graph
  genuinely needs graph-structured control flow inside a parallel branch.

### 5.6 Explicitly NOT recommended (restraint list)
Heavy embeddings / vector-RAG + rerank as the default; a sandbox for untrusted tool
code; deeply nested subgraphs; tree-of-thought / MoE. *Checkpoint/resume is DONE.*
***True parallel fan-out + barrier-join is now DONE** (Fan-out/Join control nodes:
concurrent branches on a ThreadPoolExecutor, per-branch forked state, reducer-merged
join; v1 branches are linear agent+Set-State chains — nested fan-out and if/else inside
a branch remain future work.)*

---

## 6. Testing — the regression oracle

Two tiers. The whole point: **codegen output + runtime behavior are pinned by tests,
so refactors can be proven behavior-preserving.** (`tests/conftest.py` puts the repo
root on `sys.path`.)

### 6.1 Fast tier (~7s, offline) — run after every edit
```
make check-fast    # or: python -m pytest tests/test_runtime_fragments.py \
                   #        tests/test_generate_matrix.py tests/test_marker_guard.py \
                   #        tests/test_prompt_parity.py -q
```
| Test | Pins |
|---|---|
| `test_runtime_fragments` | each `runtime/*.py` fragment compiles + disk text == loaded `*_CODE` |
| `test_generate_matrix` | 15 graphs generate & every emitted `.py` compiles; `@MARKER@` guard has no false-positive |
| `test_marker_guard` | a renamed/typo'd `@MARKER@` fails at codegen, naming the offender |
| `test_prompt_parity` | gen-time vs runtime "## Available skills" block stay byte-identical |

### 6.2 Full tier (slow) — before declaring a phase done
```
make check-all     # or: python -m pytest tests/ -q
```
Adds the `_verify_*.py` scripts, run as isolated subprocesses by `test_verify_suite.py`
(`make check-verify`). Canvas GUI tests live in `tests/canvas/` (run one file for a
fast targeted check, e.g. `python -m pytest tests/canvas/test_canvas_dialogs.py -q`).

### 6.3 Rules for any refactor
1. Fast tier green after every step. 2. Full tier green before closing a phase.
3. A single green `_verify_*` run isn't proof its inner asserts executed — lean on the
fast tier. 4. For byte-identical changes, **diff an example `agent.py` before/after**.

### 6.4 Environment notes
Repo is **not under git**; `make` may be absent on Windows (the `Makefile` doubles as a
command record). Known flake: `_verify_web_hitl_confirm.py` (websocket race). Qt tests
run under `QT_QPA_PLATFORM=offscreen`.
