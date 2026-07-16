"""Graph Designer agent — generates / modifies MetaAgent graphs (.mta / graph.json)
from a user's requirements.

It is a SECOND instance of the coding-agent chat/session core (coding_agent.py),
but with its OWN brain (the design-mta-graph skill + derived design knowledge),
its OWN tools (read / analyze / write graphs), and ISOLATED storage (its own
sessions dir) — so it never shares context, memory or history with the Tool
Generator. Runs in parallel with it.
"""

from __future__ import annotations

import json
import os

import app_config
import design_assistant
import graph_codegen
from app_config import HISTORY_PATH
from coding_agent import CodingAgent
from graph_model import Graph, save_mta

# Read-only bundle root (sys._MEIPASS when frozen) vs writable root (next to the
# exe). The skill files + example graphs ship in the bundle; graphs the designer
# SAVES go to the writable dir so they persist (frozen-safe — see app_config).
_BASE = app_config.BASE_DIR
_DATA = getattr(app_config, "DATA_DIR", app_config.BASE_DIR)
GRAPHS_DIR = os.path.join(_DATA, "graphs")            # where the designer SAVES
_BUNDLE_GRAPHS = os.path.join(_BASE, "graphs")        # bundled example graphs (read)
TOOLS_DIR_LOCAL = app_config.TOOLS_DIR
# The design skill (its .claude files must be --add-data'd into a frozen build;
# see MetaAgent.spec). Falls back to derived knowledge if absent.
_SKILL_DIR = os.path.join(_BASE, ".claude", "skills", "design-mta-graph")
# Designer sessions live in their OWN dir (a sibling of the coding agent's), so
# the two agents' histories are fully separate.
_STORAGE_DIR = os.path.join(os.path.dirname(HISTORY_PATH), "designer")


def _graph_dirs():
    """Where to look for graphs: the writable saved-dir first, then bundled examples."""
    return [d for d in (GRAPHS_DIR, _BUNDLE_GRAPHS) if os.path.isdir(d)]


def _read_skill(fname: str) -> str:
    try:
        with open(os.path.join(_SKILL_DIR, fname), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


# The Designer window installs a callback here so a written graph is rendered live
# onto the canvas (mirrors coding_agent.set_confirm_handler).
_ON_GRAPH = {"fn": None}


def set_graph_handler(fn) -> None:
    """fn(graph: Graph, name: str) -> None — called after write_graph saves, to
    render the result on the canvas."""
    _ON_GRAPH["fn"] = fn


def reset_graph_handler() -> None:
    _ON_GRAPH["fn"] = None


# ── graph tools the designer agent can call ──────────────────────────────────
def _list_graphs() -> str:
    names = set()
    for d in _graph_dirs():
        try:
            names.update(f for f in os.listdir(d) if f.endswith(".mta"))
        except OSError:
            pass
    return "\n".join(sorted(names)) if names else "(no saved graphs yet)"


def _read_graph(name: str) -> str:
    """Return an existing graph's graph.json (so the agent can MODIFY it). Looks in
    the writable saved-dir, then the bundled examples."""
    fn = name if name.endswith(".mta") else name + ".mta"
    if os.path.isabs(fn):
        candidates = [fn]
    else:
        candidates = [os.path.join(d, fn) for d in _graph_dirs()]
    path = next((p for p in candidates if os.path.isfile(p)), None)
    if path is None:
        return f"[ERROR] no graph named '{name}' (try list_graphs)."
    try:
        g, _ = _load_any(path)
    except Exception as e:  # noqa: BLE001
        return f"[ERROR] could not read '{name}': {e}"
    return json.dumps(g.to_dict(), ensure_ascii=False, indent=2)


def _load_any(path):
    from graph_model import load_mta
    res = load_mta(path, TOOLS_DIR_LOCAL)
    return (res if isinstance(res, tuple) else (res, None))[0], None


def _graph_from_json(graph_json) -> Graph:
    data = json.loads(graph_json) if isinstance(graph_json, str) else graph_json
    if not isinstance(data, dict):
        raise ValueError("graph must be a JSON object with nodes/edges")
    return Graph.from_dict(data)


def _autoplace(g: Graph) -> None:
    """Give nodes a simple grid position when the agent left them all at (0,0),
    so the rendered graph isn't a single overlapping stack."""
    nodes = list(g.nodes.values())
    if any((n.x or n.y) for n in nodes):
        return
    for i, n in enumerate(nodes):
        n.x, n.y = 60 + (i % 4) * 240, 60 + (i // 4) * 160


def _check_graph(graph_json) -> str:
    """Validate a candidate graph.json: analyze() errors/warnings + design metrics."""
    try:
        g = _graph_from_json(graph_json)
    except Exception as e:  # noqa: BLE001
        return f"[ERROR] invalid graph JSON: {e}"
    try:
        review = design_assistant.design_review(g)
    except Exception as e:  # noqa: BLE001
        return f"[ERROR] analyze failed: {e}"
    return json.dumps(review, ensure_ascii=False, indent=2, default=str)


def _write_graph(name: str, graph_json) -> str:
    """Validate then SAVE a graph as graphs/<name>.mta and render it on the canvas.
    Refuses to save if analyze() reports errors (returns them so the agent fixes)."""
    try:
        g = _graph_from_json(graph_json)
    except Exception as e:  # noqa: BLE001
        return f"[ERROR] invalid graph JSON: {e}"
    info = graph_codegen.analyze(g)
    if info.get("errors"):
        return ("[not saved] the graph has errors — fix these and call write_graph "
                "again:\n- " + "\n- ".join(info["errors"]))
    _autoplace(g)
    safe = "".join(c if c.isalnum() or c in "-_ " else "_" for c in (name or "")).strip() or "designed_graph"
    path = os.path.join(GRAPHS_DIR, safe + ".mta")
    try:
        os.makedirs(GRAPHS_DIR, exist_ok=True)
        save_mta(g, path, TOOLS_DIR_LOCAL)
    except Exception as e:  # noqa: BLE001
        return f"[ERROR] could not save: {e}"
    if _ON_GRAPH["fn"]:                       # render live on the canvas
        try:
            _ON_GRAPH["fn"](g, safe)
        except Exception:  # noqa: BLE001
            pass
    warn = ("  Warnings:\n- " + "\n- ".join(info["warnings"])) if info.get("warnings") else ""
    return f"Saved and rendered '{safe}.mta' ({len(g.nodes)} nodes)." + warn


def _read_config_table() -> str:
    """The exhaustive per-node parameter reference (ConfigTable.md)."""
    return _read_skill("ConfigTable.md") or "(reference unavailable)"


DESIGNER_TOOLS = {
    "list_graphs": _list_graphs,
    "read_graph": _read_graph,
    "check_graph": _check_graph,
    "write_graph": _write_graph,
    "read_config_table": _read_config_table,
}
DESIGNER_HIGH_RISK = {"write_graph"}   # writes disk + mutates the canvas -> HITL

DESIGNER_TOOL_DEFS = [
    {"type": "function", "function": {
        "name": "list_graphs", "description": "List saved example/graph .mta files.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "read_graph",
        "description": "Return an existing graph's graph.json so you can modify it.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "graph file name (with or without .mta)"}},
            "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "check_graph",
        "description": "Validate a candidate graph.json — returns analyze() errors/"
                       "warnings + design metrics. Use before write_graph.",
        "parameters": {"type": "object", "properties": {
            "graph_json": {"type": "string", "description": "the full graph.json"}},
            "required": ["graph_json"]}}},
    {"type": "function", "function": {
        "name": "write_graph",
        "description": "Validate, SAVE as graphs/<name>.mta, and render on the canvas. "
                       "Rejected (with the errors) if the graph is invalid.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"},
            "graph_json": {"type": "string", "description": "the full graph.json"}},
            "required": ["name", "graph_json"]}}},
    {"type": "function", "function": {
        "name": "read_config_table",
        "description": "The exhaustive per-node parameter reference (types/defaults/"
                       "when to use). Read it when you need exact node knobs.",
        "parameters": {"type": "object", "properties": {}}}},
]

_DESIGNER_INTRO = """You are MetaAgent's built-in GRAPH DESIGNER agent. From a user's \
requirements you DESIGN or MODIFY a MetaAgent agent graph (an .mta bundle / its graph.json) \
and save + render it on the canvas.

Workflow:
1. Understand the requirement; pick the right pattern (chain / router / supervisor / \
orchestrator / fan-out+join / condition+while / HITL / map-reduce / voting / memory / schedule).
2. To modify an existing graph, call read_graph first. For exact node parameters, call \
read_config_table.
3. Emit the FULL graph.json: nodes [{id,kind,name,x,y,props}], edges [{src,dst,props}], \
plus state_schema / type_defs / recursion_limit as needed. Node `id` = "<kind>_<n>"; each \
agent needs one `llm` linked; keep api_key "" (the user sets it later).
4. Call check_graph to validate; fix any errors; then call write_graph to save + render.

Graph rules: exactly ONE entry agent; a plain agent has at most one outgoing flow link \
(branch via router/condition/routing-hitl/self-routing planner/fanout); resource→agent edges \
attach llm/tool/skill/prompt/rag/memory/mcp; emitter→agent for gui/webserver/schedule/eval.

Language: match the user's language in the PROMPTS you generate (each agent's system \
prompt and any prompt-node text). If the user writes their requirements in Chinese, write \
the generated prompts in Chinese; Japanese -> Japanese, French -> French; otherwise default \
to English. (Node names / ids / kinds and other graph-structure fields stay in English.)

Below is the derived node/edge knowledge and the design skill.

"""


def designer_system_prompt() -> str:
    parts = [_DESIGNER_INTRO, design_assistant.knowledge_prompt(), _read_skill("SKILL.md")]
    return "\n\n".join(p for p in parts if p)


def make_designer_agent() -> CodingAgent:
    """A CodingAgent configured as the graph designer: design brain, graph tools,
    isolated sessions."""
    return CodingAgent(system_prompt=designer_system_prompt(),
                       local_tools=DESIGNER_TOOLS, tool_defs=DESIGNER_TOOL_DEFS,
                       high_risk=DESIGNER_HIGH_RISK, storage_dir=_STORAGE_DIR,
                       max_tool_rounds=0)   # unlimited: designing a graph is multi-step
