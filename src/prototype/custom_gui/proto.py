"""Prototype — "load a custom GUI into the GUI node" (design option 1).

This demonstrates, WITHOUT touching the production codegen, how the GUI node
could carry a hand-designed gui.py that generation emits in place of the
standard PySide6 chat window:

  1. `load_custom_gui(node, path)` reads a hand-written gui.py and INLINES its
     source onto the GUI node's props (so the design travels with the graph and
     its .mta bundle — no external file dependency).
  2. `generate_with_custom_gui(graph, name)` generates the agent normally (the
     real graph_codegen derives gui=True from the linked GUI node and writes the
     standard gui.py + the right requirements/build.bat), then OVERWRITES gui.py
     with the node's custom source (with @AGENT_NAME@ substituted, mirroring the
     template convention).

The one rule a custom gui.py must follow: drive the agent through the generated
`core` (agent.py) API — see CONTRACT.md. A GUI that ignores that API is just an
inert window.

Run the demo:  python prototype/custom_gui/demo.py
"""

from __future__ import annotations

import os
import sys

# Make the MetaAgent project importable (custom_gui -> prototype -> project root)
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import graph_codegen  # noqa: E402

# Where the inlined custom gui.py source lives on the GUI node. In production
# this same key would be written by a "Choose file…" button in GUIDialog and
# round-trips through graph JSON / .mta automatically (it's just a props value).
CUSTOM_GUI_KEY = "custom_gui"


def load_custom_gui(gui_node, path: str) -> None:
    """Inline a hand-written gui.py file onto a GUI node so the design travels
    with the graph (and its .mta bundle). `gui_node.kind` must be "gui"."""
    if gui_node.kind != "gui":
        raise ValueError(f"expected a GUI node, got kind={gui_node.kind!r}")
    with open(path, encoding="utf-8") as f:
        gui_node.props[CUSTOM_GUI_KEY] = f.read()


def entry_gui_node(graph):
    """The GUI node linked to the entry agent, or None — mirrors the production
    derivation in graph_codegen.generate_from_graph."""
    info = graph_codegen.analyze(graph)
    if info["errors"]:
        return None
    entry = info.get("entry")
    gui_ids = {n.id for n in graph.nodes.values() if n.kind == "gui"}
    for e in graph.edges:
        if e.src in gui_ids and e.dst == entry:
            return graph.nodes[e.src]
    return None


def generate_with_custom_gui(graph, name: str) -> str:
    """Generate the agent; if the entry GUI node carries a custom gui.py, write
    that in place of the standard one. Returns the output dir."""
    out_dir = graph_codegen.generate_from_graph(graph, name)  # gui derived from node
    node = entry_gui_node(graph)
    custom = node.props.get(CUSTOM_GUI_KEY) if node else None
    gui_path = os.path.join(out_dir, "gui.py")
    if custom and os.path.exists(gui_path):
        # @AGENT_NAME@ substitution mirrors the standard template's convention.
        with open(gui_path, "w", encoding="utf-8") as f:
            f.write(custom.replace("@AGENT_NAME@", name))
    return out_dir
