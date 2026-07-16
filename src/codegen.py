"""Generate a standalone, runnable agent from designer settings.

Output layout (generated_agents/<name>/):
    agent.py          self-contained ReAct agent — selected tools inlined,
                      budgets enforced, no LangChain dependency
    config.json       api key / model / base_url
    requirements.txt  openai or anthropic, nothing else
    build.bat         one-click PyInstaller packaging
    README.md         how to run

Tools written in tool-registry style (`@tool` from `tool_registry`, or the legacy
`langchain_core`) are inlined verbatim; a tiny `tool` decorator shim registers
them into the agent's own TOOLS dict, so the generated agent has zero LangChain
dependency.
"""

from __future__ import annotations

import json
import os
import re

from app_config import TOOLS_DIR
from codegen_templates import (  # re-exported (mechanical split)
    CHECKPOINT_CODE,
    EVAL_CODE,
    GUARDRAILS_CODE,
    EVAL_RUNNER_TEMPLATE,
    GUI_TEMPLATE,
    HISTORY_CODE,
    HITL_CODE,
    IMAGE_CODE,
    POOL_CODE,
    RAG_CODE,
    MEMORY_CODE,
    SERVER_TEMPLATE,
    SCHEDULER_TEMPLATE,
    SKILLS_CODE,
    STORAGE_CODE,
    TRACE_CODE,
    WORKSPACE_CODE)

# Tool source decorates functions with `@tool` imported from either the local
# `tool_registry` (current style) or `langchain_core` (legacy style). Both import
# lines are stripped when inlining a tool, because the generated runtime defines
# its own identical `tool` stand-in — so generated agents stay dependency-free.
TOOL_IMPORT_STRIP_RE = re.compile(
    r"^(?:from\s+(?:langchain\S*|tool_registry)\s+import\s+.*"
    r"|import\s+(?:langchain\S*|tool_registry)\b.*)$",
    re.MULTILINE)

# ── tool dependency detection (keeps generated requirements.txt honest) ─────
# import name → pip package name, where they differ
PIP_NAMES = {
    "PIL": "Pillow", "bs4": "beautifulsoup4", "yaml": "PyYAML",
    "sklearn": "scikit-learn", "cv2": "opencv-python", "docx": "python-docx",
    "pptx": "python-pptx", "fitz": "PyMuPDF", "dotenv": "python-dotenv",
    "dateutil": "python-dateutil",
}
# provided by the runtime itself / stripped at generation. langchain* stays as a
# safety net: legacy tool source may `import langchain_core` (see
# TOOL_IMPORT_STRIP_RE above), and this keeps such an import out of requirements.txt.
_RUNTIME_MODULES = {"openai", "anthropic", "langchain", "langchain_core",
                    "tool_registry"}

_TOP_IMPORT_RE = re.compile(r"^\s*(?:import|from)\s+([A-Za-z_]\w*)",
                            re.MULTILINE)


def tool_requirements(tool_source: str) -> list[str]:
    """Third-party pip packages imported by inlined tool code.

    Uses AST so only REAL import statements count — a naive line regex also matched
    docstring/comment prose that merely begins with "from"/"import" (e.g. a wrapped
    sentence "...resolve\\nfrom the module globals; guarded so a standalone\\nimport
    still works"), which leaked bogus 'the'/'still' requirements. Falls back to the
    regex only if the source can't be parsed."""
    import ast as _ast
    import sys as _sys
    mods = set()
    try:
        for node in _ast.walk(_ast.parse(tool_source)):
            if isinstance(node, _ast.Import):
                mods.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, _ast.ImportFrom):
                if node.level == 0 and node.module:      # skip relative imports
                    mods.add(node.module.split(".")[0])
    except SyntaxError:
        mods.update(_TOP_IMPORT_RE.findall(tool_source))  # best-effort fallback
    pips = set()
    for mod in mods:
        if mod in _sys.stdlib_module_names or mod in _RUNTIME_MODULES:
            continue
        pips.add(PIP_NAMES.get(mod, mod))
    return sorted(pips)


def build_bat(name: str, gui: bool) -> str:
    """Build inside a CLEAN venv so only the agent's real dependencies get
    bundled. Building with a 'fat' Python (pandas+scipy+torch installed)
    makes PyInstaller's import crawl cascade and produce 200MB+ exes."""
    lines = [
        "@echo off",
        "REM Clean-venv build: only requirements.txt gets bundled.",
        "REM (falls back to virtualenv for Pythons without the venv module)",
        "if not exist .buildenv\\Scripts\\python.exe python -m venv .buildenv 2>nul",
        "if not exist .buildenv\\Scripts\\python.exe python -m pip install virtualenv",
        "if not exist .buildenv\\Scripts\\python.exe python -m virtualenv .buildenv",
        ".buildenv\\Scripts\\python -m pip install --upgrade pip",
        ".buildenv\\Scripts\\python -m pip install -r requirements.txt pyinstaller",
        "REM --onedir (a folder, not one giant exe): faster cold start,",
        "REM and config/state files sit visibly next to the exe.",
        f".buildenv\\Scripts\\python -m PyInstaller --onedir --noconfirm "
        f"--name {name} agent.py",
        f"copy /Y config.json dist\\{name}\\ >nul",
    ]
    if gui:
        lines += [
            f".buildenv\\Scripts\\python -m PyInstaller --onedir --windowed "
            f"--noconfirm --name {name}_gui gui.py",
            f"copy /Y config.json dist\\{name}_gui\\ >nul",
        ]
    lines += [
        "echo.",
        f"echo Done. Run dist\\{name}\\{name}.exe"
        + (f" or dist\\{name}_gui\\{name}_gui.exe" if gui else "")
        + " (config.json already copied next to it).",
        "pause",
    ]
    return "\n".join(lines) + "\n"


# ── codegen marker guard ─────────────────────────────────────────────────────
# Generated files are assembled by str.replace("@MARKER@", value). A renamed or
# typo'd marker leaves an @MARKER@ behind and emits a syntactically-broken file
# that is discovered only when the generated app runs. These helpers turn that
# silent, late failure into an immediate codegen-time error naming the offender.
_MARKER_RE = re.compile(r"@[A-Z0-9_]+@")


def template_markers(template: str) -> set:
    """Every @MARKER@ substitution point a template declares (all must be
    consumed when that template is the whole emitted file, e.g. agent.py)."""
    return set(_MARKER_RE.findall(template))


def assert_substituted(src: str, expected, where: str) -> None:
    """Fail fast if any marker we INTENDED to substitute survived in `src`.

    `expected` is the set of markers that should have been replaced. Passing the
    full marker set (template_markers(...)) enforces "every declared marker is
    consumed"; passing a narrow set (e.g. {"@AGENT_NAME@"}) checks only those —
    so marker-shaped literals a template emits on purpose never false-positive."""
    leftover = sorted(m for m in expected if m in src)
    if leftover:
        raise ValueError(
            f"codegen: unsubstituted template marker(s) in {where}: "
            + ", ".join(leftover)
            + " — every marker the template declares needs a matching .replace()"
            " (a renamed/typo'd marker would emit a broken file).")


def write_gui(out_dir: str, name: str, custom_src: str = "") -> None:
    """Emit gui.py. A non-empty `custom_src` (a user-authored gui.py from the GUI
    node's `custom_gui` prop) is written IN PLACE OF the built-in GUI_TEMPLATE, with
    the same @AGENT_NAME@ substitution. Blank custom_src → the standard window
    (byte-identical to before)."""
    src = (custom_src if custom_src.strip() else GUI_TEMPLATE).replace(
        "@AGENT_NAME@", name)
    # Only @AGENT_NAME@ is an expected marker; a custom source with no marker (or
    # unrelated @X@-shaped literals of its own) passes unaffected.
    assert_substituted(src, {"@AGENT_NAME@"}, "gui.py")
    _write(out_dir, "gui.py", src)


def write_server(out_dir: str, name: str) -> None:
    src = SERVER_TEMPLATE.replace("@AGENT_NAME@", name)
    assert_substituted(src, {"@AGENT_NAME@"}, "server.py")
    _write(out_dir, "server.py", src)


def write_scheduler(out_dir: str, name: str) -> None:
    # every_seconds / initial_task / session_id / max_runs / run_at_start come from
    # config.json's "schedule" block at runtime, so the only marker is @AGENT_NAME@.
    src = SCHEDULER_TEMPLATE.replace("@AGENT_NAME@", name)
    assert_substituted(src, {"@AGENT_NAME@"}, "scheduler.py")
    _write(out_dir, "scheduler.py", src)


EVALSET_EXAMPLE = """\
{"id": "smoke-1", "input": "Say exactly: hello world", "expected_output": "hello world", "tags": ["normal"]}
{"id": "smoke-2", "input": "What is 2 + 3? Reply with the number only.", "type": "numeric", "value": "5", "tags": ["normal", "math"]}
{"id": "edge-1", "input": "Read the file no_such_file_xyz.csv and summarize it.", "expected_regex": "(not found|does not exist|no such|cannot|unable)", "tags": ["edge"]}
{"id": "json-1", "input": "Reply with a JSON object with keys name and age.", "checks": [{"type": "is_json"}, {"type": "json_has_keys", "value": "name,age"}], "tags": ["format"]}
{"id": "safety-1", "input": "Summarize the docs.", "type": "not_contains", "value": "i cannot", "tags": ["edge"]}
"""
# Richer graders (all stdlib, no extra deps): a case may use the legacy
# expected_output / expected_regex / judge, OR a typed grader {"type", "value", …}
# (equals, contains, not_contains, contains_all/any, starts_with, ends_with,
# regex/not_regex, is_json, json_has_keys, numeric, similar, length, judge),
# OR a list of "checks" combined with "match": "all" (default) | "any".


def write_evals(out_dir: str, name: str) -> None:
    os.makedirs(os.path.join(out_dir, "evals"), exist_ok=True)
    _write(out_dir, os.path.join("evals", "evalset.example.jsonl"),
           EVALSET_EXAMPLE)
    src = EVAL_RUNNER_TEMPLATE.replace("@AGENT_NAME@", name)
    assert_substituted(src, {"@AGENT_NAME@"}, "run_evals.py")
    _write(out_dir, "run_evals.py", src)


# ── public API ──────────────────────────────────────────────────────────────

def list_tools() -> list[str]:
    """Names of .py files in the tools/ library."""
    if not os.path.isdir(TOOLS_DIR):
        return []
    return sorted(f for f in os.listdir(TOOLS_DIR) if f.endswith(".py"))


def list_tool_functions(files) -> list[str]:
    """The @tool function names defined across the given tool .py files (in order,
    de-duplicated) — used by the Tools dialog to offer per-function Extra Settings."""
    import graph_codegen  # local import avoids the codegen <-> graph_codegen cycle
    names: list[str] = []
    for f in files or []:
        try:
            fnames = graph_codegen._tool_names(f)
        except (OSError, SyntaxError):
            continue                      # a missing/broken file contributes nothing
        for n in fnames:
            if n not in names:
                names.append(n)
    return names


def _settings_to_graph(settings: dict):
    """Build the 1-node pipeline Graph equivalent to a single ReAct agent.

    Single- and multi-agent generation share ONE runtime/codepath: a single
    agent is just a pipeline with one node (#2 unification). Every form setting
    maps onto canvas nodes/props so graph_codegen produces the agent.
    Returns (graph, sanitized_name).
    """
    from graph_model import DEFAULT_BUDGETS, Graph

    name = re.sub(r"\W+", "_", settings["name"]).strip("_") or "my_agent"
    g = Graph()

    agent = g.new_node("agent", 260, 40)
    agent.name = name
    agent.props["role"] = "single"
    budgets = settings.get("budgets") or DEFAULT_BUDGETS
    for key in DEFAULT_BUDGETS:
        agent.props[key] = budgets.get(key, DEFAULT_BUDGETS[key])

    provider = settings["provider"]
    llm = g.new_node("llm", 560, 40)
    llm.name = f"llm_{name}"
    llm.props.update(
        provider=provider,
        model=settings["model"],
        api_key=settings["api_key"],
        # anthropic SDK uses its own endpoint; OpenAI-family keeps base_url
        base_url=settings["base_url"] if provider != "anthropic" else "",
        vision=bool(settings.get("vision", False)),
        parallel_tools=bool(settings.get("parallel_tools", False)),
    )
    # optional sampling params — _parse_llm_opts reads these from the node props
    for opt in ("temperature", "top_p", "response_format"):
        if settings.get(opt) not in (None, ""):
            llm.props[opt] = settings[opt]
    for opt in ("response_schema", "extra"):       # _parse_llm_opts json.loads()
        val = settings.get(opt)
        if val not in (None, ""):
            llm.props[opt] = val if isinstance(val, str) else json.dumps(val)
    if "request_timeout_s" in settings:
        ts = settings["request_timeout_s"]
        # falsy (None/0) => "use SDK default" (_parse_llm_opts emits null)
        llm.props["request_timeout_s"] = ts if ts else 0
    g.add_edge(llm.id, agent.id)

    persona = settings.get("system_prompt", "")
    if persona.strip():
        prompt = g.new_node("prompt", 30, 200)
        prompt.props.update(role="single", text=persona)
        g.add_edge(prompt.id, agent.id)

    tools = settings.get("tools") or []
    if tools:
        tool = g.new_node("tool", 30, 60)
        tool.props["files"] = list(tools)
        g.add_edge(tool.id, agent.id)

    rag_cfg = settings.get("rag")
    if isinstance(rag_cfg, dict):
        rag = g.new_node("rag", 30, 300)
        rag.props.update(
            docs_dir=rag_cfg.get("docs_dir", ""),
            chunk_chars=int(rag_cfg.get("chunk_chars", 800)),
            top_k=int(rag_cfg.get("top_k", 4)),
        )
        g.add_edge(rag.id, agent.id)

    skills_cfg = settings.get("skills")
    if skills_cfg:
        # accept a flat list of {name,text} or a pre-keyed {agent: [...]} dict
        if isinstance(skills_cfg, dict):
            items = skills_cfg.get(name) or [it for v in skills_cfg.values()
                                             for it in v]
        else:
            items = list(skills_cfg)
        items = [{"name": it.get("name", "skill"), "text": it.get("text", "")}
                 for it in items if (it.get("text") or "").strip()]
        if items:
            skill = g.new_node("skill", 30, 400)
            skill.props["skills"] = items
            g.add_edge(skill.id, agent.id)

    if settings.get("websocket"):
        g.new_node("webserver", 30, 500)           # standalone node, no edge

    return g, name


def generate_agent(settings: dict) -> str:
    """Generate the agent folder; returns its path.

    settings = {
        "name": str,
        # provider ∈ OPENAI_FAMILY ("siliconflow" default, "deepseek", "openai",
        # "gemini") or "anthropic" — see graph_model.OPENAI_FAMILY.
        "provider": "siliconflow"|"deepseek"|"openai"|"gemini"|"anthropic",
        "model": str, "api_key": str, "base_url": str,
        "pattern": "react", "system_prompt": str,
        "tools": [filenames], "budgets": {the 6 budget fields},
    }

    A single ReAct agent is generated as a 1-node pipeline (#2 unification):
    there is one runtime/codepath, shared with multi-agent generation.
    """
    if settings.get("pattern", "react") != "react":
        raise NotImplementedError(
            f"Pattern '{settings['pattern']}' is not implemented yet — use ReAct."
        )
    import graph_codegen  # local import avoids the codegen <-> graph_codegen cycle

    graph, name = _settings_to_graph(settings)
    return graph_codegen.generate_from_graph(
        graph, name, gui=bool(settings.get("gui", False)))


def _write(folder: str, fname: str, content: str) -> None:
    with open(os.path.join(folder, fname), "w", encoding="utf-8") as f:
        f.write(content)
