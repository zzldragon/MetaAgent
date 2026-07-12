"""Verify M2: large tool results are offloaded to a workspace file.

Per-agent opt-in (offload_results). When on and a tool result exceeds the
threshold, the runtime writes the FULL text to <workspace>/offloaded/<tool>_<hash>
.txt and puts only a pointer + head/tail preview into the context; the agent gets
a read_offload tool to fetch the full text. Covers the adversarial-review fixes:
crash-proof/clamped threshold, framework-marker pass-through, head+tail preview,
workspace-RELATIVE pointer (docker-safe), read_offload round-trip + path guard,
and no re-offload loop.
"""

import glob
import importlib.util
import json
import os
import shutil
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import graph_codegen  # noqa: E402
from app_config import GENERATED_DIR  # noqa: E402
from graph_model import Graph  # noqa: E402

LLM = {"provider": "siliconflow", "model": "deepseek-ai/DeepSeek-V4-Flash",
       "api_key": "sk-test", "base_url": "https://api.siliconflow.cn/v1"}

g = Graph()
a = g.new_node("agent", 0, 0); a.name = "A"; a.props["offload_results"] = True
llm = g.new_node("llm", 0, 0); llm.props.update(LLM); g.add_edge(llm.id, a.id)

out = os.path.join(GENERATED_DIR, "verify_offload")
if os.path.exists(out):
    shutil.rmtree(out)
out = graph_codegen.generate_from_graph(g, "verify_offload", gui=False)

# 1. config: threshold only (no global toggle); wired per-agent; read_offload added.
cfg = json.load(open(os.path.join(out, "config.json"), encoding="utf-8"))
assert cfg["offload_threshold_chars"] == 12000, cfg
assert "offload_tool_results" not in cfg, "offloading is per-agent now, not a global toggle"
src = open(os.path.join(out, "agent.py"), encoding="utf-8").read()
assert '_offload_large(spec.get("offload_results")' in src, "not wired per-agent"
print("ok 1: per-agent wiring + shared threshold in config")

spec = importlib.util.spec_from_file_location("vof_agent", os.path.join(out, "agent.py"))
mod = importlib.util.module_from_spec(spec)
sys.path.insert(0, out); os.chdir(out)
spec.loader.exec_module(mod)
os.chdir(BASE)

assert mod.AGENTS["A"].get("offload_results") is True, mod.AGENTS["A"]
assert "read_offload" in mod.AGENTS["A"]["tools"], mod.AGENTS["A"]["tools"]
assert "## Large results" in mod.AGENTS["A"]["system"], "prompt tail missing"
print("ok 2: agent gets offload flag + read_offload tool + prompt tail")

big = "\n".join(f"line {i}: " + "y" * 50 for i in range(300))   # ~17k chars, >12000
assert len(big) > 12000

# 3. pass-through: disabled / read_offload's own output / framework markers / small.
assert mod._offload_large(False, "t", big) == big, "disabled must pass through"
assert mod._offload_large(True, "read_offload", big) == big, "no re-offload loop"
assert mod._offload_large(True, "t", "[ERROR] boom " + "x" * 20000).startswith("[ERROR]")
assert mod._offload_large(True, "t", "short") == "short"

# 4. crash-proof + clamped threshold (review #1 + #4): a bad/negative value can't
#    crash the turn or turn every result into an offload.
mod.set_workspace([])
mod.CONFIG["offload_threshold_chars"] = "not a number"
r = mod._offload_large(True, "t", big)
assert isinstance(r, str) and "omitted" in r, ("bad threshold must not crash", r[:80])
mod.CONFIG["offload_threshold_chars"] = -5
assert mod._offload_large(True, "t", "tiny result") == "tiny result", "clamp: tiny stays"
mod.CONFIG["offload_threshold_chars"] = 200

# 5. large + NO workspace -> head+tail truncation with an honest note (review #5).
r = mod._offload_large(True, "bigtool", big)
assert "middle omitted" in r and "beginning+end" in r and len(r) < len(big), r
print("ok 3: pass-through cases + crash-proof/clamped threshold + no-workspace head+tail")

# 6. large + workspace -> offloaded to a file, RELATIVE pointer (docker-safe), lossless.
ws = tempfile.mkdtemp(prefix="ma_ws_")
try:
    mod.set_workspace([ws])
    r2 = mod._offload_large(True, "bigtool", big)
    assert r2.startswith("[offloaded:") and "relative to your workspace" in r2, r2
    assert "offloaded/" in r2 and ws not in r2, ("pointer must be workspace-relative", r2)
    files = glob.glob(os.path.join(ws, "offloaded", "bigtool_*.txt"))
    assert files, "offload file not written"
    assert len(os.path.basename(files[0]).split("_")[-1]) == 20, "hash is 16 hex + .txt"
    assert open(files[0], encoding="utf-8").read() == big, "offload must be lossless"
    rel = "offloaded/" + os.path.basename(files[0])
    # read_offload round-trip (review #2/#3): the agent can fetch the full text back.
    assert mod._read_offload({"path": rel}) == big, "read_offload round-trip failed"
    assert mod._read_offload({"path": rel.replace("/", chr(92))}) == big, "backslash path"
    # path guard: only offloaded/*.txt basenames; no traversal / arbitrary reads.
    assert mod._read_offload({"path": "../../secret"}).startswith("[ERROR]")
    assert mod._read_offload({"path": "offloaded/nope.txt"}).startswith("[ERROR] no offloaded")
finally:
    mod.set_workspace([])
    shutil.rmtree(ws, ignore_errors=True)
print("ok 4: offloaded lossless + relative pointer + read_offload round-trip + path guard")

print("ALL OFFLOAD CHECKS PASSED")
