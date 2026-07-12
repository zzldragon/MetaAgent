"""Generate / compile matrix — the structural safety net.

For a representative spread of graphs (every pattern and most block types) plus
the single-agent ReAct path, this:
  1. generates the standalone agent into generated_agents/mtx_<key>/, and
  2. byte-compiles every emitted top-level *.py (agent.py, gui.py, server.py,
     run_evals.py) with py_compile.

No LLM calls and no network — it only checks that the code generators produce
valid, importable Python for each feature combination. This is exactly the class
of regression the @PLACEHOLDER@ string-template assembly can introduce and that
the per-feature `_verify_*` scripts (which stub `_call_one`) don't all cover.
"""

import os
import py_compile

import pytest

import codegen
import graph_codegen
from graph_model import Graph

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GENERATED_DIR = os.path.join(ROOT, "generated_agents")

LLM = dict(provider="siliconflow", model="deepseek-ai/DeepSeek-V4-Flash",
           api_key="sk-test", base_url="https://api.siliconflow.cn/v1")


@pytest.fixture(scope="module", autouse=True)
def _clean_mtx_dirs():
    """This matrix writes throwaway mtx_* / mtx_react_* agents straight into the
    curated generated_agents/ gallery; remove them afterwards so they don't look
    like real demos (runs even on failure via yield-teardown)."""
    import glob
    import shutil
    yield
    for d in glob.glob(os.path.join(GENERATED_DIR, "mtx_*")):
        shutil.rmtree(d, ignore_errors=True)


def _agent(g, name, kind="agent", role=None, llm=True, **props):
    n = g.new_node(kind, 0, 0)
    n.name = name
    if role:
        n.props["role"] = role
    n.props.update(props)
    if llm:
        node = g.new_node("llm", 0, 0)
        node.props.update(LLM)
        assert g.add_edge(node.id, n.id) is None
    return n


# ── graph builders: each returns (Graph, gui_flag) ──────────────────────────

def g_planner_executor():
    g = Graph()
    p = _agent(g, "planner", role="planner")
    w = _agent(g, "executor", role="worker")
    g.add_edge(p.id, w.id)
    return g, False


def g_pec_loop():
    g = Graph()
    p = _agent(g, "planner", role="planner")
    w = _agent(g, "executor", role="worker")
    c = _agent(g, "critic", role="critic")
    g.add_edge(p.id, w.id)
    g.add_edge(w.id, c.id)
    g.add_edge(c.id, p.id)               # bounded revise loop
    return g, False


def g_supervisor():
    g = Graph()
    s = _agent(g, "supervisor", role="supervisor")
    w = _agent(g, "worker", role="worker")
    g.add_edge(s.id, w.id)
    return g, False


def g_router():
    g = Graph()
    tri = _agent(g, "triage", kind="router")
    billing = _agent(g, "billing", role="single")
    tech = _agent(g, "tech", role="single")
    g.add_edge(tri.id, billing.id)
    g.add_edge(tri.id, tech.id)
    return g, False


def g_worker_pool():
    g = Graph()
    p = _agent(g, "planner", role="planner")
    pool = _agent(g, "pool", kind="workerpool")
    g.add_edge(p.id, pool.id)
    return g, False


def g_tools():
    g = Graph()
    a = _agent(g, "worker", role="single")
    t = g.new_node("tool", 0, 0)
    t.props["files"] = ["load_csv.py"]
    g.add_edge(t.id, a.id)
    return g, False


def g_rag():
    g = Graph()
    a = _agent(g, "worker", role="single")
    r = g.new_node("rag", 0, 0)
    r.props["docs_dir"] = os.path.join(ROOT, "templates")   # any text folder
    g.add_edge(r.id, a.id)
    return g, False


def g_mcp():
    g = Graph()
    a = _agent(g, "worker", role="single")
    m = g.new_node("mcp", 0, 0)
    m.props.update(transport="stdio", command="python",
                   args="_mcp_test_server.py")
    g.add_edge(m.id, a.id)
    return g, False


def g_hitl():
    g = Graph()
    a = _agent(g, "a", role="planner")
    b = _agent(g, "b", role="worker")
    h = g.new_node("hitl", 0, 0)
    g.add_edge(a.id, h.id)
    g.add_edge(h.id, b.id)               # a -> HITL -> b
    return g, False


def g_eval():
    g = Graph()
    a = _agent(g, "worker", role="single")
    ev = g.new_node("eval", 0, 0)
    ev.props["cases"] = [{"input": "say hi", "expected_output": "hi"}]
    g.add_edge(ev.id, a.id)
    return g, False


def g_webserver():
    g = Graph()
    _agent(g, "worker", role="single")
    g.new_node("webserver", 0, 0)        # standalone -> emits server.py
    return g, False


def g_vision():
    g = Graph()
    a = g.new_node("agent", 0, 0)
    a.name = "worker"
    a.props["role"] = "single"
    llm = g.new_node("llm", 0, 0)
    llm.props.update(LLM, vision=True)
    g.add_edge(llm.id, a.id)
    return g, False


def g_gui_and_server():
    g = Graph()
    _agent(g, "worker", role="single")
    g.new_node("webserver", 0, 0)
    return g, True                       # gui=True -> also emits gui.py


def g_fanout():
    g = Graph()
    d = _agent(g, "dispatch", role="single")
    a = _agent(g, "an_a", role="single")
    b = _agent(g, "an_b", role="single")
    t = _agent(g, "tail", role="single")
    fo = g.new_node("fanout", 0, 0); fo.name = "fo"
    jn = g.new_node("join", 0, 0); jn.name = "jn"
    g.add_edge(d.id, fo.id)
    g.add_edge(fo.id, a.id); g.add_edge(fo.id, b.id)
    g.add_edge(a.id, jn.id); g.add_edge(b.id, jn.id)
    g.add_edge(jn.id, t.id)
    return g, False


GRAPH_CASES = {
    "planner_executor": g_planner_executor,
    "fanout": g_fanout,
    "pec_loop": g_pec_loop,
    "supervisor": g_supervisor,
    "router": g_router,
    "worker_pool": g_worker_pool,
    "tools": g_tools,
    "rag": g_rag,
    "mcp": g_mcp,
    "hitl": g_hitl,
    "eval": g_eval,
    "webserver": g_webserver,
    "vision": g_vision,
    "gui_and_server": g_gui_and_server,
}


def _compile_dir(out_dir):
    compiled = []
    for fname in sorted(os.listdir(out_dir)):
        if fname.endswith(".py"):
            path = os.path.join(out_dir, fname)
            py_compile.compile(path, doraise=True)
            compiled.append(fname)
    assert "agent.py" in compiled, f"no agent.py generated in {out_dir}"
    return compiled


@pytest.mark.parametrize("key", list(GRAPH_CASES))
def test_graph_generates_and_compiles(key):
    graph, gui = GRAPH_CASES[key]()
    # analyze should not report blocking errors for these canonical graphs
    info = graph_codegen.analyze(graph)
    assert not info["errors"], f"{key}: {info['errors']}"
    out_dir = graph_codegen.generate_from_graph(graph, f"mtx_{key}", gui=gui)
    compiled = _compile_dir(out_dir)
    if gui:
        assert "gui.py" in compiled, compiled
    if key in ("webserver", "gui_and_server"):
        assert "server.py" in compiled, compiled
    if key == "eval":
        assert "run_evals.py" in compiled, compiled


# ── single-agent (ReAct) path: now the unified 1-node pipeline (#2) ─────────

@pytest.mark.parametrize("gui,websocket", [(False, False), (True, True)])
def test_single_agent_generates_and_compiles(gui, websocket):
    settings = {
        "name": f"mtx_react_{'gui' if gui else 'cli'}",
        "provider": "siliconflow",
        "model": "deepseek-ai/DeepSeek-V4-Flash",
        "api_key": "sk-test",
        "base_url": "https://api.siliconflow.cn/v1",
        "pattern": "react",
        "gui": gui,
        "websocket": websocket,
        "system_prompt": "You are a helpful test agent.",
        "tools": ["load_csv.py"],
        "budgets": {
            "max_iterations": 10, "max_tool_calls": 20,
            "max_output_tokens": 8_000,
            "max_wall_clock_s": 60,
        },
    }
    out_dir = codegen.generate_agent(settings)
    compiled = _compile_dir(out_dir)
    if gui:
        assert "gui.py" in compiled, compiled
    if websocket:
        assert "server.py" in compiled, compiled
