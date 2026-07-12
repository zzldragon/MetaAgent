"""Runnable demo of the custom-GUI prototype (option 1).

  1. build a tiny graph (agent + llm + GUI node),
  2. LOAD example_custom_gui.py into the GUI node and link it to the agent,
  3. generate the agent — the custom gui.py replaces the standard chat window,
  4. verify: it's the custom GUI (not ChatFrame), @AGENT_NAME@ was substituted,
     it byte-compiles, and it actually constructs offscreen against the real
     generated agent.py,
  5. show the design survives a .mta save/load round-trip.

Run:  python prototype/custom_gui/demo.py     (no API key needed — nothing calls an LLM)
"""

import os
import py_compile
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import proto  # noqa: E402  (inserts the project root on sys.path)
from graph_model import Graph, load_mta, save_mta  # noqa: E402

EXAMPLE = os.path.join(HERE, "example_custom_gui.py")
NAME = "custom_gui_demo"


def _build_graph():
    g = Graph()
    a = g.new_node("agent", 0, 0); a.name = "assistant"
    base = dict(provider="siliconflow", api_key="sk-test",
                base_url="https://api.siliconflow.cn/v1")
    llm = g.new_node("llm", 0, 0)
    llm.props.update(model="deepseek-ai/DeepSeek-V4", **base)
    g.add_edge(llm.id, a.id)
    llm2 = g.new_node("llm", 0, 0)                  # 2 LLMs -> switchable -> Model menu
    llm2.props.update(model="deepseek-ai/DeepSeek-V4-Flash", **base)
    g.add_edge(llm2.id, a.id)
    gui = g.new_node("gui", 0, 0); gui.name = "my_gui"
    g.add_edge(gui.id, a.id)                        # GUI node -> entry agent
    return g, gui


def main():
    g, gui = _build_graph()

    # 2. load the hand-designed GUI into the node (inlined onto props)
    proto.load_custom_gui(gui, EXAMPLE)
    assert gui.props.get(proto.CUSTOM_GUI_KEY), "custom GUI not stored on the node"
    print("loaded example_custom_gui.py into the GUI node "
          f"({len(gui.props[proto.CUSTOM_GUI_KEY])} chars)")

    # 3. generate — custom gui.py replaces the standard one
    out = proto.generate_with_custom_gui(g, NAME)
    try:
        src = open(os.path.join(out, "gui.py"), encoding="utf-8").read()

        # 4. verify
        assert "class CustomGui" in src and "class ChatFrame" not in src, \
            "generated gui.py is not the custom one"
        assert "@AGENT_NAME@" not in src and NAME in src, \
            "@AGENT_NAME@ was not substituted with the app name"
        reqs = open(os.path.join(out, "requirements.txt"), encoding="utf-8").read()
        assert "PySide6" in reqs, reqs               # GUI node still drove the deps
        print("custom gui.py emitted (CustomGui, not ChatFrame); PySide6 in requirements")

        py_compile.compile(os.path.join(out, "gui.py"), doraise=True)
        print("custom gui.py byte-compiles")

        # constructs offscreen against the REAL generated agent.py (clean
        # process) AND the Model menu actually switches the LLM the next run uses
        check = os.path.join(out, "_construct_check.py")
        with open(check, "w", encoding="utf-8") as f:
            f.write(
                "import os, sys\n"
                "os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')\n"
                "sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\n"
                "from PySide6.QtWidgets import QApplication\n"
                "import agent as core, gui\n"
                "app = QApplication.instance() or QApplication([])\n"
                "w = gui.CustomGui()\n"
                "assert '@AGENT_NAME@' not in w.windowTitle(), w.windowTitle()\n"
                "assert 'custom front-end' in w.windowTitle(), w.windowTitle()\n"
                "# the custom GUI wired a Model menu to the real LLM-switch API\n"
                "menus = [a.text().replace('&','') for a in w.menuBar().actions()]\n"
                "assert 'Model' in menus, menus\n"
                "ag = 'assistant'\n"
                "assert len(core.get_llm_options(ag)) == 2, core.get_llm_options(ag)\n"
                "assert core.get_llm_choice(ag) == 0\n"
                "w._switch_llm(ag, 1)                    # what the menu action does\n"
                "assert core.get_llm_choice(ag) == 1, core.get_llm_choice(ag)\n"
                "import os as _os\n"
                "assert _os.path.exists('llm_choice.json'), 'choice not persisted'\n"
                "# prove the NEXT run tries the selected model first\n"
                "used = []\n"
                "core._call_one = lambda an, cfg, system, messages: "
                "(used.append(cfg['model']), ('ok', []))[1]\n"
                "core.llm(ag, 'sys', [], emit=lambda s: None)\n"
                "assert used[0] == core.get_llm_options(ag)[1], used\n"
                "print('LLM_SWITCH_OK: next run uses', used[0])\n"
                "print('CONSTRUCT_OK:', w.windowTitle())\n")
        env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
        r = subprocess.run([sys.executable, "_construct_check.py"], cwd=out,
                           capture_output=True, text=True, env=env, timeout=60)
        assert "CONSTRUCT_OK" in r.stdout and "LLM_SWITCH_OK" in r.stdout, (r.stdout, r.stderr)
        print("custom gui.py constructs + Model menu switches the LLM:")
        for ln in r.stdout.strip().splitlines()[-2:]:
            print("   ", ln)
    finally:
        shutil.rmtree(out, ignore_errors=True)

    # 5. the inlined design survives a .mta save/load round-trip
    td = tempfile.mkdtemp()
    bundle = os.path.join(td, "g.mta")
    save_mta(g, bundle, tempfile.mkdtemp())
    g2, _info = load_mta(bundle, tempfile.mkdtemp())
    gui2 = next(n for n in g2.nodes.values() if n.kind == "gui")
    assert gui2.props.get(proto.CUSTOM_GUI_KEY) == gui.props[proto.CUSTOM_GUI_KEY], \
        "custom GUI lost on .mta round-trip"
    out2 = proto.generate_with_custom_gui(g2, NAME + "_rt")
    try:
        assert "class CustomGui" in open(os.path.join(out2, "gui.py"),
                                         encoding="utf-8").read()
        print("design survives .mta save/load — regenerates the same custom GUI")
    finally:
        shutil.rmtree(out2, ignore_errors=True)
        shutil.rmtree(td, ignore_errors=True)

    print("\nPROTOTYPE DEMO PASSED")


if __name__ == "__main__":
    main()
