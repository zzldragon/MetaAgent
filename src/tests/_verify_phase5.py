"""Verify Phase-5 Extra Settings: custom-GUI-in-GUI-node (single-file). A GUI node
may carry a user-authored gui.py SOURCE (`custom_gui`) that is emitted verbatim in
place of the built-in window (with @AGENT_NAME@ substituted). Blank custom_gui must
stay byte-identical to today. Also covers analyze() validation + .mta round-trip.
Qt-free (codegen + model only)."""

import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import codegen
import graph_codegen as gc
from graph_model import Graph

LLM = dict(provider="siliconflow", model="deepseek-ai/DeepSeek-V4-Flash", api_key="sk",
           base_url="https://api.siliconflow.cn/v1")


def _graph_with_gui(custom=""):
    g = Graph()
    a = g.new_node("agent", 0, 0); a.name = "Main"
    llm = g.new_node("llm", 0, 0); llm.props.update(LLM); g.add_edge(llm.id, a.id)
    gui = g.new_node("gui", -200, 0); gui.name = "GUI"
    if custom:
        gui.props["custom_gui"] = custom
    g.add_edge(gui.id, a.id)
    return g


def _gui_src(out):
    return open(os.path.join(out, "gui.py"), encoding="utf-8").read()


# ── 1. Blank custom_gui → the built-in gui.py, byte-identical to today ────────
out = gc.generate_from_graph(_graph_with_gui(""), "p5_blank")
assert _gui_src(out) == codegen.GUI_TEMPLATE.replace("@AGENT_NAME@", "p5_blank")
print("1. blank custom_gui → built-in gui.py byte-identical ok")

# ── 2. Custom source emitted verbatim (with @AGENT_NAME@ substituted) ─────────
CUSTOM = ("import agent\n\n"
          "def main():\n"
          "    print('@AGENT_NAME@')\n"
          "    print(agent.run('hi'))\n\n"
          "if __name__ == '__main__':\n"
          "    main()\n")
out = gc.generate_from_graph(_graph_with_gui(CUSTOM), "p5_custom")
assert _gui_src(out) == CUSTOM.replace("@AGENT_NAME@", "p5_custom"), _gui_src(out)
assert "GUI_TEMPLATE" not in _gui_src(out)   # sanity: not the built-in window
print("2. custom gui.py emitted verbatim (single) ok")

# ── 3. Custom source also honoured in package code_style ─────────────────────
out = gc.generate_from_graph(_graph_with_gui(CUSTOM), "p5_custom_pkg",
                             code_style="package")
assert _gui_src(out) == CUSTOM.replace("@AGENT_NAME@", "p5_custom_pkg")
print("3. custom gui.py emitted verbatim (package) ok")

# ── 4. analyze(): syntax error blocks, missing import/.run warns ─────────────
info = gc.analyze(_graph_with_gui("def (:\n"))
assert any("syntax error" in e.lower() for e in info["errors"]), info["errors"]

info = gc.analyze(_graph_with_gui("x = 1\n"))          # compiles, but inert
assert not any("syntax error" in e.lower() for e in info["errors"]), info["errors"]
assert any("import agent" in w for w in info["warnings"]), info["warnings"]
assert any(".run(" in w for w in info["warnings"]), info["warnings"]

info = gc.analyze(_graph_with_gui(CUSTOM))             # well-formed → no gui warnings
assert not any("Custom GUI" in w or "custom GUI" in w for w in info["warnings"]), info["warnings"]
print("4. analyze() validation (syntax error / import / .run) ok")

# ── 5. .mta round-trip preserves custom_gui ──────────────────────────────────
g = _graph_with_gui(CUSTOM)
g2 = Graph.from_dict(g.to_dict())
gnode = next(n for n in g2.nodes.values() if n.kind == "gui")
assert gnode.props.get("custom_gui") == CUSTOM, gnode.props
# default_props keeps blank for a fresh node (byte-identical for old graphs)
assert Graph().new_node("gui", 0, 0).props.get("custom_gui", "") == ""
print("5. .mta round-trip + default blank ok")

print("\nALL PHASE-5 CHECKS PASSED")
