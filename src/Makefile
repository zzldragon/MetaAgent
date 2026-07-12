# MetaAgent regression oracle (refactor-plan Phase 0 "Guardrail first").
# See TESTING.md. Tests put the repo root on sys.path via test/conftest.py,
# so no PYTHONPATH is needed. Override the interpreter with: make PYTEST="py -3 -m pytest"
PYTEST ?= python -m pytest

# Fast structural tier (~7s, offline). Catches the highest-risk failures:
#  - test_runtime_fragments : every runtime/*.py fragment is wired + compiles,
#                             and disk text == the loaded *_CODE constant.
#  - test_generate_matrix   : 15 graphs generate and every emitted *.py compiles
#                             (this is also what proves the @MARKER@ guard never
#                             false-positives on a valid graph).
#  - test_marker_guard      : a drifted/typo'd @MARKER@ fails at codegen, by name.
#  - test_prompt_parity     : gen-time vs runtime "## Available skills" block is
#                             byte-identical.
#  - test_codegen_format    : emitted PERSONAS/AGENTS exec back to the exact input
#                             specs (readable formatting is value-preserving).
# Run this after EVERY edit.
FAST := test/test_runtime_fragments.py test/test_generate_matrix.py test/test_marker_guard.py test/test_prompt_parity.py test/test_codegen_format.py

.PHONY: check-fast check-all check-verify
check-fast:
	$(PYTEST) $(FAST) -q

# Full tier: everything, including the 36 _verify_*.py scripts that
# test_verify_suite.py runs as isolated subprocesses (offline; slow).
# Run before declaring a refactor phase DONE.
check-all:
	$(PYTEST) test/ -q

# Just the legacy _verify_*.py verification scripts.
check-verify:
	$(PYTEST) test/test_verify_suite.py -q
