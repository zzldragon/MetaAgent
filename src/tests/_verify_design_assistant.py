"""Verify the design-assistant foundations (design_assistant.py): the knowledge
blob is DERIVED from the live registries (so it can't drift), the rendered
prompt surfaces every kind/role/pattern, and graph_metrics/design_review report
sensible deterministic stats. This is the fail-fast parity guard: add a node
kind / role / pattern to the code and this test proves the assistant sees it.
Offline, no LLM."""

import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import graph_codegen as gc  # noqa: E402
import graph_model as gm  # noqa: E402
import patterns as pat  # noqa: E402
import design_assistant as da  # noqa: E402

LLM = {"provider": "siliconflow", "model": "deepseek-ai/DeepSeek-V4-Flash",
       "api_key": "sk-x", "base_url": "https://api.siliconflow.cn/v1"}


# 0. Knowledge is DERIVED, not hand-written: it must cover exactly the live
#    registries (no drift possible, and no accidental hardcoded subset).
k = da.design_knowledge()
assert set(k["node_kinds"]) == set(gm.KIND_META), (
    set(gm.KIND_META) ^ set(k["node_kinds"]))
assert set(k["roles"]) == set(gc.ROLE_DEFAULT_PROMPTS), (
    set(gc.ROLE_DEFAULT_PROMPTS) ^ set(k["roles"]))
assert set(k["patterns"]) == set(pat.PATTERNS), (
    set(pat.PATTERNS) ^ set(k["patterns"]))
assert list(k["state_types"]) == list(gm.STATE_TYPES)
assert k["default_budgets"] == dict(gm.DEFAULT_BUDGETS)
# every kind is classified into a known group AND carries a curated description
for kind, meta in k["node_kinds"].items():
    assert meta["group"] in ("agent", "control", "flow", "resource"), (kind, meta)
    assert meta["desc"], f"kind {kind} has no KIND_DESC description"
# curated semantics must cover EVERY registry entry (no drift when a kind/role is added)
assert set(da.KIND_DESC) == set(gm.KIND_META), set(da.KIND_DESC) ^ set(gm.KIND_META)
assert set(da.ROLE_SEMANTICS) == set(gc.ROLE_DEFAULT_PROMPTS), (
    set(da.ROLE_SEMANTICS) ^ set(gc.ROLE_DEFAULT_PROMPTS))
assert all(k["role_semantics"].values()) and k["core_design"] and k["rules"]
# patterns carry their concrete topology (agents/links) where the preset defines it
assert k["patterns"]["supervisor_worker"]["agents"], "pattern topology missing"
print("0. knowledge blob mirrors registries + carries curated semantics (no drift)")

# 1. The rendered prompt actually SURFACES every kind, role, and pattern — so a
#    newly added one reaches the assistant's context, not just the dict.
md = da.knowledge_prompt()
for kind in gm.KIND_META:
    assert f"`{kind}`" in md, f"kind {kind} missing from knowledge_prompt()"
for role in gc.ROLE_DEFAULT_PROMPTS:
    assert f"`{role}`" in md, f"role {role} missing from knowledge_prompt()"
for pid in pat.PATTERNS:
    assert f"`{pid}`" in md, f"pattern {pid} missing from knowledge_prompt()"
# the rich core-design sections are present
for section in ("## Core design", "## Node kinds", "## Agent roles (runtime behaviour)",
                "## Edges (what a link means)", "## Validity rules", "## Shared state",
                "## Patterns (starting topologies)", "## Link-kind reference"):
    assert section in md, f"missing section: {section}"
# per-kind and per-role semantics actually render (not just the names)
assert gm.KIND_META["condition"]["label"] in md
assert "If/Else" in md and "spawn_subagent" in md and "REVISE" in md
assert "supervisor→worker" in md or "supervisor(supervisor)" in md  # pattern topology
print("1. knowledge_prompt() surfaces core design + every kind/role/pattern + rules")

# 2. graph_metrics + design_review on a real supervisor graph.
g = pat.build_pattern_graph("supervisor_worker", LLM)
review = da.design_review(g)
assert review["errors"] == [], review["errors"]
assert review["topology"]["mode"] == "supervisor", review["topology"]
m = review["metrics"]
assert m["agent_count"] == 2, m
assert m["min_agent_invocations"] >= 1, m
# budgets present per agent and summed
assert set(m["budgets"]["totals"]) == set(gm.DEFAULT_BUDGETS), m["budgets"]
assert all(km in m["budgets"]["per_agent"] for km in ("supervisor", "worker")), m
print("2. design_review(supervisor) — mode/agents/budgets ok, no errors")

# 3. Orchestrator graph → parallelism.orchestrator flagged, mode autonomous.
g2 = pat.build_pattern_graph("orchestrator", LLM)
m2 = da.graph_metrics(g2)
assert m2["mode"] == "autonomous", m2["mode"]
assert m2["parallelism"]["orchestrator"] is True, m2["parallelism"]
print("3. graph_metrics(orchestrator) — autonomous + orchestrator parallelism flagged")

# 4. Redundancy detection: two identical LLMs on one agent surface as a
#    redundancy warning (metrics reuse analyze()'s warnings, so this stays
#    in-sync with the linter).
g3 = pat.build_pattern_graph("react", LLM)
agent = g3.agents()[0]
llm2 = g3.new_node("llm", 0, 0)
llm2.props.update(LLM)
assert g3.add_edge(llm2.id, agent.id) is None
m3 = da.graph_metrics(g3)
assert m3["redundancy_warnings"], "duplicate identical LLM should be flagged"
print("4. graph_metrics flags a redundant duplicate LLM (via analyze warnings)")

# 5. Empty graph is handled gracefully (analyze early-returns a partial dict).
empty = gm.Graph()
r = da.design_review(empty)
assert r["errors"], "empty graph should report an error"
assert r["metrics"]["agent_count"] == 0, r["metrics"]
print("5. empty graph handled (no crash on analyze's partial return)")

print("\nALL DESIGN-ASSISTANT CHECKS PASSED")
