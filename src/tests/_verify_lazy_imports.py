"""Verify the heavy SDKs stay OFF the cheap import paths, so opening the Tool
Generator / a project is fast. Importing the coding agent + LLM client must not
drag in the OpenAI SDK (~3s) — it loads lazily on first client construction and
is pre-warmed in the background at launch. langchain must never load at all
(it's only a style convention in tool files, stripped from generated agents)."""

import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import coding_agent   # noqa: F401  — the Tool Generator's module
import llm_client      # noqa: F401  — the LLM client module

assert "openai" not in sys.modules, \
    "the OpenAI SDK must import lazily (in LLMClient.__init__), not at module load"
assert "langchain_core" not in sys.modules, \
    "the app must never import langchain (it's stripped from generated agents)"
print("ok: import coding_agent + llm_client pulls in neither openai nor langchain")

# Constructing the client is where the lazy import fires (no network call — the
# OpenAI constructor only stores config).
from llm_client import LLMClient
LLMClient(api_key="x", base_url="http://localhost:0", model="m")
assert "openai" in sys.modules, "constructing LLMClient should load the OpenAI SDK"
print("ok: the OpenAI SDK loads on first LLMClient construction")

# The background pre-warm helper must exist and be safe to call (it spawns a
# daemon thread of best-effort imports).
from canvas_qt import welcome
assert callable(welcome._prewarm_heavy_imports)
welcome._prewarm_heavy_imports()
print("ok: welcome._prewarm_heavy_imports() is callable")

print("\nALL LAZY-IMPORT CHECKS PASSED")
