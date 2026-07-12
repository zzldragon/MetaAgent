"""Pins the byte-identical contract between gen-time and runtime skill rendering
(refactor-plan Phase 0.3).

graph_codegen._compose_system_prompt renders the "## Available skills" block at
GENERATION time; runtime/skills.py skills_block renders it at RUN time. Both
docstrings say the two MUST stay byte-identical (progressive disclosure: names +
descriptions only). This test turns that review-only promise into a red/green
signal.

The ONE legitimate divergence is excluded by construction: at run time
skills_block augments the ENTRY agent with workspace-discovered _WS_SKILLS and the
prompt also appends workspace_context(). We test the non-entry / no-workspace
path (ENTRY unset, _WS_SKILLS empty), which is exactly what _compose_system_prompt
reproduces.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import graph_codegen as gc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUIDANCE_NOTOOLS = "\n\nYou have no tools; answer directly."


def _load_skills_fragment():
    """Exec runtime/skills.py with the globals the assembled agent provides — it
    is a fragment, NOT standalone-importable (references CONFIG/BASE_DIR/etc)."""
    ns = {"os": os, "json": json, "re": re, "CONFIG": {}, "BASE_DIR": ROOT}
    with open(os.path.join(ROOT, "runtime", "skills.py"), encoding="utf-8") as f:
        exec(compile(f.read(), "runtime/skills.py", "exec"), ns)
    return ns


def _runtime_block(ns, agent, items):
    ns["SKILLS"] = {agent: [dict(s) for s in items]}
    ns["_WS_SKILLS"] = []          # no-workspace path
    ns.pop("ENTRY", None)          # non-entry path
    return ns["skills_block"](agent)


def _gen_block(items):
    composed = gc._compose_system_prompt("", items, [])   # base="", no tools
    assert composed.endswith(GUIDANCE_NOTOOLS), composed
    return composed[:-len(GUIDANCE_NOTOOLS)]


CASES = {
    "none": [],
    "auto_with_desc": [{"name": "summarize", "description": "Summarize a doc"}],
    "auto_desc_from_body": [{"name": "x", "text": "# Title\n\nfirst real line"}],
    "manual_only": [{"name": "deploy", "description": "ship it",
                     "disable_model_invocation": True}],
    "mixed_order_preserved": [{"name": "a", "description": "alpha"},
                              {"name": "z", "description": "zeta",
                               "disable_model_invocation": True},
                              {"name": "m", "description": "mu"}],
    "empty_name_skipped": [{"name": "  ", "description": "ignored"},
                           {"name": "ok", "description": "kept"}],
}


def test_skill_block_parity():
    ns = _load_skills_fragment()
    for label, items in CASES.items():
        gen = _gen_block(items)
        rt = _runtime_block(ns, "writer", items)
        assert gen == rt, (f"[{label}] gen-time vs runtime skill block diverged:"
                           f"\nGEN={gen!r}\n RT={rt!r}")


def test_skill_desc_parity():
    ns = _load_skills_fragment()
    for s in ({"name": "x", "description": "explicit"},
              {"name": "y", "text": "## heading\n\nfirst real line"},
              {"name": "z"}):
        assert gc._skill_desc(s) == ns["_skill_desc"](s), s


if __name__ == "__main__":
    test_skill_block_parity()
    test_skill_desc_parity()
    print("prompt-parity OK")
