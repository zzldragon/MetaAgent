"""Verify the aggregated Skills node + Cursor/Claude-style progressive
disclosure: model/helper, config['skills'], and the generated agent's
progressive prompt (names+descriptions only), the load_skill tool (model-
invoked, with user-only gating), /<name> user-invocation, and runtime
add/update/remove — all offline."""

import importlib.util
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import graph_codegen
from graph_model import Graph, Node, default_props, skill_items

# ── 1. model: default props + skill_items carries description + manual flag ──
assert default_props("skill") == {"skills": []}
multi = Node(id="s", kind="skill", name="guides", x=0, y=0, props={"skills": [
    {"name": "tone", "description": "Set a concise tone.", "text": "Be terse."},
    {"name": "cite", "text": "Cite sources after each claim."},   # no description
    {"name": "blank", "text": "  "}]})                            # blank dropped
items = skill_items(multi)
assert [s["name"] for s in items] == ["tone", "cite"], items
assert items[0]["description"] == "Set a concise tone."
assert items[0]["disable_model_invocation"] is False
assert items[1]["description"] == ""           # carried through (empty)
# legacy single-text fallback unchanged
legacy = Node(id="s2", kind="skill", name="oldskill", x=0, y=0,
              props={"text": "Stay on topic."})
assert skill_items(legacy) == [{"name": "oldskill", "text": "Stay on topic."}]
print("ok 1: skill_items carries description + disable_model_invocation; legacy ok")

# ── 2. generate: progressive disclosure — descriptions in prompt, not bodies ─
g = Graph()
a = g.new_node("agent", 0, 0); a.name = "worker"
llm = g.new_node("llm", 0, 0)
llm.props.update(api_key="sk", model="deepseek-ai/DeepSeek-V4-Flash")
g.add_edge(llm.id, a.id)
sk = g.new_node("skill", 0, 0); sk.name = "guides"
sk.props["skills"] = [
    {"name": "summary-style", "description": "Write an executive summary.",
     "text": "STEP 1: lead with the conclusion.\nSTEP 2: at most three bullets."},
    {"name": "wipe-cache", "description": "Delete cached data.",
     "text": "Run: rm -rf cache/.", "disable_model_invocation": True}]
g.add_edge(sk.id, a.id)
out = graph_codegen.generate_from_graph(g, "demo_skills", gui=False)
cfg = json.load(open(os.path.join(out, "config.json"), encoding="utf-8"))
assert cfg["skills"]["worker"][0]["description"] == "Write an executive summary."
assert cfg["skills"]["worker"][1]["disable_model_invocation"] is True
print("ok 2: skills (with description + flag) emitted to config['skills'][agent]")

spec = importlib.util.spec_from_file_location(
    "demo_skills_agent", os.path.join(out, "agent.py"))
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)

sysp = mod.build_system("worker")
# Layer 1 (description) is in the prompt; Layer 2 (body) is NOT
assert "Write an executive summary." in sysp
assert "summary-style" in sysp and "load_skill" in sysp
assert "lead with the conclusion" not in sysp, "skill BODY must not be in the prompt"
# the user-only skill is listed by name only — its description/body stay out
assert "wipe-cache" in sysp
assert "Delete cached data." not in sysp and "rm -rf cache" not in sysp
# the load_skill tool is registered and on the skill-bearing agent
assert "load_skill" in mod.TOOLS
assert "load_skill" in mod.AGENTS["worker"]["tools"]
# persona itself stays de-baked (skills never lived in the base system text)
assert "lead with the conclusion" not in mod.AGENTS["worker"]["system"]
assert mod.list_skill_agents() == ["worker"]
print("ok 3: progressive disclosure — descriptions in prompt, bodies on demand")

# ── 4. model-invoked: load_skill loads the body; user-only is gated ─────────
body = mod.TOOLS["load_skill"]("summary-style")
assert "lead with the conclusion" in body and "three bullets" in body
gated = mod.TOOLS["load_skill"]("wipe-cache")
assert gated.startswith("[skill 'wipe-cache' is user-only")
assert "rm -rf cache" not in gated, "model must not get a user-only skill's body"
assert mod.TOOLS["load_skill"]("nope").startswith("[skill not found")
print("ok 4: load_skill returns body; user-only gated; unknown handled")

# ── 5. user-invoked: /<name> applies the body (even user-only) ──────────────
applied = mod.skill_command("/summary-style do the Q3 report")
assert "lead with the conclusion" in applied and "Q3 report" in applied
applied2 = mod.skill_command("/wipe-cache")          # user-only works via /
assert "rm -rf cache" in applied2
assert mod.skill_command("just a normal message") == "just a normal message"
assert mod.skill_command("/unknownskill x") == "/unknownskill x"   # passthrough
print("ok 5: /<name> applies the body (incl. user-only); passthrough otherwise")

# ── 6. run() applies a / command end-to-end (stubbed LLM, offline) ──────────
captured = {}
def _cap(agent_name, cfg_, system, messages):
    captured["user"] = str(messages[-1].get("content") if messages else "")
    return "done", []
mod._call_one = _cap
mod.clear_history()
mod.run("/summary-style produce the Q3 deck", emit=lambda s: None)
assert "lead with the conclusion" in captured["user"], captured["user"][:200]
assert "Q3 deck" in captured["user"]
mod.clear_history()
print("ok 6: run() routes a /<name> command's skill body to the agent")

# ── 7. runtime add / update / remove persist + carry description ────────────
mod.add_skill("worker", "format", "Use bullet points everywhere.",
              description="Bullet-point formatting.")
sysf = mod.build_system("worker")
assert "Bullet-point formatting." in sysf            # description in prompt
assert "Use bullet points everywhere." not in sysf   # body stays out
assert "Use bullet points" in mod.TOOLS["load_skill"]("format")   # body via load
assert os.path.exists(os.path.join(out, "skills.json"))
mod.update_skill("worker", 0, "summary-style", "STEP 1: TL;DR first.",
                 description="Updated summary style.")
assert "Updated summary style." in mod.build_system("worker")
assert "Write an executive summary." not in mod.build_system("worker")
mod.remove_skill("worker", 0)
saved = json.load(open(os.path.join(out, "skills.json"), encoding="utf-8"))
assert [s["name"] for s in saved["worker"]] == ["wipe-cache", "format"], saved
print("ok 7: add / update / remove persist to skills.json with descriptions")

# ── 8. an agent with no Skills node: no config['skills'], no load_skill tool ─
g2 = Graph()
a2 = g2.new_node("agent", 0, 0); a2.name = "solo"
l2 = g2.new_node("llm", 0, 0); l2.props.update(api_key="sk", model="m")
g2.add_edge(l2.id, a2.id)
out2 = graph_codegen.generate_from_graph(g2, "demo_no_skills", gui=False)
cfg2 = json.load(open(os.path.join(out2, "config.json"), encoding="utf-8"))
assert "skills" not in cfg2, "no Skills node → no skills key → no GUI menu"
spec2 = importlib.util.spec_from_file_location(
    "demo_no_skills_agent", os.path.join(out2, "agent.py"))
mod2 = importlib.util.module_from_spec(spec2); spec2.loader.exec_module(mod2)
assert "load_skill" not in mod2.AGENTS["solo"]["tools"], "no skills → no load_skill tool"
print("ok 8: no Skills node → no config['skills'], no load_skill on the agent")

# ── 9. linking an (even EMPTY) Skills node surfaces the Manage Skills menu ───
g3 = Graph()
a3 = g3.new_node("agent", 0, 0); a3.name = "helper"
l3 = g3.new_node("llm", 0, 0); l3.props.update(api_key="sk", model="m")
g3.add_edge(l3.id, a3.id)
sk3 = g3.new_node("skill", 0, 0); sk3.name = "guides"     # linked but empty
g3.add_edge(sk3.id, a3.id)
out3 = graph_codegen.generate_from_graph(g3, "demo_skills_empty", gui=True)
cfg3 = json.load(open(os.path.join(out3, "config.json"), encoding="utf-8"))
assert cfg3.get("skills") == {"helper": []}, cfg3.get("skills")
spec3 = importlib.util.spec_from_file_location(
    "demo_skills_empty_agent", os.path.join(out3, "agent.py"))
mod3 = importlib.util.module_from_spec(spec3); spec3.loader.exec_module(mod3)
assert "load_skill" in mod3.AGENTS["helper"]["tools"], "load_skill present for runtime use"
assert mod3.list_skill_agents() == ["helper"]
# the generated GUI shows the Manage Skills menu (so skills are addable at runtime)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtGui import QAction  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
sys.path.insert(0, out3)
import gui as _gui3  # noqa: E402  (generated gui.py imports its own agent.py)
app = QApplication.instance() or QApplication([])
frame3 = _gui3.ChatFrame()
labels3 = [a.text().replace("&", "") for a in frame3.findChildren(QAction)]
assert any("Manage Skills" in l for l in labels3), labels3
frame3.close()
print("ok 9: linking even an empty Skills node → Manage Skills menu + load_skill")

# ── 10. SKILL.md import: parse frontmatter (folded desc + flag) + body ──────
SAMPLE_MD = (
    "---\n"
    "name: changelog-writer\n"
    "description: >-\n"
    "  Generate CHANGELOG entries from git commits. Use when the user asks for a\n"
    "  changelog or release notes.\n"
    "disable-model-invocation: true\n"
    "---\n\n"
    "# Write Changelog\n\n## Workflow\n1. Read git log.\n2. Group by type.\n")
p = mod.parse_skill_md(SAMPLE_MD)
assert p["name"] == "changelog-writer", p
assert p["description"].startswith("Generate CHANGELOG entries")
assert "release notes." in p["description"], "folded scalar joined to one line"
assert p["disable_model_invocation"] is True
assert p["text"].startswith("# Write Changelog") and "Group by type." in p["text"]
p2 = mod.parse_skill_md("plain body, no frontmatter")
assert p2["name"] == "" and p2["text"] == "plain body, no frontmatter"
print("ok 10: parse_skill_md reads frontmatter (folded desc + flag) + body")

# ── 11. workspace auto-discovery of <ws>/.cursor/skills/<name>/SKILL.md ──────
import shutil  # noqa: E402
WS = os.path.join(BASE, "_ws_skills_test")
skdir = os.path.join(WS, ".cursor", "skills", "deploy-helper")
os.makedirs(skdir, exist_ok=True)
with open(os.path.join(skdir, "SKILL.md"), "w", encoding="utf-8") as f:
    f.write("---\nname: deploy-helper\n"
            "description: Deploy the app to staging.\n---\n\n"
            "# Deploy Helper\n\nRun the deploy script then verify health.\n")
mod.set_workspace([WS])
mod.refresh_workspace_skills()
assert "deploy-helper" in [s["name"] for s in mod._rs().ws_skills], mod._rs().ws_skills
entry = mod.ENTRY
sysw = mod.build_system(entry)
assert "deploy-helper" in sysw and "Deploy the app to staging." in sysw
assert "Run the deploy script" not in sysw, "workspace skill BODY stays out of prompt"
assert "load_skill" in mod.AGENTS[entry]["tools"], "entry agent gets load_skill"
assert "Run the deploy script" in mod.TOOLS["load_skill"]("deploy-helper")
assert "Run the deploy script" in mod.skill_command("/deploy-helper go")
mod.set_workspace([])
mod._rs().ws_skills[:] = []
shutil.rmtree(WS, ignore_errors=True)
print("ok 11: workspace .cursor/skills/<name>/SKILL.md auto-discovered + usable")

print("\nALL SKILLS CHECKS PASSED")
