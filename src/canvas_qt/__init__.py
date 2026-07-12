"""Qt (PySide6) implementation of the whole MetaAgent UI.

The app's UI lives here: the welcome launcher (``welcome.py``), the visual
canvas designer (``designer.py`` + ``dialogs.py``) on Qt's
QGraphicsView/QGraphicsScene, and the coding-agent Tool Generator
(``tool_generator.py``) — all in one process. The data model
(graph_model.Graph) and the code generator (graph_codegen) are reused unchanged;
only the UI layer is implemented here.

Run:  python main.py            # full app (welcome launcher)
      python designer_qt.py     # jump straight to a blank/loaded canvas
"""
