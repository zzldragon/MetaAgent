# CLAUDE.md — working agreement for the MetaAgent project

MetaAgent is a **visual multi-agent designer that generates readable, standalone Python**
(PySide6 canvas → `.mta` graph → dependency-free agent code, optionally a PyInstaller exe).
Its defining value: **the generated output has ZERO runtime framework dependency (only
PySide6 + openai) and can run/compile on its own.** Protect that identity in every change.

---

## Coding rules (follow these — they override default behavior)

1. **Keep documentation in sync.** After implementing a feature, update ALL relevant docs —
   both `docs/` (UserGuide, Engineering, Knowledge, Gap Analysis) and
   `.claude/skills/design-mta-graph/` (`SKILL.md`, `ConfigTable.md`). This is **mandatory**
   whenever there's a configuration change or a node is added/removed/renamed.

2. **Always compare with other frameworks first.** Before implementing something, weigh the
   design against **LangGraph, AutoGen/AG2, Dify, OpenAI (Assistants/Responses), CrewAI, and
   deepagents** — borrow the good ideas, and note where we deliberately differ (and why).

3. **Test the new feature AND run regressions.** Every feature gets a focused test
   (`tests/_verify_*.py` script or a `tests/**/test_*.py`), plus regression tests for whatever
   it could affect. Never call something done without green tests.

4. **Refuse bad requests.** If a request would add clutter, dead weight, or the wrong
   abstraction, say so and don't build it. Don't add rubbish to the project.

5. **Ask when unclear.** If you don't understand the proposal/requirement, ask — do not force
   an implementation you're guessing at.

6. **Guard the Canvas UX.** Before changing the Canvas window, ask: *is this user-friendly?*
   If not, push back and explain why. Keep the whole project user-friendly.

7. **Don't guess.** Verify against the actual code before acting.

---

## Additional rules (learned from our work — keep applying them)

8. **Byte-identical when unused.** New options must default to the old behavior and be emitted
   into generated code/config ONLY when set, so existing graphs regenerate byte-for-byte
   unchanged. (Guarded by `tests/test_generate_matrix.py` / `test_codegen_format.py` /
   `test_marker_guard.py`.)

9. **Verify, don't trust memory.** Recalled memory/notes may be stale — confirm a file,
   function, flag, or model still exists before recommending or using it (ties to rule 7).

10. **Never bundle secrets; preserve keys on regen.** API keys stay OUT of `.mta` graphs and
    the exe (`api_key` blank; the end user fills `config.json`). When regenerating a user's
    agent under `generated_agents/`, read the existing key from its `config.json` and splice it
    back into the new one.

11. **Adding/removing a node kind touches the fail-fast registries.** `graph_model.py`
    (`NODE_KINDS`, `KIND_META`, `default_props`, grouping tuples/`ALLOWED_EDGES`),
    `canvas_qt/dialogs.py` (`_DIALOGS`), `canvas_qt/designer.py` (`KIND_SHAPE`), and
    `graph_codegen.py` (`analyze` + emission). Import-time checks enforce most of these — run
    the canvas + codegen tests after.

12. **Editing a bundled tool/GUI = re-sync the `.mta` from the REAL `TOOLS_DIR`.** `save_mta`
    re-bundles tool files from the `tools_dir` you pass it; use `app_config.TOOLS_DIR`, not the
    throwaway dir `load_mta` extracted into, or you'll silently ship a stale copy. For a custom
    GUI, write `desktop_gui.props["custom_gui"]` then `save_mta(g, path, TOOLS_DIR)`, then
    regenerate the affected agents.

13. **Parse Python with AST, not regex.** e.g. import extraction (`codegen.tool_requirements`)
    must use `ast`, or docstring/comment prose leaks false positives.

14. **i18n user-facing strings.** Wrap new canvas strings in `t()` and add the Simplified-Chinese
    entry in `canvas_qt/i18n.py` (`_ZH`); Chinese is a first-class UI language.

15. **After a non-obvious change, update the persistent memory** under the `.claude/projects`
    memory dir (one fact per file + a `MEMORY.md` pointer) so future sessions have context.

16. **Every new node or pattern ships with an example `.mta`.** Once a new node kind or a new
    pattern is added, you MUST author an example `.mta` file under `graphs/` that exercises it,
    validate it with `graph_codegen.analyze`, and confirm it generates. It's the executable
    proof the addition works end-to-end and the reference users copy from (ties to rules 1, 11).

---

## Environment & how to run things

- **OS/shell:** Windows 11; Git Bash (POSIX) available alongside PowerShell. The GBK console
  can't print emoji — write test output to files or set `PYTHONIOENCODING=utf-8`.
- **Qt tests headless:** `QT_QPA_PLATFORM=offscreen`.
- **Run a verify script:** `python tests/_verify_<name>.py`.
- **Fast regression:** `python -m pytest tests/test_generate_matrix.py tests/test_marker_guard.py tests/test_codegen_format.py -q`.
- **Canvas suite:** `python -m pytest tests/canvas/ -q`.
- **LLM backend:** provider-neutral OpenAI-compatible; keys live in `config.json` (`llms`),
  never in the graph. Default model = `deepseek-ai/DeepSeek-V4-Flash` via SiliconFlow.

## Codebase map (where things live)

- `graph_model.py` — the `Graph`/`Node`/`Edge` data model, node registry (`KIND_META`),
  `default_props`, `save_mta`/`load_mta`, `expand_subgraphs`.
- `graph_codegen.py` — `analyze(graph)` (validation) + `generate_from_graph()` (emission).
- `graph_codegen_templates.py` — the runtime template (react loop, run_graph/run_pipeline,
  budgets, checkpoint, guardrails, tools) inlined into generated agents.
- `codegen.py` — tool inlining, requirements, build.bat.
- `canvas_qt/` — the Qt designer (`designer.py`), node dialogs (`dialogs.py`), i18n, tool
  generator / designer-agent window (`tool_generator.py`), replay.
- `tools/` — tool libraries; `prototype/` — custom-GUI sources; `graphs/*.mta` — saved graphs;
  `generated_agents/` — generated output; `tests/` — verify scripts + pytest suites.
