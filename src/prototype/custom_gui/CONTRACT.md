# Custom GUI ↔ agent contract

A custom `gui.py` loaded into a GUI node may lay out any widgets it likes, but to
actually *drive the agent* it must talk to the generated `agent.py`, imported as:

```python
import agent as core
```

Everything below is provided by every generated agent. The only **required** call
is `core.run(...)`; the rest are optional and feature-gated (present only when the
graph contains the matching node).

## Core (always present)

| Call | Purpose |
|---|---|
| `core.run(task, emit=fn, on_token=fn, images=None) -> str` | Run the agent on `task`. `emit(str)` receives trace/step lines; `on_token(str)` receives streamed answer tokens; `images` is an optional list of image paths (vision). Returns the final result. **Runs synchronously — call it on a worker thread.** |
| `core.request_cancel()` | Ask the in-flight `run()` to stop ASAP (wire to a Stop button). |
| `core.total_usage() -> {input_tokens, output_tokens, cost_usd}` | Session token/cost totals. |
| `core.CONFIG` | The loaded `config.json` dict (e.g. `CONFIG.get("server")`). |
| `core.reload_config()` | Re-read `config.json`. |
| `core.HISTORY`, `core.clear_history()`, `core.save_history()` | Persisted conversation history. |

## Connecting a control to a configurable parameter (read → write)

This is the answer to *"how do menus/buttons change real parameters, e.g. switch
the LLM?"* Every configurable parameter follows the same shape:

- **read** a getter to populate/initialize the control,
- **write** a setter on user action — it mutates `core`'s runtime state and
  persists to a sidecar JSON, so the **next `core.run()`** picks it up.

The GUI never edits config internals; it only calls these functions.

### Worked example — the LLM switch

```python
# READ — build the control from the current state
for agent in core.PIPELINE:                       # agent names in the pipeline
    options = core.get_llm_options(agent)         # ["modelA", "modelB", ...] (fallback chain)
    if len(options) < 2:
        continue                                  # nothing to switch
    current = core.get_llm_choice(agent)          # selected index
    # ...add one checkable menu item per option, check `current`...

# WRITE — on click
def on_pick(agent, idx):
    core.set_llm_choice(agent, idx)               # applies + writes llm_choice.json
```

`set_llm_choice` reorders that agent's fallback chain so the chosen model is
tried **first** on the next run (`cfgs[idx:] + cfgs[:idx]`); the others remain as
fallbacks. See `example_custom_gui.py::_build_model_menu` for the full PySide6
menu, and `demo.py` proves the switch reaches the next `core.run()`.

The same read→write pattern wires every other parameter below (e.g. RAG:
`rag_enabled()` → checkbox, `set_rag_enabled(bool)` on toggle; Skills:
`skills_for(a)` → list, `add_skill/remove_skill(...)` on edit).

## Threading rule (important)

`core.run()` blocks and its `emit` / `on_token` callbacks fire on **your worker
thread**. Never touch Qt widgets from there — marshal to the GUI thread with a
`Signal` (see `example_custom_gui.py`). Updating a widget directly from the worker
thread will crash.

## Human-in-the-loop (when HITL is configured)

| Call | Purpose |
|---|---|
| `core.set_confirm_handler(fn)` | `fn(prompt: str) -> bool` — approve/deny a high-risk tool. Called from the worker thread; block until the user answers. |
| `core.set_review_handler(fn)` | `fn(prompt, content) -> {"decision": "approve"\|"edit"\|"reject", "content": str, "feedback": str}` — checkpoint review. |

## Configurable parameters reference

For each group: the getter(s) to **read** (populate a control) and the setter(s) to
**write** (apply on user action), the file it persists to, and when the API exists.
Re-read the getter after each setter — `EVAL_SETS` / `SKILLS` / `HISTORY` are mutated
**in place** (not copies). Unless noted, a change is picked up by the **next
`core.run()`** with no restart or reload.

### LLM switch — *agent with ≥2 linked LLMs*
- **read:** `core.PIPELINE`, `core.get_llm_options(agent)`, `core.get_llm_choice(agent)`
- **write:** `core.set_llm_choice(agent, idx)` → **`llm_choice.json`**

Next run tries the selected model first; the rest remain as fallbacks. Only build the
menu for `[a for a in PIPELINE if len(get_llm_options(a)) > 1]`.

### RAG knowledge base — *RAG node (`CONFIG.get("rag")`)*
- **read:** `rag_enabled()`, `rag_manual_chunks()`, `rag_docs_files()`, `core.RAG` (docs_dir/chunk_chars/top_k)
- **write:** `set_rag_enabled(bool)` → **`rag_state.json`**; chunks `rag_add_chunk(text)` / `rag_add_file(path)` / `rag_update_chunk(id,text)` / `rag_delete_chunk(id)` / `rag_remove_source(src)` / `rag_clear()` → **`rag_chunks.json`**; `rag_invalidate()` (rebuild the in-memory index)

Enable-flag and chunks are live (re-read on each search). **Gotcha:** `core.RAG`
(docs_dir/chunk_chars/top_k) is a snapshot read once at import — `reload_config()`
does **not** refresh it, so changing those needs a restart. `rag_add_file` returns a
string starting `[ERROR]` on failure.

### Skills — *Skills node (`CONFIG.get("skills")`)*
- **read:** `list_skill_agents()`, `skills_for(agent)`
- **write:** `add_skill(agent,name,text)` / `update_skill(agent,idx,name,text)` / `remove_skill(agent,idx)` → **`skills.json`**

`build_system()` folds skills into the system prompt fresh each stage. (An agent stays
listed even after all its skills are removed.)

### Workspace folders — *always present*
- **read:** `get_workspace()`
- **write:** `set_workspace(list)` / `add_workspace_folder(path)` (returns the new list) → **`workspace.json`**

**Gotcha:** `get_workspace()` filters out folders that no longer exist on disk — a
control populated from it silently drops missing paths. The next run injects the
folder/file listing into the prompt and resolves relative tool-arg paths against it.

### Eval sets/cases — *always present (Evals menu when `hasattr(core,"run_evals")`)*
- **read:** `core.EVAL_SETS`, `eval_targets()`
- **write:** `add_eval_set(name,target)` / `update_eval_set(i,name,target)` / `remove_eval_set(i)` / `add_eval_case(i,case)` / `update_eval_case(i,j,case)` / `remove_eval_case(i,j)` → **`evals.json`**
- **run:** `run_evals(emit=fn) -> [{name,target,passed,total,score}]` (run on a worker thread)

`target=None` = whole pipeline; a name = that agent alone. A case is
`{"id", "input", <one of expected_output | expected_regex | judge>}`.

### Conversation / control / usage / HITL — *always present*
- **read:** `core.HISTORY`, `total_usage()`, `cost_usd(agent)`, `core.CONFIG`, `entry_vision()`, `core.IMAGE_EXTS`
- **write:** `clear_history()` (→ `history.json`), `request_cancel()` (Stop — interrupts the in-flight run), `set_confirm_handler(fn)` / `set_review_handler(fn)` (HITL — effect is immediate, no run boundary)

**Gotcha:** `entry_vision()` is read once at GUI startup — switching the LLM does **not**
live-toggle the attach UI without rebuilding the window.

### config.json-only parameters — *edit the file, then `reload_config()`*
Per-agent `model` / `api_key` / `base_url` / `temperature` / `top_p` /
`request_timeout_s` / `response_format` / `extra`, plus `hitl_confirm`,
`high_risk_tools`, `server.*`, `stream`: there is **no setter**. Edit `config.json`
and call `core.reload_config()` (it rebuilds LLM clients for the next run).
**Topology and budgets are baked at generation and not reloadable;** MCP servers need
`core.mcp_reconnect(emit=fn)` (call `reload_config()` first to pick up edits).

> The standard generated `gui.py` (`codegen_templates.GUI_TEMPLATE`) is the
> reference implementation of all of the above — copy from it when in doubt.

## Required bootstrap (before `import agent`)

A custom `gui.py` is run as `python gui.py`; under a Python started with `-P` / `PYTHONSAFEPATH` the script's own directory is NOT on `sys.path`, so `import agent` fails with **"No module named agent"**. Put this at the TOP of your custom GUI (the built-in gui.py/server.py/scheduler.py already do):

```python
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
```
