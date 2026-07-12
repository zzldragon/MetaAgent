"""Verify the str+append reducer: 'append' on a str field concatenates (blank-line
joined) instead of listifying, so several stages can accumulate one text field
(e.g. a debate transcript). list+append still list-appends (regression). Offline."""
import importlib.util
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import graph_codegen
from graph_model import Graph, DEFAULT_BUDGETS, reducers_for_type

# 0. str now offers append (and list still does)
assert "append" in reducers_for_type("str"), reducers_for_type("str")
assert "append" in reducers_for_type("list")
assert "append" not in reducers_for_type("int")
print("reducers_for_type ok: str + list offer append; int does not")

# 1. Build a 2-writer graph: entry -> second, both append to str field `log`;
#    second also appends to a list field `items`.
g = Graph()
g.state_schema = [
    {"name": "log", "type": "str", "reducer": "append", "default": "",
     "description": "Accumulated transcript."},
    {"name": "items", "type": "list", "reducer": "append", "default": [],
     "description": "Accumulated list."},
]
llm = g.new_node("llm", 300, 0)
llm.name = "m"
llm.props.update(provider="siliconflow", model="deepseek-ai/DeepSeek-V4-Flash",
                 api_key="", base_url="https://api.siliconflow.cn/v1")

def mk(name, reads, writes, y):
    a = g.new_node("agent", 0, y)
    a.name = name
    a.props["role"] = "single"
    a.props["reads"] = reads
    a.props["writes"] = writes
    for k in DEFAULT_BUDGETS:
        a.props[k] = DEFAULT_BUDGETS[k]
    g.add_edge(llm.id, a.id)
    return a

first = mk("first", [], ["log", "items"], -80)
second = mk("second", ["log"], ["log", "items"], 80)
g.add_edge(first.id, second.id)
assert not graph_codegen.analyze(g)["errors"], graph_codegen.analyze(g)["errors"]

out = graph_codegen.generate_from_graph(g, "verify_str_append", gui=False)
import py_compile
py_compile.compile(os.path.join(out, "agent.py"), doraise=True)
spec = importlib.util.spec_from_file_location("vsa", os.path.join(out, "agent.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# 2. Unit: _apply_state concatenates a str append field, stays a str
st = mod._new_state("task")
mod._apply_state(st, {"log": "alpha"})
mod._apply_state(st, {"log": "beta"})
assert st["log"] == "alpha\n\nbeta", repr(st["log"])
assert isinstance(st["log"], str), type(st["log"])
print("str+append ok: concatenates blank-line-joined, stays a str")

# 3. Unit: list append still builds a list (regression)
mod._apply_state(st, {"items": "x"})
mod._apply_state(st, {"items": "y"})
assert st["items"] == ["x", "y"], st["items"]
print("list+append ok: still list-appends")

# 4. End to end: two stages accumulate `log`; the 2nd reads the 1st's write.
SEEN = {}
def stub(agent_name, cfg, system, messages):
    SEEN[agent_name] = system + "\n" + "\n".join(
        m.get("content", "") for m in messages if isinstance(m.get("content"), str))
    return (f'[{agent_name}]\n\n```state\nlog = "from {agent_name}"\n```', [])
mod._call_one = stub
mod.clear_history()
mod.run("go", emit=lambda s: None)
assert "from first" in SEEN["second"], "second stage must see the first's appended log"
print("run ok: second stage read the accumulated log written by the first")

print("\nALL STR-APPEND CHECKS PASSED")
