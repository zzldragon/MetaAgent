import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtWidgets import QApplication
except Exception as e:
    print("PYSIDE6_UNAVAILABLE:", e); sys.exit(2)

app = QApplication.instance() or QApplication([])
import graph_model as gm
import canvas_qt.dialogs as dl

# pure default-coercion logic
assert dl._coerce_state_default("str", "hi") == ("hi", None)
assert dl._coerce_state_default("int", "") == (0, None)
assert dl._coerce_state_default("int", "5") == (5, None)
assert dl._coerce_state_default("float", "1.5") == (1.5, None)
assert dl._coerce_state_default("bool", "true") == (True, None)
assert dl._coerce_state_default("bool", "false") == (False, None)
v,e = dl._coerce_state_default("bool", "maybe"); assert v is None and e
v,e = dl._coerce_state_default("list", "[1,2]"); assert v == [1,2] and e is None
v,e = dl._coerce_state_default("list", "{}"); assert v is None and e   # wrong shape
v,e = dl._coerce_state_default("dict", '{"k":1}'); assert v == {"k":1} and e is None
v,e = dl._coerce_state_default("int", "x"); assert v is None and e
print("coerce OK")

# StateSchemaDialog load + apply round-trip
g = gm.Graph()
g.state_schema = [{"name":"score","type":"float","reducer":"max","default":0.5}]
d = dl.StateSchemaDialog(None, g)
assert len(d._fields) == 1 and d._fields[0]["name"] == "score"
# simulate edit then apply
d._fields.append({"name":"notes","type":"list","reducer":"append","default":[]})
d.apply()
assert [f["name"] for f in g.state_schema] == ["score","notes"], g.state_schema
print("StateSchemaDialog OK")

# _StateFieldDialog reducer sync + result validation
fd = dl._StateFieldDialog(None, "Add", taken={"existing"})
fd.name.setText("existing")
assert fd.result() is None   # duplicate -> None
fd.name.setText("1bad")
assert fd.result() is None   # invalid ident -> None
fd.name.setText("good")
fd.type.setCurrentText("int")
# reducer combo should now offer numeric reducers
items = [fd.reducer.itemText(i) for i in range(fd.reducer.count())]
assert items == ["overwrite","add","max","min"], items
fd.reducer.setCurrentText("add")
fd.default.setText("3")
r = fd.result()
assert r == {"name":"good","type":"int","reducer":"add","default":3}, r
print("_StateFieldDialog OK")

# _graph_of: CanvasWindow-like (.graph) and DesignerView-like (.win.graph)
class W: pass
cw = W(); cw.graph = g
assert dl._graph_of(cw) is g
view = W(); view.win = cw
assert dl._graph_of(view) is g
assert dl._graph_of(W()) is None
print("_graph_of OK")

# AgentDialog shows + applies reads/writes when fields exist
node = gm.Node(id="agent_1", kind="agent", name="a", x=0, y=0, props=gm.default_props("agent"))
ad = dl.AgentDialog(cw, node)   # parent has .graph
assert ad._state_reads is not None and ad._state_writes is not None
# check the "score"/"notes" boxes for reads
for i in range(ad._state_reads.count()):
    if ad._state_reads.item(i).text() == "score":
        from PySide6.QtCore import Qt
        ad._state_reads.item(i).setCheckState(Qt.Checked)
err = ad.apply()
assert err is None, err
assert node.props["reads"] == ["score"], node.props["reads"]
assert node.props["writes"] == [], node.props["writes"]
print("AgentDialog reads/writes OK")

# AgentDialog with no declared fields -> binds nothing, still applies cleanly
g_empty = gm.Graph(); cw2 = W(); cw2.graph = g_empty
node2 = gm.Node(id="agent_2", kind="agent", name="b", x=0, y=0, props=gm.default_props("agent"))
ad2 = dl.AgentDialog(cw2, node2)
assert ad2._state_reads is None and ad2._state_writes is None
assert ad2.apply() is None
print("AgentDialog (no fields) OK")

print("ALL QT TESTS PASSED")
