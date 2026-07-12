"""Generate a demo agent WITH the example custom GUI loaded, then launch its
REAL window — so you can actually see/use the custom GUI.

Why you can't just run example_custom_gui.py directly: it does
`import agent as core`, which resolves to the generated agent.py that sits NEXT
TO gui.py inside a generated agent folder. example_custom_gui.py is a template
(it even has @AGENT_NAME@ placeholders); there is no agent.py beside it until an
agent is generated. This script creates that folder and launches it.

    python prototype/custom_gui/run_example.py

The window opens and the Model menu works with no API key. To actually chat,
set a SiliconFlow key in Settings of the main app first (this reuses it), or
paste one into the generated agent's config.json.
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import proto  # noqa: E402  (puts the project root on sys.path)
from graph_model import Graph  # noqa: E402

# Reuse the host's configured SiliconFlow key if present, so the agent can chat.
try:
    from app_config import load_config
    _key = load_config().get("deepseek_api_key", "")
except Exception:  # noqa: BLE001
    _key = ""


def main():
    g = Graph()
    a = g.new_node("agent", 0, 0); a.name = "assistant"
    base = dict(provider="siliconflow", api_key=_key,
                base_url="https://api.siliconflow.cn/v1")
    for model in ("deepseek-ai/DeepSeek-V4", "deepseek-ai/DeepSeek-V4-Flash"):
        llm = g.new_node("llm", 0, 0); llm.props.update(model=model, **base)
        g.add_edge(llm.id, a.id)                   # 2 LLMs -> the Model menu appears
    gui = g.new_node("gui", 0, 0)
    proto.load_custom_gui(gui, os.path.join(HERE, "example_custom_gui.py"))
    g.add_edge(gui.id, a.id)                       # GUI node -> entry agent

    out = proto.generate_with_custom_gui(g, "custom_gui_example")
    print(f"generated agent (with the custom GUI) at:\n  {out}")
    print(f"  agent.py is right next to gui.py  ->  {os.path.join(out, 'agent.py')}")
    if not _key:
        print("  (no API key found — the window opens and the Model menu works, "
              "but Send needs a key in the agent's config.json)")
    print("launching gui.py …  close the window to exit.")
    subprocess.run([sys.executable, "gui.py"], cwd=out)
    print(f"\nThe folder stays at {out} — relaunch with "
          f"`python {os.path.join(out, 'gui.py')}` or delete it.")


if __name__ == "__main__":
    main()
