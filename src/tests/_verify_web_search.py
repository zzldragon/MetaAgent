"""Verify the built-in web_search tool (H2b).

An agent with web_search=True gets the keyless DuckDuckGo web_search tool: it's
added to the agent's tools + a prompt tail, marked high-risk (HITL-confirms by
default), and `ddgs` lands in requirements.txt. The tool itself is exercised with
a FAKE ddgs module (no network) plus the fail-soft no-query / no-package paths.
"""

import importlib.util
import os
import shutil
import sys
import types

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import graph_codegen  # noqa: E402
from app_config import GENERATED_DIR  # noqa: E402
from graph_model import Graph  # noqa: E402

LLM = {"provider": "siliconflow", "model": "deepseek-ai/DeepSeek-V4-Flash",
       "api_key": "sk-test", "base_url": "https://api.siliconflow.cn/v1"}

# ── build a single agent with web_search on ─────────────────────────────────
g = Graph()
a = g.new_node("agent", 0, 0); a.name = "Researcher"; a.props["web_search"] = True
llm = g.new_node("llm", 0, 0); llm.props.update(LLM); g.add_edge(llm.id, a.id)

info = graph_codegen.analyze(g)
assert not info["errors"], info["errors"]

out = os.path.join(GENERATED_DIR, "verify_web_search")
if os.path.exists(out):
    shutil.rmtree(out)
out = graph_codegen.generate_from_graph(g, "verify_web_search", gui=False)

# requirements + config wiring
reqs = open(os.path.join(out, "requirements.txt"), encoding="utf-8").read()
assert "ddgs" in reqs, reqs
import json  # noqa: E402
cfg = json.load(open(os.path.join(out, "config.json"), encoding="utf-8"))
assert "web_search" in cfg["high_risk_tools"], cfg["high_risk_tools"]
print("ok 1: web_search -> requirements(ddgs) + high_risk_tools (HITL-gated)")

spec = importlib.util.spec_from_file_location("vws_agent", os.path.join(out, "agent.py"))
mod = importlib.util.module_from_spec(spec)
sys.path.insert(0, out); os.chdir(out)
spec.loader.exec_module(mod)
os.chdir(BASE)

assert "web_search" in mod.AGENTS[mod.ENTRY]["tools"], mod.AGENTS[mod.ENTRY]["tools"]
assert "## Web search" in mod.AGENTS[mod.ENTRY]["system"], "prompt tail missing"
assert mod._web_search_tool_schema()["name"] == "web_search"
print("ok 2: tool in the agent spec + prompt tail + schema")

# fail-soft: no query
assert mod._web_search({"query": ""}).startswith("[ERROR]"), "empty query should error"
assert mod._web_search({}).startswith("[ERROR]")

# exercise with a FAKE ddgs (no network): formatting + note prefix + max_results.
fake = types.ModuleType("ddgs")


class _FakeDDGS:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def text(self, query, max_results=5):
        rows = [{"title": "Python", "href": "https://python.org", "body": "the docs"},
                {"title": "PEPs", "href": "https://peps.python.org", "body": "proposals"},
                {"title": "Wiki", "href": "https://wiki.python.org", "body": "wiki"}]
        return rows[:max_results]


fake.DDGS = _FakeDDGS
sys.modules["ddgs"] = fake
out_txt = mod._web_search({"query": "python", "max_results": 2})
assert out_txt.startswith("[note: results from a live web search"), out_txt
assert "https://python.org" in out_txt and "[Python]" in out_txt, out_txt
assert "https://wiki.python.org" not in out_txt, "max_results not honored"
print("ok 3: web_search formats results (title/url/snippet) + capping + advisory note")

# fake provider that raises -> fail-soft [ERROR], never propagates
class _BoomDDGS(_FakeDDGS):
    def text(self, query, max_results=5):
        raise RuntimeError("network down")


fake.DDGS = _BoomDDGS
assert mod._web_search({"query": "x"}).startswith("[ERROR] web search failed"), "should fail soft"
print("ok 4: web_search fails soft on provider error (no exception escapes)")

# ── multi-engine (config-driven) ─────────────────────────────────────────────
# generated config.json ships an editable web_search block (keyless default)
import json as _json
cfg_on_disk = _json.load(open(os.path.join(out, "config.json"), encoding="utf-8"))
assert cfg_on_disk["web_search"] == {"engine": "duckduckgo", "api_key": "", "base_url": ""}, cfg_on_disk.get("web_search")
print("ok 5: config.json ships an editable web_search block (duckduckgo default)")

# a non-ddg engine (tavily) via stubbed HTTP — no network, no ddgs needed
calls = {}
def _fake_http(url, headers=None, body=None, params=None, proxy=None, timeout=20):
    calls["url"] = url
    return {"results": [{"title": "T", "url": "https://t.example", "content": "hi"}]}
mod._ws_http_json = _fake_http
mod.CONFIG["web_search"] = {"engine": "tavily", "api_key": "tvly-x"}
r = mod._web_search({"query": "q"})
assert "https://t.example" in r and "tavily" in calls["url"], (r, calls)
print("ok 6: configured engine (tavily) dispatched via HTTP, formats results")

# FAILOVER: primary raises -> falls back to the keyless duckduckgo (still faked ok)
fake.DDGS = _FakeDDGS               # ddg works again
def _boom_http(*a, **k):
    raise RuntimeError("402 quota")
mod._ws_http_json = _boom_http
mod.CONFIG["web_search"] = {"engines": [{"engine": "tavily", "api_key": "bad"}]}
r2 = mod._web_search({"query": "python"})
assert "https://python.org" in r2, ("expected ddg fallback", r2)
print("ok 7: engines failover — tavily fails -> keyless duckduckgo fallback used")

# baidu -> routed through SerpApi's baidu engine (engine=baidu param)
seen = {}
def _serp_http(url, headers=None, body=None, params=None, proxy=None, timeout=20):
    seen["params"] = params
    return {"organic_results": [{"title": "百度", "link": "https://baidu.com", "snippet": "r"}]}
mod._ws_http_json = _serp_http
mod.CONFIG["web_search"] = {"engine": "baidu", "api_key": "serp"}
rb = mod._web_search({"query": "深度学习"})
assert "https://baidu.com" in rb and seen["params"]["engine"] == "baidu", (rb, seen)
assert "baidu" in mod._SEARCH_ENGINES
print("ok 8: baidu supported (via SerpApi engine=baidu)")

# direct-first, then-proxy fallback (deterministic: pin the proxy source)
mod._ws_proxy = lambda *a: "http://10.144.1.10:8080"
mod.CONFIG["web_search"] = {"engine": "duckduckgo"}
seen = []
def _fail_direct(query, n, cfg, proxy):
    seen.append(proxy)
    if proxy is None:
        raise OSError("direct blocked (firewall)")
    return [{"title": "T", "url": "https://ok", "body": "b"}]
mod._SEARCH_ENGINES["duckduckgo"] = _fail_direct
rp = mod._web_search({"query": "q"})
assert seen == [None, "http://10.144.1.10:8080"], seen        # DIRECT first, then proxy
assert "https://ok" in rp, rp
print("ok 9: web_search tries DIRECT first, falls back to the proxy on failure")

seen.clear()
mod._SEARCH_ENGINES["duckduckgo"] = lambda q, n, c, p: (seen.append(p) or [{"title": "T", "url": "https://d"}])
mod._web_search({"query": "q"})
assert seen == [None], seen                                    # direct success -> no proxy retry
print("ok 10: a successful direct search does NOT needlessly retry via the proxy")

seen.clear()
mod._ws_proxy = lambda *a: None                              # no proxy configured
def _down(q, n, c, p): seen.append(p); raise OSError("down")
mod._SEARCH_ENGINES["duckduckgo"] = _down
assert mod._web_search({"query": "q"}).startswith("[ERROR]") and seen == [None], seen
print("ok 11: no proxy configured -> direct only, fails soft")

# ── per-agent web_search config (Agent node → Extra Settings → Web search) ────
# Test the REAL resolver (_ws_config / _ws_proxy) — reload a clean module so the
# earlier monkeypatches on _ws_proxy don't shadow it.
import importlib  # noqa: E402
_spec2 = importlib.util.spec_from_file_location("vws_agent2", os.path.join(out, "agent.py"))
mod2 = importlib.util.module_from_spec(_spec2)
sys.path.insert(0, out); os.chdir(out)
_spec2.loader.exec_module(mod2)
os.chdir(BASE)
mod2.CONFIG["web_search"] = {"engine": "duckduckgo", "api_key": "", "base_url": ""}
mod2.CONFIG["proxy"] = "http://global-proxy:8080"
mod2.CONFIG["web_search_by_agent"] = {
    "researcher": {"engine": "tavily", "api_key": "tvly-A", "proxy": "http://agent-proxy:9090"},
    "scout": {"proxy": "http://scout-proxy:1234"},          # proxy-only override
}
rc = mod2._ws_config("researcher")
assert rc.get("engine") == "tavily" and rc.get("api_key") == "tvly-A", rc
assert mod2._ws_proxy("researcher") == "http://agent-proxy:9090", mod2._ws_proxy("researcher")
sc = mod2._ws_config("scout")                                # proxy-only -> keeps global engine
assert sc.get("engine") == "duckduckgo", sc
assert mod2._ws_proxy("scout") == "http://scout-proxy:1234", mod2._ws_proxy("scout")
assert mod2._ws_config("nobody").get("engine") == "duckduckgo"      # no override
assert mod2._ws_proxy("nobody") == "http://global-proxy:8080", mod2._ws_proxy("nobody")
assert mod2._ws_config(None) == mod2.CONFIG["web_search"]           # back-compatible
print("ok 12: per-agent web_search override (engine/key/proxy) merges over the global block")

# codegen emits web_search_by_agent from the Agent node props (non-blank only)
g2 = Graph()
r = g2.new_node("agent", 0, 0); r.name = "researcher"; r.props["web_search"] = True
r.props["web_search_engine"] = "tavily"; r.props["web_search_api_key"] = "tvly-X"
r.props["web_search_proxy"] = "http://p:8080"
s = g2.new_node("agent", 0, 0); s.name = "plain"; s.props["web_search"] = True   # no override
l2 = g2.new_node("llm", 0, 0); l2.props.update(LLM)
g2.add_edge(l2.id, r.id); g2.add_edge(l2.id, s.id); g2.add_edge(r.id, s.id)
out2 = graph_codegen.generate_from_graph(g2, "verify_ws_by_agent", gui=False)
cfg2 = _json.load(open(os.path.join(out2, "config.json"), encoding="utf-8"))
wba = cfg2.get("web_search_by_agent") or {}
assert wba.get("researcher") == {"engine": "tavily", "api_key": "tvly-X", "proxy": "http://p:8080"}, wba
assert "plain" not in wba, wba                        # blank override -> not emitted
print("ok 13: codegen emits web_search_by_agent only for agents with a non-blank override")

print("ALL WEB-SEARCH CHECKS PASSED")
