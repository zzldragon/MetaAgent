# MetaAgent — Framework Gap Analysis, Tiered Plan & Roadmap

> Scope: a code-verified assessment of MetaAgent vs. mainstream agent frameworks
> (LangGraph, AutoGen/AG2, CrewAI, Dify, n8n/Flowise), an honest current-state
> inventory, and a **tiered implementation plan** that says, for every gap, whether
> we plan to build it, when, how (concrete touch-points), and — where relevant — why
> we deliberately **won't**.
>
> Last refreshed 2026-07-11 from a full source investigation (node system, execution/
> deployment/providers, observability/security/memory). See §9 "What changed since the
> previous assessment" for corrections to earlier claims.

---

## 1. Guiding principle (why some gaps are "not recommended")

MetaAgent's defining value is **"visual design → readable, standalone Python with ZERO
runtime framework dependency (only PySide6 + openai), packagable to an .exe."** Every
decision below is weighed against that identity:

- **Build it** when it can be done with the stdlib / the existing runtime and keeps the
  generated output dependency-free (HTTP node, subgraph, for-each, template, REST server…).
- **Offer it as an opt-in adapter** when it needs a heavy library (vector memory, OTel
  export) — never forced into the default output.
- **Don't build it natively** when it would drag large vendor SDKs or ops platforms into
  generated agents, or when an existing feature already covers the realistic need
  (Bedrock/Vertex SDKs, gVisor/Firecracker, ragas, Presidio-bundled, superstep scheduler).

The tiers below apply this consistently.

---

## 2. Where MetaAgent is already strong (verified)

| Dimension | Current state |
|---|---|
| Output artifact | Generates readable standalone Python (single-file or package), PyInstaller `.exe`; **zero framework dependency at runtime** (PySide6 + openai only). |
| Multi-agent patterns | chain / router / supervisor / orchestrator / worker-pool (incl. dependency-aware **DAG pool**) / **fan-out→join (concurrent + barrier)** / voting. |
| Control flow | condition / while / setstate / guardrail / end / **fanout / join** / HITL (gate + route). |
| Parallelism | Concurrent fan-out branches on a thread pool with isolated state forks + reducer-merged join; worker-pool + DAG pool; parallel tool calls. |
| RAG | BM25 / dense / hybrid-RRF, parent-child, cross-encoder rerank, CRAG / Self-RAG / Adaptive; chroma / faiss / qdrant; **fully offline, key-free local embed/rerank**. |
| Durable execution (runtime) | Checkpoint/resume per stage keyed by `thread_id`, topology-fingerprinted; disk/sqlite/postgres backends. (UI to enable it is the gap — see Tier 1.) |
| Multi-user server | Per-session `asyncio.Lock`, concurrent isolated per-session runs, **owner-scoped HITL**, optional `max_connections`, TLS/`wss`/CORS. |
| Built-in assistants | Tool Generator + graph Designer agent (NL → tools/graphs). |
| Security mitigations | Prompt hardening, guardrail node + hooks (secrets/PII/injection/destructive-arg), HITL for high-risk tools, **optional Docker sandbox** for code exec. |
| Observability | Structured per-run JSONL traces + canvas replay (play/step/scrub). |

---

## 3. Positioning vs. mainstream frameworks (corrected)

| Capability | MetaAgent | LangGraph | AutoGen/AG2 | CrewAI | Dify | n8n/Flowise |
|---|---|---|---|---|---|---|
| Visual drag-and-drop | ✅ strong | ⚠️ Studio | ❌ | ⚠️ | ✅ | ✅ |
| Generates standalone code | ✅ unique | ❌ | ❌ | ❌ | ❌ | ❌ |
| Multi-agent orchestration | ✅ | ✅ | ✅ | ✅ | ⚠️ | ⚠️ |
| In-graph parallelism | ✅ fan-out/join + pool | ✅✅ superstep | ⚠️ | ⚠️ | ✅ | ⚠️ |
| **Sub-flow / graph reuse** | ✅ subgraph | ✅ subgraph | ⚠️ | ⚠️ | ✅ | ✅ |
| **Generic integration nodes (HTTP/DB)** | ❌ (write a tool) | ⚠️ | ⚠️ | ⚠️ | ✅ | ✅✅ |
| **For-each / Map over list** | ✅ foreach (parallel) | ✅ | ⚠️ | ⚠️ | ✅ | ✅ |
| Durable / resumable | ⚠️ runtime yes, **UI toggle missing** | ✅✅ + time-travel | ⚠️ | ⚠️ | ✅ | ⚠️ |
| Multi-user server sessions | ✅ concurrent + isolated | n/a | n/a | n/a | ✅ | ⚠️ |
| Server auth | ⚠️ single shared token | n/a | n/a | n/a | ✅ RBAC | ⚠️ |
| OpenAI-compatible REST out | ❌ (WS only) | ⚠️ | ⚠️ | ⚠️ | ✅ | ⚠️ |
| Observability (OTel/managed) | ⚠️ local JSONL only | ✅ LangSmith | ⚠️ | ⚠️ | ✅ | ⚠️ |
| Code-exec sandbox | ⚠️ subprocess default, **optional Docker** | external | external (e2b) | ⚠️ | ✅ | ⚠️ |
| Triggers | ⚠️ schedule only | ⚠️ | ❌ | ❌ | ✅ | ✅✅ |

---

## 4. Tier 1 — Build now (high value, strong fit, no new runtime deps)

### 4.1 Subgraph / Call-Graph node  · ✅ **DONE**
- Shipped as the `subgraph` node: a child graph embedded **inline** (`props.graph_json`) and
  **flattened** at generate time by `graph_model.expand_subgraphs` (child names namespaced
  `sub/name`, End→pass-through, shared-state merged into the parent). Cycle/self-include rejected;
  byte-identical when unused. Verified by `tests/_verify_subgraph.py`.

### 4.2 For-Each / Map-over-list node  · ✅ **DONE**
- Shipped as the `foreach` control node (single-node, While-like shape): props `over` (a
  shared-state list field), `body` (loop-body successor that links back), `item_var`,
  `result_field`, `merge`, `max_parallel`. `run_graph`'s `foreach` arm runs the body **once per
  item in parallel**, reusing the fan-out engine (`ThreadPoolExecutor` + `_run_branch` with
  `stop_at`=the For-Each node + `_apply_state` capture/replay + `_join_merge`); the item is passed
  BOTH as the body's input and (when `item_var` is set) into that state field; `result_field`
  collects outputs. Emitted as the `FOREACH` table (`_foreach_region` bounds the body); analyze
  warns on an `overwrite` body write (parallel clobber). Byte-identical when unused. Verified by
  `tests/_verify_foreach.py`.

### 4.3 HTTP / API Request node  · effort **S–M** · risk **S**
- **Gap:** no generic REST node and no built-in HTTP tool; external calls require a hand-written tool or MCP.
- **Plan:** deterministic stage kind `http`. Props: `method`, `url` (with `{state}` interpolation), `headers`, `body`, `auth` (bearer/basic/none), `out_field`, `timeout`, `proxy`. Emit **stdlib `urllib`** code (no new deps) that calls and writes the parsed JSON/text into shared state. Reuse the picturebook proxy/opener pattern.
- **Touch-points:** `graph_model.py`; `dialogs.py`; `graph_codegen.py` analyze + emission; tiny runtime `_http_call` fragment. **Best ROI: highest value-per-effort of any node.**

### 4.4 Durable-execution UI toggle + generic Resume  · effort **S + M** · risk **S**
- **Gap:** runtime checkpoint/resume is complete, but **no UI enables it** — `StorageDialog` only sets backend, never `storage["checkpoint"]`; `resume()` is only wired in the bespoke picturebook custom GUI. A designer user can't turn it on.
- **Plan:** (a) add an **"Enable crash-recovery (checkpoint/resume)"** checkbox to `StorageDialog` that writes `graph.storage["checkpoint"]` (codegen already maps it). (b) In the default generated GUI/CLI, add a stable `thread_id` + a **Resume** action calling `resume(thread_id)`, and a `--resume` CLI flag on `agent.py`.
- **Touch-points:** `canvas_qt/dialogs.py` `StorageDialog` (checkbox + `apply()`); `graph_codegen.py` GUI template + CLI entry. **Note:** no "time-travel edit-state-and-rerun" here — that's Tier 3.

### 4.5 OpenAI-compatible REST endpoint + Dockerfile  · effort **M** · risk **M**
- **Gap:** the generated server speaks only a custom WebSocket protocol; no `/v1/chat/completions`, no OpenAPI; deployment is Windows PyInstaller only (no Docker/k8s).
- **Plan:** (a) extend `SERVER_TEMPLATE.process_request` with `POST /v1/chat/completions` (+ `GET /v1/models`) that maps an OpenAI request → `core.run(session_id=…)` and returns OpenAI-shaped JSON, with SSE streaming reusing the existing per-session concurrency + token streaming. (b) Emit a **Dockerfile** (python-slim, `pip install -r requirements.txt`, run server) as a build artifact.
- **Touch-points:** `codegen_templates.py` `SERVER_TEMPLATE`; `codegen.py` (new Dockerfile emitter next to `build_bat`); WebServer node dialog (toggle REST/OpenAI-compat, reuse the single `auth_token` as the bearer key). Dramatically widens adoption + deployment surface.

---

## 5. Tier 2 — Next (worthwhile, moderate effort, mostly stdlib/opt-in)

### 5.1 Deterministic Code / Transform node · effort **S–M** · risk **M**
Pure-Python pipeline step (no LLM, no HITL) for data shaping — lighter than an agent + `run_python` (which is LLM-authored and HITL-gated). Props: `code` body, `in_fields`, `out_field`. It's the **author's own** code (same trust level as tool files), emitted inline. Touch-points mirror the `setstate` control node.

### 5.2 Single LLM-call node · effort **M** · risk **S**
`llmstep` stage = one prompt→completion, no ReAct loop / no tools. Reuse the `llm()` runner capped at one turn. Cheaper and more predictable than `agent(max_iterations=1)` for pure pipelines.

### 5.3 Media-generation node (Image / TTS / STT) · effort **M** · risk **M**
First-class `media` node. Props: `mode` (image/tts/stt), `model`, `endpoint`, `in_field`, `out_field`. Generalize the proven picturebook `gen_image` (stdlib `urllib` → OpenAI-images/SiliconFlow shapes, base64-or-URL response). Removes the "image gen must hide in a tool" limitation.

### 5.4 Template / Answer-formatting node · effort **S** · risk **S**
`template` node: fill a `{var}` template from shared state into `out_field` — deterministic report/reply assembly (End just returns the carried value today).

### 5.5 Webhook trigger node · effort **M** · risk **M**
Event-driven sibling to `schedule`: emit a small stdlib HTTP listener that calls `agent.run(payload)`. Leverage the existing client-gateway webhook code (`client/channels/*`) for signature-verify patterns.

### 5.6 Knowledge-write (RAG ingest) node/tool · effort **M** · risk **M**
RAG is read-only today. Add an `ingest`/`remember_doc` capability that appends chunks and updates the BM25 (and optional dense) index at runtime — closes "incremental indexing."

### 5.7 Vector / semantic memory (opt-in) · effort **M** · risk **S**
Memory is BM25+JSON. Add dense-embedding recall by **reusing the existing local RAG embedding + vector-store stack**, opt-in so the default stays dependency-free.

### 5.8 Cross-session run-history UI + eval regression trends · effort **M** · risk **S**
A trace/eval browser over `traces/*.jsonl` and stored eval results (persist `run_evals` outputs over time + diff). No external service — aggregates what's already produced locally.

### 5.9 Provider presets via base_url (Azure/Ollama/groq/together/Mistral) · effort **S** · risk **S**
Add OpenAI-compatible presets to `PROVIDER_DEFAULTS` for the ones reachable by URL. Azure needs a small deployment-URL/auth variant. (Bedrock/Vertex/Cohere are §7 "not recommended".)

---

## 6. Tier 3 — Optional / later (niche or large)

- **Intent-classifier node** — `router` already classifies-and-routes; a classify-into-label-without-routing node is marginal.
- **File-watch trigger** — narrower than webhook/schedule.
- **Time-travel debugger** (replay to a step, edit state, re-run) — large; read-only replay already covers most debugging.
- **Prometheus metrics endpoint** — only meaningful once a managed-deploy story exists.
- **Trajectory / tool-path eval** — current eval grades final answers only.
- **Thumbs-up/down feedback loop** (feedback → dataset → eval) — depends on a managed surface.
- **Server RBAC / OAuth / per-user identity** — sessions are concurrent+isolated but auth is a single shared token; real identity is a bigger project.
- **Prompt registry / graph versioning & diff / per-user quotas** — governance layer; defer until multi-user demand is real.
- **Provider Batch API + explicit prompt-cache breakpoints** — runtime already lays prompts out cache-friendly; explicit `cache_control` is a small later add.

---

## 7. Not recommended (conflict with identity, or already covered)

| Item | Why not |
|---|---|
| **Native AWS Bedrock / Google Vertex / Cohere providers** | Require non-OpenAI SDKs + auth → heavy deps in generated output. Prefer OpenAI-compatible `base_url` presets; leave these to an optional adapter only. |
| **ragas / embedding-heavy eval metrics in core** | Adds ML deps to a deliberately stdlib-only eval layer. Optional adapter at most. |
| **Bundled Presidio / Llama Guard / OpenAI Moderation / NeMo Guardrails** | Large deps; the regex + PII (Luhn) + injection tripwires + optional-LLM classifier already cover the common cases. Ship as optional plug-in hooks, not defaults. |
| **gVisor / Firecracker / e2b sandbox** | Ops-heavy. The **optional Docker backend already exists** (`--network=none`, read-only rootfs, mem/pid caps) and covers the realistic containment need — just expose/document it in the UI. |
| **General superstep (Pregel/BSP) scheduler** | High complexity for the single-cursor codegen model; `fanout`/`join` (concurrent + barrier), worker-pool, DAG-pool, and parallel tools already deliver practical parallelism. |
| **mem0 / Zep / Letta entity-graph memory** | Big external memory services; contradicts local-first. Opt-in vector memory (§5.7) is the pragmatic middle. |

---

## 8. Recommended build order

1. ~~**Subgraph node**~~ ✅ done — composability tier-up.
2. ~~**For-Each node**~~ ✅ done — the batch/per-item (map) pattern.
3. **HTTP node** (Tier 1) — biggest remaining value-per-effort; unblocks non-coders.
4. **Durable-execution UI toggle + generic Resume** (Tier 1) — surface a killer capability that already exists in the runtime.
5. **OpenAI-compatible REST endpoint + Dockerfile** (Tier 1) — adoption + deployment.
5. **Deterministic Code node + Template node + Single-LLM-call node** (Tier 2) — round out the pipeline primitives.
6. **Media node + Knowledge-write + opt-in vector memory** (Tier 2) — multimodal & memory.
7. Tier 3 as demand appears.

---

## 9. What changed since the previous assessment (corrections)

The prior draft was stale on several points; verified current state:

- **Code-exec sandbox:** was "❌ none." Now there is an **optional Docker backend** (`_run_docker`: `--network=none`, `--read-only` rootfs + tmpfs, `--memory`/`--cpus`/`--pids-limit`, workspace-only mount); the default subprocess backend also scrubs API keys/env, forces a workspace cwd, and uses a Windows Job Object for memory/process caps. Still "not a hard security boundary," but no longer nothing.
- **Parallelism:** was "no true parallel fan-in." There **is** concurrent fan-out→join (thread pool + isolated state forks + reducer-merged barrier), plus worker-pool, DAG-pool, and parallel tool calls. The real gap is only the *general superstep scheduler* (Tier-3/not-recommended).
- **Multi-user server:** was "single-user only." Sessions are **concurrent and isolated** with owner-scoped HITL; the remaining gap is **auth/identity** (single shared token, no RBAC/OAuth).
- **Checkpoint:** runtime resume is complete and reachable via `graph.storage["checkpoint"]`; the gap is specifically the **missing UI toggle + generic Resume UX** (Tier 1.4).
- **Eval:** lightweight **dataset management already exists** (`evals.json`, GUI-editable, seeded from Eval nodes/`evals/*.jsonl`); the gaps are trends/ragas/trajectory/feedback.
- **Node count:** **24 kinds** (see appendix) — was 22; `subgraph` (§4.1) and `foreach` (§4.2)
  landed since the previous assessment.

---

## Appendix A — Node kinds (24, verified)

`agent`, `llm`, `tool`, `skill`, `prompt`, `rag`, `memory`, `webserver`, `mcp`,
`workerpool`, `router`, `hitl`, `eval`, `gui`, `schedule`, `condition`, `while`, `foreach`,
`setstate`, `guardrail`, `end`, `fanout`, `join`, `subgraph`.
(`graph_model.py` `NODE_KINDS` / `KIND_META`; groupings: `AGENT_KINDS`, `CONTROL_KINDS`,
`_RESOURCES`, `SINGLETON_INPUTS`.)

## Appendix B — How to add a node kind (touch-points)

Fail-fast import checks force the first two; the rest are needed for it to work:

1. **`graph_model.py`** — `NODE_KINDS` (+`KIND_META` label/color; import raises otherwise); `default_props(kind)` branch (trailing `raise ValueError(kind)` catches misses); grouping tuples (`AGENT_KINDS`/`CONTROL_KINDS`/`_RESOURCES`/`SINGLETON_INPUTS`) and `ALLOWED_EDGES` (governs permitted links); `add_edge`/`flow_successors` if it's a stage/edge type.
2. **`canvas_qt/dialogs.py`** — a `QDialog` config class + registration in `_DIALOGS` (import check requires one per kind); `BUILTIN_TOOLS`/`_builtin_applies` if it hosts built-in tools.
3. **`canvas_qt/designer.py`** — colors/labels auto-derive from `KIND_META`; optionally add a `KIND_SHAPE` silhouette (defaults to rect); palette/menu iterate automatically.
4. **`graph_codegen.py`** — `analyze(graph)` validation block; per-kind codegen emission (control nodes emit routing tables like `CONDITIONS`/`SETSTATE`; resources fold into the agent spec; splice-type flow nodes follow the `_splice_hitl` pattern).
5. **Runtime** — a fragment in `runtime_source.py` / `runtime/` + `graph_codegen_templates.py` only if new runtime behavior is required.
</content>
