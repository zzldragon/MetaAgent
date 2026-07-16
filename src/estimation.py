"""Estimation: read-only design checks surfaced via the canvas 'Estimation' menu
(Estimate Prompts / Graph / Tool / All).

Phase 0 ships the shared report primitive and a fully DETERMINISTIC 'Estimate
Graph' built on design_assistant.design_review() (which itself wraps
graph_codegen.analyze() + graph_metrics()). Later phases add an LLM-judge layer
for prompt-contradiction / tool-quality / pattern-fit checks — but the report
shape here is the common currency for all of them.

Nothing in this module mutates the graph or touches Qt, so it is unit-testable
headless. The Qt rendering lives in canvas_qt/estimation_ui.py.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

# Ordered most- to least-severe: drives sorting and the summary line.
SEVERITIES = ("error", "warning", "info")


@dataclass
class Finding:
    severity: str                 # one of SEVERITIES
    message: str
    target: str = "graph"         # node/agent/tool name this is about, or "graph"
    source: str = "deterministic"  # "deterministic" | "llm"
    detail: str = ""              # optional secondary line (evidence / explanation)


@dataclass
class EstimationReport:
    title: str
    findings: list = field(default_factory=list)
    summary: str = ""
    # Optional streaming hook: called with each Finding the moment it is added, so
    # a UI can render results bit-by-bit instead of waiting for the whole run.
    on_add: object = None

    def _push(self, f: "Finding") -> None:
        self.findings.append(f)
        if self.on_add is not None:
            self.on_add(f)

    def add(self, severity: str, message: str, *, target: str = "graph",
            source: str = "deterministic", detail: str = "") -> None:
        self._push(Finding(severity, message, target, source, detail))

    def extend(self, other: "EstimationReport") -> None:
        for f in other.findings:
            self._push(f)

    def add_judge(self, jr: "JudgeResult") -> "EstimationReport":
        """Fold an LLM JudgeResult into this report: its findings on success, or
        a single info note explaining why the LLM layer was skipped/failed (so
        the deterministic findings still stand and the user knows the LLM pass
        didn't run). Each folded finding streams via on_add."""
        if jr.ok:
            for f in jr.findings:
                self._push(f)
        elif jr.note:
            self.add("info", jr.note, source="llm")
        return self

    def counts(self) -> dict:
        c = {s: 0 for s in SEVERITIES}
        for f in self.findings:
            c[f.severity] = c.get(f.severity, 0) + 1
        return c

    def sorted_findings(self) -> list:
        order = {s: i for i, s in enumerate(SEVERITIES)}
        return sorted(self.findings, key=lambda f: order.get(f.severity, len(SEVERITIES)))

    def finalize(self) -> "EstimationReport":
        c = self.counts()
        self.summary = (f"{c['error']} error(s), {c['warning']} warning(s), "
                        f"{c['info']} note(s).")
        return self


def estimate_graph(graph, *, emit=None, complete=None, cancel_event=None,
                   use_llm: bool = False) -> EstimationReport:
    """Graph estimate. Deterministic by default (analyze() errors/warnings +
    topology + cost-shape metrics). Pass use_llm=True (or an explicit `complete`
    backend) to also add a grounded LLM design critique (pattern fit, parallelism
    left on the table, over-engineering). LLM defaults OFF so programmatic callers
    and tests never make a surprise network call. `emit(finding)` streams each
    finding as it is produced."""
    import design_assistant as da

    rep = EstimationReport("Estimate Graph", on_add=emit)
    review = da.design_review(graph)

    for e in review["errors"]:
        rep.add("error", e, target="graph")
    for w in review["warnings"]:
        rep.add("warning", w, target="graph")

    topo = review["topology"]
    m = review["metrics"]
    pipeline = " → ".join(topo["pipeline"]) if topo["pipeline"] else "(none)"
    rep.add("info",
            f"Mode: {topo['mode']}  ·  entry: {topo['entry']}  ·  pipeline: {pipeline}",
            target="graph")
    totals = m["budgets"]["totals"]
    rep.add("info",
            f"Agents: {m['agent_count']}  ·  min agent invocations: "
            f"{m['min_agent_invocations']}  ·  budget totals: "
            f"iters={totals['max_iterations']}, tool_calls={totals['max_tool_calls']}, "
            f"out_tokens={totals['max_output_tokens']}",
            target="graph")

    par = m["parallelism"]
    if par["worker_pools"]:
        rep.add("info", "Worker pool fan-out: " + ", ".join(par["worker_pools"]),
                target="graph")
    if par["orchestrator"]:
        rep.add("info", "Orchestrator present — spawns sub-agents in parallel.",
                target="graph")

    loops = m["loops"]
    if loops["revise_loop"] or loops["while_nodes"]:
        rep.add("info",
                f"Loops present (revise={loops['revise_loop']}, "
                f"while_nodes={loops['while_nodes']}) — real invocations exceed the "
                "minimum above.", target="graph")

    # Phase 4: grounded LLM design critique on top of the deterministic metrics.
    if _llm_enabled(complete, use_llm):
        rep.add_judge(judge(GRAPH_RUBRIC, "graph", _graph_artifact(graph),
                            complete=(complete or _default_complete),
                            cancel_event=cancel_event))
    elif use_llm:
        rep.add("info", "LLM graph review skipped — no API key set "
                "(Settings → API Key / Model).", source="llm")

    return rep.finalize()


# ── Phase 1: the grounded LLM-judge harness ──────────────────────────────────
# A single reusable one-shot judge that the LLM-backed estimates (prompts / tool
# / graph interpretation) share. It is GROUNDED — it reviews the real artifact
# text you pass, asks for a strict JSON array of findings, and NEVER fabricates a
# graph it can't see. It degrades to a note (never crashes) when there is no API
# key, on a network/parse error, or on cancel — so the deterministic layer always
# stands on its own. LLM plumbing (openai SDK, config) is imported lazily inside
# _default_complete, so importing this module stays cheap.

_JUDGE_SYSTEM = (
    "You are a meticulous reviewer of AI-agent designs. You are given ONE "
    "artifact and a rubric. Report only genuine, specific issues you can point "
    "to in the artifact — do not invent problems and do not restate the rubric. "
    "If the artifact is fine, return an empty array. Respond with ONLY a JSON "
    'array of objects, each {"severity": "error"|"warning"|"info", "message": '
    '"<one concise sentence>", "detail": "<short evidence/why, optional>"}. '
    "No prose, no markdown fence."
)


class EstimationCancelled(Exception):
    """Raised by a completion backend when the run was cancelled mid-flight."""


@dataclass
class JudgeResult:
    ok: bool                       # did the LLM run and return parseable findings?
    findings: list = field(default_factory=list)   # list[Finding], source="llm"
    note: str = ""                 # why it was skipped/failed (shown to the user)


def llm_available() -> bool:
    """True if an LLM API key is configured — the SAME key the Tool Generator uses
    (config.json 'api_key'). No key => the LLM layer is skipped and only the
    deterministic checks run."""
    from app_config import load_config
    return bool((load_config().get("api_key") or "").strip())


def _default_complete(messages: list, cancel_event=None) -> str:
    """Real backend: one chat completion via llm_client + config.json (the coding
    agent's settings). Streams when a cancel_event is given so a Stop can abort
    it; raises EstimationCancelled if aborted."""
    from app_config import load_config
    from llm_client import CANCELLED, LLMClient

    cfg = load_config()
    client = LLMClient(
        api_key=cfg["api_key"], base_url=cfg["base_url"],
        model=cfg["model"], request_timeout_s=cfg.get("request_timeout_s") or 120,
        proxy=cfg.get("proxy") or None)
    should_cancel = cancel_event.is_set if cancel_event is not None else None
    msg = client.chat(messages, max_tokens=2048, should_cancel=should_cancel)
    if msg is CANCELLED:
        raise EstimationCancelled()
    return getattr(msg, "content", "") or ""


def _build_messages(rubric: str, target_label: str, artifact_text: str) -> list:
    user = (f"# Rubric\n{rubric.strip()}\n\n"
            f"# Artifact to review — {target_label}\n"
            "```\n" + (artifact_text or "").strip() + "\n```")
    return [{"role": "system", "content": _JUDGE_SYSTEM},
            {"role": "user", "content": user}]


def _extract_json_array(text: str):
    """Best-effort: parse a JSON array from the model's reply — the whole string,
    a ```json fenced block, or the first [...] span. None if nothing parses."""
    text = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(\[.*\])\s*```", text, re.DOTALL)
    cand = m.group(1) if m else None
    if cand is None:
        i, j = text.find("["), text.rfind("]")
        cand = text[i:j + 1] if 0 <= i < j else text
    try:
        obj = json.loads(cand)
    except Exception:
        return None
    return obj if isinstance(obj, list) else None


def _parse_findings(raw: str, target_label: str) -> list | None:
    arr = _extract_json_array(raw)
    if arr is None:
        return None
    out = []
    for item in arr:
        if not isinstance(item, dict):
            continue
        message = str(item.get("message", "")).strip()
        if not message:
            continue
        sev = item.get("severity")
        if sev not in SEVERITIES:
            sev = "info"
        out.append(Finding(sev, message, target=target_label, source="llm",
                           detail=str(item.get("detail", "")).strip()))
    return out


def judge(rubric: str, target_label: str, artifact_text: str, *,
          complete=None, cancel_event=None, max_findings: int = 20) -> JudgeResult:
    """Grounded one-shot LLM judgement of a single artifact against a rubric.

    Returns a JudgeResult — ok=True with parsed Findings (source='llm'), or
    ok=False with a human-readable note when: no API key is set, the call errors,
    it is cancelled, or the reply is unparseable. Never raises, never fabricates.

    `complete` is the completion backend `(messages, cancel_event) -> str`;
    defaults to the real llm_client backend. Tests inject a fake to run offline.
    """
    if complete is None:
        if not llm_available():
            return JudgeResult(False, [], "LLM check skipped — no API key set "
                               "(Settings → API Key / Model).")
        complete = _default_complete
    messages = _build_messages(rubric, target_label, artifact_text)
    try:
        raw = complete(messages, cancel_event)
    except EstimationCancelled:
        return JudgeResult(False, [], "LLM check cancelled.")
    except Exception as e:  # network/timeout/SDK — degrade, don't crash the panel
        return JudgeResult(False, [], f"LLM check failed: {e}")
    findings = _parse_findings(raw, target_label)
    if findings is None:
        return JudgeResult(False, [], "LLM returned unparseable output.")
    return JudgeResult(True, findings[:max_findings])


def _llm_enabled(complete, use_llm: bool) -> bool:
    """Whether to run the LLM layer: an explicit backend (tests/caller) always
    wins; otherwise only when requested AND a key is configured."""
    if complete is not None:
        return True
    return bool(use_llm) and llm_available()


def _cancelled(cancel_event) -> bool:
    return cancel_event is not None and cancel_event.is_set()


# ── rubrics (the only hand-written prose; the judge grounds them in real text) ─
PROMPT_RUBRIC = (
    "Review this agent's SYSTEM PROMPT. Flag: (1) internal contradictions or "
    "mutually conflicting instructions; (2) ambiguity that would cause "
    "inconsistent behaviour; (3) instructions that fight the agent's role or its "
    "available tools; (4) missing critical guidance (output format, stop "
    "condition, how/when to use tools). Quote the offending text in 'detail'. "
    "Do not flag mere style preferences.")

CROSS_PROMPT_RUBRIC = (
    "You are given several agents' names, roles and prompt excerpts from ONE "
    "multi-agent system. Flag cross-agent problems: (1) two agents told to do the "
    "same job; (2) conflicting expectations about who produces or consumes what; "
    "(3) a handoff where the upstream and downstream prompts disagree on the "
    "format/content passed; (4) a needed step no agent owns. Name the agents in "
    "'detail'.")

TOOL_RUBRIC = (
    "Review this tool function's source. An LLM agent decides whether to call a "
    "tool from its docstring, so flag: (1) missing/unclear docstring; (2) "
    "parameters that aren't explained; (3) a name that doesn't match what the "
    "code does; (4) a docstring that over- or under-promises vs the code. Focus "
    "on whether an agent could correctly decide WHEN and HOW to call it.")

GRAPH_RUBRIC = (
    "You are given a deterministic summary of a multi-agent graph (mode, "
    "pipeline, metrics, warnings). Comment on DESIGN — do not restate the "
    "numbers: (1) does the pattern fit the apparent goal, or is it over-/"
    "under-engineered; (2) parallelism left on the table (independent stages run "
    "sequentially); (3) redundant or missing stages; (4) budget/cost risks. Give "
    "concrete, actionable suggestions only.")

SYNTHESIS_RUBRIC = (
    "You are given the full list of findings from an automated review of a "
    "multi-agent design. Identify the 3-5 MOST IMPORTANT things to fix first and "
    "why, and call out any pattern across the findings. Do NOT repeat every "
    "finding — prioritise. Each item's 'message' is the recommendation.")


def report_to_markdown(report) -> str:
    """Plain-Markdown rendering of a report (for Copy / Save / feeding a
    synthesis pass). Deterministic and headless."""
    lines = [f"# {report.title}", "", report.summary or "", ""]
    for f in report.sorted_findings():
        tgt = f" [{f.target}]" if f.target and f.target != "graph" else ""
        src = " (LLM)" if f.source == "llm" else ""
        lines.append(f"- **{f.severity.upper()}**{src}{tgt}: {f.message}")
        if f.detail:
            lines.append(f"  - {f.detail}")
    return "\n".join(lines) + "\n"


# ── Estimate Prompts (Phase 2) ───────────────────────────────────────────────
def _deterministic_prompt_findings(graph, rep: EstimationReport) -> None:
    seen: dict[str, list] = {}
    for a in graph.agents():
        pnodes = graph.inputs_of(a.id, "prompt")   # 'prompt' is a singleton input
        role = (a.props or {}).get("role", "single")
        if not pnodes:
            rep.add("info", f"'{a.name}' has no Prompt node — uses the '{role}' "
                    "role default template.", target=a.name)
            continue
        text = (pnodes[0].props.get("text") or "").strip()
        if not text:
            rep.add("warning", f"'{a.name}' has an empty prompt.", target=a.name)
            continue
        if len(text) < 20:
            rep.add("info", f"'{a.name}' prompt is very short ({len(text)} chars) "
                    "— it may under-specify the role.", target=a.name)
        approx_tokens = max(1, len(text) // 4)
        if approx_tokens > 1500:
            rep.add("info", f"'{a.name}' prompt is long (~{approx_tokens} tokens) "
                    "— it costs context on every call.", target=a.name)
        seen.setdefault(text, []).append(a.name)
    for names in seen.values():
        if len(names) > 1:
            rep.add("warning", "Identical prompt text on " + ", ".join(names)
                    + " — these agents aren't differentiated.", target=names[0])


def estimate_prompts(graph, *, emit=None, complete=None, cancel_event=None,
                     use_llm: bool = False) -> EstimationReport:
    """Estimate the system prompts. Deterministic checks (empty/short/long/dup/
    role-default) always; a grounded LLM contradiction & clarity judge per agent
    plus a cross-agent pass when use_llm (or an explicit backend) is given.
    `emit(finding)` streams each finding as it is produced."""
    rep = EstimationReport("Estimate Prompts", on_add=emit)
    _deterministic_prompt_findings(graph, rep)

    if _llm_enabled(complete, use_llm):
        cf = complete or _default_complete
        import graph_codegen as gc
        try:
            agents = gc.system_prompts_for_graph(graph).get("agents", {})
        except ValueError:
            rep.add("info", "LLM prompt review skipped — resolve the graph errors "
                    "first (see Estimate Graph).", source="llm")
            return rep.finalize()
        for name, spec in agents.items():
            if _cancelled(cancel_event):
                break
            rep.add_judge(judge(PROMPT_RUBRIC, f"agent:{name}",
                                spec.get("system_prompt", ""),
                                complete=cf, cancel_event=cancel_event))
        if len(agents) > 1 and not _cancelled(cancel_event):
            combined = "\n\n".join(
                f"## {n} (role: {s.get('role')})\n{(s.get('base_persona') or '')[:600]}"
                for n, s in agents.items())
            rep.add_judge(judge(CROSS_PROMPT_RUBRIC, "cross-prompt", combined,
                                complete=cf, cancel_event=cancel_event))
    elif use_llm:
        rep.add("info", "LLM prompt review skipped — no API key set "
                "(Settings → API Key / Model).", source="llm")
    return rep.finalize()


# ── Estimate Tool (Phase 3) ──────────────────────────────────────────────────
def _linked_tool_files(graph) -> dict:
    """{tool_file: [agent names]} for every Tools node linked to an agent."""
    from graph_model import tool_files
    out: dict[str, list] = {}
    for a in graph.agents():
        for tnode in graph.inputs_of(a.id, "tool"):
            for f in tool_files(tnode):
                out.setdefault(f, []).append(a.name)
    return out


def _tool_functions(fname: str) -> list:
    """[(name, docstring, source)] for top-level functions in tools/<fname>."""
    import ast
    import os
    from app_config import TOOLS_DIR
    try:
        src_text = open(os.path.join(TOOLS_DIR, fname), encoding="utf-8").read()
    except OSError:
        return []
    try:
        tree = ast.parse(src_text)
    except SyntaxError:
        return []
    lines = src_text.splitlines()
    out = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = (ast.get_docstring(node) or "").strip()
            start = (node.decorator_list[0].lineno - 1
                     if node.decorator_list else node.lineno - 1)
            end = getattr(node, "end_lineno", node.lineno)
            out.append((node.name, doc, "\n".join(lines[start:end])))
    return out


def estimate_tools(graph, *, emit=None, complete=None, cancel_event=None,
                   use_llm: bool = False) -> EstimationReport:
    """Estimate the linked tools. Deterministic checks (missing/thin docstrings,
    empty files, library files not linked) always; a grounded LLM quality judge
    per function when use_llm (or an explicit backend) is given. `emit(finding)`
    streams each finding as it is produced."""
    from codegen import list_tools

    rep = EstimationReport("Estimate Tool", on_add=emit)
    linked = _linked_tool_files(graph)
    if not linked:
        rep.add("info", "No tools are linked to any agent.", target="graph")

    for fname in sorted(linked):
        funcs = _tool_functions(fname)
        if not funcs:
            rep.add("warning", f"'{fname}' defines no top-level functions.",
                    target=fname)
        for fn, doc, _src in funcs:
            if not doc:
                rep.add("warning", f"tool '{fn}' ({fname}) has no docstring — the "
                        "agent can't tell when to call it.", target=fn)
            elif len(doc) < 15:
                rep.add("info", f"tool '{fn}' ({fname}) has a very thin docstring.",
                        target=fn)

    for fname in sorted(set(list_tools()) - set(linked)):
        rep.add("info", f"'{fname}' is in the tools/ library but not linked to any "
                "agent.", target=fname)

    if _llm_enabled(complete, use_llm):
        cf = complete or _default_complete
        for fname in sorted(linked):
            for fn, _doc, src in _tool_functions(fname):
                if _cancelled(cancel_event):
                    break
                rep.add_judge(judge(TOOL_RUBRIC, f"tool:{fn}", src[:2000],
                                    complete=cf, cancel_event=cancel_event))
    elif use_llm:
        rep.add("info", "LLM tool review skipped — no API key set "
                "(Settings → API Key / Model).", source="llm")
    return rep.finalize()


# ── Estimate All (Phase 5) ───────────────────────────────────────────────────
def _graph_artifact(graph) -> str:
    import design_assistant as da
    r = da.design_review(graph)
    m, t = r["metrics"], r["topology"]
    lines = [
        f"mode: {t['mode']}", f"entry: {t['entry']}",
        "pipeline: " + (" -> ".join(t["pipeline"]) or "(none)"),
        f"agents: {m['agent_count']}",
        f"min_agent_invocations: {m['min_agent_invocations']}",
        f"budget_totals: {m['budgets']['totals']}",
        f"worker_pools: {m['parallelism']['worker_pools']}",
        f"orchestrator: {m['parallelism']['orchestrator']}",
        f"loops: {m['loops']}",
        "warnings:", *[f"  - {w}" for w in r["warnings"]],
        "errors:", *[f"  - {e}" for e in r["errors"]],
    ]
    return "\n".join(lines)


def estimate_all(graph, *, emit=None, complete=None, cancel_event=None,
                 use_llm: bool = False) -> EstimationReport:
    """Run Prompts + Graph + Tool and merge into one report, streaming each
    finding (tagged with its area) as it is produced, then a holistic synthesis
    pass. `emit(finding)` receives every finding live."""
    rep = EstimationReport("Estimate All", on_add=emit)
    subs = [
        ("Prompts", estimate_prompts),
        ("Graph", estimate_graph),
        ("Tool", estimate_tools),
    ]

    def area_emit(area, f):
        # Tag with the source area, collect into the merged report, and stream out.
        f.detail = (f"[{area}] " + f.detail) if f.detail else f"[{area}]"
        rep._push(f)

    for area, fn in subs:
        if _cancelled(cancel_event):
            break
        # The sub-estimate streams into area_emit (its own report is discarded);
        # area_emit tags + forwards each finding to rep (and thus to emit).
        fn(graph, emit=lambda f, a=area: area_emit(a, f),
           complete=complete, cancel_event=cancel_event, use_llm=use_llm)

    # Holistic synthesis: one pass over all findings to surface the top issues.
    if _llm_enabled(complete, use_llm) and rep.findings and not _cancelled(cancel_event):
        rep.add_judge(judge(SYNTHESIS_RUBRIC, "overall", report_to_markdown(rep),
                            complete=(complete or _default_complete),
                            cancel_event=cancel_event))
    return rep.finalize()


# ── Phase 6: LLM-proposed fixes (HITL + post-apply analyze() self-check) ──────
# A finding can be turned into a concrete graph edit the designer approves. We
# keep this DELIBERATELY narrow and safe: the only fix op is "set_prompt" (rewrite
# the agent's system-prompt text), applied through the real graph API, and every
# apply re-runs analyze() and REVERTS if it introduced a new error. No free-form
# code, no arbitrary structural mutation.

FIX_RUBRIC = (
    "Rewrite this agent's SYSTEM PROMPT to resolve ALL the listed issue(s) at "
    "once. Preserve the agent's role and intent; change as little as possible. "
    'Respond with ONLY a JSON object: {"text": "<the full rewritten prompt>", '
    '"rationale": "<one sentence on what you changed>"}. No prose, no code fence.')

TOOL_FIX_RUBRIC = (
    "Write a clear, accurate docstring for this Python tool function so an LLM "
    "agent knows WHEN and HOW to call it (what it does, its parameters, what it "
    "returns), resolving the listed issue(s). Base it strictly on the code — do "
    "not invent behaviour. Respond with ONLY a JSON object: "
    '{"docstring": "<the docstring text, no surrounding quotes>", "rationale": '
    '"<one sentence>"}. No prose, no code fence.')


@dataclass
class FixProposal:
    op: str                # "set_prompt" (agent prompt) | "set_docstring" (tool func)
    target: str            # agent node name, or tool function name
    before: str            # current text ("" if none)
    after: str             # proposed text
    rationale: str = ""
    meta: dict = field(default_factory=dict)   # op-specific (e.g. tool file path)


def _extract_json_object(text: str):
    """Parse a single JSON object from a reply (whole string / ```json fence /
    first {...} span). None if nothing parses to a dict."""
    text = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    cand = m.group(1) if m else None
    if cand is None:
        i, j = text.find("{"), text.rfind("}")
        cand = text[i:j + 1] if 0 <= i < j else text
    try:
        obj = json.loads(cand)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _agent_by_name(graph, name):
    return next((a for a in graph.agents() if a.name == name), None)


def is_prompt_fixable(graph, finding) -> bool:
    """A finding is prompt-fixable when it resolves to an agent AND is about that
    agent's prompt (an LLM prompt-judge finding, or a deterministic one whose
    message mentions the prompt)."""
    name = finding.target.split(":", 1)[-1] if finding.target else ""
    if _agent_by_name(graph, name) is None:
        return False
    return finding.target.startswith("agent:") or "prompt" in finding.message.lower()


def fixable_by_agent(graph, findings) -> dict:
    """Group the prompt-fixable findings by their agent name (skips non-fixable).
    Used to rewrite each agent's prompt ONCE for all its issues (no clobber)."""
    out: dict = {}
    for f in findings:
        if is_prompt_fixable(graph, f):
            out.setdefault(f.target.split(":", 1)[-1], []).append(f)
    return out


def propose_agent_fix(graph, agent_name, findings, *, complete=None, cancel_event=None):
    """Rewrite ONE agent's prompt to resolve ALL the given findings in a single
    edit — avoids the clobber of proposing one rewrite per finding (later applies
    would overwrite earlier ones). Returns a FixProposal(op='set_prompt') or None
    (no agent / no findings / no LLM / parse fail / no actual change)."""
    agent = _agent_by_name(graph, agent_name)
    if agent is None or not findings:
        return None
    if complete is None:
        if not llm_available():
            return None
        complete = _default_complete
    pnodes = graph.inputs_of(agent.id, "prompt")   # 'prompt' is a singleton input
    before = (pnodes[0].props.get("text") if pnodes else "") or ""
    role = (agent.props or {}).get("role", "single")
    issues = "\n".join(f"- {f.message}" + (f" — {f.detail}" if f.detail else "")
                       for f in findings)
    artifact = (f"Agent: {agent_name} (role: {role})\n"
                f"Issues to resolve:\n{issues}\n\n"
                "Current prompt:\n"
                + (before or "(none — uses the role default template)"))
    messages = [
        {"role": "system", "content":
            "You fix issues in AI-agent system prompts. Respond with ONLY a JSON "
            "object; change as little as possible while resolving EVERY listed issue."},
        {"role": "user", "content": FIX_RUBRIC + "\n\n" + artifact},
    ]
    try:
        raw = complete(messages, cancel_event)
    except EstimationCancelled:
        return None
    except Exception:
        return None
    obj = _extract_json_object(raw)
    after = str((obj or {}).get("text", "")).strip()
    if not after or after == before.strip():
        return None
    return FixProposal("set_prompt", agent_name, before, after,
                       str((obj or {}).get("rationale", "")).strip())


def propose_fix(graph, finding, *, complete=None, cancel_event=None):
    """Single-finding convenience wrapper over propose_agent_fix()."""
    if not is_prompt_fixable(graph, finding):
        return None
    return propose_agent_fix(graph, finding.target.split(":", 1)[-1], [finding],
                             complete=complete, cancel_event=cancel_event)


# ── tool fixes: rewrite a tool function's docstring ──────────────────────────
def _find_tool_func(graph, func_name):
    """Locate a linked tool function by name → {file, path, name, doc, src} | None."""
    import os
    from app_config import TOOLS_DIR
    for fname in _linked_tool_files(graph):
        for name, doc, src in _tool_functions(fname):
            if name == func_name:
                return {"file": fname, "path": os.path.join(TOOLS_DIR, fname),
                        "name": name, "doc": doc, "src": src}
    return None


def is_tool_fixable(graph, finding) -> bool:
    """A finding is tool-fixable when its target resolves to a linked tool
    function (whose docstring we can rewrite). Unused-file findings (target = a
    filename) are not — they don't name a function."""
    name = finding.target.split(":", 1)[-1] if finding.target else ""
    return _find_tool_func(graph, name) is not None


def propose_tool_fix(graph, func_name, findings, *, complete=None, cancel_event=None):
    """Rewrite ONE tool function's docstring to resolve its findings. Returns a
    FixProposal(op='set_docstring', meta={path,file}) or None."""
    info = _find_tool_func(graph, func_name)
    if info is None or not findings:
        return None
    if complete is None:
        if not llm_available():
            return None
        complete = _default_complete
    issues = "\n".join(f"- {f.message}" + (f" — {f.detail}" if f.detail else "")
                       for f in findings)
    artifact = (f"Function: {func_name}  (tools/{info['file']})\n"
                f"Issues:\n{issues}\n\nSource:\n{info['src'][:2000]}")
    messages = [
        {"role": "system", "content":
            "You improve Python tool docstrings so an LLM agent can call the tool "
            "correctly. Respond with ONLY a JSON object."},
        {"role": "user", "content": TOOL_FIX_RUBRIC + "\n\n" + artifact},
    ]
    try:
        raw = complete(messages, cancel_event)
    except EstimationCancelled:
        return None
    except Exception:
        return None
    obj = _extract_json_object(raw)
    after = str((obj or {}).get("docstring", "")).strip()
    if not after or after == (info["doc"] or "").strip():
        return None
    return FixProposal("set_docstring", func_name, info["doc"] or "", after,
                       str((obj or {}).get("rationale", "")).strip(),
                       meta={"path": info["path"], "file": info["file"]})


# ── unified proposer: prompts (per agent) + tools (per function) ─────────────
def is_fixable(graph, finding) -> bool:
    return is_prompt_fixable(graph, finding) or is_tool_fixable(graph, finding)


def propose_fixes(graph, findings, *, complete=None, cancel_event=None) -> list:
    """All fix proposals for a set of findings: one prompt rewrite per agent and
    one docstring rewrite per tool function. Skips anything unfixable."""
    out = []
    for agent, fs in fixable_by_agent(graph, findings).items():
        if _cancelled(cancel_event):
            return out
        p = propose_agent_fix(graph, agent, fs, complete=complete, cancel_event=cancel_event)
        if p:
            out.append(p)
    tools: dict = {}
    for f in findings:
        if is_tool_fixable(graph, f):
            tools.setdefault(f.target.split(":", 1)[-1], []).append(f)
    for func, fs in tools.items():
        if _cancelled(cancel_event):
            return out
        p = propose_tool_fix(graph, func, fs, complete=complete, cancel_event=cancel_event)
        if p:
            out.append(p)
    return out


def _restore_graph(graph, snapshot: dict) -> None:
    """Restore a graph's contents in place from a to_dict() snapshot (keeps the
    live object identity + id counter; the designer must rebuild its scene)."""
    from graph_model import Graph
    g2 = Graph.from_dict(snapshot)
    graph.nodes = g2.nodes
    graph.edges = g2.edges
    graph.state_schema = g2.state_schema
    graph.recursion_limit = g2.recursion_limit
    graph.storage = g2.storage


def apply_fix(graph, proposal) -> tuple:
    """Apply a FixProposal with a self-check + auto-revert. Returns (ok, message).
    set_prompt: edits the graph (revert if analyze() gains an error — caller must
    rebuild its scene). set_docstring: edits the tool file (revert if it no longer
    compiles / the docstring didn't take)."""
    if proposal is None:
        return (False, "No fix.")
    if proposal.op == "set_prompt":
        return _apply_set_prompt(graph, proposal)
    if proposal.op == "set_docstring":
        return _apply_set_docstring(proposal)
    return (False, f"Unsupported fix op: {proposal.op}.")


def _apply_set_prompt(graph, proposal) -> tuple:
    import graph_codegen as gc

    agent = _agent_by_name(graph, proposal.target)
    if agent is None:
        return (False, f"Agent '{proposal.target}' not found.")
    snapshot = graph.to_dict()
    errors_before = len(gc.analyze(graph).get("errors", []))
    pnodes = graph.inputs_of(agent.id, "prompt")
    if pnodes:
        pnodes[0].props["text"] = proposal.after
    else:
        p = graph.new_node("prompt", agent.x - 160, agent.y + 90)
        p.props["text"] = proposal.after
        p.props["role"] = (agent.props or {}).get("role", "single")
        err = graph.add_edge(p.id, agent.id)
        if err:
            _restore_graph(graph, snapshot)
            return (False, f"Could not link a prompt node: {err}")
    if len(gc.analyze(graph).get("errors", [])) > errors_before:
        _restore_graph(graph, snapshot)
        return (False, "Reverted — the change would introduce a new graph error.")
    return (True, f"Updated {agent.name}'s prompt.")


def _rewrite_docstring(src: str, func_name: str, new_doc: str):
    """Return `src` with func_name's docstring replaced/inserted, or None if the
    function isn't found. Pure text surgery (no I/O) so it is unit-testable; the
    caller self-checks the result compiles + parses as a docstring."""
    import ast
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == func_name), None)
    if fn is None or not fn.body:
        return None
    lines = src.split("\n")
    body0 = fn.body[0]
    indent = " " * body0.col_offset
    doc = new_doc.replace('"""', "'''")             # don't let it close the literal
    dl = doc.split("\n")
    if len(dl) == 1:
        literal = [f'{indent}"""{dl[0]}"""']
    else:
        literal = ([f'{indent}"""{dl[0]}']
                   + [(indent + x) if x else "" for x in dl[1:]]
                   + [f'{indent}"""'])
    has_doc = (isinstance(body0, ast.Expr)
               and isinstance(getattr(body0, "value", None), ast.Constant)
               and isinstance(body0.value.value, str))
    start = body0.lineno - 1
    if has_doc:                                     # replace the existing docstring span
        new_lines = lines[:start] + literal + lines[body0.end_lineno:]
    else:                                           # insert before the first statement
        new_lines = lines[:start] + literal + lines[start:]
    return "\n".join(new_lines)


def _apply_set_docstring(proposal) -> tuple:
    import ast
    import os
    import py_compile

    path = proposal.meta.get("path")
    if not path or not os.path.isfile(path):
        return (False, "Tool file not found.")
    original = open(path, encoding="utf-8").read()
    new_src = _rewrite_docstring(original, proposal.target, proposal.after)
    if new_src is None:
        return (False, f"Could not locate function '{proposal.target}'.")
    with open(path, "w", encoding="utf-8") as f:
        f.write(new_src)
    ok = False
    try:                                            # self-check: compiles + is a docstring
        py_compile.compile(path, doraise=True)
        fn = next((n for n in ast.walk(ast.parse(new_src))
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and n.name == proposal.target), None)
        ok = fn is not None and bool((ast.get_docstring(fn) or "").strip())
    except Exception:
        ok = False
    if not ok:
        with open(path, "w", encoding="utf-8") as f:
            f.write(original)                       # revert
        return (False, "Reverted — the docstring edit didn't verify.")
    return (True, f"Updated docstring for {proposal.target}.")
