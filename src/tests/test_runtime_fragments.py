"""The agent runtime now lives as real .py files under runtime/ (see
runtime_source.py). Guard that every fragment is syntactically valid Python and
is actually wired into a generator -- a check that did not exist while the
runtime was buried in escaped string literals."""

import os

import pytest

import codegen_templates as ct
import graph_codegen_templates as gt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNTIME = os.path.join(ROOT, "runtime")

FRAGMENTS = sorted(f for f in os.listdir(RUNTIME) if f.endswith(".py"))

# constant <-> file wiring that the generators rely on
WIRING = {
    "storage.py": ct.STORAGE_CODE,
    "workspace.py": ct.WORKSPACE_CODE,
    "trace.py": ct.TRACE_CODE,
    "skills.py": ct.SKILLS_CODE,
    "eval.py": ct.EVAL_CODE,
    "image.py": ct.IMAGE_CODE,
    "rag.py": ct.RAG_CODE,
    "memory.py": ct.MEMORY_CODE,
    "hitl.py": ct.HITL_CODE,
    "history.py": ct.HISTORY_CODE,
    "checkpoint.py": ct.CHECKPOINT_CODE,
    "guardrails.py": ct.GUARDRAILS_CODE,
    "pool.py": ct.POOL_CODE,
    "mcp_stub.py": gt.MCP_STUB,
    "mcp.py": gt.MCP_CODE,
}


def test_all_fragments_are_wired():
    assert set(FRAGMENTS) == set(WIRING), set(FRAGMENTS) ^ set(WIRING)


@pytest.mark.parametrize("fragment", FRAGMENTS)
def test_fragment_compiles(fragment):
    src = open(os.path.join(RUNTIME, fragment), encoding="utf-8").read()
    compile(src, f"runtime/{fragment}", "exec")


@pytest.mark.parametrize("fragment", FRAGMENTS)
def test_fragment_matches_loaded_constant(fragment):
    """What runtime_source loads must equal the file on disk (no drift)."""
    disk = open(os.path.join(RUNTIME, fragment), encoding="utf-8").read()
    assert disk.strip() == WIRING[fragment].strip()
