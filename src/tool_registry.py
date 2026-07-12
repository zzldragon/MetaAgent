"""Lightweight ``@tool`` registry — a drop-in replacement for
``langchain_core.tools.tool``.

Tools in MetaAgent's library decorate a plain function with ``@tool`` so it can
be discovered by name. This decorator just records the function in ``TOOLS`` and
returns it **unchanged** (so the function stays directly callable and testable).

Why local instead of langchain: MetaAgent generates self-contained, langchain-free
agents. When it inlines a tool's source it strips this import and provides its own
identical ``tool`` stand-in (see graph_codegen_templates.py), so generated agents
keep zero LangChain dependency. Keeping the decorator here — a ~0ms stdlib-only
import — means tool files are importable/testable standalone with no extra deps
(langchain isn't even in requirements.txt).
"""

from __future__ import annotations

# name -> function, populated as tool modules are imported. Mirrors the registry
# the generated runtime builds with its own identical stand-in.
TOOLS: dict = {}


def tool(fn=None, **_kwargs):
    """Register a tool function into ``TOOLS`` (by its ``__name__``) and return it
    unchanged. Usable bare (``@tool``) or called (``@tool(risk="high")``); keyword
    args are accepted for langchain API parity and ignored AT RUNTIME — but note
    ``risk="high"|"safe"`` is read at code-generation time (graph_codegen._tool_risk)
    to set the generated agent's per-tool HITL gating, so declare it on tools with
    side effects."""
    def _register(f):
        TOOLS[f.__name__] = f
        return f
    return _register(fn) if callable(fn) else _register
