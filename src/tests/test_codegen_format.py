"""Pins the readable spec formatting (personas hoisted into a PERSONAS block +
pretty-printed dicts). The win is cosmetic but the SAFETY bar is value-identity:
the emitted PERSONAS/AGENTS must exec back to the EXACT input specs — including
tricky personas (backslashes, embedded triple-quotes, a trailing quote) and
Python-only values (True/None) that JSON could not represent.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import graph_codegen as gc


def _roundtrip(agents_spec):
    personas_src, agents_src = gc._fmt_agents(agents_spec)
    ns = {}
    exec(personas_src, ns)              # PERSONAS = {...}
    exec("AGENTS = " + agents_src, ns)  # AGENTS references PERSONAS[...]
    return ns["AGENTS"], ns["PERSONAS"]


def test_fmt_agents_roundtrips_and_references_personas():
    spec = {"planner": {"system": "You plan.", "tools": ["t"],
                        "budgets": {"max_iterations": 10}, "parallel_tools": False}}
    agents, personas = _roundtrip(spec)
    assert agents == spec                       # same object, byte-for-byte values
    assert personas == {"planner": "You plan."}
    # AGENTS must REFERENCE the persona, not re-inline the string
    assert "PERSONAS['planner']" in gc._fmt_agents(spec)[1]


def test_fmt_agents_preserves_tricky_personas():
    tricky = ("Line 1.\nBackslash " + chr(92) + " and triple " + '"' * 3 + " quotes.\n"
              "Windows path C:" + chr(92) + "tmp" + chr(92) + "x\nEnds with a quote\"")
    spec = {"w": {"system": tricky, "tools": []}}
    agents, personas = _roundtrip(spec)
    assert agents["w"]["system"] == tricky      # exact round-trip through the source
    assert personas["w"] == tricky
    assert agents == {"w": {"system": tricky, "tools": []}}


def test_fmt_literal_valid_python_with_python_values():
    obj = {"a": {"flag": False, "n": None, "xs": [1, 2], "s": "x"}}
    ns = {}
    exec("V = " + gc._fmt_literal(obj), ns)
    assert ns["V"] == obj                        # exec's to the same object
    src = gc._fmt_literal(obj)
    assert "False" in src and "None" in src      # Python literals, not JSON false/null


if __name__ == "__main__":
    test_fmt_agents_roundtrips_and_references_personas()
    test_fmt_agents_preserves_tricky_personas()
    test_fmt_literal_valid_python_with_python_values()
    print("codegen-format OK")
