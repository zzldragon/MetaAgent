"""Node-configuration dialogs for the Qt canvas designer.

A faithful Qt (PySide6) port of canvas/dialogs.py. Each dialog exposes the same
contract as the wx version: build from a Node, and apply() writes the edited
values back to node.props (returning an error string, or None on success).
The backend helpers (graph_model, graph_codegen, codegen) are reused unchanged.
"""

from __future__ import annotations

import json
import os
import re

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

import codegen
import graph_codegen
from canvas_qt.theme import canvas_colors
from graph_model import (
    AGENT_KINDS,
    DEFAULT_BUDGETS,
    NODE_KINDS,
    RESERVED_STATE_NAMES,
    STATE_TYPES,
    Node,
    contract_fields,
    eval_cases,
    is_custom_type,
    load_mta,
    merge_policies_for,
    parse_skill_md,
    reducers_for_type,
    response_format_support,
    skill_items,
    state_fields,
    tool_files,
    type_json_schema,
    validate_type_defs,
)

# Mirrors designer_frame.PROVIDERS / PROVIDER_DEFAULTS (kept local so the Qt
# designer doesn't import the wx designer_frame module).
PROVIDERS = ["siliconflow", "deepseek", "openai", "gemini", "anthropic", "nvidia"]
PROVIDER_DEFAULTS = {
    "siliconflow": ("deepseek-ai/DeepSeek-V4-Flash", "https://api.siliconflow.cn/v1"),
    "deepseek": ("deepseek-chat", "https://api.deepseek.com"),
    "openai": ("gpt-4o", ""),
    "gemini": ("gemini-2.5-flash",
               "https://generativelanguage.googleapis.com/v1beta/openai/"),
    "anthropic": ("claude-opus-4-8", ""),
    # NVIDIA build.nvidia.com — OpenAI-compatible NIM endpoint (key = nvapi-…).
    "nvidia": ("meta/llama-3.1-70b-instruct", "https://integrate.api.nvidia.com/v1"),
}


# ── shared subtitle (mirrors canvas.dialogs._subtitle) ───────────────────────
def subtitle(node: Node) -> str:
    p = node.props
    if node.kind == "agent":
        return f"role: {p.get('role', 'single')}"
    if node.kind == "workerpool":
        return f"{p.get('role', 'worker')} ×{p.get('max_workers', 4)} parallel"
    if node.kind == "router":
        return "picks one branch (LLM)"
    if node.kind == "hitl":
        if p.get("default_route"):        # route mode (human-driven branch)
            return f"human branch · default={p['default_route']}"
        return f"human checkpoint · reject={p.get('on_reject', 'stop')}"
    if node.kind == "eval":
        return f"{len(eval_cases(node))} case(s) · link→1 agent, else whole"
    if node.kind == "gui":
        return "desktop GUI · link → entry agent"
    if node.kind == "llm":
        return p.get("model", "")
    if node.kind == "tool":
        files = tool_files(node)
        if not files:
            return "(no tools — configure!)"
        names = ", ".join(f[:-3] if f.endswith(".py") else f for f in files)
        return f"{len(files)}: {names}"[:34]
    if node.kind == "prompt":
        text = (p.get("text") or "").strip().splitlines()
        first = text[0][:18] if text else "(role template)"
        return f"{p.get('role', 'single')}: {first}"
    if node.kind == "skill":
        items = skill_items(node)
        if not items:
            return "(no skills — configure!)"
        return f"{len(items)}: {', '.join(s['name'] for s in items)}"[:34]
    if node.kind == "rag":
        docs = p.get("docs_dir") or ""
        return os.path.basename(docs.rstrip("\\/")) if docs else "(no folder — configure!)"
    if node.kind == "memory":
        return f"remember/recall · top-{p.get('top_k', 5)}"
    if node.kind == "schedule":
        return f"every {p.get('every_seconds', 3600)}s (ambient)"
    if node.kind == "webserver":
        lock = " 🔒" if p.get("auth_token") else ""
        return f"ws://{p.get('host', '?')}:{p.get('port', '?')}{lock}"
    if node.kind == "mcp":
        tr = p.get("transport", "stdio")
        if tr == "stdio":
            return "stdio: " + (p.get("command") or "(no command!)")
        return f"{tr}: " + (p.get("url") or "(no url!)")
    if node.kind == "while":
        cond = (p.get("condition") or "").strip()
        return f"while {cond}" if cond else "(set condition!)"
    if node.kind == "foreach":
        over = (p.get("over") or "").strip()
        n = p.get("max_parallel") or 0
        return (f"for each {over}" + (f" (≤{n})" if n else "")) if over else "(set list!)"
    if node.kind == "end":
        return "terminal · finishes the run early"
    if node.kind == "fanout":
        n = p.get("max_parallel") or 0
        return "parallel branches" + (f" (≤{n})" if n else "")
    if node.kind == "join":
        return f"join · merge {p.get('merge', 'concat')}"
    return ""


# ── built-in tools (framework-provided, gated per agent) ─────────────────────
# The runtime gives an agent these tools automatically based on its role/config
# (see graph_codegen). We surface them on the canvas so the user can see what a
# node provides — and what it COULD provide (available-to-enable). Descriptions
# mirror the runtime tool schemas; `enable` says how to switch an inactive one on.
BUILTIN_TOOLS = [
    {"name": "route_to", "short": "route",
     "desc": "Hand off to exactly ONE linked agent to do the next step.",
     "enable": "role = planner with self-routing (or use a Router node), and link 2+ agents"},
    {"name": "spawn_subagent", "short": "spawn",
     "desc": "Delegate a self-contained subtask to ONE isolated sub-agent; "
             "call it several times to run sub-agents in parallel.",
     "enable": "role = orchestrator, and link its sub-agents"},
    {"name": "write_todos", "short": "todos",
     "desc": "Keep a working checklist to plan multi-step work and show progress.",
     "enable": "tick 'Enable to-dos'"},
    {"name": "run_python", "short": "python",
     "desc": "Run a short Python script in an isolated process (cwd = workspace); "
             "always HITL-confirmed. Isolation, not a security sandbox.",
     "enable": "tick 'Code execution'"},
    {"name": "web_search", "short": "web",
     "desc": "Search the public web (DuckDuckGo) for external / recent facts; a "
             "network egress, HITL-confirmed by default.",
     "enable": "tick 'Web search'"},
    {"name": "read_offload", "short": "offload",
     "desc": "Offload large tool results to workspace files, with a read_offload "
             "tool to fetch the full text on demand.",
     "enable": "tick 'Offload large tool results'"},
]


def _builtin_applies(node: Node, name: str) -> bool:
    """Whether a built-in is even relevant to this node's kind (so a Router doesn't
    advertise run_python, or a worker pool spawn_subagent)."""
    if node.kind == "agent":
        return True                                   # a full agent can host any
    if node.kind == "router":
        return name == "route_to"
    if node.kind == "workerpool":
        return name == "write_todos"                  # the only built-in it can opt into
    return False


def _builtin_active(node: Node, name: str) -> bool:
    """Whether this built-in is active for the node given its current config.
    route_to/spawn_subagent ALSO need linked successor agents at generate time;
    this reflects the role/flags the node carries (a best-effort canvas hint)."""
    p = node.props
    role = p.get("role", "single")
    if name == "route_to":
        return node.kind == "router" or (role == "planner" and bool(p.get("route_self")))
    if name == "spawn_subagent":
        return role == "orchestrator"
    if name == "write_todos":
        return bool(p.get("enable_todos"))
    if name == "run_python":
        return bool(p.get("code_exec"))
    if name == "web_search":
        return bool(p.get("web_search"))
    if name == "read_offload":
        return bool(p.get("offload_results"))
    return False


def builtin_tools_for(node: Node) -> list[dict]:
    """Built-in tools relevant to this node, each as {name, short, desc, enable,
    active}. Empty for node kinds that host no built-in tools."""
    return [{**t, "active": _builtin_active(node, t["name"])}
            for t in BUILTIN_TOOLS if _builtin_applies(node, t["name"])]


def mcp_probe(url: str, verify_tls: bool) -> tuple[str, bool]:
    """Reachability probe for an MCP http/sse server URL (pure, copied from the
    wx dialogs). Any HTTP response means host/port/scheme/path are right."""
    import ssl
    import urllib.error
    import urllib.request
    ctx = None
    if url.lower().startswith("https"):
        ctx = ssl.create_default_context()
        if not verify_tls:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
    handlers = [urllib.request.ProxyHandler({})]
    if ctx is not None:
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    opener = urllib.request.build_opener(*handlers)
    try:
        opener.open(urllib.request.Request(url, method="GET"), timeout=6)
        return (f"Reachable — the server at\n{url}\nresponded (200 OK).", True)
    except urllib.error.HTTPError as e:
        return (f"Reachable — the server responded HTTP {e.code}.\n\nMCP servers "
                "usually reject a plain GET with 400/405/406, so the host / port "
                "/ scheme / path look correct.", True)
    except Exception as e:  # noqa: BLE001
        return (f"NOT reachable:\n{type(e).__name__}: {e}\n\nCheck the host/port, "
                "http:// vs https://, a proxy/VPN on localhost, and the path "
                "(streamable_http → /mcp, sse → /sse).", False)


# ── helpers ──────────────────────────────────────────────────────────────────
def _multiline(text: str = "", height: int = 0) -> QPlainTextEdit:
    w = QPlainTextEdit()
    w.setPlainText(text or "")
    if height:
        w.setMinimumHeight(height)
    return w


def _buttons(dialog: QDialog, layout: QVBoxLayout) -> None:
    bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    bb.accepted.connect(dialog.accept)
    bb.rejected.connect(dialog.reject)
    layout.addWidget(bb)


_WARN_COLOR = "#965A00"   # amber: a semantic warning (readable on both themes)


def _hint(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    # Source the muted-hint color from the active theme (matches the canvas's
    # _restyle_theme_labels convention); dialogs are modal/short-lived, so the
    # color at construction time is enough — no live-restyle wiring needed.
    lbl.setStyleSheet(f"color:{canvas_colors()['hint']}; font-size:11px;")
    return lbl


def _list_edit_row(*pairs) -> QHBoxLayout:
    """A button row for the Add/Edit/Remove list-editor dialogs, ending with a
    stretch. Variadic over (label, slot) pairs so ConditionDialog can add its
    extra up/down buttons through the same helper."""
    h = QHBoxLayout()
    for label, slot in pairs:
        b = QPushButton(label)
        b.clicked.connect(slot)
        h.addWidget(b)
    h.addStretch(1)
    return h


# ── HITL policy block (shared by agent / workerpool) ─────────────────────────
def _hitl_controls(dialog, node: Node) -> QWidget:
    box = QWidget()
    v = QVBoxLayout(box)
    v.setContentsMargins(0, 0, 0, 0)
    v.addWidget(QLabel("Human-in-the-loop (when HITL is on):"))

    r1 = QHBoxLayout()
    dialog.hitl_review = QCheckBox("Pause before this stage runs")
    dialog.hitl_review.setChecked(bool(node.props.get("hitl_review", False)))
    r1.addWidget(dialog.hitl_review)
    r1.addWidget(QLabel("on reject:"))
    dialog.hitl_on_reject = QComboBox()
    dialog.hitl_on_reject.addItems(["stop", "revise"])
    dialog.hitl_on_reject.setCurrentText(node.props.get("hitl_on_reject", "stop"))
    r1.addWidget(dialog.hitl_on_reject)
    r1.addStretch(1)
    v.addLayout(r1)

    triggers = node.props.get("hitl_triggers", ["high_risk_tool"])
    r2 = QHBoxLayout()
    r2.addWidget(QLabel("Also pause when:"))
    dialog.hitl_high_risk = QCheckBox("high-risk tool")
    dialog.hitl_high_risk.setChecked("high_risk_tool" in triggers)
    dialog.hitl_low_conf = QCheckBox("low confidence")
    dialog.hitl_low_conf.setChecked("low_confidence" in triggers)
    r2.addWidget(dialog.hitl_high_risk)
    r2.addWidget(dialog.hitl_low_conf)
    r2.addWidget(QLabel("confidence <"))
    dialog.hitl_threshold = QLineEdit(
        str(node.props.get("hitl_confidence_threshold", 0.6)))
    dialog.hitl_threshold.setMaximumWidth(60)
    r2.addWidget(dialog.hitl_threshold)
    r2.addStretch(1)
    v.addLayout(r2)
    return box


def _apply_hitl(dialog, node: Node) -> str | None:
    # Validate first, mutate node.props only once everything is valid — so an
    # invalid threshold doesn't leave the node partially updated.
    try:
        t = float(dialog.hitl_threshold.text().strip())
    except ValueError:
        return "Confidence threshold must be a number between 0 and 1."
    if not 0.0 <= t <= 1.0:
        return "Confidence threshold must be between 0 and 1."
    triggers = []
    if dialog.hitl_high_risk.isChecked():
        triggers.append("high_risk_tool")
    if dialog.hitl_low_conf.isChecked():
        triggers.append("low_confidence")
    node.props["hitl_review"] = dialog.hitl_review.isChecked()
    node.props["hitl_on_reject"] = dialog.hitl_on_reject.currentText()
    node.props["hitl_triggers"] = triggers
    node.props["hitl_confidence_threshold"] = t
    return None


def _apply_budgets(node: Node, budgets: dict[str, QLineEdit]) -> str | None:
    # Parse all fields first; only write once they all pass (no partial mutation).
    try:
        parsed = {key: int(ctrl.text().strip().replace("_", ""))
                  for key, ctrl in budgets.items()}
    except ValueError:
        return "Budgets must be whole numbers."
    if any(v < 0 for v in parsed.values()):
        return "Budgets can't be negative (0 = unlimited)."
    node.props.update(parsed)
    return None


# ── shared-state read/write selectors (agent / workerpool) ───────────────────
def _graph_of(parent):
    """The Graph behind a dialog's parent. The config dialogs are opened either
    with the CanvasWindow as parent (menu / add-linked) or the DesignerView
    (double-click), so check both."""
    g = getattr(parent, "graph", None)
    if g is not None:
        return g
    win = getattr(parent, "win", None)
    return getattr(win, "graph", None) if win is not None else None


def _field_checklist(fields, selected) -> QListWidget:
    sel = set(selected or [])
    lw = QListWidget()
    lw.setMaximumHeight(90)
    for name in fields:
        it = QListWidgetItem(name)
        it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
        it.setCheckState(Qt.Checked if name in sel else Qt.Unchecked)
        lw.addItem(it)
    return lw


def _state_io_controls(dialog, node: Node, graph) -> QWidget:
    """Per-agent shared-state read/write selectors. Lists the graph's declared
    state fields with check boxes; empty selection = the smallest-version
    default (read all / write via a fenced block). When no fields are declared,
    shows a pointer to the schema editor and binds nothing."""
    box = QWidget()
    v = QVBoxLayout(box)
    v.setContentsMargins(0, 0, 0, 0)
    fields = [f["name"] for f in state_fields(graph)] if graph is not None else []
    write_fields = [f for f in fields if f not in RESERVED_STATE_NAMES]
    dialog._require_writes = None
    if not fields:
        dialog._state_reads = dialog._state_writes = None
        v.addWidget(_hint("Shared state: no fields declared. Add them via "
                          "Graph → Shared State to let this agent read/write them."))
        return box
    v.addWidget(QLabel("Shared state — reads (injected into the prompt):"))
    dialog._state_reads = _field_checklist(fields, node.props.get("reads"))
    v.addWidget(dialog._state_reads)
    v.addWidget(_hint("tool_calls / agents are auto-maintained — tick them under "
                      "reads only to show them to this agent; they're never "
                      "written by hand."))
    dialog._state_writes = None
    if write_fields:
        v.addWidget(QLabel("Shared state — writes:"))
        dialog._state_writes = _field_checklist(write_fields,
                                                node.props.get("writes"))
        v.addWidget(dialog._state_writes)
        dialog._require_writes = QCheckBox(
            "Require: re-prompt the agent until it records these writes")
        dialog._require_writes.setChecked(bool(node.props.get("require_writes")))
        dialog._require_writes.setToolTip(
            "After the agent runs, if it didn't set every ticked field (via the "
            "set_state tool or a state block), re-prompt it to do so (bounded "
            "retries), then proceed. Best-effort; honored in chain/graph mode.")
        v.addWidget(dialog._require_writes)
    return box


def _apply_state_io(dialog, node: Node) -> None:
    for attr, key in (("_state_reads", "reads"), ("_state_writes", "writes")):
        lw = getattr(dialog, attr, None)
        if lw is None:
            continue
        node.props[key] = [lw.item(i).text() for i in range(lw.count())
                           if lw.item(i).checkState() == Qt.Checked]
    cb = getattr(dialog, "_require_writes", None)
    if cb is not None:
        node.props["require_writes"] = cb.isChecked()


# ── guardrails (deterministic content checks; per-agent overrides) ───────────
_GR_DEFAULTS = {"scan_tool_results": True, "scan_output": True,
                "block_dangerous_args": True, "pii": False,
                "injection_block": False, "scan_input": False,
                "llm_classifier": False}
_GR_ROWS = (
    ("scan_tool_results", "Scan tool results (redact secrets, block smuggled state)"),
    ("scan_output", "Scan output (redact secrets)"),
    ("block_dangerous_args", "Block dangerous tool args (rm -rf, DROP TABLE…)"),
    ("pii", "Redact PII (emails, card numbers)"),
    ("injection_block", "Block suspected injection in tool results (tripwire)"),
    ("scan_input", "Scan user input (weak — misses tool-fetched content)"),
    ("llm_classifier", "LLM safety check on output (advisory; injectable; +1 LLM call)"),
)


def _guardrail_controls(dialog, node: Node) -> QWidget:
    # A collapsible section (like Planner & routing / Capabilities / Budgets),
    # collapsed by default, so the dialog stays compact and consistent.
    g = _Collapsible("Guardrails (deterministic content checks)")
    cur = {**_GR_DEFAULTS, **(node.props.get("guardrails") or {})}
    dialog._gr = {}
    for key, label in _GR_ROWS:
        cb = QCheckBox(label)
        cb.setChecked(bool(cur.get(key)))
        dialog._gr[key] = cb
        g.span(cb)
    g.span(_hint("Filters content; does NOT reliably detect prompt injection "
                 "or sandbox tools. For dangerous actions, restrict tools and "
                 "keep HITL on. Patterns live in config.json → guardrails."))
    return g


def _apply_guardrail(dialog, node: Node) -> None:
    new = {k: cb.isChecked() for k, cb in dialog._gr.items()}
    prior = node.props.get("guardrails") or {}
    # leave it inheriting the global config unless something actually changed
    node.props["guardrails"] = {} if (not prior and new == _GR_DEFAULTS) else new


# ── dialogs ──────────────────────────────────────────────────────────────────
def _llm_mode_combo(node: Node) -> QComboBox:
    """A per-agent selector for how 2+ linked LLMs behave. 'fallback' (default)
    tries the others on error; 'manual' uses ONLY the selected LLM (no failover),
    so the choice is authoritative. Harmless for a single-LLM agent."""
    combo = QComboBox()
    combo.addItem("Fallback — try the other linked LLMs on error", "fallback")
    combo.addItem("Manual — use only the selected LLM (no fallback)", "manual")
    combo.setCurrentIndex(1 if node.props.get("llm_mode") == "manual" else 0)
    combo.setToolTip(
        "Only matters when an agent has 2+ linked LLMs. Manual makes the "
        "selected model authoritative — its errors surface instead of silently "
        "failing over. Switchable at runtime from the generated GUI's LLM menu.")
    return combo


class _Collapsible(QWidget):
    """A titled section shown as a clickable, full-width header BAR with a ▶/▾
    disclosure triangle; clicking it expands / collapses the body. Add rows via
    .row(label, widget) or .span(widget)."""

    _CSS = ("QToolButton { border: 1px solid palette(mid); border-radius: 4px;"
            " padding: 6px 8px; font-weight: bold; text-align: left;"
            " background: palette(button); }"
            "QToolButton:hover { background: palette(midlight); }"
            "QToolButton:pressed { background: palette(mid); }")

    def __init__(self, title: str, expanded: bool = False):
        super().__init__()
        self._title = title
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 3, 0, 3)
        v.setSpacing(3)
        self._btn = QToolButton()
        self._btn.setCheckable(True)
        self._btn.setChecked(expanded)
        self._btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)   # arrow + label
        self._btn.setCursor(Qt.PointingHandCursor)
        self._btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)  # full-width bar
        self._btn.setStyleSheet(self._CSS)
        self._btn.clicked.connect(self._sync)
        v.addWidget(self._btn)
        self._content = QWidget()
        self.form = QFormLayout(self._content)
        self.form.setContentsMargins(16, 2, 0, 4)
        v.addWidget(self._content)
        self._sync()

    def _sync(self) -> None:
        exp = self._btn.isChecked()
        self._content.setVisible(exp)
        # a native disclosure triangle makes the expand/collapse control obvious
        self._btn.setArrowType(Qt.DownArrow if exp else Qt.RightArrow)
        self._btn.setText("  " + self._title)

    def row(self, label: str, w) -> None:
        self.form.addRow(label, w)

    def span(self, w) -> None:
        self.form.addRow(w)


class DebugKeyDialog(QDialog):
    """Ask the DESIGNER for an LLM API key for a Debug Run only.

    The generated app's real end-users set their own key in its Settings — the
    designer isn't the agent's user — so we DON'T force a key at GUI-launch. But a
    Debug Run executes the graph here in the canvas, so it needs a working key.
    Offers a one-click 'Copy from coding agent' so you reuse the Tool Generator /
    Estimation key instead of retyping it."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("API key for Debug Run")
        v = QVBoxLayout(self)
        lab = QLabel(
            "This Debug Run executes the graph in the canvas, so it needs an LLM "
            "API key. It's used only for this debug run — the generated app's users "
            "set their own key in its Settings.")
        lab.setWordWrap(True)
        v.addWidget(lab)
        row = QHBoxLayout()
        self._key = QLineEdit()
        self._key.setPlaceholderText("sk-…  /  nvapi-…")
        self._key.setMinimumWidth(340)
        row.addWidget(self._key, 1)
        copy = QPushButton("Copy from coding agent")
        copy.setToolTip("Reuse the API key configured for the Tool Generator / "
                        "Estimation (the app's coding agent).")
        copy.clicked.connect(self._copy_coding_key)
        row.addWidget(copy)
        v.addLayout(row)
        self._hint = QLabel("")
        self._hint.setStyleSheet("color:#888; font-size:11px;")
        self._hint.setWordWrap(True)
        v.addWidget(self._hint)
        self._bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self._bb.accepted.connect(self.accept)
        self._bb.rejected.connect(self.reject)
        # OK stays disabled until a non-empty key is present — so accepting can never
        # silently bypass the gate with a blank key (cancel to run without a key).
        self._bb.button(QDialogButtonBox.Ok).setEnabled(False)
        self._key.textChanged.connect(
            lambda t: self._bb.button(QDialogButtonBox.Ok).setEnabled(bool(t.strip())))
        v.addWidget(self._bb)

    def _copy_coding_key(self) -> None:
        try:
            from app_config import load_config
            k = (load_config().get("api_key") or "").strip()
        except Exception:
            k = ""
        if k:
            self._key.setText(k)
            self._hint.setText("Copied the coding-agent key.")
        else:
            self._hint.setText("No coding-agent key is set yet (set one via the ⚙ "
                               "gear / Settings). Paste a key here instead.")

    def key(self) -> str:
        return self._key.text().strip()


class AgentDialog(QDialog):
    def __init__(self, parent, node: Node):
        super().__init__(parent)
        self.setWindowTitle(f"Configure agent: {node.name}")
        self.node = node
        self.resize(560, 620)
        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        v = QVBoxLayout(inner)
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)

        # ── identity (always visible) ────────────────────────────────────────
        top = QFormLayout()
        self.name = QLineEdit(node.name)
        top.addRow("Name:", self.name)
        self.role = QComboBox()
        self.role.addItems(["single", "planner", "worker", "critic",
                            "supervisor", "orchestrator"])
        self.role.setCurrentText(node.props.get("role", "single"))
        top.addRow("Role:", self.role)
        self.llm_mode = _llm_mode_combo(node)
        top.addRow("Multiple LLMs:", self.llm_mode)
        v.addLayout(top)

        # ── planner & routing (planner-role only; greyed otherwise) ──────────
        g = _Collapsible("Planner & routing  (planner role)")
        self.route_self = QCheckBox(
            "Self-route to one successor (needs 2+ outgoing agent links — no "
            "separate Router node)")
        self.route_self.setChecked(bool(node.props.get("route_self", False)))
        g.span(self.route_self)
        self.quick_response = QCheckBox(
            "Allow 'no branch' quick response (self-route only; answer directly "
            "and end, skipping workers — e.g. greetings)")
        self.quick_response.setChecked(bool(node.props.get("quick_response", False)))
        g.span(self.quick_response)
        self.structured_plan = QCheckBox(
            "Emit a typed dependency plan: a {id, subgoal, depends_on} DAG a "
            "downstream worker pool (with 'Dependency-aware execution') can run in "
            "parallel; falls back to free text.")
        self.structured_plan.setChecked(bool(node.props.get("structured_plan", False)))
        g.span(self.structured_plan)
        v.addWidget(g)

        # ── capabilities & tools ─────────────────────────────────────────────
        g = _Collapsible("Capabilities & tools")
        self.enable_todos = QCheckBox(
            "write_todos checklist tool (best for sustained multi-step work; "
            "maintains the built-in 'todos' state, shown live in Run trace)")
        self.enable_todos.setChecked(bool(node.props.get("enable_todos", False)))
        g.span(self.enable_todos)
        self.code_exec = QCheckBox(
            "run_python — write & run Python in an isolated subprocess (cwd = "
            "workspace), HITL-confirmed. Isolation, NOT a security sandbox.")
        self.code_exec.setChecked(bool(node.props.get("code_exec", False)))
        g.span(self.code_exec)
        self.code_exec_backend = QComboBox()
        self.code_exec_backend.addItems(["subprocess", "docker", "auto"])
        self.code_exec_backend.setCurrentText(
            node.props.get("code_exec_backend", "subprocess"))
        g.row("  ↳ exec backend:", self.code_exec_backend)
        self.code_exec_timeout = QLineEdit(str(node.props.get("code_exec_timeout", 30)))
        g.row("  ↳ timeout (s):", self.code_exec_timeout)
        self.code_exec_memory = QLineEdit(str(node.props.get("code_exec_memory_mb", 512)))
        g.row("  ↳ memory cap (MB):", self.code_exec_memory)
        self.code_exec_image = QLineEdit(
            node.props.get("code_exec_image", "python:3.11-slim"))
        g.row("  ↳ docker image:", self.code_exec_image)
        self.web_search = QCheckBox(
            "web_search — public-web lookup for external / recent facts (keyless "
            "DuckDuckGo by default; set engine + key in config.json → 'web_search': "
            "tavily / serpapi / brave / bing / searxng / baidu). Network egress; HITL by default.")
        self.web_search.setChecked(bool(node.props.get("web_search", False)))
        g.span(self.web_search)
        self.offload_results = QCheckBox(
            "Offload large tool results to workspace files — keep big results out "
            "of the context window (adds a read_offload tool to fetch the full text).")
        self.offload_results.setChecked(bool(node.props.get("offload_results", False)))
        g.span(self.offload_results)
        v.addWidget(g)

        # ── retrieval & answer quality ───────────────────────────────────────
        g = _Collapsible("Retrieval & answer quality")
        self.adaptive_retrieval = QCheckBox(
            "Adaptive retrieval (Adaptive-RAG) — decide first whether to search at "
            "all, then route to the best source. Needs a RAG or web_search tool.")
        self.adaptive_retrieval.setChecked(bool(node.props.get("adaptive_retrieval", False)))
        g.span(self.adaptive_retrieval)
        self.groundedness_check = QCheckBox(
            "Groundedness check (Self-RAG) — grade the final answer for grounding + "
            "answering the question, and revise if it falls short.")
        self.groundedness_check.setChecked(bool(node.props.get("groundedness_check", False)))
        g.span(self.groundedness_check)
        self.max_regen = QLineEdit(str(node.props.get("max_regen", 1)))
        self.max_regen.setPlaceholderText("regenerate attempts if the grade fails")
        g.row("  ↳ max revises:", self.max_regen)
        v.addWidget(g)

        # ── structured final answer ───────────────────────────────────────────
        g = _Collapsible("Structured final answer")
        g.span(QLabel("Force this agent's FINAL answer to a JSON Schema (validated, "
                      "with bounded re-ask). Blank = no constraint. Different from an "
                      "LLM node's response format, which shapes every call."))
        self.final_schema = _multiline(node.props.get("final_schema", ""), 90)
        self.final_schema.setPlaceholderText(
            '{"type":"object","properties":{"answer":{"type":"string"}},'
            '"required":["answer"]}')
        g.span(self.final_schema)
        self.final_schema_retries = QLineEdit(str(node.props.get("final_schema_retries", 2)))
        g.row("Max re-asks on mismatch:", self.final_schema_retries)
        v.addWidget(g)

        # ── budgets (0 = unlimited; set a real cap here yourself) ────────────
        g = _Collapsible("Budgets")
        self.budgets = {}
        for key in DEFAULT_BUDGETS:
            ctrl = QLineEdit(str(node.props.get(key, DEFAULT_BUDGETS[key])))
            ctrl.setToolTip("0 = unlimited. Set a cap to bound cost/runtime.")
            self.budgets[key] = ctrl
            g.row(key + ":", ctrl)
        g.span(_hint("Budgets default to 0 = UNLIMITED so demos run to completion. Set a "
                     "real cap (iterations / tool-calls / output-tokens / wall-clock "
                     "seconds) per agent to bound cost and runtime in production."))
        v.addWidget(g)

        # ── shared state / HITL (grouped boxes) + guardrails (collapsible) ───
        v.addWidget(_state_io_controls(self, node, _graph_of(parent)))
        v.addWidget(_hitl_controls(self, node))
        v.addWidget(_guardrail_controls(self, node))

        # ── Extra Settings (advanced / power-user knobs; blank = unchanged) ──
        xg = _Collapsible("Extra Settings")
        self.mode_label = QLineEdit(node.props.get("mode_label", ""))
        self.mode_label.setPlaceholderText("blank = single-pattern app")
        self.mode_label.setToolTip(
            "Tag this sub-pipeline's ENTRY agent so end-users can switch patterns "
            "at runtime with /mode <label> (multi-pattern apps). Blank = off.")
        xg.row("Mode label (multi-pattern /mode):", self.mode_label)
        self.max_rpm = QLineEdit(str(node.props.get("max_rpm", 0) or ""))
        self.max_rpm.setPlaceholderText("blank/0 = unlimited")
        xg.row("Max requests/min (RPM):", self.max_rpm)
        self.stage_retries = QLineEdit(str(node.props.get("stage_retries", 0) or ""))
        self.stage_retries.setPlaceholderText("blank/0 = no stage retry")
        xg.row("Stage retries (transient errors):", self.stage_retries)
        self.max_budget_usd = QLineEdit(str(node.props.get("max_budget_usd", 0) or ""))
        self.max_budget_usd.setPlaceholderText("0 = no cap (needs LLM prices set)")
        self.max_budget_usd.setToolTip(
            "Abort the run when the estimated cost reaches this many USD. Only bites "
            "when the agent's LLM node(s) have input/output prices set.")
        xg.row("Max budget ($):", self.max_budget_usd)
        self.on_budget = QComboBox()
        self.on_budget.addItems(["continue", "stop", "retry"])
        self.on_budget.setCurrentText(node.props.get("on_budget", "continue") or "continue")
        self.on_budget.setToolTip(
            "When this stage hits ANY budget cap (wall-clock / iterations / tool-calls "
            "/ cost): continue = pass the note downstream (default); stop = end the "
            "whole run; retry = re-run the stage up to 'Stage retries' times, then stop.")
        xg.row("On budget exceeded:", self.on_budget)
        self.compact_threshold = QLineEdit(str(node.props.get("compact_threshold", 85) or 85))
        self.compact_threshold.setPlaceholderText("85 = default")
        self.compact_threshold.setToolTip(
            "When the ENTRY agent's estimated context reaches this % of its usable "
            "window (set the LLM node's context_capacity), older turns are compacted "
            "into a summary. 1–100; 85 = default. Only the entry agent compacts.")
        xg.row("Context compact at (%):", self.compact_threshold)
        # ── web search (this agent): engine / key / base URL / proxy ─────────
        xg.span(_hint("Web search (this agent) — blank = inherit the config.json "
                      "'web_search' block. Only non-blank fields override it. Leave "
                      "the API key blank in a shared .mta (secret); fill it in "
                      "config.json on the target machine."))
        self.ws_engine = QComboBox()
        self.ws_engine.addItems(["", "duckduckgo", "tavily", "serpapi", "brave",
                                 "bing", "searxng", "baidu"])
        self.ws_engine.setCurrentText(node.props.get("web_search_engine", "") or "")
        self.ws_engine.setToolTip("blank = inherit config.json; else this agent's engine")
        xg.row("  ↳ search engine:", self.ws_engine)
        self.ws_api_key = QLineEdit(node.props.get("web_search_api_key", ""))
        self.ws_api_key.setPlaceholderText("blank = keyless / set in config.json (keep blank in a shared .mta)")
        xg.row("  ↳ API key:", self.ws_api_key)
        self.ws_base_url = QLineEdit(node.props.get("web_search_base_url", ""))
        self.ws_base_url.setPlaceholderText("engine-specific (SearXNG URL / Bing endpoint / SerpApi base)")
        xg.row("  ↳ base URL:", self.ws_base_url)
        self.ws_proxy = QLineEdit(node.props.get("web_search_proxy", ""))
        self.ws_proxy.setPlaceholderText("blank = config.json 'proxy' / env (a DIRECT connection is tried first)")
        xg.row("  ↳ proxy:", self.ws_proxy)
        v.addWidget(xg)
        v.addStretch(1)

        # role-aware enable/grey: routing/plan fields only for a planner; the
        # code-exec sub-fields only when code-exec is on; max-revises only when
        # the groundedness check is on.
        self.role.currentTextChanged.connect(lambda _t: self._refresh_enabled())
        self.route_self.toggled.connect(lambda _b: self._refresh_enabled())
        self.code_exec.toggled.connect(lambda _b: self._refresh_enabled())
        self.groundedness_check.toggled.connect(lambda _b: self._refresh_enabled())
        self.web_search.toggled.connect(lambda _b: self._refresh_enabled())
        self._refresh_enabled()

        _buttons(self, outer)      # OK/Cancel stay outside the scroll, always visible

    @staticmethod
    def _gate(w, on: bool) -> None:
        """Enable/disable a field AND explicitly grey its TEXT when disabled — a
        clearer 'not editable here' hint than the style's default dimming."""
        w.setEnabled(on)
        w.setStyleSheet("" if on else "color: #888;")

    def _refresh_enabled(self) -> None:
        # Role-gating: route_self / quick_response / structured_plan are the ONLY
        # role-specific fields — codegen emits routing (route_to) + the typed plan
        # ONLY for role=='planner', so they are a no-op for every other role and
        # are greyed with an explanation. Every capability (tools/retrieval/quality)
        # works for ALL roles (codegen gates them on their own flag, not the role),
        # so they stay editable — greying them would silently drop a valid config.
        role = self.role.currentText()
        planner = role == "planner"
        why = "" if planner else f"Planner role only (this agent is '{role}')."
        for w in (self.route_self, self.structured_plan):
            self._gate(w, planner)
            w.setToolTip(why)
        self._gate(self.quick_response, planner and self.route_self.isChecked())
        self.quick_response.setToolTip(
            why or ("" if self.route_self.isChecked()
                    else "Enable 'Self-route to one successor' first."))
        exec_on = self.code_exec.isChecked()          # capability gate, not a role gate
        for w in (self.code_exec_backend, self.code_exec_timeout,
                  self.code_exec_memory, self.code_exec_image):
            self._gate(w, exec_on)
        self._gate(self.max_regen, self.groundedness_check.isChecked())
        ws_on = self.web_search.isChecked()           # per-agent web-search knobs
        for w in (self.ws_engine, self.ws_api_key, self.ws_base_url, self.ws_proxy):
            self._gate(w, ws_on)

    def apply(self) -> str | None:
        err = _apply_budgets(self.node, self.budgets)
        if err:
            return err
        self.node.name = self.name.text().strip() or self.node.name
        self.node.props["role"] = self.role.currentText()
        self.node.props["route_self"] = self.route_self.isChecked()
        self.node.props["quick_response"] = self.quick_response.isChecked()
        self.node.props["enable_todos"] = self.enable_todos.isChecked()
        self.node.props["code_exec"] = self.code_exec.isChecked()
        self.node.props["code_exec_backend"] = self.code_exec_backend.currentText()
        try:
            self.node.props["code_exec_timeout"] = max(1, int(self.code_exec_timeout.text()))
            self.node.props["code_exec_memory_mb"] = max(64, int(self.code_exec_memory.text()))
        except ValueError:
            return "Code-exec timeout and memory cap must be integers."
        self.node.props["code_exec_image"] = (
            self.code_exec_image.text().strip() or "python:3.11-slim")
        self.node.props["web_search"] = self.web_search.isChecked()
        self.node.props["web_search_engine"] = self.ws_engine.currentText().strip()
        self.node.props["web_search_api_key"] = self.ws_api_key.text().strip()
        self.node.props["web_search_base_url"] = self.ws_base_url.text().strip()
        self.node.props["web_search_proxy"] = self.ws_proxy.text().strip()
        self.node.props["offload_results"] = self.offload_results.isChecked()
        self.node.props["adaptive_retrieval"] = self.adaptive_retrieval.isChecked()
        self.node.props["groundedness_check"] = self.groundedness_check.isChecked()
        try:
            self.node.props["max_regen"] = max(0, int(self.max_regen.text().strip() or 1))
        except ValueError:
            return "Max revises must be a whole number."
        self.node.props["structured_plan"] = self.structured_plan.isChecked()
        self.node.props["llm_mode"] = self.llm_mode.currentData()
        self.node.props["mode_label"] = self.mode_label.text().strip()
        # Extra Settings: RPM + stage retries (non-negative ints, blank = 0).
        try:
            self.node.props["max_rpm"] = max(0, int(self.max_rpm.text().strip() or 0))
            self.node.props["stage_retries"] = max(
                0, int(self.stage_retries.text().strip() or 0))
        except ValueError:
            return "Max RPM and stage retries must be whole numbers."
        self.node.props["on_budget"] = self.on_budget.currentText()
        raw_budget = self.max_budget_usd.text().strip()
        if raw_budget:
            try:
                self.node.props["max_budget_usd"] = max(0.0, float(raw_budget))
            except ValueError:
                return "Max budget ($) must be a number (or left blank)."
        else:
            self.node.props["max_budget_usd"] = 0
        raw_ct = self.compact_threshold.text().strip()
        if raw_ct:
            try:
                ct = int(raw_ct)
                if not 1 <= ct <= 100:
                    raise ValueError
            except ValueError:
                return "Context compact threshold must be a whole number 1–100."
            self.node.props["compact_threshold"] = ct
        else:
            self.node.props["compact_threshold"] = 85
        # Structured final answer: a JSON-object schema (or blank) + bounded re-asks.
        fs = self.final_schema.toPlainText().strip()
        if fs:
            try:
                _parsed = json.loads(fs)
            except (json.JSONDecodeError, ValueError) as e:
                return f"Final-answer schema must be valid JSON: {e}"
            if not isinstance(_parsed, dict):
                return "Final-answer schema must be a JSON object (a JSON Schema)."
        self.node.props["final_schema"] = fs
        try:
            self.node.props["final_schema_retries"] = max(
                0, int(self.final_schema_retries.text().strip() or 2))
        except ValueError:
            return "Max re-asks must be a whole number."
        _apply_state_io(self, self.node)
        _apply_guardrail(self, self.node)
        return _apply_hitl(self, self.node)


class WorkerPoolDialog(QDialog):
    def __init__(self, parent, node: Node):
        super().__init__(parent)
        self.setWindowTitle(f"Configure worker pool: {node.name}")
        self.node = node
        self.resize(560, 620)
        outer = QVBoxLayout(self)
        _scroll = QScrollArea()
        _scroll.setWidgetResizable(True)
        _inner = QWidget()
        v = QVBoxLayout(_inner)
        _scroll.setWidget(_inner)
        outer.addWidget(_scroll, 1)
        form = QFormLayout()
        self.name = QLineEdit(node.name)
        form.addRow("Name:", self.name)
        self.max_workers = QLineEdit(str(node.props.get("max_workers", 4)))
        form.addRow("Max parallel workers:", self.max_workers)
        self.role = QComboBox()
        self.role.addItems(["worker", "single"])
        self.role.setCurrentText(node.props.get("role", "worker"))
        form.addRow("Role:", self.role)
        self.enable_todos = QCheckBox(
            "Give workers a write_todos checklist tool (maintains the built-in "
            "'todos' state)")
        self.enable_todos.setChecked(bool(node.props.get("enable_todos", False)))
        form.addRow("", self.enable_todos)
        self.dag_plan = QCheckBox(
            "Dependency-aware execution (run an upstream planner's typed "
            "{id, subgoal, depends_on} plan as a DAG: independents in parallel, "
            "dependents wait for and receive their inputs; falls back to flat "
            "parallel when no valid plan is present)")
        self.dag_plan.setChecked(bool(node.props.get("dag_plan", False)))
        form.addRow("", self.dag_plan)
        self.llm_mode = _llm_mode_combo(node)
        form.addRow("Multiple LLMs:", self.llm_mode)
        self.budgets = {}
        for key in DEFAULT_BUDGETS:
            ctrl = QLineEdit(str(node.props.get(key, DEFAULT_BUDGETS[key])))
            self.budgets[key] = ctrl
            form.addRow(key + ":", ctrl)
        v.addLayout(form)
        v.addWidget(_hint("When the upstream agent emits a list of subtasks, the "
                          "pool runs them in parallel (budgets are per worker)."))
        v.addWidget(_state_io_controls(self, node, _graph_of(parent)))
        v.addWidget(_hitl_controls(self, node))
        v.addWidget(_guardrail_controls(self, node))
        xg = _Collapsible("Extra Settings")
        self.max_rpm = QLineEdit(str(node.props.get("max_rpm", 0) or ""))
        self.max_rpm.setPlaceholderText("blank/0 = unlimited")
        xg.row("Max requests/min (RPM):", self.max_rpm)
        self.stage_retries = QLineEdit(str(node.props.get("stage_retries", 0) or ""))
        self.stage_retries.setPlaceholderText("blank/0 = no stage retry")
        xg.row("Stage retries (transient errors):", self.stage_retries)
        v.addWidget(xg)
        v.addStretch(1)
        _buttons(self, outer)      # OK/Cancel stay outside the scroll, always visible

    def apply(self) -> str | None:
        err = _apply_budgets(self.node, self.budgets)
        if err:
            return err
        try:
            self.node.props["max_rpm"] = max(0, int(self.max_rpm.text().strip() or 0))
            self.node.props["stage_retries"] = max(
                0, int(self.stage_retries.text().strip() or 0))
        except ValueError:
            return "Max RPM and stage retries must be whole numbers."
        try:
            n = int(self.max_workers.text().strip())
            if n < 1:
                raise ValueError
        except ValueError:
            return "Max workers must be an integer ≥ 1."
        self.node.name = self.name.text().strip() or self.node.name
        self.node.props["max_workers"] = n
        self.node.props["role"] = self.role.currentText()
        self.node.props["enable_todos"] = self.enable_todos.isChecked()
        self.node.props["dag_plan"] = self.dag_plan.isChecked()
        self.node.props["llm_mode"] = self.llm_mode.currentData()
        _apply_state_io(self, self.node)
        _apply_guardrail(self, self.node)
        return _apply_hitl(self, self.node)


class RouterDialog(QDialog):
    def __init__(self, parent, node: Node):
        super().__init__(parent)
        self.setWindowTitle(f"Configure router: {node.name}")
        self.node = node
        self.resize(560, 560)
        outer = QVBoxLayout(self)
        _scroll = QScrollArea()
        _scroll.setWidgetResizable(True)
        _inner = QWidget()
        v = QVBoxLayout(_inner)
        _scroll.setWidget(_inner)
        outer.addWidget(_scroll, 1)
        form = QFormLayout()
        self.name = QLineEdit(node.name)
        form.addRow("Name:", self.name)
        v.addLayout(form)
        v.addWidget(QLabel("Routing instructions (optional — a sensible default "
                           "is used if blank):"))
        self.instructions = _multiline(node.props.get("instructions", ""), 120)
        v.addWidget(self.instructions)
        bform = QFormLayout()
        self.budgets = {}
        for key in DEFAULT_BUDGETS:
            ctrl = QLineEdit(str(node.props.get(key, DEFAULT_BUDGETS[key])))
            self.budgets[key] = ctrl
            bform.addRow(key + ":", ctrl)
        v.addLayout(bform)
        v.addWidget(_hint("Link an LLM (required) and the agents to route to. "
                          "The router chooses among them by name + role."))

        # ── Extra Settings (default branch + a cheaper routing-only LLM) ─────
        graph = _graph_of(parent)
        succ = ([graph.nodes[s].name for s in graph.agent_successors(node.id)]
                if graph is not None else [])
        xg = _Collapsible("Extra Settings")
        self.default_route = QComboBox()
        self.default_route.addItems([""] + succ)
        if node.props.get("default_route") in succ:
            self.default_route.setCurrentText(node.props["default_route"])
        self.default_route.setToolTip("Branch chosen when the reply is ambiguous "
                                      "(else the first successor).")
        xg.row("Default branch (tie-break):", self.default_route)
        self.routing_provider = QComboBox()
        self.routing_provider.addItems([""] + PROVIDERS)
        self.routing_provider.setCurrentText(node.props.get("routing_provider", ""))
        xg.row("Routing LLM provider:", self.routing_provider)
        self.routing_model = QLineEdit(node.props.get("routing_model", ""))
        self.routing_model.setPlaceholderText("blank = use the linked LLM for routing too")
        xg.row("  ↳ model:", self.routing_model)
        self.routing_base_url = QLineEdit(node.props.get("routing_base_url", ""))
        xg.row("  ↳ base URL:", self.routing_base_url)
        self.routing_api_key = QLineEdit(node.props.get("routing_api_key", ""))
        xg.row("  ↳ API key:", self.routing_api_key)
        v.addWidget(xg)
        v.addStretch(1)
        _buttons(self, outer)      # OK/Cancel stay outside the scroll, always visible

    def apply(self) -> str | None:
        err = _apply_budgets(self.node, self.budgets)
        if err:
            return err
        self.node.name = self.name.text().strip() or self.node.name
        self.node.props["instructions"] = self.instructions.toPlainText()
        self.node.props["default_route"] = self.default_route.currentText().strip()
        self.node.props["routing_provider"] = self.routing_provider.currentText().strip()
        self.node.props["routing_model"] = self.routing_model.text().strip()
        self.node.props["routing_base_url"] = self.routing_base_url.text().strip()
        self.node.props["routing_api_key"] = self.routing_api_key.text().strip()
        return None


class HITLDialog(QDialog):
    def __init__(self, parent, node: Node):
        super().__init__(parent)
        self.node = node
        # A HITL with 2+ outgoing links is a ROUTE-mode node (human-driven branch):
        # the reviewer picks WHICH successor runs next. 0-1 outgoing = classic GATE.
        graph = _graph_of(parent)
        self._succ = ([graph.nodes[s].name for s in graph.flow_successors(node.id)]
                      if graph is not None else [])
        self._route_mode = len(self._succ) >= 2
        self.setWindowTitle(f"Configure HITL: {node.name}  —  "
                            + ("ROUTE mode (branch)" if self._route_mode
                               else "GATE mode (review)"))
        self.resize(560, 560)
        outer = QVBoxLayout(self)
        outer.addWidget(self._mode_banner())   # loud, always-visible mode indicator
        _scroll = QScrollArea()
        _scroll.setWidgetResizable(True)
        _inner = QWidget()
        v = QVBoxLayout(_inner)
        _scroll.setWidget(_inner)
        outer.addWidget(_scroll, 1)
        form = QFormLayout()
        self.name = QLineEdit(node.name)
        form.addRow("Name:", self.name)
        v.addLayout(form)
        v.addWidget(QLabel("Prompt shown to the reviewer:"))
        self.prompt = _multiline(node.props.get("prompt", ""), 80)
        v.addWidget(self.prompt)
        rb = QHBoxLayout()
        self._lbl_reject = QLabel("On reject:")
        rb.addWidget(self._lbl_reject)
        self.on_reject = QComboBox()
        self.on_reject.addItems(["stop", "revise"])
        self.on_reject.setCurrentText(node.props.get("on_reject", "stop"))
        rb.addWidget(self.on_reject)
        rb.addStretch(1)
        v.addLayout(rb)
        if self._route_mode:
            v.addWidget(_hint(
                f"ROUTE mode ({len(self._succ)} outgoing links): this is a "
                "human-driven branch — the reviewer picks which successor runs "
                f"next ({', '.join(self._succ)}). The prompt above is what they're "
                "asked. 'On reject' / 'Decisions' don't apply here; pick the "
                "tie-break/timeout branch below."))
            rd = QHBoxLayout()
            rd.addWidget(QLabel("Default branch (timeout / tie-break):"))
            self.default_route = QComboBox()
            self.default_route.addItems([""] + self._succ)
            if node.props.get("default_route") in self._succ:
                self.default_route.setCurrentText(node.props["default_route"])
            rd.addWidget(self.default_route)
            rd.addStretch(1)
            v.addLayout(rd)
        else:
            self.default_route = None
            v.addWidget(_hint("Link it in the flow: agent → HITL → agent (gate a "
                              "hand-off), or HITL → agent (gate the start). Reject "
                              "'stop' ends the run; 'revise' re-runs the upstream "
                              "agent with the reviewer's feedback. Draw 2+ outgoing "
                              "links to turn this into a human-driven branch (route "
                              "mode)."))

        # ── Extra Settings (allowed decisions + unattended timeout) ──────────
        g = _Collapsible("Extra Settings", expanded=self._route_mode)
        self._dec_lbl = QLabel("Decisions the reviewer may take:")
        g.span(self._dec_lbl)
        _dec = node.props.get("decisions") or ["approve", "edit", "reject"]
        self.dec_approve = QCheckBox("Approve"); self.dec_approve.setChecked("approve" in _dec)
        self.dec_edit = QCheckBox("Edit"); self.dec_edit.setChecked("edit" in _dec)
        self.dec_reject = QCheckBox("Reject"); self.dec_reject.setChecked("reject" in _dec)
        for cb in (self.dec_approve, self.dec_edit, self.dec_reject):
            g.span(cb)
        self.hitl_timeout = QLineEdit(str(node.props.get("timeout", 0) or ""))
        self.hitl_timeout.setPlaceholderText("blank/0 = wait indefinitely")
        g.row("Auto-decide after (s):", self.hitl_timeout)
        self.on_timeout = QComboBox(); self.on_timeout.addItems(["approve", "reject"])
        self.on_timeout.setCurrentText(node.props.get("on_timeout", "approve"))
        g.row("On timeout:", self.on_timeout)
        g.span(_hint("Timeout is for unattended runs — if no one answers in time the "
                     "run auto-decides. A denied decision still honours 'On reject'."))
        v.addWidget(g)
        v.addStretch(1)

        # In ROUTE mode the gate-only options (approve/edit/reject, on-reject, and the
        # gate's approve/reject-on-timeout) DON'T apply — the reviewer picks a branch —
        # so gray them out (widgets AND their labels) to make that obvious. The
        # timeout-seconds and Default-branch controls stay live (route mode uses them).
        if self._route_mode:
            _ot_lbl = g.form.labelForField(self.on_timeout)
            for w in (self._lbl_reject, self.on_reject, self._dec_lbl,
                      self.dec_approve, self.dec_edit, self.dec_reject,
                      self.on_timeout, _ot_lbl):
                if w is not None:
                    w.setEnabled(False)
                    w.setToolTip("Disabled in route mode — the reviewer chooses a "
                                 "branch, so approve / edit / reject and the gate's "
                                 "on-timeout decision don't apply.")
        _buttons(self, outer)      # OK/Cancel stay outside the scroll, always visible

    def apply(self) -> str | None:
        dec = [d for d, cb in (("approve", self.dec_approve), ("edit", self.dec_edit),
                               ("reject", self.dec_reject)) if cb.isChecked()]
        if not dec and not self._route_mode:      # decisions don't apply in route mode
            return "Enable at least one reviewer decision (approve / edit / reject)."
        to = self.hitl_timeout.text().strip()
        if to:
            try:
                if int(to) < 0:
                    raise ValueError
            except ValueError:
                return "Auto-decide timeout must be a whole number of seconds (or blank)."
        self.node.name = self.name.text().strip() or self.node.name
        self.node.props["prompt"] = self.prompt.toPlainText().strip()
        self.node.props["on_reject"] = self.on_reject.currentText()
        self.node.props["decisions"] = dec or ["approve", "edit", "reject"]
        self.node.props["timeout"] = int(to) if to else 0
        self.node.props["on_timeout"] = self.on_timeout.currentText()
        if self.default_route is not None:
            self.node.props["default_route"] = self.default_route.currentText().strip()
        else:
            self.node.props["default_route"] = ""   # gate mode: clear any stale route default
        return None

    def _mode_banner(self) -> QLabel:
        """A loud, always-visible banner showing GATE vs ROUTE mode. Route mode is
        auto-enabled by drawing 2+ outgoing links — so the gate banner also says how
        to switch. Colours are self-contained (readable in both light and dark)."""
        if self._route_mode:
            txt = ("🔀  ROUTE MODE — human-driven branch.  The reviewer picks which "
                   "step runs next:   " + "   ·   ".join(self._succ))
            css = "background:#0d5c47; color:#eafff8; border:1px solid #14b88a;"
        else:
            txt = ("✋  REVIEW GATE — the reviewer approves / edits / rejects before "
                   "the next stage.\n▸  Draw a 2nd outgoing link from this HITL node "
                   "to turn it into a human-driven BRANCH (route mode).")
            css = "background:#5c470d; color:#fff8ea; border:1px solid #b8891a;"
        lbl = QLabel(txt)
        lbl.setWordWrap(True)
        lbl.setStyleSheet("QLabel { %s border-radius:6px; padding:8px 10px; "
                          "font-weight:bold; }" % css)
        return lbl


class LLMDialog(QDialog):
    def __init__(self, parent, node: Node):
        super().__init__(parent)
        self.setWindowTitle(f"Configure LLM: {node.name}")
        self.node = node
        self.resize(560, 640)
        # Scroll the fields (like AgentDialog): the growing Extra Settings group can
        # push this dialog taller than the screen — a scroll area lets it shrink.
        outer = QVBoxLayout(self)
        _scroll = QScrollArea()
        _scroll.setWidgetResizable(True)
        _inner = QWidget()
        v = QVBoxLayout(_inner)
        _scroll.setWidget(_inner)
        outer.addWidget(_scroll, 1)
        form = QFormLayout()
        self.name = QLineEdit(node.name)
        self.provider = QComboBox()
        self.provider.addItems(PROVIDERS)
        self.provider.setCurrentText(node.props.get("provider", PROVIDERS[0]))
        self.model = QLineEdit(node.props.get("model", ""))
        self.api_key = QLineEdit(node.props.get("api_key", ""))
        self.base_url = QLineEdit(node.props.get("base_url", ""))
        self.temperature = QLineEdit(str(node.props.get("temperature", "")))
        self.top_p = QLineEdit(str(node.props.get("top_p", "")))
        self.request_timeout = QLineEdit(str(node.props.get("request_timeout_s", "")))
        self.proxy = QLineEdit(node.props.get("proxy", ""))
        self.proxy.setPlaceholderText(
            "e.g. http://1.1.1.1:8080  —  blank = use system / env proxy")
        _cap = node.props.get("context_capacity", 0) or 0
        self.context_capacity = QLineEdit("" if not _cap else str(_cap))
        self.context_capacity.setToolTip(
            "This model's context window, in tokens (e.g. 128000). When set, the "
            "main agent compacts older conversation to stay under it. Blank/0 = no "
            "context control.")
        self.response_format = QComboBox()
        self.response_format.addItems(["text", "json_object", "json_schema"])
        self.response_format.setCurrentText(node.props.get("response_format") or "text")
        for label, ctrl in (("Name:", self.name), ("Provider:", self.provider),
                            ("Model:", self.model), ("API key:", self.api_key),
                            ("Base URL:", self.base_url),
                            ("Temperature:", self.temperature),
                            ("Top-p:", self.top_p),
                            ("Request timeout (s):", self.request_timeout),
                            ("Proxy (optional):", self.proxy),
                            ("Context capacity (tokens):", self.context_capacity),
                            ("Response format:", self.response_format)):
            form.addRow(label, ctrl)
        self.provider.currentTextChanged.connect(self._on_provider)
        self.response_format.currentTextChanged.connect(lambda _t: self._refresh_hint())
        v.addLayout(form)

        self.parallel_tools = QCheckBox(
            "Run tool calls in parallel when the model requests several at once")
        self.parallel_tools.setChecked(bool(node.props.get("parallel_tools", False)))
        v.addWidget(self.parallel_tools)
        self.vision = QCheckBox("Accepts image input (vision model)")
        self.vision.setChecked(bool(node.props.get("vision", False)))
        v.addWidget(self.vision)

        self.fmt_hint = QLabel("")
        self.fmt_hint.setWordWrap(True)
        self.fmt_hint.setStyleSheet(f"color:{_WARN_COLOR};")
        v.addWidget(self.fmt_hint)

        v.addWidget(QLabel("Response schema (JSON Schema object — used only for "
                           "json_schema):"))
        self.response_schema = _multiline(node.props.get("response_schema", ""), 80)
        self.response_schema.setPlaceholderText(
            'JSON Schema, e.g.\n'
            '{"type":"object","properties":{"answer":{"type":"string"}},'
            '"required":["answer"]}')
        v.addWidget(self.response_schema)

        # ── Extra Settings (advanced sampling; blank = provider default) ─────
        grp = _Collapsible("Extra Settings  (advanced sampling)")
        self.reasoning_effort = QComboBox()
        self.reasoning_effort.addItems(["", "minimal", "low", "medium", "high"])
        self.reasoning_effort.setCurrentText(str(node.props.get("reasoning_effort", "")))
        grp.row("Reasoning effort:", self.reasoning_effort)
        self.seed = QLineEdit(str(node.props.get("seed", "")))
        grp.row("Seed (int):", self.seed)
        self.top_k = QLineEdit(str(node.props.get("top_k", "")))
        grp.row("Top-k (int):", self.top_k)
        self.presence_penalty = QLineEdit(str(node.props.get("presence_penalty", "")))
        grp.row("Presence penalty:", self.presence_penalty)
        self.frequency_penalty = QLineEdit(str(node.props.get("frequency_penalty", "")))
        grp.row("Frequency penalty:", self.frequency_penalty)
        self.max_retries = QLineEdit(str(node.props.get("max_retries", "")))
        self.max_retries.setPlaceholderText("blank = default (2); 0 = no retry")
        grp.row("Max retries:", self.max_retries)
        self.tool_choice = QComboBox()
        self.tool_choice.addItems(["auto", "any", "none", "specific"])
        self.tool_choice.setCurrentText(node.props.get("tool_choice") or "auto")
        self.tool_choice.setToolTip("First-turn tool use: auto (model decides) · any "
                                    "(must call some tool) · none (no tools) · specific "
                                    "(force the named tool).")
        grp.row("Tool choice (1st turn):", self.tool_choice)
        self.tool_choice_name = QLineEdit(node.props.get("tool_choice_name", ""))
        self.tool_choice_name.setPlaceholderText("tool/function name — for 'specific'")
        grp.row("  ↳ specific tool:", self.tool_choice_name)
        self.price_in = QLineEdit(str(node.props.get("price_in_per_1m", "")))
        self.price_in.setPlaceholderText("blank = no cost tracking")
        grp.row("Input price ($/1M tok):", self.price_in)
        self.price_out = QLineEdit(str(node.props.get("price_out_per_1m", "")))
        self.price_out.setPlaceholderText("blank = no cost tracking")
        grp.row("Output price ($/1M tok):", self.price_out)
        grp.span(QLabel("Stop sequences (one per line):"))
        self.stop = _multiline(node.props.get("stop", ""), 44)
        self.stop.setPlaceholderText(
            "one sequence per line, e.g.\nEND\n###\n"
            "(generation halts when any is produced; blank = none)")
        grp.span(self.stop)
        grp.span(QLabel('Extra API params (JSON object, e.g. {"logprobs": true}) — '
                        'merged last, overrides the fields above:'))
        self.extra = _multiline(node.props.get("extra", ""), 60)
        self.extra.setPlaceholderText(
            'raw JSON object passed verbatim to the provider, e.g.\n'
            '{"logprobs": true, "top_logprobs": 5}\n'
            '(escape hatch for params not shown above; blank = none)')
        grp.span(self.extra)
        v.addWidget(grp)

        v.addWidget(_hint("Temperature/Top-p: blank = provider default; both are "
                          "rejected by Anthropic Opus 4.x. json_object = valid "
                          "JSON; json_schema constrains output to your schema. Extra "
                          "Settings are provider-specific (e.g. top_k / reasoning "
                          "effort aren't accepted everywhere) — blank = unchanged."))
        self._refresh_hint()
        _buttons(self, outer)      # OK/Cancel stay outside the scroll, always visible

    def _on_provider(self, _t=None) -> None:
        model, base_url = PROVIDER_DEFAULTS[self.provider.currentText()]
        self.model.setText(model)
        self.base_url.setText(base_url)
        self._refresh_hint()

    def _refresh_hint(self) -> None:
        provider = self.provider.currentText()
        fmt = self.response_format.currentText()
        support = response_format_support(provider, fmt)
        msgs = {
            "no": f"⚠ '{fmt}' is not supported by {provider} and will be ignored. "
                  + ("Use json_schema for Claude." if provider == "anthropic" else ""),
            "weak": f"⚠ {provider} accepts json_schema but enforcement depends on "
                    "the model — it may be ignored. json_object is safer.",
            "yes": "",
        }
        self.fmt_hint.setText(msgs.get(support, ""))

    def apply(self) -> str | None:
        temp = self.temperature.text().strip()
        top_p = self.top_p.text().strip()
        for label, val in (("Temperature", temp), ("Top-p", top_p)):
            if val:
                try:
                    float(val)
                except ValueError:
                    return f"{label} must be a number (or left blank)."
        timeout = self.request_timeout.text().strip()
        if timeout:
            try:
                if float(timeout) <= 0:
                    raise ValueError
            except ValueError:
                return "Request timeout must be a positive number of seconds."
        extra = self.extra.toPlainText().strip()
        if extra:
            try:
                parsed = json.loads(extra)
            except (json.JSONDecodeError, ValueError) as e:
                return f"Extra API params must be valid JSON: {e}"
            if not isinstance(parsed, dict):
                return "Extra API params must be a JSON object (e.g. {...})."
        cap_raw = self.context_capacity.text().strip()
        capacity = 0
        if cap_raw:
            try:
                capacity = int(float(cap_raw))
                if capacity < 0:
                    raise ValueError
            except ValueError:
                return "Context capacity must be a positive whole number of tokens (or blank)."
        fmt = self.response_format.currentText()
        schema = self.response_schema.toPlainText().strip()
        if fmt == "json_schema":
            if not schema:
                return ("json_schema needs a Response schema (a JSON Schema "
                        "object). Add one, or pick json_object/text.")
            try:
                parsed = json.loads(schema)
            except (json.JSONDecodeError, ValueError) as e:
                return f"Response schema must be valid JSON: {e}"
            if not isinstance(parsed, dict):
                return "Response schema must be a JSON object (e.g. {...})."
        # Extra Settings: validate the numeric sampling fields (blank = unset).
        for label, val in (("Seed", self.seed.text().strip()),
                           ("Top-k", self.top_k.text().strip())):
            if val:
                try:
                    int(float(val))
                except ValueError:
                    return f"{label} must be a whole number (or left blank)."
        for label, val in (("Presence penalty", self.presence_penalty.text().strip()),
                           ("Frequency penalty", self.frequency_penalty.text().strip()),
                           ("Input price", self.price_in.text().strip()),
                           ("Output price", self.price_out.text().strip())):
            if val:
                try:
                    float(val)
                except ValueError:
                    return f"{label} must be a number (or left blank)."
        mr = self.max_retries.text().strip()
        if mr:
            try:
                if int(float(mr)) < 0:
                    raise ValueError
            except ValueError:
                return "Max retries must be a whole number >= 0 (or left blank)."
        if (self.tool_choice.currentText() == "specific"
                and not self.tool_choice_name.text().strip()):
            return "Tool choice 'specific' needs a tool name (or pick auto/any/none)."
        self.node.name = self.name.text().strip() or self.node.name
        self.node.props.update(
            provider=self.provider.currentText(), model=self.model.text().strip(),
            api_key=self.api_key.text().strip(), base_url=self.base_url.text().strip(),
            temperature=temp, top_p=top_p, request_timeout_s=timeout,
            proxy=self.proxy.text().strip(),
            response_format=fmt, response_schema=schema,
            parallel_tools=self.parallel_tools.isChecked(),
            vision=self.vision.isChecked(), context_capacity=capacity, extra=extra,
            reasoning_effort=self.reasoning_effort.currentText().strip(),
            seed=self.seed.text().strip(), top_k=self.top_k.text().strip(),
            presence_penalty=self.presence_penalty.text().strip(),
            frequency_penalty=self.frequency_penalty.text().strip(),
            stop=self.stop.toPlainText().strip(),
            max_retries=self.max_retries.text().strip(),
            tool_choice=self.tool_choice.currentText(),
            tool_choice_name=self.tool_choice_name.text().strip(),
            price_in_per_1m=self.price_in.text().strip(),
            price_out_per_1m=self.price_out.text().strip())
        return None


class _ToolCodeDialog(QDialog):
    """View / edit a tool's Python source (tools/<fname>). Opened by
    double-clicking a tool in the Tools node dialog."""

    def __init__(self, parent, fname: str):
        super().__init__(parent)
        self.fname = fname
        self.path = os.path.join(codegen.TOOLS_DIR, fname)
        self.setWindowTitle(f"Tool source — {fname}")
        self.setWindowModality(Qt.WindowModal)
        self.resize(780, 620)
        v = QVBoxLayout(self)
        try:
            with open(self.path, encoding="utf-8") as f:
                src = f.read()
            self._readonly = False
        except OSError as e:
            src = f"# could not read {self.path}\n# {e}"
            self._readonly = True
        self.editor = QPlainTextEdit(src)
        self.editor.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.editor.setStyleSheet(
            "font-family: Consolas, 'Courier New', monospace; font-size: 12px;")
        self.editor.setReadOnly(self._readonly)
        v.addWidget(self.editor, 1)
        v.addWidget(_hint("Edit the tool's Python and click Save. Save runs a "
                          "syntax check; linked agents pick up the change the "
                          "next time you generate."))
        bb = QDialogButtonBox()
        if not self._readonly:
            bb.addButton("Save", QDialogButtonBox.AcceptRole).clicked.connect(
                self.on_save)
        bb.addButton("Close", QDialogButtonBox.RejectRole)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def on_save(self) -> None:
        text = self.editor.toPlainText()
        try:
            compile(text, self.fname, "exec")          # syntax check
        except SyntaxError as e:
            if QMessageBox.warning(
                    self, "Syntax error",
                    f"{self.fname} has a syntax error:\n\n{e}\n\nSave anyway?",
                    QMessageBox.Save | QMessageBox.Cancel) != QMessageBox.Save:
                return
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                f.write(text)
        except OSError as e:
            QMessageBox.warning(self, "Save failed", str(e))
            return
        self.accept()


class ToolDialog(QDialog):
    def __init__(self, parent, node: Node):
        super().__init__(parent)
        self.setWindowTitle(f"Configure Tools: {node.name}")
        # Window-modal (not application-modal) so the Tool Generator window
        # opened from "Create a new tool…" stays interactive while this dialog
        # is up. exec() honours an explicitly-set WindowModal.
        self.setWindowModality(Qt.WindowModal)
        self.node = node
        self._selected = tool_files(node)
        self._lib_cache = None  # last codegen.list_tools(); skip needless rebuilds
        self.resize(560, 640)
        outer = QVBoxLayout(self)
        _scroll = QScrollArea()
        _scroll.setWidgetResizable(True)
        _inner = QWidget()
        v = QVBoxLayout(_inner)
        _scroll.setWidget(_inner)
        outer.addWidget(_scroll, 1)
        form = QFormLayout()
        self.name = QLineEdit(node.name)
        form.addRow("Name:", self.name)
        v.addLayout(form)
        v.addWidget(QLabel("Tools in this node (check to add/remove; "
                           "double-click to view / edit the code):"))
        self.listw = QListWidget()
        self.listw.setMinimumSize(360, 200)
        self.listw.itemDoubleClicked.connect(self._view_tool_code)
        v.addWidget(self.listw)
        self._reload_library()

        # Author a new tool without leaving the canvas, then check it here.
        row = QHBoxLayout()
        new_btn = QPushButton("Create a new tool…")
        new_btn.setToolTip("Open the Tool Generator to write a new tool, then "
                           "check it in this list.")
        new_btn.clicked.connect(self._open_tool_generator)
        refresh_btn = QPushButton("↻ Refresh")
        refresh_btn.setToolTip("Re-scan the tool library.")
        refresh_btn.clicked.connect(self._reload_library)
        row.addWidget(new_btn)
        row.addWidget(refresh_btn)
        row.addStretch(1)
        v.addLayout(row)

        v.addWidget(_hint("Link this one node to an agent — it gives the agent "
                          "every function in the checked files. HITL gating uses "
                          "each tool's @tool(risk=\"high\"|\"safe\") declaration "
                          "(authoritative over the name guess); otherwise the "
                          "name heuristic applies."))

        # ── Extra Settings: per-tool-function overrides (blank row = unchanged) ──
        self._tp = {k: dict(v_) for k, v_ in (node.props.get("tool_props") or {}).items()
                    if isinstance(v_, dict)}
        self._rows = []
        xg = _Collapsible("Extra Settings  (per tool function)")
        self.tool_table = QTableWidget(0, 6)
        self.tool_table.setHorizontalHeaderLabels(
            ["Function", "Return\ndirect", "On error", "Retries", "Risk", "Description override"])
        self.tool_table.verticalHeader().setVisible(False)
        self.tool_table.setMinimumHeight(150)
        xg.span(self.tool_table)
        xg.span(_hint("Return-direct: the tool's output becomes the final answer. "
                      "On-error: return (tell the model) · retry (N times) · raise "
                      "(abort). Risk overrides the tool's own declaration. Overrides "
                      "are app-wide by function name; retrying a write/send tool "
                      "repeats its side effect."))
        v.addWidget(xg)
        self._rebuild_tool_rows()
        self.listw.itemChanged.connect(lambda *_: self._rebuild_tool_rows())
        _buttons(self, outer)      # OK/Cancel stay outside the scroll, always visible

    def _rebuild_tool_rows(self) -> None:
        """Rebuild the per-function rows from the CHECKED files, preserving edits."""
        if not getattr(self, "tool_table", None):
            return
        self._capture_tool_rows()
        funcs = codegen.list_tool_functions(self._checked_files())
        self.tool_table.setRowCount(len(funcs))
        self._rows = []
        for i, fn in enumerate(funcs):
            p = self._tp.get(fn, {})
            item = QTableWidgetItem(fn)
            item.setFlags(Qt.ItemIsEnabled)
            self.tool_table.setItem(i, 0, item)
            rd = QCheckBox(); rd.setChecked(bool(p.get("return_direct")))
            self.tool_table.setCellWidget(i, 1, rd)
            em = QComboBox(); em.addItems(["return", "retry", "raise"])
            em.setCurrentText(p.get("error_mode", "return") or "return")
            self.tool_table.setCellWidget(i, 2, em)
            rt = QLineEdit(str(p.get("error_retries", "") or "")); rt.setPlaceholderText("0")
            self.tool_table.setCellWidget(i, 3, rt)
            rk = QComboBox(); rk.addItems(["default", "high", "safe"])
            rk.setCurrentText(p.get("risk", "default") or "default")
            self.tool_table.setCellWidget(i, 4, rk)
            ds = QLineEdit(p.get("description", "") or "")
            self.tool_table.setCellWidget(i, 5, ds)
            self._rows.append((fn, rd, em, rt, rk, ds))

    def _capture_tool_rows(self) -> None:
        """Fold the current row widgets back into self._tp (only non-default cells)."""
        for fn, rd, em, rt, rk, ds in getattr(self, "_rows", []):
            d = {}
            if rd.isChecked():
                d["return_direct"] = True
            if em.currentText() != "return":
                d["error_mode"] = em.currentText()
                try:
                    d["error_retries"] = max(0, int(rt.text().strip() or 0))
                except ValueError:
                    d["error_retries"] = 0
            if rk.currentText() != "default":
                d["risk"] = rk.currentText()
            if ds.text().strip():
                d["description"] = ds.text().strip()
            if d:
                self._tp[fn] = d
            else:
                self._tp.pop(fn, None)

    def _checked_files(self) -> list[str]:
        # Only real tool entries are user-checkable; the "(tool library is empty)"
        # placeholder has that flag cleared, so this skips it.
        files = []
        for i in range(self.listw.count()):
            it = self.listw.item(i)
            if (it.flags() & Qt.ItemIsUserCheckable) and it.checkState() == Qt.Checked:
                files.append(it.text())
        return files

    def _reload_library(self) -> None:
        """(Re)build the checkable list from the tool library, preserving the
        current checked selection so a tool just created in the Tool Generator
        appears without losing what the user already ticked."""
        library = codegen.list_tools()
        # The activation-refresh fires on every focus-in; rebuilding clears the
        # highlighted row and scroll position, so skip it when nothing changed.
        if getattr(self, "_built", False) and library == self._lib_cache:
            return
        self._lib_cache = library
        checked = set(self._checked_files()) if getattr(self, "_built", False) \
            else set(self._selected)
        choices = list(dict.fromkeys(library + sorted(checked)))
        self.listw.clear()
        if choices:
            for f in choices:
                it = QListWidgetItem(f)
                it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
                it.setCheckState(Qt.Checked if f in checked else Qt.Unchecked)
                self.listw.addItem(it)
        else:
            placeholder = QListWidgetItem("(tool library is empty)")
            placeholder.setFlags(Qt.ItemIsEnabled)  # display-only: not checkable
            self.listw.addItem(placeholder)
        self._built = True

    def _view_tool_code(self, item) -> None:
        """Double-click a tool → view / edit its Python source. The
        '(tool library is empty)' placeholder isn't checkable, so it's skipped."""
        if not (item.flags() & Qt.ItemIsUserCheckable):
            return
        _ToolCodeDialog(self, item.text()).exec()

    def _open_tool_generator(self) -> None:
        # Lazy import: the coding agent + its LLM deps stay off the canvas
        # startup path until the Tool Generator is actually opened.
        from canvas_qt.tool_generator import open_tool_generator
        open_tool_generator()

    def changeEvent(self, event):
        # When focus returns to this dialog (e.g. after saving a tool in the
        # Tool Generator), re-scan so the new file shows up.
        if (event.type() == QEvent.ActivationChange and self.isActiveWindow()
                and getattr(self, "_built", False)):
            self._reload_library()
        super().changeEvent(event)

    def apply(self) -> str | None:
        files = self._checked_files()
        self.node.props["files"] = files
        self.node.props.pop("file", None)
        # per-function Extra Settings: capture edits, then keep only functions that
        # still belong to a checked file (drop stale entries so they can't leak).
        self._capture_tool_rows()
        live = set(codegen.list_tool_functions(files))
        self.node.props["tool_props"] = {k: v for k, v in self._tp.items() if k in live}
        self.node.name = self.name.text().strip() or self.node.name
        return None


class _SkillEntryDialog(QDialog):
    def __init__(self, parent, title: str, name: str = "", text: str = "",
                 description: str = "", disable_model_invocation: bool = False):
        super().__init__(parent)
        self.setWindowTitle(title)
        v = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(name)
        form.addRow("Name:", self.name)
        self.description = QLineEdit(description)
        self.description.setPlaceholderText(
            "What it does + when to use it — the routing hint the agent sees")
        form.addRow("Description:", self.description)
        v.addLayout(form)
        self.manual = QCheckBox("Apply only when the user types /<name> "
                                "(not auto-selected by the agent)")
        self.manual.setChecked(bool(disable_model_invocation))
        v.addWidget(self.manual)
        row = QHBoxLayout()
        row.addWidget(QLabel("Skill body (the full instructions — loaded on "
                             "demand, not kept in the prompt):"), 1)
        load = QPushButton("Load from .md…")
        load.clicked.connect(self.on_load_md)
        row.addWidget(load)
        v.addLayout(row)
        self.text = _multiline(text, 200)
        v.addWidget(self.text)
        _buttons(self, v)

    def on_load_md(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import SKILL.md", "",
            "Markdown (*.md *.markdown *.txt);;All files (*)")
        if not path:
            return
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                s = parse_skill_md(f.read())
        except OSError as e:
            QMessageBox.warning(self, "Import failed", str(e))
            return
        if s["name"]:
            self.name.setText(s["name"])
        if s["description"]:
            self.description.setText(s["description"])
        self.manual.setChecked(s["disable_model_invocation"])
        self.text.setPlainText(s["text"])

    def result(self) -> dict:
        return {"name": self.name.text().strip() or "skill",
                "description": self.description.text().strip(),
                "text": self.text.toPlainText().strip(),
                "disable_model_invocation": self.manual.isChecked()}


class SkillsNodeDialog(QDialog):
    def __init__(self, parent, node: Node):
        super().__init__(parent)
        self.setWindowTitle(f"Configure Skills: {node.name}")
        self.node = node
        self._skills = [dict(s) for s in skill_items(node)]
        v = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(node.name)
        form.addRow("Name:", self.name)
        v.addLayout(form)
        v.addWidget(QLabel("Skills (only name + description go in the prompt; the "
                           "agent loads a skill's body on demand, or /<name>):"))
        self.listw = QListWidget()
        self.listw.setMinimumSize(420, 180)
        v.addWidget(self.listw)
        v.addLayout(_list_edit_row(("Add...", self.on_add),
                                   ("Edit...", self.on_edit),
                                   ("Remove", self.on_remove)))
        v.addWidget(_hint("If a Skills node is in the graph, the generated GUI "
                          "gets a Skills menu to manage these at runtime."))
        self._reload()
        _buttons(self, v)

    def _reload(self) -> None:
        self.listw.clear()
        for s in self._skills:
            self.listw.addItem(f"{s['name']}: " + s["text"].replace("\n", " ")[:48])

    def on_add(self) -> None:
        dlg = _SkillEntryDialog(self, "Add Skill")
        if dlg.exec() == QDialog.Accepted:
            r = dlg.result()
            if r["text"]:
                self._skills.append(r)
                self._reload()

    def on_edit(self) -> None:
        i = self.listw.currentRow()
        if i < 0:
            return
        s = self._skills[i]
        dlg = _SkillEntryDialog(self, "Edit Skill", s["name"], s["text"],
                                s.get("description", ""),
                                s.get("disable_model_invocation", False))
        if dlg.exec() == QDialog.Accepted:
            r = dlg.result()
            if r["text"]:
                self._skills[i] = r
                self._reload()

    def on_remove(self) -> None:
        i = self.listw.currentRow()
        if i >= 0:
            self._skills.pop(i)
            self._reload()

    def apply(self) -> str | None:
        self.node.props["skills"] = self._skills
        self.node.props.pop("text", None)
        self.node.name = self.name.text().strip() or self.node.name
        return None


class PromptDialog(QDialog):
    ROLES = ["single", "planner", "worker", "critic", "supervisor",
             "orchestrator"]

    def __init__(self, parent, node: Node):
        super().__init__(parent)
        self.setWindowTitle(f"Configure prompt: {node.name}")
        self.node = node
        v = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("Name:"))
        self.name = QLineEdit(node.name)
        top.addWidget(self.name, 1)
        top.addWidget(QLabel("Role:"))
        self.role = QComboBox()
        self.role.addItems(self.ROLES)
        self.role.setCurrentText(node.props.get("role", "single"))
        top.addWidget(self.role)
        tmpl = QPushButton("Load Role Template")
        tmpl.clicked.connect(self.on_template)
        top.addWidget(tmpl)
        ffile = QPushButton("From file...")
        ffile.clicked.connect(self.on_load_file)
        top.addWidget(ffile)
        v.addLayout(top)
        text = node.props.get("text", "") or graph_codegen.role_template(
            self.role.currentText())
        self.text = _multiline(text, 240)
        v.addWidget(self.text)
        v.addWidget(_hint("{agent_name} is replaced with the linked agent's name "
                          "at generation time."))
        self.role.currentTextChanged.connect(self.on_role)
        _buttons(self, v)

    def on_role(self, _t=None) -> None:
        role = self.role.currentText()
        if QMessageBox.question(self, "Load template",
                                f"Replace the text with the '{role}' role "
                                "template?") == QMessageBox.Yes:
            self.text.setPlainText(graph_codegen.role_template(role))

    def on_template(self) -> None:
        self.text.setPlainText(graph_codegen.role_template(self.role.currentText()))

    def on_load_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Load prompt text", "", "Text (*.txt *.md);;All files (*)")
        if path:
            with open(path, encoding="utf-8") as f:
                self.text.setPlainText(f.read())

    def apply(self) -> str | None:
        self.node.name = self.name.text().strip() or self.node.name
        self.node.props["role"] = self.role.currentText()
        self.node.props["text"] = self.text.toPlainText()
        return None


class TextDialog(QDialog):
    """Fallback name + text body editor."""

    def __init__(self, parent, node: Node, what: str):
        super().__init__(parent)
        self.setWindowTitle(f"Configure {what}: {node.name}")
        self.node = node
        v = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("Name:"))
        self.name = QLineEdit(node.name)
        top.addWidget(self.name, 1)
        v.addLayout(top)
        self.text = _multiline(node.props.get("text", ""), 220)
        v.addWidget(self.text)
        _buttons(self, v)

    def apply(self) -> str | None:
        self.node.name = self.name.text().strip() or self.node.name
        self.node.props["text"] = self.text.toPlainText()
        return None


class RagDialog(QDialog):
    def __init__(self, parent, node: Node):
        super().__init__(parent)
        self.setWindowTitle(f"Configure RAG: {node.name}")
        self.node = node
        self.resize(560, 660)
        # Scroll: RAG has many fields — a scroll area keeps it within the screen.
        outer = QVBoxLayout(self)
        _scroll = QScrollArea()
        _scroll.setWidgetResizable(True)
        _inner = QWidget()
        v = QVBoxLayout(_inner)
        _scroll.setWidget(_inner)
        outer.addWidget(_scroll, 1)
        form = QFormLayout()
        self.name = QLineEdit(node.name)
        form.addRow("Name:", self.name)
        row = QHBoxLayout()
        self.docs_dir = QLineEdit(node.props.get("docs_dir", ""))
        browse = QPushButton("Browse...")
        browse.clicked.connect(self.on_browse)
        row.addWidget(self.docs_dir, 1)
        row.addWidget(browse)
        rw = QWidget()
        rw.setLayout(row)
        form.addRow("Docs folder:", rw)
        self.chunk = QLineEdit(str(node.props.get("chunk_chars", 800)))
        form.addRow("Chunk chars:", self.chunk)
        self.top_k = QLineEdit(str(node.props.get("top_k", 4)))
        form.addRow("Top-k chunks:", self.top_k)
        self.description = _multiline(node.props.get("description", ""), 70)
        self.description.setPlaceholderText(
            "What this knowledge base covers and when to use it. Shown to the "
            "agent as the search tool's description and appended to its prompt. "
            "Recommended when an agent has several knowledge bases.")
        form.addRow("Description:", self.description)
        v.addLayout(form)

        # Advanced pipeline — each field defaults to the plain BM25 baseline, so
        # leaving these untouched keeps today's behavior. Grouped into collapsible
        # sections (like the Agent node) so the dialog opens compact; expand only
        # the area you're tuning.
        p = node.props

        chunking = _Collapsible("Chunking")
        self.chunk_strategy = QComboBox()
        self.chunk_strategy.addItems(["fixed", "recursive", "markdown", "code"])
        self.chunk_strategy.setCurrentText(p.get("chunk_strategy", "fixed"))
        chunking.row("Chunk strategy:", self.chunk_strategy)
        self.chunk_overlap = QLineEdit(str(p.get("chunk_overlap", 0)))
        self.chunk_overlap.setPlaceholderText("0 = auto (chunk size / 8)")
        chunking.row("Chunk overlap:", self.chunk_overlap)
        self.granularity = QComboBox()
        self.granularity.addItems(["chunk", "parent_child"])
        self.granularity.setCurrentText(p.get("retrieval_granularity", "chunk"))
        chunking.row("Retrieval granularity:", self.granularity)
        self.parent_chunk = QLineEdit(str(p.get("parent_chunk_chars", 2400)))
        self.parent_chunk.setPlaceholderText(
            "parent_child only: index small children, return this bigger parent block")
        chunking.row("  ↳ parent chars:", self.parent_chunk)
        v.addWidget(chunking)

        ranking = _Collapsible("Retrieval & ranking")
        self.retrieval = QComboBox()
        self.retrieval.addItems(["bm25", "dense", "hybrid"])
        self.retrieval.setCurrentText(p.get("retrieval_algorithm", "bm25"))
        ranking.row("Retrieval:", self.retrieval)
        self.recall_n = QLineEdit(str(p.get("recall_n", 0)))
        self.recall_n.setPlaceholderText("0 = top-k (wider pool feeds rerank/MMR)")
        ranking.row("Recall N:", self.recall_n)
        self.mmr = QCheckBox("Diversify results (MMR)")
        self.mmr.setChecked(bool(p.get("mmr", False)))
        ranking.span(self.mmr)
        self.mmr_lambda = QLineEdit(str(p.get("mmr_lambda", 0.5)))
        ranking.row("MMR lambda (0-1):", self.mmr_lambda)
        self.rerank_mode = QComboBox()
        self.rerank_mode.addItems(["none", "llm", "cross_encoder"])
        self.rerank_mode.setCurrentText(p.get("rerank_mode", "none"))
        ranking.row("Rerank:", self.rerank_mode)
        self.rerank_model = QLineEdit(p.get("rerank_model", ""))
        self.rerank_model.setPlaceholderText(
            "cross_encoder only, free & local (blank = BAAI/bge-reranker-base); "
            "CJK-strong: BAAI/bge-reranker-v2-m3; fast/EN: ms-marco-MiniLM-L-6-v2")
        ranking.row("  ↳ reranker model:", self.rerank_model)
        self.score_threshold = QLineEdit(str(p.get("score_threshold", 0.0) or ""))
        self.score_threshold.setPlaceholderText("0 = off; dense/cross-encoder only, e.g. 0.30")
        ranking.row("Score threshold:", self.score_threshold)
        self.metadata_filter = QLineEdit(p.get("metadata_filter", ""))
        self.metadata_filter.setPlaceholderText("source glob(s), e.g. *.md or policies/*  (blank = all)")
        ranking.row("Source filter:", self.metadata_filter)
        self.evict_used = QCheckBox("Evict used retrievals (a newer search drops "
                                    "earlier results this turn, to save context)")
        self.evict_used.setChecked(bool(p.get("evict_used", False)))
        ranking.span(self.evict_used)
        v.addWidget(ranking)

        correction = _Collapsible("Query rewriting & correction")
        self.query_transform = QComboBox()
        self.query_transform.addItems(["none", "rewrite", "multi_query"])
        self.query_transform.setCurrentText(p.get("query_transform", "none"))
        correction.row("Query transform:", self.query_transform)
        self.multi_query_n = QLineEdit(str(p.get("multi_query_n", 3)))
        self.multi_query_n.setPlaceholderText("multi_query only: number of query variants")
        correction.row("  ↳ multi-query N:", self.multi_query_n)
        self.grade_docs = QCheckBox("Grade chunks for relevance (LLM) — drop "
                                    "clearly-irrelevant results before answering")
        self.grade_docs.setChecked(bool(p.get("grade_docs", False)))
        correction.span(self.grade_docs)
        self.corrective = QCheckBox("Corrective re-retrieval (CRAG) — if a search "
                                    "finds nothing, rewrite the query and retry")
        self.corrective.setChecked(bool(p.get("corrective", False)))
        correction.span(self.corrective)
        self.corrective_max = QLineEdit(str(p.get("corrective_max_rewrites", 2)))
        self.corrective_max.setPlaceholderText("extra retries when nothing is found")
        correction.row("  ↳ max retries:", self.corrective_max)
        v.addWidget(correction)

        embedding = _Collapsible("Embedding & vector store")
        self.embed_provider = QComboBox()
        self.embed_provider.addItems(["local", "openai"])
        self.embed_provider.setCurrentText(p.get("embed_provider", "local"))
        embedding.row("Embedding provider:", self.embed_provider)
        self.embed_model = QLineEdit(p.get("embed_model", "BAAI/bge-small-zh-v1.5"))
        self.embed_model.setPlaceholderText(
            "local (free, no key): BAAI/bge-small-zh-v1.5 / bge-small-en-v1.5; "
            "openai: e.g. BAAI/bge-m3")
        embedding.row("Embedding model:", self.embed_model)
        self.embed_base_url = QLineEdit(p.get("embed_base_url", ""))
        self.embed_base_url.setPlaceholderText("openai only; blank = app backend")
        embedding.row("Embedding base URL:", self.embed_base_url)
        self.embed_api_key = QLineEdit(p.get("embed_api_key", ""))
        self.embed_api_key.setPlaceholderText("openai only; blank = app backend key")
        embedding.row("Embedding API key:", self.embed_api_key)
        self.normalize = QCheckBox("L2-normalize embeddings (cosine)")
        self.normalize.setChecked(bool(p.get("normalize", True)))
        embedding.span(self.normalize)
        self.vector_db = QComboBox()
        self.vector_db.addItems(["memory", "chroma", "faiss", "qdrant"])
        self.vector_db.setCurrentText(p.get("vector_db", "memory"))
        embedding.row("Vector store:", self.vector_db)
        self.qdrant_url = QLineEdit(p.get("qdrant_url", ""))
        self.qdrant_url.setPlaceholderText(
            "qdrant only; blank = embedded on-disk (./rag_qdrant, no server). "
            "Server: http://host:6333")
        embedding.row("  ↳ Qdrant URL:", self.qdrant_url)
        self.qdrant_api_key = QLineEdit(p.get("qdrant_api_key", ""))
        self.qdrant_api_key.setPlaceholderText("qdrant server only; blank = none / embedded")
        embedding.row("  ↳ Qdrant API key:", self.qdrant_api_key)
        v.addWidget(embedding)

        v.addWidget(_hint("Linked agents get a search tool over this folder "
                          "(.txt/.md/.csv/.py/...). Several RAG nodes on one "
                          "agent each become their own tool; the description "
                          "tells the agent which one to use."))
        _buttons(self, outer)      # OK/Cancel stay outside the scroll, always visible

    def on_browse(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choose the documents folder")
        if path:
            self.docs_dir.setText(path)

    def apply(self) -> str | None:
        try:
            chunk = int(self.chunk.text().strip())
            top_k = int(self.top_k.text().strip())
            overlap = int(self.chunk_overlap.text().strip() or 0)
            recall_n = int(self.recall_n.text().strip() or 0)
            corrective_max = max(0, int(self.corrective_max.text().strip() or 2))
            parent_chunk = max(200, int(self.parent_chunk.text().strip() or 2400))
        except ValueError:
            return ("Chunk chars, top-k, overlap, recall N, max retries and parent "
                    "chars must be integers.")
        try:
            mmr_lambda = float(self.mmr_lambda.text().strip() or 0.5)
        except ValueError:
            return "MMR lambda must be a number between 0 and 1."
        try:
            score_threshold = float(self.score_threshold.text().strip() or 0.0)
        except ValueError:
            return "Score threshold must be a number (or blank)."
        try:
            multi_query_n = max(1, int(self.multi_query_n.text().strip() or 3))
        except ValueError:
            return "Multi-query N must be a whole number."
        self.node.name = self.name.text().strip() or self.node.name
        self.node.props.update(
            docs_dir=self.docs_dir.text().strip(),
            chunk_chars=chunk, top_k=top_k,
            description=self.description.toPlainText().strip(),
            chunk_strategy=self.chunk_strategy.currentText(),
            chunk_overlap=overlap,
            retrieval_granularity=self.granularity.currentText(),
            parent_chunk_chars=parent_chunk,
            retrieval_algorithm=self.retrieval.currentText(),
            recall_n=recall_n,
            mmr=self.mmr.isChecked(),
            mmr_lambda=mmr_lambda,
            rerank_mode=self.rerank_mode.currentText(),
            rerank_model=self.rerank_model.text().strip(),
            grade_docs=self.grade_docs.isChecked(),
            corrective=self.corrective.isChecked(),
            corrective_max_rewrites=corrective_max,
            query_transform=self.query_transform.currentText(),
            score_threshold=score_threshold,
            metadata_filter=self.metadata_filter.text().strip(),
            multi_query_n=multi_query_n,
            embed_provider=self.embed_provider.currentText(),
            embed_model=self.embed_model.text().strip(),
            embed_base_url=self.embed_base_url.text().strip(),
            embed_api_key=self.embed_api_key.text().strip(),
            normalize=self.normalize.isChecked(),
            vector_db=self.vector_db.currentText(),
            qdrant_url=self.qdrant_url.text().strip(),
            qdrant_api_key=self.qdrant_api_key.text().strip(),
            evict_used=self.evict_used.isChecked(),
        )
        return None


class WebServerDialog(QDialog):
    def __init__(self, parent, node: Node):
        super().__init__(parent)
        self.setWindowTitle(f"Configure WebServer: {node.name}")
        self.node = node
        self.resize(560, 520)
        outer = QVBoxLayout(self)
        _scroll = QScrollArea()
        _scroll.setWidgetResizable(True)
        _inner = QWidget()
        v = QVBoxLayout(_inner)
        _scroll.setWidget(_inner)
        outer.addWidget(_scroll, 1)
        form = QFormLayout()
        self.name = QLineEdit(node.name)
        self.host = QLineEdit(node.props.get("host", "127.0.0.1"))
        self.port = QLineEdit(str(node.props.get("port", 8765)))
        self.token = QLineEdit(node.props.get("auth_token", ""))
        for label, ctrl in (("Name:", self.name), ("Host:", self.host),
                            ("Port:", self.port), ("Auth token (optional):", self.token)):
            form.addRow(label, ctrl)
        v.addLayout(form)
        v.addWidget(_hint("Generates server.py: clients connect via WebSocket, "
                          "send a task and receive live traces + the result."))

        # ── Extra Settings (TLS / CORS / limits / headless HITL) ─────────────
        xg = _Collapsible("Extra Settings")
        self.auto_allow = QCheckBox("Auto-approve tool calls (headless — no HITL prompt)")
        self.auto_allow.setChecked(bool(node.props.get("auto_allow_tools", False)))
        xg.span(self.auto_allow)
        self.autostart = QCheckBox(
            "Start the server automatically when the app launches")
        self.autostart.setToolTip(
            "Desktop app (gui.py): open the WebSocket port on launch instead of "
            "waiting for the Server menu. Headless server.py always listens on start, "
            "so this only affects the GUI app.")
        self.autostart.setChecked(bool(node.props.get("autostart", False)))
        xg.span(self.autostart)
        self.tls_cert = QLineEdit(node.props.get("tls_cert", ""))
        self.tls_cert.setPlaceholderText("path to cert .pem (wss:// — needs key too)")
        xg.row("TLS cert:", self.tls_cert)
        self.tls_key = QLineEdit(node.props.get("tls_key", ""))
        self.tls_key.setPlaceholderText("path to private key .pem")
        xg.row("TLS key:", self.tls_key)
        self.origins = QLineEdit(", ".join(node.props.get("allowed_origins") or []))
        self.origins.setPlaceholderText("CORS origins, comma-separated (blank = any)")
        xg.row("Allowed origins:", self.origins)
        self.max_conns = QLineEdit(str(node.props.get("max_connections", 0) or ""))
        self.max_conns.setPlaceholderText("0 = unlimited")
        xg.row("Max connections:", self.max_conns)
        v.addWidget(xg)
        v.addStretch(1)
        _buttons(self, outer)      # OK/Cancel stay outside the scroll, always visible

    def apply(self) -> str | None:
        try:
            port = int(self.port.text().strip())
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            return "Port must be an integer between 1 and 65535."
        cert, key = self.tls_cert.text().strip(), self.tls_key.text().strip()
        if bool(cert) != bool(key):
            return "TLS needs BOTH a cert and a key (or neither)."
        mc = self.max_conns.text().strip()
        if mc:
            try:
                if int(mc) < 0:
                    raise ValueError
            except ValueError:
                return "Max connections must be a whole number (or blank)."
        self.node.name = self.name.text().strip() or self.node.name
        self.node.props.update(
            host=self.host.text().strip() or "127.0.0.1",
            port=port, auth_token=self.token.text().strip(),
            auto_allow_tools=self.auto_allow.isChecked(),
            autostart=self.autostart.isChecked(),
            tls_cert=cert, tls_key=key,
            allowed_origins=[o.strip() for o in self.origins.text().split(",") if o.strip()],
            max_connections=int(mc) if mc else 0)
        return None


class McpDialog(QDialog):
    TRANSPORTS = ["stdio", "streamable_http", "sse"]

    def __init__(self, parent, node: Node):
        super().__init__(parent)
        self.setWindowTitle(f"Configure MCP client: {node.name}")
        self.node = node
        self.resize(560, 620)
        outer = QVBoxLayout(self)
        _scroll = QScrollArea()
        _scroll.setWidgetResizable(True)
        _inner = QWidget()
        v = QVBoxLayout(_inner)
        _scroll.setWidget(_inner)
        outer.addWidget(_scroll, 1)
        form = QFormLayout()
        self.name = QLineEdit(node.name)
        self.transport = QComboBox()
        self.transport.addItems(self.TRANSPORTS)
        self.transport.setCurrentText(node.props.get("transport", "streamable_http"))
        self.command = QLineEdit(node.props.get("command", ""))
        self.args = QLineEdit(node.props.get("args", ""))
        self.url = QLineEdit(node.props.get("url", ""))
        self.verify_tls = QCheckBox("verify TLS certificate (uncheck for self-signed https)")
        self.verify_tls.setChecked(bool(node.props.get("verify_tls", True)))
        for label, ctrl in (("Name:", self.name), ("Transport:", self.transport),
                            ("Command (stdio):", self.command), ("Args (stdio):", self.args),
                            ("Server URL (http/sse):", self.url), ("TLS:", self.verify_tls)):
            form.addRow(label, ctrl)
        v.addLayout(form)
        self.test_btn = QPushButton("Test connection")
        self.test_btn.clicked.connect(self.on_test)
        v.addWidget(self.test_btn)
        v.addWidget(_hint("stdio launches a local server (command 'python', args "
                          "'my_server.py'). streamable_http URL usually ends in "
                          "/mcp; sse in /sse."))

        # ── Extra Settings (tool filter / timeouts / env / headers) ──────────
        xg = _Collapsible("Extra Settings")
        self.allow_tools = QLineEdit(node.props.get("allow_tools", ""))
        self.allow_tools.setPlaceholderText("blank = expose all; e.g. search, fetch")
        xg.row("Allow tools (comma-sep):", self.allow_tools)
        self.deny_tools = QLineEdit(node.props.get("deny_tools", ""))
        self.deny_tools.setPlaceholderText("hide these server tools")
        xg.row("Deny tools (comma-sep):", self.deny_tools)
        self.connect_timeout = QLineEdit(str(node.props.get("connect_timeout", 0) or ""))
        self.connect_timeout.setPlaceholderText("blank = 30s")
        xg.row("Connect timeout (s):", self.connect_timeout)
        self.call_timeout = QLineEdit(str(node.props.get("call_timeout", 0) or ""))
        self.call_timeout.setPlaceholderText("blank = 60s")
        xg.row("Call timeout (s):", self.call_timeout)
        xg.span(QLabel("Env vars (stdio) — KEY=value, one per line:"))
        self.env = _multiline(node.props.get("env", ""), 44)
        self.env.setPlaceholderText("KEY=value, one per line, e.g.\nGITHUB_TOKEN=ghp_xxx")
        xg.span(self.env)
        xg.span(QLabel("Headers (http/sse) — Header: value, one per line:"))
        self.headers = _multiline(node.props.get("headers", ""), 44)
        self.headers.setPlaceholderText(
            "Header: value, one per line, e.g.\nAuthorization: Bearer xxx")
        xg.span(self.headers)
        v.addWidget(xg)
        v.addStretch(1)

        self.transport.currentTextChanged.connect(lambda _t: self._sync())
        self._sync()
        _buttons(self, outer)      # OK/Cancel stay outside the scroll, always visible

    def on_test(self) -> None:
        tr = self.transport.currentText()
        if tr == "stdio":
            cmd = self.command.text().strip()
            if not cmd:
                QMessageBox.information(self, "Test connection", "Enter a command first.")
                return
            import shutil
            path = shutil.which(cmd)
            if path:
                QMessageBox.information(self, "stdio command found",
                                       f"OK — '{cmd}' is on PATH:\n{path}")
            else:
                QMessageBox.warning(self, "Command not found",
                                    f"'{cmd}' was not found on PATH.")
            return
        url = self.url.text().strip()
        if not url:
            QMessageBox.information(self, "Test connection", "Enter a server URL first.")
            return
        msg, ok = mcp_probe(url, self.verify_tls.isChecked())
        (QMessageBox.information if ok else QMessageBox.warning)(
            self, "MCP connection test", msg)

    def _sync(self) -> None:
        stdio = self.transport.currentText() == "stdio"
        self.command.setEnabled(stdio)
        self.args.setEnabled(stdio)
        self.url.setEnabled(not stdio)
        self.verify_tls.setEnabled(not stdio)
        self.env.setEnabled(stdio)              # env is stdio-only
        self.headers.setEnabled(not stdio)      # headers are http/sse-only

    def apply(self) -> str | None:
        tr = self.transport.currentText()
        if tr == "stdio" and not self.command.text().strip():
            return "stdio transport needs a command."
        if tr != "stdio" and not self.url.text().strip():
            return f"{tr} transport needs a server URL."
        for label, w in (("Connect timeout", self.connect_timeout),
                         ("Call timeout", self.call_timeout)):
            t = w.text().strip()
            if t:
                try:
                    if int(t) < 0:
                        raise ValueError
                except ValueError:
                    return f"{label} must be a whole number of seconds (or blank)."
        self.node.name = self.name.text().strip() or self.node.name
        self.node.props.update(transport=tr, command=self.command.text().strip(),
                               args=self.args.text().strip(), url=self.url.text().strip(),
                               verify_tls=self.verify_tls.isChecked(),
                               allow_tools=self.allow_tools.text().strip(),
                               deny_tools=self.deny_tools.text().strip(),
                               connect_timeout=self.connect_timeout.text().strip(),
                               call_timeout=self.call_timeout.text().strip(),
                               env=self.env.toPlainText().strip(),
                               headers=self.headers.toPlainText().strip())
        return None


class _EvalCaseDialog(QDialog):
    """One eval case: an input plus a grader from the full registry. contains /
    regex / judge with no extra options save the legacy keys (byte-identical); any
    other grader, or a negate/case-sensitive/param, saves the modern `type` form."""
    # (display label, grader key) — distinct registry graders (aliases deduped)
    _GRADERS = [
        ("contains (substring)", "contains"),
        ("does NOT contain", "not_contains"),
        ("contains ALL of (list)", "contains_all"),
        ("contains ANY of (list)", "contains_any"),
        ("equals (exact)", "equals"),
        ("starts with", "starts_with"),
        ("ends with", "ends_with"),
        ("regex matches", "regex"),
        ("regex does NOT match", "not_regex"),
        ("is valid JSON", "is_json"),
        ("JSON has keys (list)", "json_has_keys"),
        ("numeric (± tolerance)", "numeric"),
        ("similar (fuzzy ≥ threshold)", "similar"),
        ("length in range", "length"),
        ("judge (LLM rubric)", "llm_rubric"),
    ]
    _LEGACY = {"contains": "expected_output", "regex": "expected_regex",
               "llm_rubric": "judge"}
    _LIST = {"contains_all", "contains_any", "json_has_keys"}
    _NO_VALUE = {"is_json", "length"}
    # graders that honour a case-sensitive flag (string matching)
    _CSENS = {"contains", "not_contains", "contains_all", "contains_any", "equals",
              "starts_with", "ends_with", "regex", "not_regex"}
    _VALUE_LABEL = {
        "contains_all": "Substrings (comma / newline separated):",
        "contains_any": "Substrings (comma / newline separated):",
        "json_has_keys": "Required keys (comma / newline separated):",
        "regex": "Regex pattern:", "not_regex": "Regex pattern:",
        "similar": "Reference text:", "numeric": "Expected number:",
        "llm_rubric": "Criterion the LLM checks:",
    }

    def __init__(self, parent, title: str, case: dict | None = None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(480, 520)
        case = case or {}
        v = QVBoxLayout(self)
        form = QFormLayout()
        self.cid = QLineEdit(case.get("id", ""))
        form.addRow("Case id:", self.cid)
        v.addLayout(form)
        v.addWidget(QLabel("Input (sent to the agent):"))
        self.input = _multiline(case.get("input", ""), 70)
        v.addWidget(self.input)

        gr = QHBoxLayout()
        gr.addWidget(QLabel("Pass when answer:"))
        self.grade = QComboBox()
        for label, key in self._GRADERS:
            self.grade.addItem(label, key)
        gr.addWidget(self.grade)
        gr.addStretch(1)
        v.addLayout(gr)

        self.value_lbl = QLabel("Expected substring:")
        v.addWidget(self.value_lbl)
        self.value = _multiline("", 60)
        v.addWidget(self.value)

        # per-grader params (shown/hidden by _on_type)
        self._pform = QFormLayout()
        self.tolerance = QLineEdit(); self.tolerance.setPlaceholderText("0")
        self._pform.addRow("Tolerance (±):", self.tolerance)
        self.threshold = QLineEdit(); self.threshold.setPlaceholderText("0.8")
        self._pform.addRow("Similarity threshold (0-1):", self.threshold)
        self.lmin = QLineEdit(); self.lmax = QLineEdit()
        self._pform.addRow("Min length:", self.lmin)
        self._pform.addRow("Max length:", self.lmax)
        v.addLayout(self._pform)
        self.case_sensitive = QCheckBox("Case-sensitive")
        v.addWidget(self.case_sensitive)
        self.negate = QCheckBox("Negate — pass when this does NOT hold")
        v.addWidget(self.negate)
        _buttons(self, v)

        self.grade.currentIndexChanged.connect(self._on_type)
        self._load(case)

    def _key(self) -> str:
        return self.grade.currentData()

    def _load(self, case: dict) -> None:
        """Populate from a case dict, mirroring the runtime's precedence (a hand-
        authored multi-check `checks` list loads its FIRST check, best-effort)."""
        if isinstance(case.get("checks"), list) and case["checks"]:
            a = next((c for c in case["checks"] if isinstance(c, dict) and c.get("type")),
                     {"type": "contains"})
        elif case.get("type"):
            a = {k: v for k, v in case.items() if k not in ("id", "input")}
        elif case.get("judge"):
            a = {"type": "llm_rubric", "value": case["judge"]}
        elif case.get("expected_regex"):
            a = {"type": "regex", "value": case["expected_regex"]}
        else:
            a = {"type": "contains", "value": case.get("expected_output", "")}
        idx = self.grade.findData(a.get("type", "contains"))
        self.grade.setCurrentIndex(idx if idx >= 0 else 0)
        val = a.get("value", "")
        if isinstance(val, list):
            val = "\n".join(str(x) for x in val)
        self.value.setPlainText(str(val))
        self.case_sensitive.setChecked(bool(a.get("case_sensitive")))
        self.negate.setChecked(bool(a.get("not")))
        for fld, w in (("tolerance", self.tolerance), ("threshold", self.threshold),
                       ("min", self.lmin), ("max", self.lmax)):
            if a.get(fld) not in (None, ""):
                w.setText(str(a[fld]))
        self._on_type()

    def _on_type(self, *_a) -> None:
        key = self._key()
        self.value_lbl.setText(self._VALUE_LABEL.get(key, "Expected substring:"))
        has_value = key not in self._NO_VALUE
        self.value_lbl.setVisible(has_value)
        self.value.setVisible(has_value)
        self.case_sensitive.setVisible(key in self._CSENS)
        # hide whole rows (label + field) for params that don't apply
        self._pform.setRowVisible(self.tolerance, key == "numeric")
        self._pform.setRowVisible(self.threshold, key == "similar")
        self._pform.setRowVisible(self.lmin, key == "length")
        self._pform.setRowVisible(self.lmax, key == "length")

    def result(self) -> dict:
        key = self._key()
        case = {"id": self.cid.text().strip() or "case",
                "input": self.input.toPlainText().strip()}
        negate = self.negate.isChecked()
        csens = self.case_sensitive.isChecked() and key in self._CSENS
        val = self.value.toPlainText().strip()
        # Legacy fast-path (byte-identical): plain contains/regex/judge, no options.
        if key in self._LEGACY and not negate and not csens and key not in self._LIST:
            case[self._LEGACY[key]] = val
            return case
        a: dict = {"type": key}
        if key in self._LIST:
            a["value"] = [s.strip() for s in val.replace(",", "\n").splitlines()
                          if s.strip()]
        elif key not in self._NO_VALUE:
            a["value"] = val
        if csens:
            a["case_sensitive"] = True
        if key == "numeric" and self.tolerance.text().strip():
            try:
                a["tolerance"] = float(self.tolerance.text().strip())
            except ValueError:
                pass
        if key == "similar" and self.threshold.text().strip():
            try:
                a["threshold"] = float(self.threshold.text().strip())
            except ValueError:
                pass
        if key == "length":
            for fld, w in (("min", self.lmin), ("max", self.lmax)):
                if w.text().strip():
                    try:
                        a[fld] = int(w.text().strip())
                    except ValueError:
                        pass
        if negate:
            a["not"] = True
        case.update(a)
        return case


class EvalDialog(QDialog):
    def __init__(self, parent, node: Node):
        super().__init__(parent)
        self.setWindowTitle(f"Configure Eval: {node.name}")
        self.node = node
        self._cases = [dict(c) for c in node.props.get("cases", [])]
        v = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(node.name)
        form.addRow("Name:", self.name)
        v.addLayout(form)
        v.addWidget(QLabel("Eval cases:"))
        self.listw = QListWidget()
        self.listw.setMinimumSize(440, 170)
        v.addWidget(self.listw)
        v.addLayout(_list_edit_row(("Add...", self.on_add),
                                   ("Edit...", self.on_edit),
                                   ("Remove", self.on_remove)))
        v.addWidget(_hint("Link eval → agent to test that agent alone; leave it "
                          "unlinked to test the whole pipeline. Run via "
                          "'python run_evals.py'."))
        self._reload()
        _buttons(self, v)

    def _reload(self) -> None:
        self.listw.clear()
        for c in self._cases:
            if isinstance(c.get("checks"), list):
                kind = f"checks×{len(c['checks'])}"
            elif c.get("type"):
                kind = str(c["type"]) + ("¬" if c.get("not") else "")
            elif c.get("judge"):
                kind = "judge"
            elif c.get("expected_regex"):
                kind = "regex"
            else:
                kind = "contains"
            self.listw.addItem(f"[{kind}] {c.get('id', '?')}: {(c.get('input') or '')[:40]}")

    def on_add(self) -> None:
        dlg = _EvalCaseDialog(self, "Add eval case")
        if dlg.exec() == QDialog.Accepted and dlg.result()["input"]:
            self._cases.append(dlg.result())
            self._reload()

    def on_edit(self) -> None:
        i = self.listw.currentRow()
        if i < 0:
            return
        dlg = _EvalCaseDialog(self, "Edit eval case", self._cases[i])
        if dlg.exec() == QDialog.Accepted and dlg.result()["input"]:
            self._cases[i] = dlg.result()
            self._reload()

    def on_remove(self) -> None:
        i = self.listw.currentRow()
        if i >= 0:
            self._cases.pop(i)
            self._reload()

    def apply(self) -> str | None:
        self.node.props["cases"] = self._cases
        self.node.name = self.name.text().strip() or self.node.name
        return None


_STATE_BLANK = {"int": 0, "float": 0.0, "bool": False, "list": [], "dict": {}}


def _coerce_state_default(ftype: str, text: str):
    """Parse a state field's default-value string for its type.
    Returns (value, None) on success or (None, error_message)."""
    if ftype == "str":
        return text, None
    if not text:
        return _STATE_BLANK[ftype], None
    if ftype == "bool":
        low = text.lower()
        if low in ("true", "1", "yes"):
            return True, None
        if low in ("false", "0", "no"):
            return False, None
        return None, "Default for bool must be true or false."
    try:
        if ftype == "int":
            return int(text), None
        if ftype == "float":
            return float(text), None
        val = json.loads(text)            # list / dict
    except ValueError:
        return None, f"Could not parse the default as {ftype}."
    if ftype == "list" and not isinstance(val, list):
        return None, "Default must be a JSON list, e.g. [] or [1, 2]."
    if ftype == "dict" and not isinstance(val, dict):
        return None, 'Default must be a JSON object, e.g. {} or {"k": 1}.'
    return val, None


class _StateFieldDialog(QDialog):
    """Add / edit one shared-state field (name, type, reducer, default)."""

    def __init__(self, parent, title: str, field: dict | None = None,
                 taken: set | None = None, type_defs: dict | None = None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self._taken = taken or set()
        self._type_defs = type_defs or {}
        field = field or {}
        v = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(field.get("name", ""))
        form.addRow("Field name:", self.name)
        self.type = QComboBox()
        # native scalars/containers + every custom type + list[CustomType]
        opts = list(STATE_TYPES)
        for tn in sorted(self._type_defs):
            opts += [tn, f"list[{tn}]"]
        self.type.addItems(opts)
        cur_t = field.get("type", "str")
        if cur_t not in opts:                       # a type that no longer exists
            self.type.addItem(cur_t)
        self.type.setCurrentText(cur_t)
        form.addRow("Type:", self.type)
        self.reducer = QComboBox()
        form.addRow("Update (reducer):", self.reducer)
        self.merge_key = QLineEdit(field.get("merge_key", ""))
        self.merge_key.setPlaceholderText("id field to merge records by (upsert_by_key)")
        self._merge_key_label = QLabel("  ↳ merge key:")
        form.addRow(self._merge_key_label, self.merge_key)
        self.default = QLineEdit(self._default_text(field))
        form.addRow("Default:", self.default)
        self.description = QPlainTextEdit(field.get("description", ""))
        self.description.setMinimumHeight(72)        # ~3 lines, wraps + scrolls
        self.description.setMinimumWidth(320)
        self.description.setTabChangesFocus(True)    # Tab moves on, doesn't insert
        form.addRow("Description:", self.description)
        v.addLayout(form)
        v.addWidget(_hint("Update = how repeated/concurrent writes combine: overwrite "
                          "(last wins), append/extend (lists), add/max/min (numbers), "
                          "merge_shallow/merge_deep (records), upsert_by_key (merge a "
                          "list of records by an id). Custom/nested types are defined in "
                          "Graph → Define Types. Defaults are JSON for containers ([], {})."))
        self.type.currentTextChanged.connect(self._sync_reducers)
        self.reducer.currentTextChanged.connect(self._sync_merge_key)
        self._sync_reducers()
        if field.get("reducer"):
            self.reducer.setCurrentText(field["reducer"])
        self._sync_merge_key()
        _buttons(self, v)

    @staticmethod
    def _default_text(field) -> str:
        d = field.get("default")
        if d is None:
            return ""
        if isinstance(d, bool):
            return "true" if d else "false"
        if isinstance(d, (list, dict)):
            return json.dumps(d)
        return str(d)

    def _sync_reducers(self) -> None:
        cur = self.reducer.currentText()
        opts = merge_policies_for(self.type.currentText(), self._type_defs)
        self.reducer.clear()
        self.reducer.addItems(opts)
        if cur in opts:
            self.reducer.setCurrentText(cur)
        self._sync_merge_key()

    def _sync_merge_key(self) -> None:
        show = self.reducer.currentText() == "upsert_by_key"
        self.merge_key.setVisible(show)
        self._merge_key_label.setVisible(show)

    def result(self) -> dict | None:
        """Validated field, or None (a message box has already been shown)."""
        name = self.name.text().strip()
        if not name.isidentifier():
            QMessageBox.warning(self, "Invalid name",
                                "Field name must be a valid identifier: letters, "
                                "digits and underscores, not starting with a digit.")
            return None
        if name in RESERVED_STATE_NAMES:
            QMessageBox.warning(self, "Reserved name",
                                f"'{name}' is a built-in, auto-maintained field "
                                "and can't be redefined or renamed.")
            return None
        if name in self._taken:
            QMessageBox.warning(self, "Duplicate name",
                                f"A field named '{name}' already exists.")
            return None
        ftype = self.type.currentText()
        reducer = self.reducer.currentText()
        raw_default = self.default.text().strip()
        if is_custom_type(ftype):
            # custom record / list[Type]: default is JSON (blank = {} or [])
            if not raw_default:
                value = [] if ftype.startswith("list[") else {}
            else:
                try:
                    value = json.loads(raw_default)
                except (json.JSONDecodeError, ValueError) as e:
                    QMessageBox.warning(self, "Invalid default",
                                        f"Default for a custom type must be JSON: {e}")
                    return None
        else:
            value, err = _coerce_state_default(ftype, raw_default)
            if err:
                QMessageBox.warning(self, "Invalid default", err)
                return None
        mkey = self.merge_key.text().strip()
        if reducer == "upsert_by_key" and not mkey:
            QMessageBox.warning(self, "Merge key required",
                                "upsert_by_key needs a merge key — the id field to "
                                "merge records by (e.g. 'id').")
            return None
        rec = {"name": name, "type": ftype, "reducer": reducer, "default": value,
               "description": self.description.toPlainText().strip()}
        if mkey and reducer == "upsert_by_key":
            rec["merge_key"] = mkey
        return rec


class StateSchemaDialog(QDialog):
    """Graph-level editor for the shared-state schema: declare typed state
    fields agents can read/write. (Data model only for now — codegen wires the
    state through in a later step.)"""

    def __init__(self, parent, graph):
        super().__init__(parent)
        self.setWindowTitle("Shared state schema")
        self.graph = graph
        self._fields = [dict(f) for f in state_fields(graph, include_builtins=False)]
        # Built-ins for THIS graph (todos appears only when an agent opted in).
        self._builtins = [f for f in state_fields(graph) if f.get("builtin")]
        self._n_builtin = len(self._builtins)          # locked rows shown first
        v = QVBoxLayout(self)
        v.addWidget(QLabel("Shared-state fields (read/written by agent stages):"))
        self.listw = QListWidget()
        self.listw.setMinimumSize(460, 180)
        self.listw.itemDoubleClicked.connect(lambda *_: self.on_edit())
        v.addWidget(self.listw)
        v.addLayout(_list_edit_row(("Add...", self.on_add),
                                   ("Edit...", self.on_edit),
                                   ("Remove", self.on_remove)))
        v.addWidget(_hint("A graph-wide, typed scratchpad agents share. Each "
                          "field's description is added to every agent's prompt. "
                          "Per-agent reads/writes are set in each agent's dialog."))
        v.addWidget(_hint("The locked built-in fields (shown first on every graph) "
                          "are auto-maintained — tools called fill tool_calls, "
                          "agents visited fill agents. Read them via an agent's "
                          "reads or a Condition; they can't be edited or written."))
        # Graph-mode loop guard (Condition back-edges). 0 = auto.
        lh = QHBoxLayout()
        lh.addWidget(QLabel("Max loop steps (graph mode, 0 = auto):"))
        self.limit = QSpinBox()
        self.limit.setRange(0, 100000)
        self.limit.setValue(int(getattr(graph, "recursion_limit", 0) or 0))
        lh.addWidget(self.limit)
        lh.addStretch(1)
        v.addLayout(lh)
        # Whole-run wall-clock deadline (0 = none). Stops the WHOLE run when total
        # time exceeds it (checked between steps/hops — not mid-call). Distinct from a
        # single agent's max_wall_clock_s (Agent → Extra Settings).
        rh = QHBoxLayout()
        rh.addWidget(QLabel("Max run wall-clock (seconds, 0 = none):"))
        self.run_wall = QSpinBox()
        self.run_wall.setRange(0, 86400)
        self.run_wall.setValue(int(getattr(graph, "run_wall_clock_s", 0) or 0))
        rh.addWidget(self.run_wall)
        rh.addStretch(1)
        v.addLayout(rh)
        self._reload()
        _buttons(self, v)

    def _names(self, exclude: int = -1) -> set:
        return {f["name"] for i, f in enumerate(self._fields) if i != exclude}

    def _reload(self) -> None:
        self.listw.clear()
        # Built-in fields first — locked rows so the user sees them but can't touch
        # them. tool_calls/agents are always present; todos appears only when an
        # agent enabled the write_todos tool.
        for f in self._builtins:
            it = QListWidgetItem(
                f"\U0001F512 {f['name']} : {f['type']}  [{f['reducer']}]  "
                f"(built-in) — {f['description']}")
            it.setFlags(Qt.NoItemFlags)         # not selectable / not editable
            self.listw.addItem(it)
        for f in self._fields:
            dv = (json.dumps(f["default"])
                  if isinstance(f["default"], (list, dict)) else f["default"])
            row = f"{f['name']} : {f['type']}  [{f['reducer']}]  = {dv}"
            desc = (f.get("description") or "").strip()
            if desc:
                row += f"   — {desc}"
            self.listw.addItem(row)

    def on_add(self) -> None:
        dlg = _StateFieldDialog(self, "Add state field", taken=self._names(),
                                type_defs=getattr(self.graph, "type_defs", None))
        if dlg.exec() == QDialog.Accepted:
            r = dlg.result()
            if r:
                self._fields.append(r)
                self._reload()

    def on_edit(self) -> None:
        i = self.listw.currentRow() - self._n_builtin
        if i < 0:                              # a locked built-in row (or none)
            return
        dlg = _StateFieldDialog(self, "Edit state field", self._fields[i],
                                taken=self._names(exclude=i),
                                type_defs=getattr(self.graph, "type_defs", None))
        if dlg.exec() == QDialog.Accepted:
            r = dlg.result()
            if r:
                self._fields[i] = r
                self._reload()

    def on_remove(self) -> None:
        i = self.listw.currentRow() - self._n_builtin
        if i >= 0:
            self._fields.pop(i)
            self._reload()

    def apply(self) -> None:
        self.graph.state_schema = self._fields
        self.graph.recursion_limit = self.limit.value()
        self.graph.run_wall_clock_s = self.run_wall.value()


def open_state_schema_dialog(parent, graph) -> bool:
    """Edit a graph's shared-state schema. Returns True if applied (accepted)."""
    dlg = StateSchemaDialog(parent, graph)
    if dlg.exec() == QDialog.Accepted:
        dlg.apply()
        return True
    return False


# ── custom / nested state types (Approach A) ─────────────────────────────────
# Map the visual sub-field type picker <-> a JSON-Schema node. A sub-field's type
# is a native primitive, object/array, a custom type Name ($type ref), or list[Name].
_VISUAL_PRIM = {"str": "string", "int": "integer", "float": "number",
                "bool": "boolean"}


def _visual_type_to_schema(t: str) -> dict:
    if t in _VISUAL_PRIM:
        return {"type": _VISUAL_PRIM[t]}
    if t in ("object", "dict"):
        return {"type": "object"}
    if t in ("array", "list"):
        return {"type": "array", "items": {}}
    m = re.match(r"^list\[(\w+)\]$", t or "")
    if m:
        return {"type": "array", "items": {"$type": m.group(1)}}
    return {"$type": t}                       # a custom type reference


def _schema_to_visual_type(node) -> str:
    if not isinstance(node, dict):
        return "str"
    if isinstance(node.get("$type"), str):
        return node["$type"]
    jt = node.get("type")
    inv = {v: k for k, v in _VISUAL_PRIM.items()}
    if jt in inv:
        return inv[jt]
    if jt == "object":
        return "object"
    if jt == "array":
        items = node.get("items") or {}
        if isinstance(items.get("$type"), str):
            return f"list[{items['$type']}]"
        return "array"
    return "str"


class _TypeFieldDialog(QDialog):
    """One sub-field of a record type: name + type (native / object / array /
    custom type / list[CustomType])."""

    def __init__(self, parent, title, name="", ftype="str", type_names=(), taken=()):
        super().__init__(parent)
        self.setWindowTitle(title)
        self._taken = set(taken)
        v = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(name)
        form.addRow("Property name:", self.name)
        self.type = QComboBox()
        opts = list(STATE_TYPES) + ["object", "array"]
        for tn in sorted(type_names):
            opts += [tn, f"list[{tn}]"]
        if ftype not in opts:
            opts.append(ftype)
        self.type.addItems(opts)
        self.type.setCurrentText(ftype or "str")
        form.addRow("Type:", self.type)
        v.addLayout(form)
        v.addWidget(_hint("Reference another custom type by name to nest records; "
                          "use list[Type] for a list of them."))
        _buttons(self, v)

    def result(self):
        nm = self.name.text().strip()
        if not nm.isidentifier():
            QMessageBox.warning(self, "Invalid name",
                                "Property name must be a valid identifier.")
            return None
        if nm in self._taken:
            QMessageBox.warning(self, "Duplicate", f"'{nm}' already exists.")
            return None
        return (nm, self.type.currentText())


class _TypeDefDialog(QDialog):
    """Define/edit ONE custom type: name, description, default merge policy +
    merge_key, and the JSON Schema — authored visually (record sub-fields) OR as
    raw JSON (two tabs kept in sync)."""

    _POLICIES = ("overwrite", "merge_shallow", "merge_deep",
                 "append", "extend", "upsert_by_key", "custom")

    def __init__(self, parent, name="", td=None, taken=(), type_names=()):
        super().__init__(parent)
        self.setWindowTitle("Define type" if not name else f"Edit type: {name}")
        self.resize(560, 600)
        td = td or {}
        self._taken = set(taken)
        self._type_names = [t for t in type_names if t != name]
        v = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(name)
        form.addRow("Type name:", self.name)
        self.description = QLineEdit(td.get("description", ""))
        form.addRow("Description:", self.description)
        self.merge = QComboBox(); self.merge.addItems(self._POLICIES)
        self.merge.setCurrentText(td.get("merge") or "merge_deep")
        form.addRow("Default update:", self.merge)
        self.merge_key = QLineEdit(td.get("merge_key", ""))
        self.merge_key.setPlaceholderText("id field for upsert_by_key")
        form.addRow("  ↳ merge key:", self.merge_key)
        v.addLayout(form)

        # P4 escape hatch: a custom merge function (shown only for merge=custom)
        self._merge_src_lbl = QLabel("Custom merge — def merge(old, new):")
        v.addWidget(self._merge_src_lbl)
        self.merge_src = _multiline(td.get("merge_src", ""), 96)
        self.merge_src.setPlaceholderText(
            "def merge(old, new):\n    # return the merged value\n    return new")
        v.addWidget(self.merge_src)
        self.merge.currentTextChanged.connect(self._sync_merge_src)
        self._sync_merge_src()

        self.tabs = QTabWidget()
        # visual tab
        vis = QWidget(); vl = QVBoxLayout(vis)
        vl.addWidget(QLabel("Record properties:"))
        self.listw = QListWidget(); self.listw.setMinimumHeight(170)
        self.listw.itemDoubleClicked.connect(lambda *_: self.on_edit())
        vl.addWidget(self.listw)
        vl.addLayout(_list_edit_row(("Add...", self.on_add),
                                    ("Edit...", self.on_edit),
                                    ("Remove", self.on_remove)))
        self.tabs.addTab(vis, "Fields (visual)")
        # json tab
        jt = QWidget(); jl = QVBoxLayout(jt)
        jl.addWidget(QLabel("JSON Schema:"))
        self.jsonbox = _multiline("", 160)
        jl.addWidget(self.jsonbox)
        self.tabs.addTab(jt, "JSON Schema")
        v.addWidget(self.tabs)
        v.addWidget(_hint("Visual builds an object (record) type. Use the JSON tab "
                          "for arrays or advanced schema. The tabs sync when you "
                          "switch; on OK the ACTIVE tab wins."))

        # seed from existing schema (prefer visual if it's a plain object)
        schema = td.get("schema") if isinstance(td.get("schema"), dict) else {"type": "object", "properties": {}}
        self._fields = []                       # [(name, visual_type)]
        props = schema.get("properties") if schema.get("type") == "object" else None
        if isinstance(props, dict):
            self._fields = [(k, _schema_to_visual_type(v)) for k, v in props.items()]
        self.jsonbox.setPlainText(json.dumps(schema, indent=2, ensure_ascii=False))
        self._reload()
        self.tabs.currentChanged.connect(self._on_tab)
        self._last_tab = 0
        _buttons(self, v)

    # visual list management
    def _names(self, exclude=-1):
        return {n for i, (n, _) in enumerate(self._fields) if i != exclude}

    def _reload(self):
        self.listw.clear()
        for n, t in self._fields:
            self.listw.addItem(f"{n} : {t}")

    def on_add(self):
        d = _TypeFieldDialog(self, "Add property", type_names=self._type_names,
                             taken=self._names())
        if d.exec() == QDialog.Accepted:
            r = d.result()
            if r:
                self._fields.append(r); self._reload()

    def on_edit(self):
        i = self.listw.currentRow()
        if i < 0:
            return
        n, t = self._fields[i]
        d = _TypeFieldDialog(self, "Edit property", n, t,
                             type_names=self._type_names, taken=self._names(exclude=i))
        if d.exec() == QDialog.Accepted:
            r = d.result()
            if r:
                self._fields[i] = r; self._reload()

    def on_remove(self):
        i = self.listw.currentRow()
        if i >= 0:
            self._fields.pop(i); self._reload()

    def _sync_merge_src(self) -> None:
        show = self.merge.currentText() == "custom"
        self._merge_src_lbl.setVisible(show)
        self.merge_src.setVisible(show)

    def _visual_schema(self) -> dict:
        return {"type": "object",
                "properties": {n: _visual_type_to_schema(t) for n, t in self._fields}}

    def _on_tab(self, idx):
        # sync the tab being LEFT into the one being entered
        if self._last_tab == 0 and idx == 1:            # visual -> json
            self.jsonbox.setPlainText(
                json.dumps(self._visual_schema(), indent=2, ensure_ascii=False))
        elif self._last_tab == 1 and idx == 0:          # json -> visual (best effort)
            try:
                sc = json.loads(self.jsonbox.toPlainText() or "{}")
                props = sc.get("properties") if sc.get("type") == "object" else None
                if isinstance(props, dict):
                    self._fields = [(k, _schema_to_visual_type(v)) for k, v in props.items()]
                    self._reload()
            except (json.JSONDecodeError, ValueError):
                pass
        self._last_tab = idx

    def result(self):
        nm = self.name.text().strip()
        if not nm.isidentifier():
            QMessageBox.warning(self, "Invalid name", "Type name must be an identifier.")
            return None
        if nm in STATE_TYPES:
            QMessageBox.warning(self, "Reserved", f"'{nm}' shadows a built-in type.")
            return None
        if nm in RESERVED_STATE_NAMES:
            QMessageBox.warning(self, "Reserved", f"'{nm}' is a reserved name.")
            return None
        if nm in self._taken:
            QMessageBox.warning(self, "Duplicate", f"Type '{nm}' already exists.")
            return None
        if self.tabs.currentIndex() == 1:               # JSON tab active
            try:
                schema = json.loads(self.jsonbox.toPlainText() or "{}")
            except (json.JSONDecodeError, ValueError) as e:
                QMessageBox.warning(self, "Invalid JSON Schema", str(e)); return None
            if not isinstance(schema, dict):
                QMessageBox.warning(self, "Invalid JSON Schema",
                                    "The schema must be a JSON object."); return None
        else:
            schema = self._visual_schema()
        td = {"schema": schema, "merge": self.merge.currentText(),
              "description": self.description.text().strip()}
        mkey = self.merge_key.text().strip()
        if td["merge"] == "upsert_by_key" and not mkey:
            QMessageBox.warning(self, "Merge key required",
                                "upsert_by_key needs a merge key (the id field).")
            return None
        if mkey:
            td["merge_key"] = mkey
        # P4: custom merge function source (validated by TypeDefsDialog.apply →
        # validate_type_defs, which requires a top-level `def merge`).
        src = self.merge_src.toPlainText().strip()
        if td["merge"] == "custom" and not src:
            QMessageBox.warning(self, "Custom merge needed",
                                "merge 'custom' needs a Python def merge(old, new).")
            return None
        if src:
            td["merge_src"] = src
        return nm, td


class TypeDefsDialog(QDialog):
    """Graph-level manager for custom/nested state types (Approach A). Types here
    become available in the shared-state field editor as `Name` and `list[Name]`."""

    def __init__(self, parent, graph):
        super().__init__(parent)
        self.setWindowTitle("Define custom state types")
        self.graph = graph
        self._defs = {k: dict(v) for k, v in (getattr(graph, "type_defs", None) or {}).items()}
        v = QVBoxLayout(self)
        v.addWidget(QLabel("Custom / nested types (use as a state field's type, "
                           "or list[Type]):"))
        self.listw = QListWidget(); self.listw.setMinimumSize(480, 200)
        self.listw.itemDoubleClicked.connect(lambda *_: self.on_edit())
        v.addWidget(self.listw)
        v.addLayout(_list_edit_row(("Add...", self.on_add),
                                   ("Edit...", self.on_edit),
                                   ("Remove", self.on_remove)))
        v.addWidget(_hint("A named JSON-Schema type + a default update policy. The "
                          "schema shapes the set_state tool so the model emits "
                          "well-formed nested values. Reference one type from another "
                          "to nest; use list[Type] for a list of records."))
        self._reload()
        _buttons(self, v)

    def _reload(self):
        self.listw.clear()
        for name, td in self._defs.items():
            top = (td.get("schema") or {}).get("type", "?")
            nprops = len((td.get("schema") or {}).get("properties") or {})
            extra = f"{nprops} field(s)" if top == "object" else top
            mk = f" key={td['merge_key']}" if td.get("merge_key") else ""
            self.listw.addItem(f"{name}  [{td.get('merge','overwrite')}{mk}]  "
                               f"— {extra}")

    def on_add(self):
        d = _TypeDefDialog(self, taken=set(self._defs), type_names=list(self._defs))
        if d.exec() == QDialog.Accepted:
            r = d.result()
            if r:
                self._defs[r[0]] = r[1]; self._reload()

    def on_edit(self):
        i = self.listw.currentRow()
        if i < 0:
            return
        name = list(self._defs)[i]
        d = _TypeDefDialog(self, name, self._defs[name],
                           taken=set(self._defs) - {name}, type_names=list(self._defs))
        if d.exec() == QDialog.Accepted:
            r = d.result()
            if r:
                # a rename drops the old key
                if r[0] != name:
                    self._defs.pop(name, None)
                self._defs[r[0]] = r[1]; self._reload()

    def on_remove(self):
        i = self.listw.currentRow()
        if i >= 0:
            self._defs.pop(list(self._defs)[i], None); self._reload()

    def apply(self) -> str | None:
        errs = validate_type_defs(self._defs)
        if errs:
            return "\n".join(errs)
        self.graph.type_defs = self._defs
        return None


def open_type_defs_dialog(parent, graph) -> bool:
    """Manage a graph's custom/nested state types. Returns True if applied."""
    dlg = TypeDefsDialog(parent, graph)
    make_dialog_resizable(dlg)
    if dlg.exec() == QDialog.Accepted:
        err = dlg.apply()
        if err:
            QMessageBox.warning(parent, "Invalid types", err)
            return False
        return True
    return False


class StorageDialog(QDialog):
    """Per-graph storage backend for memory (chat sessions) + checkpoints:
    local disk (default), SQLite, or PostgreSQL."""

    _BACKENDS = [("disk", "Local disk — JSON files (default)"),
                 ("sqlite", "SQLite — single file, no extra dependency"),
                 ("postgres", "PostgreSQL — psycopg, shared/remote DB")]

    def __init__(self, parent, graph):
        super().__init__(parent)
        self.setWindowTitle("Storage / persistence")
        self.graph = graph
        st = dict(getattr(graph, "storage", None) or {})
        v = QVBoxLayout(self)
        form = QFormLayout()
        self.backend = QComboBox()
        for key, label in self._BACKENDS:
            self.backend.addItem(label, key)
        cur = (st.get("backend") or "disk").lower()
        self.backend.setCurrentIndex(
            next((i for i, (k, _) in enumerate(self._BACKENDS) if k == cur), 0))
        form.addRow("Backend:", self.backend)
        self.sqlite_path = QLineEdit(st.get("sqlite_path") or "memory.db")
        form.addRow("SQLite file:", self.sqlite_path)
        self.dsn = QLineEdit(st.get("dsn") or "")
        self.dsn.setPlaceholderText(
            "postgresql://user:pass@host:5432/db  (blank = read $DATABASE_URL)")
        form.addRow("Postgres DSN:", self.dsn)
        v.addLayout(form)
        v.addWidget(_hint("Where the generated agent stores conversation memory "
                          "(sessions) and graph checkpoints. Disk keeps the JSON "
                          "files under the app folder; SQLite needs no extra "
                          "dependency; PostgreSQL adds psycopg to requirements. "
                          "Leave the DSN blank to read $DATABASE_URL at runtime so "
                          "credentials stay out of config.json."))
        self.backend.currentIndexChanged.connect(self._sync)
        self._sync()
        _buttons(self, v)

    def _sync(self) -> None:
        b = self.backend.currentData()
        self.sqlite_path.setEnabled(b == "sqlite")
        self.dsn.setEnabled(b == "postgres")

    def apply(self) -> None:
        b = self.backend.currentData()
        if b == "sqlite":
            self.graph.storage = {"backend": "sqlite",
                                  "sqlite_path": self.sqlite_path.text().strip()
                                  or "memory.db"}
        elif b == "postgres":
            self.graph.storage = {"backend": "postgres",
                                  "dsn": self.dsn.text().strip()}
        else:                                   # disk = default, store nothing
            self.graph.storage = {}


def open_storage_dialog(parent, graph) -> bool:
    """Edit a graph's storage backend. Returns True if applied (accepted)."""
    dlg = StorageDialog(parent, graph)
    if dlg.exec() == QDialog.Accepted:
        dlg.apply()
        return True
    return False


# ── condition (If/Else) node ─────────────────────────────────────────────────
class _BranchDialog(QDialog):
    """One condition branch: a target stage + a predicate (empty = else)."""

    def __init__(self, parent, title: str, targets: list, branch: dict | None = None):
        super().__init__(parent)
        self.setWindowTitle(title)
        branch = branch or {}
        v = QVBoxLayout(self)
        form = QFormLayout()
        self.to = QComboBox()
        self.to.addItems(targets)
        if branch.get("to") in targets:
            self.to.setCurrentText(branch["to"])
        form.addRow("Go to:", self.to)
        self.expr = QLineEdit(branch.get("expr", ""))
        form.addRow("When (expr):", self.expr)
        v.addLayout(form)
        v.addWidget(_hint("Predicate over state fields, e.g. score < 0.5 or "
                          "len(notes) > 3. Leave EMPTY for the else/fallback. "
                          "Branches are tried top-to-bottom; first true wins."))
        _buttons(self, v)

    def result(self) -> dict:
        return {"to": self.to.currentText(), "expr": self.expr.text().strip()}


class ConditionDialog(QDialog):
    def __init__(self, parent, node: Node):
        super().__init__(parent)
        self.setWindowTitle(f"Configure If/Else: {node.name}")
        self.node = node
        graph = _graph_of(parent)
        self._targets = ([graph.nodes[s].name for s in graph.flow_successors(node.id)]
                         if graph is not None else [])
        self._fields = [f["name"] for f in state_fields(graph)] if graph is not None else []
        self._branches = [dict(b) for b in node.props.get("branches", [])]
        v = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(node.name)
        form.addRow("Name:", self.name)
        v.addLayout(form)
        v.addWidget(QLabel("Branches (first matching expr wins; empty expr = else):"))
        self.listw = QListWidget()
        self.listw.setMinimumSize(460, 160)
        self.listw.itemDoubleClicked.connect(lambda *_: self.on_edit())
        v.addWidget(self.listw)
        v.addLayout(_list_edit_row(("Add...", self.on_add),
                                   ("Edit...", self.on_edit),
                                   ("Remove", self.on_remove),
                                   ("↑", self.on_up), ("↓", self.on_down)))
        hint = "Outgoing links: " + (", ".join(self._targets)
                                     or "none yet — draw links to targets first")
        if self._fields:
            hint += ".   State fields: " + ", ".join(self._fields)
        v.addWidget(_hint(hint))
        self._reload()
        _buttons(self, v)

    def _reload(self) -> None:
        self.listw.clear()
        for b in self._branches:
            expr = (b.get("expr") or "").strip()
            self.listw.addItem(f"{expr or '(else)'}   →   {b.get('to', '?')}")

    def on_add(self) -> None:
        if not self._targets:
            QMessageBox.warning(self, "No targets", "Draw links from this node to "
                                "its target stages first, then add branches.")
            return
        dlg = _BranchDialog(self, "Add branch", self._targets)
        if dlg.exec() == QDialog.Accepted:
            self._branches.append(dlg.result())
            self._reload()

    def on_edit(self) -> None:
        i = self.listw.currentRow()
        if i < 0 or not self._targets:
            return
        dlg = _BranchDialog(self, "Edit branch", self._targets, self._branches[i])
        if dlg.exec() == QDialog.Accepted:
            self._branches[i] = dlg.result()
            self._reload()

    def on_remove(self) -> None:
        i = self.listw.currentRow()
        if i >= 0:
            self._branches.pop(i)
            self._reload()

    def _move(self, d: int) -> None:
        i = self.listw.currentRow()
        j = i + d
        if 0 <= i < len(self._branches) and 0 <= j < len(self._branches):
            self._branches[i], self._branches[j] = self._branches[j], self._branches[i]
            self._reload()
            self.listw.setCurrentRow(j)

    def on_up(self) -> None:
        self._move(-1)

    def on_down(self) -> None:
        self._move(1)

    def apply(self) -> str | None:
        self.node.name = self.name.text().strip() or self.node.name
        self.node.props["branches"] = self._branches
        return None


class WhileDialog(QDialog):
    """A loop guard: run the BODY successor while the condition holds, else take
    the EXIT successor. Compiles to the same routing primitive as a Condition —
    [(condition, body), (else, exit)] — so it reuses the deterministic runtime."""

    def __init__(self, parent, node: Node):
        super().__init__(parent)
        self.setWindowTitle(f"Configure While: {node.name}")
        self.node = node
        graph = _graph_of(parent)
        self._targets = ([graph.nodes[s].name for s in graph.flow_successors(node.id)]
                         if graph is not None else [])
        self._fields = [f["name"] for f in state_fields(graph)] if graph is not None else []
        v = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(node.name)
        form.addRow("Name:", self.name)
        self.condition = QLineEdit(node.props.get("condition", ""))
        self.condition.setPlaceholderText("e.g. attempts < 3 and not solved")
        form.addRow("Loop while (condition):", self.condition)
        self.body = QComboBox()
        self.body.addItems(self._targets)
        if node.props.get("body") in self._targets:
            self.body.setCurrentText(node.props["body"])
        form.addRow("Loop body (runs while true):", self.body)
        self.max_iters = QLineEdit(str(node.props.get("max_iterations", 0) or ""))
        self.max_iters.setPlaceholderText("blank/0 = only the graph recursion limit")
        form.addRow("Max iterations:", self.max_iters)
        v.addLayout(form)
        hint = ("The body runs while the condition is true, then must link BACK to "
                "this While node to re-check. When the condition becomes false, "
                "control goes to the OTHER outgoing link (the exit). The loop is "
                "bounded by the graph's recursion limit (Graph -> Edit Shared State).")
        hint += ("\nOutgoing links: " + (", ".join(self._targets)
                 or "none yet — draw links to the body AND the exit first"))
        if self._fields:
            hint += ".   State fields: " + ", ".join(self._fields)
        v.addWidget(_hint(hint))
        _buttons(self, v)

    def apply(self) -> str | None:
        mi = self.max_iters.text().strip()
        if mi:
            try:
                if int(mi) < 0:
                    raise ValueError
            except ValueError:
                return "Max iterations must be a whole number (or blank)."
        self.node.name = self.name.text().strip() or self.node.name
        self.node.props["condition"] = self.condition.text().strip()
        self.node.props["body"] = self.body.currentText().strip()
        self.node.props["max_iterations"] = int(mi) if mi else 0
        return None


class ForeachDialog(QDialog):
    """Map-over-list: run the BODY successor ONCE PER ITEM of a shared-state list
    field, the item runs in PARALLEL (isolated fork). Each item is passed to the
    body BOTH as its input AND (when set) written to `item variable`; the body must
    link BACK here; the OTHER outgoing link is the exit (taken once, after all items)."""

    def __init__(self, parent, node: Node):
        super().__init__(parent)
        self.setWindowTitle(f"Configure For-Each: {node.name}")
        self.node = node
        graph = _graph_of(parent)
        self._targets = ([graph.nodes[s].name for s in graph.flow_successors(node.id)]
                         if graph is not None else [])
        self._fields = [f["name"] for f in state_fields(graph)] if graph is not None else []
        v = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(node.name)
        form.addRow("Name:", self.name)
        self.over = QComboBox()
        self.over.addItems([""] + self._fields)     # a state LIST field to iterate
        if node.props.get("over") in self._fields:
            self.over.setCurrentText(node.props["over"])
        form.addRow("For each item in (list field):", self.over)
        self.body = QComboBox()
        self.body.addItems(self._targets)
        if node.props.get("body") in self._targets:
            self.body.setCurrentText(node.props["body"])
        form.addRow("Loop body (runs per item):", self.body)
        self.item_var = QComboBox()
        self.item_var.addItems([""] + self._fields)  # optional: item -> this field
        if node.props.get("item_var") in self._fields:
            self.item_var.setCurrentText(node.props["item_var"])
        form.addRow("Write current item to (optional):", self.item_var)
        self.result_field = QComboBox()
        self.result_field.addItems([""] + self._fields)  # optional: collect outputs here
        if node.props.get("result_field") in self._fields:
            self.result_field.setCurrentText(node.props["result_field"])
        form.addRow("Collect each output into (optional):", self.result_field)
        self.merge = QComboBox()
        self.merge.addItems(["concat", "first", "last", "state_only", "vote"])
        self.merge.setCurrentText(node.props.get("merge", "concat"))
        form.addRow("Merge item outputs:", self.merge)
        self.max_parallel = QLineEdit(str(node.props.get("max_parallel", 0) or ""))
        self.max_parallel.setPlaceholderText("blank/0 = all items at once")
        form.addRow("Max parallel:", self.max_parallel)
        v.addLayout(form)
        hint = ("Runs the body once per item of the chosen list field, the items in "
                "PARALLEL on isolated state forks. Each item is passed to the body as "
                "its input AND (if set) written to the item field. The body must link "
                "BACK to this For-Each node; the OTHER outgoing link is the exit, taken "
                "once after every item finishes. The list is produced upstream (an "
                "agent write or a Set-State).")
        hint += ("\nOutgoing links: " + (", ".join(self._targets)
                 or "none yet — draw links to the body AND the exit first"))
        if self._fields:
            hint += ".   State fields: " + ", ".join(self._fields)
        else:
            hint += ".   No state fields yet — add a list field via Graph -> Edit Shared State."
        v.addWidget(_hint(hint))
        _buttons(self, v)

    def apply(self) -> str | None:
        mp = self.max_parallel.text().strip()
        if mp:
            try:
                if int(mp) < 0:
                    raise ValueError
            except ValueError:
                return "Max parallel must be a whole number (or blank)."
        self.node.name = self.name.text().strip() or self.node.name
        self.node.props["over"] = self.over.currentText().strip()
        self.node.props["body"] = self.body.currentText().strip()
        self.node.props["item_var"] = self.item_var.currentText().strip()
        self.node.props["result_field"] = self.result_field.currentText().strip()
        self.node.props["merge"] = self.merge.currentText()
        self.node.props["max_parallel"] = int(mp) if mp else 0
        return None


# ── set-state node ────────────────────────────────────────────────────────────
class _AssignDialog(QDialog):
    """One set-state assignment: a state field + a literal value."""

    def __init__(self, parent, title: str, fields: list, assign: dict | None = None):
        super().__init__(parent)
        self.setWindowTitle(title)
        assign = assign or {}
        v = QVBoxLayout(self)
        form = QFormLayout()
        self.field = QComboBox()
        self.field.addItems(fields)
        if assign.get("field") in fields:
            self.field.setCurrentText(assign["field"])
        form.addRow("Field:", self.field)
        self.value = QLineEdit(assign.get("value", ""))
        form.addRow("Value:", self.value)
        v.addLayout(form)
        v.addWidget(_hint('Value is a literal (number, "string", [list], {dict}) '
                          "OR an expression starting with '=' over state fields "
                          "and `output` (the upstream text) — e.g. =attempts + 1, "
                          "=score * 2, or =output. Merged via the field's reducer."))
        _buttons(self, v)

    def result(self) -> dict:
        return {"field": self.field.currentText(), "value": self.value.text().strip()}


class SetStateDialog(QDialog):
    def __init__(self, parent, node: Node):
        super().__init__(parent)
        self.setWindowTitle(f"Configure Set State: {node.name}")
        self.node = node
        graph = _graph_of(parent)
        self._fields = ([f["name"] for f in state_fields(graph)
                         if f["name"] not in RESERVED_STATE_NAMES]
                        if graph is not None else [])
        self._assigns = [dict(a) for a in node.props.get("assignments", [])]
        v = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(node.name)
        form.addRow("Name:", self.name)
        v.addLayout(form)
        v.addWidget(QLabel("Assignments (applied in order, via each field's reducer):"))
        self.listw = QListWidget()
        self.listw.setMinimumSize(420, 150)
        self.listw.itemDoubleClicked.connect(lambda *_: self.on_edit())
        v.addWidget(self.listw)
        v.addLayout(_list_edit_row(("Add...", self.on_add),
                                   ("Edit...", self.on_edit),
                                   ("Remove", self.on_remove)))
        v.addWidget(_hint("State fields: " + (", ".join(self._fields)
                    or "none — define them via Graph → Edit Shared State first.")))
        self._reload()
        _buttons(self, v)

    def _reload(self) -> None:
        self.listw.clear()
        for a in self._assigns:
            self.listw.addItem(f"{a.get('field', '?')} = {a.get('value', '')}")

    def on_add(self) -> None:
        if not self._fields:
            QMessageBox.warning(self, "No state fields", "Define shared-state "
                                "fields first (Graph → Edit Shared State).")
            return
        dlg = _AssignDialog(self, "Add assignment", self._fields)
        if dlg.exec() == QDialog.Accepted:
            self._assigns.append(dlg.result())
            self._reload()

    def on_edit(self) -> None:
        i = self.listw.currentRow()
        if i < 0 or not self._fields:
            return
        dlg = _AssignDialog(self, "Edit assignment", self._fields, self._assigns[i])
        if dlg.exec() == QDialog.Accepted:
            self._assigns[i] = dlg.result()
            self._reload()

    def on_remove(self) -> None:
        i = self.listw.currentRow()
        if i >= 0:
            self._assigns.pop(i)
            self._reload()

    def apply(self) -> str | None:
        self.node.name = self.name.text().strip() or self.node.name
        self.node.props["assignments"] = self._assigns
        return None


class GuardrailNodeDialog(QDialog):
    """Inline guardrail gate (graph mode): scan the content flowing through and
    redact or block. Deterministic; reuses the runtime guardrail engine."""

    def __init__(self, parent, node: Node):
        super().__init__(parent)
        self.setWindowTitle(f"Configure Guardrail: {node.name}")
        self.node = node
        checks = node.props.get("checks") or {}
        self.resize(560, 560)
        outer = QVBoxLayout(self)
        _scroll = QScrollArea()
        _scroll.setWidgetResizable(True)
        _inner = QWidget()
        v = QVBoxLayout(_inner)
        _scroll.setWidget(_inner)
        outer.addWidget(_scroll, 1)
        form = QFormLayout()
        self.name = QLineEdit(node.name)
        form.addRow("Name:", self.name)
        v.addLayout(form)
        v.addWidget(QLabel("Scan the content flowing through this gate for:"))
        self.cb_secret = QCheckBox("Secrets (API keys, tokens, private keys)")
        self.cb_secret.setChecked(bool(checks.get("secret", True)))
        self.cb_pii = QCheckBox("PII (emails, card numbers)")
        self.cb_pii.setChecked(bool(checks.get("pii", False)))
        self.cb_injection = QCheckBox("Suspected prompt-injection phrases")
        self.cb_injection.setChecked(bool(checks.get("injection", False)))
        for cb in (self.cb_secret, self.cb_pii, self.cb_injection):
            v.addWidget(cb)
        h = QHBoxLayout()
        h.addWidget(QLabel("On a hit:"))
        self.on_trip = QComboBox()
        self.on_trip.addItems(["redact", "block"])
        self.on_trip.setCurrentText(node.props.get("on_trip", "redact"))
        h.addWidget(self.on_trip)
        h.addStretch(1)
        v.addLayout(h)
        v.addWidget(_hint("redact = scrub secrets/PII in place and continue; "
                          "block = stop the run. Suspected injection always blocks. "
                          "Place it between agents (e.g. before a send/publish step). "
                          "Deterministic only — not an injection shield or a sandbox."))

        # ── Extra Settings (custom patterns + length cap; blank = unchanged) ──
        g = _Collapsible("Extra Settings  (custom patterns, length cap)")
        g.span(QLabel("Custom regexes to redact/block (one per line):"))
        self.patterns = _multiline("\n".join(node.props.get("patterns") or []), 54)
        self.patterns.setPlaceholderText(
            "one regex per line, e.g.\nACME-\\d{4,}\n(?i)internal-only")
        g.span(self.patterns)
        g.span(QLabel("Literal keywords to redact/block (one per line, case-insensitive):"))
        self.keywords = _multiline("\n".join(node.props.get("keywords") or []), 44)
        self.keywords.setPlaceholderText(
            "one word/phrase per line, e.g.\nProject Bluebird\nconfidential")
        g.span(self.keywords)
        self.max_length = QLineEdit(str(node.props.get("max_length", 0) or ""))
        self.max_length.setPlaceholderText("blank/0 = no cap")
        g.row("Max content length (chars):", self.max_length)
        v.addWidget(g)
        v.addStretch(1)
        _buttons(self, outer)      # OK/Cancel stay outside the scroll, always visible

    def apply(self) -> str | None:
        import re as _re
        pats = [p.strip() for p in self.patterns.toPlainText().splitlines() if p.strip()]
        for p in pats:                       # catch typos now — the engine fails open
            try:
                _re.compile(p)
            except _re.error as e:
                return f"Invalid custom regex {p!r}: {e}"
        kws = [k.strip() for k in self.keywords.toPlainText().splitlines() if k.strip()]
        ml_raw = self.max_length.text().strip()
        if ml_raw:
            try:
                if int(ml_raw) < 0:
                    raise ValueError
            except ValueError:
                return "Max content length must be a whole number (or blank)."
        self.node.name = self.name.text().strip() or self.node.name
        self.node.props["checks"] = {"secret": self.cb_secret.isChecked(),
                                     "pii": self.cb_pii.isChecked(),
                                     "injection": self.cb_injection.isChecked()}
        self.node.props["on_trip"] = self.on_trip.currentText()
        self.node.props["patterns"] = pats
        self.node.props["keywords"] = kws
        self.node.props["max_length"] = int(ml_raw) if ml_raw else 0
        return None


class ToolConfirmDialog(QDialog):
    """Confirm (and optionally EDIT) a high-risk tool call before it runs, shown
    during a Debug Run. Full args in a resizable, scrollable editor — never
    trimmed. Returns {decision, args, remember} via outcome()."""

    def __init__(self, parent, tool_name: str, args: dict):
        super().__init__(parent)
        self.setWindowTitle("Confirm tool call")
        self.resize(680, 540)
        self._args = dict(args or {})
        self._decision = "deny"
        self._pkey, _best = None, -1
        for k, val in self._args.items():
            if isinstance(val, str) and len(val) > _best:
                self._pkey, _best = k, len(val)
        v = QVBoxLayout(self)
        lbl = QLabel(f"The agent wants to run the high-risk tool "
                     f"<b>{tool_name}</b>. Review — and edit if you like — "
                     "before allowing:")
        lbl.setWordWrap(True)
        v.addWidget(lbl)
        others = {k: val for k, val in self._args.items() if k != self._pkey}
        if others:
            v.addWidget(QLabel("Other arguments:"))
            oj = QPlainTextEdit(json.dumps(others, ensure_ascii=False, indent=2))
            oj.setReadOnly(True)
            oj.setMaximumHeight(120)
            v.addWidget(oj)
        v.addWidget(QLabel((self._pkey or "arguments (JSON)") + " — editable:"))
        init = (self._args.get(self._pkey) if self._pkey is not None
                else json.dumps(self._args, ensure_ascii=False, indent=2))
        self.text = QPlainTextEdit(init if isinstance(init, str) else str(init))
        v.addWidget(self.text, 1)
        self.remember = QCheckBox(f"Don't ask again for {tool_name} (this run)")
        v.addWidget(self.remember)
        row = QHBoxLayout()
        row.addStretch(1)
        deny = QPushButton("Deny")
        allow_edited = QPushButton("Allow edited")
        allow = QPushButton("Allow")
        deny.clicked.connect(lambda: self._finish("deny"))
        allow_edited.clicked.connect(lambda: self._finish("edit"))
        allow.clicked.connect(lambda: self._finish("allow"))
        for b in (deny, allow_edited, allow):
            row.addWidget(b)
        allow.setDefault(True)
        v.addLayout(row)
        make_dialog_resizable(self)

    def _finish(self, decision: str) -> None:
        self._decision = decision
        self.accept()

    def outcome(self) -> dict:
        remember = self.remember.isChecked()
        if self._decision == "deny":
            return {"decision": "deny", "remember": remember}
        args = dict(self._args)
        if self._decision == "edit":
            edited = self.text.toPlainText()
            if self._pkey is not None:
                args[self._pkey] = edited
            else:
                try:
                    parsed = json.loads(edited)
                    if isinstance(parsed, dict):
                        args = parsed
                except Exception:
                    pass
        return {"decision": "allow", "args": args, "remember": remember}


class ReviewDialog(QDialog):
    """Runtime HITL prompt shown during a Debug Run. GATE mode = approve / edit /
    reject; ROUTE mode (when `choices` is given) = one BUTTON PER BRANCH — the human
    picks which successor runs next (the human mirror of a router)."""

    def __init__(self, parent, prompt: str, content: str, choices=None):
        super().__init__(parent)
        self._choices = list(choices) if choices else None
        self.setWindowTitle("Choose the next step" if self._choices else "Human review")
        self.resize(680, 560)
        self._orig = content
        self.decision = (self._choices[0] if self._choices else "approve")
        v = QVBoxLayout(self)
        lbl = QLabel(prompt or "Review before continuing.")
        lbl.setWordWrap(True)
        v.addWidget(lbl)
        v.addWidget(QLabel("Content (edit to change what flows on):"))
        self.content = _multiline(content, 240)
        v.addWidget(self.content, 1)
        make_dialog_resizable(self)
        h = QHBoxLayout()
        h.addStretch(1)
        if self._choices:                       # ROUTE mode: one button per branch
            v.addWidget(QLabel("Choose the next step (you may edit the text above):"))
            for c in self._choices:
                b = QPushButton(str(c))
                b.clicked.connect(lambda _=False, name=c: self._finish(name))
                h.addWidget(b)
        else:                                   # GATE mode: approve / edit / reject
            v.addWidget(QLabel("Rejection feedback (used if you reject):"))
            self.feedback = QLineEdit()
            v.addWidget(self.feedback)
            approve = QPushButton("Approve / Edit")
            reject = QPushButton("Reject")
            approve.clicked.connect(lambda: self._finish("approve"))
            reject.clicked.connect(lambda: self._finish("reject"))
            h.addWidget(approve)
            h.addWidget(reject)
        v.addLayout(h)

    def _finish(self, decision: str) -> None:
        self.decision = decision
        self.accept()

    def result(self) -> dict:
        text = self.content.toPlainText()
        if self._choices:                       # route mode: decision = chosen branch
            return {"decision": self.decision, "content": text, "feedback": ""}
        if self.decision == "reject":
            return {"decision": "reject", "content": self._orig,
                    "feedback": self.feedback.text().strip()}
        if text != self._orig:
            return {"decision": "edit", "content": text, "feedback": ""}
        return {"decision": "approve", "content": self._orig, "feedback": ""}


def _validate_custom_gui(src: str) -> str | None:
    """Return an error message if `src` (a user-authored gui.py) can't be emitted
    as-is, else None. Blank source (= use the built-in window) is valid. Only a
    syntax error is a HARD failure; a missing `import agent` / `.run(...)` is a soft
    caveat surfaced in the status label (and by analyze() at generate time)."""
    s = (src or "").strip()
    if not s:
        return None
    try:
        compile(s.replace("@AGENT_NAME@", "Agent"), "gui.py", "exec")
    except SyntaxError as e:
        return f"Custom GUI has a syntax error: {e.msg} (line {e.lineno})."
    return None


class GUIDialog(QDialog):
    """The desktop-GUI module. By default it generates the built-in PySide6 chat
    window (gui.py). Under Extra Settings you can supply your OWN single-file gui.py
    to emit in its place — it drives the agent via `import agent` / `agent.run(...)`.
    Linking this node to the entry agent is what turns on gui.py generation."""

    def __init__(self, parent, node: Node):
        super().__init__(parent)
        self.setWindowTitle(f"Configure GUI: {node.name}")
        self.node = node
        self._src = node.props.get("custom_gui", "") or ""
        self.resize(560, 420)
        outer = QVBoxLayout(self)
        _scroll = QScrollArea()
        _scroll.setWidgetResizable(True)
        _inner = QWidget()
        v = QVBoxLayout(_inner)
        _scroll.setWidget(_inner)
        outer.addWidget(_scroll, 1)
        top = QHBoxLayout()
        top.addWidget(QLabel("Name:"))
        self.name = QLineEdit(node.name)
        top.addWidget(self.name, 1)
        v.addLayout(top)
        v.addWidget(_hint("Link this GUI module to the entry agent (Planner / "
                          "Single / Supervisor) to generate a PySide6 desktop "
                          "chat window (gui.py)."))

        extra = _Collapsible("Extra Settings")
        self.status = QLabel()
        self.status.setWordWrap(True)
        extra.span(self.status)
        row = QHBoxLayout()
        choose = QPushButton("Custom gui.py…")
        choose.clicked.connect(self._choose)
        clear = QPushButton("Use built-in")
        clear.clicked.connect(self._clear)
        row.addWidget(choose)
        row.addWidget(clear)
        row.addStretch(1)
        _rw = QWidget()
        _rw.setLayout(row)
        extra.span(_rw)
        extra.span(_hint("Optional: replace the built-in window with your own "
                         "single-file gui.py. It must import the generated agent "
                         "(`import agent`) and call `agent.run(...)`. Any @AGENT_NAME@ "
                         "in the file is substituted with the agent name at generate "
                         "time. Multi-file GUIs aren't supported here."))
        v.addWidget(extra)
        v.addStretch(1)
        self._refresh_status()
        _buttons(self, outer)      # OK/Cancel stay outside the scroll, always visible

    def _refresh_status(self) -> None:
        s = (self._src or "").strip()
        if not s:
            self.status.setText("Using the built-in gui.py.")
            return
        warn = ""
        if "import agent" not in s:
            warn = "  ⚠ doesn't `import agent`"
        elif ".run(" not in s:
            warn = "  ⚠ never calls `.run(...)`"
        self.status.setText(f"Custom gui.py loaded ({len(s)} chars).{warn}")

    def _choose(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose custom gui.py", "", "Python (*.py);;All files (*)")
        if not path:
            return
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                src = f.read()
        except OSError as e:
            QMessageBox.warning(self, "Load failed", str(e))
            return
        err = _validate_custom_gui(src)
        if err:
            QMessageBox.warning(self, "Invalid custom GUI", err)
            return
        self._src = src
        self._refresh_status()

    def _clear(self) -> None:
        self._src = ""
        self._refresh_status()

    def apply(self) -> str | None:
        self.node.name = self.name.text().strip() or self.node.name
        err = _validate_custom_gui(self._src)
        if err:
            return err
        self.node.props["custom_gui"] = self._src or ""
        return None


class EndDialog(QDialog):
    """The terminal End node. No config — when the graph flow reaches it the run
    finishes early and returns whatever output was carried in. Handy on an
    If/Else else-branch or a While exit to stop before the rest of the pipeline."""

    def __init__(self, parent, node: Node):
        super().__init__(parent)
        self.setWindowTitle(f"Configure End: {node.name}")
        self.node = node
        v = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("Name:"))
        self.name = QLineEdit(node.name)
        top.addWidget(self.name, 1)
        v.addLayout(top)
        v.addWidget(_hint("Terminal node (a sink — no outgoing links). When the "
                          "flow reaches it the run finishes early and returns the "
                          "output carried into it. Link a stage or an If/Else "
                          "else-branch / While exit into it to return early."))
        _buttons(self, v)

    def apply(self) -> str | None:
        self.node.name = self.name.text().strip() or self.node.name
        return None


class FanoutDialog(QDialog):
    """Parallel fan-out: its 2+ outgoing branches run and reconverge at a paired
    Join. Branches are independent linear agent chains (v1)."""

    def __init__(self, parent, node: Node):
        super().__init__(parent)
        self.setWindowTitle(f"Configure Fan-out: {node.name}")
        self.node = node
        v = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(node.name)
        form.addRow("Name:", self.name)
        self.max_parallel = QLineEdit(str(node.props.get("max_parallel", 0) or ""))
        self.max_parallel.setPlaceholderText("0 / blank = unbounded")
        form.addRow("Max parallel:", self.max_parallel)
        v.addLayout(form)
        v.addWidget(_hint("Runs each outgoing branch, then reconverges at the paired "
                          "Join. Link Fan-out → 2+ branch agents, each branch → the "
                          "same Join, and Join → the next stage."))
        _buttons(self, v)

    def apply(self) -> str | None:
        self.node.name = self.name.text().strip() or self.node.name
        try:
            self.node.props["max_parallel"] = max(0, int(self.max_parallel.text().strip() or 0))
        except ValueError:
            return "Max parallel must be a whole number (0 = unbounded)."
        return None


class JoinDialog(QDialog):
    """Barrier that reconverges a Fan-out's branches, then continues to one successor."""

    def __init__(self, parent, node: Node):
        super().__init__(parent)
        self.setWindowTitle(f"Configure Join: {node.name}")
        self.node = node
        v = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(node.name)
        form.addRow("Name:", self.name)
        self.merge = QComboBox()
        self.merge.addItems(["concat", "first", "last", "state_only", "vote"])
        self.merge.setCurrentText(node.props.get("merge", "concat"))
        form.addRow("Merge outputs:", self.merge)
        v.addLayout(form)
        v.addWidget(_hint("Waits for all fan-out branches, then continues to its one "
                          "successor. 'Merge outputs' combines the branch result text: "
                          "concat / first / last / state_only (drop it; branches spoke "
                          "via shared state) / vote (majority — the most common branch "
                          "output; best for a label/number, ties → first branch)."))
        _buttons(self, v)

    def apply(self) -> str | None:
        self.node.name = self.name.text().strip() or self.node.name
        self.node.props["merge"] = self.merge.currentText()
        return None


class MemoryDialog(QDialog):
    """A persistent cross-run MEMORY store linked to an agent: the agent gets
    remember(content, tags) + recall(query) tools backed by a JSON store + BM25
    retrieval (reuses the RAG ranker), so it can learn across runs (Reflexion-style)."""

    def __init__(self, parent, node: Node):
        super().__init__(parent)
        self.setWindowTitle(f"Configure Memory: {node.name}")
        self.node = node
        v = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(node.name)
        form.addRow("Name:", self.name)
        self.top_k = QLineEdit(str(node.props.get("top_k", 5) or 5))
        self.top_k.setToolTip("How many memories recall() returns by default.")
        form.addRow("Recall top-K:", self.top_k)
        v.addLayout(form)
        v.addWidget(QLabel("Description (when should the agent use this memory?):"))
        self.description = _multiline(node.props.get("description", ""), 80)
        v.addWidget(self.description)
        v.addWidget(_hint("Link it to an agent (memory → agent). The agent gains "
                          "remember(content, tags) and recall(query) tools backed by a "
                          "persistent store (memory_store.json) + BM25 retrieval — so it "
                          "recalls past lessons before acting and remembers new ones "
                          "after, learning ACROSS runs. Prompt the agent to use them."))
        _buttons(self, v)

    def apply(self) -> str | None:
        to = self.top_k.text().strip()
        if to:
            try:
                if int(to) < 1:
                    raise ValueError
            except ValueError:
                return "Recall top-K must be a positive whole number (or blank)."
        self.node.name = self.name.text().strip() or self.node.name
        self.node.props["description"] = self.description.toPlainText().strip()
        self.node.props["top_k"] = int(to) if to else 5
        return None


class ScheduleDialog(QDialog):
    """A Schedule module: linking it to the entry agent emits scheduler.py, which runs
    the agent on an interval (an ambient agent — no user prompt)."""

    def __init__(self, parent, node: Node):
        super().__init__(parent)
        self.setWindowTitle(f"Configure Schedule: {node.name}")
        self.node = node
        v = QVBoxLayout(self)
        form = QFormLayout()
        self._form = form
        self.name = QLineEdit(node.name)
        form.addRow("Name:", self.name)
        # Strategy — picks ONE timing mode; the other modes' fields grey out.
        self._MODES = [("interval", "Periodic — every N seconds"),
                       ("daily", "Daily — at a local time"),
                       ("once", "Once — at an exact timestamp")]
        self.mode = QComboBox()
        self.mode.addItems([lbl for _, lbl in self._MODES])
        _cur = node.props.get("mode", "interval")
        self.mode.setCurrentIndex(
            next((i for i, (v, _) in enumerate(self._MODES) if v == _cur), 0))
        self.mode.currentIndexChanged.connect(lambda *_: self._sync_mode())
        form.addRow("Strategy:", self.mode)
        self.every = QLineEdit(str(node.props.get("every_seconds", 3600) or 3600))
        self.every.setToolTip("Run the agent every N seconds (60 = minute, 3600 = hour, "
                              "86400 = day).")
        form.addRow("Every (seconds):", self.every)
        self.offset = QLineEdit(str(node.props.get("offset_seconds", 0) or 0))
        self.offset.setToolTip("Initial delay before the FIRST tick — stagger multiple "
                               "schedules so they don't all fire at once (0 = no delay).")
        form.addRow("Start offset (seconds):", self.offset)
        self.max_runs = QLineEdit(str(node.props.get("max_runs", 0) or 0))
        self.max_runs.setToolTip("Stop after this many runs (0 = run forever).")
        form.addRow("Max runs (0 = ∞):", self.max_runs)
        self.session_id = QLineEdit(node.props.get("session_id", ""))
        self.session_id.setPlaceholderText("blank = one rolling conversation across ticks")
        form.addRow("Session id:", self.session_id)
        # ── wall-clock timing (optional; overrides the interval) ─────────────
        self.at = QLineEdit(node.props.get("at", ""))
        self.at.setPlaceholderText("e.g. 09:00 — daily at this local time (blank = use interval)")
        self.at.setToolTip("Run DAILY at this local time (HH:MM or HH:MM:SS, 24-hour). "
                           "Takes precedence over the interval.")
        form.addRow("Run daily at:", self.at)
        self.start_at = QLineEdit(node.props.get("start_at", ""))
        self.start_at.setPlaceholderText("e.g. 2026-07-08 14:30 — exact first-run time")
        self.start_at.setToolTip("First run at this exact LOCAL date & time "
                                 "(YYYY-MM-DD HH:MM, optionally :SS), then every interval.")
        form.addRow("First run at:", self.start_at)
        v.addLayout(form)
        self.run_at_start = QCheckBox("Run once immediately at start")
        self.run_at_start.setChecked(bool(node.props.get("run_at_start", True)))
        v.addWidget(self.run_at_start)
        v.addWidget(QLabel("Task to run each tick:"))
        self.task = _multiline(node.props.get("initial_task", ""), 100)
        v.addWidget(self.task)
        v.addWidget(_hint("Link it to the entry agent (schedule → agent). Generation emits "
                          "scheduler.py — run `python scheduler.py` for an ambient agent that "
                          "wakes on the interval and runs the task (no user prompt). Pair it "
                          "with a Memory node so it learns across ticks. Ctrl+C stops it "
                          "cleanly. Settings live in config.json (edit without regenerating)."))
        self._sync_mode()      # grey the fields the chosen strategy doesn't use
        _buttons(self, v)

    def _mode_value(self) -> str:
        return self._MODES[self.mode.currentIndex()][0]

    def _set_enabled(self, w, on: bool) -> None:
        w.setEnabled(on)
        lbl = self._form.labelForField(w)
        if lbl is not None:
            lbl.setEnabled(on)

    def _sync_mode(self) -> None:
        m = self._mode_value()
        interval, daily, once = (m == "interval"), (m == "daily"), (m == "once")
        self._set_enabled(self.every, interval)
        self._set_enabled(self.offset, interval)
        self.run_at_start.setEnabled(interval)
        self._set_enabled(self.at, daily)
        self._set_enabled(self.start_at, once)
        self._set_enabled(self.max_runs, interval or daily)   # 'once' fires exactly one time

    def apply(self) -> str | None:
        mode = self._mode_value()
        # validate ONLY the active strategy's field
        if mode == "interval":
            try:
                if int(self.every.text().strip()) < 1:
                    raise ValueError
            except ValueError:
                return "Every (seconds) must be a whole number ≥ 1."
        off = self.offset.text().strip() or "0"
        try:
            if int(off) < 0:
                raise ValueError
        except ValueError:
            return "Start offset must be a whole number of seconds ≥ 0."
        mr = self.max_runs.text().strip() or "0"
        try:
            if int(mr) < 0:
                raise ValueError
        except ValueError:
            return "Max runs must be a whole number ≥ 0 (0 = forever)."
        import datetime as _dt
        _at = self.at.text().strip()
        if mode == "daily" and not _at:
            return "Daily strategy needs a time — set 'Run daily at' (e.g. 09:00)."
        if _at:
            _p = _at.split(":")
            _ok = len(_p) in (2, 3) and all(x.isdigit() for x in _p)
            if _ok:
                _v = [int(x) for x in _p]
                _ok = (0 <= _v[0] <= 23 and 0 <= _v[1] <= 59
                       and (len(_v) < 3 or 0 <= _v[2] <= 59))
            if not _ok:
                return "'Run daily at' must be HH:MM or HH:MM:SS (24-hour), e.g. 09:00."
        _sa = self.start_at.text().strip()
        if mode == "once" and not _sa:
            return "Once strategy needs a time — set 'First run at' (e.g. 2026-07-08 14:30)."
        if _sa and not any(self._try_dt(_dt, _sa, f) for f in
                           ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                            "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M")):
            return "'First run at' must be YYYY-MM-DD HH:MM (optionally :SS), e.g. 2026-07-08 14:30."
        self.node.name = self.name.text().strip() or self.node.name
        self.node.props["mode"] = mode
        self.node.props["every_seconds"] = int(self.every.text().strip() or 3600)
        self.node.props["offset_seconds"] = int(off)
        self.node.props["max_runs"] = int(mr)
        self.node.props["session_id"] = self.session_id.text().strip()
        self.node.props["run_at_start"] = self.run_at_start.isChecked()
        self.node.props["initial_task"] = self.task.toPlainText().strip()
        self.node.props["at"] = _at
        self.node.props["start_at"] = _sa
        return None

    @staticmethod
    def _try_dt(_dt, s, fmt) -> bool:
        try:
            _dt.datetime.strptime(s, fmt)
            return True
        except ValueError:
            return False


class SubgraphDialog(QDialog):
    """Embed another graph as a reusable component. 'Load .mta…' reads a child graph
    and stores its full definition INLINE on this node (props['graph_json']), so the
    parent stays self-contained; at generation time the child is flattened in."""

    def __init__(self, parent, node: Node):
        super().__init__(parent)
        self.setWindowTitle(f"Configure Subgraph: {node.name}")
        self.node = node
        self.resize(520, 240)
        self._child_json = dict(node.props.get("graph_json") or {})
        v = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(node.name)
        form.addRow("Name:", self.name)
        self.gname = QLineEdit(node.props.get("graph_name", ""))
        self.gname.setPlaceholderText("label for the embedded graph")
        form.addRow("Graph name:", self.gname)
        v.addLayout(form)
        row = QHBoxLayout()
        load = QPushButton("Load .mta…")
        load.clicked.connect(self._load)
        row.addWidget(load); row.addStretch(1)
        v.addLayout(row)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        v.addWidget(self.summary)
        v.addWidget(_hint("The embedded graph runs as one step: this node's incoming "
                          "edge feeds the child's entry, and the child's End (or last "
                          "stage) continues to this node's successor. The child's tools/"
                          "prompts/LLMs come along; its shared-state fields merge in."))
        self._refresh()
        _buttons(self, v)

    def _refresh(self):
        cj = self._child_json
        if cj and cj.get("nodes"):
            kinds = [n.get("kind") for n in cj["nodes"]]
            agents = sum(1 for k in kinds if k in AGENT_KINDS)
            self.summary.setText("✓ Embedded: %d node(s), %d agent stage(s)."
                                 % (len(kinds), agents))
        else:
            self.summary.setText("⚠ No graph embedded yet — click 'Load .mta…'.")

    def _load(self):
        import os
        try:
            from app_config import GRAPHS_DIR as _gd
        except Exception:  # noqa: BLE001
            _gd = ""
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a graph to embed", _gd, "MetaAgent bundle (*.mta)")
        if not path:
            return
        try:
            child, _info = load_mta(path, codegen.TOOLS_DIR)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Load failed", str(e)); return
        self._child_json = child.to_dict()
        if not self.gname.text().strip():
            self.gname.setText(os.path.splitext(os.path.basename(path))[0])
        self._refresh()

    def apply(self) -> str | None:
        if not (self._child_json and self._child_json.get("nodes")):
            return "Subgraph has no embedded graph — click 'Load .mta…' first."
        self.node.name = self.name.text().strip() or self.node.name
        self.node.props["graph_name"] = self.gname.text().strip()
        self.node.props["graph_json"] = self._child_json
        return None


_DIALOGS = {
    "agent": AgentDialog, "workerpool": WorkerPoolDialog, "router": RouterDialog,
    "hitl": HITLDialog, "eval": EvalDialog, "llm": LLMDialog, "tool": ToolDialog,
    "skill": SkillsNodeDialog, "prompt": PromptDialog, "rag": RagDialog,
    "memory": MemoryDialog,
    "webserver": WebServerDialog, "mcp": McpDialog, "gui": GUIDialog,
    "schedule": ScheduleDialog,
    "condition": ConditionDialog, "while": WhileDialog, "foreach": ForeachDialog,
    "setstate": SetStateDialog,
    "guardrail": GuardrailNodeDialog, "end": EndDialog,
    "fanout": FanoutDialog, "join": JoinDialog, "subgraph": SubgraphDialog,
}

# Fail fast at import (explicit raise, NOT assert — survives `python -O`): a new
# NODE_KINDS entry that forgets its dialog would otherwise silently degrade to the
# generic TextDialog fallback below instead of erroring.
if set(_DIALOGS) != set(NODE_KINDS):
    raise RuntimeError(
        "_DIALOGS must cover every NODE_KINDS kind exactly; "
        f"missing={set(NODE_KINDS) - set(_DIALOGS)} "
        f"extra={set(_DIALOGS) - set(NODE_KINDS)}")


def make_dialog_resizable(dlg: QDialog) -> None:
    """Let the user resize / maximize a config window. Qt gives dialogs a minimal
    title bar (close only — no maximize/minimize) and no resize grip, so the window
    feels fixed; add the maximize+minimize title-bar buttons (the maximize button
    toggles maximize↔restore, i.e. reset to the normal size) and a corner grip.
    Flags are set before exec()/show, so there's no window-recreate flicker."""
    dlg.setSizeGripEnabled(True)                     # drag-resize grip (bottom-right)
    # Capture the size the dialog asked for in __init__ (via self.resize). On Windows
    # setWindowFlags() recreates the native window and DISCARDS that size, reverting
    # to a content-driven size where word-wrapped hints wrap tall — which produces the
    # "QWindowsWindow::setGeometry: Unable to set geometry ...ClassWindow" clamp
    # warning on the field-heavy (scroll-wrapped) dialogs. Re-assert the intended
    # size AFTER the flag change so those dialogs open wide enough that no post-show
    # grow (hence no clamp) is needed. Scoped to scroll-wrapped dialogs so plain,
    # naturally-sized dialogs keep opening at their own sizeHint.
    _wide = bool(dlg.findChildren(QScrollArea))
    _want = dlg.size()
    dlg.setWindowFlags(dlg.windowFlags()
                       | Qt.WindowMaximizeButtonHint | Qt.WindowMinimizeButtonHint)
    if _wide:
        dlg.resize(_want.expandedTo(dlg.sizeHint()))


# ── link (edge) contracts ─────────────────────────────────────────────────────
class _ContractFieldDialog(QDialog):
    """Add / edit one data-contract field: name, type, description (no reducer or
    default — a contract describes a handoff, it isn't a stored state field)."""

    def __init__(self, parent, title: str, field: dict | None = None,
                 taken: set | None = None, type_defs: dict | None = None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self._taken = taken or set()
        field = field or {}
        v = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(field.get("name", ""))
        form.addRow("Field name:", self.name)
        self.type = QComboBox()
        # native scalars + any custom type + list[CustomType] (shared type_defs)
        opts = list(STATE_TYPES)
        for tn in sorted(type_defs or {}):
            opts += [tn, f"list[{tn}]"]
        cur = field.get("type", "str")
        if cur not in opts:
            opts.append(cur)
        self.type.addItems(opts)
        self.type.setCurrentText(cur)
        form.addRow("Type:", self.type)
        self.description = QPlainTextEdit(field.get("description", ""))
        self.description.setMinimumHeight(72)
        self.description.setMinimumWidth(320)
        self.description.setTabChangesFocus(True)
        form.addRow("Description:", self.description)
        v.addLayout(form)
        v.addWidget(_hint("One field of the handoff — what the upstream agent "
                          "produces and the downstream agent consumes. Name + type "
                          "+ description are written into both agents' prompts. "
                          "Custom types (Graph → Define Types) may be used here too."))
        _buttons(self, v)

    def result(self) -> dict | None:
        name = self.name.text().strip()
        if not name.isidentifier():
            QMessageBox.warning(self, "Invalid name",
                                "Field name must be a valid identifier: letters, "
                                "digits and underscores, not starting with a digit.")
            return None
        if name in self._taken:
            QMessageBox.warning(self, "Duplicate name",
                                f"A field named '{name}' already exists.")
            return None
        return {"name": name, "type": self.type.currentText(),
                "description": self.description.toPlainText().strip()}


class EdgeLlmPriorityDialog(QDialog):
    """Fallback priority for an llm→agent link (1 = primary). Priority is stored
    on the LINK, not the LLM node, so the SAME LLM can be primary for one agent
    and a fallback for another. The runtime tries this agent's LLMs in this order,
    failing over on error."""

    def __init__(self, parent, edge, graph):
        super().__init__(parent)
        self.edge = edge
        self.graph = graph
        self._llm = graph.nodes[edge.src].name
        self._agent = graph.nodes[edge.dst].name
        self.setWindowTitle(f"Fallback priority: {self._llm} → {self._agent}")
        sibs = graph.llm_edges_of(edge.dst)          # priority-ordered llm links
        n = max(1, len(sibs))
        try:
            cur = sibs.index(edge) + 1
        except ValueError:
            cur = int(edge.props.get("priority") or 1)
        self._start = cur
        v = QVBoxLayout(self)
        v.addWidget(QLabel(f"'{self._agent}' has {n} LLM(s). Lower number = tried "
                           "first; the rest are fallbacks on error."))
        form = QFormLayout()
        self.spin = QSpinBox()
        self.spin.setRange(1, n)
        self.spin.setValue(cur)
        form.addRow("Priority (1 = primary):", self.spin)
        v.addLayout(form)
        order = "  →  ".join(
            (f"{i}. {graph.nodes[e.src].name}" + ("  ◀ this" if e is edge else ""))
            for i, e in enumerate(sibs, 1))
        v.addWidget(_hint("Current fallback order for '" + self._agent + "':\n"
                          + (order or "(only this LLM)")))
        _buttons(self, v)

    def apply(self) -> str | None:
        val = self.spin.value()
        if val != self._start:
            # Write the requested slot minus a half so THIS link wins the tie at
            # that position; the next rebuild's renumber_llm_fallbacks re-packs
            # every link back to clean contiguous 1..N ints.
            self.edge.props["priority"] = val - 0.5
        return None


class EdgeContractDialog(QDialog):
    """Data-handoff contract on an agent→agent link: the fields the upstream
    produces = the downstream consumes. Injected into BOTH agents' prompts at
    generation (organised like shared state, but stored per-edge)."""

    def __init__(self, parent, edge, graph):
        super().__init__(parent)
        self.edge = edge
        self.graph = graph
        self._src = graph.nodes[edge.src].name
        self._dst = graph.nodes[edge.dst].name
        self.setWindowTitle(f"Link contract: {self._src} → {self._dst}")
        self._fields = list(contract_fields(edge, getattr(graph, "type_defs", None)))
        v = QVBoxLayout(self)
        v.addWidget(QLabel(f"Data handoff  {self._src}  →  {self._dst}"))
        self.listw = QListWidget()
        self.listw.setMinimumSize(460, 160)
        self.listw.itemDoubleClicked.connect(lambda *_: self.on_edit())
        v.addWidget(self.listw)
        v.addLayout(_list_edit_row(("Add...", self.on_add),
                                   ("Edit...", self.on_edit),
                                   ("Remove", self.on_remove)))
        v.addWidget(_hint(
            f"Declare the fields {self._src} outputs and {self._dst} receives. "
            f"Written to both system prompts: {self._src} is told to PRODUCE them, "
            f"{self._dst} to EXPECT them."))
        ef = QFormLayout()
        self.enforce = QCheckBox(
            f"Validate & retry — check {self._src}'s output as JSON against this "
            "contract; re-run it if it doesn't match, else stop the run")
        self.enforce.setChecked(bool(edge.props.get("contract_enforce", False)))
        ef.addRow(self.enforce)
        self.max_retries = QLineEdit(str(edge.props.get("contract_max_retries", 2)))
        self.max_retries.setPlaceholderText("retries before the run stops (default 2)")
        ef.addRow("  ↳ max retries:", self.max_retries)
        v.addLayout(ef)
        v.addWidget(_hint(
            "Off (default): the contract shapes the prompts only. On: "
            f"{self._src} must reply with a JSON object of exactly these fields — "
            "the runtime validates it and re-runs on a mismatch."))
        self._reload()
        _buttons(self, v)

    def _names(self, exclude: int = -1) -> set:
        return {f["name"] for i, f in enumerate(self._fields) if i != exclude}

    def _reload(self) -> None:
        self.listw.clear()
        for f in self._fields:
            row = f"{f['name']} : {f['type']}"
            d = (f.get("description") or "").strip()
            if d:
                row += f"   — {d}"
            self.listw.addItem(row)

    def on_add(self) -> None:
        dlg = _ContractFieldDialog(self, "Add contract field", taken=self._names(),
                                   type_defs=getattr(self.graph, "type_defs", None))
        if dlg.exec() == QDialog.Accepted:
            r = dlg.result()
            if r:
                self._fields.append(r)
                self._reload()

    def on_edit(self) -> None:
        i = self.listw.currentRow()
        if i < 0:
            return
        dlg = _ContractFieldDialog(self, "Edit contract field", self._fields[i],
                                   taken=self._names(exclude=i),
                                   type_defs=getattr(self.graph, "type_defs", None))
        if dlg.exec() == QDialog.Accepted:
            r = dlg.result()
            if r:
                self._fields[i] = r
                self._reload()

    def on_remove(self) -> None:
        i = self.listw.currentRow()
        if i >= 0:
            self._fields.pop(i)
            self._reload()

    def apply(self) -> str | None:
        if self._fields:
            self.edge.props["contract"] = self._fields
        else:
            self.edge.props.pop("contract", None)
        try:
            retries = max(0, int(self.max_retries.text().strip() or 2))
        except ValueError:
            return "Max retries must be an integer."
        # only enforce when there are fields to validate against
        self.edge.props["contract_enforce"] = bool(
            self.enforce.isChecked() and self._fields)
        self.edge.props["contract_max_retries"] = retries
        return None


class EdgeConditionBranchDialog(QDialog):
    """Set which branch THIS If/Else link is: the predicate that routes to it
    (empty = else). Writes back to the condition node's `branches` list — the
    single source of truth the runtime routes on."""

    def __init__(self, parent, edge, graph):
        super().__init__(parent)
        self.edge = edge
        self.cond = graph.nodes[edge.src]
        self.dst_name = graph.nodes[edge.dst].name
        self.setWindowTitle(f"Branch: {self.cond.name} → {self.dst_name}")
        cur = ""
        for b in (self.cond.props.get("branches") or []):
            if (b.get("to") or "") == self.dst_name:
                cur = b.get("expr") or ""
                break
        fields = [f["name"] for f in state_fields(graph)]
        v = QVBoxLayout(self)
        form = QFormLayout()
        self.expr = QLineEdit(cur)
        self.expr.setPlaceholderText("e.g. score < 0.5   (empty = else / fallback)")
        form.addRow(f"Route to {self.dst_name} when:", self.expr)
        v.addLayout(form)
        hint = ("Predicate over shared state; leave EMPTY for the else / fallback. "
                "Branches are tried top-to-bottom — reorder them in the If/Else "
                "node's own dialog; first true wins.")
        if fields:
            hint += "   State fields: " + ", ".join(fields)
        v.addWidget(_hint(hint))
        _buttons(self, v)

    def apply(self) -> str | None:
        expr = self.expr.text().strip()
        branches = [dict(b) for b in (self.cond.props.get("branches") or [])]
        for b in branches:
            if (b.get("to") or "") == self.dst_name:
                b["expr"] = expr
                break
        else:
            branches.append({"to": self.dst_name, "expr": expr})
        self.cond.props["branches"] = branches
        return None


class EdgeWhileBranchDialog(QDialog):
    """Mark THIS While link as the loop body or the exit. Writes the While node's
    `body` (the exit is derived as the other successor)."""

    def __init__(self, parent, edge, graph):
        super().__init__(parent)
        self.edge = edge
        self.wh = graph.nodes[edge.src]
        self.dst_name = graph.nodes[edge.dst].name
        # All links the user can actually draw out of the While — read the raw
        # edges (a body/exit may be a hitl gate, which flow_successors() omits).
        self._succ = [graph.nodes[e.dst].name for e in graph.edges
                      if e.src == self.wh.id]
        self.setWindowTitle(f"While link: {self.wh.name} → {self.dst_name}")
        v = QVBoxLayout(self)
        form = QFormLayout()
        self.role = QComboBox()
        self.role.addItems(["loop body", "exit"])
        self.role.setCurrentText(
            "loop body" if self.wh.props.get("body") == self.dst_name else "exit")
        form.addRow(f"This link ({self.dst_name}) is the:", self.role)
        v.addLayout(form)
        v.addWidget(_hint("The loop BODY runs while the condition holds and must "
                          "link back to the While node; the EXIT is taken when the "
                          "condition turns false. Set the condition in the While "
                          "node's own dialog."))
        _buttons(self, v)

    def apply(self) -> str | None:
        if self.role.currentText() == "loop body":
            self.wh.props["body"] = self.dst_name
            return None
        # "exit": this link must NOT be the body.
        if self.wh.props.get("body") == self.dst_name:
            # Demoting the CURRENT body to exit — refuse rather than silently
            # promoting some other link to body (which would invert the loop).
            return ("This link is currently the loop body. To make it the exit, "
                    "first mark a different link as the loop body (open that link, "
                    "or set the body in the While node's own dialog).")
        # dst isn't the body. If no body is set yet, adopt another successor as the
        # body so this one is a genuine exit; otherwise it's already the exit.
        if not self.wh.props.get("body"):
            others = [n for n in self._succ if n != self.dst_name]
            if others:
                self.wh.props["body"] = others[0]
            else:
                return ("A While needs a loop-body link (that loops back) AND an "
                        "exit link. Draw the body link first, then mark this one "
                        "as the exit.")
        return None


def open_edge_config_dialog(parent, edge, graph) -> str | None:
    """Configure a link. agent→agent: a data contract. condition→X: the branch
    predicate. while→X: loop-body / exit. Other links (resources) have no
    contract — an info box explains. Returns an error message or None."""
    src = graph.nodes.get(edge.src)
    dst = graph.nodes.get(edge.dst)
    if src is None or dst is None:
        return None
    # A data contract needs a data-PRODUCING source (a plain agent / worker pool).
    # A router only picks a branch and forwards the same text, so it can't honour a
    # contract — treat it like the informational case.
    if src.kind == "llm" and dst.kind in AGENT_KINDS:
        dlg = EdgeLlmPriorityDialog(parent, edge, graph)
    elif src.kind in ("agent", "workerpool") and dst.kind in AGENT_KINDS:
        dlg = EdgeContractDialog(parent, edge, graph)
    elif src.kind in ("condition", "while"):
        # Branches are keyed by destination NAME; refuse if that's ambiguous.
        sibs = [graph.nodes[e.dst].name for e in graph.edges if e.src == src.id]
        if sibs.count(dst.name) > 1:
            QMessageBox.warning(
                parent, "Duplicate stage name",
                f"Two links from '{src.name}' lead to stages both named "
                f"'{dst.name}'. Branches are keyed by name, so this is ambiguous — "
                "rename one of them first, then configure the branch.")
            return None
        dlg = (EdgeConditionBranchDialog(parent, edge, graph)
               if src.kind == "condition"
               else EdgeWhileBranchDialog(parent, edge, graph))
    elif src.kind == "router":
        QMessageBox.information(
            parent, "Link",
            f"A router chooses a branch, it doesn't reshape data — the "
            f"'{src.name}' → '{dst.name}' link carries no contract. Put a data "
            "contract on a plain agent→agent link instead.")
        return None
    else:
        QMessageBox.information(
            parent, "Link",
            f"A {src.kind} → {dst.kind} link has no contract to configure — "
            f"it feeds '{dst.name}' as a resource. Data contracts apply to "
            "agent→agent links; branch labels to If/Else and While links.")
        return None
    make_dialog_resizable(dlg)
    err = None
    if dlg.exec() == QDialog.Accepted:
        err = dlg.apply()
    return err


def open_config_dialog(parent, node: Node) -> str | None:
    """Open the right dialog for the node; returns an error message or None."""
    cls = _DIALOGS.get(node.kind)
    dlg = cls(parent, node) if cls else TextDialog(parent, node, node.kind)
    make_dialog_resizable(dlg)                       # maximize / restore / drag-resize
    err = None
    if dlg.exec() == QDialog.Accepted:
        err = dlg.apply()
    return err
