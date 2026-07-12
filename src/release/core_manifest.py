"""Single source of truth for MetaAgent's open-core split.

Classifies top-level modules so the protected build (release/protect_core.py) and
any future packaging step agree on exactly what is hidden vs shipped as source.

Three buckets:
  CORE     -- the codegen engine + AI-designer logic. It only ever RUNS inside the
              designer and never appears in generated products, so it is safe to
              compile to a .pyd (Nuitka) and ship WITHOUT source. This is the IP
              worth protecting.
  INLINED  -- its source TEXT is inlined verbatim into every generated agent (the
              product's defining "zero-dependency standalone Python" feature), so
              it is impossible to hide and must stay as plain .py. Compiling it
              would also break its __file__-based reads.
  OPEN     -- the public shell / infra (UI, launcher, tool library, runtime
              fragments). Intended to be open-sourced as-is; not listed here.
"""

# --- hide these (compile to .pyd, drop the .py) ---
CORE = [
    "graph_model",       # data model, node registry, save_mta/load_mta
    "graph_codegen",     # analyze() validation + generate_from_graph() emission
    "codegen",           # tool inlining, requirements, build.bat
    "patterns",          # the pattern library
    "estimation",        # cost/latency estimation
    "designer_agent",    # AI designer agent logic
    "design_assistant",  # AI design assistant logic
    "coding_agent",      # tool-generator coding agent logic
]

# --- never hideable: source text ends up in the generated product ---
INLINED = [
    "graph_codegen_templates",
    "codegen_templates",
    # NOTE: runtime/*.py fragments are loaded as TEXT by runtime_source.block()
    # and inlined too -- keep the whole runtime/ directory as .py as well.
]


def core_files(root):
    """Absolute paths of the CORE .py modules under `root` that currently exist."""
    import os
    out = []
    for name in CORE:
        p = os.path.join(root, name + ".py")
        if os.path.isfile(p):
            out.append(p)
    return out


def summary():
    return (
        "MetaAgent open-core split\n"
        "  CORE (compile -> .pyd, hide source):\n    "
        + "\n    ".join(CORE)
        + "\n  INLINED (cannot hide, keep .py):\n    "
        + "\n    ".join(INLINED)
        + "\n  OPEN: everything else (UI shell, launcher, tools/, runtime/ fragments)."
    )


if __name__ == "__main__":
    print(summary())
