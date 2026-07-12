---
name: design-mta-graph
description: >-
  Use when designing, authoring, or hand-writing a MetaAgent agent graph — an `.mta`
  bundle or its `graph.json` (nodes / edges / props) — choosing which pattern fits a
  task (chain, router, supervisor, orchestrator, fan-out/join, condition/while, HITL,
  map-reduce, voting, memory, schedule), OR packaging the result: writing/sharing tool
  files, sharing `.mta` graphs, generating the standalone agent, and compiling either
  the generated agent OR the MetaAgent designer itself to a Windows `.exe`. Teaches the
  graph JSON shape, the edge + mode rules, a progression from a single "hello" agent to
  complex multi-pattern graphs, and the full share/compile lifecycle. Trigger on:
  "MetaAgent graph", ".mta", "graph.json", "design an agent graph", "which pattern",
  "router/supervisor/fan-out/HITL node", "share tools/graphs", "compile MetaAgent to exe".
---

# Designing a MetaAgent (`.mta` / graph.json) graph

MetaAgent turns a **node-and-edge graph** into a standalone, zero-dependency Python
agent (`agent.py`). You design the graph; codegen emits the runtime. This skill is the
map from *"what do I want the agent to do"* → *which nodes + edges to draw*.

**Two ways to author.** (1) The **canvas** (`python -m canvas_qt`) — drag nodes, draw
links, double-click to configure; this is the source of truth and validates as you go.
(2) **By hand** — write `graph.json`, load it with `graph_model.load_mta` / `Graph.from_dict`,
then `graph_codegen.generate_from_graph(graph, name)`. Prefer the canvas unless you're
scripting; hand-JSON must obey the rules below or `analyze()` rejects it.

The authoritative reference is `docs/MetaAgent_UserGuide.md`. This skill is the quick,
pattern-routing version.

> **📋 Full parameter reference → [`ConfigTable.md`](ConfigTable.md)** (next to this file).
> This skill teaches *which* nodes/patterns to pick; `ConfigTable.md` is the exhaustive
> table of **every node, every configurable parameter, its type/default/allowed values,
> when to use it, and the preferred value in specific situations** — plus link contracts,
> shared state, budgets, graph-level settings, and storage. When you need the exact knobs
> for a node (not just which node), read `ConfigTable.md`. It is the intended reference for
> an automated **design agent**.

---

## 1. The file: what a graph is

`.mta` = a ZIP bundle: `manifest.json` + `graph.json` + bundled `tools/*.py`. The graph
itself is `graph.json`:

```json
{
  "nodes": [
    {"id": "agent_1", "kind": "agent", "name": "assistant", "x": 0,   "y": 0, "props": {"role": "single"}},
    {"id": "llm_2",   "kind": "llm",   "name": "model",     "x": 300, "y": 0,
     "props": {"provider": "siliconflow", "model": "deepseek-ai/DeepSeek-V4-Flash",
               "api_key": "", "base_url": "https://api.siliconflow.cn/v1"}}
  ],
  "edges": [
    {"src": "llm_2", "dst": "agent_1", "props": {}}
  ],
  "state_schema": [],
  "recursion_limit": 0,
  "storage": {}
}
```

- **Node** = `{id, kind, name, x, y, props}`. `id` is unique (`"<kind>_<n>"` by convention);
  `name` is what shows in prompts/tables and **must be unique across all stage nodes**.
  `props` are kind-specific (see §3).
- **Edge** = `{src, dst, props}`. Direction matters. `props` is usually `{}`; on an
  agent→agent link it can hold a data-handoff `contract`.
- **`state_schema`** = graph-level shared state (§ pattern L3): a list of
  `{name, type, reducer, default, description}`.
- **`api_key` stays blank** in the graph — the *end user* sets it (config.json / GUI
  Settings); the designer supplies a key only for a Debug Run.

> **Tip — discover exact props:** the cleanest way to get a node kind's real prop shape
> is `python -c "import graph_model,json; print(json.dumps(graph_model.default_props('router'),indent=2))"`,
> or build one node in the canvas, Save, and read the `.mta`'s `graph.json`.

---

## 2. The mental model: four node families + the edge rule

Every kind is one of four families, and edges are only legal between certain families
(`graph_model.ALLOWED_EDGES`):

| Family | Kinds | Role | Edge |
|---|---|---|---|
| **Stage** (runs) | `agent`, `workerpool`, `router` | the units that execute (an LLM loop) | stage→stage = **flow** |
| **Resource** (feeds an agent) | `llm`, `tool`, `skill`, `prompt`, `rag`, `memory`, `mcp` | attach *into* a stage | resource→agent |
| **Control** (flow, no LLM) | `condition`, `while`, `setstate`, `guardrail`, `end`, `fanout`, `join`, `hitl`* | steer the flow | flow↔flow |
| **Emitter** (extra files) | `gui`, `webserver`, `schedule`, `eval` | link to the entry agent → emit gui.py/server.py/scheduler.py/run_evals.py | emitter→agent |

The **two rules that trip newcomers**:
1. **Exactly one entry agent** — one stage with no incoming *agent/flow* link. Every agent
   must be reachable from it.
2. **A plain agent may have AT MOST ONE outgoing flow link.** To branch you MUST use a
   brancher: a **Router** (LLM picks), a **Condition** (data picks), a **routing HITL**
   (human picks), a **self-routing Planner**, or **Fan-out** (all branches, in parallel).
   A second edge off a plain agent is a generation error.

Every agent needs **one `llm`** linked to it. **Several llms = a failover chain**, tried
in **fallback priority** order (1 = primary). Priority lives on the **link** (double-click
the `llm→agent` edge to set it), *not* the LLM node — so the same LLM can be primary for
one agent and a fallback for another. The number shows as a `#N` badge on the link.

---

## 3. The nodes (key props)

- **`agent`** — `role` (`single`|`planner`|`worker`|`critic`|`supervisor`|`orchestrator`),
  `reads`/`writes` (shared-state fields), **budgets** (`max_iterations`, `max_tool_calls`,
  `max_output_tokens`, `max_wall_clock_s` — **all default 0 = UNLIMITED**; set a positive
  value to cap), plus opt-in capability toggles (`web_search`, `code_exec`, `enable_todos`, …).
- **`llm`** — `provider` (`siliconflow`|`deepseek`|`openai`|`gemini`|`nvidia`|`anthropic`),
  `model`, `api_key` (blank), `base_url`, `temperature`.
- **`tool`** — `files: ["mytools.py"]` (every top-level `def` in the file becomes a tool;
  helpers must be lambdas).
- **`prompt`** — `text` (the agent's system persona). At most one per agent.
- **`rag`** — `docs_dir`, `description`, retrieval knobs → gives a `search_<kb>` tool.
- **`memory`** — `description`, `top_k` → gives persistent `remember`/`recall` (cross-run).
- **`router`** — `role="router"`, `instructions`, `default_route`; branches = its outgoing
  agent names.
- **`condition`** — `branches: [{expr, to}]` — first true `expr` wins; empty `expr` = else.
  `expr` is a safe predicate over shared state.
- **`while`** — `condition` (guard expr) + `body` (loop-body successor name); the body links
  back; the other outgoing edge is the exit.
- **`setstate`** — `assignments: [{field, value}]` (applied through each field's reducer).
- **`guardrail`** — `checks`, `on_trip` (`redact`|`block`).
- **`fanout`** — `max_parallel`; **`join`** — `merge` (`concat`|`first`|`last`|`state_only`|`vote`).
- **`hitl`** — `prompt`, `on_reject`; **route mode** kicks in automatically when it has
  **2+ outgoing edges** (`default_route`, per-decision branches).
- **`end`** — terminal sink; **`schedule`** — `mode` (`interval`|`daily`|`once`) + timing;
  link it to the **entry** agent to drive the whole graph, or link **separate** schedules
  to **different** agents to drive each on its own timer (each is its own concurrent job).
  **`gui`/`webserver`/`eval`** — emitters.

> Every parameter above (and its preferred value per situation) is tabulated in
> **[`ConfigTable.md`](ConfigTable.md)**.

---

## 4. Routing to a pattern (decision guide)

Answer top-down; the first match is your pattern.

- **One task, one agent, maybe some tools?** → **Hello / ReAct** (L0–L1).
- **Fixed sequence of steps?** → **Chain** (L2).
- **Iterate until good enough?** → **Planner–Executor–Critic** revise loop (L5).
- **Pick ONE path at runtime…**
  - …by the *content* (LLM judgement)? → **Router** (L4).
  - …by *data / a computed flag*? → **Condition** on shared state (L3).
  - …by a *human*? → **routing HITL** (L8).
- **Delegate sub-tasks to specialists?**
  - one-at-a-time, reviewed? → **Supervisor** (L6).
  - autonomous, parallel spawns? → **Orchestrator** (L6).
- **Do the SAME thing over many items?** → **Worker Pool** (L7).
- **Run DIFFERENT specialists at once, then combine?** → **Fan-out → Join / Map-reduce** (L7).
- **Answer N ways and take the consensus?** → **Voting / self-consistency** (L7).
- **Approve/gate a step before it proceeds?** → **HITL gate** (L8).
- **Learn across runs / run on a schedule?** → **Memory / Schedule** (L10).
- **Several of the above?** → compose them (graph mode), or **multi-pattern `/mode`** (L12).

MetaAgent ships most of these as **Patterns-menu presets** — inserting one is the fastest
way to see a correct example, then edit it.

---

## 5. The progression: hello → complicated

Each level is a recipe: **nodes** + **edges**. Build up by adding to the previous.

### L0 — Hello MTA (chain mode)
`llm → agent`. One agent, one model. That's a runnable agent.
→ nodes: `agent(entry)`, `llm`; edges: `llm→agent`.

### L1 — Give it powers (resources)
Attach any of: `tool→agent`, `rag→agent`, `memory→agent`, `skill→agent`, `prompt→agent`.
Each is a resource edge; the agent gains that capability. Still chain mode.

### L2 — Chain (pipeline)
`agent_a → agent_b → agent_c` (each with its own `llm`+`prompt`). Output flows down the
line. Optional per-edge **contract** (`edge.props.contract`) documents the handoff fields.

### L3 — Branch on data (Condition + shared state)  →  *graph mode*
Declare `state_schema` (e.g. `{"name":"score","type":"float","reducer":"overwrite","default":0}`).
An agent `writes` `score`; a **`condition`** node routes: `branches:[{"expr":"score>=0.8","to":"ship"},{"expr":"","to":"rework"}]`.
→ `agent → condition → {ship, rework}`. Deterministic, no extra LLM call.

**Custom / nested types (when scalars are too thin):** declare named types in `graph.type_defs`
(canvas: **Graph → Define Types**) — a JSON-Schema record + a default **update policy**. Then a
state field's `type` can be `Name` or `list[Name]`, with policies `merge_shallow`/`merge_deep`
(records), `extend`/`upsert_by_key(merge_key)` (lists). The schema drives the `set_state` tool so
the model emits well-formed nested values. Full detail + the policy table: **[`ConfigTable.md`](ConfigTable.md) §7 / §7a.**

### L4 — Branch on content (Router)  →  *graph mode*
`router` (with its own `llm`) → several agents. The router LLM reads the input and picks
ONE successor by name. Set `default_route` for ties. Forwards the payload unchanged.

### L5 — Plan–Execute–Critic (revise loop)
`planner → executor → critic`, plus a **back-edge** `critic → planner`. The back-edge is
auto-detected as the *revise loop* (drawn red). The critic (role `critic`) triggers it by
starting its answer with **`REVISE:`**; otherwise it's the final answer. Bounded by
`MAX_REVISE_ROUNDS`. (Preset: *Planner–Executor–Critic*.)

### L6 — Delegate
- **Supervisor**: entry `role="supervisor"` → worker leaves. Delegates one instruction at a
  time (NEXT/DONE), reviewing each. Mode = `supervisor`.
- **Orchestrator**: entry `role="orchestrator"` → sub-agent leaves. Owns the built-in
  `spawn_subagent` tool; spawns isolated sub-agents (possibly in parallel). Mode =
  `autonomous`. (Link action tools to the *sub-agents*, not the orchestrator, to control
  when it delegates.)

### L7 — Parallel
- **Fan-out/Join**: `agent → fanout → {b1, b2, b3} → join → next`. All branches run
  CONCURRENTLY; branch shared-state writes merge via reducers at the join; `join.merge`
  combines the outputs. (Concurrent writes to one `overwrite` field are rejected — use
  `append`/`add`/`max`/`min`.)
- **Map-reduce** (preset): coordinator → fan-out → N *distinct* workers → join → reducer.
- **Voting / self-consistency** (preset): framer → fan-out → N *identical* solvers → join →
  judge; or set `join.merge="vote"` for a deterministic majority on a bare label.
- **Worker Pool** (`workerpool` node): ONE agent that splits its input into subtasks and
  runs `max_workers` in parallel — same work over many items (vs fan-out's different work).

### L8 — Human in the loop (HITL)
- **Gate** (1 outgoing): `agent → hitl → agent`. Pauses for approve / edit / reject before
  the next stage. `on_reject` = `stop` | `revise`.
- **Route mode** (2+ outgoing): `agent → hitl → {send, reviser, escalate, end}`. A human
  picks the branch (the human mirror of a Router). Set `default_route` for the unattended /
  timeout branch. (Preset: *Human approval (routing HITL)*.)

### L9 — Loops, gates, early exit
- **While**: `while` node with `condition` guard + `body` successor that links back; the
  other edge is the exit. Bounded by `recursion_limit`.
- **Guardrail**: inline `guardrail` node redacts/blocks content passing through.
- **End**: `end` node finishes the run early (great on a Condition/While branch, or a
  routing-HITL "stop" branch).

### L10 — Persistence & ambient agents
- **Memory** node → cross-run `remember`/`recall` (Reflexion-style learning).
- **Storage** (`graph.storage`) → sessions/checkpoints on disk / sqlite / postgres.
- **Schedule** node → emits `scheduler.py`, an ambient runner. Pick a **strategy**:
  `interval` (every N s, `+ offset_seconds` to stagger), `daily` (`at="09:00"`), or `once`
  (`start_at="2026-07-08 14:30"`). Multiple Schedule nodes = independent concurrent jobs.
  Link to the **entry** agent to run the whole graph, or to a **different** agent to drive
  just that one (each schedule → its own agent + timer). Pairs beautifully with Memory.

### L11 — Frontends & tests (emitters)
Link to the entry agent: **`gui`** → `gui.py` (desktop chat), **`webserver`** → `server.py`
(web UI + multi-user), **`schedule`** → `scheduler.py`, **`eval`** → graded test set.

### L12 — Multi-pattern in one app (`/mode`)
Tag agents with `mode_label` to compile several selectable patterns into one app; the end
user switches with `/mode`. (See the UserGuide's mode-switching section.)

---

## 6. Which runner your topology triggers (mode)

Codegen picks the runner from the shape — you don't set it directly:
- **chain** — a straight pipeline, no branchers/control nodes.
- **graph** — any Router, self-routing planner, or any control node (Condition/While/
  Set-State/Guardrail/End/Fan-out/Join/routing-HITL) is present. BFS walk with runtime routing.
- **supervisor** / **autonomous** — entry agent's role is `supervisor` / `orchestrator`.

---

## 7. Validate before you ship

Run `graph_codegen.analyze(graph)` → `{errors, warnings, mode, entry, ...}`. **Errors block
generation; fix them.** Then `graph_codegen.generate_from_graph(graph, "my_agent")`.
Common errors (and the rule they map to):
- *"needs exactly one entry agent"* → one stage with no incoming link (§2 rule 1).
- *"more than one outgoing link"* → use a brancher (§2 rule 2).
- *"Stage names must be unique"* → rename; names key the runtime tables.
- *"RAG/Memory not linked"*, *"prompt role mismatch"*, *"Schedule needs a time"* → configure it.
- A name with `"""` or a `@MARKER@`-shape → rename (it breaks generated code).

---

## 8. Gotchas learned building MetaAgent

- **Budgets default to 0 = unlimited** — demos run to completion; set caps yourself for prod.
- **`api_key` blank in the graph** — real users set it; the designer's key is Debug-Run only
  (there's a *Copy from coding agent* button). The scheduler's key goes in `config.json`.
- **One `prompt` per agent**; a prompt's `role` must match its agent's (or be `single`).
- **Tool files**: every top-level `def` becomes a tool — helpers must be lambdas.
- **HITL route mode = edge count** (2+ outgoing), not a flag; a 1-out HITL is a spliced gate.
- **Fan-out branches** are linear agent+Set-State chains (v1); no control flow *inside* a branch.
- **Shared-state `str` + `append`** concatenates; `overwrite` clobbers (don't let two
  parallel branches write the same `overwrite` field).
- **Unique names, one entry, at-most-one-outgoing** — the three that cause most rejects.
- Start from a **Patterns-menu preset** whenever one is close; edit rather than build blank.

---

## 9. Writing & sharing tools

A **`tool` node** links a Python file to an agent (`tool→agent`); `node.props.files =
["mytools.py"]`. The file lives in `tools/`.

**Tool file format** (`tools/mytools.py`):
```python
from tool_registry import tool

@tool
def get_weather(city: str) -> str:
    """Return the current weather for a city. (The docstring is what the LLM sees —
    say WHEN to use it and describe each arg.)"""
    return "..."

_fmt = lambda x: str(x)     # helpers MUST be lambdas (see gotcha)
```
Rules:
- **Every top-level `def` becomes a tool** (codegen registers them all). A plain
  `def` helper would be exposed as a broken tool → **make helpers lambdas** (or
  nest them inside a tool).
- The **docstring drives the tool schema** (description + `Args:`), so write it for the LLM.
- Declare risk for HITL: `@tool(risk="high")` (always confirm) / `@tool(risk="safe")`
  (never) — codegen bakes these into `config.json`'s high/safe lists.
- Top-level `import`s in the file flow into the generated `requirements.txt`.

**Sharing tools:** drop the `.py` into `tools/`, link a Tool node to it. When you save a
graph as `.mta`, the tool files it uses are **bundled inside** the `.mta` — so a shared
`.mta` is self-contained (no separate tool files to send). To ship tools with the
compiled designer, put them in `tools/` before building (see §11 / installer.txt).

## 10. Saving, sharing & loading graphs (`.mta`)

- **`.mta` = a ZIP**: `manifest.json` + `graph.json` + `tools/*.py` (the tool files the
  graph references). Self-contained and portable.
- **Save/Load** in the canvas: Graph ▸ Save… / Load… (`Ctrl+S` / `Ctrl+O`). By script:
  `graph_model.save_mta(graph, "MyAgent.mta", tools_dir)` /
  `graph, info = graph_model.load_mta("MyAgent.mta", tools_dir)` — load restores the
  bundled tool files into `tools_dir` and reports `restored / conflicts / missing`
  (an existing tool file that DIFFERS is kept, never overwritten, and flagged).
- **Share a graph:** send the single `.mta` file — the recipient loads it and gets the
  graph *and* its tools. Example graphs live in `graphs/` (they populate the welcome
  launcher; bundle that folder to ship them with the designer exe).
- `.mta` round-trips cleanly: any node kind + its props serialize as plain JSON, so new
  node kinds need no special handling.

## 11. Generate, then compile to `.exe`

**Generate the agent** from a graph: `graph_codegen.generate_from_graph(graph, "MyAgent",
gui=True, code_style="single")` → `generated_agents/MyAgent/` containing `agent.py`
(+ `gui.py` / `server.py` / `scheduler.py` / `run_evals.py` if those emitter nodes are
present), plus `config.json`, `requirements.txt`, `build.bat`, `README.md`. `code_style`:
`"single"` (one portable `agent.py`) or `"package"` (a `runtime/` package + thin
`agent.py`). The output is **zero-LangChain, dependency-light** Python.

**Compile the GENERATED AGENT to an exe:** canvas ▸ **Generate ▸ Compile (PyInstaller)**,
or run the agent's own `build.bat` (after `pip install -r requirements.txt`). Build in a
**clean venv** so the exe stays lean.

**Compile the MetaAgent DESIGNER itself to an exe** (so a designer needs no Python): see
`installer.txt` — PyInstaller recipes (both `--onedir` and `--onefile`) that bundle the
data the app reads at runtime (**`templates/`, `runtime/`, `assets/`, `graphs/`, `tools/`**).
Key point: the `runtime/` and `templates/` folders MUST be `--add-data`'d or the exe
launches but "Generate" fails (codegen reads them at run time). **Do NOT bundle your
working `config.json` — it holds your API key**; the app self-creates a blank-key config
on first run (written NEXT TO the exe) and the designer sets their own key in Settings
(see installer.txt §3a). **Distribution:** `--onedir` produces `dist/MetaAgent/` with an
`_internal/` folder — ship the WHOLE folder (the exe alone can't find `python3XX.dll`);
`--onefile` produces a single self-contained `dist/MetaAgent.exe`. Put your tools in
`tools/` and graphs in `graphs/` before building to ship them inside the exe.

**Lifecycle at a glance:** design graph → add tool files → validate (`analyze`) →
generate → share (`.mta` bundles tools) → compile (agent exe, or the designer exe via
`installer.txt`).
