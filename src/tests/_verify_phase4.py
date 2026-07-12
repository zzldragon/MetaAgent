"""Verify Phase-4 Extra Settings (RAG / MCP / Router / WebServer). Codegen wiring +
byte-identical-when-blank, plus pure runtime helpers (no network). The While cap is
covered by _verify_while.py."""

import importlib.util
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import graph_codegen as gc
from graph_model import Graph

LLM = dict(provider="siliconflow", model="deepseek-ai/DeepSeek-V4-Flash", api_key="sk",
           base_url="https://api.siliconflow.cn/v1")


def _gen(tag, build):
    g = Graph()
    build(g)
    out = gc.generate_from_graph(g, tag, gui=False)
    return out, json.load(open(os.path.join(out, "config.json"), encoding="utf-8"))


def _mod(out, tag):
    spec = importlib.util.spec_from_file_location(tag + "_m", os.path.join(out, "agent.py"))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


# ── 1. RAG: score_threshold / metadata_filter / multi_query_n ────────────────
def _rag(g, rag_props):
    a = g.new_node("agent", 0, 0); a.name = "a"
    llm = g.new_node("llm", 0, 0); llm.props.update(LLM); g.add_edge(llm.id, a.id)
    r = g.new_node("rag", -200, 0); r.name = "kb"
    r.props["docs_dir"] = BASE            # any real folder (we only inspect config)
    r.props.update(rag_props)
    g.add_edge(r.id, a.id)


out, cfg = _gen("p4_rag", lambda g: _rag(g, {"score_threshold": 0.3,
                                             "metadata_filter": "*.md",
                                             "query_transform": "multi_query",
                                             "multi_query_n": 5}))
rc = cfg["rag"][0]
assert rc["score_threshold"] == 0.3 and rc["metadata_filter"] == "*.md"
assert rc["multi_query_n"] == 5 and rc["query_transform"] == "multi_query"
_, cfg0 = _gen("p4_rag_blank", lambda g: _rag(g, {}))
for k in ("score_threshold", "metadata_filter", "multi_query_n"):
    assert k not in cfg0["rag"][0], f"{k} must be absent when unset ({cfg0['rag'][0]})"
m = _mod(out, "p4_rag")
assert m._rag_source_match("policies/a.md", "*.md") and not m._rag_source_match("b.txt", "*.md")
assert m._rag_source_match("x/y.txt", "*.md, *.txt")           # multi-glob
chunks = [{"source": "a", "text": "low"}, {"source": "b", "text": "high"}]
parts = m._rag_format([(0, 0.1), (1, 0.5)], chunks, 5, False, False, 0.3)
assert len(parts) == 1 and "high" in parts[0], parts    # 0.1 dropped by threshold
print("1. RAG score_threshold + metadata_filter + multi_query wired ok")

# ── 2. MCP: allow/deny/timeouts/env/headers (via _mcp_server_config) ─────────
g = Graph()
mstdio = g.new_node("mcp", 0, 0); mstdio.name = "srv"
mstdio.props.update(transport="stdio", command="python", args="s.py",
                    allow_tools="add, shout", deny_tools="danger",
                    connect_timeout="5", call_timeout="10", env="FOO=bar\nBAZ=1")
e = gc._mcp_server_config(mstdio)
assert e["allow_tools"] == ["add", "shout"] and e["deny_tools"] == ["danger"]
assert e["connect_timeout"] == 5 and e["call_timeout"] == 10
assert e["env"] == {"FOO": "bar", "BAZ": "1"}
mhttp = g.new_node("mcp", 0, 0); mhttp.name = "h"
mhttp.props.update(transport="streamable_http", url="http://x/mcp",
                   headers="Authorization: Bearer tok\nX-Env: prod")
eh = gc._mcp_server_config(mhttp)
assert eh["headers"] == {"Authorization": "Bearer tok", "X-Env": "prod"}, eh
mblank = g.new_node("mcp", 0, 0); mblank.name = "b"
mblank.props.update(transport="stdio", command="python")
eb = gc._mcp_server_config(mblank)
for k in ("allow_tools", "deny_tools", "connect_timeout", "call_timeout", "env", "headers"):
    assert k not in eb, f"{k} must be absent when unset ({eb})"
print("2. MCP allow/deny/timeouts/env/headers wired; blank byte-identical ok")

# ── 3. Router: default_route + routing-LLM override; _extract_route default ──
def _router(g, rprops):
    r = g.new_node("router", 0, 0); r.name = "router"; r.props.update(rprops)
    lr = g.new_node("llm", 0, 0); lr.props.update(LLM); g.add_edge(lr.id, r.id)
    for nm in ("A", "B"):
        n = g.new_node("agent", 0, 0); n.name = nm
        ln = g.new_node("llm", 0, 0); ln.props.update(LLM); g.add_edge(ln.id, n.id)
        g.add_edge(r.id, n.id)


out, cfg = _gen("p4_router", lambda g: _router(g, {"default_route": "B",
                                                   "routing_provider": "openai",
                                                   "routing_model": "cheap"}))
assert cfg["llms"]["router"][0]["model"] == "cheap", cfg["llms"]["router"][0]
m = _mod(out, "p4_router")
assert m.AGENTS["router"].get("default_route") == "B", m.AGENTS["router"]
# distinctive route names + text with none of them, so the substring matcher
# genuinely falls through to the default (then to the first successor)
assert m._extract_route([], "zzz qqq", ["Writer", "Reader"], "Reader") == "Reader"
assert m._extract_route([], "zzz qqq", ["Writer", "Reader"]) == "Writer"
out0, cfg0 = _gen("p4_router_blank", lambda g: _router(g, {}))
m0 = _mod(out0, "p4_router_blank")
assert "default_route" not in m0.AGENTS["router"], "blank must not set default_route"
assert len(cfg0["llms"]["router"]) == 1, "blank must not prepend a routing LLM"
print("3. Router default_route + routing-LLM override wired; blank byte-identical ok")

# ── 4. WebServer: tls / cors / max_conn / auto_allow_tools ───────────────────
def _ws(g, wprops):
    a = g.new_node("agent", 0, 0); a.name = "a"
    llm = g.new_node("llm", 0, 0); llm.props.update(LLM); g.add_edge(llm.id, a.id)
    w = g.new_node("webserver", 0, 0); w.name = "srv"; w.props.update(wprops)
    g.add_edge(a.id, w.id)


_, cfg = _gen("p4_ws", lambda g: _ws(g, {"auto_allow_tools": True,
                                         "tls_cert": "c.pem", "tls_key": "k.pem",
                                         "allowed_origins": ["https://x"],
                                         "max_connections": 5}))
s = cfg["server"]
assert s["auto_allow_tools"] is True and s["tls_cert"] == "c.pem" and s["tls_key"] == "k.pem"
assert s["allowed_origins"] == ["https://x"] and s["max_connections"] == 5
_, cfg0 = _gen("p4_ws_blank", lambda g: _ws(g, {}))
s0 = cfg0["server"]
assert s0["auto_allow_tools"] is False
for k in ("tls_cert", "tls_key", "allowed_origins", "max_connections"):
    assert k not in s0, f"{k} must be absent when unset ({s0})"
print("4. WebServer tls/cors/max_conn/auto_allow wired; blank byte-identical ok")

print("\nALL PHASE-4 CHECKS PASSED")
