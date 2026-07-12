"""Verify the upgraded high-risk tool confirmation: the handler sees the FULL
args (no 300-char trim), may EDIT them (applied in place so the tool runs the
reviewed version), may say "don't ask again for this tool this run", and
legacy bool / 1-arg handlers still work. Also that the emitted GUI ships a
resizable confirm dialog with Allow / Edit / Deny + a remember checkbox."""

import importlib.util
import os
import py_compile
import shutil
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import graph_codegen
import graph_model as gm

LLM = {"provider": "siliconflow", "model": "deepseek-ai/DeepSeek-V4-Flash",
       "api_key": "sk-test", "base_url": "https://api.siliconflow.cn/v1"}


def _agent_module(name, gui=False):
    g = gm.Graph()
    a = g.new_node("agent", 0, 0)
    a.name = "A"
    llm = g.new_node("llm", -200, 0)
    llm.props.update(LLM)
    g.add_edge(llm.id, a.id)
    if gui:
        gn = g.new_node("gui", 0, 120)
        g.add_edge(gn.id, a.id)
    out = graph_codegen.generate_from_graph(g, name, gui=gui)
    return out


# 1. confirm_tool contract on a real generated module.
out = _agent_module("demo_hitl_confirm")
spec = importlib.util.spec_from_file_location("dhc_agent", os.path.join(out, "agent.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m.CONFIG["hitl_confirm"] = True
m.CONFIG["high_risk_tools"] = ["danger"]

BIG = "code=" + "0123456789" * 80              # ~805 chars — over the old 300 cap
seen = {}


def handler(tool_name, args):
    seen["tool"] = tool_name
    seen["len"] = len(args.get("code", ""))
    return seen.get("next", {"decision": "allow"})


m.set_confirm_handler(handler)

# a. full args reach the handler (never trimmed)
seen["next"] = {"decision": "allow"}
args = {"code": BIG}
assert m.confirm_tool("danger", args) is True
assert seen["len"] == len(BIG), ("trimmed", seen["len"], len(BIG))

# b. edited args are applied IN PLACE (the tool would run the reviewed version)
seen["next"] = {"decision": "allow", "args": {"code": "REVIEWED"}}
args = {"code": BIG}
assert m.confirm_tool("danger", args) is True and args == {"code": "REVIEWED"}, args

# c. deny blocks
seen["next"] = {"decision": "deny"}
assert m.confirm_tool("danger", {"code": "x"}) is False

# d. remember → handler called once, then auto-allowed until reset
m.reset_confirm_session()
calls = [0]
m.set_confirm_handler(lambda tn, a: (calls.__setitem__(0, calls[0] + 1),
                                     {"decision": "allow", "remember": True})[1])
assert m.confirm_tool("danger", {}) is True and calls[0] == 1
assert m.confirm_tool("danger", {}) is True and calls[0] == 1   # not re-asked
m.reset_confirm_session()
assert m.confirm_tool("danger", {}) is True and calls[0] == 2   # asked again

# e. legacy handlers: bool return, and a 1-arg fn(prompt)->bool via the fallback
m.reset_confirm_session()
m.set_confirm_handler(lambda tn, a: True)
assert m.confirm_tool("danger", {}) is True
m.reset_confirm_session()
m.set_confirm_handler(lambda prompt: False)
assert m.confirm_tool("danger", {}) is False

# f. non-high-risk tools never prompt
assert m.confirm_tool("safe_read", {}) is True
shutil.rmtree(out, ignore_errors=True)
print("1. confirm_tool: full args, edit-in-place, deny, remember/reset, legacy ok")

# 2. the emitted GUI ships a resizable Allow/Edit/Deny confirm dialog.
out = _agent_module("demo_hitl_confirm_gui", gui=True)
py_compile.compile(os.path.join(out, "gui.py"), doraise=True)
src = open(os.path.join(out, "gui.py"), encoding="utf-8").read()
assert "_ToolConfirmDialog" in src
assert "Allow edited" in src and "Don't ask again" in src
assert "setSizeGripEnabled" in src            # resizable
assert "QMessageBox.question" not in src.split("_ToolConfirmDialog")[1][:1200], \
    "confirm should use the rich dialog, not a QMessageBox"
shutil.rmtree(out, ignore_errors=True)
print("2. generated GUI ships a resizable Allow/Edit/Deny confirm dialog")

print("\nALL HITL-CONFIRM CHECKS PASSED")
