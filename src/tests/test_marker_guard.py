"""Guards the @MARKER@ substitution contract (refactor-plan Phase 0.1/0.2).

Generated files are assembled by str.replace("@MARKER@", value). A renamed or
typo'd template marker that no .replace() consumes must fail at CODEGEN time with
a ValueError naming the offender — not silently emit a syntactically-broken file
discovered only when the generated app runs.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from codegen import assert_substituted, template_markers
from graph_codegen_templates import PIPELINE_TEMPLATE


def test_template_markers_extracts_declared_markers():
    ms = template_markers(PIPELINE_TEMPLATE)
    # a few markers we know the template declares
    for m in ("@AGENTS@", "@AGENT_NAME@", "@RAG_EVICT_USED@", "@TOOLS_SOURCE@"):
        assert m in ms, m
    assert all(m.startswith("@") and m.endswith("@") for m in ms)


def test_assert_substituted_passes_when_all_consumed():
    # fully rendered source with none of the expected markers left → no raise
    assert_substituted("def f():\n    return 1\n", {"@AGENTS@", "@ENTRY@"}, "x.py")


def test_assert_substituted_raises_and_names_only_survivors():
    with pytest.raises(ValueError) as e:
        assert_substituted("xs = [1]  # @AGENTS@ never replaced",
                           {"@AGENTS@", "@ENTRY@"}, "agent.py")
    msg = str(e.value)
    assert "agent.py" in msg
    assert "@AGENTS@" in msg          # the survivor is named
    assert "@ENTRY@" not in msg       # consumed markers are not falsely flagged


def test_narrow_expected_ignores_intentional_marker_literals():
    # gui/server/eval emit only @AGENT_NAME@; an unrelated @OTHER@ literal in the
    # generated file must NOT trip a narrow check (this is why the helper takes an
    # explicit `expected` set rather than scanning for any @X@ token).
    assert_substituted("label = '@OTHER@'\n", {"@AGENT_NAME@"}, "gui.py")


if __name__ == "__main__":
    test_template_markers_extracts_declared_markers()
    test_assert_substituted_passes_when_all_consumed()
    test_assert_substituted_raises_and_names_only_survivors()
    test_narrow_expected_ignores_intentional_marker_literals()
    print("marker-guard OK")
