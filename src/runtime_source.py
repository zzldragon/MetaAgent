"""Single source of truth for the generated-agent runtime.

The agent runtime (LLM client blocks, workspace, trace, history, HITL, skills,
RAG, image input, eval harness, MCP) lives as REAL Python modules under
``runtime/`` rather than escaped string literals. They are editable, syntax-
highlighted and compile-checked as code; the code generators read them from
disk and inline them verbatim into each standalone agent (see codegen.py /
graph_codegen.py). Both generators ``.strip()`` every block, so leading/trailing
whitespace in these files is irrelevant to the emitted output.
"""

import glob
import os

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runtime")


def block(name: str) -> str:
    """Return the verbatim source text of a runtime/<name> fragment."""
    with open(os.path.join(_DIR, name), encoding="utf-8") as f:
        return f.read()


# Every runtime fragment, loaded once: {filename: verbatim source text}. ONE
# glob-driven map (deterministic sorted order) so the generators alias their
# *_CODE constants off it (RAG_CODE = FRAGMENTS["rag.py"], ...) instead of a
# per-file _rt("x.py") call scattered across two template modules. The FULL
# un-stripped text is stored; both generators .strip() at substitution time.
# test/test_runtime_fragments.py asserts every *.py here is wired to a constant.
FRAGMENTS = {name: block(name)
             for name in sorted(os.path.basename(p)
                                for p in glob.glob(os.path.join(_DIR, "*.py")))}
