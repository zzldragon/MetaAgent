"""Verify custom / nested shared-state types (Approach A: JSON-Schema named types
+ structured merge policies).

Covers: the data model (round-trip, resolution, validation), analyze errors/warnings,
codegen (generate + compile with custom types), the runtime merge policies
(merge_deep/merge_shallow/extend/upsert_by_key), the schema-driven set_state tool,
and native-only byte-identity (no json_schema/merge_key leaks onto plain fields)."""
import importlib.util
import os
import py_compile
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import graph_codegen
import graph_model as gm
from graph_model import Graph

LLM = dict(provider="siliconflow", model="deepseek-ai/DeepSeek-V4-Flash",
           api_key="", base_url="https://api.siliconflow.cn/v1")


def _mkgraph(state_schema, type_defs, writes):
    g = Graph()
    llm = g.new_node("llm", 300, 0); llm.name = "m"; llm.props.update(LLM)
    a = g.new_node("agent", 0, 0); a.name = "w"; a.props["role"] = "single"
    a.props["writes"] = writes
    g.add_edge(llm.id, a.id)
    g.type_defs = type_defs
    g.state_schema = state_schema
    return g


# ── 1. data model: resolution + defaults + json schema ───────────────────────
tds = {"Finding": {"schema": {"type": "object", "properties": {
            "id": {"type": "string"}, "score": {"type": "number"}}},
        "merge": "merge_deep"}}
assert gm.merge_policies_for("list[Finding]", tds) == ("overwrite", "append", "extend", "upsert_by_key")
assert gm.merge_policies_for("Finding", tds) == ("overwrite", "merge_shallow", "merge_deep")
js = gm.type_json_schema("list[Finding]", tds)
assert js["type"] == "array" and js["items"]["properties"]["id"]["type"] == "string"
assert gm.default_for_type("Finding", tds) == {} and gm.default_for_type("list[Finding]", tds) == []
print("model ok: merge_policies_for / type_json_schema / default_for_type")

# validate_type_defs error surface
assert gm.validate_type_defs({"X": {"schema": {"type": "array"}, "merge": "upsert_by_key"}}), "upsert needs key"
assert gm.validate_type_defs({"list": {"schema": {"type": "object"}}}), "shadow native"
assert gm.validate_type_defs({"A": {"schema": {"type": "object", "properties": {"b": {"$type": "Ghost"}}}}}), "undefined ref"
assert gm.validate_type_defs(tds) == []
print("model ok: validate_type_defs")

# ── 2. analyze: surfaces bad types (state_fields silently coerces) ────────────
gbad_type = _mkgraph([{"name": "x", "type": "Nope", "reducer": "overwrite"}], {}, ["x"])
assert any("undefined type 'Nope'" in e for e in graph_codegen.analyze(gbad_type)["errors"])
gbad_upsert = _mkgraph([{"name": "xs", "type": "list[Finding]", "reducer": "upsert_by_key"}],
                       tds, ["xs"])                       # no merge_key anywhere
assert any("upsert_by_key" in e and "merge key" in e for e in graph_codegen.analyze(gbad_upsert)["errors"])
gbad_td = _mkgraph([{"name": "r", "type": "Bad", "reducer": "overwrite"}],
                   {"Bad": {"schema": {"type": "array"}, "merge": "upsert_by_key"}}, ["r"])
assert graph_codegen.analyze(gbad_td)["errors"], "bad type_def must error"
gok = _mkgraph([{"name": "r", "type": "Finding", "reducer": "merge_deep"}], tds, ["r"])
assert not graph_codegen.analyze(gok)["errors"], graph_codegen.analyze(gok)["errors"]
print("analyze ok: undefined type / missing upsert key / bad type_def -> errors; valid -> none")

# ── 3. codegen + runtime merge policies ──────────────────────────────────────
g = _mkgraph(
    [{"name": "profile", "type": "dict", "reducer": "merge_deep"},
     {"name": "cfg", "type": "dict", "reducer": "merge_shallow"},
     {"name": "tasks", "type": "list[Finding]", "reducer": "upsert_by_key", "merge_key": "id"},
     {"name": "notes", "type": "list", "reducer": "extend"}],
    tds, ["profile", "cfg", "tasks", "notes"])
out = graph_codegen.generate_from_graph(g, "verify_custom_types", gui=False)
ap = os.path.join(out, "agent.py")
py_compile.compile(ap, doraise=True)
spec = importlib.util.spec_from_file_location("vct", ap); m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

st = m._new_state("t")
m._apply_state(st, {"profile": {"a": 1, "n": {"x": 1}}}); m._apply_state(st, {"profile": {"b": 2, "n": {"y": 2}}})
assert st["profile"] == {"a": 1, "b": 2, "n": {"x": 1, "y": 2}}, st["profile"]
m._apply_state(st, {"cfg": {"a": 1}}); m._apply_state(st, {"cfg": {"a": 9, "b": 2}})
assert st["cfg"] == {"a": 9, "b": 2}, st["cfg"]
m._apply_state(st, {"notes": ["a", "b"]}); m._apply_state(st, {"notes": ["c"]})
assert st["notes"] == ["a", "b", "c"], st["notes"]
m._apply_state(st, {"tasks": [{"id": "1", "score": 1}]})
m._apply_state(st, {"tasks": {"id": "1", "score": 9}})   # replace by key
m._apply_state(st, {"tasks": {"id": "2", "score": 5}})   # append
assert [(t["id"], t["score"]) for t in st["tasks"]] == [("1", 9), ("2", 5)], st["tasks"]
print("runtime ok: merge_deep / merge_shallow / extend / upsert_by_key")

# ── 4. set_state tool schema is nested for custom types ──────────────────────
props = m._set_state_tool_schema("w")["parameters"]["properties"]
assert props["tasks"]["type"] == "array" and props["tasks"]["items"]["properties"]["id"]["type"] == "string", props["tasks"]
assert props["profile"]["type"] == "object"
print("tool schema ok: custom field carries nested JSON Schema")

# ── 5. native-only byte-identity: no json_schema/merge_key on plain fields ────
gn = _mkgraph([{"name": "s", "type": "str", "reducer": "append"}], {}, ["s"])
f = [x for x in gm.state_fields(gn, include_builtins=False)][0]
assert set(f) == {"name", "type", "reducer", "default", "description"}, f
print("byte-identity ok: native field shape unchanged")

# ── 6. P4: custom merge function escape hatch ────────────────────────────────
_SRC = "def merge(old, new):\n    return (old or 0) + (sum(new) if isinstance(new, list) else new)"
gc = _mkgraph([{"name": "tally", "type": "Tally", "reducer": "custom"}],
              {"Tally": {"schema": {"type": "object"}, "merge": "custom", "merge_src": _SRC}},
              ["tally"])
assert "custom" in gm.merge_policies_for("Tally", gc.type_defs)
assert not graph_codegen.analyze(gc)["errors"], graph_codegen.analyze(gc)["errors"]
# a merge_src that doesn't define `def merge` is an error
gc_bad = _mkgraph([{"name": "t", "type": "T", "reducer": "custom"}],
                  {"T": {"schema": {"type": "object"}, "merge": "custom",
                         "merge_src": "def other(a, b): return a"}}, ["t"])
assert any("def merge" in e for e in graph_codegen.analyze(gc_bad)["errors"])
outc = graph_codegen.generate_from_graph(gc, "verify_custom_merge_fn", gui=False)
apc = os.path.join(outc, "agent.py"); py_compile.compile(apc, doraise=True)
mc = importlib.util.module_from_spec(
    importlib.util.spec_from_file_location("vcmf", apc))
importlib.util.spec_from_file_location("vcmf", apc).loader.exec_module(mc)
assert "Tally" in mc._CUSTOM_MERGES
stc = mc._new_state("t")
mc._apply_state(stc, {"tally": 5}); mc._apply_state(stc, {"tally": [1, 2, 3]})
assert stc["tally"] == 11, stc["tally"]
print("P4 ok: custom merge fn — validated, generated, applied ->", stc["tally"])

print("\nALL CUSTOM-STATE-TYPE CHECKS PASSED")
