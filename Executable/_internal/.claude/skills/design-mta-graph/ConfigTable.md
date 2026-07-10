# MetaAgent — Configuration Reference (`ConfigTable.md`)

Exhaustive table of **every node kind, every configurable parameter, its type / default /
allowed values, when to use it, and the preferred value in specific situations** — plus
link contracts, shared state, budgets, graph-level settings, and storage.

Companion to [`SKILL.md`](SKILL.md): the SKILL teaches *which* node/pattern to pick; this
file is the *exact knobs*. It is the intended reference for an automated **design agent**.

**Source of truth:** `graph_model.default_props(kind)` (parameters) and
`graph_model.ALLOWED_EDGES` (links). To dump a live default:
`python -c "import graph_model,json; print(json.dumps(graph_model.default_props('rag'),indent=2))"`.

**Conventions used below**
- **Default** is the value codegen sees when the field is left untouched. **Blank / `""` /
  `0` / `false` / `[]` almost always means "unchanged / provider-default / unlimited"** and
  keeps generated output byte-identical — set a value only to opt in.
- Types: `str`, `int`, `float`, `bool`, `list`, `dict`, `enum(...)`.
- `api_key` fields stay **blank** in a shared graph; the end user supplies the key.

---

## 0. Node families & edge rules (recap)

| Family | Kinds | Legal edge |
|---|---|---|
| **Stage** (executes) | `agent`, `workerpool`, `router` | stage→stage (flow) |
| **Resource** (feeds a stage) | `llm`, `tool`, `skill`, `prompt`, `rag`, `memory`, `mcp` | resource→agent |
| **Control** (flow, no LLM) | `condition`, `while`, `setstate`, `guardrail`, `end`, `fanout`, `join`, `hitl` | flow↔flow |
| **Emitter** (extra files) | `gui`, `webserver`, `schedule`, `eval` | emitter→agent |

Hard rules: **exactly one entry agent**; **a plain agent has at most one outgoing flow
link** (branch only via router / condition / routing-hitl / self-routing planner / fanout);
**every agent needs one `llm`** (several = a failover chain, priority on the *link*);
`prompt` and `gui` are **singleton inputs** (at most one per agent).

---

## 1. Cross-cutting parameter groups

These appear on several stage nodes.

### 1a. Budgets (on `agent`, `workerpool`, `router`) — all default `0` = **UNLIMITED**
| Param | Type | Default | Set it when… | Preferred |
|---|---|---|---|---|
| `max_iterations` | int | 0 | cap the ReAct loop (runaway tool loops) | 0 for demos; 6–12 for a bounded tool agent |
| `max_tool_calls` | int | 0 | cap total tool calls in a run | 0, or ~20 for cost control |
| `max_output_tokens` | int | 0 | cap tokens generated per call | 0; set to model max only if truncation needed |
| `max_wall_clock_s` | int | 0 | hard time limit per run | 0; 60–300 for a user-facing UI |

> Leave all at **0** so a demo runs smoothly; add caps only for untrusted/production loops.

### 1b. Per-agent HITL knobs (active only when the global HITL switch is on)
| Param | Type | Default | Values / notes |
|---|---|---|---|
| `hitl_review` | bool | false | pause to review this stage's **input** before it runs |
| `hitl_triggers` | list | `["high_risk_tool"]` | `high_risk_tool` (before a write/send/delete tool), `low_confidence` |
| `hitl_confidence_threshold` | float | 0.6 | pause when self-rated confidence < this (with `low_confidence`) |
| `hitl_on_reject` | enum | `stop` | `stop` \| `revise` (re-run upstream with feedback) |
| `hitl_decisions` | list | `["approve","edit","reject"]` | which actions the reviewer may take |
| `hitl_timeout` | int | 0 | auto-decide after N s (0 = wait forever) |
| `hitl_on_timeout` | enum | `approve` | `approve` \| `reject` on timeout |

### 1c. Shared-state access (on `agent`, `workerpool`)
| Param | Type | Default | Meaning |
|---|---|---|---|
| `reads` | list[str] | `[]` | state fields injected into the prompt (`[]` = read all) |
| `writes` | list[str] | `[]` | state fields this stage may write (`[]` = free-form fenced block) |
| `require_writes` | bool | false | re-prompt if it didn't record every `writes` field (chain/graph mode) |

---

## 2. Stage nodes

### 2a. `agent` — the core unit (an LLM ReAct loop)
| Param | Type | Default | Allowed / notes | When to use / preferred |
|---|---|---|---|---|
| `role` | enum | `single` | `single`, `planner`, `worker`, `critic`, `supervisor`, `orchestrator` | `single` for one-shot; `planner`+`worker`+`critic` for PEC; `supervisor`/`orchestrator` for delegation |
| `route_self` | bool | false | planner-only; planner picks one successor itself (no Router LLM) | on when a planner has 2+ agent successors and you want to save a routing call |
| `quick_response` | bool | false | self-routing planner: allow "no branch fits" → end with planner's own answer | on for chat that should answer trivial input directly |
| `reads`/`writes`/`require_writes` | — | — | see §1c | declare when using shared state |
| `guardrails` | dict | `{}` | per-agent guardrail config (tool-result/arg/output hooks, PII, image allow-list) | set for untrusted tools/output |
| `enable_todos` | bool | false | gives built-in `write_todos` (checklist in `todos` state) | on for long multi-step tasks |
| `mode_label` | str | `""` | tag a sub-pipeline entry (`react`/`pec`/`supervisor`) → multi-pattern app (`/mode`) | set on each pattern's entry agent to make a switchable multi-pattern app |
| `code_exec` | bool | false | built-in `run_python` (isolated subprocess; HITL-gated; needs a workspace) | on for data/analysis agents; **isolation ≠ security sandbox** |
| `code_exec_backend` | enum | `subprocess` | `subprocess`, `docker`, `auto` | `docker` for true containment (needs Docker) |
| `code_exec_timeout` | int | 30 | seconds per run | raise for heavy compute |
| `code_exec_memory_mb` | int | 512 | docker backend memory cap | — |
| `code_exec_image` | str | `python:3.11-slim` | docker image | add libs via a custom image |
| `web_search` | bool | false | built-in web search (network egress; HITL-gated). Engine defaults are in `config.json` → `web_search` (keyless **DuckDuckGo** default; or `tavily`/`serpapi`/`brave`/`bing`/`searxng`/`baidu` with a key — `baidu` via SerpApi's Baidu engine; `serpapi` sub-engine via `serpapi_engine`; `engines:[…]` = failover chain) — keys stay OUT of the graph. **Proxy:** each engine tries a **DIRECT** connection first; only if that fails does it retry via `config.json` → `proxy` (blank = env `HTTP(S)_PROXY`; no proxy set = direct only). A successful direct search never retries via the proxy. | on for research agents; leave off offline |
| `web_search_engine` / `web_search_api_key` / `web_search_base_url` / `web_search_proxy` | str | `""` | **per-agent** web-search override (Agent dialog → Extra Settings → Web search). Blank = inherit the global `config.json` `web_search` block; only non-blank fields override it (so a proxy-only override keeps the global engine). Emitted to `config.json` → `web_search_by_agent[<agent>]` and merged over the global block at runtime. Keep `api_key` blank in a shared `.mta` (secret) and fill it in `config.json` on the target machine. | when different agents need different engines/keys/proxies |
| `offload_results` | bool | false | spill very large tool results to a workspace file + pointer | on for agents with big tool outputs |
| `adaptive_retrieval` | bool | false | Adaptive-RAG: decide whether to retrieve at all + pick source | on only when the agent has RAG/web tools |
| `groundedness_check` | bool | false | Self-RAG: grade answer grounding, regenerate if weak | on for RAG QA where hallucination matters |
| `max_regen` | int | 1 | regenerations allowed by groundedness_check | 1–2 |
| `max_rpm` | int | 0 | requests/min rate limit (0 = unlimited) | set to your provider's RPM limit |
| `stage_retries` | int | 0 | re-run this stage on a transient error | 1–2 for flaky endpoints |
| `max_budget_usd` | float | 0 | abort when est. cost hits cap (needs LLM prices) | set for cost-critical prod |
| `final_schema` | str(JSON) | `""` | force the FINAL answer to a JSON Schema | set when a caller needs structured output |
| `final_schema_retries` | int | 2 | re-asks on schema mismatch | 2 |
| budgets, HITL knobs | — | — | §1a, §1b | |

### 2b. `workerpool` — N identical parallel workers
| Param | Type | Default | Notes | Preferred |
|---|---|---|---|---|
| `role` | str | `worker` | shares prompt/tools/LLM across workers | `worker` |
| `max_workers` | int | 4 | parallel workers over the subtasks handed in | 3–8; ≥1 required |
| `reads`/`writes`/`require_writes`, `guardrails`, `enable_todos`, `max_rpm`, `stage_retries`, budgets, HITL | — | — | as `agent` | mind `max_rpm` × workers vs provider limits |

### 2c. `router` — an LLM picks one outgoing agent per input
| Param | Type | Default | Notes | Preferred |
|---|---|---|---|---|
| `instructions` | str | `""` | how to choose among branches | describe each branch clearly |
| `default_route` | str | `""` | tie-break/ambiguous branch (successor name) | set to a safe fallback branch |
| `routing_provider` / `routing_model` / `routing_base_url` / `routing_api_key` | str | `""` | a **cheaper** LLM just for routing | point at a small/cheap model to save cost |
| budgets | — | — | §1a | |

> Branches = the router's outgoing agent links (by name). Use a router when the *content*
> decides the path and an LLM judgment is needed; use `condition` when a **data rule** decides.

---

## 3. Resource nodes (feed an agent)

### 3a. `llm` — a model config (several per agent = failover chain)
| Param | Type | Default | Allowed / notes | Preferred value by situation |
|---|---|---|---|---|
| `provider` | enum | `siliconflow` | `siliconflow`, `deepseek`, `openai`, `gemini`, `anthropic`, `nvidia` | match your `base_url`/key |
| `model` | str | `deepseek-ai/DeepSeek-V4-Flash` | provider model id | latest capable chat model; on SiliconFlow use the **`-Flash`** variant |
| `api_key` | str | `""` | **leave blank in shared graphs** | end user fills it |
| `base_url` | str | provider default | must NOT include `/chat/completions` | — |
| `temperature` | str→float | `""` | blank = provider default | `0` for deterministic/extraction/routing; `0.7` for creative; **blank/omit for Anthropic Opus** |
| `top_p` | str→float | `""` | blank = default | leave blank unless you know you need it; **omit for Anthropic Opus** |
| `response_format` | enum | `text` | `text`, `json_object` (OpenAI-family), `json_schema` | `json_schema` when the caller parses output |
| `response_schema` | str(JSON) | `""` | required for `json_schema` | — |
| `parallel_tools` | bool | false | run 2+ tool calls concurrently (parallel-safe only) | on for read-heavy tool agents |
| `request_timeout_s` | str→int | 120 | per-call hard cap; blank = SDK default (~600) | 60–120 for UIs |
| `proxy` | str | `""` | e.g. `http://1.1.1.1:8080`; blank = env/system proxy | set behind a corporate proxy |
| `vision` | bool | false | model accepts image input → chat lets users attach images | on for the agent that receives user input, if the model supports vision |
| `context_capacity` | int | 0 | the model's context window; >0 → the **entry** agent compacts to stay under it | set to the real window (e.g. 128000) for long chats |
| `compact_threshold` (Agent node → Extra Settings) | int % | 85 | when the **entry** agent's estimated context reaches this % of its usable window (from the LLM's `context_capacity`), older turns are folded into a summary. 1–100; only the entry agent compacts; default 85 is byte-identical (not emitted). | lower (e.g. 70) to compact earlier/safer; higher (e.g. 95) to keep more raw history before compacting |
| `stop` | str | `""` | stop sequence(s), one per line | — |
| `seed` | str→int | `""` | deterministic seed | set with `temperature=0` for reproducibility |
| `presence_penalty` / `frequency_penalty` | str→float | `""` | provider-specific | leave blank |
| `top_k` | str→int | `""` | providers that support it | leave blank |
| `reasoning_effort` | enum | `""` | `""`, `minimal`, `low`, `medium`, `high` | `high` for hard reasoning models; blank otherwise |
| `max_retries` | str→int | `""` | blank = framework default (2); `0` = no retry | 2 |
| `tool_choice` | enum | `auto` | `auto`, `any`, `none`, `specific` | `auto`; `none` to forbid tools; `specific` to force one |
| `tool_choice_name` | str | `""` | function name for `tool_choice=specific` | — |
| `price_in_per_1m` / `price_out_per_1m` | str→float | `""` | $ per 1M tokens → enables `max_budget_usd` | set to enable cost caps |
| `extra` | str(JSON) | `""` | raw API params (overrides the above) | escape hatch only |

> **Fallback priority is NOT on this node — it's on the `llm→agent` LINK** (`edge.props["priority"]`,
> 1 = primary). Double-click the link to set it. The same LLM can be `#1` for one agent and
> `#2` for another. See §6b.

### 3b. `tool` — a Tools node (aggregates tool files)
| Param | Type | Default | Notes |
|---|---|---|---|
| `files` | list[str] | `[]` | tool-library `.py` files under `tools/`; every top-level `def` becomes a tool (helpers must be lambdas) |
| `tool_props` | dict | `{}` | per-function Extra Settings: `{func: {return_direct, error_mode, error_retries, risk, description}}` |

Per-function `tool_props` values: `return_direct` (bool, end the run with this tool's result),
`error_mode` (`retry`\|`ignore`\|`fail`), `error_retries` (int), `risk` (`low`\|`high` → HITL-gate
high), `description` (override the docstring). Preferred: set `risk="high"` on write/send/delete tools.

### 3c. `skill` — named guidance snippets appended to the prompt
| Param | Type | Default | Notes |
|---|---|---|---|
| `skills` | list | `[]` | each `{name, description, text, disable_model_invocation?}`; progressive disclosure — prompt shows name+description, body loads on `/name` or `load_skill` |

### 3d. `prompt` — the agent's system persona (≤1 per agent)
| Param | Type | Default | Notes |
|---|---|---|---|
| `role` | enum | `single` | `single`\|`planner`\|`worker`\|`critic` — chooses a template under `templates/` |
| `text` | str | `""` | the persona; blank = the role's template |

### 3e. `rag` — a retrieval knowledge base → a `search_<kb>` tool
| Param | Type | Default | Allowed / notes | Preferred value by situation |
|---|---|---|---|---|
| `docs_dir` | str | `""` | folder of documents to index | required |
| `description` | str | `""` | routing hint → the tool's description + a prompt-tail line | describe what's in the KB (drives multi-RAG routing) |
| `chunk_chars` | int | 800 | chunk size | 500–800 prose; 1200–1600 code/tables |
| `top_k` | int | 4 | chunks returned | 3–5; raise for broad questions |
| `chunk_strategy` | enum | `fixed` | `fixed`, `recursive`, `markdown`, `code` | `markdown` for docs, `code` for source, `recursive` for mixed |
| `chunk_overlap` | int | 0 | 0 → size//8 | leave 0 (auto) |
| `retrieval_granularity` | enum | `chunk` | `chunk`, `parent_child` (index small, return parent) | `parent_child` when small hits need fuller context |
| `parent_chunk_chars` | int | 2400 | parent block size (parent_child) | — |
| `retrieval_algorithm` | enum | `bm25` | `bm25`, `dense`, `hybrid` (RRF) | `bm25` offline/keyword; `hybrid` for best recall (needs embeddings) |
| `recall_n` | int | 0 | candidates before rerank/MMR (0 = top_k) | 20–50 when reranking |
| `mmr` / `mmr_lambda` | bool/float | false / 0.5 | diversity re-ranking | on to reduce near-duplicate chunks |
| `rerank_mode` / `rerank_model` | enum/str | `none` / `""` | `none`, `llm`, `cross_encoder` | `cross_encoder` for precision; `llm` if no local model |
| `grade_docs` | bool | false | LLM relevance gate drops irrelevant chunks | on for noisy corpora (1 extra call/search) |
| `corrective` / `corrective_max_rewrites` | bool/int | false / 2 | CRAG: rewrite query + retry when nothing found | on when queries often miss |
| `query_transform` | enum | `none` | `none`, `hyde`, `multi_query`, `rewrite` | `multi_query` for recall; `hyde` for sparse corpora |
| `multi_query_n` | int | 3 | variants for `multi_query` | 3–5 |
| `score_threshold` | float | 0.0 | drop chunks below score (dense/cross-enc) | 0.2–0.4 to cut weak hits |
| `metadata_filter` | str | `""` | source glob(s), e.g. `*.md` | restrict to a doc subset |
| `embed_provider` | enum | `local` | `local` (FREE, no key), `openai`, … | `local` (BAAI/bge-small-zh) unless you need a hosted embedder |
| `embed_model` | str | `BAAI/bge-small-zh-v1.5` | ~90 MB local model | keep for zh/mixed; swap for en-only corpora |
| `embed_base_url`/`embed_api_key` | str | `""` | openai only | — |
| `normalize` | bool | true | normalize embeddings | keep true |
| `vector_db` | enum | `memory` | `memory`, `chroma`, `faiss`, `qdrant` | `memory` for small KBs; `qdrant`/`chroma` for large/persistent |
| `qdrant_url`/`qdrant_api_key` | str | `""` | blank URL = embedded on-disk (`./rag_qdrant`) | set URL for a remote Qdrant |
| `evict_used` | bool | false | drop earlier search hits when a newer search runs | on for long tool-heavy turns |

> Multiple RAG nodes → multiple `search_*` tools; write a clear `description` on each so the
> agent routes to the right one.

### 3f. `memory` — persistent cross-run store (Reflexion) → `remember`/`recall`
| Param | Type | Default | Notes | Preferred |
|---|---|---|---|---|
| `description` | str | `""` | routing hint prepended to the tool docs | describe what to remember |
| `top_k` | int | 5 | memories `recall` returns by default | 3–8 |

> Pairs with a **Schedule** node (learn across scheduled runs) and with a blank-`session_id`
> agent (one rolling conversation).

### 3g. `mcp` — connect to an external MCP tool server
| Param | Type | Default | Allowed / notes | Preferred |
|---|---|---|---|---|
| `transport` | enum | `streamable_http` | `stdio`, `streamable_http`, `sse` | `streamable_http` for a hosted server; `stdio` for a local command |
| `command` / `args` | str | `""` | stdio: process to spawn | `stdio` only |
| `url` | str | `http://127.0.0.1:8000/mcp` | http/sse endpoint | — |
| `verify_tls` | bool | true | https cert check | keep true in prod |
| `allow_tools` / `deny_tools` | str | `""` | comma-sep server tool names | allow-list untrusted servers |
| `connect_timeout` / `call_timeout` | int | 0 | seconds (0 = 30 / 60 default) | raise for slow servers |
| `env` / `headers` | str | `""` | `KEY=val` (stdio) / `Header: val` (http) | auth headers here |

---

## 4. Control nodes (flow, no LLM)

### 4a. `condition` — deterministic If/Else on shared state
| Param | Type | Default | Notes |
|---|---|---|---|
| `branches` | list | `[]` | ordered `[{to, expr}]`; first true `expr` wins; empty `expr` = else/fallback. `to` = successor stage name; `expr` = safe predicate over state (e.g. `score >= 0.8`) |

### 4b. `while` — loop while a state predicate holds
| Param | Type | Default | Notes | Preferred |
|---|---|---|---|---|
| `condition` | str | `""` | guard expr over shared state | keep it eventually-false |
| `body` | str | `""` | loop-body successor name (must link back to this node) | — |
| `max_iterations` | int | 0 | per-loop cap (0 = only graph `recursion_limit`) | set a cap for safety |

### 4c. `setstate` — deterministic shared-state write
| Param | Type | Default | Notes |
|---|---|---|---|
| `assignments` | list | `[]` | `[{field, value}]`; each value applied through that field's reducer |

### 4d. `guardrail` — inline content gate (no LLM)
| Param | Type | Default | Allowed / notes | Preferred |
|---|---|---|---|---|
| `checks` | dict | `{secret:true, pii:false, injection:false}` | which scans to run | enable `pii` for user-facing output |
| `on_trip` | enum | `redact` | `redact` (scrub in place) or `block` (stop). **injection always blocks** | `redact` for output, `block` for input |
| `patterns` | list | `[]` | custom regexes to redact/block | add org-specific secrets |
| `keywords` | list | `[]` | literal terms (case-insensitive) | banned words |
| `max_length` | int | 0 | truncate over N chars (0 = no cap) | cap runaway content |

### 4e. `end` — terminal sink
No parameters. Reaching it finishes the run and returns the carried output. Great on a
Condition/While branch or a routing-HITL "stop" branch for early exit.

### 4f. `fanout` — parallel branch fan-out
| Param | Type | Default | Notes |
|---|---|---|---|
| `max_parallel` | int | 0 | concurrency cap (0 = unbounded) |

### 4g. `join` — barrier that reconverges a fan-out
| Param | Type | Default | Allowed | Preferred |
|---|---|---|---|---|
| `merge` | enum | `concat` | `concat`, `first`, `last`, `state_only`, `vote` | `concat` to combine; `vote` for majority (voting pattern); `state_only` when branches wrote to shared state |

### 4h. `hitl` — human checkpoint (two shapes by outgoing-edge count)
| Param | Type | Default | Notes |
|---|---|---|---|
| `prompt` | str | "Review the output before continuing." | the question shown |
| `on_reject` | enum | `stop` | `stop` ends the run; `revise` re-runs the upstream agent with feedback |
| `decisions` | list | `["approve","edit","reject"]` | which the reviewer may take |
| `timeout` | int | 0 | auto-decide after N s (0 = wait) |
| `on_timeout` | enum | `approve` | GATE mode timeout action (`approve`/`reject`) |
| `default_route` | str | `""` | **ROUTE mode** branch on timeout / tie-break |

> **1 outgoing link → GATE** (approve/edit/reject before the next stage). **2+ outgoing links
> → ROUTE** (human picks which successor runs; `default_route` is the timeout branch). The
> HITL↔agent link renders as flow (blue), not a resource "uses" edge.

---

## 5. Emitter nodes (link to the entry agent → emit an extra file)

### 5a. `gui` — desktop PySide6 chat window → `gui.py`
| Param | Type | Default | Notes |
|---|---|---|---|
| `custom_gui` | str | `""` | optional user-authored `gui.py` SOURCE emitted instead of the built-in window (drives the agent via `import agent as core` / `core.run(...)`); blank = standard chat window |

### 5b. `webserver` — WebSocket server (web UI + multi-user) → `server.py`
| Param | Type | Default | Notes | Preferred |
|---|---|---|---|---|
| `host` | str | `127.0.0.1` | bind address | `0.0.0.0` to expose |
| `port` | int | 8765 | — | — |
| `auth_token` | str | `""` | shared-secret gate | set when exposed |
| `auto_allow_tools` | bool | false | headless: auto-approve tools (no HITL prompt) | on for unattended servers |
| `autostart` | bool | false | **desktop `gui.py` only** — open the WebSocket port on launch instead of waiting for the Server menu. Headless `server.py` **always** listens on start (`python server.py`), so this flag does not affect it. | on if the GUI app should also serve on launch |
| `tls_cert` / `tls_key` | str | `""` | `wss://` (both required together) | set for public deploys |
| `allowed_origins` | list | `[]` | CORS allow-list ([] = any) | restrict in prod |
| `max_connections` | int | 0 | 0 = unlimited | cap for shared hosts |

**When does the agent start listening?** Headless `server.py` binds `host:port` **immediately on start** — no menu, no GUI needed (deploy just `agent.py` + `server.py` + `config.json` + `requirements.txt`, no PySide6). The desktop `gui.py` starts the embedded server only when you toggle **Server → Enable WebSocket Server**, unless `autostart` is set. For unattended headless use also set `auto_allow_tools: true` (else HITL blocks waiting for a client) and `host: 0.0.0.0` + an `auth_token`.

### 5c. `schedule` — ambient runner → `scheduler.py`
| Param | Type | Default | Notes | Preferred value by situation |
|---|---|---|---|---|
| `mode` | enum | `interval` | `interval`, `daily`, `once` (mutually exclusive; other fields greyed) | pick the one strategy you need |
| `every_seconds` | int | 3600 | interval mode period | set to your cadence |
| `offset_seconds` | int | 0 | initial delay before the first tick | stagger multiple jobs so they don't all fire at once |
| `run_at_start` | bool | true | fire immediately (after offset) vs after a full interval | off if the first run should wait |
| `at` | str | `""` | daily mode: `HH:MM` / `HH:MM:SS` (local) | e.g. `09:00` |
| `start_at` | str | `""` | once mode: `YYYY-MM-DD HH:MM[:SS]` (local) | a future timestamp |
| `initial_task` | str | `""` | the prompt run each tick (no user input) | describe the recurring job |
| `session_id` | str | `""` | blank = isolate this job by its node name | set to share memory across jobs |
| `max_runs` | int | 0 | 0 = forever | small number for testing |

> **Link the schedule to the ENTRY agent** to drive the whole graph (B), or **link separate
> schedules to different agents** to drive each independently (A). Each schedule = its own
> concurrent thread + session. The run starts at the linked agent.

### 5d. `eval` — a graded test set → `run_evals.py`
| Param | Type | Default | Notes |
|---|---|---|---|
| `cases` | list | `[]` | each `{input, ...graders}`. Graders (15-family registry): `expected_output`/`contains`, `expected_regex`, `numeric`, `is_json`+`json_has_keys`, `not_contains`, `judge` (LLM criterion), etc. `not` flag inverts. Link to **one agent** → tests that agent; **standalone** (no link) → tests the whole graph |

---

## 6. Links (edge configuration)

Double-click a link to configure it. `edge.props` holds the config.

### 6a. `agent → agent` — data-handoff **contract**
| Prop | Type | Default | Notes |
|---|---|---|---|
| `contract` | list | (none) | `[{name, type, description}]` — fields the upstream PRODUCES = the downstream EXPECTS; injected into **both** system prompts. `type` ∈ native types **OR a custom type `Name` / `list[Name]`** from `graph.type_defs` (§7a) — a custom-typed field also injects its nested JSON shape so the producer emits well-formed structured output |
| `contract_enforce` | bool | false | validate the upstream's output as JSON against the contract; re-run on mismatch |
| `contract_max_retries` | int | 2 | re-runs before the run stops (enforce mode) |

Use a contract to make a hand-off explicit; turn on `contract_enforce` only when the
downstream truly needs structured input.

### 6b. `llm → agent` — **fallback priority**
| Prop | Type | Default | Notes |
|---|---|---|---|
| `priority` | int | auto | 1 = primary, 2 = first fallback, … Per-**link**, so a shared LLM is numbered independently per agent. The designer keeps these contiguous (1..N) and shows a `#N` badge on the link (only when an agent has 2+ LLMs). Runtime tries them in order, failing over on error |

### 6c. `condition → X` / `while → X` — branch labels
- **Condition**: the branch's `expr` + target live in the condition node's `branches` (keyed by
  the destination stage **name**); edit via the link.
- **While**: the link is either the **loop body** (`while.body == dst.name`) or the **exit**
  (the other outgoing edge).

Resource links (`tool/skill/rag/...→agent`) carry no contract.

---

## 7. Graph-level: shared state (`state_schema`)

A list of field dicts `{name, type, reducer, default, description, merge_key?}` on
`graph.state_schema`.

| Field attr | Values | Notes |
|---|---|---|
| `type` | `str`, `int`, `float`, `bool`, `list`, `dict`, **a custom type `Name`, or `list[Name]`** | custom types are declared in `graph.type_defs` (§7a) |
| `reducer` | update policy — see the table below | how repeated/concurrent writes combine |
| `default` | matches type (custom → JSON) | initial value; blank custom → `{}`/`[]` |
| `merge_key` | str | id field for `upsert_by_key` (else inherited from the type) |

**Update (reducer) policies** — which apply depends on the type's kind:

| Policy | Applies to | Effect | Deterministic on fan-in? |
|---|---|---|---|
| `overwrite` | any (default) | last write wins | ❌ (order-dependent) |
| `append` | list / list[Type] / str | add one element (str: `\n\n`-join) | ✅ |
| `extend` | list / list[Type] | concatenate lists | ✅ |
| `add` / `max` / `min` | int / float | arithmetic | ✅ |
| `merge_shallow` | dict / record | `{**old, **new}` (new keys win) | ❌ (per-key last-wins) |
| `merge_deep` | dict / record / nested | recursive dict merge | ✅ |
| `upsert_by_key` | list[record] | insert-or-**update** by `merge_key` (a matching record is deep-merged, so a partial update keeps other fields) | ✅ |

`type` kinds: **scalar** (str/int/float/bool) → overwrite/add/max/min/append(str); **list**
(native list, `list[Name]`) → overwrite/append/extend/upsert_by_key; **record** (native dict
or a custom object type) → overwrite/merge_shallow/merge_deep.

**Reserved fields (framework-maintained, never declare/write):** `user_input` (the original
request, read-only), `tool_calls` (every tool name appended), `agents` (every stage visited),
`todos` (when `enable_todos`). Agents opt IN to read them via `reads`.

Preferred: single-writer → `overwrite`; fan-in/parallel writers → an accumulate policy
(`append`/`extend`/`add`/`merge_deep`/`upsert_by_key`) so writes don't clobber. **Analyze
errors** on a fan-out where ≥2 parallel branches write the same `overwrite`/`merge_shallow`
field (nondeterministic); it **warns** for the sequential multi-writer case.

### 7a. Custom / nested types (`graph.type_defs`)

For structured state (the native scalars are too thin for complex tasks), declare named types:
`graph.type_defs = {Name: {schema, merge, merge_key?, description?}}`. Author them in the canvas
via **Graph → Define Types** (a visual record editor *and* a raw JSON-Schema tab).

| Type-def attr | Type | Notes |
|---|---|---|
| `schema` | dict | a JSON Schema (object or array). Nest another type with `{"$type": "Name"}` |
| `merge` | policy | the type's DEFAULT update policy (a field may override it); `custom` = use `merge_src` |
| `merge_key` | str | id field for `upsert_by_key` |
| `merge_src` | str (Python) | escape hatch: a top-level `def merge(old, new): …` used when `merge`/reducer is `custom` (for logic the built-in policies can't express) |
| `description` | str | shown to the designer |

**Custom merge (`merge: "custom"` + `merge_src`)** — when none of the built-in policies fit,
write a Python `def merge(old, new)` that returns the merged value. It's compiled into the
generated agent (`_CUSTOM_MERGES[Name]`) and called by `_apply_state` for any field of that type
with reducer `custom`. Analyze validates it defines a top-level `merge`; keep it pure and total.

Why it matters: the schema is compiled into the `set_state` tool's parameter schema, so the LLM
emits **well-formed nested values** (a bare `dict` gives it no shape). Use a custom `Record` type
+ `list[Record]` with `upsert_by_key` for an agent that maintains a growing table of items.
Backward-compatible: a graph with no `type_defs` generates byte-identically.

---

## 8. Graph-level: other settings

| Setting | Type | Default | Notes | Preferred |
|---|---|---|---|---|
| `recursion_limit` | int | 0 | max stage hops in graph mode (0 = auto, scales with graph size) | leave 0 unless loops need more room |
| `storage.backend` | enum | `disk` | `disk`, `sqlite`, `postgres` — where sessions (memory) + checkpoints live | `disk` for single-user; `sqlite`/`postgres` for multi-user/servers |
| `storage.sqlite_path` | str | `memory.db` | sqlite backend file | — |
| `storage.dsn` | str | `""` | postgres DSN | set for postgres |

Checkpoint/resume (by `thread_id`) is only meaningful when there is user-declared state.

---

## 9. Providers & preferred models

`PROVIDERS = siliconflow, deepseek, openai, gemini, anthropic, nvidia`.

- Default stack: **DeepSeek-V4-Flash via SiliconFlow** (provider-neutral; any OpenAI-compatible
  endpoint works). On SiliconFlow always use the **`-Flash`** model id.
- `base_url` must **not** include `/chat/completions` (the SDK appends it).
- For the newest, most capable Claude models when targeting Anthropic, prefer the latest
  Opus/Sonnet; **omit `temperature`/`top_p`** for Opus 4.x (they're rejected).
- Split cheap vs deep: use a small model on `router` (`routing_model`) and quick agents;
  a stronger model on the reasoning/critic agents.

---

## 10. "Which value?" quick answers

- **Demo that must just run** → all budgets `0`, `temperature` blank, `hitl` off, `storage=disk`.
- **Deterministic extraction / routing** → `temperature=0` (+ `seed` for reproducibility),
  `response_format=json_schema` or agent `final_schema`.
- **Long conversation** → set the LLM's `context_capacity` to the real window; `storage=sqlite`.
- **Untrusted tools / user-facing output** → agent `guardrails`, a `guardrail` node
  (`pii`+`block` on input), tool `risk="high"`, HITL on write tools.
- **Cost control** → LLM `price_*`, agent `max_budget_usd`, `max_tool_calls`, a cheap `routing_model`.
- **Big / persistent KB** → `retrieval_algorithm=hybrid`, `vector_db=qdrant`, `rerank_mode=cross_encoder`.
- **Reliable fallback** → 2–3 `llm`s per agent on different providers, priority set on each link.
- **Recurring / learning agent** → `schedule` (+ `offset` to stagger) + `memory` node.
