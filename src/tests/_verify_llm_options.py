"""Verify the LLM node's optional API params (temperature, top_p,
response_format, extra) flow from canvas props -> config.json -> the generated
runtime's _sampling_kwargs(), with the right provider gating."""

import importlib.util
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import graph_codegen
from graph_model import Graph, default_props

# ── 1. _parse_llm_opts: blanks omitted, numbers parsed, json mode + extra ──
# _parse_llm_opts always emits request_timeout_s (default 120); section 5b
# tests that explicitly. Here we strip it so the "no override" cases stay sharp.
def P(props):
    opts = graph_codegen._parse_llm_opts(props)
    opts.pop("request_timeout_s", None)
    return opts

assert P({"temperature": "", "top_p": "", "response_format": "text",
          "extra": ""}) == {}, "all-blank props must yield no overrides"
assert P({"temperature": "0.2", "top_p": "0.9"}) == {
    "temperature": 0.2, "top_p": 0.9}
assert P({"temperature": "not-a-number"}) == {}, "bad number is dropped"
assert P({"response_format": "json_object"}) == {
    "response_format": "json_object"}
assert P({"response_format": "text"}) == {}, "text format = no override"
assert P({"extra": '{"seed": 7, "stop": ["x"]}'}) == {
    "extra": {"seed": 7, "stop": ["x"]}}
assert P({"extra": "not json"}) == {}, "invalid extra JSON is dropped"
assert P({"extra": "[1,2]"}) == {}, "non-object extra JSON is dropped"
print("ok 1: _parse_llm_opts parses/validates props")

# defaults carry the new keys
d = default_props("llm")
for k in ("temperature", "top_p", "response_format", "extra"):
    assert k in d, k
assert d["response_format"] == "text"
print("ok 2: llm default_props expose the new fields")

# ── 3. props -> config.json (graph_codegen wiring) ──────────────────────────
g = Graph()
a = g.new_node("agent", 0, 0); a.name = "planner"
llm = g.new_node("llm", 0, 0)
llm.props.update(provider="siliconflow", model="deepseek-ai/DeepSeek-V4-Flash",
                 api_key="sk-test",
                 base_url="https://api.siliconflow.cn/v1",
                 temperature="0.3", top_p="0.8",
                 response_format="json_object",
                 extra='{"seed": 42}')
g.add_edge(llm.id, a.id)
out_dir = graph_codegen.generate_from_graph(g, "demo_llmopts", gui=False)
cfg = json.load(open(os.path.join(out_dir, "config.json"), encoding="utf-8"))
lc = cfg["llms"]["planner"][0]
assert lc["temperature"] == 0.3 and lc["top_p"] == 0.8, lc
assert lc["response_format"] == "json_object", lc
assert lc["extra"] == {"seed": 42}, lc
print("ok 3: options threaded into config.json llms[planner][0]")

# ── 4. generated _sampling_kwargs: provider gating ──────────────────────────
spec = importlib.util.spec_from_file_location(
    "demo_llmopts_agent", os.path.join(out_dir, "agent.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

oai = mod._sampling_kwargs(lc, "siliconflow")
assert oai["temperature"] == 0.3 and oai["top_p"] == 0.8
assert oai["response_format"] == {"type": "json_object"}, oai
assert oai["seed"] == 42, "extra params merged verbatim"

ant = mod._sampling_kwargs(lc, "anthropic")
assert ant["temperature"] == 0.3 and ant["top_p"] == 0.8
assert "response_format" not in ant, "JSON mode is OpenAI-only"
assert ant["seed"] == 42
print("ok 4: _sampling_kwargs gates response_format to OpenAI providers")

# a bare LLM (no options) adds no sampling kwargs
bare = {"provider": "siliconflow", "model": "m"}
assert mod._sampling_kwargs(bare, "siliconflow") == {}
print("ok 5: a bare LLM adds no sampling kwargs")

# ── 5b. request_timeout_s: parsed, threaded, and passed as `timeout` ────────
from graph_model import default_props
assert default_props("llm")["request_timeout_s"] == 120
assert graph_codegen._parse_llm_opts({"request_timeout_s": "45"}) == {
    "request_timeout_s": 45.0}
assert graph_codegen._parse_llm_opts({"request_timeout_s": ""}) == {
    "request_timeout_s": 120}  # blank field keeps the 120s default
import httpx
# request_timeout_s now becomes an httpx.Timeout with a SHORT connect timeout so a
# blocked/proxied connection fails fast instead of hanging; read covers a slow model.
to = mod._sampling_kwargs({"request_timeout_s": 45}, "siliconflow")["timeout"]
assert isinstance(to, httpx.Timeout) and to.read == 45.0 and to.connect == 15.0, to
to = mod._sampling_kwargs({"request_timeout_s": 30}, "anthropic")["timeout"]
assert isinstance(to, httpx.Timeout) and to.read == 30.0 and to.connect == 15.0, to  # both
to = mod._sampling_kwargs({"request_timeout_s": 8}, "siliconflow")["timeout"]
assert to.connect == 8.0, to    # connect clamped to <= the read timeout
assert "timeout" not in mod._sampling_kwargs(bare, "siliconflow")  # none set
print("ok 5b: request_timeout_s → per-call httpx.Timeout (short connect) on every provider")

# ── 5c. proxy: parsed per-LLM (only when set) and wired into the http client ──
assert "proxy" not in graph_codegen._parse_llm_opts({"proxy": ""})        # blank omitted
assert "proxy" not in graph_codegen._parse_llm_opts({})                   # absent omitted
assert graph_codegen._parse_llm_opts({"proxy": "http://p:8080"})["proxy"] == "http://p:8080"
assert default_props("llm")["proxy"] == ""
# _eff_proxy precedence: per-LLM proxy, else top-level CONFIG proxy, else None.
assert mod._eff_proxy({"proxy": "http://a:1"}) == "http://a:1"
mod.CONFIG["proxy"] = "http://glob:2"
assert mod._eff_proxy({}) == "http://glob:2"          # falls back to top-level config
assert mod._eff_proxy({"proxy": "http://a:1"}) == "http://a:1"   # per-LLM still wins
mod.CONFIG.pop("proxy", None)
assert mod._eff_proxy({}) is None                     # neither set → env fallback (trust_env)
# the built client carries the proxy + the short connect timeout
hc = mod._http_client({"proxy": "http://10.0.0.9:8080", "request_timeout_s": 60})
assert any("10.0.0.9" in str(getattr(getattr(t, "_pool", None), "_proxy_url", ""))
           for t in hc._mounts.values()), "proxy not wired into the http client"
assert hc.timeout.connect == 15.0 and hc.timeout.read == 60.0, hc.timeout
print("ok 5c: per-LLM proxy parsed + wired (per-LLM > config > env), short connect timeout")

# ── 6. provider-capability map ──────────────────────────────────────────────
from graph_model import response_format_support as sup
assert sup("openai", "text") == "yes"
assert sup("siliconflow", "json_object") == "yes"
assert sup("anthropic", "json_object") == "no"     # Claude has no bare JSON mode
assert sup("openai", "json_schema") == "yes"
assert sup("gemini", "json_schema") == "yes"
assert sup("anthropic", "json_schema") == "yes"    # via output_config.format
assert sup("siliconflow", "json_schema") == "weak"  # model-dependent
assert sup("deepseek", "json_schema") == "weak"
print("ok 6: response_format_support classifies vendors correctly")

# ── 7. json_schema translates per vendor ────────────────────────────────────
SCHEMA = {"type": "object", "properties": {"x": {"type": "string"}},
          "required": ["x"], "additionalProperties": False}
g2 = Graph()
a2 = g2.new_node("agent", 0, 0); a2.name = "planner"
llm2 = g2.new_node("llm", 0, 0)
llm2.props.update(provider="openai", model="gpt-4o", api_key="sk",
                  response_format="json_schema",
                  response_schema=json.dumps(SCHEMA))
g2.add_edge(llm2.id, a2.id)
out2 = graph_codegen.generate_from_graph(g2, "demo_jsonschema", gui=False)
cfg2 = json.load(open(os.path.join(out2, "config.json"), encoding="utf-8"))
lc2 = cfg2["llms"]["planner"][0]
assert lc2["response_format"] == "json_schema"
assert lc2["response_schema"] == SCHEMA
spec2 = importlib.util.spec_from_file_location(
    "demo_jsonschema_agent", os.path.join(out2, "agent.py"))
m2 = importlib.util.module_from_spec(spec2); spec2.loader.exec_module(m2)

# OpenAI-family → response_format.json_schema (strict)
oai2 = m2._response_format_kwargs(lc2, "openai")
assert oai2["response_format"]["type"] == "json_schema"
assert oai2["response_format"]["json_schema"]["schema"] == SCHEMA
assert oai2["response_format"]["json_schema"]["strict"] is True

# Gemini reuses the OpenAI-family shape
gem = m2._response_format_kwargs(lc2, "gemini")
assert gem["response_format"]["type"] == "json_schema"

# Anthropic → output_config.format (no response_format key)
ant2 = m2._response_format_kwargs(lc2, "anthropic")
assert "response_format" not in ant2
assert ant2["output_config"]["format"] == {"type": "json_schema", "schema": SCHEMA}
print("ok 7: json_schema → response_format (OpenAI/Gemini) vs output_config "
      "(Anthropic)")

# ── 8. json_schema with no schema degrades gracefully ───────────────────────
noschema = {"provider": "openai", "response_format": "json_schema"}  # schema absent
assert m2._response_format_kwargs(noschema, "openai") == {
    "response_format": {"type": "json_object"}}, "degrade to JSON mode"
assert m2._response_format_kwargs(noschema, "anthropic") == {}, "skip on Anthropic"
print("ok 8: schema-less json_schema degrades (OpenAI→json_object, Anthropic→none)")

# ── 9. Gemini provider generates against the OpenAI client path ──────────────
g3 = Graph()
a3 = g3.new_node("agent", 0, 0); a3.name = "worker"
llm3 = g3.new_node("llm", 0, 0)
llm3.props.update(provider="gemini", model="gemini-2.5-flash", api_key="sk",
                  base_url="https://generativelanguage.googleapis.com/v1beta/openai/")
g3.add_edge(llm3.id, a3.id)
out3 = graph_codegen.generate_from_graph(g3, "demo_gemini", gui=False)
reqs = open(os.path.join(out3, "requirements.txt"), encoding="utf-8").read()
assert "openai" in reqs and "anthropic" not in reqs, reqs
src = open(os.path.join(out3, "agent.py"), encoding="utf-8").read()
assert "from openai import OpenAI" in src
print("ok 9: Gemini provider uses the OpenAI client path (openai requirement)")

print("\nALL LLM-OPTION CHECKS PASSED")
