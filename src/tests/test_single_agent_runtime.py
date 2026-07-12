"""Behavioral contract for codegen.generate_agent (the single 'ReAct' agent).

After #2 the single-agent path is unified onto graph_codegen (a 1-node pipeline).
These tests assert the *behavior* a single agent must keep -- it generates, the
agent.py imports, run() returns, the public API exists, and feature flags
(vision / rag / tools / gui / websocket) take effect -- independent of whether
the agent.py is produced by the old template or the unified pipeline path.
"""

import importlib.util
import json
import os

import pytest

import codegen

BASE = {
    "provider": "siliconflow",
    "model": "deepseek-ai/DeepSeek-V4-Flash",
    "api_key": "sk-test",
    "base_url": "https://api.siliconflow.cn/v1",
    "pattern": "react",
    "system_prompt": "You are a helpful test agent.",
    "tools": [],
    "budgets": {"max_iterations": 6, "max_tool_calls": 10,
                "max_output_tokens": 4_000,
                "max_wall_clock_s": 60},
}


def _gen_and_import(settings, modname):
    out_dir = codegen.generate_agent(settings)
    spec = importlib.util.spec_from_file_location(
        modname, os.path.join(out_dir, "agent.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return out_dir, mod


def _stub_llm(mod):
    """Stub the model call for whichever runtime shape was generated."""
    if hasattr(mod, "_call_one"):                 # unified pipeline runtime
        mod._call_one = lambda agent_name, cfg, system, messages: ("stub answer", [])
    elif hasattr(mod, "llm"):                      # legacy single-agent runtime
        mod.llm = lambda *a, **k: ("stub answer", [])
    if hasattr(mod, "clear_history"):
        mod.clear_history()


def test_public_api_present():
    _out, mod = _gen_and_import({**BASE, "name": "sa_api"}, "sa_api_mod")
    for fn in ("run", "set_trace_sink", "request_cancel"):
        assert hasattr(mod, fn), fn


def test_runs_and_returns():
    _out, mod = _gen_and_import({**BASE, "name": "sa_run"}, "sa_run_mod")
    _stub_llm(mod)
    result = mod.run("do the thing", emit=lambda s: None)
    assert "stub answer" in str(result), result


def test_trace_sink_emits_run_lifecycle():
    _out, mod = _gen_and_import({**BASE, "name": "sa_trace"}, "sa_trace_mod")
    records = []
    mod.set_trace_sink(records.append)
    _stub_llm(mod)
    mod.run("go", emit=lambda s: None)
    kinds = [r.get("kind") for r in records]
    assert "run_start" in kinds and "run_end" in kinds, kinds


def test_tools_inlined():
    _out, mod = _gen_and_import(
        {**BASE, "name": "sa_tools", "tools": ["load_csv.py"]}, "sa_tools_mod")
    assert "load_csv" in mod.TOOLS


def test_vision_flag_takes_effect():
    out, mod = _gen_and_import(
        {**BASE, "name": "sa_vision", "vision": True}, "sa_vision_mod")
    assert mod.entry_vision() is True
    assert hasattr(mod, "encode_image") and hasattr(mod, "set_run_images")


def test_rag_adds_search_docs(tmp_path):
    out, mod = _gen_and_import(
        {**BASE, "name": "sa_rag",
         "rag": {"docs_dir": str(tmp_path), "top_k": 3}}, "sa_rag_mod")
    assert "search_docs" in mod.TOOLS


@pytest.mark.parametrize("gui,websocket", [(False, False), (True, True)])
def test_gui_and_server_files(gui, websocket):
    out, _mod = _gen_and_import(
        {**BASE, "name": f"sa_files_{int(gui)}{int(websocket)}",
         "gui": gui, "websocket": websocket}, f"sa_files_{int(gui)}{int(websocket)}_mod")
    assert os.path.exists(os.path.join(out, "gui.py")) == gui
    assert os.path.exists(os.path.join(out, "server.py")) == websocket
