"""Build a Codex / Cursor-style coding agent as a MetaAgent graph.

Design (see .claude/skills/design-mta-graph): a coding agent is best modelled as
ONE powerful ReAct agent (L1) with a rich filesystem+shell toolset and built-in
TODO planning — the same shape as Cursor's / Codex's agent loop — rather than a
multi-stage pipeline. So the graph is:

    prompt ─┐
    llm   ──┤
    tools ──┼──► agent (entry, role=single, enable_todos)
    gui   ──┘        ▲
    eval ───────────┘   (graded smoke test)

Run:
    python build_coding_agent.py

Outputs:
    graphs/CodingAgent.mta                 self-contained bundle (graph + tools)
    generated_agents/CodingAgent/          standalone agent.py + gui.py + config
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)                     # run from anywhere / safe-path envs

import graph_codegen  # noqa: E402
import graph_model    # noqa: E402
from graph_model import Graph  # noqa: E402

TOOLS_DIR = os.path.join(HERE, "tools")
GRAPHS_DIR = os.path.join(HERE, "graphs")

# ─────────────────────────────────────────────────────────────────────────────
# The system prompt — the heart of a coding agent. Written for a general LLM:
# it establishes identity, the agentic loop, planning, safe editing discipline,
# verification, and communication style. Kept tool-name-accurate to the kit in
# tools/coding_agent_tools.py.
# ─────────────────────────────────────────────────────────────────────────────
PROMPT = r"""You are a coding agent — an autonomous, expert software engineer that works directly in the user's codebase, in the spirit of tools like Cursor and Codex. You complete real engineering tasks end to end: understand the request, explore the code, make the changes, and verify them.

## Operating loop
Work in a tight loop until the task is genuinely done:
1. Understand the goal. If the request is ambiguous in a way that blocks progress, ask ONE focused question; otherwise make the most reasonable assumption, state it briefly, and proceed.
2. Gather context BEFORE acting. Use `list_dir`, `glob_search`, and `grep_search` to find the relevant files, then `read_file` to study them. Never edit a file you have not read.
3. Plan. For any task with more than ~2 steps, call `write_todos` to lay out the plan, then keep it updated — mark each item in_progress before you start it and completed the moment it is done. Keep exactly one item in_progress.
4. Make focused changes with `edit_file` (surgical, exact-string edits) or `write_file` (new files). Prefer the smallest edit that correctly solves the problem.
5. Verify. Use `run_shell` to run the tests, linter, type checker, formatter, or build that prove your change works. Fix what you break. Do not claim success you have not checked.
6. Report concisely when the whole task is complete.

## Gathering context
- Start broad (`list_dir` at the root, `glob_search` for file types) then narrow (`grep_search` for a symbol, then `read_file`).
- Match the project's existing style, libraries, and conventions. Read a neighbouring file to learn the patterns before adding code. Do not introduce a new dependency or framework when the project already has one that fits.

## Editing code
- Always `read_file` the exact region before you `edit_file` it. `old_string` must reproduce the file byte-for-byte (indentation and all) and be unique — include a few surrounding lines for uniqueness, or pass `replace_all=True` deliberately.
- Keep edits minimal and localized; do not reformat unrelated code or churn imports.
- Write code that runs. Keep it complete — no `TODO`, no stubbed-out bodies, no placeholder values — unless the user explicitly asked for a scaffold.
- Do NOT add comments that merely restate the code. Comment only non-obvious intent or constraints.
- After a set of related edits, re-read or grep to confirm the change landed and nothing dangling references the old code.

## Verifying
- Prefer running the project's own checks: its test command (e.g. `pytest -q`, `npm test`), linter, or a quick script. If you added a function, exercise it. If you fixed a bug, reproduce it first, then show it is gone.
- Treat a non-zero exit code or an error in the output as a failure to fix, not to ignore.

## Safety
- `write_file`, `edit_file`, `move_path`, `delete_path`, and `run_shell` change the user's system. Be deliberate: double-check the path before deleting or overwriting, and never run a destructive command (mass delete, force push, `rm -rf` on a broad path, resetting git history) unless the user explicitly asked for it.
- Prefer editing an existing file over creating a new one. Do not create documentation or README files unless asked.
- Never invent file contents — read first. If a tool returns `[ERROR] ...`, read the message, adapt, and try a corrected call instead of repeating the same one.

## Communication
- Be concise and direct. Skip filler and flattery. Use Markdown; wrap file, directory, function, and command names in backticks.
- Briefly narrate what you are about to do before a batch of tool calls, and give a short summary of what changed when you finish — reference the files you touched.
- When you present code that already exists in the repo, cite it rather than pasting large blocks; keep any new code blocks short and focused.
- Stop when the task is complete and verified. Do not ask if you should keep going when there is an obvious next step — just do it."""


def build_graph() -> Graph:
    """Assemble the coding-agent graph from typed default props (so it stays in
    sync with graph_model) then override only what this design needs."""
    def props(kind: str, **over) -> dict:
        p = graph_model.default_props(kind)
        p.update(over)
        return p

    # Entry agent: a single autonomous ReAct loop with built-in todo planning.
    # Budgets left at 0 (unlimited) so it behaves like a real agent; offload_results
    # spills very large tool outputs to a file so file dumps can't flood context.
    agent = props(
        "agent", role="single", enable_todos=True, offload_results=True,
    )
    # A capable model, tuned for coding: deterministic (temperature 0), high
    # reasoning effort, a large context window so the entry agent auto-compacts
    # long sessions, and a generous per-call timeout for big edits.
    llm = props(
        "llm", model="deepseek-ai/DeepSeek-V4-Flash", temperature="0",
        reasoning_effort="high", context_capacity=131072, request_timeout_s=600,
    )
    prompt = props("prompt", role="single", text=PROMPT)
    tools = props("tool", files=["coding_agent_tools.py"])
    gui = props("gui")
    eval_node = props("eval", cases=[
        {"input": "Reply with exactly the single word: pong",
         "expected_output": "pong"},
        {"input": "In one short sentence, describe your very first step when "
                   "given a new coding task in an unfamiliar repository.",
         "judge": "The answer should say it explores / reads / inspects the "
                  "codebase (gathers context) before making changes."},
    ])

    nodes = [
        {"id": "agent_1",  "kind": "agent",  "name": "coder",      "x": 360, "y": 40,  "props": agent},
        {"id": "llm_1",    "kind": "llm",    "name": "model",      "x": 0,   "y": 0,   "props": llm},
        {"id": "prompt_1", "kind": "prompt", "name": "persona",    "x": 0,   "y": 150, "props": prompt},
        {"id": "tool_1",   "kind": "tool",   "name": "code_tools", "x": 0,   "y": 300, "props": tools},
        {"id": "gui_1",    "kind": "gui",    "name": "desktop",    "x": 720, "y": 40,  "props": gui},
        {"id": "eval_1",   "kind": "eval",   "name": "smoke",      "x": 720, "y": 200, "props": eval_node},
    ]
    edges = [
        {"src": "llm_1",    "dst": "agent_1", "props": {}},
        {"src": "prompt_1", "dst": "agent_1", "props": {}},
        {"src": "tool_1",   "dst": "agent_1", "props": {}},
        {"src": "gui_1",    "dst": "agent_1", "props": {}},
        {"src": "eval_1",   "dst": "agent_1", "props": {}},
    ]
    return Graph.from_dict({
        "nodes": nodes, "edges": edges,
        "state_schema": [], "recursion_limit": 0, "storage": {},
    })


def main() -> None:
    graph = build_graph()

    info = graph_codegen.analyze(graph)
    print("mode   :", info.get("mode"))
    print("entry  :", info.get("entry"))
    if info.get("warnings"):
        print("warnings:")
        for w in info["warnings"]:
            print("  -", w)
    if info.get("errors"):
        print("ERRORS (blocking):")
        for e in info["errors"]:
            print("  -", e)
        raise SystemExit(1)

    os.makedirs(GRAPHS_DIR, exist_ok=True)
    mta_path = os.path.join(GRAPHS_DIR, "CodingAgent.mta")
    graph_model.save_mta(graph, mta_path, TOOLS_DIR)
    print("saved  :", mta_path)

    out = graph_codegen.generate_from_graph(graph, "CodingAgent")
    print("agent  :", out)


if __name__ == "__main__":
    main()
