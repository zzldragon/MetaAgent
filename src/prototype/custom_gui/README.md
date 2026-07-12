# Prototype: load a custom GUI into the GUI node (option 1)

Today the GUI node is a **toggle** — linking it to the entry agent makes
generation emit the one standard PySide6 chat window (`codegen_templates.GUI_TEMPLATE`).
This prototype shows how the node could instead carry a **hand-designed `gui.py`**
that generation emits in its place — *"design a GUI, load it into the node, link
it to the agent."*

It is self-contained and does **not** modify any production code.

## How it works

1. **Load** — `proto.load_custom_gui(gui_node, path)` reads your `gui.py` and
   **inlines its source onto `gui_node.props["custom_gui"]`**. Because it's just
   a props value, the design travels with the graph JSON and the `.mta` bundle —
   no external file to lose.
2. **Generate** — `proto.generate_with_custom_gui(graph, name)` runs the real
   `graph_codegen.generate_from_graph` (the linked GUI node already makes it emit
   the standard `gui.py` plus the correct `requirements.txt`/`build.bat`), then
   **overwrites `gui.py`** with the node's custom source (substituting
   `@AGENT_NAME@`, like the standard template).
3. **Contract** — the custom `gui.py` drives the agent only through the generated
   `core` (agent.py) API. That's the one rule. See **CONTRACT.md**.

## Files

| File | What |
|---|---|
| `proto.py` | `load_custom_gui()` + `generate_with_custom_gui()` — the mechanism. |
| `example_custom_gui.py` | A minimal hand-designed GUI (the thing you "design and load"). |
| `CONTRACT.md` | The `core` API a custom GUI may call (the agent-wiring contract). |
| `demo.py` | Runnable end-to-end demo with assertions. |

## Run it

**Headless proof (no API key, no window):**

```
python prototype/custom_gui/demo.py
```

The demo loads `example_custom_gui.py` into a GUI node, generates the agent, and
verifies the generated `gui.py` is the custom one (not `ChatFrame`), that
`@AGENT_NAME@` was substituted, that it byte-compiles, that it **constructs
offscreen against the real generated `agent.py`**, that the **Model menu actually
switches the LLM** the next run uses, and that the design survives a `.mta`
save/load round-trip.

**See the real window:**

```
python prototype/custom_gui/run_example.py
```

This generates a demo agent with the custom GUI into `generated_agents/custom_gui_example/`
and launches its real `gui.py`. The window + Model menu work with no key; to chat,
set a SiliconFlow key in the main app's Settings first (it's reused) or paste one
into the generated agent's `config.json`.

> **Note:** `example_custom_gui.py` is a template — you can't run it directly
> (`import agent as core` needs the generated `agent.py` that lives next to
> `gui.py` inside a generated agent folder). Use `run_example.py`, which creates
> that folder for you.

## Graduating this into production

Two small, localized changes (sketch — not applied here):

**1. `canvas_qt/dialogs.py` — `GUIDialog` gets a "Choose file…" button** that
reads a `.py` and stores it on the node:

```python
def _choose(self):
    path, _ = QFileDialog.getOpenFileName(self, "Choose a custom gui.py", "",
                                          "Python (*.py)")
    if path:
        with open(path, encoding="utf-8") as f:
            self.node.props["custom_gui"] = f.read()
        self._status.setText(f"Loaded {os.path.basename(path)} "
                             f"({len(self.node.props['custom_gui'])} chars)")
# + a "Clear" button that pops props["custom_gui"], and a label showing the state.
```

**2. `codegen.write_gui()` — emit the custom source when present:**

```python
def write_gui(out_dir, name, custom_src=None):
    src = custom_src if custom_src else GUI_TEMPLATE
    _write(out_dir, "gui.py", src.replace("@AGENT_NAME@", name))
```

…and in `graph_codegen.generate_from_graph`, when `gui` is on, pass the entry
GUI node's `props.get("custom_gui")` through to `write_gui`.

That's the whole feature. Everything else (requirements, build.bat, the gui flag
derivation, `.mta` persistence) already works because the GUI node and its props
are first-class.

## Honest caveats

- **The contract is on the user.** A loaded `gui.py` that ignores the `core` API
  is just an inert window. Ship `CONTRACT.md` + `example_custom_gui.py` as the
  starting point, and keep the standard `GUI_TEMPLATE` as the reference for the
  full feature surface (menus, HITL, vision, evals, …).
- **No validation/sandboxing.** Production should at least byte-compile the
  loaded source and warn if it never imports `agent`/`core` or never calls
  `core.run`. (The demo byte-compiles; it does not sandbox.)
- This prototype overwrites the standard `gui.py` after generating it (simple and
  keeps requirements/build correct). The production `write_gui` branch above
  avoids writing the standard one first.
