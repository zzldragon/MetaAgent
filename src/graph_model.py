"""Data model for the visual agent canvas: nodes, links, validation, save/load.

Node kinds and what a link INTO an agent means:
    llm    → agent   assigns an LLM (one or more; the first link is the
                     primary, additional links are fallbacks in link order)
    tool   → agent   attaches a tool from the tools/ library
    skill  → agent   appends skill text to the agent's system prompt
    prompt → agent   sets the agent's persona (at most 1)
    rag    → agent   gives the agent a search_docs tool over a local document
                     folder (BM25 retrieval; at most 1 per agent)
    mcp    → agent   connects the agent to an MCP server (stdio or HTTP-SSE);
                     the server's tools are discovered at runtime and become
                     callable. Multiple MCP clients may link to one agent.
    agent  → agent   control flow: run left agent, feed its output to the right
                     (a link back to an earlier agent = revise loop)

The webserver node is standalone (no links): its presence makes generation
emit a server.py that exposes the whole agent app over WebSocket.

The gui node links to the entry agent (gui → agent): doing so makes generation
emit the PySide6 desktop GUI (gui.py). No gui node linked = headless agent only.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import zipfile
from dataclasses import asdict, dataclass, field

NODE_KINDS = ("agent", "llm", "tool", "skill", "prompt", "rag", "memory",
              "webserver", "mcp", "workerpool", "router", "hitl", "eval", "gui",
              "schedule", "condition", "while", "foreach", "setstate", "guardrail",
              "end", "fanout", "join", "subgraph")

# Single source of truth for per-kind PRESENTATION data (plain data only — no Qt,
# so it stays in the model layer). The canvas view (canvas_qt/designer.py) derives
# its KIND_LABELS / KIND_COLORS tables from this; the kind->DialogClass map stays
# in canvas_qt/dialogs.py (Qt) guarded by its own check. INSERTION ORDER defines
# the palette / add-menu display order — keep it identical to NODE_KINDS.
KIND_META = {
    "agent":      {"label": "Agent",       "color": "#BBDEFB"},
    "llm":        {"label": "LLM",         "color": "#C8E6C9"},
    "tool":       {"label": "Tools",       "color": "#FFE0B2"},
    "skill":      {"label": "Skills",      "color": "#E1BEE7"},
    "prompt":     {"label": "Prompt",      "color": "#FFF9C4"},
    "rag":        {"label": "RAG",         "color": "#B2DFDB"},
    "memory":     {"label": "Memory",      "color": "#80CBC4"},
    "webserver":  {"label": "WebServer",   "color": "#FFCDD2"},
    "mcp":        {"label": "MCP",         "color": "#D1C4E9"},
    "workerpool": {"label": "Worker Pool", "color": "#C5CAE9"},
    "router":     {"label": "Router",      "color": "#B0BEC5"},
    "hitl":       {"label": "HITL",        "color": "#FFE082"},
    "eval":       {"label": "Eval",        "color": "#DCEDC8"},
    "gui":        {"label": "GUI",         "color": "#FFCCBC"},
    "schedule":   {"label": "Schedule",    "color": "#FFAB91"},
    "condition":  {"label": "If/Else",     "color": "#F8BBD0"},
    "while":      {"label": "While",       "color": "#CE93D8"},
    "foreach":    {"label": "For-Each",    "color": "#B39DDB"},
    "setstate":   {"label": "Set State",   "color": "#B3E5FC"},
    "guardrail":  {"label": "Guardrail",   "color": "#EF9A9A"},
    "end":        {"label": "End",         "color": "#90A4AE"},
    "fanout":     {"label": "Fan-out",     "color": "#80DEEA"},
    "join":       {"label": "Join",        "color": "#4DB6AC"},
    "subgraph":   {"label": "Subgraph",    "color": "#A5D6A7"},
}

# Fail fast at import (explicit raise, NOT assert — survives `python -O`): a kind
# added to NODE_KINDS but missing label/color here would otherwise be a silent
# blank node / KeyError in paint() at runtime.
if set(KIND_META) != set(NODE_KINDS):
    raise RuntimeError(
        "KIND_META keys must match NODE_KINDS exactly; "
        f"missing={set(NODE_KINDS) - set(KIND_META)} "
        f"extra={set(KIND_META) - set(NODE_KINDS)}")
for _k, _m in KIND_META.items():
    if not (_m.get("label") and _m.get("color")):
        raise RuntimeError(f"KIND_META[{_k!r}] needs both a label and a color")

# Providers that speak the OpenAI Chat Completions wire format (incl. Gemini's
# OpenAI-compatible endpoint). Everything else here means Anthropic's Messages
# API, which uses a different shape for structured output.
OPENAI_FAMILY = ("siliconflow", "deepseek", "openai", "gemini", "nvidia")


def response_format_support(provider: str, fmt: str) -> str:
    """How well a provider honors a response_format choice.

    Returns "yes" (full support), "weak" (accepted but may be silently
    ignored — gateway/model dependent), or "no" (not expressible at all).
    Drives the LLM dialog's hint and the codegen translation:
    OpenAI-family uses `response_format`; Anthropic uses `output_config.format`
    and has no bare JSON mode (only schema-constrained output).
    """
    if fmt in ("", "text"):
        return "yes"
    if fmt == "json_object":
        # Anthropic has no plain "valid JSON" mode — only json_schema.
        return "no" if provider == "anthropic" else "yes"
    if fmt == "json_schema":
        if provider in ("openai", "gemini", "anthropic"):
            return "yes"
        # deepseek / siliconflow accept it but enforcement depends on the model
        return "weak"
    return "no"


def tool_files(node) -> list:
    """Tool-library files a Tools node contributes, in order. Reads the new
    'files' list and falls back to the legacy single 'file' prop so graphs
    saved before the Tools node still load."""
    files = node.props.get("files")
    if files is None:
        one = node.props.get("file")
        return [one] if one else []
    return [f for f in files if f]


def eval_cases(node) -> list:
    """Eval cases on an eval node, keeping only those with an input and at
    least one expectation — legacy (expected_output / expected_regex / judge)
    or the richer grader forms (a single `type`, or a `checks` list)."""
    out = []
    for c in node.props.get("cases", []):
        if (c.get("input") or "").strip() and (
                c.get("expected_output") or c.get("expected_regex")
                or c.get("judge") or c.get("type") or c.get("checks")):
            out.append(c)
    return out


def skill_items(node) -> list:
    """Skills a Skills node contributes, as [{"name", "text"}, ...] with text.
    Reads the new 'skills' list and falls back to the legacy single 'text' prop
    (named after the node) so graphs saved before the Skills node still load."""
    skills = node.props.get("skills")
    if skills is None:
        text = (node.props.get("text") or "").strip()
        return [{"name": node.name, "text": text}] if text else []
    out = []
    for s in skills:
        text = (s.get("text") or "").strip()
        if text:
            out.append({"name": (s.get("name") or "skill").strip() or "skill",
                        "description": (s.get("description") or "").strip(),
                        "text": text,
                        "disable_model_invocation":
                            bool(s.get("disable_model_invocation"))})
    return out


def _parse_skill_frontmatter(fm: str) -> dict:
    """Tiny YAML-subset parser for SKILL.md frontmatter (no PyYAML): key: value,
    quoted values, and folded/literal block scalars (>, >-, |, |-). Mirrors
    runtime/skills.py _parse_frontmatter."""
    fields, lines, i = {}, fm.split("\n"), 0
    while i < len(lines):
        line = lines[i]; i += 1
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = re.match(r"([A-Za-z0-9_-]+)\s*:\s*(.*)$", line)
        if not m:
            continue
        key, val = m.group(1).strip(), m.group(2).strip()
        if val in (">", ">-", ">+", "|", "|-", "|+"):
            block = []
            while i < len(lines) and (not lines[i].strip()
                                      or lines[i][:1] in (" ", "\t")):
                block.append(lines[i].strip()); i += 1
            fields[key] = ("\n" if val.startswith("|") else " ").join(block).strip()
        else:
            if len(val) >= 2 and val[0] in "\"'" and val[-1] == val[0]:
                val = val[1:-1]
            fields[key] = val
    return fields


def parse_skill_md(text: str) -> dict:
    """Parse a Cursor/Claude SKILL.md (YAML frontmatter + markdown body) into
    {name, description, text, disable_model_invocation}. Mirrors
    runtime/skills.py parse_skill_md so the canvas importer matches the runtime."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    name = description = ""
    disable = False
    body = text
    m = re.match(r"\s*---\n(.*?)\n---\n?(.*)", text, re.DOTALL)
    if m:
        body = m.group(2)
        f = _parse_skill_frontmatter(m.group(1))
        name = (f.get("name") or "").strip()
        description = (f.get("description") or "").strip()
        dv = f.get("disable-model-invocation", f.get("disable_model_invocation"))
        disable = str(dv).strip().lower() in ("true", "yes", "1", "on")
    return {"name": name, "description": description, "text": body.strip(),
            "disable_model_invocation": disable}


# ── shared-state schema (graph-level) ────────────────────────────────────────
# A graph may declare typed STATE fields that agent stages read and write at
# runtime. This is the data model ONLY — threading the state through generated
# code is a later step. Each field is a plain dict {name, type, reducer,
# default}, stored on Graph.state_schema (kept raw like node props / eval cases,
# validated on use via state_fields()).
STATE_TYPES = ("str", "int", "float", "bool", "list", "dict")

# How repeated / (later) concurrent writes to a field combine, borrowing
# LangGraph's per-key reducer idea: overwrite = last write wins; the rest let a
# field accumulate so fan-in becomes well-defined. The structured policies
# (extend / merge_shallow / merge_deep / upsert_by_key) operate on list/dict and
# custom nested types — they are generic over the data, so no per-type code is
# needed (see runtime _REDUCERS). upsert_by_key merges a list of records by an id
# field named in the field/type's `merge_key`.
STATE_REDUCERS = ("overwrite", "append", "add", "max", "min",
                  "extend", "merge_shallow", "merge_deep", "upsert_by_key",
                  # escape hatch (P4): a custom type may ship its own merge(old,new)
                  # in type_defs[Name]["merge_src"]; a field/type uses it via "custom".
                  "custom")

_STATE_TYPE_DEFAULTS = {"str": "", "int": 0, "float": 0.0,
                        "bool": False, "list": [], "dict": {}}


def reducers_for_type(ftype: str) -> tuple:
    """SCALAR reducers for a NATIVE field type (str/int/float/bool). numbers →
    add/max/min, str append = text concatenation (type-aware at runtime), else
    overwrite. dict/list and custom types are handled by merge_policies_for()."""
    if ftype == "list":
        return ("overwrite", "append")
    if ftype == "str":
        return ("overwrite", "append")   # append on a str concatenates (\n\n-joined)
    if ftype in ("int", "float"):
        return ("overwrite", "add", "max", "min")
    return ("overwrite",)


# ── custom / nested state types (Approach A: declarative JSON-Schema types) ───
# A graph may declare named types in Graph.type_defs:
#   {Name: {"schema": <json-schema dict>, "merge": <policy>, "merge_key": <str>,
#           "description": <str>}}
# A state field's `type` may then be a native scalar, a custom Name, or list[Name].
# A schema node {"$type": "Name"} references another declared type (resolved by
# type_json_schema, cycle-guarded). See metaagent-custom-state-types.

_LIST_OF_RE = re.compile(r"^list\[(\w+)\]$")
_JSON_PRIMITIVE = {"str": "string", "int": "integer", "float": "number",
                   "bool": "boolean", "list": "array", "dict": "object"}


def is_custom_type(ftype: str) -> bool:
    """True if `ftype` is not a bare native scalar/container — i.e. a custom type
    name or list[Name]. (Native str/int/float/bool/list/dict return False.)"""
    return bool(ftype) and ftype not in STATE_TYPES and not _is_native(ftype)


def _is_native(ftype: str) -> bool:
    return ftype in STATE_TYPES


def type_kind(ftype: str, type_defs: dict | None = None) -> str:
    """Classify a field type for merge/UI purposes: 'record' (object-shaped:
    native dict or a custom object type), 'list' (native list, list[Name], or a
    custom array type), or 'scalar' (str/int/float/bool or unknown)."""
    type_defs = type_defs or {}
    if ftype == "dict":
        return "record"
    if ftype == "list" or _LIST_OF_RE.match(ftype or ""):
        return "list"
    if ftype in STATE_TYPES:
        return "scalar"
    td = type_defs.get(ftype)
    if td:
        return "list" if (td.get("schema") or {}).get("type") == "array" else "record"
    return "scalar"


def merge_policies_for(ftype: str, type_defs: dict | None = None) -> tuple:
    """Allowed merge policies for a field type (drives the dialog + validation).
    Superset of reducers_for_type: records get shallow/deep merge, lists get
    extend/upsert, native scalars keep their scalar reducers. A custom type that
    supplies its own merge function (merge_src) also offers 'custom' (P4)."""
    type_defs = type_defs or {}
    kind = type_kind(ftype, type_defs)
    if kind == "list":
        base = ("overwrite", "append", "extend", "upsert_by_key")
    elif kind == "record":
        base = ("overwrite", "merge_shallow", "merge_deep")
    else:
        return reducers_for_type(ftype)
    m = _LIST_OF_RE.match(ftype or "")
    tname = m.group(1) if m else ftype
    td = type_defs.get(tname) or {}
    if td.get("merge") == "custom" or (td.get("merge_src") or "").strip():
        base = base + ("custom",)
    return base


def type_json_schema(ftype: str, type_defs: dict | None = None,
                     _seen: frozenset = frozenset()) -> dict:
    """JSON Schema for a state field type — used as the set_state tool's parameter
    schema so the LLM emits well-formed (nested) values. Native → primitive;
    list[Name] → array of Name; custom Name → its schema with any {"$type":"X"}
    nodes resolved recursively (cycle-guarded → falls back to a bare object)."""
    type_defs = type_defs or {}
    if ftype in STATE_TYPES:
        js: dict = {"type": _JSON_PRIMITIVE[ftype]}
        if ftype == "list":
            js["items"] = {}
        return js
    m = _LIST_OF_RE.match(ftype or "")
    if m:
        return {"type": "array", "items": type_json_schema(m.group(1), type_defs, _seen)}
    if ftype in _seen:                       # reference cycle — stop expanding
        return {"type": "object"}
    td = type_defs.get(ftype)
    if td and isinstance(td.get("schema"), dict):
        return _resolve_schema_refs(td["schema"], type_defs, _seen | {ftype})
    return {"type": "string"}                # unknown type name → safe scalar


def _resolve_schema_refs(schema, type_defs, seen):
    """Deep-copy a JSON Schema, expanding {"$type": "Name"} nodes into that type's
    schema (recursively, cycle-guarded)."""
    if isinstance(schema, dict):
        ref = schema.get("$type")
        if isinstance(ref, str):
            return type_json_schema(ref, type_defs, seen)
        return {k: _resolve_schema_refs(v, type_defs, seen) for k, v in schema.items()}
    if isinstance(schema, list):
        return [_resolve_schema_refs(x, type_defs, seen) for x in schema]
    return schema


def default_for_type(ftype: str, type_defs: dict | None = None):
    """The empty default value for a field type: [] for lists, {} for records,
    else the native scalar default."""
    kind = type_kind(ftype, type_defs)
    if kind == "list":
        return []
    if kind == "record":
        return {}
    return _STATE_TYPE_DEFAULTS.get(ftype, "")


def validate_type_defs(type_defs: dict | None) -> list:
    """Return a list of error strings for a graph's type_defs (empty = valid).
    Checks: name is a non-reserved identifier not shadowing a native type; schema
    is a dict; merge is a known policy; upsert_by_key has a merge_key; every
    {"$type": X} reference resolves; no reference cycles."""
    errors: list = []
    defs = type_defs or {}
    for name, td in defs.items():
        where = f"Type '{name}'"
        if not str(name).isidentifier():
            errors.append(f"{where}: name must be a valid identifier.")
        if name in STATE_TYPES:
            errors.append(f"{where}: shadows a built-in type — pick another name.")
        if name in RESERVED_STATE_NAMES:
            errors.append(f"{where}: '{name}' is a reserved name.")
        if not isinstance(td, dict) or not isinstance(td.get("schema"), dict):
            errors.append(f"{where}: needs a JSON-Schema object in 'schema'.")
            continue
        merge = td.get("merge") or "overwrite"
        if merge not in STATE_REDUCERS:
            errors.append(f"{where}: unknown merge policy '{merge}'.")
        if merge == "upsert_by_key" and not (td.get("merge_key") or "").strip():
            errors.append(f"{where}: merge 'upsert_by_key' needs a 'merge_key' "
                          "(the id field to merge records by).")
        # P4 escape hatch: a custom merge function must be valid Python defining a
        # top-level `def merge(old, new)`.
        src = (td.get("merge_src") or "").strip()
        if merge == "custom" and not src:
            errors.append(f"{where}: merge 'custom' needs merge_src — a Python "
                          "`def merge(old, new): ...`.")
        if src:
            import ast as _ast
            try:
                _tree = _ast.parse(src)
                if not any(isinstance(n, _ast.FunctionDef) and n.name == "merge"
                           for n in _tree.body):
                    errors.append(f"{where}: merge_src must define a top-level "
                                  "`def merge(old, new)`.")
            except SyntaxError as e:
                errors.append(f"{where}: merge_src has a syntax error: {e}")
        for ref in _schema_refs(td["schema"]):
            if ref not in defs:
                errors.append(f"{where}: references undefined type '{ref}' "
                              "($type). Define it or fix the name.")
    # reference-cycle guard
    for name in defs:
        if _has_ref_cycle(name, defs, set()):
            errors.append(f"Type '{name}': its $type references form a cycle.")
    return errors


def _schema_refs(schema):
    """All $type reference names inside a schema (recursive)."""
    out = []
    if isinstance(schema, dict):
        if isinstance(schema.get("$type"), str):
            out.append(schema["$type"])
        for v in schema.values():
            out += _schema_refs(v)
    elif isinstance(schema, list):
        for x in schema:
            out += _schema_refs(x)
    return out


def _has_ref_cycle(name, defs, stack):
    if name in stack:
        return True
    td = defs.get(name)
    if not td or not isinstance(td.get("schema"), dict):
        return False
    stack = stack | {name}
    return any(_has_ref_cycle(r, defs, stack) for r in _schema_refs(td["schema"])
               if r in defs)


# Shared-state fields the FRAMEWORK maintains automatically — the user never
# declares or writes them. `user_input` is set ONCE at the start of a run to the
# user's original request and never changed (read-only for the whole graph); at
# runtime every tool execution appends its name to `tool_calls` and every
# agent-stage visit appends its name to `agents`. Agents may opt IN to READ any of
# them (list them in the agent's `reads`, or reference them in a Condition); they
# are never user-writable and can't be redeclared in the schema editor.
RESERVED_STATE_FIELDS = [
    {"name": "user_input", "type": "str", "reducer": "overwrite", "default": "",
     "description": "The user's original request for this run — set once at the "
                    "start and never changed (read-only for the whole graph).",
     "builtin": True},
    {"name": "tool_calls", "type": "list", "reducer": "append", "default": [],
     "description": "Tools called so far this run (auto-maintained).",
     "builtin": True},
    {"name": "agents", "type": "list", "reducer": "append", "default": [],
     "description": "Agent stages visited so far this run (auto-maintained).",
     "builtin": True},
]
# Opt-in working checklist, written by the built-in `write_todos` tool (NOT the
# framework — unlike tool_calls/agents). Injected into the schema only when an
# agent enables the tool. Overwrite reducer: each write_todos call replaces the
# whole list (the model resends it with updated statuses).
TODOS_STATE_FIELD = {
    "name": "todos", "type": "list", "reducer": "overwrite", "default": [],
    "description": "Working checklist an agent maintains via the write_todos tool.",
    "builtin": True}
# All reserved names (incl. todos) — users may not declare or write any of them,
# even on a graph where the todos field isn't currently injected.
RESERVED_STATE_NAMES = frozenset(
    [f["name"] for f in RESERVED_STATE_FIELDS] + [TODOS_STATE_FIELD["name"]])


def todos_enabled(graph) -> bool:
    """True if any agent / worker-pool node opted into the write_todos tool."""
    for n in (getattr(graph, "nodes", None) or {}).values():
        if (getattr(n, "kind", "") in ("agent", "workerpool")
                and (getattr(n, "props", None) or {}).get("enable_todos")):
            return True
    return False


def state_fields(graph, include_builtins: bool = True) -> list:
    """Validated shared-state fields of a graph, as
    [{"name","type","reducer","default","description"}, ...]. Drops entries
    without a valid identifier name or with a duplicate name; coerces an unknown
    type/reducer to a safe default. Mirrors eval_cases()/skill_items() so the
    schema dialog and codegen share one normalization.

    The two RESERVED_STATE_FIELDS (tool_calls, agents) are prepended by default;
    because they go in FIRST, a user field colliding on those names is dropped by
    the dup guard below. Pass include_builtins=False for only the user-declared
    fields (the schema editor uses this so it never re-persists the built-ins)."""
    out, seen = [], set()
    if include_builtins:
        for bf in RESERVED_STATE_FIELDS:
            out.append(dict(bf))
            seen.add(bf["name"])
        if todos_enabled(graph):
            out.append(dict(TODOS_STATE_FIELD))
            seen.add(TODOS_STATE_FIELD["name"])
    type_defs = getattr(graph, "type_defs", None) or {}
    for f in getattr(graph, "state_schema", None) or []:
        name = (f.get("name") or "").strip()
        if not name.isidentifier() or name in seen:
            continue
        seen.add(name)
        # accept a native scalar/container, OR a declared custom type / list[Name];
        # anything else (a stale/undefined type) coerces to the safe 'str'.
        raw = f.get("type") or "str"
        ftype = raw if (raw in STATE_TYPES or _is_declared_type(raw, type_defs)) else "str"
        reducer = (f.get("reducer")
                   if f.get("reducer") in merge_policies_for(ftype, type_defs)
                   else "overwrite")
        default = f.get("default")
        if default is None:
            default = default_for_type(ftype, type_defs)
        rec = {"name": name, "type": ftype, "reducer": reducer,
               "default": default,
               "description": (f.get("description") or "").strip()}
        # Custom/nested types carry: (a) a precomputed JSON Schema so the generated
        # set_state tool can constrain the model's value, and (b) merge_key for
        # upsert_by_key. Both are added ONLY for custom types / non-empty keys, so
        # native-only graphs keep the exact old field shape (byte-identical output).
        if is_custom_type(ftype):
            rec["json_schema"] = type_json_schema(ftype, type_defs)
        m = _LIST_OF_RE.match(ftype)
        base = m.group(1) if m else ftype
        mkey = (f.get("merge_key") or "").strip()
        if not mkey:
            mkey = ((type_defs.get(base) or {}).get("merge_key") or "").strip()
        if mkey:
            rec["merge_key"] = mkey
        # P4: a 'custom' reducer routes to the base type's merge_src at runtime;
        # record which type so _apply_state can look up its compiled function.
        if reducer == "custom":
            rec["merge_type"] = base
        out.append(rec)
    return out


def _is_declared_type(ftype: str, type_defs: dict) -> bool:
    """True if `ftype` is a declared custom type name or list[Name] with Name
    declared."""
    if ftype in type_defs:
        return True
    m = _LIST_OF_RE.match(ftype or "")
    return bool(m and m.group(1) in type_defs)


# "Agent stages" sit in the control flow and may have agent-stage successors.
# - workerpool: an agent that fans out over a list of subtasks at runtime.
# - router: an agent that picks ONE of its successors at runtime (an LLM
#   classifies the input). A router may have several outgoing agent edges;
#   plain agents/pools may have at most one.
AGENT_KINDS = ("agent", "workerpool", "router")

# Deterministic control-flow nodes (no LLM): they sit in the graph-mode walk
# between agents. `condition` is an If/Else that routes on shared state; `while`
# is a loop guard (run its body while a condition holds, else take the exit) —
# it compiles to the same routing primitive as `condition`; `setstate` writes
# shared state; `guardrail` is an inline content gate that redacts/blocks the
# content flowing through it; `end` is a terminal SINK (no outgoing links) that
# finishes the run early, returning whatever output reached it — handy on an
# If/Else else-branch or a While exit to stop before the rest of the pipeline.
# Unlike agents they have no system prompt / budgets and never appear in the
# per-agent spec — run_graph handles them.
CONTROL_KINDS = ("condition", "while", "foreach", "setstate", "guardrail", "end",
                 "fanout", "join")

# Flow nodes sit in the control flow. A hitl node is a human-in-the-loop
# checkpoint: it carries no LLM/tools, it just pauses the run between stages so
# a person can approve / edit / reject the work in flight. At generation time a
# hitl node is spliced out of the agent flow and recorded as a review gate on
# the agent it feeds (see graph_codegen._splice_hitl).
# A `subgraph` node embeds another graph (its full to_dict lives in props['graph_json'])
# and runs it as one step. It carries no LLM/tools of its own; at generation time
# `expand_subgraphs` FLATTENS it into the parent (child nodes namespaced + spliced in
# place), so codegen/analyze never see the subgraph kind — it's pure design-time reuse.
# It sits in the flow like a stage: agent → subgraph → agent, condition → subgraph, etc.
SUBGRAPH_KINDS = ("subgraph",)
FLOW_KINDS = AGENT_KINDS + ("hitl",) + CONTROL_KINDS + SUBGRAPH_KINDS

# resource kinds an agent stage can consume
_RESOURCES = ("llm", "tool", "skill", "prompt", "rag", "memory", "mcp")
ALLOWED_EDGES = {(r, a) for r in _RESOURCES for a in AGENT_KINDS}
ALLOWED_EDGES |= {(a, b) for a in FLOW_KINDS for b in FLOW_KINDS}
# an eval node may target one agent stage (eval → agent); standalone = no edge
ALLOWED_EDGES |= {("eval", a) for a in AGENT_KINDS}
# a gui node, linked to the entry agent, makes generation emit the PySide6
# desktop GUI (gui.py). Like eval it links gui → agent; unlinked = no GUI.
ALLOWED_EDGES |= {("gui", a) for a in AGENT_KINDS}
# a schedule node, linked to the entry agent, makes generation emit scheduler.py —
# an ambient runner that calls the agent on an interval (schedule → agent).
ALLOWED_EDGES |= {("schedule", a) for a in AGENT_KINDS}
# an End node is a terminal SINK: things link INTO it (agent/condition/while/... →
# end, already allowed by the FLOW_KINDS product) but nothing links OUT of it.
ALLOWED_EDGES -= {("end", b) for b in FLOW_KINDS}
# ...and NOT from a router: a router branches among AGENTS (the LLM names one —
# End isn't an agent). Reach End from a Condition / While branch (or a plain agent
# / setstate / guardrail edge) instead. A HITL→End edge IS allowed: for a route-
# mode HITL (2+ outgoing) it's a valid "human stops here" branch; a gate-mode HITL
# (1 outgoing) that points only at End is still caught as malformed by _validate_hitl.
ALLOWED_EDGES -= {("router", "end")}

# An agent may have at most one inbound link of these kinds.
# (llm and mcp are NOT here: several LLMs = fallback chain; several MCP
# clients = several tool servers. rag is NOT here either: several RAG nodes =
# several knowledge bases, each exposed as its own retrieval tool.)
SINGLETON_INPUTS = {"prompt", "gui"}

# Budgets default to UNLIMITED (0) so demos run to completion out of the box — the
# designer opts INTO a real cap per agent. 0 means: no iteration/tool-call/wall-clock
# limit, and no explicit output-token cap (provider default; Anthropic keeps 8000).
DEFAULT_BUDGETS = {
    "max_iterations": 0,
    "max_tool_calls": 0,
    "max_output_tokens": 0,
    "max_wall_clock_s": 0,
}


def default_props(kind: str) -> dict:
    # Human-in-the-loop knobs (active only when the global HITL switch is on):
    #  hitl_review (default off): pause for review of this stage's INPUT before
    #    it runs — the property form of a HITL checkpoint, for the entry stage.
    #  hitl_triggers: conditions that pause this agent automatically —
    #    "high_risk_tool" (before a write/send/delete tool) and/or
    #    "low_confidence" (after answering, when the agent's self-rated
    #    confidence is below hitl_confidence_threshold).
    #  hitl_on_reject: "stop" | "revise" — shared by all of the above.
    _HITL = {"hitl_review": False, "hitl_on_reject": "stop",
             "hitl_triggers": ["high_risk_tool"],
             "hitl_confidence_threshold": 0.6,
             # Extra Settings for the per-agent review gate (opt-in; blank = unchanged)
             "hitl_decisions": ["approve", "edit", "reject"],
             "hitl_timeout": 0, "hitl_on_timeout": "approve"}
    if kind == "agent":
        # route_self (planner role only): the planner picks ONE of its successor
        # agents itself, instead of needing a separate Router node — saves a
        # routing LLM call. Honored only when role == "planner" and it has >1
        # outgoing agent link.
        # reads / writes (shared state): names of graph state fields this agent
        # may read (injected into its prompt) and write. Empty = the smallest-
        # version default (read all / write via a fenced block); honored by
        # codegen in a later step.
        # quick_response (self-routing planner only): offer the planner a "no
        # branch fits" choice (route_to __none__) that ends the run with the
        # planner's own answer — skips the downstream workers/critic for trivial
        # input (a greeting, a directly-answerable question). Honored only with
        # route_self + 2+ outgoing agent links.
        # enable_todos: give this agent the built-in write_todos tool — a working
        # checklist it maintains in the `todos` shared-state field (opt-in; off by
        # default). Best for agents doing sustained multi-step work.
        # mode_label (multi-pattern apps): tag a sub-pipeline's ENTRY agent with a
        # label (e.g. "react"/"pec"/"supervisor") to make it a runtime-selectable
        # pattern. ≥1 tagged agent => the graph is multi-pattern: each tagged
        # entry + its reachable sub-pipeline becomes a mode the end-user picks via
        # /mode <label> (or the GUI dropdown). Empty = ordinary single-pattern.
        # code_exec: give this agent the built-in run_python tool — it writes &
        # runs short Python in an ISOLATED subprocess (workspace cwd, scrubbed env,
        # timeout, HITL-gated). Isolation, NOT a security sandbox. Off by default.
        # require_writes: after this agent runs, if it did NOT record every field
        # in `writes` (via the set_state tool or a ```state block), re-prompt it to
        # do so (bounded retries), then proceed. A best-effort "force the write",
        # honored in chain/graph mode. Off by default; only meaningful with writes.
        return {"role": "single", "route_self": False, "quick_response": False,
                "reads": [], "writes": [], "require_writes": False,
                "guardrails": {}, "enable_todos": False,
                "mode_label": "", "code_exec": False,
                "code_exec_backend": "subprocess",     # subprocess | docker | auto
                "code_exec_timeout": 30, "code_exec_memory_mb": 512,
                "code_exec_image": "python:3.11-slim",  # docker backend only
                # web_search: a built-in keyless web search (DuckDuckGo). A NETWORK
                # egress — HITL-gated by default; off by default (offline-first).
                "web_search": False,
                # per-agent web_search config (blank = inherit the global config.json
                # 'web_search' block). Lets THIS agent pick its own engine / key /
                # base URL / proxy. Keep the api_key blank in a shared .mta (secret);
                # a non-empty value is merged over the global block at generate time.
                "web_search_engine": "",     # duckduckgo|tavily|serpapi|brave|bing|searxng|baidu
                "web_search_api_key": "",
                "web_search_base_url": "",
                "web_search_proxy": "",       # blank = config.json 'proxy'/env (direct tried first)
                # offload_results: when a tool result is very large, write it to a
                # workspace file and keep only a pointer + preview in context
                # (auto-adds a read_offload tool to re-read it). Off by default.
                "offload_results": False,
                # adaptive_retrieval: Adaptive-RAG-style guidance — decide FIRST
                # whether to retrieve at all (answer simple/known queries directly)
                # and pick the best source. Only affects agents with a RAG/web tool.
                "adaptive_retrieval": False,
                # groundedness_check: Self-RAG-style — grade the final answer for
                # grounding in the retrieved sources + answering the question, and
                # regenerate up to max_regen times if it falls short. Off by
                # default; one extra LLM call per grade; fail-soft.
                "groundedness_check": False, "max_regen": 1,
                # ── Extra Settings (opt-in; blank/0 = unchanged & byte-identical) ──
                "max_rpm": 0,              # requests/min rate limit (0 = unlimited)
                "stage_retries": 0,        # re-run this stage on a transient error
                "max_budget_usd": 0,       # abort when est. cost hits cap (needs LLM prices)
                # on_budget: what to do when THIS stage hits any budget cap (wall-clock/
                # iterations/tool-calls/cost): "continue" (default — pass the [budget]
                # note downstream), "stop" (end the whole run), "retry" (re-run the
                # stage up to stage_retries times, then stop).
                "on_budget": "continue",
                "final_schema": "",        # force FINAL answer to a JSON Schema (JSON string)
                "final_schema_retries": 2,  # bounded re-asks on schema mismatch
                # compact_threshold (%): when the ENTRY agent's estimated context
                # reaches this % of its usable window (from the LLM's context_capacity),
                # older turns are compacted. Default 85; only the entry agent compacts.
                "compact_threshold": 85,
                **_HITL, **DEFAULT_BUDGETS}
    if kind == "workerpool":
        # identical workers sharing prompt/tools/LLM; max_workers run in
        # parallel over the subtasks handed to the pool. reads/writes: see agent.
        return {"role": "worker", "max_workers": 4,
                "reads": [], "writes": [], "require_writes": False,
                "guardrails": {}, "enable_todos": False,
                "max_rpm": 0, "stage_retries": 0,   # Extra Settings (see agent)
                **_HITL, **DEFAULT_BUDGETS}
    if kind == "router":
        # an LLM picks one of the router's outgoing agent stages per input.
        return {"role": "router", "instructions": "",
                # Extra Settings (opt-in; blank = unchanged & byte-identical):
                "default_route": "",       # tie-break branch when the reply is ambiguous
                "routing_provider": "", "routing_model": "",   # cheaper LLM just for routing
                "routing_base_url": "", "routing_api_key": "",
                **DEFAULT_BUDGETS}
    if kind == "eval":
        # an eval set. Linked to one agent → tests that agent alone; standalone
        # (no link) → tests the whole harness. Each case: input + one of
        # expected_output / expected_regex / judge (LLM-graded criterion).
        return {"cases": []}
    if kind == "hitl":
        # a human checkpoint between stages. prompt = the question shown.
        # on_reject: "stop" ends the run; "revise" re-runs the upstream agent
        # with the human's feedback (bounded). No LLM/budgets — it just pauses.
        # TWO shapes, chosen by how many outgoing links you draw:
        #   1 outgoing  → GATE mode (today): approve/edit/reject before the next
        #                 stage runs; spliced onto the downstream agent.
        #   2+ outgoing → ROUTE mode: a human-driven branch (mirror of a Router) —
        #                 the reviewer picks WHICH successor runs next. Branches are
        #                 the outgoing targets by name; default_route is the tie-break
        #                 /timeout branch.
        return {"prompt": "Review the output before continuing.",
                "on_reject": "stop",
                # Extra Settings (opt-in; blank/default = unchanged & byte-identical):
                "decisions": ["approve", "edit", "reject"],   # which the reviewer may take
                "timeout": 0,               # auto-decide after N s unattended (0 = wait)
                "on_timeout": "approve",    # approve | reject on timeout (GATE mode)
                "default_route": ""}        # ROUTE mode: branch taken on timeout / tie-break
    if kind == "llm":
        # temperature/top_p blank = provider default. response_format:
        #   text        — no constraint
        #   json_object — valid JSON, no schema (OpenAI-family only)
        #   json_schema — output conforms to response_schema; translated per
        #                 vendor (OpenAI response_format vs Anthropic
        #                 output_config.format). response_schema is a JSON
        #                 object string, required for json_schema.
        # extra = JSON object of any other API params (seed, stop, top_k, ...).
        # Note: temperature/top_p are rejected by Anthropic Opus 4.x.
        # parallel_tools: when the model emits 2+ tool calls in one turn, run
        # them concurrently (only the parallel-safe ones; writes stay serial).
        # Default off = simple sequential execution. The agent's primary LLM
        # decides this for the whole agent.
        # request_timeout_s: hard cap (seconds) on EACH API call so a stalled
        # endpoint can't hang the agent forever; blank = SDK default (~10 min).
        # vision: this model accepts image input — when on, the generated chat
        # (desktop + web) lets the user attach/drop images, sent to the agent
        # that receives the user's input. Leave off for text-only models.
        return {
            "provider": "siliconflow",
            "model": "deepseek-ai/DeepSeek-V4-Flash",
            "api_key": "",
            "base_url": "https://api.siliconflow.cn/v1",
            "temperature": "",
            "top_p": "",
            "response_format": "text",
            "response_schema": "",
            "parallel_tools": False,
            "request_timeout_s": 120,
            # Optional HTTP/HTTPS proxy for this LLM's API calls, e.g.
            # "http://10.144.1.10:8080". Blank = use the HTTP(S)_PROXY env vars (or a
            # direct connection). Set this when the agent runs without inheriting a
            # corporate proxy and the API host is only reachable through it.
            "proxy": "",
            "vision": False,
            # context_capacity: this model's context window in tokens. When set
            # (>0), the agent it powers compacts older conversation to stay under
            # it (only the main/entry agent does this). 0/blank = no context
            # control at all (rely on the provider's own limit).
            "context_capacity": 0,
            # ── Extra Settings (advanced sampling; blank = provider default) ──
            # These fold into the per-call API params (via the same path as `extra`),
            # so a blank field changes nothing and generated configs stay identical.
            # Provider-specific: e.g. top_k / reasoning_effort aren't accepted by
            # every provider — opt in only when your model supports them.
            "stop": "",                 # stop sequence(s): one per line
            "seed": "",                 # deterministic sampling seed (int)
            "presence_penalty": "",     # float
            "frequency_penalty": "",    # float
            "top_k": "",                # int (providers that support it)
            "reasoning_effort": "",     # "" | minimal | low | medium | high
            "max_retries": "",          # blank = framework default (2); "0" = no retry
            "tool_choice": "auto",      # auto | any | none | specific (auto = omit)
            "tool_choice_name": "",     # function name; only for tool_choice=specific
            # Optional per-1M-token prices ($) — enable an agent's max_budget_usd cap.
            # Blank = no cost tracking (config stays byte-identical).
            "price_in_per_1m": "",
            "price_out_per_1m": "",
            "extra": "",                # raw JSON escape hatch (overrides the above)
        }
        # NOTE: fallback priority is NOT stored here. The same LLM can feed several
        # agents with a DIFFERENT priority each, so priority lives on the LINK
        # (edge.props["priority"]), not the node. See renumber_llm_fallbacks / the
        # llm→agent branch of open_edge_config_dialog.
    if kind == "tool":
        # a Tools node aggregates several tool-library files; the agent it links
        # to gets every function in them. (Legacy single-"file" nodes still load
        # — see tool_files().) tool_props = optional per-FUNCTION Extra Settings,
        # {func_name: {return_direct, error_mode, error_retries, risk, description}};
        # empty = unchanged & byte-identical.
        return {"files": [], "tool_props": {}}
    if kind == "skill":
        # a Skills node aggregates several named guidance snippets; each is
        # appended to the agent's system prompt. (Legacy single-"text" nodes
        # still load — see skill_items().) The generated GUI can manage these
        # at runtime (add / edit / remove).
        return {"skills": []}
    if kind == "prompt":
        # role: single | planner | worker | critic — each has a template file
        # under templates/; empty text falls back to the role's template.
        return {"role": "single", "text": ""}
    if kind == "rag":
        # Basic: docs_dir + chunk_chars + top_k + description (routing hint —
        # becomes this KB's tool description AND a line in the agent's
        # "Knowledge bases" prompt tail). Advanced pipeline knobs default to the
        # plain offline BM25 baseline (chunk_strategy=fixed, chunk_overlap=0 =>
        # size//8, retrieval=bm25, recall_n=0 => top_k, no MMR/rerank/rewrite,
        # vector_db=memory). The embedding default is FREE + NO API KEY: a small
        # local model (BAAI/bge-small-zh-v1.5, ~90MB, run via fastembed /
        # sentence-transformers) — used only if the user switches retrieval to
        # dense/hybrid, and it degrades to BM25 when no local lib is installed.
        return {"docs_dir": "", "chunk_chars": 800, "top_k": 4, "description": "",
                "chunk_strategy": "fixed", "chunk_overlap": 0,
                # small-to-big / parent-child retrieval (§4.3): "chunk" indexes and
                # returns the same piece; "parent_child" indexes small children but
                # returns their bigger parent block for fuller context.
                "retrieval_granularity": "chunk", "parent_chunk_chars": 2400,
                "retrieval_algorithm": "bm25", "recall_n": 0,
                "mmr": False, "mmr_lambda": 0.5,
                "rerank_mode": "none", "rerank_model": "",
                # grade_docs: an LLM relevance gate (Self-RAG style) that drops
                # clearly-irrelevant chunks before they reach the agent. Opt-in;
                # one extra LLM call per search; no-op on failure.
                "grade_docs": False,
                # corrective: CRAG-style local correction — a search that finds
                # nothing rewrites the query (LLM) and retries, up to
                # corrective_max_rewrites extra passes. Opt-in; offline-safe (a
                # failed rewrite just stops the loop).
                "corrective": False, "corrective_max_rewrites": 2,
                "query_transform": "none",
                # Extra Settings (opt-in; blank/0/none = unchanged & byte-identical):
                "score_threshold": 0.0,    # drop chunks below this score (dense/cross-enc only)
                "metadata_filter": "",     # source glob(s) to restrict retrieval, e.g. *.md
                "multi_query_n": 3,        # query_transform=multi_query: number of variants
                "embed_provider": "local",
                "embed_model": "BAAI/bge-small-zh-v1.5",
                "embed_base_url": "", "embed_api_key": "",
                "normalize": True, "vector_db": "memory",
                # qdrant vector store: blank URL = embedded on-disk (./rag_qdrant,
                # no server); set a URL (+ optional key) to use a remote Qdrant.
                "qdrant_url": "", "qdrant_api_key": "",
                # evict_used: when on, a newer document search elides earlier
                # search results in the same turn (stale chunks stop eating tokens).
                "evict_used": False}
    if kind == "webserver":
        return {"host": "127.0.0.1", "port": 8765, "auth_token": "",
                # Extra Settings (opt-in; blank/0/False = unchanged):
                "auto_allow_tools": False,   # headless: auto-approve tools (no HITL prompt)
                # autostart: gui.py starts the embedded server on launch (headless
                # server.py ALWAYS listens on start, so this only affects the GUI app).
                "autostart": False,
                "tls_cert": "", "tls_key": "",   # wss:// (both required together)
                "allowed_origins": [],       # CORS origin allow-list ([] = any)
                "max_connections": 0}        # 0 = unlimited
    if kind == "memory":
        # a persistent cross-run memory store (Reflexion-style) linked to an agent:
        # gives it remember(content,tags) + recall(query,k) tools backed by a JSON
        # store + BM25 retrieval (reuses the RAG ranker). description = the routing
        # hint; top_k = how many memories recall returns by default.
        return {"description": "", "top_k": 5}
    if kind == "mcp":
        # transport: stdio | streamable_http | sse.
        # stdio uses command+args; http/sse use url (+ verify_tls for https).
        return {"transport": "streamable_http", "command": "", "args": "",
                "url": "http://127.0.0.1:8000/mcp", "verify_tls": True,
                # Extra Settings (opt-in; blank/0 = unchanged & byte-identical):
                "allow_tools": "", "deny_tools": "",   # comma-sep server tool names
                "connect_timeout": 0, "call_timeout": 0,   # seconds (0 = 30 / 60 default)
                "env": "", "headers": ""}              # KEY=val (stdio) / Header: val (http)
    if kind == "gui":
        # the PySide6 desktop GUI. Linking it to the entry agent turns on gui.py
        # generation. custom_gui (optional): a user-authored gui.py SOURCE emitted
        # in place of the built-in window — it drives the agent via `import agent as
        # core` / core.run(...) (see prototype/custom_gui/CONTRACT.md). Blank = the
        # standard chat window. The source text travels in graph.json + the .mta.
        return {"custom_gui": ""}
    if kind == "schedule":
        # linking it to an agent emits scheduler.py — an AMBIENT runner that calls
        # that agent every `every_seconds` with `initial_task` (no user prompt).
        # MANY schedule nodes on one graph run as INDEPENDENT concurrent jobs, each its
        # own task/period/phase/session. Link to the ENTRY agent to drive the whole
        # graph (B); link DIFFERENT schedules to DIFFERENT agents to drive each
        # separately (A — the run starts at the linked agent). offset_seconds = initial delay before the first
        # tick (stagger jobs so they don't all fire at once); session_id blank = isolate
        # this job by its node name; max_runs 0 = forever; run_at_start fires the first
        # tick immediately (after offset) instead of after a full interval.
        # `mode` picks ONE scheduling strategy (mutually exclusive — the others' knobs
        # are ignored/greyed in the dialog):
        #   "interval" — every `every_seconds` (+ offset / run_at_start).  [default]
        #   "daily"    — every day at local time `at` ("HH:MM" / "HH:MM:SS").
        #   "once"     — a single run at absolute local `start_at`
        #                ("YYYY-MM-DD HH:MM[:SS]").
        return {"mode": "interval",
                "every_seconds": 3600, "offset_seconds": 0, "run_at_start": True,
                "at": "", "start_at": "",
                "initial_task": "", "session_id": "", "max_runs": 0}
    if kind == "while":
        # A loop guard: while `condition` (a predicate over shared state) holds,
        # control goes to `body` (which must link back here to re-check); when it
        # fails, control goes to the OTHER outgoing link (the exit). `body` is the
        # name of the loop-body successor. Compiles to a condition table:
        # [(condition, body), (else, exit)]. Bound by the graph's recursion_limit.
        return {"condition": "", "body": "",
                "max_iterations": 0}   # per-loop cap (0 = only the graph recursion limit)
    if kind == "foreach":
        # Map-over-list (parallel): run `body` (the loop-body successor) ONCE PER
        # ITEM of the shared-state list field `over`, the item copies running
        # concurrently (isolated per-item state fork, like a dynamic fan-out).
        # Each item is passed to the body BOTH as its input AND, when `item_var`
        # is set, written to that shared-state field. The body must link BACK here
        # (marks the region / where each item's run stops); the OTHER outgoing link
        # is the exit, taken once after all items finish. `result_field` (optional)
        # = a shared-state list each item's OUTPUT is appended to; `merge` = how the
        # item outputs combine into the carried value (concat | first | last |
        # state_only | vote). max_parallel caps concurrency (0 = unbounded).
        return {"over": "", "body": "", "item_var": "", "result_field": "",
                "merge": "concat", "max_parallel": 0}
    if kind == "condition":
        # Deterministic If/Else on shared state (no LLM). branches: an ordered
        # list of {to, expr} — first branch whose expr is true wins; an empty
        # expr marks the else/fallback. `to` is a successor stage name.
        return {"branches": []}
    if kind == "setstate":
        # Deterministic shared-state write (no LLM). assignments: a list of
        # {field, value}; each value is applied through that field's reducer.
        return {"assignments": []}
    if kind == "guardrail":
        # Inline content gate (no LLM): scans the content flowing through it.
        # checks = which deterministic scans to run; on_trip = "redact" (scrub
        # secrets/PII in place) or "block" (stop the run). injection always blocks.
        return {"checks": {"secret": True, "pii": False, "injection": False},
                "on_trip": "redact",
                # Extra Settings (opt-in; blank = unchanged & byte-identical):
                "patterns": [],      # custom regexes to redact/block
                "keywords": [],      # literal terms to redact/block (case-insensitive)
                "max_length": 0}     # truncate content over N chars (0 = no cap)
    if kind == "end":
        # Terminal sink: no config. When the graph flow reaches an End node the
        # run finishes early and returns whatever output was carried into it.
        return {}
    if kind == "fanout":
        # Parallel fan-out: run its N branch successors, then reconverge at the
        # paired join. max_parallel caps concurrency (0 = unbounded); ignored while
        # execution is still sequential (v1) and honored once branches run on threads.
        return {"max_parallel": 0}
    if kind == "join":
        # Barrier that reconverges a fan-out's branches. merge = how the branch
        # OUTPUT strings combine into the carried value: concat | first | last |
        # state_only (branches communicated via shared state; carried is dropped).
        return {"merge": "concat"}
    if kind == "subgraph":
        # Embed another graph as a reusable component. graph_json = the child's
        # to_dict() (embedded so the parent stays self-contained); graph_name = a
        # label for messages / recursion guard. Flattened into the parent at
        # generation time by expand_subgraphs (child nodes namespaced + spliced in).
        return {"graph_name": "", "graph_json": {}}
    raise ValueError(kind)


@dataclass
class Node:
    id: str
    kind: str
    name: str
    x: int
    y: int
    props: dict = field(default_factory=dict)


@dataclass
class Edge:
    src: str
    dst: str
    # Optional per-link config. For an agent→agent link this holds a data-handoff
    # "contract": props["contract"] = [{name, type, description}, ...] describing
    # the fields the upstream produces = the downstream consumes (see
    # contract_fields). Defaults empty; old graphs (no props key) load fine.
    props: dict = field(default_factory=dict)


def contract_fields(edge, type_defs: dict | None = None) -> list[dict]:
    """Validated data-handoff contract on an edge: a list of
    {name, type, description} dicts declared on edge.props['contract']. Entries
    without a valid identifier name (or duplicates) are dropped. A type is kept
    when it's native OR a declared custom type / list[Name] (pass `type_defs` from
    the graph); anything else coerces to 'str'. Empty list when no contract."""
    raw = (getattr(edge, "props", None) or {}).get("contract") or []
    type_defs = type_defs or {}
    out, seen = [], set()
    for f in raw:
        if not isinstance(f, dict):
            continue
        name = str(f.get("name", "")).strip()
        if not name.isidentifier() or name in seen:
            continue
        seen.add(name)
        t = f.get("type")
        ftype = t if (t in STATE_TYPES or _is_declared_type(t, type_defs)) else "str"
        out.append({"name": name, "type": ftype,
                    "description": str(f.get("description", "")).strip()})
    return out


class Graph:
    def __init__(self):
        self.nodes: dict[str, Node] = {}
        self.edges: list[Edge] = []
        # Graph-level shared-state schema: a list of
        # {name,type,reducer,default,description} dicts. Raw here; validated via
        # state_fields(). Empty by default.
        self.state_schema: list[dict] = []
        # Custom/nested state types (Approach A). {Name: {schema, merge, merge_key,
        # description}}. Raw here; validated via validate_type_defs(). Empty =>
        # only native types, generated output byte-identical to before.
        self.type_defs: dict = {}
        # Graph-mode loop guard: max stage transitions before a run errors out.
        # 0 = auto (codegen derives a bound from the graph size).
        self.recursion_limit: int = 0
        # Optional whole-run wall-clock deadline (seconds; 0 = none). Checked between
        # react steps + graph hops; on exceed the WHOLE run stops with a [budget]
        # result. Distinct from a stage's own max_wall_clock_s (per-stage soft cap).
        self.run_wall_clock_s: int = 0
        # Where the generated agent stores memory (chat sessions) + checkpoints:
        # {"backend": "disk"|"sqlite"|"postgres", "sqlite_path": ..., "dsn": ...}.
        # Empty/absent => disk (the default, unchanged file layout).
        self.storage: dict = {}
        self._counter = itertools.count(1)

    # ── nodes ───────────────────────────────────────────────────────────────
    def new_node(self, kind: str, x: int, y: int) -> Node:
        n = next(self._counter)
        node = Node(
            id=f"{kind}_{n}", kind=kind, name=f"{kind}_{n}",
            x=x, y=y, props=default_props(kind),
        )
        self.nodes[node.id] = node
        return node

    def remove_node(self, node_id: str) -> None:
        self.nodes.pop(node_id, None)
        self.edges = [e for e in self.edges if node_id not in (e.src, e.dst)]

    # ── edges ───────────────────────────────────────────────────────────────
    def add_edge(self, src_id: str, dst_id: str) -> str | None:
        """Create a link; returns an error message, or None on success."""
        if src_id == dst_id:
            return "A module cannot link to itself."
        src, dst = self.nodes[src_id], self.nodes[dst_id]
        if (src.kind, dst.kind) not in ALLOWED_EDGES:
            return (
                f"Cannot link {src.kind} → {dst.kind}. Allowed: "
                "llm/tool/skill/prompt → agent, agent → agent."
            )
        if any(e.src == src_id and e.dst == dst_id for e in self.edges):
            return "These modules are already linked."
        if src.kind in SINGLETON_INPUTS and any(
            self.nodes[e.src].kind == src.kind and e.dst == dst_id for e in self.edges
        ):
            return f"'{dst.name}' already has a {src.kind} — delete that link first."
        if src.kind == "tool":
            new = set(tool_files(src))
            for other in self.inputs_of(dst_id, "tool"):
                if other.id == src_id:
                    continue
                dup = new & set(tool_files(other))
                if dup:
                    return (f"'{dst.name}' already has tool file(s) "
                            f"{', '.join(sorted(dup))} via '{other.name}' — a "
                            "duplicate would register the same function twice.")
        self.edges.append(Edge(src_id, dst_id))
        return None

    def link_warning(self, src_id: str, dst_id: str) -> str | None:
        """Non-blocking duplicate-content warning for a just-created link."""
        src, dst = self.nodes[src_id], self.nodes[dst_id]
        if src.kind == "llm":
            for other in self.inputs_of(dst_id, "llm"):
                if other.id != src_id and all(
                    (other.props.get(k) or "") == (src.props.get(k) or "")
                    for k in ("provider", "model", "base_url")
                ):
                    return (f"'{dst.name}' now links two identical LLMs "
                            f"({src.props.get('model')}) — the duplicate adds "
                            "no real fallback (same provider/model). Configure "
                            "a different model, or delete one link.")
        if src.kind == "skill":
            new = {s["text"] for s in skill_items(src)}
            for other in self.inputs_of(dst_id, "skill"):
                if other.id == src_id:
                    continue
                if new & {s["text"] for s in skill_items(other)}:
                    return (f"'{dst.name}' already has a skill with identical "
                            f"text (via '{other.name}') — it would be repeated "
                            "in the system prompt.")
        return None

    def remove_edge(self, edge: Edge) -> None:
        self.edges = [e for e in self.edges if e is not edge]

    # ── queries ─────────────────────────────────────────────────────────────
    def inputs_of(self, agent_id: str, kind: str) -> list[Node]:
        # `e.src in self.nodes` guards a dangling edge (endpoint node missing, e.g.
        # from a hand-edited/corrupt graph.json) so this never raises KeyError.
        pairs = [
            (e, self.nodes[e.src])
            for e in self.edges
            if e.dst == agent_id and e.src in self.nodes
            and self.nodes[e.src].kind == kind
        ]
        if kind == "llm":
            # Order LLMs by this AGENT's per-link fallback priority (1 = primary),
            # stored on the EDGE (a shared LLM can be primary for one agent and a
            # fallback for another). A stable sort keeps unset (0) links in edge
            # order and after the numbered ones, so a graph that never set a
            # priority is byte-identical to before.
            pairs.sort(key=lambda p: (p[0].props.get("priority") or 0) or float("inf"))
        return [n for _, n in pairs]

    def llm_edges_of(self, agent_id: str) -> list["Edge"]:
        """The llm→agent links feeding this agent, in fallback-priority order
        (mirrors inputs_of(agent,'llm') but returns the EDGES that hold the
        priority — used to render/edit the number on the link)."""
        edges = [e for e in self.edges
                 if e.dst == agent_id and e.src in self.nodes
                 and self.nodes[e.src].kind == "llm"]
        edges.sort(key=lambda e: (e.props.get("priority") or 0) or float("inf"))
        return edges

    def renumber_llm_fallbacks(self) -> bool:
        """Give each agent's incoming LLM LINKS a contiguous fallback priority
        1..N (1 = primary), written onto the EDGES so the number stays in sync
        after connect/delete and can be shown on the link. Priority is per-link,
        so the same LLM feeding two agents is numbered independently for each.
        Returns True if anything changed (so a caller can skip dirtying on a
        no-op)."""
        changed = False
        for a in self.agents():
            for rank, e in enumerate(self.llm_edges_of(a.id), 1):
                if e.props.get("priority") != rank:
                    e.props["priority"] = rank
                    changed = True
        return changed

    def agents(self) -> list[Node]:
        """Pipeline-stage nodes: plain agents and worker pools."""
        return [n for n in self.nodes.values() if n.kind in AGENT_KINDS]

    def agent_successors(self, agent_id: str) -> list[str]:
        return [
            e.dst for e in self.edges
            if e.src == agent_id and e.dst in self.nodes
            and self.nodes[e.dst].kind in AGENT_KINDS
        ]

    def flow_successors(self, node_id: str) -> list[str]:
        """Successor stages for the graph-mode walk: agents, control nodes
        (condition / setstate / …) AND a route-mode HITL (a human-driven branch
        stage), unlike agent_successors (agents only). A 1-out HITL gate is spliced
        out before any codegen walk, so it never appears here in generation."""
        return [
            e.dst for e in self.edges
            if e.src == node_id and e.dst in self.nodes
            and self.nodes[e.dst].kind in FLOW_KINDS
        ]

    # ── persistence ─────────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "nodes": [asdict(n) for n in self.nodes.values()],
            "edges": [asdict(e) for e in self.edges],
            "state_schema": [dict(f) for f in self.state_schema],
            "type_defs": {k: dict(v) for k, v in self.type_defs.items()},
            "recursion_limit": self.recursion_limit,
            "run_wall_clock_s": self.run_wall_clock_s,
            "storage": dict(self.storage),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Graph":
        g = cls()
        for nd in data.get("nodes", []):
            g.nodes[nd["id"]] = Node(**nd)
        for ed in data.get("edges", []):
            e = Edge(**ed)
            # Drop a dangling edge (endpoint node absent — hand-edited/corrupt
            # graph.json, a merge conflict, partial data) so it can't survive to
            # crash analyze()/generate later; the canvas already never draws it.
            if e.src in g.nodes and e.dst in g.nodes:
                g.edges.append(e)
        g.state_schema = [dict(f) for f in (data.get("state_schema") or [])]
        g.type_defs = {k: dict(v) for k, v in (data.get("type_defs") or {}).items()
                       if isinstance(v, dict)}
        g.recursion_limit = int(data.get("recursion_limit") or 0)
        g.run_wall_clock_s = int(data.get("run_wall_clock_s") or 0)
        g.storage = dict(data.get("storage") or {})
        # continue numbering above any existing "<kind>_<n>" ids
        top = 0
        for nid in g.nodes:
            tail = nid.rsplit("_", 1)[-1]
            if tail.isdigit():
                top = max(top, int(tail))
        g._counter = itertools.count(top + 1)
        return g

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)

    @classmethod
    def load(cls, path: str) -> "Graph":
        with open(path, encoding="utf-8") as f:
            return cls.from_dict(json.load(f))


# ── .mta bundle: a self-contained, portable graph (a zip) ───────────────────
# A .mta file holds graph.json (full node/edge info — prompts and skills are
# already inline in node props) PLUS every referenced tool .py source, and
# readable copies of the prompts/skills. Bundling the tool sources is what makes
# a graph portable: graph.json references tools by FILENAME and the generator
# inlines them from tools/ at build time, so a machine without those files
# couldn't build the agent — load_mta restores them.
MTA_FORMAT_VERSION = 1
_MTA_MISSING = "# [missing at export]"


# ── subgraph flattening (design-time graph reuse) ────────────────────────────────
def _flow_entry_id(g: "Graph"):
    """The entry point of a (sub)graph = a flow node with no incoming FLOW edge;
    prefer an agent. Resources (llm/tool/…) don't count as incoming flow."""
    flow = [n.id for n in g.nodes.values() if n.kind in FLOW_KINDS]
    has_in = {e.dst for e in g.edges
              if e.src in g.nodes and e.dst in g.nodes
              and g.nodes[e.src].kind in FLOW_KINDS and g.nodes[e.dst].kind in FLOW_KINDS}
    roots = [i for i in flow if i not in has_in]
    agent_roots = [i for i in roots if g.nodes[i].kind in AGENT_KINDS]
    picks = agent_roots or roots or flow
    return picks[0] if picks else None


def _exit_ids(g: "Graph"):
    """Where a (sub)graph hands control onward: the predecessors of its End node(s)
    (which are then dropped), else the flow sinks (no outgoing flow edge).
    Returns (exit_node_ids, end_node_ids_to_drop)."""
    end_ids = [n.id for n in g.nodes.values() if n.kind == "end"]
    if end_ids:
        exits = [e.src for e in g.edges
                 if e.dst in end_ids and e.src in g.nodes and e.src not in end_ids]
        return list(dict.fromkeys(exits)), end_ids
    out = {e.src for e in g.edges
           if e.src in g.nodes and e.dst in g.nodes
           and g.nodes[e.src].kind in FLOW_KINDS and g.nodes[e.dst].kind in FLOW_KINDS}
    sinks = [n.id for n in g.nodes.values() if n.kind in FLOW_KINDS and n.id not in out]
    return sinks, []


def expand_subgraphs(graph: "Graph", _seen: tuple = ()) -> "Graph":
    """Return a FLAT copy of `graph` with every `subgraph` node replaced by the child
    graph it embeds (props['graph_json']). Child nodes are namespaced ('<sub>/<name>')
    and spliced in: the subgraph node's incoming edges feed the child's entry, and the
    child's exits feed the subgraph node's successors (the child's End becomes a pass-
    through). Nested subgraphs are flattened first; a recursive include raises. Graphs
    with no subgraph node are returned unchanged (byte-identical downstream)."""
    if not any(n.kind == "subgraph" for n in graph.nodes.values()):
        return graph
    g = Graph.from_dict(graph.to_dict())          # work on a deep copy
    while True:
        sub = next((n for n in g.nodes.values() if n.kind == "subgraph"), None)
        if sub is None:
            break
        name = (sub.props.get("graph_name") or sub.name or "subgraph")
        if name in _seen:
            raise ValueError("Recursive subgraph include of '%s'." % name)
        cj = sub.props.get("graph_json")
        if not isinstance(cj, dict) or not cj.get("nodes"):
            raise ValueError("Subgraph node '%s' has no embedded graph." % sub.name)
        child = expand_subgraphs(Graph.from_dict(cj), _seen + (name,))
        c_entry = _flow_entry_id(child)
        c_exits, c_ends = _exit_ids(child)
        if c_entry is None:
            raise ValueError("Subgraph '%s' has no entry node." % name)
        prefix = sub.name or name
        idmap = {}
        for cn in child.nodes.values():          # copy child nodes with fresh ids
            nn = g.new_node(cn.kind, cn.x, cn.y)
            nn.props = dict(cn.props)
            nn.name = "%s/%s" % (prefix, cn.name)
            idmap[cn.id] = nn.id
        for ce in child.edges:                   # copy child edges (remapped)
            if ce.src in idmap and ce.dst in idmap:
                g.edges.append(Edge(idmap[ce.src], idmap[ce.dst], dict(ce.props or {})))
        entry_new = idmap[c_entry]
        exits_new = [idmap[x] for x in c_exits if x in idmap]
        for e in [e for e in g.edges if e.dst == sub.id]:   # parent → child entry
            e.dst = entry_new
        for e in [e for e in g.edges if e.src == sub.id]:   # child exits → parent succ
            for x in exits_new:
                g.edges.append(Edge(x, e.dst, dict(e.props or {})))
        g.edges = [e for e in g.edges if sub.id not in (e.src, e.dst)]
        for endc in c_ends:                      # child End becomes a pass-through
            if idmap.get(endc):
                g.remove_node(idmap[endc])
        g.remove_node(sub.id)
        existing = {f["name"] for f in g.state_schema}      # merge child shared state
        for f in child.state_schema:
            if not f.get("builtin") and f["name"] not in existing:
                g.state_schema.append(dict(f)); existing.add(f["name"])
        for k, v in (getattr(child, "type_defs", {}) or {}).items():
            g.type_defs.setdefault(k, v)
    return g


def _mta_safe(s: str) -> str:
    return re.sub(r"[^\w.-]", "_", s or "") or "node"


def save_mta(graph: "Graph", path: str, tools_dir: str) -> None:
    """Write `graph` to a self-contained .mta bundle (zip)."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(
            {"format": "metaagent-graph", "version": MTA_FORMAT_VERSION}, indent=2))
        z.writestr("graph.json",
                   json.dumps(graph.to_dict(), indent=2, ensure_ascii=False))
        seen = set()
        for n in graph.nodes.values():
            if n.kind == "tool":
                for fname in tool_files(n):
                    if fname in seen:
                        continue
                    seen.add(fname)
                    try:
                        with open(os.path.join(tools_dir, fname),
                                  encoding="utf-8") as f:
                            z.writestr("tools/" + fname, f.read())
                    except OSError:
                        z.writestr("tools/" + fname,
                                   _MTA_MISSING + " " + fname + "\n")
            elif n.kind == "prompt":          # readable copy (also inline in JSON)
                z.writestr("prompts/" + _mta_safe(n.id) + ".txt",
                           n.props.get("text") or "")
            elif n.kind == "skill":           # readable copy (also inline in JSON)
                z.writestr("skills/" + _mta_safe(n.id) + ".json",
                           json.dumps(skill_items(n), indent=2, ensure_ascii=False))


def load_mta(path: str, tools_dir: str):
    """Load a Graph from a .mta bundle and restore its bundled tool .py files into
    `tools_dir`. Returns (graph, info) where info = {restored, conflicts, missing}
    (tool-file name lists). An existing tool file that DIFFERS from the bundle is
    kept (never overwritten) and reported as a conflict; the graph then uses the
    existing version."""
    info = {"restored": [], "conflicts": [], "missing": []}
    with zipfile.ZipFile(path) as z:
        graph = Graph.from_dict(json.loads(z.read("graph.json").decode("utf-8")))
        os.makedirs(tools_dir, exist_ok=True)
        for name in z.namelist():
            if not name.startswith("tools/") or not name.endswith(".py"):
                continue
            fname = os.path.basename(name)
            if not fname:
                continue
            content = z.read(name).decode("utf-8")
            if content.lstrip().startswith(_MTA_MISSING):
                info["missing"].append(fname)
                continue
            dest = os.path.join(tools_dir, fname)
            if os.path.isfile(dest):
                try:
                    with open(dest, encoding="utf-8") as f:
                        if f.read() == content:
                            continue          # already present, identical
                except OSError:
                    pass
                info["conflicts"].append(fname)
                continue
            with open(dest, "w", encoding="utf-8") as f:
                f.write(content)
            info["restored"].append(fname)
    return graph, info
