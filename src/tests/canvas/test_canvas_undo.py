"""Undo/redo history for the canvas designer (Ctrl+Z / Ctrl+Y)."""


def test_edit_menu_localized_zh():
    """The Edit menu + undo/redo strings have Simplified-Chinese translations."""
    import canvas_qt.i18n as i18n
    prev = i18n.get_language()
    try:
        i18n.set_language("zh")
        assert i18n.t("&Edit") == "编辑(&E)"
        assert i18n.t("&Undo") == "撤销(&U)"
        assert i18n.t("&Redo") == "重做(&R)"
        assert i18n.t("Nothing to undo.") == "没有可撤销的操作。"
    finally:
        i18n.set_language(prev)


def test_undo_redo_add_nodes(win):
    # baseline history = the initial (empty) graph
    assert win._undo and len(win._undo) == 1
    assert not win.act_undo.isEnabled() and not win.act_redo.isEnabled()
    n0 = len(win.graph.nodes)

    win.add_node("agent")
    win.add_node("llm")
    assert len(win.graph.nodes) == n0 + 2
    assert len(win._undo) == 3               # baseline + 2 edits
    assert win.act_undo.isEnabled()

    win.on_undo()                            # undo the llm
    assert len(win.graph.nodes) == n0 + 1
    assert win.act_redo.isEnabled()
    win.on_undo()                            # undo the agent -> back to baseline
    assert len(win.graph.nodes) == n0
    assert not win.act_undo.isEnabled()      # nothing left to undo

    win.on_redo()                            # redo the agent
    assert len(win.graph.nodes) == n0 + 1
    win.on_redo()                            # redo the llm
    assert len(win.graph.nodes) == n0 + 2
    assert not win.act_redo.isEnabled()


def test_new_edit_clears_redo(win):
    win.add_node("agent")
    win.on_undo()
    assert win._redo                          # something to redo
    win.add_node("tool")                      # a fresh edit invalidates the redo path
    assert win._redo == []
    assert not win.act_redo.isEnabled()


def test_undo_restores_node_positions(win):
    win.add_node("agent")
    nid = next(iter(win.graph.nodes))
    win.graph.nodes[nid].x = 123
    win.graph.nodes[nid].y = 456
    win._record_history()                     # simulate a drag-commit
    win.graph.nodes[nid].x = 999
    win._record_history()
    win.on_undo()
    assert win.graph.nodes[nid].x == 123 and win.graph.nodes[nid].y == 456


def test_load_resets_history(win, tmp_path, monkeypatch):
    win.add_node("agent")
    assert len(win._undo) >= 2
    # a load starts a fresh baseline (can't undo past it)
    from PySide6.QtWidgets import QFileDialog
    target = str(tmp_path / "g.json")
    win.graph.save(target)
    monkeypatch.setattr(QFileDialog, "getOpenFileName",
                        staticmethod(lambda *a, **k: (target, "")))
    win.on_load()
    assert len(win._undo) == 1 and win._redo == []
    assert not win.act_undo.isEnabled()
