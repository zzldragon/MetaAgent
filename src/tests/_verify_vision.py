"""Verify image input (vision): LLM-node `vision` flag, config emission +
entry_vision(), encode_image/decode_data_url, the provider wire mappings
(_to_openai / _to_anthropic), and that images reach only the entry agent and
only when its model is a vision model (no network — the LLM is monkeypatched)."""

import importlib.util
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import graph_codegen
from graph_model import Graph, default_props

# ── 1. model: LLM node gains a vision flag (default off) ────────────────────
assert default_props("llm")["vision"] is False
print("ok 1: LLM node default_props has vision=False")


def _build(vision_entry: bool, name: str):
    g = Graph()
    a = g.new_node("agent", 0, 0); a.name = "alpha"      # entry
    b = g.new_node("agent", 0, 0); b.name = "beta"
    la = g.new_node("llm", 0, 0)
    la.props.update(api_key="sk", model="vision-model", vision=vision_entry)
    lb = g.new_node("llm", 0, 0)
    lb.props.update(api_key="sk", model="text-model", vision=False)
    g.add_edge(la.id, a.id); g.add_edge(lb.id, b.id)
    g.add_edge(a.id, b.id)                                # alpha → beta chain
    out = graph_codegen.generate_from_graph(g, name, gui=True)
    spec = importlib.util.spec_from_file_location(name + "_agent",
                                                  os.path.join(out, "agent.py"))
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return out, mod

# ── 2. config emission + entry_vision() ─────────────────────────────────────
out, mod = _build(True, "demo_vision")
cfg = json.load(open(os.path.join(out, "config.json"), encoding="utf-8"))
assert cfg["llms"]["alpha"][0]["vision"] is True
assert cfg["llms"]["beta"][0]["vision"] is False
assert mod.entry_vision() is True and mod._vision_ok("beta") is False
print("ok 2: vision threads into config + entry_vision() reflects the entry LLM")

# ── 3. encode_image + decode_data_url ───────────────────────────────────────
img_path = os.path.join(out, "_probe.png")
with open(img_path, "wb") as f:
    f.write(b"\\x89PNG\\r\\n\\x1a\\n fake but png-extensioned")
part = mod.encode_image(img_path)
assert part["type"] == "image" and part["media_type"] == "image/png"
assert isinstance(part["data"], str) and part["data"]
try:
    mod.encode_image(out + "/x.tiff")
    raise AssertionError("should reject unsupported type")
except ValueError:
    pass
d = mod.decode_data_url("data:image/jpeg;base64,QUJD")
assert d == {"type": "image", "media_type": "image/jpeg", "data": "QUJD"}, d
print("ok 3: encode_image (+ reject unknown) and decode_data_url work")

# ── 4. provider wire mappings for a multimodal user turn ────────────────────
content = mod._with_images("describe this", [part])
assert isinstance(content, list) and content[0]["type"] == "text"
oai = mod._to_openai("sys", [{"role": "user", "content": content}])
uimg = oai[-1]["content"]
assert uimg[0]["type"] == "text" and uimg[1]["type"] == "image_url"
assert uimg[1]["image_url"]["url"].startswith("data:image/png;base64,")
ant = mod._to_anthropic([{"role": "user", "content": content}])
ablocks = ant[-1]["content"]
assert ablocks[0]["type"] == "text" and ablocks[1]["type"] == "image"
assert ablocks[1]["source"] == {"type": "base64", "media_type": "image/png",
                                "data": part["data"]}, ablocks[1]
# plain text content is passed through unchanged (no regression)
assert mod._to_openai("s", [{"role": "user", "content": "hi"}])[-1]["content"] == "hi"
print("ok 4: _to_openai -> image_url, _to_anthropic -> base64 source")

# ── 5. images reach the ENTRY agent only, attached to its first user turn ───
captured = {}
def fake_llm(agent_name, system, messages, emit=print):
    captured[agent_name] = messages[0]["content"]
    return "done", []
mod.llm = fake_llm
mod.clear_history()
mod.run("look", emit=lambda s: None, images=[img_path])
assert isinstance(captured["alpha"], list), "entry got a multimodal turn"
assert any(p.get("type") == "image" for p in captured["alpha"]), captured["alpha"]
assert isinstance(captured["beta"], str), "downstream agent got plain text"
print("ok 5: images attach to the entry agent's first turn, not downstream")

# ── 6. a text-only entry model drops attached images ────────────────────────
out2, mod2 = _build(False, "demo_vision_text")
assert mod2.entry_vision() is False
cap2 = {}
def fake_llm2(agent_name, system, messages, emit=print):
    cap2[agent_name] = messages[0]["content"]
    return "done", []
mod2.llm = fake_llm2
mod2.clear_history()
mod2.run("look", emit=lambda s: None, images=[img_path])
assert isinstance(cap2["alpha"], str), "non-vision entry must not get image parts"
print("ok 6: a non-vision entry model drops attached images (text only)")

# ── 6b. if the API rejects the image, run() finishes fast with a clear hint
#        (it must NOT hang or crash the GUI), and images don't leak forward ──
class _ImgErr(Exception):
    status_code = 400
def boom(agent_name, system, messages, emit=print):
    raise _ImgErr("This model does not support image input")
mod.llm = boom
mod.clear_history()
res = mod.run("look", emit=lambda s: None, images=[img_path])
assert res.startswith("[error]"), res
assert "image input" in res and "vision-capable" in res, res
assert mod._RUN_IMAGES == [], "images cleared after the failed run"
# a fresh text-only run after the failure is unaffected (no stale images)
mod.llm = lambda a, s, m, emit=print: ("ok", [])
assert mod.run("hi", emit=lambda s: None) == "ok"
print("ok 6b: image-reject finishes fast with an actionable hint; no leak")

# ── 7. single-agent path also wires vision (config + entry_vision) ──────────
# A single agent is generated as a 1-node pipeline (#2 unification), so vision
# lives per-agent under config['llms'][<agent>][0]['vision'].
import codegen
settings = {
    "name": "demo_vision_single", "provider": "openai", "model": "gpt-4o",
    "api_key": "sk", "base_url": "", "pattern": "react", "gui": True,
    "system_prompt": "You see images.", "tools": [], "vision": True,
    "budgets": {"max_iterations": 6, "max_tool_calls": 10,
                "max_output_tokens": 4000,
                "max_wall_clock_s": 60},
}
sout = codegen.generate_agent(settings)
scfg = json.load(open(os.path.join(sout, "config.json"), encoding="utf-8"))
assert scfg["llms"]["demo_vision_single"][0]["vision"] is True
sspec = importlib.util.spec_from_file_location(
    "demo_vision_single_agent", os.path.join(sout, "agent.py"))
smod = importlib.util.module_from_spec(sspec); sspec.loader.exec_module(smod)
assert smod.entry_vision() is True
assert hasattr(smod, "encode_image") and hasattr(smod, "set_run_images")
print("ok 7: single-agent path threads vision + has the image helpers")

print("\nALL VISION CHECKS PASSED")
