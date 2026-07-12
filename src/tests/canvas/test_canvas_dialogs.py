"""Node-config dialog apply() round-trips."""

from __future__ import annotations

import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402,F401
from PySide6.QtGui import QAction  # noqa: E402,F401
from PySide6.QtWidgets import QApplication  # noqa: E402,F401

from canvas_qt import dialogs as D  # noqa: E402,F401
from canvas_qt.designer import CanvasWindow, EdgeItem, NodeItem  # noqa: E402,F401
from graph_model import Graph  # noqa: E402,F401
# --- shared cross-section imports (from the former monolith) ---
from PySide6.QtCore import QEvent, QPointF, QRectF  # noqa: E402,F401
from PySide6.QtGui import QMouseEvent, QPainterPath, QKeyEvent  # noqa: E402,F401
from PySide6.QtWidgets import QGraphicsView  # noqa: E402,F401
from canvas_qt.dialogs import AgentDialog, builtin_tools_for  # noqa: E402,F401
from canvas_qt.designer import NODE_W, NODE_H  # noqa: E402,F401
import canvas_qt.designer as _dz  # noqa: E402,F401



# ── dialogs apply() round-trips ──────────────────────────────────────────────
def _node(kind):
    g = Graph()
    return g.new_node(kind, 10, 10)


def test_llm_dialog_apply(qapp):
    node = _node("llm")
    dlg = D.LLMDialog(None, node)
    dlg.provider.setCurrentText("openai")
    dlg._on_provider()
    dlg.model.setText("gpt-4o")
    dlg.temperature.setText("0.7")
    assert dlg.apply() is None
    assert node.props["model"] == "gpt-4o"
    assert node.props["temperature"] == "0.7"
    assert node.props["provider"] == "openai"


def test_llm_dialog_offers_nvidia_provider(qapp):
    """build.nvidia.com is a first-class provider: selectable and auto-fills the NIM
    base URL (OpenAI-compatible, so the runtime needs no new branch)."""
    from canvas_qt.dialogs import PROVIDERS, PROVIDER_DEFAULTS
    assert "nvidia" in PROVIDERS
    assert PROVIDER_DEFAULTS["nvidia"][1] == "https://integrate.api.nvidia.com/v1"
    node = _node("llm")
    dlg = D.LLMDialog(None, node)
    dlg.provider.setCurrentText("nvidia")
    dlg._on_provider()
    assert dlg.base_url.text() == "https://integrate.api.nvidia.com/v1"
    assert dlg.model.text() == "meta/llama-3.1-70b-instruct"
    assert dlg.apply() is None
    assert node.props["provider"] == "nvidia"
    assert node.props["base_url"] == "https://integrate.api.nvidia.com/v1"


def test_debug_key_dialog_copies_coding_agent_key(qapp, monkeypatch):
    """The Debug Run key prompt can copy the coding-agent's API key in one click."""
    import app_config
    monkeypatch.setattr(app_config, "load_config", lambda: {"api_key": "sk-coding-123"})
    from canvas_qt.dialogs import DebugKeyDialog
    dlg = DebugKeyDialog()
    assert dlg.key() == ""
    dlg._copy_coding_key()
    assert dlg.key() == "sk-coding-123"


def test_llm_dialog_rejects_bad_temperature(qapp):
    node = _node("llm")
    dlg = D.LLMDialog(None, node)
    dlg.temperature.setText("hot")
    assert dlg.apply() is not None


def test_llm_dialog_context_capacity_roundtrip(qapp):
    node = _node("llm")
    dlg = D.LLMDialog(None, node)
    dlg.context_capacity.setText("128000")
    assert dlg.apply() is None
    assert node.props["context_capacity"] == 128000
    # blank = unset (0 = no context control)
    dlg2 = D.LLMDialog(None, _node("llm"))
    dlg2.context_capacity.setText("")
    assert dlg2.apply() is None and dlg2.node.props["context_capacity"] == 0
    # non-numeric is rejected
    dlg3 = D.LLMDialog(None, _node("llm"))
    dlg3.context_capacity.setText("lots")
    assert dlg3.apply() is not None


def test_agent_dialog_dropped_max_input_tokens(qapp):
    # max_input_tokens was removed in favor of the LLM node's context_capacity
    dlg = D.AgentDialog(None, _node("agent"))
    assert "max_input_tokens" not in dlg.budgets
    assert "max_input_tokens" not in _node("agent").props


def test_llm_dialog_json_schema_requires_schema(qapp):
    node = _node("llm")
    dlg = D.LLMDialog(None, node)
    dlg.response_format.setCurrentText("json_schema")
    assert dlg.apply() is not None
    dlg.response_schema.setPlainText('{"type": "object"}')
    assert dlg.apply() is None


def test_llm_extra_settings_roundtrip(qapp):
    """The new LLM 'Extra Settings' fields round-trip to props and fold into the
    per-call `extra` API params (via graph_codegen._parse_llm_opts)."""
    import graph_codegen as gc
    node = _node("llm")
    dlg = D.LLMDialog(None, node)
    dlg.seed.setText("7")
    dlg.top_k.setText("40")
    dlg.presence_penalty.setText("0.5")
    dlg.frequency_penalty.setText("0.2")
    dlg.reasoning_effort.setCurrentText("high")
    dlg.stop.setPlainText("END\n###")
    assert dlg.apply() is None
    assert node.props["seed"] == "7" and node.props["reasoning_effort"] == "high"
    ex = gc._parse_llm_opts(node.props)["extra"]
    assert ex["seed"] == 7 and ex["top_k"] == 40
    assert ex["presence_penalty"] == 0.5 and ex["frequency_penalty"] == 0.2
    assert ex["reasoning_effort"] == "high" and ex["stop"] == ["END", "###"]


def test_llm_extra_settings_blank_is_byte_identical(qapp):
    """A fresh LLM node with no Extra Settings emits NO `extra` key — so graphs
    that don't use them keep byte-identical configs."""
    import graph_codegen as gc
    assert "extra" not in gc._parse_llm_opts(_node("llm").props)


def test_llm_extra_raw_json_overrides_fields(qapp):
    """The raw `extra` JSON escape hatch is merged last and overrides a first-class
    field (and can add arbitrary params)."""
    import graph_codegen as gc
    node = _node("llm")
    node.props["seed"] = "1"
    node.props["extra"] = '{"seed": 99, "logprobs": true}'
    ex = gc._parse_llm_opts(node.props)["extra"]
    assert ex["seed"] == 99 and ex["logprobs"] is True


def test_llm_extra_settings_reject_bad_number(qapp):
    node = _node("llm")
    dlg = D.LLMDialog(None, node)
    dlg.seed.setText("notanint")
    assert dlg.apply() is not None


def test_llm_has_extra_settings_group(qapp):
    from canvas_qt.dialogs import _Collapsible
    dlg = D.LLMDialog(None, _node("llm"))
    titles = [s._title for s in dlg.findChildren(_Collapsible)]
    assert any(t.startswith("Extra Settings") for t in titles), titles


def test_agent_mode_label_roundtrip(qapp):
    """The Agent 'Extra Settings' group exposes the previously-hidden mode_label."""
    from canvas_qt.dialogs import _Collapsible
    node = _node("agent")
    dlg = D.AgentDialog(None, node)
    assert any(s._title == "Extra Settings" for s in dlg.findChildren(_Collapsible))
    dlg.mode_label.setText("fast")
    assert dlg.apply() is None
    assert node.props["mode_label"] == "fast"


# ── Phase 2 Extra Settings ───────────────────────────────────────────────────
def test_llm_max_retries_and_tool_choice(qapp):
    import graph_codegen as gc
    node = _node("llm")
    dlg = D.LLMDialog(None, node)
    dlg.max_retries.setText("0")                    # 0 is meaningful (no retry)
    dlg.tool_choice.setCurrentText("any")
    assert dlg.apply() is None
    o = gc._parse_llm_opts(node.props)
    assert o["max_retries"] == 0 and o["tool_choice"] == "any"
    # 'specific' needs a tool name
    n2 = _node("llm"); d2 = D.LLMDialog(None, n2)
    d2.tool_choice.setCurrentText("specific")
    assert d2.apply() is not None
    d2.tool_choice_name.setText("search")
    assert d2.apply() is None
    o2 = gc._parse_llm_opts(n2.props)
    assert o2["tool_choice"] == "specific" and o2["tool_choice_name"] == "search"
    # blank = byte-identical (no keys emitted)
    o3 = gc._parse_llm_opts(_node("llm").props)
    assert "max_retries" not in o3 and "tool_choice" not in o3


def test_llm_prices_roundtrip(qapp):
    import graph_codegen as gc
    node = _node("llm")
    dlg = D.LLMDialog(None, node)
    dlg.price_in.setText("0.3"); dlg.price_out.setText("1.2")
    assert dlg.apply() is None
    o = gc._parse_llm_opts(node.props)
    assert o["price_in_per_1m"] == 0.3 and o["price_out_per_1m"] == 1.2
    assert "price_in_per_1m" not in gc._parse_llm_opts(_node("llm").props)


def test_agent_extra_rpm_retry_budget(qapp):
    node = _node("agent")
    dlg = D.AgentDialog(None, node)
    dlg.max_rpm.setText("30"); dlg.stage_retries.setText("2")
    dlg.max_budget_usd.setText("0.5")
    assert dlg.apply() is None
    assert node.props["max_rpm"] == 30 and node.props["stage_retries"] == 2
    assert node.props["max_budget_usd"] == 0.5
    # a fresh agent stays byte-identical (0 / blank)
    fresh = _node("agent")
    assert fresh.props["max_rpm"] == 0 and fresh.props["max_budget_usd"] == 0
    d2 = D.AgentDialog(None, _node("agent")); d2.max_rpm.setText("x")
    assert d2.apply() is not None
    d3 = D.AgentDialog(None, _node("agent")); d3.max_budget_usd.setText("lots")
    assert d3.apply() is not None


def test_agent_final_schema_roundtrip(qapp):
    node = _node("agent")
    dlg = D.AgentDialog(None, node)
    dlg.final_schema.setPlainText('{"type":"object","required":["answer"]}')
    dlg.final_schema_retries.setText("3")
    assert dlg.apply() is None
    assert node.props["final_schema"].startswith("{")
    assert node.props["final_schema_retries"] == 3
    d2 = D.AgentDialog(None, _node("agent")); d2.final_schema.setPlainText("{bad")
    assert d2.apply() is not None                    # invalid JSON
    d3 = D.AgentDialog(None, _node("agent")); d3.final_schema.setPlainText("[]")
    assert d3.apply() is not None                    # not an object


def test_agent_final_schema_reaches_codegen(qapp):
    import graph_model as gm, graph_codegen as gc
    LLM = {"provider": "siliconflow", "model": "m", "api_key": "sk", "base_url": "u"}
    g = gm.Graph()
    a = g.new_node("agent", 0, 0); a.name = "w"
    a.props["final_schema"] = '{"type":"object","required":["answer"]}'
    llm = g.new_node("llm", -200, 0); llm.props.update(LLM)
    g.add_edge(llm.id, a.id)
    out = gc.generate_from_graph(g, "phase2_fs_probe", gui=False)
    import os
    src = open(os.path.join(out, "agent.py"), encoding="utf-8").read()
    import shutil; shutil.rmtree(out, ignore_errors=True)
    assert "final_schema" in src and "## Structured final answer" in src


def test_eval_case_dialog_graders(qapp):
    # default = legacy expected_output (byte-identical: no `type` key)
    d = D._EvalCaseDialog(None, "Add")
    d.input.setPlainText("2+2?"); d.value.setPlainText("4")
    r = d.result()
    assert r.get("expected_output") == "4" and "type" not in r
    # numeric + tolerance -> modern form
    d2 = D._EvalCaseDialog(None, "Add")
    d2.grade.setCurrentIndex(d2.grade.findData("numeric"))
    d2.input.setPlainText("pi?"); d2.value.setPlainText("3.14"); d2.tolerance.setText("0.01")
    r2 = d2.result()
    assert r2["type"] == "numeric" and r2["value"] == "3.14" and r2["tolerance"] == 0.01
    # contains + negate -> type form (legacy can't express `not`)
    d3 = D._EvalCaseDialog(None, "Add")
    d3.input.setPlainText("x"); d3.value.setPlainText("err"); d3.negate.setChecked(True)
    r3 = d3.result()
    assert r3["type"] == "contains" and r3["not"] is True and "expected_output" not in r3
    # contains_all -> list value
    d4 = D._EvalCaseDialog(None, "Add")
    d4.grade.setCurrentIndex(d4.grade.findData("contains_all"))
    d4.input.setPlainText("x"); d4.value.setPlainText("a\nb")
    assert d4.result()["value"] == ["a", "b"]
    # modern case round-trips through load
    d5 = D._EvalCaseDialog(None, "Edit",
                           {"id": "c", "input": "i", "type": "length", "min": 5, "max": 9})
    r5 = d5.result()
    assert r5["type"] == "length" and r5["min"] == 5 and r5["max"] == 9


# ── Phase 3 Extra Settings (tool / HITL / guardrail) ─────────────────────────
def test_guardrail_extra_settings(qapp):
    import graph_codegen as gc
    node = _node("guardrail")
    dlg = D.GuardrailNodeDialog(None, node)
    dlg.patterns.setPlainText(r"SECRET-\d+")
    dlg.keywords.setPlainText("banana\ncherry")
    dlg.max_length.setText("500")
    assert dlg.apply() is None
    assert node.props["patterns"] == [r"SECRET-\d+"]
    assert node.props["keywords"] == ["banana", "cherry"]
    assert node.props["max_length"] == 500
    cfg = gc._guardrail_node_cfg(node)
    assert cfg["patterns"] and cfg["keywords"] and cfg["max_length"] == 500
    # bad regex is caught at authoring
    d2 = D.GuardrailNodeDialog(None, _node("guardrail"))
    d2.patterns.setPlainText("(unclosed")
    assert d2.apply() is not None
    # blank guardrail node stays byte-identical (only checks + on_trip)
    assert set(gc._guardrail_node_cfg(_node("guardrail"))) == {"checks", "on_trip"}


def test_tool_extra_settings_roundtrip(qapp):
    import codegen
    node = _node("tool")
    node.props["files"] = ["load_csv.py"]
    funcs = codegen.list_tool_functions(["load_csv.py"])
    if not funcs:
        return                                    # tool library missing this file
    fn = funcs[0]
    node.props["tool_props"] = {fn: {"return_direct": True, "risk": "high"}}
    dlg = D.ToolDialog(None, node)
    assert dlg.apply() is None
    tp = node.props["tool_props"]
    assert tp[fn]["return_direct"] is True and tp[fn]["risk"] == "high"


def test_hitl_extra_settings(qapp):
    import graph_codegen as gc
    node = _node("hitl")
    dlg = D.HITLDialog(None, node)
    dlg.dec_edit.setChecked(False)                # allow only approve + reject
    dlg.hitl_timeout.setText("30")
    dlg.on_timeout.setCurrentText("reject")
    assert dlg.apply() is None
    assert set(node.props["decisions"]) == {"approve", "reject"}
    assert node.props["timeout"] == 30 and node.props["on_timeout"] == "reject"
    gate = {}
    gc._hitl_extra(gate, node.props["decisions"], node.props["timeout"],
                   node.props["on_timeout"])
    assert set(gate["decisions"]) == {"approve", "reject"} and gate["timeout"] == 30
    # at least one decision required
    d2 = D.HITLDialog(None, _node("hitl"))
    for cb in (d2.dec_approve, d2.dec_edit, d2.dec_reject):
        cb.setChecked(False)
    assert d2.apply() is not None
    # all-decisions + no-timeout adds nothing (byte-identical)
    g2 = {}
    gc._hitl_extra(g2, ["approve", "edit", "reject"], 0, "approve")
    assert g2 == {}


def test_hitl_route_mode_grays_gate_options(qapp):
    """A HITL with 2+ outgoing links auto-enters ROUTE mode: the gate-only options
    (on-reject, approve/edit/reject, on-timeout) are grayed out; the route controls
    (default branch, prompt, timeout) stay live. A 1-outgoing HITL stays GATE mode."""
    from PySide6.QtWidgets import QWidget
    g = Graph()
    a = g.new_node("agent", 0, 0); a.name = "A"
    h = g.new_node("hitl", 100, 0); h.name = "gate"
    b = g.new_node("agent", 200, -40); b.name = "B"
    c = g.new_node("agent", 200, 40); c.name = "C"
    g.add_edge(a.id, h.id); g.add_edge(h.id, b.id); g.add_edge(h.id, c.id)  # 2 out -> route
    parent = QWidget(); parent.graph = g
    dlg = D.HITLDialog(parent, h)
    assert dlg._route_mode is True
    # gate-only options grayed out (widgets AND labels)
    assert not dlg.on_reject.isEnabled() and not dlg._lbl_reject.isEnabled()
    assert not dlg.dec_approve.isEnabled() and not dlg.dec_reject.isEnabled()
    assert not dlg.on_timeout.isEnabled() and not dlg._dec_lbl.isEnabled()
    # route controls stay live
    assert dlg.default_route is not None and dlg.default_route.isEnabled()
    assert dlg.prompt.isEnabled() and dlg.hitl_timeout.isEnabled()
    dlg.default_route.setCurrentText("C")
    assert dlg.apply() is None
    assert h.props["default_route"] == "C"

    # a 1-outgoing HITL stays GATE mode with the review options ENABLED
    g2 = Graph()
    a2 = g2.new_node("agent", 0, 0); h2 = g2.new_node("hitl", 100, 0)
    d2 = g2.new_node("agent", 200, 0)
    g2.add_edge(a2.id, h2.id); g2.add_edge(h2.id, d2.id)
    p2 = QWidget(); p2.graph = g2
    dg = D.HITLDialog(p2, h2)
    assert dg._route_mode is False
    assert dg.on_reject.isEnabled() and dg.dec_approve.isEnabled()
    assert dg.default_route is None


def test_schedule_strategy_grays_unused_fields(qapp):
    """The Schedule strategy selector picks ONE mode and grays the others' fields:
    daily -> only 'at'; once -> only 'start_at'; interval -> every/offset."""
    n = _node("schedule"); n.props["mode"] = "daily"; n.props["at"] = "09:00"
    d = D.ScheduleDialog(None, n)
    assert not d.every.isEnabled() and not d.offset.isEnabled() and not d.run_at_start.isEnabled()
    assert d.at.isEnabled() and not d.start_at.isEnabled()
    assert d.apply() is None and n.props["mode"] == "daily" and n.props["at"] == "09:00"

    n2 = _node("schedule"); n2.props["mode"] = "once"; n2.props["start_at"] = "2030-01-02 03:04"
    d2 = D.ScheduleDialog(None, n2)
    assert d2.start_at.isEnabled() and not d2.every.isEnabled() and not d2.at.isEnabled()
    assert d2.apply() is None and n2.props["mode"] == "once"

    n3 = _node("schedule")   # interval (default): every/offset live, at/start_at grayed
    d3 = D.ScheduleDialog(None, n3)
    assert d3.every.isEnabled() and not d3.at.isEnabled() and not d3.start_at.isEnabled()

    n4 = _node("schedule"); n4.props["mode"] = "daily"; n4.props["at"] = ""   # daily, no time
    assert D.ScheduleDialog(None, n4).apply() is not None      # blocks with an error


# ── Phase 4 Extra Settings (RAG / MCP / While / Router / WebServer) ──────────
def test_rag_extra_settings(qapp):
    node = _node("rag")
    dlg = D.RagDialog(None, node)
    dlg.query_transform.setCurrentText("multi_query")
    dlg.multi_query_n.setText("5")
    dlg.score_threshold.setText("0.3")
    dlg.metadata_filter.setText("*.md")
    assert dlg.apply() is None
    assert node.props["query_transform"] == "multi_query"
    assert node.props["multi_query_n"] == 5 and node.props["score_threshold"] == 0.3
    assert node.props["metadata_filter"] == "*.md"


def test_rag_advanced_is_collapsible(qapp):
    """The RAG advanced pipeline lives in collapsible sections (like the Agent
    node), collapsed by default, so the dialog opens compact — and its fields
    still round-trip through apply()."""
    from canvas_qt.dialogs import _Collapsible
    dlg = D.RagDialog(None, _node("rag"))
    sections = dlg.findChildren(_Collapsible)
    titles = [s._title for s in sections]
    for want in ("Chunking", "Retrieval & ranking", "Embedding & vector store"):
        assert want in titles, titles
    assert all(s._btn.isChecked() is False for s in sections), \
        "advanced RAG sections collapsed by default"


def test_rag_qdrant_vector_store(qapp):
    """Qdrant is selectable as a vector store and its URL/key round-trip (blank
    URL = embedded on-disk)."""
    node = _node("rag")
    dlg = D.RagDialog(None, node)
    stores = [dlg.vector_db.itemText(i) for i in range(dlg.vector_db.count())]
    assert "qdrant" in stores, stores
    dlg.vector_db.setCurrentText("qdrant")
    dlg.qdrant_url.setText("http://localhost:6333")
    dlg.qdrant_api_key.setText("secret")
    assert dlg.apply() is None
    assert node.props["vector_db"] == "qdrant"
    assert node.props["qdrant_url"] == "http://localhost:6333"
    assert node.props["qdrant_api_key"] == "secret"


def test_mcp_extra_settings(qapp):
    node = _node("mcp")
    dlg = D.McpDialog(None, node)
    dlg.transport.setCurrentText("stdio"); dlg.command.setText("python")
    dlg.allow_tools.setText("search, fetch"); dlg.connect_timeout.setText("5")
    dlg.env.setPlainText("FOO=bar")
    assert dlg.apply() is None
    assert node.props["allow_tools"] == "search, fetch"
    assert node.props["connect_timeout"] == "5" and node.props["env"] == "FOO=bar"
    d2 = D.McpDialog(None, _node("mcp")); d2.transport.setCurrentText("stdio")
    d2.command.setText("python"); d2.call_timeout.setText("x")
    assert d2.apply() is not None                    # bad timeout rejected


def test_while_max_iterations(qapp):
    node = _node("while")
    dlg = D.WhileDialog(None, node)
    dlg.max_iters.setText("3")
    assert dlg.apply() is None
    assert node.props["max_iterations"] == 3
    d2 = D.WhileDialog(None, _node("while")); d2.max_iters.setText("-1")
    assert d2.apply() is not None


def test_router_extra_settings(qapp):
    node = _node("router")
    dlg = D.RouterDialog(None, node)
    dlg.routing_model.setText("cheap"); dlg.routing_provider.setCurrentText("openai")
    assert dlg.apply() is None
    assert node.props["routing_model"] == "cheap"
    assert node.props["routing_provider"] == "openai"


def test_webserver_extra_settings(qapp):
    node = _node("webserver")
    dlg = D.WebServerDialog(None, node)
    dlg.auto_allow.setChecked(True)
    dlg.origins.setText("https://a, https://b")
    dlg.max_conns.setText("10")
    assert dlg.apply() is None
    assert node.props["auto_allow_tools"] is True
    assert node.props["allowed_origins"] == ["https://a", "https://b"]
    assert node.props["max_connections"] == 10
    d2 = D.WebServerDialog(None, _node("webserver")); d2.tls_cert.setText("c.pem")
    assert d2.apply() is not None                    # TLS needs both cert + key


# ── Phase 5 Extra Settings (custom GUI in the GUI node) ──────────────────────
def test_gui_dialog_default_blank(qapp):
    """A fresh GUI node has no custom source and apply() keeps it blank (blank =
    the built-in window = byte-identical generated gui.py)."""
    node = _node("gui")
    dlg = D.GUIDialog(None, node)
    assert dlg._src == ""
    assert dlg.status.text().startswith("Using the built-in")
    assert dlg.apply() is None
    assert node.props.get("custom_gui", "") == ""


def test_gui_dialog_custom_source_roundtrip(qapp):
    """A valid custom gui.py source round-trips to props; a syntax error blocks."""
    node = _node("gui")
    dlg = D.GUIDialog(None, node)
    dlg._src = "import agent\nprint(agent.run('hi'))\n"
    dlg._refresh_status()
    assert "Custom gui.py loaded" in dlg.status.text()
    assert "⚠" not in dlg.status.text()               # imports agent + calls .run
    assert dlg.apply() is None
    assert node.props["custom_gui"] == "import agent\nprint(agent.run('hi'))\n"
    # a syntax error is a hard failure at apply()
    d2 = D.GUIDialog(None, _node("gui"))
    d2._src = "def (:\n"
    assert d2.apply() is not None


def test_gui_dialog_soft_warnings(qapp):
    """Compilable-but-inert sources save, but the status flags the caveat."""
    node = _node("gui")
    dlg = D.GUIDialog(None, node)
    dlg._src = "x = 1\n"                               # no import agent
    dlg._refresh_status()
    assert "⚠" in dlg.status.text() and "import agent" in dlg.status.text()
    assert dlg.apply() is None                         # soft caveat, still saves
    # clearing reverts to the built-in window
    dlg._clear()
    assert dlg._src == "" and dlg.status.text().startswith("Using the built-in")


def test_validate_custom_gui_helper(qapp):
    assert D._validate_custom_gui("") is None           # blank = built-in
    assert D._validate_custom_gui("import agent\n") is None
    assert D._validate_custom_gui("print('@AGENT_NAME@')\n") is None  # marker ok
    assert D._validate_custom_gui("def (:\n") is not None


def test_gui_dialog_has_extra_settings_group(qapp):
    from canvas_qt.dialogs import _Collapsible
    dlg = D.GUIDialog(None, _node("gui"))
    titles = [s._title for s in dlg.findChildren(_Collapsible)]
    assert any(t.startswith("Extra Settings") for t in titles), titles


def test_extra_settings_multiline_have_placeholders(qapp):
    """Every large blank multiline box in an Extra Settings group (and the LLM
    response-schema box) carries an in-box example hint (placeholderText), so it's
    never an unexplained empty box."""
    cases = [
        (D.LLMDialog, "llm", ["stop", "extra", "response_schema"]),
        (D.McpDialog, "mcp", ["env", "headers"]),
        (D.GuardrailNodeDialog, "guardrail", ["patterns", "keywords"]),
    ]
    for cls, kind, fields in cases:
        dlg = cls(None, _node(kind))
        for f in fields:
            ph = getattr(dlg, f).placeholderText()
            assert ph.strip(), f"{kind}.{f} multiline box has no placeholder hint"


def test_big_config_dialogs_are_scrollable(qapp):
    """Every dialog that grew an Extra Settings collapsible wraps its contents in a
    QScrollArea, so its minimum height stays below the screen and the collapsible can
    expand without the window exceeding the display (regression: LLMDialog, then
    GUIDialog, hit `QWindowsWindow::setGeometry: Unable to set geometry` — the clamp
    warning — because a word-wrapped hint + expanding group pushed the real minimum
    above the initially-requested geometry)."""
    from PySide6.QtWidgets import QScrollArea
    for kind, cls in _WIDE_DIALOGS:
        dlg = cls(None, _node(kind))
        assert dlg.findChildren(QScrollArea), f"{kind} dialog must be scrollable"
        assert dlg.minimumSizeHint().height() < 700, \
            (kind, dlg.minimumSizeHint().height())


_WIDE_DIALOGS = (
    ("agent", D.AgentDialog), ("llm", D.LLMDialog), ("rag", D.RagDialog),
    ("gui", D.GUIDialog), ("workerpool", D.WorkerPoolDialog),
    ("router", D.RouterDialog), ("hitl", D.HITLDialog), ("tool", D.ToolDialog),
    ("webserver", D.WebServerDialog), ("mcp", D.McpDialog),
    ("guardrail", D.GuardrailNodeDialog),
)


def test_open_path_opens_wide_dialogs_at_preferred_size(qapp):
    """The real bug behind the setGeometry clamp warning: open_config_dialog calls
    make_dialog_resizable(), whose setWindowFlags() recreates the native window on
    Windows and DISCARDS the size set in __init__ — the dialog then opened narrow,
    word-wrapped hints wrapped tall, and Qt clamped (warning). make_dialog_resizable
    must re-assert a size >= the layout's sizeHint in BOTH dimensions (so there is no
    upward clamp, since sizeHint >= minimumSize) for the scroll-wrapped dialogs."""
    for kind, cls in _WIDE_DIALOGS:
        dlg = cls(None, _node(kind))
        D.make_dialog_resizable(dlg)
        sh = dlg.sizeHint()
        assert dlg.size().width() >= sh.width(), (kind, dlg.size().width(), sh.width())
        assert dlg.size().height() >= sh.height(), (kind, dlg.size().height(), sh.height())
        assert dlg.size().width() >= 500, (kind, dlg.size().width())   # opened wide


def test_open_path_leaves_plain_dialogs_alone(qapp):
    """make_dialog_resizable must NOT enlarge naturally-sized dialogs (no QScrollArea)
    — only the scroll-wrapped ones get their explicit size re-asserted."""
    dlg = D.ConditionDialog(None, _node("condition"))
    before = dlg.size()
    D.make_dialog_resizable(dlg)
    assert dlg.size() == before, (before, dlg.size())


def test_agent_dialog_apply(qapp):
    node = _node("agent")
    dlg = D.AgentDialog(None, node)
    dlg.name.setText("planner1")
    dlg.role.setCurrentText("planner")
    dlg.hitl_threshold.setText("0.5")
    assert dlg.apply() is None
    assert node.name == "planner1"
    assert node.props["role"] == "planner"
    assert node.props["hitl_confidence_threshold"] == 0.5


def test_guardrails_is_a_collapsible_group(qapp):
    """Guardrails is a collapsible section (like Planner/Capabilities/Budgets),
    collapsed by default, and its checkboxes still round-trip to node.props."""
    from canvas_qt.dialogs import _Collapsible
    node = _node("agent")
    dlg = D.AgentDialog(None, node)
    sections = dlg.findChildren(_Collapsible)
    titles = [s._title for s in sections]
    assert any(t.startswith("Guardrails") for t in titles), titles
    gr = next(s for s in sections if s._title.startswith("Guardrails"))
    assert gr._btn.isChecked() is False, "guardrails group collapsed by default"
    # the guardrail checkboxes remain wired + round-trip on apply()
    assert "scan_output" in dlg._gr and "pii" in dlg._gr
    dlg._gr["pii"].setChecked(True)
    assert dlg.apply() is None
    assert node.props["guardrails"]["pii"] is True


def test_agent_dialog_offers_orchestrator_role(qapp):
    import patterns
    dlg = D.AgentDialog(None, _node("agent"))
    roles = [dlg.role.itemText(i) for i in range(dlg.role.count())]
    assert "orchestrator" in roles, roles
    assert "orchestrator" in patterns.PATTERNS


def test_prompt_dialog_offers_orchestrator_template(qapp):
    import graph_codegen
    # the orchestrator role must be selectable on a Prompt node...
    dlg = D.PromptDialog(None, _node("prompt"))
    roles = [dlg.role.itemText(i) for i in range(dlg.role.count())]
    assert "orchestrator" in roles, roles
    # ...and "Load Role Template" must fill in the real orchestrator template
    # (not the 'single' fallback). Set the role without firing on_role, which
    # would pop a modal "replace text?" prompt and block headless.
    dlg.role.blockSignals(True)
    dlg.role.setCurrentText("orchestrator")
    dlg.role.blockSignals(False)
    dlg.on_template()
    loaded = dlg.text.toPlainText()
    assert "spawn_subagent" in loaded
    assert loaded == graph_codegen.role_template("orchestrator")
    assert loaded != graph_codegen.role_template("single")


def test_agent_dialog_rejects_bad_budget(qapp):
    node = _node("agent")
    dlg = D.AgentDialog(None, node)
    list(dlg.budgets.values())[0].setText("lots")
    assert dlg.apply() is not None


def test_workerpool_dialog_apply(qapp):
    node = _node("workerpool")
    dlg = D.WorkerPoolDialog(None, node)
    dlg.max_workers.setText("8")
    assert dlg.apply() is None
    assert node.props["max_workers"] == 8


def test_prompt_dialog_loads_template(qapp):
    node = _node("prompt")
    dlg = D.PromptDialog(None, node)
    dlg.role.blockSignals(True)
    dlg.role.setCurrentText("planner")
    dlg.role.blockSignals(False)
    dlg.on_template()
    assert dlg.apply() is None
    assert node.props["role"] == "planner"
    assert node.props["text"].strip()


def test_rag_dialog_validates_ints(qapp):
    node = _node("rag")
    dlg = D.RagDialog(None, node)
    dlg.chunk.setText("xx")
    assert dlg.apply() is not None
    dlg.chunk.setText("500")
    dlg.top_k.setText("3")
    dlg.docs_dir.setText("docs")
    assert dlg.apply() is None
    assert node.props["chunk_chars"] == 500


def test_tool_dialog_view_and_edit_code(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr("codegen.TOOLS_DIR", str(tmp_path))
    f = tmp_path / "demo_tool.py"
    f.write_text("from tool_registry import tool\n\n\n@tool\ndef demo():\n    return 1\n",
                 encoding="utf-8")
    # the code dialog shows the source and is editable
    cd = D._ToolCodeDialog(None, "demo_tool.py")
    assert "def demo()" in cd.editor.toPlainText() and cd._readonly is False
    # a valid edit saves to disk
    cd.editor.setPlainText(
        "from tool_registry import tool\n\n\n@tool\ndef demo():\n    return 2\n")
    cd.on_save()
    assert "return 2" in f.read_text(encoding="utf-8")
    # an invalid edit + Cancel must NOT overwrite the file
    monkeypatch.setattr(D.QMessageBox, "warning",
                        lambda *a, **k: D.QMessageBox.Cancel)
    cd.editor.setPlainText("def broken(:\n")
    cd.on_save()
    assert "return 2" in f.read_text(encoding="utf-8"), "cancelled save must not write"
    # the Tools node dialog lists the file; double-click routes to the code dialog
    node = _node("tool")
    node.props["files"] = ["demo_tool.py"]
    td = D.ToolDialog(None, node)
    names = [td.listw.item(i).text() for i in range(td.listw.count())]
    assert "demo_tool.py" in names
    monkeypatch.setattr(D._ToolCodeDialog, "exec", lambda self: 0)
    item = next(td.listw.item(i) for i in range(td.listw.count())
                if td.listw.item(i).text() == "demo_tool.py")
    td._view_tool_code(item)            # opens the (stubbed) code dialog; must not raise


def test_webserver_dialog_validates_port(qapp):
    node = _node("webserver")
    dlg = D.WebServerDialog(None, node)
    dlg.port.setText("99999")
    assert dlg.apply() is not None
    dlg.port.setText("8800")
    assert dlg.apply() is None
    assert node.props["port"] == 8800


def test_mcp_dialog_requires_url_for_http(qapp):
    node = _node("mcp")
    dlg = D.McpDialog(None, node)
    dlg.transport.setCurrentText("streamable_http")
    dlg.url.setText("")
    assert dlg.apply() is not None
    dlg.url.setText("https://localhost:7777/mcp")
    assert dlg.apply() is None


def test_eval_dialog_apply(qapp):
    node = _node("eval")
    dlg = D.EvalDialog(None, node)
    dlg._cases = [{"id": "c1", "input": "hi", "expected_output": "hello"}]
    assert dlg.apply() is None
    assert node.props["cases"][0]["id"] == "c1"


def test_skills_dialog_apply(qapp):
    node = _node("skill")
    dlg = D.SkillsNodeDialog(None, node)
    dlg._skills = [{"name": "tone", "text": "be concise"}]
    assert dlg.apply() is None
    assert node.props["skills"][0]["name"] == "tone"


# ── custom / nested state types (Approach A) ─────────────────────────────────
def test_type_defs_dialog_defines_custom_type():
    """TypeDefsDialog builds a named type from visual sub-fields, validates, and
    writes it onto graph.type_defs."""
    from canvas_qt.dialogs import TypeDefsDialog, _TypeDefDialog
    g = Graph()
    td = _TypeDefDialog(None, "", None, taken=set(), type_names=[])
    td.name.setText("Finding")
    td._fields = [("id", "str"), ("score", "float")]
    td._reload()
    td.merge.setCurrentText("merge_deep")
    name, spec = td.result()
    assert name == "Finding"
    assert spec["schema"]["properties"]["score"]["type"] == "number"
    dlg = TypeDefsDialog(None, g)
    dlg._defs = {name: spec}
    assert dlg.apply() is None                       # valid → no error
    assert g.type_defs["Finding"]["merge"] == "merge_deep"


def test_state_field_dialog_offers_custom_types_and_upsert():
    """A custom type shows as `Name` and `list[Name]`; list[Name] offers upsert;
    the field carries merge_key."""
    from canvas_qt.dialogs import _StateFieldDialog
    tds = {"Finding": {"schema": {"type": "object",
                                  "properties": {"id": {"type": "string"}}},
                       "merge": "merge_deep"}}
    d = _StateFieldDialog(None, "Add state field", type_defs=tds)
    opts = [d.type.itemText(i) for i in range(d.type.count())]
    assert "Finding" in opts and "list[Finding]" in opts
    d.type.setCurrentText("list[Finding]")
    d._sync_reducers()
    reds = [d.reducer.itemText(i) for i in range(d.reducer.count())]
    assert "upsert_by_key" in reds and "extend" in reds
    d.name.setText("findings")
    d.reducer.setCurrentText("upsert_by_key")
    d.merge_key.setText("id")
    r = d.result()
    assert r["type"] == "list[Finding]" and r["reducer"] == "upsert_by_key"
    assert r["merge_key"] == "id"


def test_type_defs_survive_graph_round_trip():
    """Defined types persist through to_dict/from_dict (and thus .mta)."""
    g = Graph()
    g.type_defs = {"Rec": {"schema": {"type": "object",
                                      "properties": {"k": {"type": "string"}}},
                           "merge": "merge_shallow"}}
    g2 = Graph.from_dict(g.to_dict())
    assert g2.type_defs == g.type_defs


def test_type_def_custom_merge_function():
    """merge=custom exposes a merge_src editor; the resulting type_def carries the
    source, and a field of that type then offers the 'custom' update policy."""
    from canvas_qt.dialogs import _TypeDefDialog, _StateFieldDialog
    d = _TypeDefDialog(None, "", None, taken=set())
    d.name.setText("Tally")
    d.merge.setCurrentText("custom")
    d._sync_merge_src()
    assert not d.merge_src.isHidden()          # shown for custom (dialog isn't exec'd)
    d.merge.setCurrentText("merge_deep"); d._sync_merge_src()
    assert d.merge_src.isHidden()              # hidden for non-custom
    d.merge.setCurrentText("custom"); d._sync_merge_src()
    d.merge_src.setPlainText("def merge(old, new):\n    return (old or 0) + new")
    name, spec = d.result()
    assert name == "Tally" and spec["merge"] == "custom"
    assert "def merge" in spec["merge_src"]
    # a field of Tally now offers 'custom'
    sf = _StateFieldDialog(None, "Add state field", type_defs={name: spec})
    sf.type.setCurrentText("Tally")
    sf._sync_reducers()
    reds = [sf.reducer.itemText(i) for i in range(sf.reducer.count())]
    assert "custom" in reds, reds


def test_agent_dialog_on_budget_roundtrip(qapp):
    """Agent → Extra Settings → 'On budget exceeded' round-trips continue/stop/retry."""
    node = _node("agent")
    dlg = D.AgentDialog(None, node)
    dlg.on_budget.setCurrentText("stop")
    assert dlg.apply() is None
    assert node.props["on_budget"] == "stop"


def test_state_dialog_run_wall_clock_roundtrip(qapp):
    """Graph → Edit Shared State → 'Max run wall-clock' round-trips to the graph."""
    g = Graph()
    dlg = D.StateSchemaDialog(None, g)
    dlg.run_wall.setValue(45)
    dlg.apply()
    assert g.run_wall_clock_s == 45
