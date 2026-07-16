"""Generate a standalone (multi-)agent script from a canvas Graph.

Pipeline semantics:
- agent → agent links define execution order; each agent receives the original
  task plus the previous agent's output.
- One link pointing BACK to an earlier agent is allowed: it becomes a bounded
  revise loop (the classic critic pattern). The looping agent signals a redo
  by starting its Final Answer with "REVISE:".
- Each agent has its own LLM, tools, skills, prompt and budgets.

Output: generated_agents/<name>/ with agent.py, config.json, requirements.txt,
build.bat, README.md — same layout as the form-based designer, no LangChain
dependency.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import pprint
import re
import shlex

from app_config import GENERATED_DIR, TEMPLATES_DIR, TOOLS_DIR
from codegen import (CHECKPOINT_CODE, EVAL_CODE, GUARDRAILS_CODE, HISTORY_CODE,
                     HITL_CODE, IMAGE_CODE, MEMORY_CODE, POOL_CODE, RAG_CODE, SKILLS_CODE,
                     STORAGE_CODE, TOOL_IMPORT_STRIP_RE, TRACE_CODE,
                     WORKSPACE_CODE, _write, assert_substituted, build_bat,
                     template_markers, tool_requirements, write_evals, write_gui,
                     write_scheduler, write_server)
from graph_model import (AGENT_KINDS, CONTROL_KINDS, DEFAULT_BUDGETS, FLOW_KINDS,
                         STATE_TYPES, Graph, Node, RESERVED_STATE_NAMES,
                         contract_fields, eval_cases, expand_subgraphs,
                         is_custom_type, merge_policies_for, skill_items,
                         state_fields, tool_files, type_json_schema,
                         validate_type_defs)
from graph_codegen_templates import (  # re-exported (mechanical split)
    MCP_CODE,
    MCP_STUB,
    PIPELINE_TEMPLATE)

MAX_REVISE_ROUNDS = 2
MAX_SUPERVISOR_ROUNDS = 6

DEF_NAME_RE = re.compile(r"^def\s+(\w+)\s*\(", re.MULTILINE)


# ── readable formatting of the substituted spec literals ─────────────────────
# The generated agent embeds its specs as Python literals. repr() crushes them
# onto one line (AGENTS reached ~5000 chars, personas mangled into \n-escapes);
# these emit the SAME values, just readable. Behavior is identical (the literals
# exec to equal objects) — verification is value-equality, not byte-equality.
def _fmt_literal(obj) -> str:
    """Pretty-print a spec value as valid, multi-line Python — like repr() but
    indented, and (like repr) emitting Python True/None and preserving order."""
    return pprint.pformat(obj, sort_dicts=False, width=88)


def _py_block_string(s: str) -> str:
    """`s` as a triple-quoted Python string literal that preserves content
    EXACTLY: real newlines stay multi-line (the readability win) while EVERY
    backslash and double-quote is escaped — so no run of quotes (incl. a trailing
    `"` or an embedded `\"\"\"`) can fuse with the closing delimiter."""
    body = s.replace("\\", "\\\\").replace('"', '\\"')
    return '"""' + body + '"""'


def _custom_merge_code(type_defs) -> str:
    """Code for the @CUSTOM_MERGE_CODE@ marker (P4): build `_CUSTOM_MERGES =
    {type_name: merge_fn}` from each custom type's `merge_src`. Each source (a
    top-level `def merge(old, new)`, validated by analyze) is wrapped in a factory
    that returns its `merge`; registration is fail-soft. '_CUSTOM_MERGES = {}' when
    no type ships one — byte-identical for graphs without a custom merge."""
    defs = type_defs or {}
    lines = ["_CUSTOM_MERGES = {}"]
    for name, td in defs.items():
        src = (td.get("merge_src") or "").strip()
        if not src:
            continue
        fac = "_make_merge__" + re.sub(r"\W", "_", name)
        body = "\n".join(("    " + ln) if ln.strip() else "" for ln in src.splitlines())
        lines += ["", f"def {fac}():", body, "    return merge",
                  "try:", f"    _CUSTOM_MERGES[{name!r}] = {fac}()",
                  "except Exception:", "    pass"]
    return "\n".join(lines)


def _fmt_agents(agents_spec: dict) -> tuple:
    """Return (personas_src, agents_src): each agent's system prompt is hoisted
    into a readable `PERSONAS = {name: <triple-quoted>}` block, and AGENTS
    references PERSONAS[name]. exec's to the SAME objects as repr(agents_spec)."""
    personas, spec_copy = {}, {}
    for i, (name, spec) in enumerate(agents_spec.items()):
        s = dict(spec)
        personas[name] = s.get("system", "")
        s["system"] = "\x00P%d\x00" % i      # sentinel (NUL bytes never occur in prose)
        spec_copy[name] = s
    agents_src = _fmt_literal(spec_copy)
    for i, name in enumerate(agents_spec):    # swap each sentinel for a reference
        agents_src = agents_src.replace(repr("\x00P%d\x00" % i),
                                        "PERSONAS[%r]" % name)
    lines = ["PERSONAS = {"]
    for name, text in personas.items():
        lines.append("    %r: %s," % (name, _py_block_string(text)))
    lines.append("}")
    return "\n".join(lines), agents_src

ROLE_DEFAULT_PROMPTS = {
    "single": "You are {name}, a helpful AI agent.",
    "planner": (
        "You are {name}, the planner. Break the task into a short, numbered "
        "list of concrete steps for the next agent. Do not execute the steps."
    ),
    "worker": (
        "You are {name}, the worker. Execute the task (and the plan, if one "
        "is given) thoroughly and report the result."
    ),
    "critic": (
        "You are {name}, the critic. Review the previous agent's output "
        "against the original task. If it is acceptable, give the final, "
        "polished answer. If it must be redone, start your Final Answer with "
        "'REVISE:' followed by concrete, actionable feedback."
    ),
    "supervisor": (
        "You are {name}, the supervisor. Delegate one instruction at a time "
        "to your workers and review each result. Reply with exactly "
        "'NEXT: <instruction>' (or 'NEXT <worker_name>: <instruction>') to "
        "delegate, or 'DONE: <final answer>' when the task is complete."
    ),
    "orchestrator": (
        "You are {name}, an orchestrator. You solve the task by coordinating "
        "specialist sub-agents. Use the spawn_subagent tool to delegate a "
        "complete, self-contained subtask to one sub-agent by name: it runs in "
        "its OWN isolated context with ONLY its own tools and returns just its "
        "final result — you never see its internal steps, and it cannot see "
        "your conversation. You may spawn several in one turn to run them in "
        "parallel. Answer directly when you can; delegate only when a "
        "sub-agent's tools or expertise are needed, the subtask is large enough "
        "to isolate, or work can run in parallel. You may spawn more across "
        "several turns; once you have everything you need, synthesize their "
        "results into the final answer yourself."
    ),
}

REVISE_NOTE = (
    "\n\nIf the work must be redone, start your answer with 'REVISE:' "
    "followed by concrete feedback; otherwise give the final answer."
)

# Role → editable template file under templates/. The same files back the
# prompt-node config dialog and the no-prompt fallback.
ROLE_TEMPLATE_FILES = {
    "single": "system_prompt_template.txt",
    "planner": "prompt_planner.txt",
    "worker": "prompt_worker.txt",
    "critic": "prompt_critic.txt",
    "supervisor": "prompt_supervisor.txt",
    "orchestrator": "prompt_orchestrator.txt",
}


def role_template(role: str) -> str:
    fname = ROLE_TEMPLATE_FILES.get(role, ROLE_TEMPLATE_FILES["single"])
    try:
        with open(os.path.join(TEMPLATES_DIR, fname), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ROLE_DEFAULT_PROMPTS.get(role, ROLE_DEFAULT_PROMPTS["single"])


def _parse_llm_opts(props: dict) -> dict:
    """Normalize the LLM node's optional API params into a config dict.

    temperature / top_p: float when set, omitted when blank (provider default).
    response_format: stored when 'json_object' or 'json_schema'; 'text'/blank
    means no override. response_schema: parsed JSON object, carried only for
    json_schema. extra: a JSON object string of any other API params, merged
    verbatim (seed, stop, frequency_penalty, ...). The vendor wire shape is
    chosen at runtime (OpenAI response_format vs Anthropic output_config).
    """
    opts: dict = {}
    for key in ("temperature", "top_p"):
        raw = str(props.get(key, "")).strip()
        if raw:
            try:
                opts[key] = float(raw)
            except ValueError:
                pass
    # Hard per-call timeout (seconds). Default 120s so a stalled endpoint can't
    # hang the run; a positive node value overrides it, and an explicit 0/negative
    # means "use the SDK default" (emitted as null; the runtime guard treats a
    # falsy value as no app-level timeout). A blank/garbage field keeps 120.
    raw = str(props.get("request_timeout_s", "")).strip()
    opts["request_timeout_s"] = 120
    if raw:
        try:
            t = float(raw)
            opts["request_timeout_s"] = t if t > 0 else None
        except ValueError:
            pass
    # Optional per-LLM proxy (e.g. http://10.144.1.10:8080). Emitted only when set
    # so graphs without a proxy keep byte-identical configs; the runtime falls back
    # to a top-level config "proxy" then to HTTP(S)_PROXY env vars.
    px = str(props.get("proxy", "")).strip()
    if px:
        opts["proxy"] = px
    # Optional per-1M-token prices ($) — carried on the per-LLM cfg so the runtime
    # can accrue a cost estimate for an agent's max_budget_usd cap. Emitted only when
    # set, so price-less graphs keep byte-identical configs.
    for pk in ("price_in_per_1m", "price_out_per_1m"):
        raw = str(props.get(pk, "")).strip()
        if raw:
            try:
                opts[pk] = float(raw)
            except ValueError:
                pass
    rf = str(props.get("response_format", "")).strip()
    if rf in ("json_object", "json_schema"):
        opts["response_format"] = rf
        if rf == "json_schema":
            schema = str(props.get("response_schema", "")).strip()
            if schema:
                try:
                    parsed = json.loads(schema)
                    if isinstance(parsed, dict):
                        opts["response_schema"] = parsed
                except (json.JSONDecodeError, ValueError):
                    pass
    # Extra Settings (advanced sampling) — first-class fields fold into the same
    # per-call `extra` params the runtime merges verbatim. Blank fields are omitted
    # (so a graph that sets none keeps a byte-identical config). The raw `extra`
    # JSON escape hatch is merged LAST, so it can override a first-class field.
    merged: dict = {}
    for k in ("seed", "top_k"):
        raw = str(props.get(k, "")).strip()
        if raw:
            try:
                merged[k] = int(float(raw))
            except ValueError:
                pass
    for k in ("presence_penalty", "frequency_penalty"):
        raw = str(props.get(k, "")).strip()
        if raw:
            try:
                merged[k] = float(raw)
            except ValueError:
                pass
    effort = str(props.get("reasoning_effort", "")).strip()
    if effort:
        merged["reasoning_effort"] = effort
    stop = str(props.get("stop", "")).strip()
    if stop:
        seqs = [s for s in (line.strip() for line in stop.splitlines()) if s]
        if seqs:
            merged["stop"] = seqs[0] if len(seqs) == 1 else seqs
    extra = str(props.get("extra", "")).strip()
    if extra:
        try:
            parsed = json.loads(extra)
            if isinstance(parsed, dict):
                merged.update(parsed)          # raw JSON wins over first-class fields
        except (json.JSONDecodeError, ValueError):
            pass
    if merged:
        opts["extra"] = merged
    # max_retries: per-LLM override of MAX_LLM_RETRIES (2). "0" is meaningful (no
    # retry) so gate on the raw STRING being non-empty, not the int being truthy.
    raw = str(props.get("max_retries", "")).strip()
    if raw:
        try:
            n = int(float(raw))
            if n >= 0:
                opts["max_retries"] = n
        except ValueError:
            pass
    # tool_choice: force/allow/forbid tool use on the FIRST turn. auto/blank = omit
    # (so the model decides and the config stays byte-identical).
    tc = str(props.get("tool_choice", "")).strip().lower()
    if tc in ("any", "none"):
        opts["tool_choice"] = tc
    elif tc == "specific":
        name = str(props.get("tool_choice_name", "")).strip()
        if name:                      # a 'specific' choice with no target = no override
            opts["tool_choice"] = "specific"
            opts["tool_choice_name"] = name
    return opts


# ── human-in-the-loop checkpoint nodes ──────────────────────────────────────

def _hitl_wiring(graph: Graph, h):
    """(inbound source flow-node | None, outbound target flow-node | None) for
    a hitl node, considering only flow-node edges."""
    ins = [graph.nodes[e.src] for e in graph.edges if e.dst == h.id
           and graph.nodes[e.src].kind in FLOW_KINDS]
    # a hitl gate feeds a downstream AGENT it reviews before; an End node (a
    # terminal sink) is not a valid downstream — exclude it so a hand-edited
    # hitl→End degrades to a clear "malformed hitl" validation error instead of
    # a review gate that run_graph would silently skip at the terminal.
    outs = [graph.nodes[e.dst] for e in graph.edges if e.src == h.id
            and graph.nodes[e.dst].kind in FLOW_KINDS
            and graph.nodes[e.dst].kind != "end"]
    return ins, outs


def _hitl_route_outs(graph: Graph, h):
    """All outgoing flow-node targets of a hitl node INCLUDING End (unlike
    _hitl_wiring, which drops End). A hitl with 2+ of these is a ROUTE-mode node
    (a human-driven branch); with <=1 it is the classic GATE that gets spliced.
    Branching to End is a valid route-mode outcome (a human 'stop here')."""
    return [graph.nodes[e.dst] for e in graph.edges if e.src == h.id
            and graph.nodes[e.dst].kind in FLOW_KINDS]


def _is_hitl_route(graph: Graph, h) -> bool:
    return len(_hitl_route_outs(graph, h)) >= 2


_HITL_DECISIONS = ("approve", "edit", "reject")


def _hitl_extra(gate: dict, decisions, timeout, on_timeout) -> None:
    """Fold a HITL node's / agent's Extra Settings into a review-gate dict, ONLY when
    non-default (blank → nothing added → byte-identical). `decisions` = which of
    approve/edit/reject the reviewer may take; timeout>0 auto-decides `on_timeout`
    after N seconds unattended."""
    if decisions and set(decisions) != set(_HITL_DECISIONS):
        gate["decisions"] = [d for d in _HITL_DECISIONS if d in decisions] or ["approve"]
    to = int(timeout or 0)
    if to > 0:
        gate["timeout"] = to
        gate["on_timeout"] = on_timeout or "approve"


def _validate_hitl(graph: Graph) -> list[str]:
    """A v1 HITL node sits BETWEEN two plain agents/pools: exactly one inbound
    and one outbound, neither side a router or another hitl. To gate an agent's
    start instead, use its 'review before run' property. Returns error strings."""
    errors: list[str] = []
    for h in (n for n in graph.nodes.values() if n.kind == "hitl"):
        if _is_hitl_route(graph, h):
            # ROUTE MODE (2+ outgoing): a human-driven branch, mirror of a Router.
            # Needs an inbound; branches are the outgoing targets (agents / End /
            # control nodes). No router/hitl NEIGHBOUR (keep v1 free of nested-HITL
            # + router-in-a-branch coupling); default_route is validated at emit time
            # (dropped if it names no successor, like a Router's).
            ins = [graph.nodes[e.src] for e in graph.edges if e.dst == h.id
                   and graph.nodes[e.src].kind in FLOW_KINDS]
            if len(ins) < 1:
                errors.append(f"Routing HITL '{h.name}' needs an incoming link "
                              "(the stage whose output the human reviews).")
            for nb in ins + _hitl_route_outs(graph, h):
                if nb.kind == "hitl":
                    errors.append(f"HITL '{h.name}' can't connect directly to "
                                  f"another HITL ('{nb.name}').")
            if h.props.get("on_reject", "stop") not in ("stop", "revise"):
                errors.append(f"HITL '{h.name}' has an invalid on_reject "
                              "(use 'stop' or 'revise').")
            continue
        ins, outs = _hitl_wiring(graph, h)
        if len(ins) != 1 or len(outs) != 1:
            errors.append(
                f"HITL '{h.name}' must sit between two agents "
                "(agent → HITL → agent). To pause before an agent's start, "
                "enable 'review before this stage runs' on the agent instead.")
        for nb in ins + outs:
            if nb.kind == "router":
                errors.append(f"HITL '{h.name}' can't connect to a router "
                              f"('{nb.name}') in this version.")
            if nb.kind == "hitl":
                errors.append(f"HITL '{h.name}' can't connect directly to "
                              f"another HITL ('{nb.name}').")
        if h.props.get("on_reject", "stop") not in ("stop", "revise"):
            errors.append(f"HITL '{h.name}' has an invalid on_reject "
                          "(use 'stop' or 'revise').")
    return errors


def _splice_hitl(graph: Graph) -> tuple[Graph, dict]:
    """Return (effective graph, gates). The effective graph has every hitl node
    removed and replaced by a direct agent→agent edge, so the existing pipeline
    analysis runs unchanged. gates maps the *downstream* agent name → a review
    gate {node, source, prompt, on_reject} that the runtime applies before that
    agent runs. A malformed hitl node is left in place (validation reports it)."""
    eff = Graph.from_dict(graph.to_dict())
    gates: dict[str, dict] = {}
    for h in [n for n in graph.nodes.values() if n.kind == "hitl"]:
        if _is_hitl_route(graph, h):
            continue                       # ROUTE mode: KEEP the node — it runs as a
            #                                human-driven branch stage in run_graph
            #                                (not a spliced gate). See _human_route.
        ins, outs = _hitl_wiring(graph, h)
        eff.remove_node(h.id)              # always drop H and its H-edges
        if len(ins) != 1 or len(outs) != 1:
            continue  # malformed — _validate_hitl already flagged it
        src, dst = ins[0], outs[0]
        gates[dst.name] = {
            "node": h.name,
            "source": src.name,
            "prompt": h.props.get("prompt", "").strip()
            or "Review the output before continuing.",
            "on_reject": h.props.get("on_reject", "stop"),
        }
        _hitl_extra(gates[dst.name], h.props.get("decisions"),
                    h.props.get("timeout", 0), h.props.get("on_timeout", "approve"))
        # Preserve a data contract that lived on the original handoff (A→H or H→B)
        # onto the spliced A→B edge, so a contract and a review gate can coexist on
        # the same handoff instead of the splice silently dropping the contract.
        # Carry the enforcement flags (contract_enforce / contract_max_retries)
        # too — copying only "contract" would silently DOWNGRADE an enforced
        # contract to advisory the moment a review gate sits on the handoff.
        c_edge = (next((e for e in graph.edges
                        if e.src == src.id and e.dst == h.id
                        and e.props.get("contract")), None)
                  or next((e for e in graph.edges
                           if e.src == h.id and e.dst == dst.id
                           and e.props.get("contract")), None))
        eff.add_edge(src.id, dst.id)       # splice A → B directly
        if c_edge is not None:
            spliced = next((e for e in eff.edges
                            if e.src == src.id and e.dst == dst.id), None)
            if spliced is not None:
                for _k in ("contract", "contract_enforce", "contract_max_retries"):
                    if _k in c_edge.props:
                        spliced.props[_k] = c_edge.props[_k]
    return eff, gates


# ── validation / graph analysis ─────────────────────────────────────────────

# Substrings that mark a model as NON-chat (image / video / audio / embedding).
# An agent's LLM must be a text chat model that drives the ReAct loop and calls
# tools; image generation belongs INSIDE a tool, not on the agent's LLM node.
# Catches the common mistake of setting an "illustrator" agent's LLM to an image
# model (e.g. Qwen/Qwen-Image-Edit), which fails at runtime with "Model does not
# exist" on the chat endpoint.
_NON_CHAT_MODEL_HINTS = (
    "image", "flux", "stable-diffusion", "sdxl", "kolors", "/sd3",
    "embedding", "rerank", "bge-", "gte-", "/m3e",
    "whisper", "-tts", "tts-", "cosyvoice", "sensevoice", "fish-speech",
    "wan2", "cogvideo", "hunyuanvideo", "stepvideo", "ltx-video",
)


def _looks_non_chat(model: str) -> bool:
    m = (model or "").lower()
    return any(h in m for h in _NON_CHAT_MODEL_HINTS)


# ── condition / setstate (deterministic control nodes) ──────────────────────
# Allow-listed AST nodes for a condition/setstate expression. Mirrors the
# runtime _eval_ast in graph_codegen_templates — keep the two in sync. Includes
# arithmetic (BinOp + unary +/-) so setstate can compute e.g. `=attempts + 1`.
_EXPR_ALLOWED = (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp,
                 ast.Not, ast.USub, ast.UAdd, ast.BinOp, ast.Add, ast.Sub,
                 ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Compare,
                 ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq,
                 ast.In, ast.NotIn, ast.Name, ast.Load, ast.Constant, ast.Call)


def _validate_expr(expr: str, names: set) -> str | None:
    """None if `expr` is a safe expression over the given names; otherwise a
    short reason. No eval — only the allow-listed grammar; `len(x)` is the only
    call."""
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        return f"can't parse ({e.msg})"
    for node in ast.walk(tree):
        if not isinstance(node, _EXPR_ALLOWED):
            return f"unsupported syntax ({type(node).__name__})"
        if isinstance(node, ast.Call):
            if not (isinstance(node.func, ast.Name) and node.func.id == "len"
                    and len(node.args) == 1 and not node.keywords):
                return "only len(field) calls are allowed"
        if isinstance(node, ast.Name) and node.id != "len" and node.id not in names:
            return f"unknown name '{node.id}'"
    return None


def _condition_table(node: Node) -> list:
    """A condition node's branches as [(expr_or_None, target_name), ...] in
    declared (priority) order; an empty expr becomes None (the else branch)."""
    out = []
    for b in node.props.get("branches") or []:
        to = (b.get("to") or "").strip()
        if not to:
            continue
        expr = (b.get("expr") or "").strip()
        out.append((expr or None, to))
    return out


def _while_condition_table(node: Node, succ_names: list) -> list:
    """A while node, lowered to the SAME table a condition emits: loop while the
    guard holds, else take the exit. `body` is the loop-body successor (it links
    back here to re-check); the first OTHER successor is the exit. Returns
    [(guard, body), (None, exit)] — _eval_cond then routes to body while guard is
    true and to exit (the else) when it is false."""
    guard = (node.props.get("condition") or "").strip()
    body = (node.props.get("body") or "").strip()
    out = []
    if body:
        out.append((guard or None, body))
    exits = [s for s in succ_names if s != body]
    if exits:
        out.append((None, exits[0]))
    return out


def _setstate_table(node: Node, fields: set) -> list:
    """A setstate node's assignments as an ordered [(field, raw_value), ...] for
    declared fields. raw_value is a literal, or an '=expr' the runtime evaluates
    against the state (+ `output` = the upstream text)."""
    out = []
    for a in node.props.get("assignments") or []:
        field = (a.get("field") or "").strip()
        if field in fields:
            out.append((field, (a.get("value") or "").strip()))
    return out


def _valid_daily_time(s: str) -> bool:
    """True if `s` is a 24-hour daily time 'HH:MM' or 'HH:MM:SS'."""
    parts = s.split(":")
    if len(parts) not in (2, 3):
        return False
    try:
        vals = [int(p) for p in parts]
    except ValueError:
        return False
    sec = vals[2] if len(vals) == 3 else 0
    return 0 <= vals[0] <= 23 and 0 <= vals[1] <= 59 and 0 <= sec <= 59


def _parse_start_at(s: str):
    """Parse an absolute LOCAL datetime; a datetime or None. Accepts
    'YYYY-MM-DD HH:MM[:SS]' and the 'T'-separated variant. (Must match the parsing
    scheduler.py does at runtime — keep the format list in sync.)"""
    import datetime as _dt
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return _dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def analyze(graph: Graph) -> dict:
    """Validate the graph; returns {pipeline, control, revise_edge, mode, errors,
    warnings}. Errors block generation; warnings are shown but allowed."""
    errors: list[str] = []
    warnings: list[str] = []
    # Subgraph nodes are FLATTENED into the parent first, so all validation below
    # runs over the fully-expanded flow (a bad/recursive include raises here).
    try:
        graph = expand_subgraphs(graph)
    except ValueError as e:
        return {"pipeline": [], "control": [], "revise_edge": None,
                "mode": "chain", "errors": [str(e)], "warnings": []}
    # HITL checkpoints are validated on the original graph, then spliced out so
    # the pipeline machinery below runs over a plain agent flow.
    errors += _validate_hitl(graph)
    graph = _splice_hitl(graph)[0]
    agents = graph.agents()
    if not agents:
        return {"errors": (errors + ["Add at least one agent node."]),
                "warnings": []}

    # Custom/nested state types (Approach A): the type registry must be valid, and
    # every state field must reference a native OR declared custom type with a
    # merge policy that fits. state_fields() coerces silently; analyze SURFACES it.
    _type_defs = getattr(graph, "type_defs", None) or {}
    errors += validate_type_defs(_type_defs)
    import re as _re
    _list_of = _re.compile(r"^list\[(\w+)\]$")
    for f in (getattr(graph, "state_schema", None) or []):
        _nm = (f.get("name") or "").strip()
        _t = (f.get("type") or "str").strip()
        if not _nm:
            continue
        _base = _t
        _m = _list_of.match(_t)
        if _m:
            _base = _m.group(1)
        _known = _t in STATE_TYPES or _base in _type_defs
        if not _known:
            errors.append(f"State field '{_nm}' uses undefined type '{_t}'. Define "
                          "it in Graph → Define Types, or use a built-in type.")
            continue
        _red = (f.get("reducer") or "overwrite")
        if _red not in merge_policies_for(_t, _type_defs):
            warnings.append(f"State field '{_nm}' ({_t}) has update policy '{_red}', "
                            "which doesn't fit that type — it will fall back to "
                            "'overwrite'. Pick a compatible policy in Edit Shared State.")
        if _red == "upsert_by_key":
            _key = (f.get("merge_key")
                    or (_type_defs.get(_base) or {}).get("merge_key") or "").strip()
            if not _key:
                errors.append(f"State field '{_nm}' uses upsert_by_key but has no "
                              "merge key — set the id field to merge records by.")

    control_nodes = [n for n in graph.nodes.values() if n.kind in CONTROL_KINDS]
    # A route-mode HITL survived the splice above and becomes a runtime STAGE (keyed
    # by name in SUCCESSORS / STAGE_KINDS / HITL_NODES), so it shares the one stage
    # name space too — include it in the uniqueness + character guards below (its kind
    # is neither an AGENT_KIND nor a CONTROL_KIND, so it isn't in agents/control_nodes).
    route_hitls = [n for n in graph.nodes.values() if n.kind == "hitl"]
    stage_nodes = agents + control_nodes + route_hitls
    # All stage nodes share one name space at runtime (SUCCESSORS / STAGE_KINDS /
    # CONDITIONS / SETSTATE / HITL_NODES are keyed by name), so they must be unique.
    names = [n.name for n in stage_nodes]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        errors.append(f"Stage names (agents, control nodes, routing HITL) must be "
                      f"unique; duplicated: {', '.join(dupes)}.")
    # Stage names flow into the generated module docstring, dict keys and routing
    # tables, so reject characters that would emit uncompilable code (a name with
    # triple-quotes broke the docstring; a @MARKER@-shaped token could survive the
    # substitution guard). Clean error beats an uncompilable agent.py.
    for n in stage_nodes:
        nm = n.name or ""
        bad = ("triple quotes" if '"""' in nm else
               "newlines or control characters" if any(c in nm for c in "\n\r\t\x00") else
               "a @MARKER@-style token" if re.search(r"@[A-Z0-9_]+@", nm) else None)
        if bad:
            errors.append(f"Stage name {nm!r} contains {bad}; rename it "
                          "(it would break the generated code).")

    ws_nodes = [n for n in graph.nodes.values() if n.kind == "webserver"]
    if len(ws_nodes) > 1:
        errors.append("Only one WebServer module per graph is supported — "
                      "remove the extra one.")
    for w in ws_nodes:
        try:
            port = int(w.props.get("port", 8765))
            if not 1 <= port <= 65535:
                raise ValueError
        except (TypeError, ValueError):
            errors.append(f"WebServer '{w.name}' has an invalid port "
                          "(must be 1-65535).")

    # Multiple Schedule nodes are allowed — each is an INDEPENDENT concurrent job over
    # the entry agent (its own task / period / phase / session).
    sched_nodes = [n for n in graph.nodes.values() if n.kind == "schedule"]
    for s in sched_nodes:
        _mode = (s.props.get("mode") or "interval")
        _at = (s.props.get("at") or "").strip()
        _sa = (s.props.get("start_at") or "").strip()
        # validate ONLY the field the chosen strategy uses (the others are ignored)
        if _mode == "daily":
            if not _at:
                errors.append(f"Schedule '{s.name}' (daily) needs a time — set "
                              "'Run daily at' (HH:MM).")
            elif not _valid_daily_time(_at):
                errors.append(f"Schedule '{s.name}' has an invalid daily time '{_at}' "
                              "(use HH:MM or HH:MM:SS, 24-hour).")
        elif _mode == "once":
            if not _sa:
                errors.append(f"Schedule '{s.name}' (once) needs a time — set "
                              "'First run at' (YYYY-MM-DD HH:MM).")
            elif _parse_start_at(_sa) is None:
                errors.append(f"Schedule '{s.name}' has an invalid time '{_sa}' "
                              "(use YYYY-MM-DD HH:MM, optionally with :SS).")
        else:                              # interval
            try:
                if int(s.props.get("every_seconds", 3600)) < 1:
                    raise ValueError
            except (TypeError, ValueError):
                errors.append(f"Schedule '{s.name}' needs every_seconds >= 1 "
                              "(double-click it to configure).")
        if not (s.props.get("initial_task") or "").strip():
            warnings.append(f"Schedule '{s.name}' has no task — set the prompt it "
                            "should run each tick (it will use a generic default otherwise).")
        _links_agent = any(e.src == s.id and graph.nodes.get(e.dst)
                           and graph.nodes[e.dst].kind in AGENT_KINDS
                           for e in graph.edges)
        if not _links_agent:
            warnings.append(f"Schedule '{s.name}' isn't linked to an agent, so no "
                            "scheduler.py is emitted (link schedule → the agent it "
                            "should drive; several schedules can drive different agents).")

    rag_nodes = [n for n in graph.nodes.values() if n.kind == "rag"]
    single_rag = len(rag_nodes) == 1
    _seen_tool = {}
    for r in rag_nodes:
        if not r.props.get("docs_dir"):
            errors.append(f"RAG '{r.name}' has no docs folder "
                          "(double-click it to configure).")
        if not any(e.src == r.id for e in graph.edges):
            errors.append(f"RAG '{r.name}' is not linked to any agent.")
        tn = rag_tool_name(r, single_rag)
        if tn in _seen_tool:
            errors.append(f"RAG '{r.name}' and '{_seen_tool[tn]}' map to the "
                          f"same tool name '{tn}' — give them different names.")
        else:
            _seen_tool[tn] = r.name

    for mem in (n for n in graph.nodes.values() if n.kind == "memory"):
        if not any(e.src == mem.id for e in graph.edges):
            errors.append(f"Memory '{mem.name}' is not linked to any agent "
                          "(link it memory → agent to add remember/recall).")

    for m in (n for n in graph.nodes.values() if n.kind == "mcp"):
        transport = m.props.get("transport", "stdio")
        if transport == "stdio" and not m.props.get("command"):
            errors.append(f"MCP client '{m.name}' (stdio) needs a command "
                          "(double-click it to configure).")
        if transport != "stdio" and not m.props.get("url"):
            errors.append(f"MCP client '{m.name}' ({transport}) needs a "
                          "server URL.")
        if not any(e.src == m.id for e in graph.edges):
            errors.append(f"MCP client '{m.name}' is not linked to any agent.")

    for ev in (n for n in graph.nodes.values() if n.kind == "eval"):
        targets = [e.dst for e in graph.edges if e.src == ev.id]
        if len(targets) > 1:
            errors.append(f"Eval '{ev.name}' links to more than one agent — an "
                          "Eval node tests a single agent (link one) or the "
                          "whole pipeline (link none).")
        # An empty Eval node is allowed: it provisions an eval set that can be
        # filled in from the generated GUI ('Evals -> Edit Eval Sets...').
        if not eval_cases(ev) and ev.props.get("cases"):
            warnings.append(f"Eval '{ev.name}' has cases missing an input or a "
                            "grader (contains / regex / judge) — those are "
                            "skipped. Edit it to complete them.")

    for a in agents:
        llms = graph.inputs_of(a.id, "llm")
        if not llms:
            errors.append(f"Agent '{a.name}' needs at least one linked LLM "
                          "(the first link is primary, extras are fallbacks).")
        if (a.props.get("adaptive_retrieval")
                and not (graph.inputs_of(a.id, "rag") or a.props.get("web_search"))):
            warnings.append(f"Agent '{a.name}' has 'adaptive retrieval' on but no "
                            "RAG or web_search tool — it has nothing to route, so "
                            "the setting has no effect.")
        if a.kind == "workerpool":
            try:
                if int(a.props.get("max_workers", 4)) < 1:
                    raise ValueError
            except (TypeError, ValueError):
                errors.append(f"Worker pool '{a.name}' needs max_workers ≥ 1.")
        for t in graph.inputs_of(a.id, "tool"):
            if not tool_files(t):
                errors.append(f"Tools node '{t.name}' has no tools selected "
                              "(double-click it to choose).")
        # the same tool file reaching one agent twice (across Tools nodes)
        # would register the same function twice
        files = [f for t in graph.inputs_of(a.id, "tool") for f in tool_files(t)]
        dup_files = sorted({f for f in files if files.count(f) > 1})
        if dup_files:
            errors.append(f"Agent '{a.name}' links tool file(s) "
                          f"{', '.join(dup_files)} more than once — remove "
                          "the duplicate link.")
        seen_llm = set()
        for ln in llms:
            key = (ln.props.get("provider"), ln.props.get("model"),
                   ln.props.get("base_url") or "")
            if key in seen_llm:
                warnings.append(f"Agent '{a.name}' links the same LLM "
                                f"({key[1]}) more than once — the duplicate "
                                "adds no real fallback.")
            seen_llm.add(key)
            if _looks_non_chat(ln.props.get("model", "")):
                warnings.append(
                    f"Agent '{a.name}' uses LLM model "
                    f"'{ln.props.get('model')}', which looks like an image/"
                    "embedding/audio model, not a text chat model. An agent's "
                    "LLM must be a chat model that reasons and calls tools — "
                    "image generation belongs inside a tool. Use a chat model "
                    "(e.g. deepseek-ai/DeepSeek-V3).")
        skill_texts = [(item.get("text") or "").strip()
                       for s in graph.inputs_of(a.id, "skill")
                       for item in skill_items(s)]
        skill_texts = [t for t in skill_texts if t]
        if len(skill_texts) != len(set(skill_texts)):
            warnings.append(f"Agent '{a.name}' has skills with identical "
                            "text — it will repeat in the system prompt.")

        arole = a.props.get("role", "single")
        for p in graph.inputs_of(a.id, "prompt"):
            prole = p.props.get("role", "single")
            if prole != "single" and arole != "single" and prole != arole:
                errors.append(
                    f"Prompt '{p.name}' has role '{prole}' but is linked to "
                    f"agent '{a.name}' whose role is '{arole}' — match them "
                    "or set one to 'single'."
                )
        _self_router = (a.props.get("role") == "planner"
                        and a.props.get("route_self"))
        if (a.kind != "router" and a.props.get("role") not in
                ("supervisor", "orchestrator")
                and not _self_router
                and len(graph.flow_successors(a.id)) > 1):
            errors.append(f"Agent '{a.name}' has more than one outgoing link — "
                          "only a router, a supervisor, or a self-routing planner "
                          "may branch. To branch on data, send it to one Condition "
                          "node and branch there.")
        if a.kind == "router" and len(graph.agent_successors(a.id)) < 1:
            errors.append(f"Router '{a.name}' needs at least one outgoing "
                          "agent link (its branches).")

    # Control nodes: validate branches, exprs, assignments and guardrail gates.
    if control_nodes:
        sfnames = {f["name"] for f in state_fields(graph)}
        # condition/while/setstate read or write state; guardrail does not.
        if (not sfnames and any(c.kind in ("condition", "while", "foreach", "setstate")
                                for c in control_nodes)):
            errors.append("Condition / While / For-Each / Set-State nodes need shared-"
                          "state fields — define them via Graph → Edit Shared State first.")
        for c in control_nodes:
            succ_names = {graph.nodes[s].name for s in graph.flow_successors(c.id)}
            if c.kind == "condition":
                branches = c.props.get("branches") or []
                if not branches:
                    errors.append(f"Condition '{c.name}' has no branches — "
                                  "double-click it to add expr → target rows.")
                has_else, covered = False, set()
                for b in branches:
                    to = (b.get("to") or "").strip()
                    expr = (b.get("expr") or "").strip()
                    covered.add(to)
                    if to not in succ_names:
                        errors.append(f"Condition '{c.name}' branch targets "
                                      f"'{to}', not one of its outgoing links.")
                    if not expr:
                        has_else = True
                    else:
                        err = _validate_expr(expr, sfnames)
                        if err:
                            errors.append(
                                f"Condition '{c.name}' expr \"{expr}\": {err}.")
                for sn in succ_names - covered:
                    warnings.append(f"Condition '{c.name}' links to '{sn}' but has "
                                    "no branch for it — that path can't be taken.")
                if branches and not has_else:
                    warnings.append(f"Condition '{c.name}' has no else branch (a "
                                    "row with an empty expr) — the run errors if "
                                    "nothing matches.")
            elif c.kind == "while":
                guard = (c.props.get("condition") or "").strip()
                body = (c.props.get("body") or "").strip()
                if not guard:
                    errors.append(f"While '{c.name}' has no loop condition — "
                                  "double-click it to set one.")
                else:
                    err = _validate_expr(guard, sfnames)
                    if err:
                        errors.append(f"While '{c.name}' condition \"{guard}\": {err}.")
                if not body:
                    errors.append(f"While '{c.name}' has no loop body — pick which "
                                  "outgoing link is the body (it runs while the "
                                  "condition holds).")
                elif body not in succ_names:
                    errors.append(f"While '{c.name}' body '{body}' is not one of its "
                                  "outgoing links.")
                exits = succ_names - {body}
                if not exits:
                    errors.append(f"While '{c.name}' needs an exit link — a second "
                                  "outgoing link (besides the body) taken when the "
                                  "condition becomes false; otherwise the loop has "
                                  "nowhere to go when it ends.")
                # The body must be able to RETURN here to re-check, else it 'loops'
                # at most once. The body may route back through other stages (e.g. a
                # Set-State that bumps a counter), so test reachability over flow
                # edges from the body, not just a direct body->while link.
                loops_back = False
                if body:
                    body_id = next((s for s in graph.flow_successors(c.id)
                                    if graph.nodes[s].name == body), None)
                    if body_id is not None:
                        seen, stack = set(), [body_id]
                        while stack:
                            cur = stack.pop()
                            if cur == c.id:
                                loops_back = True
                                break
                            if cur in seen:
                                continue
                            seen.add(cur)
                            stack.extend(graph.flow_successors(cur))
                if body in succ_names and not loops_back:
                    warnings.append(f"While '{c.name}': its body '{body}' never links "
                                    f"back to '{c.name}', so the loop runs at most "
                                    "once. Route the body back to the While node.")
            elif c.kind == "foreach":  # map a body sub-flow over a runtime list (parallel)
                over = (c.props.get("over") or "").strip()
                if not over:
                    errors.append(f"For-Each '{c.name}' has no list to iterate — set "
                                  "'over' to a shared-state list field (double-click "
                                  "it to configure).")
                elif over not in sfnames:
                    errors.append(f"For-Each '{c.name}' iterates unknown state field "
                                  f"'{over}' — declare it via Graph → Edit Shared State.")
                for _opt, _lbl in (("item_var", "item variable"),
                                   ("result_field", "result field")):
                    _v = (c.props.get(_opt) or "").strip()
                    if _v and _v not in sfnames:
                        errors.append(f"For-Each '{c.name}' {_lbl} '{_v}' is not a "
                                      "declared state field.")
                _merge = (c.props.get("merge") or "concat").strip()
                if _merge not in ("concat", "first", "last", "state_only", "vote"):
                    errors.append(f"For-Each '{c.name}' has an invalid merge '{_merge}'.")
                exit_id, _entry, _stages = _foreach_region(graph, c.id)
                if exit_id is None:
                    errors.append(f"For-Each '{c.name}' {_entry}.")
                else:
                    # Parallel forks fold their writes into the shared state via
                    # reducers; an overwrite/merge_shallow field written by the body
                    # clobbers nondeterministically across items — warn (as fan-out).
                    _red = {f["name"]: (f.get("reducer") or "overwrite")
                            for f in state_fields(graph)}
                    _managed = {over, (c.props.get("item_var") or "").strip(),
                                (c.props.get("result_field") or "").strip(), ""}
                    _writes = set()
                    for sid in _stages:
                        sn = graph.nodes[sid]
                        if sn.kind == "setstate":
                            _writes |= {(a.get("field") or "").strip()
                                        for a in (sn.props.get("assignments") or [])}
                        else:
                            _writes |= set(sn.props.get("writes") or [])
                    for w in sorted(_writes - _managed):
                        if _red.get(w) in ("overwrite", "merge_shallow"):
                            warnings.append(
                                f"For-Each '{c.name}': body writes '{w}' with an "
                                f"'{_red.get(w)}' reducer — parallel items clobber it "
                                "nondeterministically. Use append/extend/add/merge_deep, "
                                "or collect item outputs via the 'result field'.")
            elif c.kind == "setstate":
                succ = graph.flow_successors(c.id)
                if len(succ) != 1:
                    errors.append(f"Set-State '{c.name}' must have exactly one "
                                  f"outgoing link (has {len(succ)}).")
                assigns = c.props.get("assignments") or []
                if not assigns:
                    warnings.append(f"Set-State '{c.name}' has no assignments.")
                for a in assigns:
                    field = (a.get("field") or "").strip()
                    if field in RESERVED_STATE_NAMES:
                        warnings.append(f"Set-State '{c.name}' targets the built-in "
                                        f"field '{field}', which is auto-maintained "
                                        "— that assignment is ignored.")
                        continue
                    if field not in sfnames:
                        errors.append(f"Set-State '{c.name}' assigns unknown state "
                                      f"field '{field}'.")
                    val = (a.get("value") or "").strip()
                    if val.startswith("="):       # computed expression
                        err = _validate_expr(val[1:], sfnames | {"output"})
                        if err:
                            errors.append(f"Set-State '{c.name}' expr "
                                          f"\"{val}\": {err}.")
            elif c.kind == "end":  # terminal sink: finishes the run early
                if any(e.src == c.id for e in graph.edges):
                    errors.append(f"End '{c.name}' is a terminal node — it can't "
                                  "have outgoing links; remove them. (It finishes "
                                  "the run and returns the output that reached it.)")
                if not any(e.dst == c.id for e in graph.edges):
                    warnings.append(f"End '{c.name}' has no incoming link, so it "
                                    "can never be reached — link a stage or an "
                                    "If/Else branch into it.")
            elif c.kind == "fanout":  # parallel fan-out: branches reconverge at a join
                jid, _msg, _tails = _fanout_region(graph, c.id)   # jid None on failure
                if jid is None:
                    errors.append(f"Fan-out '{c.name}': {_msg}.")
            elif c.kind == "join":  # barrier: exactly one exit; reached ONLY by its fan-out
                outs = graph.flow_successors(c.id)
                if len(outs) != 1:
                    errors.append(f"Join '{c.name}' must have exactly one outgoing "
                                  f"link (has {len(outs)}).")
                paired = None
                for f in control_nodes:
                    if f.kind == "fanout":
                        fj, _fb, ftails = _fanout_region(graph, f.id)
                        if fj == c.id:
                            paired = (f, ftails)
                            break
                if paired is None:
                    errors.append(f"Join '{c.name}' is not paired with any fan-out "
                                  "(no fan-out's branches reconverge here).")
                else:
                    _f, _ftails = paired
                    actual = {e.src for e in graph.edges if e.dst == c.id
                              and graph.nodes.get(e.src)
                              and graph.nodes[e.src].kind in AGENT_KINDS + CONTROL_KINDS}
                    expected = set(t for t in _ftails if t)
                    if actual != expected:
                        extra = sorted(graph.nodes[s].name for s in actual - expected)
                        miss = sorted(graph.nodes[s].name for s in expected - actual)
                        m = (f"Join '{c.name}' must be reached only by fan-out "
                             f"'{_f.name}'s branches")
                        if extra:
                            m += f"; unexpected incoming from {extra}"
                        if miss:
                            m += f"; missing branch(es) {miss}"
                        errors.append(m + ".")
            else:  # guardrail — inline content gate (no state needed)
                succ = graph.flow_successors(c.id)
                if len(succ) != 1:
                    errors.append(f"Guardrail '{c.name}' must have exactly one "
                                  f"outgoing link (has {len(succ)}).")
                if not (any((c.props.get("checks") or {}).values())
                        or [p for p in (c.props.get("patterns") or []) if p]
                        or [k for k in (c.props.get("keywords") or []) if k]
                        or (c.props.get("max_length") or 0)):
                    warnings.append(f"Guardrail '{c.name}' has no checks enabled — "
                                    "it will pass everything through.")

    # Count flow edges INTO each agent (from an agent, a control node, OR a
    # route-mode HITL branch) so an agent fed through a condition / setstate /
    # human-router is not mistaken for a 2nd entry. (Gate-mode HITLs are already
    # spliced out here, so any hitl source left is a route-mode branch.)
    incoming = {a.id: 0 for a in agents}
    for e in graph.edges:
        if (graph.nodes[e.dst].kind in AGENT_KINDS
                and graph.nodes[e.src].kind in FLOW_KINDS):
            incoming[e.dst] += 1

    # Fan-in advisories (Step 5). Reconvergence already works: >1 incoming path
    # is allowed and the single cursor arrives via whichever branch ran. These
    # are WARNINGS only (never block generation) — they nudge the user toward an
    # accumulating reducer when they likely want to combine across paths/loops.
    for aid, n in incoming.items():
        if n > 1:
            warnings.append(
                f"Agent '{graph.nodes[aid].name}' has {n} incoming paths "
                "(a fan-in). Only ONE path runs per execution (a Condition picks "
                "the branch); it receives that branch's output. To combine "
                "results across paths or loop iterations, write them into a "
                "shared-state field with an append / add / max / min reducer.")
    _reducer_of = {f["name"]: f["reducer"] for f in state_fields(graph)}
    if _reducer_of:
        _writers: dict[str, set] = {}
        for a in agents:
            for w in (a.props.get("writes") or []):
                if w in RESERVED_STATE_NAMES:
                    warnings.append(f"Agent '{a.name}' declares a write to the "
                                    f"built-in field '{w}', which is auto-"
                                    "maintained — that declaration is ignored.")
                    continue
                _writers.setdefault(w, set()).add(a.name)
        for c in control_nodes:
            if c.kind == "setstate":
                for asg in (c.props.get("assignments") or []):
                    fld = (asg.get("field") or "").strip()
                    if fld:
                        _writers.setdefault(fld, set()).add(c.name)
        # overwrite AND merge_shallow are last-writer-wins per key → order-dependent
        # across multiple writers; the deep/list/numeric merges are deterministic.
        _clobber = {"overwrite", "merge_shallow"}
        for field, who in _writers.items():
            if (len(who) >= 2 and _reducer_of.get(field) in _clobber):
                warnings.append(
                    f"State field '{field}' is written by {len(who)} stages "
                    f"({', '.join(sorted(who))}) with a '{_reducer_of.get(field)}' "
                    "reducer — later writes clobber earlier ones. If you mean to "
                    "accumulate, switch it to append / extend / add / merge_deep / "
                    "upsert_by_key in Graph -> Edit Shared State.")
        # Concurrent fan-out: ≥2 PARALLEL branches writing the SAME overwrite field is a
        # nondeterministic clobber (unlike the sequential warning above) — ERROR. An
        # accumulate reducer (append/add/max/min) merges deterministically, so it's fine.
        for c in control_nodes:
            if c.kind != "fanout":
                continue
            _jid, _ents, _tl = _fanout_region(graph, c.id)
            if _jid is None:
                continue                       # already reported by the fanout arm
            _fld_branches: dict[str, int] = {}
            for _entry in _ents:
                _wrote, _cur, _seen = set(), _entry, set()
                while _cur and _cur != _jid and _cur not in _seen:
                    _seen.add(_cur)
                    _nd = graph.nodes.get(_cur)
                    if _nd is None:
                        break
                    if _nd.kind in AGENT_KINDS:
                        _wrote |= {w for w in (_nd.props.get("writes") or [])
                                   if w not in RESERVED_STATE_NAMES}
                    elif _nd.kind == "setstate":
                        _wrote |= {(a.get("field") or "").strip()
                                   for a in (_nd.props.get("assignments") or [])
                                   if (a.get("field") or "").strip()
                                   and (a.get("field") or "").strip() not in RESERVED_STATE_NAMES}
                    _sc = graph.flow_successors(_cur)
                    _cur = _sc[0] if len(_sc) == 1 else None
                for _w in _wrote:
                    _fld_branches[_w] = _fld_branches.get(_w, 0) + 1
            for _w, _cnt in _fld_branches.items():
                if _cnt >= 2 and _reducer_of.get(_w) in ("overwrite", "merge_shallow"):
                    errors.append(
                        f"Fan-out '{c.name}': {_cnt} parallel branches write state "
                        f"field '{_w}' with a '{_reducer_of.get(_w)}' reducer — the merged "
                        "result is nondeterministic. Give it an accumulate reducer "
                        "(append / extend / add / merge_deep / upsert_by_key) in "
                        "Graph -> Edit Shared State, or "
                        "write it from only one branch.")

    entries = [aid for aid, n in incoming.items() if n == 0]
    # Multi-pattern: agents tagged with a `mode_label` are each the entry of a
    # selectable pattern. The graph then has several intentional entries (one per
    # mode) — the default mode (first tagged) drives this single-graph analysis;
    # _build_modes compiles each tagged component separately.
    mode_entries = [((n.props.get("mode_label") or "").strip(), aid)
                    for aid, n in graph.nodes.items()
                    if n.kind in AGENT_KINDS and (n.props.get("mode_label") or "").strip()]
    entry: str | None = None
    if mode_entries:
        entry = mode_entries[0][1]
    elif len(agents) == 1:
        entry = agents[0].id
    elif len(entries) == 1:
        entry = entries[0]
    elif not entries:
        # Full loop: every agent has an incoming link, so the entry is
        # ambiguous. This happens when a Condition (while-loop) or a revise edge
        # points back at the first stage. Disambiguate by the 'planner' role.
        planners = [a.id for a in agents if a.props.get("role") == "planner"]
        if len(planners) == 1:
            entry = planners[0]
        else:
            errors.append(
                "Every agent has an incoming link, so the start is ambiguous — "
                "this happens when a Condition loop or a revise edge points back "
                "at the first stage. Mark the starting agent with the 'planner' "
                "role (double-click it), or remove the loop-back link."
            )
    else:
        errors.append(
            "The agent chain needs exactly one entry agent (an agent with no "
            f"incoming agent link); found {len(entries)}."
        )

    # An orchestrator's spawn_subagent tool + isolated-sub-agent contract only
    # make sense at the top level, so the role is valid only on the entry agent.
    # Without this, a mid-chain orchestrator would still receive spawn_subagent
    # (see _build_agent_specs) yet run under chain/graph/supervisor control flow.
    if entry is not None:
        for a in agents:
            if a.props.get("role") == "orchestrator" and a.id != entry:
                errors.append(
                    f"Agent '{a.name}' has the 'orchestrator' role but is not the "
                    "main (entry) agent — an orchestrator can only be the "
                    "top-level agent. Give it the 'worker' role, or make it the "
                    "entry agent (the one with no incoming agent link).")

    mode = "chain"
    control_ids: list = []
    has_router = any(n.kind == "router" for n in agents)
    has_self_router = any(
        n.props.get("role") == "planner" and n.props.get("route_self")
        and len(graph.agent_successors(n.id)) > 1 for n in agents)
    # A route-mode HITL survives _splice_hitl (only 1-out gates are spliced away),
    # so any remaining hitl node is a human-driven branch → force graph mode, same
    # as a router does.
    has_hitl_branch = any(n.kind == "hitl" for n in graph.nodes.values())
    has_control = bool(control_nodes)
    if ((has_control or has_hitl_branch) and entry is not None
            and graph.nodes[entry].props.get("role") in ("supervisor",
                                                         "orchestrator")):
        errors.append("Condition / Set-State / routing-HITL nodes aren't supported "
                      "with a supervisor or orchestrator entry — use a plain or "
                      "planner entry agent (graph mode).")
    if entry is None:
        pipeline_ids, revise_edge = [], None
    elif ((has_router or has_self_router or has_control or has_hitl_branch)
          and graph.nodes[entry].props.get("role") not in ("supervisor",
                                                           "orchestrator")):
        # Graph mode: BFS from the entry over agent-stage edges; routers pick
        # one successor at runtime. A DFS back-edge (e.g. critic→planner) is
        # treated as a revise loop, same as chain mode.
        mode = "graph"
        revise_edge = None
        pipeline_ids = [entry]
        control_ids = []
        _walk_seen = {entry}
        queue = [entry]
        while queue:
            cur = queue.pop(0)
            for s in graph.flow_successors(cur):
                if s in _walk_seen:
                    continue
                _walk_seen.add(s)
                (pipeline_ids if graph.nodes[s].kind in AGENT_KINDS
                 else control_ids).append(s)
                queue.append(s)

        # DFS to find a back-edge → the revise loop (a stage pointing at an
        # ancestor still on the recursion stack).
        _found = []
        _seen, _stack = set(), set()

        def _dfs(u):
            _seen.add(u)
            _stack.add(u)
            for v in graph.agent_successors(u):
                if v in _stack and not _found:
                    _found.append((u, v))
                elif v not in _seen:
                    _dfs(v)
            _stack.discard(u)

        _dfs(entry)
        revise_edge = _found[0] if _found else None

        unreached = [a.name for a in agents if a.id not in pipeline_ids]
        if unreached and not mode_entries:
            errors.append(f"Agent(s) not reachable from the entry "
                          f"'{graph.nodes[entry].name}': {', '.join(unreached)}.")
    elif graph.nodes[entry].props.get("role") == "supervisor":
        # Supervisor pattern: star topology, delegation loop at runtime.
        mode = "supervisor"
        workers = graph.agent_successors(entry)
        if not workers:
            errors.append("The supervisor needs at least one linked worker "
                          "agent (supervisor → worker).")
        for w in workers:
            if graph.agent_successors(w):
                errors.append(f"In the supervisor pattern, worker "
                              f"'{graph.nodes[w].name}' must not have "
                              "outgoing agent links.")
        pipeline_ids, revise_edge = [entry] + workers, None
        unreached = [a.name for a in agents if a.id not in pipeline_ids]
        if unreached and not mode_entries:
            errors.append(f"Agent(s) not connected to the supervisor: "
                          f"{', '.join(unreached)}.")
    elif graph.nodes[entry].props.get("role") == "orchestrator":
        # Autonomous pattern: the orchestrator spawns isolated sub-agents at
        # runtime via the built-in spawn_subagent tool (star topology, but
        # tool-driven + parallel + isolated rather than the NEXT/DONE loop).
        mode = "autonomous"
        subs = graph.agent_successors(entry)
        if not subs:
            errors.append("The orchestrator needs at least one linked sub-agent "
                          "(orchestrator → agent) to spawn.")
        for s in subs:
            sn = graph.nodes[s]
            if sn.kind != "agent":
                errors.append(f"In the autonomous pattern, sub-agent "
                              f"'{sn.name}' must be a plain Agent node — a Router "
                              "or Worker-pool can't be a spawnable sub-agent.")
            elif sn.props.get("role") == "orchestrator":
                # caught by the entry-only rule too, but be explicit here
                errors.append(f"In the autonomous pattern, sub-agent "
                              f"'{sn.name}' can't itself be an orchestrator "
                              "(no nested orchestration in v1).")
            elif graph.agent_successors(s):
                errors.append(f"In the autonomous pattern, sub-agent "
                              f"'{sn.name}' must not have outgoing agent links "
                              "(sub-agents are leaves).")
        pipeline_ids, revise_edge = [entry] + subs, None
        unreached = [a.name for a in agents if a.id not in pipeline_ids]
        if unreached and not mode_entries:
            errors.append(f"Agent(s) not connected to the orchestrator: "
                          f"{', '.join(unreached)}.")
    else:
        pipeline_ids, revise_edge = [entry], None
        cur = entry
        while True:
            nxt = graph.agent_successors(cur)
            if not nxt:
                break
            nxt = nxt[0]
            if nxt in pipeline_ids:           # back-edge → revise loop
                revise_edge = (cur, nxt)
                break
            pipeline_ids.append(nxt)
            cur = nxt
        unreached = [a.name for a in agents if a.id not in pipeline_ids]
        if unreached and not mode_entries:
            errors.append(f"Agent(s) not connected to the chain: "
                          f"{', '.join(unreached)}.")

    # GUI node: linking it to the entry agent turns on gui.py generation.
    gui_nodes = [n for n in graph.nodes.values() if n.kind == "gui"]
    if len(gui_nodes) > 1:
        warnings.append("Only one GUI module is used — link a single GUI node "
                        "to the entry agent.")
    # Only meaningful once the entry agent is resolved; if entry is None a
    # blocking error already explains the real problem.
    if (gui_nodes and entry is not None
            and not any(e.src == g.id and e.dst == entry
                        for g in gui_nodes for e in graph.edges)):
        warnings.append(f"GUI module isn't linked to the entry agent "
                        f"('{graph.nodes[entry].name}') — no desktop GUI (gui.py) "
                        "will be generated. Link the GUI node to it to enable it.")
    # A GUI node may carry a user-authored gui.py source (custom_gui). It replaces
    # the built-in window verbatim, so it MUST at least compile; a source that never
    # drives the agent (import agent / .run(...)) is almost certainly a mistake.
    for g in gui_nodes:
        _cg = (g.props.get("custom_gui", "") or "").strip()
        if not _cg:
            continue
        try:
            compile(_cg.replace("@AGENT_NAME@", "AgentName"), "gui.py", "exec")
        except SyntaxError as _e:
            errors.append(f"Custom GUI (gui.py) has a syntax error: "
                          f"{_e.msg} (line {_e.lineno}).")
            continue
        if "import agent" not in _cg:
            warnings.append("Custom GUI doesn't `import agent` — it won't be able "
                            "to run the generated agent.")
        if ".run(" not in _cg:
            warnings.append("Custom GUI never calls the agent's `.run(...)` — the "
                            "generated agent will never be invoked from the GUI.")

    # Structured-plan advisory: a planner asked to emit a typed dependency plan
    # only pays off if a downstream Worker Pool has 'Dependency-aware execution'
    # on; otherwise the plan is ignored (the pool runs the numbered steps flat).
    if (any(n.props.get("role") == "planner" and n.props.get("structured_plan")
            for n in graph.nodes.values() if n.kind in AGENT_KINDS)
            and not any(n.kind == "workerpool" and n.props.get("dag_plan")
                        for n in graph.nodes.values())):
        warnings.append(
            "A planner has 'Emit a typed dependency plan' enabled but no Worker "
            "Pool has 'Dependency-aware execution' on — the typed plan won't be "
            "used. Enable it on the downstream Worker Pool, or turn off the "
            "planner option.")

    return {"errors": errors, "warnings": warnings, "pipeline": pipeline_ids,
            "control": control_ids, "revise_edge": revise_edge, "mode": mode,
            "entry": entry, "mode_entries": mode_entries}


# ── helpers ─────────────────────────────────────────────────────────────────

def _tool_names(fname: str) -> list[str]:
    with open(os.path.join(TOOLS_DIR, fname), encoding="utf-8") as f:
        return DEF_NAME_RE.findall(f.read())


def _inline_tool_files(files: list[str]) -> str:
    chunks = []
    for fname in files:
        with open(os.path.join(TOOLS_DIR, fname), encoding="utf-8") as f:
            src = TOOL_IMPORT_STRIP_RE.sub("", f.read()).strip()
        chunks.append(f"# --- from tools/{fname} ---\n{src}\n")
    return "\n\n".join(chunks) if chunks else "# (no tools)"


def _tool_descs(tool_file_names) -> list:
    """[(function_name, first-docstring-line)] for the tools in the given files,
    so a self-routing planner's prompt can describe each successor's tools."""
    out = []
    for fname in tool_file_names:
        try:
            with open(os.path.join(TOOLS_DIR, fname), encoding="utf-8") as f:
                tree = ast.parse(f.read())
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                doc = (ast.get_docstring(node) or "").strip().splitlines()
                out.append((node.name, doc[0].strip() if doc else ""))
    return out


def _rag_nodes(graph) -> list:
    return [n for n in graph.nodes.values() if n.kind == "rag"]


def _rag_slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9_]+", "_", (name or "").lower()).strip("_")
    if not s:
        s = "kb"
    if s[0].isdigit():
        s = "kb_" + s
    return s


def rag_tool_name(node, single_default: bool) -> str:
    """Retrieval tool name for a RAG node. A lone RAG node keeps the legacy
    'search_docs'; with several knowledge bases each gets 'search_<slug(name)>'
    so the agent can route between them."""
    return "search_docs" if single_default else "search_" + _rag_slug(node.name)


def _rag_descr(node) -> str:
    """Routing hint for a RAG node (its tool description + a system-prompt-tail
    line). Uses the user-edited description, else a generic one naming the KB."""
    d = (node.props.get("description") or "").strip()
    if d:
        return d
    docs = os.path.basename((node.props.get("docs_dir") or "").rstrip("/\\"))
    return (f"Search the '{node.name}' knowledge base"
            + (f" ({docs})" if docs else "")
            + "; returns relevant text chunks with their sources. Use it for "
              "questions answered by those documents.")


def _successor_menu(graph, agents_spec, ids) -> list:
    """Persona + tool-list menu lines for each successor id — shared by the
    self-routing planner (route_to tail) and the orchestrator (spawn_subagent
    tail). Produces only the per-successor lines; the planner appends its own
    quick-response line afterward."""
    lines = []
    for sid in ids:
        s = graph.nodes[sid]
        persona = agents_spec.get(s.name, {}).get("system", "").strip().splitlines()
        lines.append(f"- {s.name}: {persona[0] if persona else s.name}")
        descs = _tool_descs([f for t in graph.inputs_of(sid, "tool")
                             for f in tool_files(t)])
        rags = graph.inputs_of(sid, "rag")
        if rags:
            single = len(_rag_nodes(graph)) == 1
            for r in rags:
                descs.append((rag_tool_name(r, single),
                              _rag_descr(r).splitlines()[0]))
        for tn, td in descs:
            lines.append(f"    - tool {tn}: {td}" if td else f"    - tool {tn}")
    return lines


_RISK_HIGH = ("high", "danger", "dangerous", "write", "destructive")
_RISK_SAFE = ("safe", "low", "readonly", "read_only", "none")


def _tool_risk(tool_file_names) -> tuple:
    """Scan tool files for an explicit ``@tool(risk="high"|"safe")`` declaration
    per function. Returns (high_names, safe_names) so the generated config's
    high_risk_tools / safe_tools make each tool's OWN declaration authoritative
    over the runtime name-substring heuristic (item 6). Tools with no risk= are
    left to the heuristic. Mirrors _tool_descs' AST walk."""
    high, safe = [], []
    for fname in tool_file_names:
        try:
            with open(os.path.join(TOOLS_DIR, fname), encoding="utf-8") as f:
                tree = ast.parse(f.read())
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            risk = None
            # Accept both `@tool(risk=...)` (bare name, the canonical/coding-agent
            # form) and `@pkg.tool(risk=...)` (attribute form), mirroring how
            # _tool_descs ignores the decorator shape. Only the keyword form is
            # read (positional `@tool("high")` is not — such a tool wouldn't even
            # register in the runtime shim).
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call):
                    continue
                fn = dec.func
                if ((isinstance(fn, ast.Name) and fn.id == "tool")
                        or (isinstance(fn, ast.Attribute) and fn.attr == "tool")):
                    for kw in dec.keywords:
                        if kw.arg == "risk" and isinstance(kw.value, ast.Constant):
                            risk = str(kw.value.value).strip().lower()
            if risk in _RISK_HIGH:
                high.append(node.name)
            elif risk in _RISK_SAFE:
                safe.append(node.name)
    return high, safe


def _split_names(s) -> list:
    """Split a comma/whitespace-separated names string into a clean list."""
    return [t for t in re.split(r"[,\s]+", str(s or "").strip()) if t]


def _parse_kv(s, sep="=") -> dict:
    """Parse `KEY<sep>VALUE` lines into a dict (split on the FIRST sep; skip blank /
    '#' lines). Used for MCP stdio env (=) and http headers (:)."""
    out = {}
    for line in str(s or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or sep not in line:
            continue
        k, v = line.split(sep, 1)
        if k.strip():
            out[k.strip()] = v.strip()
    return out


def _mcp_server_config(node) -> dict:
    """Build a config.json mcp_servers entry from an mcp canvas node.
    The node id is the stable server id used to attach tools to agents."""
    transport = node.props.get("transport", "stdio")
    # id stays stable (tool attachment keys off it); name is the canvas label
    # shown in the runtime's [mcp] log/error messages.
    entry = {"id": node.id, "name": node.name, "transport": transport}
    if transport == "stdio":
        entry["command"] = node.props.get("command", "")
        entry["args"] = shlex.split(node.props.get("args", "") or "")
    else:  # streamable_http / sse
        entry["url"] = node.props.get("url", "")
        entry["verify_tls"] = bool(node.props.get("verify_tls", True))
    # Extra Settings — emit only when set (byte-identical otherwise).
    allow = _split_names(node.props.get("allow_tools", ""))
    if allow:
        entry["allow_tools"] = allow
    deny = _split_names(node.props.get("deny_tools", ""))
    if deny:
        entry["deny_tools"] = deny
    for k in ("connect_timeout", "call_timeout"):
        raw = str(node.props.get(k, "")).strip()
        if raw:
            try:
                v = int(float(raw))
                if v > 0:
                    entry[k] = v
            except ValueError:
                pass
    if transport == "stdio":
        env = _parse_kv(node.props.get("env", ""))
        if env:
            entry["env"] = env
    else:
        hdrs = _parse_kv(node.props.get("headers", ""), sep=":")
        if hdrs:
            entry["headers"] = hdrs
    return entry


DEFAULT_ROUTER_PROMPT = (
    "You are a router. Read the input and decide which one downstream agent "
    "should handle it. Reply with ONLY the name of the chosen route — no "
    "explanation.")


def _persona(graph: Graph, agent: Node, has_revise_target: bool) -> str:
    if agent.kind == "router":
        return (agent.props.get("instructions") or "").strip() \
            or DEFAULT_ROUTER_PROMPT
    prompts = graph.inputs_of(agent.id, "prompt")
    if prompts:
        persona = prompts[0].props.get("text", "").strip()
        if not persona:  # empty prompt node → its role's template
            persona = role_template(prompts[0].props.get("role", "single"))
    else:  # no prompt node → the agent role's template
        persona = role_template(agent.props.get("role", "single"))
    persona = (persona.replace("{agent_name}", agent.name)
               .replace("{name}", agent.name))
    # Skills are no longer baked in here — they are emitted to config['skills']
    # and appended at runtime (so the generated GUI can manage them live).
    if has_revise_target and "REVISE" not in persona:
        persona += REVISE_NOTE
    return persona


def _build_agent_specs(graph: Graph, pipeline_ids: list, revise_edge,
                       hitl_gates: dict):
    """Build the per-agent spec dicts from a (HITL-spliced) graph. Extracted from
    generate_from_graph so the system-prompt dumper (system_prompts_for_graph)
    resolves every agent's prompt through the exact same logic — no drift.
    Returns (agents_spec, llm_configs, all_tool_files, agent_skills, providers)."""
    agents_spec: dict[str, dict] = {}
    llm_configs: dict[str, dict] = {}
    all_tool_files: list[str] = []
    agent_skills: dict[str, list] = {}      # agent name -> [{name, text}, ...]
    providers: set[str] = set()

    for aid in pipeline_ids:
        agent = graph.nodes[aid]
        cfgs = []
        llm_nodes = graph.inputs_of(aid, "llm")  # link order = priority
        # The primary (first-linked) LLM decides parallel tool execution for
        # the whole agent; default off = sequential.
        parallel_tools = bool(
            llm_nodes and llm_nodes[0].props.get("parallel_tools", False))
        for llm_node in llm_nodes:
            provider = llm_node.props["provider"]
            providers.add(provider)
            cfgs.append({
                "provider": provider,
                "model": llm_node.props["model"],
                "api_key": llm_node.props["api_key"],
                "base_url": llm_node.props.get("base_url", ""),
                "vision": bool(llm_node.props.get("vision", False)),
                # model context window (tokens); 0 = no context control. The
                # primary LLM's value drives the entry agent's compaction.
                "context_capacity": int(llm_node.props.get("context_capacity", 0) or 0),
                **_parse_llm_opts(llm_node.props),
            })
        # Router routing-LLM override (Extra Settings): prepend a cheap model as the
        # PRIMARY used for the routing decision; the linked LLM(s) remain as fallback,
        # so the "router needs ≥1 LLM" validation still holds. Only when set.
        if agent.kind == "router" and (agent.props.get("routing_model") or "").strip():
            _rp = (agent.props.get("routing_provider") or "").strip() \
                or (cfgs[0]["provider"] if cfgs else "siliconflow")
            providers.add(_rp)
            cfgs.insert(0, {"provider": _rp,
                            "model": agent.props["routing_model"].strip(),
                            "api_key": (agent.props.get("routing_api_key") or "").strip(),
                            "base_url": (agent.props.get("routing_base_url") or "").strip()})
        llm_configs[agent.name] = cfgs

        stage_tool_files = [f for t in graph.inputs_of(aid, "tool")
                            for f in tool_files(t)]
        for f in stage_tool_files:
            if f not in all_tool_files:
                all_tool_files.append(f)
        tool_names = [n for f in stage_tool_files for n in _tool_names(f)]
        rags = graph.inputs_of(aid, "rag")
        if rags:
            single = len(_rag_nodes(graph)) == 1
            for r in rags:
                tool_names.append(rag_tool_name(r, single))

        if graph.inputs_of(aid, "memory"):          # a linked memory node → remember/recall
            tool_names += ["remember", "recall"]

        skill_nodes = graph.inputs_of(aid, "skill")
        if skill_nodes:
            # Register the agent whenever a Skills node is LINKED — even if it's
            # empty — so the generated GUI always shows the Manage Skills menu
            # and the agent gets the load_skill tool, letting the user add and
            # use skills at runtime.
            agent_skills[agent.name] = [item for s in skill_nodes
                                        for item in skill_items(s)]
            tool_names.append("load_skill")     # built-in progressive-disclosure loader

        budgets = {k: agent.props[k] for k in DEFAULT_BUDGETS}
        # Optional per-agent cost cap ($). Emitted into budgets only when >0 (needs
        # LLM prices to bite); blank/0 keeps the budgets dict byte-identical. NOT in
        # DEFAULT_BUDGETS so the bulk-emit above is untouched.
        _cap = float(agent.props.get("max_budget_usd", 0) or 0)
        if _cap > 0:
            budgets["max_budget_usd"] = _cap
        has_revise = bool(revise_edge and revise_edge[0] == aid)
        mcp_ids = [m.id for m in graph.inputs_of(aid, "mcp")]
        agents_spec[agent.name] = {
            "system": _persona(graph, agent, has_revise),
            "tools": tool_names,
            "budgets": budgets,
            "parallel_tools": parallel_tools,
            # per-agent HITL trigger policy (active when hitl_confirm is on)
            "hitl_triggers": agent.props.get("hitl_triggers",
                                             ["high_risk_tool"]),
            "hitl_confidence_threshold": float(
                agent.props.get("hitl_confidence_threshold", 0.6)),
            "hitl_on_reject": agent.props.get("hitl_on_reject", "stop"),
        }
        # Shared-state read/write declarations (graph state_schema). Only emit
        # non-empty ones; absent "reads" means "read all" at runtime. The built-in
        # fields are framework-maintained, so they are stripped from `writes` (a
        # user can never write them) but kept in `reads` (opt-in to read them).
        for _k in ("reads", "writes"):
            vals = list(agent.props.get(_k) or [])
            if _k == "writes":
                vals = [w for w in vals if w not in RESERVED_STATE_NAMES]
            if vals:
                agents_spec[agent.name][_k] = vals
        # per-agent guardrail overrides (merged over config["guardrails"] at runtime)
        if agent.props.get("guardrails"):
            agents_spec[agent.name]["guardrails"] = dict(agent.props["guardrails"])
        # Extra Settings (opt-in, emitted only when set → byte-identical otherwise):
        # max_rpm (requests/min rate limit) + stage_retries (re-run stage on a
        # transient error). Applies to agent and workerpool kinds alike.
        _rpm = int(agent.props.get("max_rpm", 0) or 0)
        if _rpm > 0:
            agents_spec[agent.name]["max_rpm"] = _rpm
        _sr = int(agent.props.get("stage_retries", 0) or 0)
        if _sr > 0:
            agents_spec[agent.name]["stage_retries"] = _sr
        # on_budget policy (stop | retry); "continue" (default) is NOT emitted so
        # graphs that don't set it stay byte-identical.
        _ob = (agent.props.get("on_budget") or "continue").strip()
        if _ob in ("stop", "retry"):
            agents_spec[agent.name]["on_budget"] = _ob
        # Context-compaction trigger (%): when the entry agent's estimated input
        # reaches this fraction of its usable window, older turns are compacted.
        # Default 85 (the historical constant) is NOT emitted → byte-identical.
        _ct = int(agent.props.get("compact_threshold", 85) or 85)
        if _ct != 85 and 1 <= _ct <= 100:
            agents_spec[agent.name]["compact_threshold"] = round(_ct / 100.0, 4)
        # Structured final answer (opt-in): force THIS agent's FINAL answer to a JSON
        # schema (validated + bounded re-ask in react()). Different from an LLM node's
        # response_format (which shapes every call). Blank/invalid → emit nothing, and
        # the persona/spec stay byte-identical.
        _fs_raw = str(agent.props.get("final_schema", "")).strip()
        if _fs_raw:
            try:
                _fs = json.loads(_fs_raw)
            except (json.JSONDecodeError, ValueError):
                _fs = None
            if isinstance(_fs, dict):
                spec = agents_spec[agent.name]
                spec["final_schema"] = _fs
                spec["final_schema_retries"] = max(
                    0, int(agent.props.get("final_schema_retries", 2) or 0))
                spec["system"] = spec["system"].rstrip() + (
                    "\n\n## Structured final answer\n"
                    "Your FINAL answer MUST be a single JSON object (no prose, no "
                    "markdown fence) conforming to this JSON Schema:\n"
                    + json.dumps(_fs, ensure_ascii=False, indent=2))
        # human checkpoint before this stage: an explicit HITL node wins;
        # otherwise the agent's own 'review before run' property (default off).
        if agent.name in hitl_gates:
            agents_spec[agent.name]["review"] = hitl_gates[agent.name]
        elif agent.props.get("hitl_review"):
            preds = [graph.nodes[e.src].name for e in graph.edges
                     if e.dst == aid and graph.nodes[e.src].kind in AGENT_KINDS]
            agents_spec[agent.name]["review"] = {
                "node": None,               # a property, not a canvas node
                "source": preds[0] if len(preds) == 1 else None,
                "prompt": "Review this stage's input before it runs.",
                "on_reject": agent.props.get("hitl_on_reject", "stop"),
            }
            _hitl_extra(agents_spec[agent.name]["review"],
                        agent.props.get("hitl_decisions"),
                        agent.props.get("hitl_timeout", 0),
                        agent.props.get("hitl_on_timeout", "approve"))
        if mcp_ids:
            agents_spec[agent.name]["mcp"] = mcp_ids
        if agent.kind == "workerpool":
            agents_spec[agent.name]["pool"] = True
            agents_spec[agent.name]["max_workers"] = int(
                agent.props.get("max_workers", 4))
            if agent.props.get("dag_plan"):      # dependency-aware DAG dispatch
                agents_spec[agent.name]["dag"] = True

    # Routing via the built-in route_to tool: hand it (with the routes enum) to
    # Router nodes AND self-routing planners. A self-routing planner also gets a
    # system-prompt tail describing each successor (what it does + its tools) so
    # it can pick well. The runtime keeps a text-line 'ROUTE: <name>' fallback.
    for aid in pipeline_ids:
        n = graph.nodes[aid]
        succ_ids = graph.agent_successors(aid)
        is_router = (n.kind == "router")
        is_self_planner = (n.props.get("role") == "planner"
                           and n.props.get("route_self") and len(succ_ids) > 1)
        if not (is_router or is_self_planner):
            continue
        spec = agents_spec[n.name]
        spec["routes"] = [graph.nodes[s].name for s in succ_ids]
        if "route_to" not in spec["tools"]:
            spec["tools"] = spec["tools"] + ["route_to"]
        # Router default/tie-break branch (Extra Settings): the explicit fallback
        # when the reply is ambiguous, instead of the implicit first successor. Only
        # emitted when set to a real successor (byte-identical otherwise).
        if is_router:
            _dr = (n.props.get("default_route") or "").strip()
            if _dr and _dr in spec["routes"]:
                spec["default_route"] = _dr
        if not is_self_planner:
            continue                         # routers route via route(), no tail
        spec["route_self"] = True
        lines = _successor_menu(graph, agents_spec, succ_ids)
        # Quick-response branch (opt-in): let the planner end early with its own
        # answer when no successor fits — e.g. a greeting or a directly-
        # answerable question — instead of forcing the input through a worker.
        quick_tail = ""
        if n.props.get("quick_response"):
            spec["quick_response"] = True
            lines.append(
                "- __none__: NONE of the above agents fit (e.g. a greeting, "
                "small talk, or a question you can answer directly) — choose this "
                "to reply to the user yourself now, without handing off.")
            quick_tail = " (or 'ROUTE: __none__' to answer directly)"
        spec["system"] = spec["system"].rstrip() + (
            "\n\n## Choose the next agent\n"
            "After your plan, hand off to exactly ONE of these agents — based on "
            "what each does and the tools it has — by calling the route_to tool "
            "with its name:\n" + "\n".join(lines)
            + "\n\n(If you cannot call tools, instead end your reply with a final "
              "line, exactly: ROUTE: <agent name>" + quick_tail + ".)")

    # Autonomous spawning: hand the built-in spawn_subagent tool (with the
    # sub-agent enum) to orchestrator agents, plus a system-prompt tail listing
    # each sub-agent (what it does + its tools) so the orchestrator picks well.
    for aid in pipeline_ids:
        n = graph.nodes[aid]
        # spawn_subagent is an entry-only, autonomous-mode contract (analyze()
        # rejects an orchestrator anywhere but the entry, and pipeline_ids[0] is
        # always the entry); guard here too so a mid-pipeline orchestrator can
        # never silently receive the tool even if validation is bypassed.
        if n.props.get("role") != "orchestrator" or aid != pipeline_ids[0]:
            continue
        sub_ids = graph.agent_successors(aid)
        if not sub_ids:
            continue
        spec = agents_spec[n.name]
        spec["spawnable"] = [graph.nodes[s].name for s in sub_ids]
        if "spawn_subagent" not in spec["tools"]:
            spec["tools"] = spec["tools"] + ["spawn_subagent"]
        spec["parallel_tools"] = True   # spawns are isolated → safe to run in parallel
        lines = _successor_menu(graph, agents_spec, sub_ids)
        spec["system"] = spec["system"].rstrip() + (
            "\n\n## Your sub-agents (spawn_subagent)\n"
            "Delegate a complete, self-contained task to ONE of these by name; "
            "each runs in isolation with only its own tools and returns just its "
            "result:\n" + "\n".join(lines))

    # Opt-in working checklist: agents with enable_todos get the built-in
    # write_todos tool + a short prompt tail. Backed by the `todos` built-in state
    # field (state_fields() injects it whenever any agent opts in).
    for aid in pipeline_ids:
        n = graph.nodes[aid]
        spec = agents_spec.get(n.name)
        if not spec or not n.props.get("enable_todos"):
            continue
        if "write_todos" not in spec["tools"]:
            spec["tools"] = spec["tools"] + ["write_todos"]
        spec["system"] = spec["system"].rstrip() + (
            "\n\n## Working checklist (write_todos)\n"
            "For multi-step work, call the write_todos tool to keep a short "
            "checklist. Pass the FULL list each time (it replaces the previous "
            "one); mark an item in_progress when you start it and completed when "
            "you finish. Keep it to a handful of concrete items and update it as "
            "you go — it's your plan, visible to the user.")

    # Opt-in code execution: agents with code_exec get the built-in run_python
    # tool (an ISOLATED subprocess, cwd = the workspace) + a short prompt tail.
    # Always HITL-gated (run_python added to high_risk_tools below). This is
    # process isolation, NOT a security boundary.
    for aid in pipeline_ids:
        n = graph.nodes[aid]
        spec = agents_spec.get(n.name)
        if not spec or not n.props.get("code_exec"):
            continue
        if "run_python" not in spec["tools"]:
            spec["tools"] = spec["tools"] + ["run_python"]
        spec["system"] = spec["system"].rstrip() + (
            "\n\n## Ad-hoc Python (run_python)\n"
            "You can write and run short Python scripts in an isolated process "
            "whose working directory IS your workspace. Use the named tools FIRST; "
            "use run_python ONLY for analysis they don't cover. Read inputs and "
            "write outputs (CSV/PNG) into the current directory, and print() any "
            "result you need to see — only stdout/stderr comes back.")

    # Opt-in web search: agents with web_search get the built-in keyless web_search
    # tool (DuckDuckGo) + a short prompt tail. Always HITL-gated (added to
    # high_risk_tools below) — it is a NETWORK egress that crosses the offline-first
    # boundary, so it must be an explicit, confirmable action.
    for aid in pipeline_ids:
        n = graph.nodes[aid]
        spec = agents_spec.get(n.name)
        if not spec or not n.props.get("web_search"):
            continue
        if "web_search" not in spec["tools"]:
            spec["tools"] = spec["tools"] + ["web_search"]
        spec["system"] = spec["system"].rstrip() + (
            "\n\n## Web search (web_search)\n"
            "You can search the public web for EXTERNAL or RECENT facts the local "
            "documents and your own knowledge don't cover. Prefer any local "
            "knowledge base FIRST; use web_search only when needed, then cite the "
            "result URLs in your answer.")

    # Opt-in context offloading: agents with offload_results get large tool results
    # written to a workspace file (pointer + preview) instead of into the context
    # window, plus a read_offload tool to fetch the full text on demand.
    for aid in pipeline_ids:
        n = graph.nodes[aid]
        spec = agents_spec.get(n.name)
        if not spec or not n.props.get("offload_results"):
            continue
        spec["offload_results"] = True
        if "read_offload" not in spec["tools"]:
            spec["tools"] = spec["tools"] + ["read_offload"]
        spec["system"] = spec["system"].rstrip() + (
            "\n\n## Large results\n"
            "A very large tool result is replaced with a short '[offloaded: ...]' "
            "note that gives a workspace-relative file path and a preview. If you "
            "need the FULL content, call read_offload with that path; otherwise work "
            "from the preview.")

    # Context quarantine (M3): a SPAWNABLE sub-agent equipped with retrieval tools
    # (a linked RAG knowledge base and/or web_search) is a "research sub-agent" — it
    # runs in an ISOLATED react loop (spawn_subagent), so give it a research
    # contract: gather with its tools, then return a COMPACT, CITED summary (not raw
    # chunks) to the orchestrator. Keeps multi-search noise out of the main thread
    # (complements M2). Automatic — no extra prop; a pure prompt affordance.
    _spawnable = {nm for s in agents_spec.values() for nm in (s.get("spawnable") or [])}
    for aid in pipeline_ids:
        n = graph.nodes[aid]
        spec = agents_spec.get(n.name)
        if not spec or n.name not in _spawnable:
            continue
        has_rag = bool(graph.inputs_of(aid, "rag"))
        has_web = bool(n.props.get("web_search"))
        if not (has_rag or has_web):
            continue
        srcs = ("the documents and the web" if (has_rag and has_web)
                else "the document knowledge base" if has_rag else "the web")
        spec["system"] = spec["system"].rstrip() + (
            "\n\n## Research contract\n"
            f"You are a research sub-agent working in an ISOLATED context. Search "
            f"{srcs} with your tools as many times as needed, then return a COMPACT, "
            "well-organized summary that answers the delegated task and CITES its "
            "sources (file names / URLs). Return ONLY that summary — the raw search "
            "results stay with you and never reach the caller.")

    # Adaptive retrieval (L1, opt-in): Adaptive-RAG-style routing realized in the
    # tool-calling model — decide FIRST whether retrieval is needed, then pick the
    # best source. Only meaningful for an agent that actually has a retrieval tool
    # (a linked RAG KB and/or web_search); a pure prompt affordance.
    for aid in pipeline_ids:
        n = graph.nodes[aid]
        spec = agents_spec.get(n.name)
        if not spec or not n.props.get("adaptive_retrieval"):
            continue
        if not (graph.inputs_of(aid, "rag") or n.props.get("web_search")):
            continue                        # no retrieval tool → nothing to route
        spec["system"] = spec["system"].rstrip() + (
            "\n\n## Adaptive retrieval\n"
            "Before calling a search tool, decide whether retrieval is needed AT "
            "ALL: for greetings, small talk, or facts you already know confidently, "
            "answer DIRECTLY without searching. Search only when the answer depends "
            "on the documents or on external/recent facts. When you do search, pick "
            "the SOURCE that fits — the knowledge base whose description matches the "
            "question, or web_search for external or up-to-date facts. Don't search "
            "more than necessary.")

    # L2 (opt-in): answer-groundedness grading + a bounded regenerate loop
    # (Self-RAG checkpoints 2 & 3). Enabled per agent via groundedness_check +
    # max_regen; the flags flow into the runtime spec (react() grades the final
    # answer and, if it falls short, feeds back and regenerates up to max_regen).
    # Purely additive — off by default, fail-soft, one extra LLM call per grade.
    for aid in pipeline_ids:
        n = graph.nodes[aid]
        spec = agents_spec.get(n.name)
        if not spec or not n.props.get("groundedness_check"):
            continue
        spec["groundedness_check"] = True
        # enabling the check implies at least one revise (max_regen 0 would be a
        # silent no-op yet still add the prompt tail — clamp to >= 1).
        spec["max_regen"] = max(1, int(n.props.get("max_regen", 1)))
        spec["system"] = spec["system"].rstrip() + (
            "\n\n## Grounded answers\n"
            "Your final answer is checked for grounding in the retrieved sources "
            "and for whether it answers the question; if it falls short you will be "
            "asked to revise. Base your answer on the retrieved information and cite "
            "sources.")

    # Opt-in structured plan: a planner with `structured_plan` is asked to append
    # a typed {id, subgoal, depends_on} DAG in a ```plan fence so a downstream
    # worker pool (with `dag`) can run independents in parallel and feed each
    # dependent its prerequisites' outputs. Tolerated, not enforced — the runtime
    # falls back to free-text parsing if the block is absent or invalid, so the
    # numbered-steps body of the planner persona is intentionally kept intact.
    for aid in pipeline_ids:
        n = graph.nodes[aid]
        spec = agents_spec.get(n.name)
        if (not spec or n.props.get("role") != "planner"
                or not n.props.get("structured_plan")):
            continue
        spec["structured_plan"] = True       # informational (drives prompt only)
        spec["system"] = spec["system"].rstrip() + (
            "\n\n## Typed plan (optional)\n"
            "After your numbered plan, you MAY append EXACTLY ONE fenced block:\n"
            '```plan\n'
            '{"subgoals":[{"id":"s1","subgoal":"<one concrete action>",'
            '"depends_on":[]},{"id":"s2","subgoal":"...","depends_on":["s1"]}]}\n'
            '```\n'
            "- `id`: a short unique token (s1, s2, ...).\n"
            "- `subgoal`: one concrete instruction a worker can do on its own.\n"
            "- `depends_on`: the ids whose RESULTS this subgoal needs; [] if "
            "independent.\n"
            "- Keep it a DAG (no cycles), 3-7 subgoals. If you cannot, just give "
            "the numbered plan and omit the block.")

    # Shared state on the system prompt: DESCRIBE the shared fields (name, type,
    # meaning) on EVERY agent's tail so each agent knows the shared vocabulary;
    # current values are injected per turn at run time (_state_preamble). Agents
    # that declare writes ALSO get the write mechanics (the ```state block).
    sfields = state_fields(graph)
    if sfields:
        rule = {f["name"]: f["reducer"] for f in sfields}
        # USER-declared fields get the uniform "## Shared state" describe block on
        # every agent. The built-ins (tool_calls/agents) are auto-maintained and
        # NOT described uniformly — an agent only learns of one when it opts to
        # READ it (below), so trivial graphs keep clean prompts.
        user_fields = [f for f in sfields if not f.get("builtin")]
        if user_fields:
            desc_lines = []
            for f in user_fields:
                d = (f.get("description") or "").strip()
                line = f"- {f['name']} ({f['type']})"
                desc_lines.append(f"{line}: {d}" if d else line)
            state_desc = ("\n\n## Shared state\n"
                          "This workflow shares these fields across agents; their "
                          "current values are shown to you each turn:\n"
                          + "\n".join(desc_lines))
            for aid in pipeline_ids:
                spec = agents_spec.get(graph.nodes[aid].name)
                if spec:
                    spec["system"] = spec["system"].rstrip() + state_desc
        # Per-agent built-in read note: only for agents that opt IN (list a
        # built-in in their reads). Documents the auto-maintained field so the
        # agent understands the values injected each turn by _state_preamble.
        builtins = {f["name"]: f for f in sfields if f.get("builtin")}
        if builtins:
            for aid in pipeline_ids:
                n = graph.nodes[aid]
                spec = agents_spec.get(n.name)
                wants = [builtins[r] for r in (n.props.get("reads") or [])
                         if r in builtins]
                if spec and wants:
                    lines = "\n".join(f"- {f['name']} ({f['type']}): "
                                      f"{f['description']}" for f in wants)
                    spec["system"] = spec["system"].rstrip() + (
                        "\n\n## Run state (read-only, auto-maintained)\n"
                        "These fields update automatically as the run proceeds; "
                        "their current values are shown to you each turn:\n"
                        + lines)
        for aid in pipeline_ids:
            n = graph.nodes[aid]
            spec = agents_spec.get(n.name)
            writes = [w for w in (n.props.get("writes") or [])
                      if w in rule and w not in RESERVED_STATE_NAMES]
            if not spec or not writes:
                continue
            # writer agents get the built-in set_state tool (the reliable native
            # channel) alongside the ```state block (kept for back-compat).
            if "set_state" not in spec["tools"]:
                spec["tools"] = spec["tools"] + ["set_state"]
            field_lines = "\n".join(f"- {w} (merge: {rule[w]})" for w in writes)
            require = bool(n.props.get("require_writes"))
            if require:                       # runtime gate reads this off AGENTS
                spec["require_writes"] = True
            must = ("You MUST call set_state to record the field(s) below before "
                    "you finish.\n" if require else "")
            spec["system"] = spec["system"].rstrip() + (
                "\n\n## Updating shared state\n" + must
                + "Record shared-state fields in one of two ways — PREFER the tool:\n"
                "1. Call the **set_state** tool with the field(s) to set (reliable; "
                "works even when your final reply must be a specific format).\n"
                "2. Or append ONE fenced block at the very end of your reply:\n"
                "```state\n<field> = <value>      # or  <field> += <value>\n```\n"
                "You may write only these fields:\n" + field_lines + "\n"
                "Quote strings; use JSON for lists/dicts; omit fields you didn't "
                "change.")
    # ## Data contract on agent→agent links: a two-sided handoff spec the link
    # author declares (the fields the upstream produces = the downstream
    # consumes). BOTH endpoints get it on their prompt tail — the producer to
    # shape its output, the consumer to interpret its input. Stored per-edge
    # (edge.props['contract']); values still flow as text at run time.
    _tdefs = getattr(graph, "type_defs", None) or {}

    def _fmt_contract(fields):
        lines = []
        for f in fields:
            head = (f"- {f['name']} ({f['type']}): {f['description']}"
                    if f["description"] else f"- {f['name']} ({f['type']})")
            # for a custom/nested type, show its JSON shape so the producer emits
            # a well-formed value (mirrors what the set_state tool schema does).
            if is_custom_type(f["type"]):
                shape = json.dumps(type_json_schema(f["type"], _tdefs),
                                   ensure_ascii=False, separators=(",", ":"))
                head += f"\n    shape: {shape}"
            lines.append(head)
        return "\n".join(lines)

    for e in graph.edges:
        src, dst = graph.nodes.get(e.src), graph.nodes.get(e.dst)
        if not src or not dst:
            continue
        # Only a data-PRODUCING source (a plain agent / worker pool) has an output
        # to contract about. A router doesn't reshape the payload — it just picks a
        # branch and forwards the SAME upstream text — so a contract on a router
        # edge would inject a promise neither side can honour. Skip those.
        if src.kind not in ("agent", "workerpool") or dst.kind not in AGENT_KINDS:
            continue
        fields = contract_fields(e, _tdefs)
        if not fields:
            continue
        enforce = bool(e.props.get("contract_enforce"))
        body = _fmt_contract(fields)
        sp = agents_spec.get(src.name)
        if sp:
            if enforce:                       # output is validated as JSON + retried
                head = (f"\n\n## Output contract → {dst.name} (ENFORCED)\n"
                        f"Your reply goes to {dst.name} and is AUTOMATICALLY "
                        "VALIDATED. Reply with ONLY a JSON object containing EXACTLY "
                        "these fields, each correctly typed — no prose, no code "
                        "fence:\n")
            else:
                head = (f"\n\n## Output contract → {dst.name}\n"
                        f"Your output is passed to {dst.name} as its input; make "
                        "sure it provides these fields:\n")
            sp["system"] = sp["system"].rstrip() + head + body
        dp = agents_spec.get(dst.name)
        if dp:
            recv = ("a JSON object with these fields" if enforce
                    else "these fields")
            dp["system"] = dp["system"].rstrip() + (
                f"\n\n## Input contract ← {src.name}\n"
                f"The input you receive from {src.name} is {recv}:\n" + body)

    # ## Knowledge bases tail: each linked RAG node's description doubles as its
    # tool's routing hint and a line here, so the agent knows which KB to query.
    single_rag = len(_rag_nodes(graph)) == 1
    for aid in pipeline_ids:
        n = graph.nodes[aid]
        spec = agents_spec.get(n.name)
        rags = graph.inputs_of(aid, "rag")
        if not spec or not rags:
            continue
        kb_lines = "\n".join(
            f"- {rag_tool_name(r, single_rag)}: {_rag_descr(r).splitlines()[0]}"
            for r in rags)
        spec["system"] = spec["system"].rstrip() + (
            "\n\n## Knowledge bases\n"
            "Retrieve from these document knowledge bases with the matching "
            "tool; pick the one whose description fits the question, cite the "
            "sources it returns, and if nothing relevant comes back say so "
            "instead of guessing:\n" + kb_lines)
    return agents_spec, llm_configs, all_tool_files, agent_skills, providers


def _load_skill_renderers():
    """Single-source the skill-block rendering across the gen/runtime boundary.
    runtime/skills.py is a fragment (not importable: references CONFIG/BASE_DIR),
    so we exec the SAME text that gets inlined into the agent (SKILLS_CODE) in a
    throwaway namespace and keep only its pure, globals-free helpers. The
    fragment's module-level code runs harmlessly here (empty CONFIG; no skills.json
    under the bogus BASE_DIR) and its result is discarded — gen time and runtime
    then share ONE definition of the rendering. (Guard: test_prompt_parity.py.)"""
    ns = {"os": os, "json": json, "re": re, "CONFIG": {},
          "BASE_DIR": os.path.join(os.path.dirname(__file__),
                                   "__no_such_skills_dir__")}
    exec(SKILLS_CODE, ns)
    return ns["_render_skills_block"], ns["_skill_desc"]


_render_skills_block, _skill_desc = _load_skill_renderers()


def _compose_system_prompt(base: str, skills: list, tool_names: list) -> str:
    """Reproduce the generated runtime's initial build_system() output: base
    persona (+ route tail) + skills block + tool guidance. The skills block is
    rendered by the SAME _render_skills_block the runtime uses (loaded from the
    skills fragment), so the two can't drift. The runtime also appends
    workspace_context() — empty unless workspace folders are configured — so it's
    omitted here."""
    block = _render_skills_block(skills)
    if tool_names:
        guidance = ("\n\nUse the provided tools when they help. When you "
                    "have enough information, reply with your final answer "
                    "as plain text (no more tool calls).")
    else:
        guidance = "\n\nYou have no tools; answer directly."
    return (base or "").strip() + block + guidance


def _prompt_dump(agents_spec: dict, agent_skills: dict, roles: dict) -> dict:
    """The debug JSON of every agent's resolved system prompt (insertion order =
    pipeline order)."""
    agents = {}
    for nm, spec in agents_spec.items():
        skills = agent_skills.get(nm, [])
        agents[nm] = {
            "role": roles.get(nm),
            "system_prompt": _compose_system_prompt(
                spec.get("system", ""), skills, spec.get("tools", [])),
            "base_persona": (spec.get("system") or "").strip(),
            "skills": skills,
            "tools": spec.get("tools", []),
            "self_routing": bool(spec.get("route_self")),
            "quick_response": bool(spec.get("quick_response")),
            "routes": spec.get("routes"),
            "worker_pool": bool(spec.get("pool")),
            "review_gate": spec.get("review"),
        }
    return {
        "_about": ("Resolved system prompt per canvas agent, for debugging. "
                   "'system_prompt' mirrors the generated runtime's initial "
                   "build_system(): base persona + route tail + skills + tool "
                   "guidance. At run time the agent also appends "
                   "workspace_context() (empty unless workspace folders are set) "
                   "and reflects live skill edits, so it can differ once running."),
        "agent_count": len(agents),
        "agents": agents,
    }


_RES_KINDS = ("llm", "tool", "skill", "prompt", "rag", "mcp")
_MODE_RUNNER = {"chain": "pipeline", "supervisor": "supervisor",
                "graph": "graph", "autonomous": "autonomous"}


def _component_subgraph(graph: "Graph", entry_id: str) -> "Graph":
    """The sub-graph for one mode: the entry agent + everything flow-reachable
    from it (agents + control nodes) + those stages' resource inputs (llm / tool
    / skill / prompt / rag / mcp). Modes are separate components; a resource
    shared by several modes is just copied into each sub-graph."""
    flow = {entry_id}
    queue = [entry_id]
    while queue:
        cur = queue.pop(0)
        for s in graph.flow_successors(cur):
            if s not in flow:
                flow.add(s)
                queue.append(s)
    ids = set(flow)
    for e in graph.edges:
        if e.dst in flow and graph.nodes[e.src].kind in _RES_KINDS:
            ids.add(e.src)
    d = graph.to_dict()
    sub = {"nodes": [nd for nd in d["nodes"] if nd["id"] in ids],
           "edges": [e for e in d["edges"] if e["src"] in ids and e["dst"] in ids],
           "state_schema": d.get("state_schema", []),
           "recursion_limit": d.get("recursion_limit", 0),
           "run_wall_clock_s": d.get("run_wall_clock_s", 0),
           "storage": d.get("storage", {})}
    return Graph.from_dict(sub)


def _guardrail_node_cfg(node: "Node") -> dict:
    """Runtime config for a guardrail node: checks + on_trip ALWAYS (same key order
    → byte-identical literal), plus custom patterns / keywords / max_length only
    when set (blank → absent → unchanged output)."""
    cfg = {"checks": dict(node.props.get("checks") or {}),
           "on_trip": node.props.get("on_trip", "redact")}
    pats = [p for p in (node.props.get("patterns") or []) if p]
    if pats:
        cfg["patterns"] = pats
    kws = [k for k in (node.props.get("keywords") or []) if k]
    if kws:
        cfg["keywords"] = kws
    ml = node.props.get("max_length") or 0
    if ml:
        cfg["max_length"] = int(ml)
    return cfg


def _fanout_region(graph: "Graph", fanout_id: str):
    """Pair a fanout with the join its branches reconverge at. Walks each branch
    forward — v1 branches are LINEAR, node-DISJOINT agent chains (exactly one flow
    successor per stage until the join; that also forbids nested fan-outs). Returns
    (join_id, [branch_entry_ids], [branch_tail_ids]) on success, or (None, error_str,
    None) on failure — fixed 3-arity so callers unpack safely. Shared by analyze()
    (validation) and _topology_globals() (emission) so they can't drift."""
    branches = graph.flow_successors(fanout_id)
    if len(branches) < 2:
        return None, "a fan-out needs at least 2 branches", None
    joins, tails, visited = set(), [], set()
    for entry in branches:
        e0 = graph.nodes.get(entry)
        if e0 is None:
            return None, "a branch has a dangling edge", None
        if e0.kind == "join":
            return None, ("a branch goes straight to the join — every branch must "
                          "start with an agent stage"), None
        cur, prev, seen = entry, None, set()
        while cur and cur not in seen:
            seen.add(cur)
            node = graph.nodes.get(cur)
            if node is None:
                return None, "a branch has a dangling edge", None
            if node.kind == "join":
                joins.add(cur)
                tails.append(prev)          # prev = last agent before the join
                break
            if node.kind not in AGENT_KINDS + ("setstate",):
                return None, (f"branch stage '{node.name}' ({node.kind}) is not allowed "
                              "in a fan-out branch yet — agents and Set-State only "
                              "(if/else, while and nested fan-out inside a branch come "
                              "later)"), None
            if cur in visited:              # a stage shared by 2+ branches
                return None, (f"branch stage '{node.name}' is shared by more than one "
                              "branch — branches must be independent until the join"), None
            visited.add(cur)
            succ = graph.flow_successors(cur)
            if len(succ) != 1:
                return None, (f"branch stage '{node.name}' must have exactly one "
                              "successor until the join (no branching/nesting in v1)"), None
            prev, cur = cur, succ[0]
        else:
            return None, "a branch never reaches a join", None
    if len(joins) != 1:
        return None, "branches must reconverge at exactly one shared join", None
    return joins.pop(), branches, tails


def _foreach_region(graph: "Graph", foreach_id: str):
    """Resolve a For-Each node's body region + exit. Single-node (While-like) shape:
    the node has a `body` successor (the loop-body sub-flow, run once PER ITEM) that
    must link BACK here, and one OTHER successor = the exit (taken once after all
    items). Walks the body forward — a LINEAR agent/Set-State chain (exactly one flow
    successor per stage) that must return to `foreach_id`. Returns
    (exit_id, body_entry_id, [body_stage_ids]) on success, or (None, error_str, None)
    — fixed 3-arity. Shared by analyze() and _topology_globals() so they can't drift."""
    node0 = graph.nodes[foreach_id]
    body_name = (node0.props.get("body") or "").strip()
    succ = graph.flow_successors(foreach_id)
    succ_by_name = {graph.nodes[s].name: s for s in succ if graph.nodes.get(s)}
    if not body_name:
        return None, ("has no loop body — pick which outgoing link runs for each "
                      "item"), None
    if body_name not in succ_by_name:
        return None, f"body '{body_name}' is not one of its outgoing links", None
    body_entry = succ_by_name[body_name]
    exits = [s for s in succ if s != body_entry]
    if not exits:
        return None, ("needs an exit link — a second outgoing link (besides the "
                      "body) taken once after every item is processed"), None
    exit_id = exits[0]
    body_stages, cur, seen = [], body_entry, set()
    while cur and cur not in seen:
        if cur == foreach_id:
            break                       # returned to the For-Each node → region closed
        if cur == exit_id:
            return None, (f"body '{body_name}' flows into the exit instead of linking "
                          "back to the For-Each node"), None
        seen.add(cur)
        node = graph.nodes.get(cur)
        if node is None:
            return None, "the body has a dangling edge", None
        if node.kind not in AGENT_KINDS + ("setstate",):
            return None, (f"body stage '{node.name}' ({node.kind}) is not allowed in a "
                          "For-Each body yet — agents and Set-State only"), None
        body_stages.append(cur)
        s = graph.flow_successors(cur)
        if len(s) != 1:
            return None, (f"body stage '{node.name}' must have exactly one successor "
                          "until it links back to the For-Each node (no branching/"
                          "nesting in the body yet)"), None
        cur = s[0]
    else:
        return None, (f"body '{body_name}' must link back to the For-Each node so each "
                      "item's run knows where to stop"), None
    if not body_stages:
        return None, "the body is empty", None
    return exit_id, body_entry, body_stages


def _topology_globals(graph: "Graph", info: dict) -> dict:
    """The name-keyed topology the runtime walks: successors / stage_kinds / entry
    / revise back-edge / control-node tables (conditions, setstate, guardrails),
    over BOTH agents and control nodes. Pure function of (graph, info) — `graph`
    is the HITL-spliced graph, `info` the analyze() result of the un-spliced one.

    Single source of truth for the per-graph block in generate_from_graph and the
    per-component block in _compile_component, which must produce byte-identical
    globals (default mode vs the /mode MODES table). Does NOT cover pipeline_names
    or spawnable (computed/used differently per call site)."""
    pipeline_ids = info["pipeline"]
    control_ids = info.get("control", [])
    revise_edge = info["revise_edge"]
    walk_ids = pipeline_ids + control_ids
    _nm = {aid: graph.nodes[aid].name for aid in walk_ids}

    def _stage_kind(aid):
        k = graph.nodes[aid].kind
        return ("router" if k == "router" else "pool" if k == "workerpool"
                else "hitl" if k == "hitl"    # route-mode HITL: a human-driven branch
                else k if k in CONTROL_KINDS else "agent")

    sfnames = {f["name"] for f in state_fields(graph)}
    # fanout -> {join, branches, max_parallel} and join -> {merge, fanout}. Pairing is
    # validated in analyze(); here we just emit (skip a mispaired one defensively).
    _fanouts, _joins = {}, {}
    for aid in control_ids:
        n = graph.nodes[aid]
        if n.kind != "fanout":
            continue
        jid, branches, _tails = _fanout_region(graph, aid)
        if jid is None or jid not in _nm:
            continue
        _fanouts[n.name] = {
            "join": _nm[jid],
            "branches": [_nm[b] for b in branches if b in _nm],
            "max_parallel": int(n.props.get("max_parallel", 0) or 0)}
        _joins[_nm[jid]] = {
            "merge": graph.nodes[jid].props.get("merge", "concat"),
            "fanout": n.name}
    # foreach -> {over, item_var, result_field, body, exit, merge, max_parallel}. The
    # body runs once per item of state[over] (parallel, isolated fork), then `exit`.
    _foreachs = {}
    for aid in control_ids:
        n = graph.nodes[aid]
        if n.kind != "foreach":
            continue
        exit_id, body_entry, _stages = _foreach_region(graph, aid)
        if exit_id is None or exit_id not in _nm or body_entry not in _nm:
            continue
        _foreachs[n.name] = {
            "over": (n.props.get("over") or "").strip(),
            "item_var": (n.props.get("item_var") or "").strip(),
            "result_field": (n.props.get("result_field") or "").strip(),
            "body": _nm[body_entry],
            "exit": _nm[exit_id],
            "merge": (n.props.get("merge") or "concat").strip(),
            "max_parallel": int(n.props.get("max_parallel", 0) or 0)}
    # route-mode HITL (a human-driven branch that SURVIVED _splice_hitl) -> its
    # prompt / timeout / default-branch. The branch set itself rides in SUCCESSORS
    # (like a router's), so this table only carries the human-prompt config.
    _hitls = {}
    for aid in control_ids:
        n = graph.nodes[aid]
        if n.kind != "hitl":
            continue
        _succ = [_nm[s] for s in graph.flow_successors(aid)
                 if s in _nm and (aid, s) != revise_edge]
        _dr = str(n.props.get("default_route", "") or "").strip()
        _hitls[n.name] = {
            "prompt": n.props.get("prompt", "").strip() or "Choose how to proceed.",
            "timeout": int(n.props.get("timeout", 0) or 0),
            "default_route": _dr if _dr in _succ else ""}
    return {
        "SUCCESSORS": {
            _nm[aid]: [_nm[s] for s in graph.flow_successors(aid)
                       if s in _nm and (aid, s) != revise_edge]
            for aid in walk_ids},
        "STAGE_KINDS": {_nm[aid]: _stage_kind(aid) for aid in walk_ids},
        "ENTRY": _nm.get(info.get("entry"),
                         pipeline_ids and _nm[pipeline_ids[0]] or ""),
        "REVISE_EDGE": ([graph.nodes[revise_edge[0]].name,
                         graph.nodes[revise_edge[1]].name] if revise_edge else None),
        # condition AND while nodes share the runtime condition table + _eval_cond
        # (a while is just a guard→body / else→exit pair that loops back).
        "CONDITIONS": {
            **{graph.nodes[aid].name: _condition_table(graph.nodes[aid])
               for aid in control_ids if graph.nodes[aid].kind == "condition"},
            **{graph.nodes[aid].name: _while_condition_table(
                   graph.nodes[aid],
                   [_nm[s] for s in graph.flow_successors(aid)
                    if s in _nm and (aid, s) != revise_edge])
               for aid in control_ids if graph.nodes[aid].kind == "while"}},
        "SETSTATE": {
            graph.nodes[aid].name:
                _setstate_table(graph.nodes[aid], sfnames - RESERVED_STATE_NAMES)
            for aid in control_ids if graph.nodes[aid].kind == "setstate"},
        "GUARDRAIL_NODES": {
            graph.nodes[aid].name: _guardrail_node_cfg(graph.nodes[aid])
            for aid in control_ids if graph.nodes[aid].kind == "guardrail"},
        "FANOUT": _fanouts,
        "JOIN": _joins,
        "FOREACH": _foreachs,
        "HITL_NODES": _hitls,
    }


def _compile_component(graph: "Graph", info: dict, hitl_gates: dict) -> dict:
    """Compile one single-pattern component into its runner + topology globals +
    agent specs — mirrors the per-graph computation in generate_from_graph."""
    pipeline_ids = info["pipeline"]
    revise_edge = info["revise_edge"]
    topo = _topology_globals(graph, info)
    pipeline_names = [graph.nodes[aid].name for aid in pipeline_ids]
    specs, llm_configs, tool_files, skills, providers = _build_agent_specs(
        graph, pipeline_ids, revise_edge, hitl_gates)
    spawnable = sorted({n for s in specs.values() for n in s.get("spawnable", [])})
    return {
        "runner": _MODE_RUNNER.get(info["mode"], "pipeline"),
        "globals": {
            "PIPELINE": pipeline_names, "ENTRY": topo["ENTRY"],
            "SUCCESSORS": topo["SUCCESSORS"], "STAGE_KINDS": topo["STAGE_KINDS"],
            "REVISE_EDGE": topo["REVISE_EDGE"], "SPAWNABLE": spawnable,
            "CONDITIONS": topo["CONDITIONS"], "SETSTATE": topo["SETSTATE"],
            "GUARDRAIL_NODES": topo["GUARDRAIL_NODES"],
            "FANOUT": topo["FANOUT"], "JOIN": topo["JOIN"],
            "FOREACH": topo["FOREACH"],
            "HITL_NODES": topo["HITL_NODES"]},
        "agents_spec": specs, "llm_configs": llm_configs,
        "tool_files": tool_files, "agent_skills": skills, "providers": providers,
    }


def _build_modes(graph, info, hitl_gates):
    """Compile a multi-pattern graph into the runtime MODES table. Each
    mode_label-tagged agent's component is one selectable pattern. Returns
    (modes, default_label, extra): modes maps label -> {runner, globals}; `extra`
    carries the NON-default modes' agent specs / llm configs / tools / skills /
    providers to union into the app (the default mode's are already built by the
    main flow). Returns ({}, "", None) for a single-pattern graph (no tags) so the
    legacy PATTERN_MODE path is byte-identical."""
    mode_entries = info.get("mode_entries") or []
    if not mode_entries:
        return {}, "", None
    state_schema = state_fields(graph)
    default_label, default_entry = mode_entries[0]
    modes, extra = {}, {"agents_spec": {}, "llm_configs": {}, "tool_files": [],
                        "agent_skills": {}, "providers": set()}
    for label, entry_id in mode_entries:
        sub_info = analyze(_component_subgraph(graph, entry_id))
        if sub_info["errors"]:
            raise ValueError(f"pattern mode '{label}': "
                             + "; ".join(sub_info["errors"]))
        sub2, sub_hitl = _splice_hitl(_component_subgraph(graph, entry_id))
        comp = _compile_component(sub2, sub_info, sub_hitl)
        gl = comp["globals"]
        gl["_TOPO_SIG"] = hashlib.sha1(
            (repr(gl["SUCCESSORS"]) + repr(gl["STAGE_KINDS"]) + repr(state_schema)
             ).encode("utf-8")).hexdigest()
        modes[label] = {"runner": comp["runner"], "globals": gl}
        if entry_id != default_entry:
            extra["agents_spec"].update(comp["agents_spec"])
            extra["llm_configs"].update(comp["llm_configs"])
            for f in comp["tool_files"]:
                if f not in extra["tool_files"]:
                    extra["tool_files"].append(f)
            extra["agent_skills"].update(comp["agent_skills"])
            extra["providers"] |= comp["providers"]
    return modes, default_label, extra


# ── module ("package") code-style emitter ────────────────────────────────────
# Splits the assembled single-file runtime into a real Python package. Design:
# functions always run against their DEFINING module's globals, so if runtime/
# _core.py holds every shared name and each other module does `from ._core import
# *`, the whole package shares ONE live CONFIG / _RUN / HISTORY. Layering is a
# strict DAG with no import cycles:
#     runtime/_core.py   data + LLM primitives + trace/storage/workspace/image
#        ^  (from ._core import *)
#     runtime/<feature>  hitl, guardrails(→hitl), skills, rag, mcp, history, checkpoint
#        ^  (from runtime.<f> import *)
#     agent.py           topology + ReAct loop + run* + tools + pool + eval + main
# pool and eval call BACK into the engine (react/run), so they stay inline in
# agent.py rather than becoming importable modules (that would be the one cycle).

# marker -> (destination, module_name). "core" folds into _core.py; "agent" stays
# inline in agent.py; "module" becomes runtime/<name>.py importing the core.
_PKG_FRAG_ROUTE = {
    "@TRACE_CODE@": ("core", None),
    "@STORAGE_CODE@": ("core", None),
    "@WORKSPACE_CODE@": ("core", None),
    "@IMAGE_CODE@": ("core", None),
    # MCP registers native tool schemas the core LLM path (tool_schema) reads, so
    # its _MCP registry is foundational — fold it in rather than making it a leaf.
    "@MCP_CODE@": ("core", None),
    "@POOL_CODE@": ("agent", None),
    "@EVAL_CODE@": ("agent", None),
    "@HITL_CODE@": ("module", "hitl"),
    "@GUARDRAILS_CODE@": ("module", "guardrails"),
    "@SKILLS_CODE@": ("module", "skills"),
    "@RAG_CODE@": ("module", "rag"),
    "@MEMORY_CODE@": ("module", "memory"),
    "@HISTORY_CODE@": ("module", "history"),
    "@CHECKPOINT_CODE@": ("module", "checkpoint"),
}
# extra sibling imports a feature module needs beyond the shared core
_PKG_MODULE_SIBLINGS = {"guardrails": ["hitl"],   # GuardrailTripped/HumanRejected
                        "memory": ["rag"]}         # reuses _rag_tokenize/_rag_rank_bm25

# Re-export EVERY module-level name (single-underscore state like _RUN/_call_one
# included — only dunders hidden) so `import *` shares the same live objects.
# globals() (not dir()) because dir() inside a comprehension sees the comp scope.
_PKG_EXPORT_ALL = (
    "\n\n# Re-export every module-level name (including _single_underscore state\n"
    "# like _RUN / _call_one; only dunders are hidden) so `import *` shares the\n"
    "# SAME live objects across the package — one CONFIG / _RUN / HISTORY.\n"
    "__all__ = [_n for _n in list(globals()) if not _n.startswith('__')]\n")

_MOD_MARKERS = ("# @@MOD:core@@", "# @@MOD:agent@@")


def _emit_package(out_dir, skeleton, frag_map, user_text, name):
    """Write the module ("package") code-style layout. `skeleton` is
    PIPELINE_TEMPLATE with the NON-fragment markers already substituted (the
    @X_CODE@ fragment markers and the `# @@MOD:...@@` region comments are still
    present); `frag_map` maps each fragment marker to its final source."""
    core, agent_buf, modules = [], [], {}
    region = "core"
    sinks = {"core": core, "agent": agent_buf}
    for line in skeleton.split("\n"):
        s = line.strip()
        if s in _MOD_MARKERS:
            region = "core" if s == "# @@MOD:core@@" else "agent"
            continue
        if s in _PKG_FRAG_ROUTE:
            dest, mod = _PKG_FRAG_ROUTE[s]
            if dest == "module":
                modules.setdefault(mod, []).append(frag_map[s])
            else:
                sinks[dest].append(frag_map[s])
            continue
        sinks[region].append(line)

    os.makedirs(os.path.join(out_dir, "runtime"), exist_ok=True)

    # runtime/_core.py — shared foundation. config.json is at the package root,
    # one level above runtime/, so widen `_here` (the base for BOTH the frozen
    # _MEIPASS check and the non-frozen path) by one dirname. Must match the
    # `_here = ...` line in the PIPELINE_TEMPLATE config-resolution block.
    core_src = "\n".join(core).replace(
        "_here = os.path.dirname(os.path.abspath(__file__))",
        "_here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))")
    for _sent, _val in user_text.items():        # personas/AGENTS live in core
        core_src = core_src.replace(_sent, _val)
    core_src = core_src.rstrip() + "\n" + _PKG_EXPORT_ALL
    _write(out_dir, os.path.join("runtime", "__init__.py"),
           '"""Runtime for %s (module code-style, generated by MetaAgent).\n\n'
           'The canvas is the source of truth; re-generate to pick up changes."""\n'
           % name)
    _write(out_dir, os.path.join("runtime", "_core.py"), core_src)

    for mod, blocks in modules.items():
        head = ["from __future__ import annotations", "",
                "from ._core import *  # noqa: F401,F403  (shared state/LLM/trace)"]
        for sib in _PKG_MODULE_SIBLINGS.get(mod, []):
            head.append("from .%s import *  # noqa: F401,F403" % sib)
        body = ("\n".join(head) + "\n\n\n"
                + "\n".join(blocks).strip() + "\n" + _PKG_EXPORT_ALL)
        _write(out_dir, os.path.join("runtime", mod + ".py"), body)

    # agent.py — thin engine; star-imports every runtime name so the historical
    # `import agent; agent.run()/agent.CONFIG/...` surface (gui.py, server.py,
    # run_evals.py) keeps working unchanged.
    imp = ["from __future__ import annotations", "",
           "from runtime._core import *  # noqa: F401,F403"]
    imp += ["from runtime.%s import *  # noqa: F401,F403" % m for m in modules]
    agent_body = "\n".join(agent_buf)
    for _sent, _val in user_text.items():        # inlined tool source lives here
        agent_body = agent_body.replace(_sent, _val)
    doc = ('"""%s — engine (module code-style, generated by MetaAgent).\n\n'
           'Topology, the ReAct loop, run()/run_pipeline/run_graph/run_supervisor,\n'
           'the worker pool and eval runner live here; the runtime services\n'
           '(hitl, rag, history, guardrails, skills, mcp, checkpoint) are the\n'
           'runtime/ package. Run:  python agent.py "your task"\n"""\n')
    agent_src = doc + "\n".join(imp) + "\n\n\n" + agent_body.strip() + "\n"
    _write(out_dir, "agent.py", agent_src)

    # drift guard, per file: no @MARKER@ may survive the routing.
    files = (["agent.py", os.path.join("runtime", "_core.py")]
             + [os.path.join("runtime", m + ".py") for m in modules])
    for rel in files:
        with open(os.path.join(out_dir, rel), encoding="utf-8") as _fh:
            leftover = re.findall(r"@[A-Z][A-Z0-9_]*@", _fh.read())
        if leftover:
            raise AssertionError("package emit: unsubstituted %s in %s"
                                 % (sorted(set(leftover)), rel))
    return sorted(modules)


# ── public API ──────────────────────────────────────────────────────────────

def generate_from_graph(graph: Graph, name: str, gui: bool | None = None,
                        code_style: str = "single") -> str:
    """Generate the agent folder from a canvas graph; returns its path.

    gui=None (default) derives the desktop GUI from the graph: a GUI node linked
    to the entry agent turns on gui.py. Pass True/False to force it.

    code_style: "single" (default) emits one self-contained agent.py with the
    runtime inlined — the portable legacy layout. "package" splits the runtime
    into a `runtime/` package (runtime/_core.py + one module per feature) with a
    thin agent.py engine, for a conventional editable project. Behaviour of the
    generated agent is identical either way; it is purely how the code is laid
    out on disk."""
    if code_style not in ("single", "package"):
        raise ValueError("code_style must be 'single' or 'package'")
    # Flatten subgraph nodes into the parent up front so the whole function (GUI
    # detection, HITL splice, agent specs, codegen) operates on the expanded flow.
    # No-op (same object) when there are no subgraph nodes, so existing graphs are
    # byte-identical. analyze() expands again but that's idempotent here.
    graph = expand_subgraphs(graph)
    info = analyze(graph)
    if info["errors"]:
        raise ValueError("\n".join(info["errors"]))
    _entry = info.get("entry")
    _gui_node = next((graph.nodes[e.src] for e in graph.edges
                      if graph.nodes.get(e.src) and graph.nodes[e.src].kind == "gui"
                      and e.dst == _entry), None)
    if gui is None:
        gui = _gui_node is not None
    # A GUI node may carry a user-authored gui.py SOURCE (custom_gui) to emit in
    # place of the built-in window. Captured BEFORE _splice_hitl reassigns `graph`.
    custom_gui_src = (_gui_node.props.get("custom_gui", "") if _gui_node else "") or ""

    # Work on the HITL-spliced graph so the agent flow is plain agent→agent;
    # `hitl_gates[agent_name]` records the human checkpoint feeding each agent.
    graph, hitl_gates = _splice_hitl(graph)

    pipeline_ids = info["pipeline"]
    revise_edge = info["revise_edge"]
    mode = info["mode"]
    # Name-keyed topology the runtime walks (successors / stage kinds / entry /
    # revise back-edge / control-node tables), over BOTH agents and control nodes.
    # Shared verbatim with each mode's component (the /mode MODES table) so the two
    # can't drift — see _topology_globals.
    topo = _topology_globals(graph, info)
    successors = topo["SUCCESSORS"]
    stage_kinds = topo["STAGE_KINDS"]
    entry_name = topo["ENTRY"]
    revise_names = topo["REVISE_EDGE"]
    conditions = topo["CONDITIONS"]
    setstate = topo["SETSTATE"]
    guardrail_nodes = topo["GUARDRAIL_NODES"]
    fanout_tbl = topo["FANOUT"]
    join_tbl = topo["JOIN"]
    foreach_tbl = topo["FOREACH"]
    hitl_tbl = topo["HITL_NODES"]
    # ENFORCED output contracts: producer name -> {fields, max_retries}. The runtime
    # validates the producer's output as JSON and retries/stops. Only for links that
    # turned "validate" on; fields unioned if a producer has several such edges.
    contracts_out: dict = {}
    for e in graph.edges:
        s, d = graph.nodes.get(e.src), graph.nodes.get(e.dst)
        if not (s and d and e.props.get("contract_enforce")):
            continue
        if s.kind not in ("agent", "workerpool") or d.kind not in AGENT_KINDS:
            continue
        flds = contract_fields(e, getattr(graph, "type_defs", None))
        if not flds:
            continue
        cur = contracts_out.setdefault(s.name, {"fields": [], "max_retries": 0})
        # A producer with several enforced out-edges validates against the UNION
        # of their fields in one pass, so honour the most generous retry budget any
        # of those edges asked for — taking only the first edge's was order-
        # dependent and silently dropped the later edges' contract_max_retries.
        cur["max_retries"] = max(
            cur["max_retries"], max(0, int(e.props.get("contract_max_retries", 2) or 0)))
        _have = {f["name"] for f in cur["fields"]}
        for f in flds:
            if f["name"] not in _have:
                cur["fields"].append({"name": f["name"], "type": f["type"]})
                _have.add(f["name"])
    rag_nodes = [n for n in graph.nodes.values() if n.kind == "rag"]
    rag_node = rag_nodes[0] if rag_nodes else None
    mem_nodes = [n for n in graph.nodes.values() if n.kind == "memory"]
    ws_nodes = [n for n in graph.nodes.values() if n.kind == "webserver"]
    ws_node = ws_nodes[0] if ws_nodes else None
    # every schedule node LINKED to an agent becomes an independent concurrent job
    # in scheduler.py. Each job drives the agent it points at: linking to the entry
    # runs the whole graph (B: several cron jobs over one agent); linking to a
    # DIFFERENT agent starts the run at that agent (A: several schedules each
    # driving a separate agent in one graph). An unlinked schedule emits nothing.
    # Node order = job order. sched_targets[node.id] = the target agent's NAME.
    sched_nodes, sched_targets = [], {}
    for n in graph.nodes.values():
        if n.kind != "schedule":
            continue
        _tgt = next((graph.nodes[e.dst].name for e in graph.edges
                     if e.src == n.id and graph.nodes.get(e.dst)
                     and graph.nodes[e.dst].kind in AGENT_KINDS), None)
        if _tgt is not None:
            sched_nodes.append(n)
            sched_targets[n.id] = _tgt
    mcp_nodes = [n for n in graph.nodes.values() if n.kind == "mcp"]
    eval_nodes = [n for n in graph.nodes.values() if n.kind == "eval"]

    name = re.sub(r"\W+", "_", name).strip("_") or "my_agent"
    out_dir = os.path.join(GENERATED_DIR, name)
    os.makedirs(out_dir, exist_ok=True)

    (agents_spec, llm_configs, all_tool_files,
     agent_skills, providers) = _build_agent_specs(
        graph, pipeline_ids, revise_edge, hitl_gates)

    pipeline_names = [graph.nodes[aid].name for aid in pipeline_ids]
    state_schema = state_fields(graph)   # validated [{name,type,reducer,default,...}]
    # User-declared fields only (the built-ins tool_calls/agents are always
    # present); checkpoint/resume is only meaningful when there is USER state.
    _user_state = [f for f in state_schema if not f.get("builtin")]
    # Graph-mode loop guard: explicit graph.recursion_limit, else an auto bound
    # that scales with the graph size (preserves the pre-Step-4 hop budget).
    _auto_limit = len(stage_kinds) * (MAX_REVISE_ROUNDS + 3) + 5
    recursion_limit = (graph.recursion_limit
                       if getattr(graph, "recursion_limit", 0) > 0 else _auto_limit)

    # Multi-pattern runtime-switch table (label -> {runner, globals}); empty for
    # single-pattern apps (then the legacy PATTERN_MODE path runs unchanged).
    # `_extra` unions the non-default modes' agents/tools/skills into this app.
    modes, default_label, _extra = _build_modes(graph, info, hitl_gates)
    if _extra:
        agents_spec.update(_extra["agents_spec"])
        llm_configs.update(_extra["llm_configs"])
        for _f in _extra["tool_files"]:
            if _f not in all_tool_files:
                all_tool_files.append(_f)
        agent_skills.update(_extra["agent_skills"])
        providers |= _extra["providers"]

    imports = []
    if providers - {"anthropic"}:
        imports.append("from openai import OpenAI")
    if "anthropic" in providers:
        imports.append("import anthropic")
    if imports:                              # httpx ships with both SDKs; the runtime
        imports.append("import httpx")       # uses it to build the proxied http client

    tools_source = _inline_tool_files(all_tool_files)   # build once, reuse below
    # Readable spec literals: personas hoisted into a PERSONAS block (triple-quoted
    # text) referenced by AGENTS, and the structural dicts pretty-printed. Same
    # VALUES as repr() (Python True/None, order preserved) — see _fmt_agents.
    personas_src, agents_src = _fmt_agents(agents_spec)
    header = (
        "\nThis file is GENERATED from a MetaAgent canvas graph — the canvas is "
        "the source of truth.\nRe-generate to pick up design changes; hand-edits "
        "here are not reflected back.\n"
        f"Pattern: {mode}.  Agents: {' -> '.join(pipeline_names)}.\n"
        "config.json holds per-agent LLMs/keys; system_prompts.json shows each "
        "agent's resolved system prompt.")
    # Free-form USER text (system prompts, agent specs incl. descriptions, and the
    # inlined tool source) is injected via NUL sentinels and restored AFTER the
    # drift guard below — so a marker-shaped token inside user text (e.g. a prompt
    # that literally says "@AGENT_NAME@", or a tool file mentioning "@MODES@")
    # can't false-trip assert_substituted. NUL never occurs in the source.
    _user_text = {"\x00S_PERSONAS\x00": personas_src,
                  "\x00S_AGENTS\x00": agents_src,
                  "\x00S_TOOLS\x00": tools_source}
    # The marker->value substitution as an ORDERED list (applied left-to-right ==
    # the former chained .replace()), split into non-fragment markers and the
    # runtime-fragment @X_CODE@ markers. Package code-style (_emit_package) reuses
    # both to route each fragment to its own module file instead of inlining it.
    _nonfrag = [
        ("@AGENT_NAME@", name),
        ("@HEADER@", header),
        ("@LLM_IMPORTS@", "\n".join(imports)),
        # spec literals emit Python values (True/None), not JSON (true/null)
        ("@PERSONAS@", "\x00S_PERSONAS\x00"),
        ("@AGENTS@", "\x00S_AGENTS\x00"),
        ("@PIPELINE@", repr(pipeline_names)),
        ("@REVISE_EDGE@", repr(revise_names)),
        ("@SUCCESSORS@", _fmt_literal(successors)),
        ("@STAGE_KINDS@", _fmt_literal(stage_kinds)),
        ("@ENTRY@", repr(entry_name)),
        ("@SPAWNABLE@", repr(sorted(
            {n for s in agents_spec.values() for n in s.get("spawnable", [])}))),
        ("@STATE_SCHEMA@", _fmt_literal(state_schema)),
        ("@CUSTOM_MERGE_CODE@", _custom_merge_code(getattr(graph, "type_defs", None))),
        ("@CONDITIONS@", _fmt_literal(conditions)),
        ("@CONTRACTS_OUT@", _fmt_literal(contracts_out)),
        ("@SETSTATE@", _fmt_literal(setstate)),
        ("@GUARDRAIL_NODES@", _fmt_literal(guardrail_nodes)),
        ("@FANOUT@", _fmt_literal(fanout_tbl)),
        ("@JOIN@", _fmt_literal(join_tbl)),
        ("@FOREACH@", _fmt_literal(foreach_tbl)),
        ("@HITL_NODES@", _fmt_literal(hitl_tbl)),
        ("@RECURSION_LIMIT@", str(recursion_limit)),
        ("@MAX_REVISE_ROUNDS@", str(MAX_REVISE_ROUNDS)),
        ("@PATTERN_MODE@", mode),
        ("@MODES@", _fmt_literal(modes)),
        ("@DEFAULT_LABEL@", default_label),
        ("@RAG_EVICT_USED@",
         repr(any(r.props.get("evict_used") for r in rag_nodes))),
        ("@MAX_SUPERVISOR_ROUNDS@", str(MAX_SUPERVISOR_ROUNDS)),
        ("@TOOLS_SOURCE@", "\x00S_TOOLS\x00"),
    ]
    _frag = [
        # the memory node REUSES the RAG BM25 tokenizer/ranker, so emit the RAG
        # fragment whenever EITHER a RAG or a memory node is present.
        ("@RAG_CODE@",
         RAG_CODE.strip() if (rag_nodes or mem_nodes) else "# (no RAG module linked)"),
        ("@MEMORY_CODE@",
         MEMORY_CODE.strip() if mem_nodes else "# (no memory module linked)"),
        ("@MCP_CODE@", MCP_CODE.strip() if mcp_nodes else MCP_STUB.strip()),
        ("@POOL_CODE@", POOL_CODE.strip()),
        ("@STORAGE_CODE@", STORAGE_CODE.strip()),
        ("@WORKSPACE_CODE@", WORKSPACE_CODE.strip()),
        ("@HISTORY_CODE@", HISTORY_CODE.strip()),
        ("@CHECKPOINT_CODE@",
         CHECKPOINT_CODE.strip() if _user_state
         else "# (no shared state — checkpoint/resume disabled)"),
        ("@GUARDRAILS_CODE@", GUARDRAILS_CODE.strip()),
        ("@TRACE_CODE@", TRACE_CODE.strip()),
        ("@HITL_CODE@", HITL_CODE.strip()),
        ("@SKILLS_CODE@", SKILLS_CODE.strip()),
        ("@IMAGE_CODE@", IMAGE_CODE.strip()),
        ("@EVAL_CODE@", EVAL_CODE.strip()),
    ]

    def _apply(text, pairs):
        for _m, _v in pairs:
            text = text.replace(_m, _v)
        return text

    agent_src = _apply(PIPELINE_TEMPLATE, _nonfrag + _frag)
    # Fail fast if the marker/replace contract drifted: every @MARKER@ the
    # template declares must have been consumed above. A leftover would emit a
    # broken agent.py noticed only when the app runs — catch it here, by name.
    # (User text is still held out as sentinels, so this sees only the code-
    # controlled skeleton and can't be fooled by marker-shaped prose.) This runs
    # for BOTH code-styles: it validates the same marker contract the package
    # emitter relies on to route fragments.
    assert_substituted(agent_src, template_markers(PIPELINE_TEMPLATE), "agent.py")
    for _sentinel, _value in _user_text.items():
        agent_src = agent_src.replace(_sentinel, _value)
    # `# @@MOD:...@@` are package-emitter region hints; inert in the single file.
    agent_src = "\n".join(l for l in agent_src.split("\n")
                          if l.strip() not in _MOD_MARKERS)

    reqs = []
    if providers - {"anthropic"}:
        reqs.append("openai>=1.40.0")
    if "anthropic" in providers:
        reqs.append("anthropic>=0.40.0")
    # Embedding/vector-store deps are needed only by KBs that actually do dense
    # or hybrid retrieval (the default is BM25 -> no extra deps). The default
    # embedding provider is 'local' (free, no API key) via fastembed.
    _dense = [r for r in rag_nodes
              if r.props.get("retrieval_algorithm", "bm25") in ("dense", "hybrid")]
    if any(r.props.get("embed_provider", "local") == "local" for r in _dense):
        reqs.append("fastembed>=0.3")                 # free local embeddings
    if (any(r.props.get("embed_provider", "local") == "openai" for r in _dense)
            and "openai>=1.40.0" not in reqs):
        reqs.append("openai>=1.40.0")
    _vdbs = {r.props.get("vector_db", "memory") for r in _dense}
    if "chroma" in _vdbs:                 # optional persistent vector store
        reqs.append("chromadb>=0.4")
    if "faiss" in _vdbs:                  # optional in-memory ANN index
        reqs.append("faiss-cpu>=1.7")
    if "qdrant" in _vdbs:                 # optional vector store (embedded or server)
        reqs.append("qdrant-client>=1.7")
    if gui:
        reqs.append("PySide6>=6.5.0")
    if mcp_nodes:
        reqs.append("mcp>=1.0.0")
    if (getattr(graph, "storage", None) or {}).get("backend") in (
            "postgres", "postgresql", "pg"):
        reqs.append("psycopg[binary]>=3.1")   # only when the PG backend is chosen
    reqs += tool_requirements(tools_source)

    llm_lines = "\n".join(
        f"- {agent_name}: " + " -> ".join(c["model"] for c in cfgs)
        + (" (fallback order)" if len(cfgs) > 1 else "")
        for agent_name, cfgs in llm_configs.items()
    )
    readme = (
        f"# {name}\n\nMulti-agent app generated by MetaAgent.\n\n"
        f"Pattern mode: {mode}\n\n"
        f"Agents: {' -> '.join(pipeline_names)}"
        + (f" (revise loop: {revise_names[0]} -> {revise_names[1]})"
           if revise_names else "")
        + f"\n\nLLMs per agent:\n{llm_lines}"
        + "\n\n## Run\n\n    pip install -r requirements.txt\n"
          "    python agent.py \"your task\"\n\nAPI keys live in "
          "config.json under each agent's entry.\n"
    )
    readme += ("\n## Debug\n\nsystem_prompts.json lists each agent's resolved "
               "system prompt (base persona + route tail + skills + tool "
               "guidance), matching what build_system() sends at startup.\n")
    if gui:
        readme += ("\n## GUI (optional)\n\nA PySide6 desktop chat window. The "
                   "agent also runs headless (python agent.py) without it.\n\n"
                   "    pip install -r requirements.txt\n    python gui.py\n")
    if ws_node:
        ws_host = ws_node.props.get("host", "127.0.0.1")
        ws_port = ws_node.props.get("port", 8765)
        readme += (
            "\n## WebSocket server + web UI\n\n    python server.py\n\n"
            f"Open http://{ws_host}:{ws_port}/ in a browser for the chat web "
            f"UI, or connect a WebSocket client to ws://{ws_host}:{ws_port} "
            "and send JSON frames:\n"
            '    {"type": "task", "task": "..."}\n'
            "The server streams {\"type\": \"trace\"} messages and finishes "
            "with {\"type\": \"result\"}. Settings: config.json -> server.\n")

    # Tool-node Extra Settings (per-function; aggregated app-wide keyed by function
    # name — the runtime TOOLS map is one global). Union across nodes; last-wins on a
    # conflict for the same function. Empty everywhere → no config keys (byte-identical).
    _rd_tools, _err_pol, _desc_ov, _force_high, _force_safe = [], {}, {}, [], []
    for _tn in graph.nodes.values():
        if _tn.kind != "tool":
            continue
        for _fn, _p in (_tn.props.get("tool_props") or {}).items():
            if not isinstance(_p, dict):
                continue
            if _p.get("return_direct"):
                _rd_tools.append(_fn)
            _m = _p.get("error_mode")
            if _m and _m != "return":
                _err_pol[_fn] = {"mode": _m,
                                 "error_retries": int(_p.get("error_retries", 0) or 0)}
            _r = _p.get("risk")
            if _r == "high":
                _force_high.append(_fn)
            elif _r == "safe":
                _force_safe.append(_fn)
            _d = (_p.get("description") or "").strip()
            if _d:
                _desc_ov[_fn] = _d

    # Per-tool HITL risk from each tool's own @tool(risk=...) declaration
    # (authoritative over the runtime name heuristic; editable in config.json).
    _risk_high, _risk_safe = _tool_risk(all_tool_files)
    for _t in _force_safe:                    # node override → force safe
        if _t in _risk_high:
            _risk_high.remove(_t)
        if _t not in _risk_safe:
            _risk_safe.append(_t)
    for _t in _force_high:                    # node override → force high (wins over safe)
        if _t in _risk_safe:
            _risk_safe.remove(_t)
        if _t not in _risk_high:
            _risk_high.append(_t)
    # run_python (code_exec) is always high-risk -> always HITL-confirms; its name
    # matches no heuristic marker, so add it explicitly.
    if (any(graph.nodes[a].props.get("code_exec") for a in pipeline_ids)
            and "run_python" not in _risk_high):
        _risk_high = _risk_high + ["run_python"]
    # web_search is a network egress -> always high-risk (HITL-confirms by default);
    # its optional `ddgs` package is added to requirements only when it's enabled.
    if any(graph.nodes[a].props.get("web_search") for a in pipeline_ids):
        if "web_search" not in _risk_high:
            _risk_high = _risk_high + ["web_search"]
        if "ddgs>=6.0" not in reqs:
            reqs.append("ddgs>=6.0")          # keyless DuckDuckGo search (optional dep)
    # parallel_tools here is the app-wide fallback; each agent's own value
    # (from its primary LLM node, default off) takes precedence at runtime.
    gen_config = {"llms": llm_configs, "hitl_confirm": True,
                  # Wrap tool results / retrieved docs in untrusted-data tags +
                  # add a system clause (indirect-prompt-injection hardening; a
                  # mitigation, not a boundary — HITL/guardrails still enforce).
                  "harden_prompts": True,
                  # Hard char bound on the cross-run rolling summary so it (and the
                  # injected context) can't grow unbounded over many runs.
                  "summary_max_chars": 4000,
                  "high_risk_tools": _risk_high, "safe_tools": _risk_safe,
                  "stream": True,
                  # How this app's code was laid out on disk ("single" | "package").
                  # Recorded for provenance; the runtime does not read it.
                  "code_style": code_style,
                  "parallel_tools": False, "sequential_tools": [],
                  "parallel_safe_tools": [],
                  # Char threshold above which an offloading agent's tool result is
                  # written to a workspace file (pointer + preview) instead of
                  # flooding the context. Per-agent opt-in via the agent's
                  # `offload_results` prop; this is the shared size cut-off.
                  "offload_threshold_chars": 12000,
                  # graph-mode checkpoint/resume (opt-in; needs shared state)
                  "checkpoint": False,
                  # deterministic guardrails (defense-in-depth; see runtime/
                  # guardrails.py). Reliable parts on by default; injection
                  # tripwire opt-in. Extend the *_patterns / *_denylist lists.
                  "guardrails": {"enabled": True, "scan_tool_results": True,
                                 "scan_output": True, "block_dangerous_args": True,
                                 "injection_block": False, "scan_input": False,
                                 "pii": False, "llm_classifier": False,
                                 "file_max_mb": 10, "allowed_image_types": [],
                                 "secret_patterns": [], "arg_denylist": [],
                                 "injection_phrases": []}}
    # Tool-node Extra Settings → app-wide config (emitted only when set, so a graph
    # with no per-tool overrides stays byte-identical). risk overrides already folded
    # into high_risk_tools/safe_tools above.
    if _rd_tools:
        gen_config["return_direct_tools"] = sorted(set(_rd_tools))
    if _err_pol:
        gen_config["tool_error_policy"] = _err_pol
    if _desc_ov:
        gen_config["tool_descriptions"] = _desc_ov
    # code-exec settings (run_python backend): app-wide, taken from the first
    # code_exec agent (the runtime is one process). backend subprocess|docker|auto.
    _ce = next((graph.nodes[a] for a in pipeline_ids
                if graph.nodes[a].props.get("code_exec")), None)
    if _ce is not None:
        gen_config["code_exec"] = {
            "backend": _ce.props.get("code_exec_backend", "subprocess"),
            "timeout": int(_ce.props.get("code_exec_timeout", 30)),
            "memory_mb": int(_ce.props.get("code_exec_memory_mb", 512)),
            "image": _ce.props.get("code_exec_image", "python:3.11-slim")}
    # web search: emit an editable config.json block so the end user can choose an
    # engine + paste a key WITHOUT regenerating (keyless DuckDuckGo by default;
    # engine ∈ duckduckgo|tavily|serpapi|brave|bing|searxng; add `engines: [...]`
    # for a failover chain). No key is baked into the graph.
    if any(graph.nodes[a].props.get("web_search") for a in pipeline_ids):
        gen_config["web_search"] = {"engine": "duckduckgo", "api_key": "",
                                    "base_url": ""}
        # per-agent overrides (set in the Agent node dialog → Extra Settings → Web
        # search): each agent may pick its own engine/key/base_url/proxy, merged over
        # the global block above at runtime. Only non-blank fields are emitted, so an
        # untouched graph stays byte-identical (no web_search_by_agent key at all).
        _ws_by_agent = {}
        for a in pipeline_ids:
            n = graph.nodes[a]
            if not n.props.get("web_search"):
                continue
            block = {}
            for prop, key in (("web_search_engine", "engine"),
                              ("web_search_api_key", "api_key"),
                              ("web_search_base_url", "base_url"),
                              ("web_search_proxy", "proxy")):
                val = str(n.props.get(prop, "") or "").strip()
                if val:
                    block[key] = val
            if block:
                _ws_by_agent[n.name] = block
        if _ws_by_agent:
            gen_config["web_search_by_agent"] = _ws_by_agent
    # per-graph storage backend for memory (sessions) + checkpoints
    _storage = getattr(graph, "storage", None) or {}
    if mem_nodes:                     # default recall() size = the largest memory node's top_k
        gen_config["memory_top_k"] = max(int(m.props.get("top_k", 5) or 5) for m in mem_nodes)
        _mem_desc = "; ".join(d for d in (m.props.get("description", "").strip()
                                          for m in mem_nodes) if d)
        if _mem_desc:                 # routing hint -> woven into the remember/recall tool docs
            gen_config["memory_description"] = _mem_desc
    gen_config["storage"] = {
        "backend": (_storage.get("backend") or "disk"),
        "sqlite_path": (_storage.get("sqlite_path") or "memory.db"),
        "dsn": (_storage.get("dsn") or ""),
    }
    # graph-mode crash-recovery: a graph opts in via storage['checkpoint']=True.
    # Only meaningful with shared state (the runtime also gates on that); the run
    # then snapshots its cursor+state at each stage and resumes by thread_id.
    if _storage.get("checkpoint"):
        gen_config["checkpoint"] = True
    # Whole-run wall-clock deadline (seconds; 0/unset = none). Checked between react
    # steps + graph hops; on exceed the run stops with a [budget] result. Emitted only
    # when set, so graphs without it stay byte-identical.
    _rwc = int(getattr(graph, "run_wall_clock_s", 0) or 0)
    if _rwc > 0:
        gen_config["max_run_wall_clock_s"] = _rwc
    # per-agent LLM mode: only 'manual' is emitted (fallback is the default).
    # Runtime default; the generated GUI/llm_mode.json can override it live.
    llm_modes = {n.name: "manual" for n in graph.nodes.values()
                 if n.kind in AGENT_KINDS and n.props.get("llm_mode") == "manual"}
    if llm_modes:
        gen_config["llm_modes"] = llm_modes
    if rag_nodes:
        _single_rag = len(rag_nodes) == 1
        gen_config["rag"] = [{
            "name": r.name,
            "tool": rag_tool_name(r, _single_rag),
            "description": (r.props.get("description") or "").strip(),
            "docs_dir": r.props.get("docs_dir", ""),
            "chunk_strategy": r.props.get("chunk_strategy", "fixed"),
            "chunk_chars": int(r.props.get("chunk_chars", 800)),
            "chunk_overlap": int(r.props.get("chunk_overlap", 0)),
            # small-to-big / parent-child (§4.3): index small children, return the
            # big parent block. "chunk" = index & return the same piece (default).
            "retrieval_granularity": r.props.get("retrieval_granularity", "chunk"),
            "parent_chunk_chars": int(r.props.get("parent_chunk_chars", 2400)),
            "top_k": int(r.props.get("top_k", 4)),
            "recall_n": int(r.props.get("recall_n", 0)),
            "retrieval_algorithm": r.props.get("retrieval_algorithm", "bm25"),
            "mmr": bool(r.props.get("mmr", False)),
            "mmr_lambda": float(r.props.get("mmr_lambda", 0.5)),
            "rerank": {"mode": r.props.get("rerank_mode", "none"),
                       "model": (r.props.get("rerank_model") or "").strip()},
            "grade_docs": bool(r.props.get("grade_docs", False)),
            "corrective": bool(r.props.get("corrective", False)),
            "corrective_max_rewrites": int(r.props.get("corrective_max_rewrites", 2)),
            "query_transform": r.props.get("query_transform", "none"),
            "embedding": {"provider": r.props.get("embed_provider", "local"),
                          "model": (r.props.get("embed_model") or "").strip(),
                          "base_url": (r.props.get("embed_base_url") or "").strip(),
                          "api_key": (r.props.get("embed_api_key") or "").strip(),
                          "normalize": bool(r.props.get("normalize", True))},
            "vector_db": r.props.get("vector_db", "memory"),
        } for r in rag_nodes]
        # Extra Settings — emit conditionally so graphs that don't use them keep a
        # byte-identical config.json (multi_query_n only when the mode selects it).
        for _d, _r in zip(gen_config["rag"], rag_nodes):
            _thr = float(_r.props.get("score_threshold", 0) or 0)
            if _thr > 0:
                _d["score_threshold"] = _thr
            _mf = (_r.props.get("metadata_filter") or "").strip()
            if _mf:
                _d["metadata_filter"] = _mf
            if _d["query_transform"] == "multi_query":
                _d["multi_query_n"] = int(_r.props.get("multi_query_n", 3) or 3)
            if _d["vector_db"] == "qdrant":      # only a remote server needs these
                _qu = (_r.props.get("qdrant_url") or "").strip()
                if _qu:
                    _d["qdrant_url"] = _qu
                _qk = (_r.props.get("qdrant_api_key") or "").strip()
                if _qk:
                    _d["qdrant_api_key"] = _qk
    if agent_skills:                 # canvas Skills node(s) → runtime-managed
        gen_config["skills"] = agent_skills
    if eval_nodes:                   # canvas Eval node(s) → config['evals']
        evals = []
        for ev in eval_nodes:
            # keep usable cases; an empty set is still emitted so the GUI can
            # fill it in (the Eval node may be left empty in the canvas)
            cases = eval_cases(ev)
            tgt_ids = [e.dst for e in graph.edges if e.src == ev.id]
            target = (graph.nodes[tgt_ids[0]].name
                      if tgt_ids and tgt_ids[0] in graph.nodes else None)
            evals.append({"name": ev.name, "target": target, "cases": cases})
        gen_config["evals"] = evals
    if ws_node:
        gen_config["server"] = {
            "host": ws_node.props.get("host", "127.0.0.1"),
            "port": int(ws_node.props.get("port", 8765)),
            "auth_token": ws_node.props.get("auth_token", ""),
            "auto_allow_tools": bool(ws_node.props.get("auto_allow_tools", False)),
        }
        # Extra Settings — emit only when set (byte-identical otherwise).
        if ws_node.props.get("autostart"):       # gui.py: start server on launch
            gen_config["server"]["autostart"] = True
        for _k in ("tls_cert", "tls_key"):
            if ws_node.props.get(_k):
                gen_config["server"][_k] = ws_node.props[_k]
        _orig = ws_node.props.get("allowed_origins") or []
        if _orig:
            gen_config["server"]["allowed_origins"] = _orig
        _mc = int(ws_node.props.get("max_connections", 0) or 0)
        if _mc:
            gen_config["server"]["max_connections"] = _mc
        reqs.append("websockets>=13.0")
    if sched_nodes:                   # scheduler.py reads its jobs from here (one per node)
        gen_config["schedules"] = [{
            "name": s.name,
            "target": sched_targets.get(s.id, ""),          # agent this job drives (A)
            "mode": (s.props.get("mode") or "interval"),   # interval | daily | once
            "task": (s.props.get("initial_task") or "").strip(),
            "every_seconds": int(s.props.get("every_seconds", 3600) or 3600),
            "offset_seconds": int(s.props.get("offset_seconds", 0) or 0),
            "session_id": (s.props.get("session_id") or "").strip(),
            "max_runs": int(s.props.get("max_runs", 0) or 0),
            "run_at_start": bool(s.props.get("run_at_start", True)),
            "at": (s.props.get("at") or "").strip(),            # daily HH:MM[:SS]
            "start_at": (s.props.get("start_at") or "").strip(),  # absolute datetime
        } for s in sched_nodes]
    # While-node per-loop caps → app-wide config (emitted only when any set).
    _wmax = {w.name: int(w.props.get("max_iterations", 0) or 0)
             for w in graph.nodes.values()
             if w.kind == "while" and int(w.props.get("max_iterations", 0) or 0) > 0}
    if _wmax:
        gen_config["while_max_iterations"] = _wmax
    if mcp_nodes:
        gen_config["mcp_servers"] = [_mcp_server_config(m) for m in mcp_nodes]
        readme += (
            "\n## MCP clients\n\nThis agent connects to MCP server(s) at "
            "startup and uses their tools:\n"
            + "".join(
                f"- {m.name}: {m.props.get('transport')} "
                + (m.props.get('command', '') if m.props.get('transport') == 'stdio'
                   else m.props.get('url', '')) + "\n"
                for m in mcp_nodes)
            + "Edit config.json -> mcp_servers to change them.\n")
    if code_style == "package":
        # Split the runtime into runtime/_core.py + one module per feature +
        # a thin agent.py engine. Reuses the SAME substitution lists the single-
        # file path validated above, so the two styles can't diverge.
        _emit_package(out_dir, _apply(PIPELINE_TEMPLATE, _nonfrag),
                      dict(_frag), _user_text, name)
    else:
        _write(out_dir, "agent.py", agent_src)
    _write(out_dir, "config.json", json.dumps(gen_config, indent=2))
    # regeneration is authoritative from the canvas: drop any stale runtime
    # overrides so the freshly-designed skills / eval sets take effect.
    for _stale_name in ("skills.json", "evals.json"):
        _stale = os.path.join(out_dir, _stale_name)
        if os.path.exists(_stale):
            os.remove(_stale)
    _write(out_dir, "requirements.txt", "\n".join(reqs) + "\n")
    _write(out_dir, "build.bat", build_bat(name, gui))
    _write(out_dir, "README.md", readme)
    write_evals(out_dir, name)
    if ws_node:
        write_server(out_dir, name)
    if sched_nodes:
        write_scheduler(out_dir, name)
    if gui:
        write_gui(out_dir, name, custom_gui_src)
    # debugging aid: each agent's resolved system prompt, next to config.json
    _roles = {graph.nodes[aid].name:
              (graph.nodes[aid].props.get("role") or graph.nodes[aid].kind)
              for aid in pipeline_ids}
    _write(out_dir, "system_prompts.json", json.dumps(
        _prompt_dump(agents_spec, agent_skills, _roles),
        indent=2, ensure_ascii=False))
    return out_dir


def system_prompts_for_graph(graph: Graph) -> dict:
    """Resolve every canvas agent's system prompt WITHOUT generating the app —
    returns the same structure written to system_prompts.json. Used by the
    canvas 'Dump System Prompts' button. Raises ValueError with the analyzer's
    errors if the graph isn't ready (same bar as generation)."""
    info = analyze(graph)
    if info["errors"]:
        raise ValueError("\n".join(info["errors"]))
    graph, hitl_gates = _splice_hitl(graph)
    pipeline_ids = info["pipeline"]
    revise_edge = info["revise_edge"]
    agents_spec, _llm, _tools, agent_skills, _providers = _build_agent_specs(
        graph, pipeline_ids, revise_edge, hitl_gates)
    roles = {graph.nodes[aid].name:
             (graph.nodes[aid].props.get("role") or graph.nodes[aid].kind)
             for aid in pipeline_ids}
    return _prompt_dump(agents_spec, agent_skills, roles)
