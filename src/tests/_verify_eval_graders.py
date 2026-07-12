"""Verify the richer eval grader set (borrowed from promptfoo / LangChain /
DeepEval, kept dependency-free). Exercises grade() in a generated agent across
every grader type, the `not` flag, `checks` arrays (all/any), and legacy
back-compat (expected_output / expected_regex / judge keep their exact behavior)."""

import importlib.util
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import graph_codegen
from graph_model import Graph

LLM = {"provider": "siliconflow", "model": "deepseek-ai/DeepSeek-V4-Flash",
       "api_key": "sk-test", "base_url": "https://api.siliconflow.cn/v1"}

g = Graph()
a = g.new_node("agent", 0, 0); a.name = "agent"
llm = g.new_node("llm", 0, 0); llm.props.update(LLM)
g.add_edge(llm.id, a.id)
out = graph_codegen.generate_from_graph(g, "demo_eval_graders", gui=False)
spec = importlib.util.spec_from_file_location("demo_eval_graders_agent",
                                              os.path.join(out, "agent.py"))
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
G = mod.grade


def ok(case, answer):
    assert G(case, answer) is True, ("expected PASS", case, answer)


def no(case, answer):
    assert G(case, answer) is False, ("expected FAIL", case, answer)


# equals (case-insensitive default; case_sensitive opt)
ok({"type": "equals", "value": "Hello"}, "  hello ")
no({"type": "equals", "value": "Hello", "case_sensitive": True}, "hello")
# contains / not_contains
ok({"type": "contains", "value": "World"}, "hello world")
ok({"type": "not_contains", "value": "error"}, "all good")
no({"type": "not_contains", "value": "error"}, "got an ERROR")
# contains_all / contains_any (comma list)
ok({"type": "contains_all", "value": "a, b ,c"}, "x a y b z c")
no({"type": "contains_all", "value": "a,b,c"}, "a and b only")
ok({"type": "contains_any", "value": "x,y"}, "only y here")
no({"type": "contains_any", "value": "x,y"}, "none present")
# starts_with / ends_with
ok({"type": "starts_with", "value": "Sure"}, "sure, here you go")
ok({"type": "ends_with", "value": "done."}, "the task is Done.")
print("ok 1: string graders (equals/contains/not/all/any/starts/ends)")

# regex / not_regex
ok({"type": "regex", "value": r"\d{3}-\d{4}"}, "call 555-1234")
ok({"type": "not_regex", "value": r"\d"}, "no digits here")
no({"type": "regex", "value": r"["}, "unbalanced regex must not crash")   # bad regex -> False
# is_json (raw + embedded blob) / json_has_keys
ok({"type": "is_json"}, '{"a": 1, "b": [2, 3]}')
ok({"type": "is_json"}, 'here you go: {"a": 1} thanks')
no({"type": "is_json"}, "not json at all")
ok({"type": "json_has_keys", "value": "a,b"}, '{"a":1,"b":2,"c":3}')
no({"type": "json_has_keys", "value": "a,z"}, '{"a":1,"b":2}')
print("ok 2: structural graders (regex/not_regex/is_json/json_has_keys)")

# numeric (+tolerance) / similar (+threshold) / length
ok({"type": "numeric", "value": "5"}, "the answer is 5")
ok({"type": "numeric", "value": "5", "tolerance": 0.5}, "got 5.3")
no({"type": "numeric", "value": "5", "tolerance": 0.1}, "got 6")
ok({"type": "similar", "value": "hello world", "threshold": 0.8}, "hello wrld")
no({"type": "similar", "value": "hello world", "threshold": 0.9}, "totally different text")
ok({"type": "length", "min": 3, "max": 10}, "hello")
no({"type": "length", "min": 3}, "hi")
print("ok 3: fuzzy/numeric/length graders (numeric/close/similar/length)")

# per-assertion negation via `not`
ok({"type": "contains", "value": "zzz", "not": True}, "no triple-z here")
no({"type": "contains", "value": "ok", "not": True}, "ok")
print("ok 4: `not` negation flag")

# checks array — all (default) and any
ok({"checks": [{"type": "contains", "value": "a"}, {"type": "contains", "value": "b"}]},
   "a then b")
no({"checks": [{"type": "contains", "value": "a"}, {"type": "contains", "value": "b"}]},
   "only a")
ok({"checks": [{"type": "contains", "value": "a"}, {"type": "contains", "value": "b"}],
    "match": "any"}, "only a")
print("ok 5: checks array combines with all (default) / any")

# LLM-judge grader (stub eval_judge — module global resolved at call time)
seen = {}
mod.eval_judge = lambda target, task, ans, crit: (seen.update(crit=crit) or "great" in ans.lower())
ok({"type": "judge", "value": "is the tone positive?"}, "this is GREAT work")
no({"type": "judge", "value": "is the tone positive?"}, "this is terrible")
assert seen["crit"] == "is the tone positive?", seen
print("ok 6: llm_rubric/judge grader routes the criterion to eval_judge")

# legacy back-compat: exact old precedence + behavior preserved
ok({"expected_output": "Foo"}, "the FOO bar")              # substring, case-insensitive
ok({"expected_regex": r"\d+"}, "x42")                      # regex, IGNORECASE
no({"expected_regex": r"\d+"}, "no numbers")
mod.eval_judge = lambda target, task, ans, crit: True
ok({"judge": "anything"}, "x")
# judge precedence over regex/contains (one assertion, like before)
mod.eval_judge = lambda target, task, ans, crit: False
no({"judge": "c", "expected_output": "x", "expected_regex": "x"}, "x")
# no expectation at all -> fail
no({"id": "empty", "input": "hi"}, "any answer")
print("ok 7: legacy expected_output/expected_regex/judge unchanged; empty=fail")

print("\nEVAL-GRADER CHECKS PASSED")
