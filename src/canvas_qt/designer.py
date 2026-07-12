"""Qt visual agent designer — editable canvas + main window.

Ports canvas_frame.py (the wx designer) onto QGraphicsView/QGraphicsScene while
reusing the unchanged backend: graph_model.Graph for the data, graph_codegen for
analysis/generation, patterns for presets, runner for run/compile, and
runtime_overlay for the live debug overlay.
"""

from __future__ import annotations

import copy
import html
import importlib.util
import json
import os
import threading

from PySide6.QtCore import QObject, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QActionGroup,
    QBrush,
    QColor,
    QFont,
    QIcon,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPen,
    QPixmap,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QGraphicsItem,
    QGraphicsPathItem,
    QGraphicsScene,
    QGraphicsView,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QMenuBar,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

import codegen
import graph_codegen
import patterns

# NOTE: `runner` is imported lazily (only when Run/Compile is used) so its
# PySide6 dialog + subprocess machinery stays off the canvas startup path,
# keeping `import designer` cheap. (runner is Qt-native — no wx — since the port.)
from app_config import (BASE_DIR, TOOLS_DIR, add_recent_project, get_language,
                        get_theme, set_theme as persist_theme)
from canvas_qt.i18n import set_language as _i18n_set, t
from graph_model import (AGENT_KINDS, CONTROL_KINDS, FLOW_KINDS, KIND_META, Edge,
                         Graph, Node, contract_fields, load_mta, save_mta)
from runtime_overlay import STATUS_COLOR, RuntimeOverlay

from .dialogs import (PROVIDER_DEFAULTS, PROVIDERS, ReviewDialog, ToolConfirmDialog,
                      builtin_tools_for, open_config_dialog,
                      open_edge_config_dialog, open_state_schema_dialog,
                      open_storage_dialog, open_type_defs_dialog, subtitle)
from .theme import (
    apply_theme,
    canvas_colors,
)
from .chat_panel import ChatPanel
from .replay import ReplayBar
from .trace_panel import TracePanel

GRAPHS_DIR = os.path.join(BASE_DIR, "graphs")
NODE_W, NODE_H = 168, 64
PORT_R = 6
# Height of the built-in-tools strip drawn BELOW the card on agent-like nodes
# (the card itself and the ports stay at NODE_H, so edges are unaffected).
BUILTIN_PER_ROW = 3      # built-in-tool pills per row on an agent node
BUILTIN_ROW_H = 18       # height of one pill row (below the card)

# In-app copy/paste clipboard for canvas nodes (Ctrl+C / Ctrl+V). Module-level so
# it survives scene rebuilds and can paste across canvas windows in the same
# process. Shape: {"nodes": [{id,kind,name,x,y,props}], "edges": [(src,dst)]}.
_NODE_CLIPBOARD: dict | None = None

# Canvas background + grid for the ACTIVE theme. Module-level so the scene/view
# read them; set_canvas_theme() repoints them when the user switches theme.
GRID_STEP = 24
BG = canvas_colors()["bg"]
GRID = canvas_colors()["grid"]
PANEL_BG = canvas_colors()["panel"]


def set_canvas_theme(name: str) -> None:
    """Repoint the canvas color globals to a theme. The scene/view read BG/GRID
    on the next paint; callers should also refresh open windows."""
    global BG, GRID, PANEL_BG
    c = canvas_colors(name)
    BG, GRID, PANEL_BG = c["bg"], c["grid"], c["panel"]

# Derived views over graph_model.KIND_META (the single source of truth). Kept
# under these names so every existing KIND_COLORS[k] / KIND_LABELS.items() site
# is zero-diff; the dict-comprehension preserves KIND_META's order, which the
# palette grid and add-menu iterate.
KIND_COLORS = {k: m["color"] for k, m in KIND_META.items()}
KIND_LABELS = {k: m["label"] for k, m in KIND_META.items()}

# Node SILHOUETTE by role — the shape encodes what a node *does* (its color still
# tells the exact kind apart), so a graph is legible at a glance instead of a wall
# of identical rectangles. Grouped by role:
#   rect          actors            agent, workerpool (stacked)
#   diamond       decision/branch   router, condition, while
#   cylinder      knowledge store   rag
#   document      text/instructions prompt, skill
#   hexagon       compute engine    llm, eval
#   parallelogram capability / IO   tool, mcp, setstate
#   octagon       gate/checkpoint   guardrail, hitl
#   trapezoid     interface/output  webserver, gui
#   stadium       terminal          end
KIND_SHAPE = {
    "agent": "rect", "workerpool": "rect", "subgraph": "rect",
    "router": "diamond", "condition": "diamond", "while": "diamond",
    "foreach": "diamond",
    "rag": "cylinder", "memory": "cylinder",
    "prompt": "document", "skill": "document",
    "llm": "hexagon", "eval": "hexagon",
    "tool": "parallelogram", "mcp": "parallelogram", "setstate": "parallelogram",
    "guardrail": "octagon", "hitl": "octagon",
    "webserver": "trapezoid", "gui": "trapezoid", "schedule": "trapezoid",
    "end": "stadium",
    "fanout": "diamond", "join": "diamond",   # branch / reconverge control nodes
}
# Horizontal text inset so the 3-line label block clears each silhouette's
# angled / cut top corners (straight-sided shapes use the default 10).
SHAPE_TEXT_INSET = {"hexagon": 24, "parallelogram": 20, "octagon": 18,
                    "trapezoid": 16, "stadium": 20}
# Short tag shown inside the decision diamonds.
DIAMOND_TAG = {"while": "while", "condition": "if / else", "router": "route",
               "fanout": "fan-out", "join": "join", "foreach": "for each"}
CYLINDER_RY = 9.0        # vertical radius of a cylinder node's elliptical caps


def shape_path(shape: str, r: QRectF) -> QPainterPath:
    """The outline QPainterPath for a node silhouette within rect `r`. Every shape
    fits the SAME w×h box and keeps the left/right port midpoints on the box edges,
    so ports, edges and hit-testing are unaffected — only the skin changes."""
    path = QPainterPath()
    cx, cy = r.center().x(), r.center().y()
    if shape == "diamond":
        path.moveTo(cx, r.top())
        path.lineTo(r.right(), cy)
        path.lineTo(cx, r.bottom())
        path.lineTo(r.left(), cy)
        path.closeSubpath()
    elif shape == "cylinder":
        ry = CYLINDER_RY
        path.moveTo(r.left(), r.top() + ry)
        path.arcTo(r.left(), r.top(), r.width(), 2 * ry, 180, -180)          # top cap
        path.lineTo(r.right(), r.bottom() - ry)
        path.arcTo(r.left(), r.bottom() - 2 * ry, r.width(), 2 * ry, 0, -180)  # bottom
        path.closeSubpath()
    elif shape == "document":
        w = 8.0
        path.moveTo(r.left(), r.top())
        path.lineTo(r.right(), r.top())
        path.lineTo(r.right(), r.bottom() - w)
        path.cubicTo(r.right() - r.width() * 0.30, r.bottom() - 2 * w,
                     r.left() + r.width() * 0.30, r.bottom(),
                     r.left(), r.bottom() - w)                                # wavy foot
        path.closeSubpath()
    elif shape == "hexagon":
        cut = 22.0
        path.moveTo(r.left(), cy)
        path.lineTo(r.left() + cut, r.top())
        path.lineTo(r.right() - cut, r.top())
        path.lineTo(r.right(), cy)
        path.lineTo(r.right() - cut, r.bottom())
        path.lineTo(r.left() + cut, r.bottom())
        path.closeSubpath()
    elif shape == "parallelogram":
        s = 18.0
        path.moveTo(r.left() + s, r.top())
        path.lineTo(r.right(), r.top())
        path.lineTo(r.right() - s, r.bottom())
        path.lineTo(r.left(), r.bottom())
        path.closeSubpath()
    elif shape == "octagon":
        c = 16.0
        path.moveTo(r.left() + c, r.top())
        path.lineTo(r.right() - c, r.top())
        path.lineTo(r.right(), r.top() + c)
        path.lineTo(r.right(), r.bottom() - c)
        path.lineTo(r.right() - c, r.bottom())
        path.lineTo(r.left() + c, r.bottom())
        path.lineTo(r.left(), r.bottom() - c)
        path.lineTo(r.left(), r.top() + c)
        path.closeSubpath()
    elif shape == "trapezoid":
        s = 16.0
        path.moveTo(r.left() + s, r.top())
        path.lineTo(r.right() - s, r.top())
        path.lineTo(r.right(), r.bottom())
        path.lineTo(r.left(), r.bottom())
        path.closeSubpath()
    elif shape == "stadium":            # terminal: pill with fully-rounded ends
        rad = r.height() / 2.0
        path.addRoundedRect(r, rad, rad)
    else:  # rect
        path.addRoundedRect(r, 10, 10)
    return path


HINT = ("Drag a module to move it. Drag empty space to box-select several, then "
        "drag any of them to move the group. Drag from a right port ● onto another "
        "module to link. Middle-drag: pan. Double-click: configure. "
        "Ctrl+C / Ctrl+V: copy/paste. Del: delete.")


# ── items ────────────────────────────────────────────────────────────────────
class NodeItem(QGraphicsItem):
    def __init__(self, node: Node):
        super().__init__()
        self.node = node
        self.edges: list[EdgeItem] = []
        self.setPos(node.x, node.y)
        self.setFlags(QGraphicsItem.ItemIsMovable | QGraphicsItem.ItemIsSelectable
                      | QGraphicsItem.ItemSendsGeometryChanges)
        self.setZValue(1)

    def _builtins(self) -> list:
        """Built-in tools to advertise on this node (empty for kinds without any).
        Each: {name, short, desc, enable, active}."""
        return builtin_tools_for(self.node)

    def boundingRect(self) -> QRectF:
        # Agent-like nodes draw a built-in-tools strip below the card (pills wrap
        # BUILTIN_PER_ROW per row); widen the box to cover however many rows it
        # needs (the card + ports stay at NODE_H, so edges are unaffected).
        n = len(self._builtins())
        rows = -(-n // BUILTIN_PER_ROW) if n else 0          # ceil(n / per_row)
        extra = rows * BUILTIN_ROW_H + 3 if rows else 0
        return QRectF(-4, -12, NODE_W + PORT_R + 10, NODE_H + 18 + extra)

    def body_rect(self) -> QRectF:
        return QRectF(self.scenePos().x(), self.scenePos().y(), NODE_W, NODE_H)

    def out_port(self) -> QPointF:
        return self.scenePos() + QPointF(NODE_W, NODE_H / 2)

    def in_port(self) -> QPointF:
        return self.scenePos() + QPointF(0, NODE_H / 2)

    @staticmethod
    def _elide(p: QPainter, text: str, max_w: float) -> str:
        fm = p.fontMetrics()
        if fm.horizontalAdvance(text) <= max_w:
            return text
        while text and fm.horizontalAdvance(text + "\u2026") > max_w:
            text = text[:-1]
        return text + "\u2026"

    def itemChange(self, change, value):
        # No position clamp: nodes may live anywhere (incl. negative coords), so
        # the origin isn't a top-left wall and the whole graph can be dragged
        # freely (e.g. after Ctrl+A) to any empty area of the canvas.
        if change == QGraphicsItem.ItemPositionHasChanged:
            self.node.x = int(self.pos().x())
            self.node.y = int(self.pos().y())
            for e in self.edges:
                e.update_path()
        return super().itemChange(change, value)

    def _tooltip_text(self) -> str:
        """Hover summary: module identity plus, during/after a run, the live
        per-node detail the overlay already tracks."""
        node = self.node
        lines = [f"{t(KIND_LABELS[node.kind])}: {node.name}"]
        sub = subtitle(node)
        if sub:
            lines.append(sub)
        tools = self._builtins()
        if tools:
            lines.append("")
            lines.append("Built-in tools (✓ provided · — available to enable):")
            for bt in tools:
                mark = "✓" if bt["active"] else "—"
                line = f"  {mark} {bt['name']} — {bt['desc']}"
                if not bt["active"]:
                    line += f"  [enable: {bt['enable']}]"
                lines.append(line)
        overlay = getattr(self.scene(), "overlay", None)
        if overlay is not None:
            detail = overlay.detail(node.name)
            if detail:
                lines.append(detail)
        return "\n".join(lines)

    def update_tooltip(self) -> None:
        """Refresh the hover tooltip. Called when the node is (re)built and on
        each overlay refresh — NOT from paint() — so it isn't rebuilt every
        frame. The text is escaped and wrapped in a pre-formatted span: Qt
        auto-detects rich text (Qt.mightBeRichText), so an unescaped name/prompt/
        note containing '<' or '&' would otherwise render as mangled HTML."""
        text = html.escape(self._tooltip_text())
        self.setToolTip(f"<span style='white-space:pre'>{text}</span>")

    def paint(self, p: QPainter, option, widget=None):
        p.setRenderHint(QPainter.Antialiasing, True)
        node = self.node
        body = QRectF(0, 0, NODE_W, NODE_H)
        overlay = getattr(self.scene(), "overlay", None)
        status = overlay.status_of(node.name) if overlay else "idle"
        ring = STATUS_COLOR.get(status)

        # Per-role silhouette (see KIND_SHAPE): the shape says what the node does,
        # the fill colour which exact kind it is.
        shape = KIND_SHAPE.get(node.kind, "rect")
        is_diamond = shape == "diamond"
        path = shape_path(shape, body)

        # Worker pool: a faint offset copy behind the body, implying many workers.
        if node.kind == "workerpool":
            p.setPen(QPen(QColor(0, 0, 0, 45), 1))
            p.setBrush(QBrush(QColor(KIND_COLORS[node.kind]).darker(110)))
            p.drawRoundedRect(body.translated(7, 7), 10, 10)

        # selection / status glow ring — the same silhouette, expanded
        if self.isSelected() or ring:
            if self.isSelected():
                p.setPen(QPen(QColor("#FF5252"), 3))
            else:
                glow = QColor(ring)
                glow.setAlpha(150)
                p.setPen(QPen(glow, 4))
            p.setBrush(Qt.NoBrush)
            p.drawPath(shape_path(shape, body.adjusted(-3, -3, 3, 3)))

        # body
        p.setPen(QPen(QColor(0, 0, 0, 70), 1))
        p.setBrush(QBrush(QColor(KIND_COLORS[node.kind])))
        p.drawPath(path)

        # cylinder rim: the visible front curve of the top cap (the database look)
        if shape == "cylinder":
            p.setPen(QPen(QColor(0, 0, 0, 60), 1))
            p.setBrush(Qt.NoBrush)
            rim = QPainterPath()
            rim.moveTo(body.left(), body.top() + CYLINDER_RY)
            rim.arcTo(body.left(), body.top(), body.width(), 2 * CYLINDER_RY, 180, 180)
            p.drawPath(rim)

        if is_diamond:
            # name + a short tag ("route" / "if / else" / "while") centered in the
            # diamond's wide middle band
            p.setPen(QColor("#1a1a1a"))
            nf = QFont("Segoe UI", 10)
            nf.setBold(True)
            p.setFont(nf)
            p.drawText(QRectF(24, NODE_H / 2 - 15, NODE_W - 48, 16), Qt.AlignCenter,
                       self._elide(p, node.name, NODE_W * 0.55))
            tag = DIAMOND_TAG.get(node.kind, "")
            if tag:
                p.setPen(QColor(0, 0, 0, 150))
                ef = QFont("Segoe UI", 7)
                ef.setBold(True)
                ef.setCapitalization(QFont.AllUppercase)
                p.setFont(ef)
                p.drawText(QRectF(24, NODE_H / 2 + 1, NODE_W - 48, 12),
                           Qt.AlignCenter, tag)
        else:
            # text block inset to clear the silhouette's angled/cut corners
            tx = SHAPE_TEXT_INSET.get(shape, 10)
            tw = NODE_W - 2 * tx
            # kind label (small uppercase)
            p.setPen(QColor(0, 0, 0, 150))
            kf = QFont("Segoe UI", 7)
            kf.setBold(True)
            kf.setCapitalization(QFont.AllUppercase)
            p.setFont(kf)
            p.drawText(QRectF(tx, 6, tw, 12), Qt.AlignVCenter,
                       t(KIND_LABELS[node.kind]))
            # name
            p.setPen(QColor("#1a1a1a"))
            nf = QFont("Segoe UI", 10)
            nf.setBold(True)
            p.setFont(nf)
            p.drawText(QRectF(tx, 20, tw, 18), Qt.AlignVCenter,
                       self._elide(p, node.name, tw))
            # subtitle
            p.setPen(QColor(0, 0, 0, 160))
            p.setFont(QFont("Segoe UI", 8))
            p.drawText(QRectF(tx, 41, tw, 16), Qt.AlignVCenter,
                       self._elide(p, subtitle(node), tw))

        # runtime status badge (top-right)
        if ring:
            badge = overlay.badge(node.name) or ""
            p.setBrush(QBrush(QColor(ring)))
            p.setPen(Qt.NoPen)
            p.drawEllipse(QRectF(NODE_W - 12, -10, 20, 20))
            p.setPen(QColor("white"))
            p.setFont(QFont("Segoe UI", 7, QFont.Bold))
            p.drawText(QRectF(NODE_W - 12, -10, 20, 20), Qt.AlignCenter,
                       badge[:2] or "\u25CF")

        # out-port (drag from here to link)
        p.setPen(QPen(QColor("#0d1117"), 1))
        p.setBrush(QBrush(QColor("#42A5F5")))
        p.drawEllipse(QPointF(NODE_W, NODE_H / 2), PORT_R, PORT_R)

        # built-in tools strip (agent-like nodes): a pill per built-in tool —
        # filled green = provided now, faint = available to enable. Full names +
        # descriptions are in the hover tooltip.
        self._paint_builtins(p)

    def _paint_builtins(self, p: QPainter) -> None:
        tools = self._builtins()
        if not tools:
            return
        gap, h = 4, 15
        pw = (NODE_W - gap * (BUILTIN_PER_ROW - 1)) / BUILTIN_PER_ROW
        f = QFont("Segoe UI", 7)
        f.setBold(True)
        p.setFont(f)
        for i, t in enumerate(tools):
            row, col = divmod(i, BUILTIN_PER_ROW)      # wrap into rows of BUILTIN_PER_ROW
            r = QRectF(col * (pw + gap), NODE_H + 4 + row * BUILTIN_ROW_H, pw, h)
            if t["active"]:
                p.setBrush(QBrush(QColor("#2E7D32")))      # green = provided now
                p.setPen(Qt.NoPen)
                p.drawRoundedRect(r, 7, 7)
                p.setPen(QColor("white"))
            else:
                p.setBrush(QBrush(QColor(0, 0, 0, 16)))     # faint = available
                p.setPen(QPen(QColor(0, 0, 0, 55), 1))
                p.drawRoundedRect(r, 7, 7)
                p.setPen(QColor(0, 0, 0, 120))
            p.drawText(r, Qt.AlignCenter, self._elide(p, t["short"], pw - 6))


class EdgeItem(QGraphicsPathItem):
    def __init__(self, scene_ref, edge: Edge, src: NodeItem, dst: NodeItem):
        super().__init__()
        self._scene = scene_ref
        self.edge = edge
        self.src = src
        self.dst = dst
        self.setZValue(0)
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        src.edges.append(self)
        dst.edges.append(self)
        self.update_path()
        # Tooltip content depends only on the edge's config, not its geometry — set
        # it once here (EdgeItems are recreated on rebuild, incl. after a config
        # edit), NOT in update_path() which runs on every node-drag frame.
        self._refresh_tooltip()

    def _sides(self):
        """Attach to the nearest facing sides of each card (prototype look)."""
        sr, dr = self.src.body_rect(), self.dst.body_rect()
        a, b = sr.center(), dr.center()
        dx, dy = b.x() - a.x(), b.y() - a.y()
        if abs(dx) >= abs(dy):
            if dx >= 0:
                return (QPointF(sr.right(), sr.center().y()),
                        QPointF(dr.left(), dr.center().y()))
            return (QPointF(sr.left(), sr.center().y()),
                    QPointF(dr.right(), dr.center().y()))
        if dy >= 0:
            return (QPointF(sr.center().x(), sr.bottom()),
                    QPointF(dr.center().x(), dr.top()))
        return (QPointF(sr.center().x(), sr.top()),
                QPointF(dr.center().x(), dr.bottom()))

    def update_path(self):
        p1, p2 = self._sides()
        dx = (p2.x() - p1.x()) * 0.5
        dy = (p2.y() - p1.y()) * 0.5
        horizontal = abs(p2.x() - p1.x()) >= abs(p2.y() - p1.y())
        c1 = QPointF(p1.x() + (dx if horizontal else 0), p1.y() + (0 if horizontal else dy))
        c2 = QPointF(p2.x() - (dx if horizontal else 0), p2.y() - (0 if horizontal else dy))
        path = QPainterPath(p1)
        path.cubicTo(c1, c2, p2)
        self.setPath(path)
        self._end = p2
        self._cdir = c2

    def _refresh_tooltip(self) -> None:
        """Hover text for a configured link: the full contract fields (the on-edge
        label truncates), or the branch role. Empty otherwise."""
        sk, dk = self.src.node.kind, self.dst.node.kind
        if sk in AGENT_KINDS and dk in AGENT_KINDS:
            fields = contract_fields(self.edge, getattr(self._scene.graph, "type_defs", None))
            if fields:
                lines = [f"Contract  {self.src.node.name} → {self.dst.node.name}"]
                for f in fields:
                    d = (f.get("description") or "").strip()
                    lines.append(f"  • {f['name']} ({f['type']})"
                                 + (f": {d}" if d else ""))
                self.setToolTip("\n".join(lines))
                return
        self.setToolTip("")

    def _edge_label(self):
        """(text, bg, border, fg) badge for this link, or None. If/Else → the
        predicate/else; While → loop/exit; agent→agent → the contract fields."""
        sk, dk = self.src.node.kind, self.dst.node.kind
        if sk == "condition":
            for b in (self.src.node.props.get("branches") or []):
                if (b.get("to") or "") == self.dst.node.name:
                    txt = (b.get("expr") or "").strip() or "else"
                    return txt, "#FCE4EC", "#AD1457", "#880E4F"
            return None
        if sk == "while":
            body = self.src.node.props.get("body") == self.dst.node.name
            return ("loop" if body else "exit"), "#E1F5FE", "#0277BD", "#01579B"
        if sk == "foreach":
            body = self.src.node.props.get("body") == self.dst.node.name
            return ("each" if body else "exit"), "#EDE7F6", "#5E35B1", "#311B92"
        if sk in AGENT_KINDS and dk in AGENT_KINDS:
            fields = contract_fields(self.edge, getattr(self._scene.graph, "type_defs", None))
            if fields:
                names = ", ".join(f["name"] for f in fields)
                return "◇ " + names, "#E8EAF6", "#3949AB", "#283593"
        if sk == "llm" and dk in AGENT_KINDS:
            # fallback priority badge — only when this agent has 2+ LLMs (a single
            # LLM is always "#1", so the number would just be noise).
            g = self._scene.graph
            if len(g.llm_edges_of(self.edge.dst)) > 1:
                n = self.edge.props.get("priority") or 0
                if n:
                    return f"#{int(n)}", "#C8E6C9", "#2E7D32", "#1B5E20"
        return None

    def shape(self):
        stroker = QPainterPathStroker()
        stroker.setWidth(12)
        return stroker.createStroke(self.path())

    def _style(self):
        sk, dk = self.src.node.kind, self.dst.node.kind
        agent_link = sk in AGENT_KINDS and dk in AGENT_KINDS
        is_revise = self._scene.revise == (self.edge.src, self.edge.dst)
        overlay = self._scene.overlay
        if self.isSelected():
            return "#D32F2F", 3, True
        if overlay is not None:
            sn, dn = self.src.node.name, self.dst.node.name
            if overlay.is_edge_active(sn, dn):
                return "#FFA000", 4, False
            if overlay.is_edge_traversed(sn, dn):
                return "#2E7D32", 3, False
        if is_revise:
            return "#D32F2F", 2, True
        if agent_link:
            return "#1565C0", 2, False
        if sk == "condition":
            return "#AD1457", 2, False          # If/Else branch edge (crimson)
        if sk == "hitl" or dk == "hitl":
            # a HITL is a FLOW node (a review gate or a human-driven branch), never a
            # resource — style its links like agent→agent flow, not a gray "uses" edge.
            return "#1565C0", 2, False
        if sk in CONTROL_KINDS or dk in CONTROL_KINDS:
            return "#0277BD", 2, False          # control flow to/from a node
        return "#9E9E9E", 2, False

    def paint(self, p: QPainter, option, widget=None):
        p.setRenderHint(QPainter.Antialiasing, True)
        color, width, dashed = self._style()
        pen = QPen(QColor(color), width)
        if dashed:
            pen.setStyle(Qt.DashLine)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawPath(self.path())
        # arrowhead at the target end, aligned with the incoming curve direction
        end, cdir = self._end, self._cdir
        d = end - cdir
        length = (d.x() ** 2 + d.y() ** 2) ** 0.5 or 1.0
        ux, uy = d.x() / length, d.y() / length
        size = 10
        left = QPointF(end.x() - ux * size - uy * size * 0.55,
                       end.y() - uy * size + ux * size * 0.55)
        right = QPointF(end.x() - ux * size + uy * size * 0.55,
                        end.y() - uy * size - ux * size * 0.55)
        p.setBrush(QBrush(QColor(color)))
        p.setPen(Qt.NoPen)
        p.drawPolygon(QPolygonF([end, left, right]))
        # Label the link so its role is readable on the canvas: If/Else branch
        # predicate (or "else"), While loop/exit, or an agent→agent data contract.
        badge = self._edge_label()
        if badge:
            label, bg, border, fg = badge
            mid = self.path().pointAtPercent(0.5)
            f = p.font()
            f.setPointSize(8)
            p.setFont(f)
            fm = p.fontMetrics()
            text = label if len(label) <= 28 else label[:27] + "…"
            w = fm.horizontalAdvance(text) + 8
            h = fm.height() + 2
            rect = QRectF(mid.x() - w / 2, mid.y() - h / 2, w, h)
            p.setBrush(QBrush(QColor(bg)))
            p.setPen(QPen(QColor(border), 1))
            p.drawRoundedRect(rect, 3, 3)
            p.setPen(QColor(fg))
            p.drawText(rect, Qt.AlignCenter, text)


# ── scene + view ─────────────────────────────────────────────────────────────
class DesignerScene(QGraphicsScene):
    def __init__(self, graph: Graph, status_cb):
        super().__init__()
        self.graph = graph
        self.status = status_cb
        self.node_items: dict[str, NodeItem] = {}
        self.overlay: RuntimeOverlay | None = None
        self.revise = None
        # Called after every structural rebuild (set by the window) so the view
        # can auto-fit. Unset during construction so the very first rebuild —
        # before the view exists / is sized — doesn't try to fit.
        self.on_rebuilt = None
        self.setBackgroundBrush(QBrush(QColor(BG)))
        self.rebuild()

    def rebuild(self):
        self.clear()
        self.node_items.clear()
        # keep each agent's LLM fallback numbers contiguous + in sync with the
        # current links (covers connect / delete / unlink / drag / load).
        self.graph.renumber_llm_fallbacks()
        try:
            self.revise = graph_codegen.analyze(self.graph).get("revise_edge")
        except Exception:  # noqa: BLE001
            self.revise = None
        for n in self.graph.nodes.values():
            item = NodeItem(n)
            self.addItem(item)
            self.node_items[n.id] = item
            item.update_tooltip()
        for e in self.graph.edges:
            s, d = self.node_items.get(e.src), self.node_items.get(e.dst)
            if s and d:
                self.addItem(EdgeItem(self, e, s, d))
        self.update()
        if self.on_rebuilt is not None:
            self.on_rebuilt()

    def node_at_scene(self, pt: QPointF) -> NodeItem | None:
        for item in self.items(pt):
            if isinstance(item, NodeItem):
                return item
        return None

    def edge_at_scene(self, pt: QPointF) -> "EdgeItem | None":
        for item in self.items(pt):
            if isinstance(item, EdgeItem):
                return item
        return None

    def port_at(self, pt: QPointF) -> NodeItem | None:
        for item in self.node_items.values():
            if (pt - item.out_port()).manhattanLength() <= (PORT_R + 6) * 2:
                d = pt - item.out_port()
                if d.x() ** 2 + d.y() ** 2 <= (PORT_R + 6) ** 2:
                    return item
        return None

    def refresh_overlay(self):
        for item in self.node_items.values():
            item.update_tooltip()
            item.update()
        for item in self.items():
            if isinstance(item, EdgeItem):
                item.update()


class DesignerView(QGraphicsView):
    def __init__(self, scene: DesignerScene, window):
        super().__init__(scene)
        self.win = window
        self.setRenderHint(QPainter.Antialiasing, True)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        # Left-drag on empty canvas = rubber-band multi-select; left-drag on a node
        # moves it (and the whole selection if it's part of one). Panning is on the
        # middle mouse button (see mousePressEvent).
        self.setDragMode(QGraphicsView.RubberBandDrag)
        self.setRubberBandSelectionMode(Qt.IntersectsItemShape)
        self.setBackgroundBrush(QBrush(QColor(BG)))
        self._panning = False
        self._link_src = None
        self._temp = None
        self._last = QPointF()
        self.setMinimumSize(640, 480)

    def _scene_pos(self, event):
        return self.mapToScene(event.position().toPoint())

    def drawBackground(self, painter, rect):
        super().drawBackground(painter, rect)
        step = GRID_STEP
        left = int(rect.left()) - (int(rect.left()) % step)
        top = int(rect.top()) - (int(rect.top()) % step)
        painter.setPen(QPen(QColor(GRID), 1))
        x = left
        while x < rect.right():
            painter.drawLine(int(x), int(rect.top()), int(x), int(rect.bottom()))
            x += step
        y = top
        while y < rect.bottom():
            painter.drawLine(int(rect.left()), int(y), int(rect.right()), int(y))
            y += step

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Keep the whole graph in view as the window grows/shrinks (auto-fit on).
        self.win.autofit_now()

    def wheelEvent(self, event):
        self.scale(1.15 ** (event.angleDelta().y() / 120.0),
                   1.15 ** (event.angleDelta().y() / 120.0))
        # A manual zoom means "I want to control the view" — stop auto-fitting so
        # the zoom isn't undone on the next change/resize.
        self.win.set_autofit(False)

    def mousePressEvent(self, event):
        sc = self.scene()
        pt = self._scene_pos(event)
        # Middle button pans (left-drag is reserved for rubber-band select / move).
        if event.button() == Qt.MiddleButton:
            self._panning = True
            self._last = event.position()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        if event.button() == Qt.LeftButton:
            port = sc.port_at(pt)
            if port is not None:                     # drag from a port -> create a link
                self._link_src = port.node.id
                self._temp = sc.addPath(QPainterPath(pt),
                                        QPen(QColor("#1565C0"), 2, Qt.DashLine))
                self._temp.setZValue(5)
                event.accept()
                return
        # Empty space -> RubberBandDrag draws a marquee and selects the nodes it
        # covers; on a node -> Qt moves it (and the whole selection if it's in one).
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._link_src is not None and self._temp is not None:
            src = self.scene().node_items[self._link_src]
            path = QPainterPath(src.out_port())
            path.lineTo(self._scene_pos(event))
            self._temp.setPath(path)
            event.accept()
            return
        if self._panning:
            delta = event.position() - self._last
            self._last = event.position()
            if delta.x() or delta.y():
                # actual pan movement (not a bare click) → stop auto-fitting so
                # the user's scrolled position isn't snapped back
                self.win.set_autofit(False)
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - int(delta.x()))
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - int(delta.y()))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        sc = self.scene()
        if self._link_src is not None:
            if self._temp is not None:
                sc.removeItem(self._temp)
                self._temp = None
            target = sc.node_at_scene(self._scene_pos(event))
            src_id = self._link_src
            self._link_src = None
            if target is not None and target.node.id != src_id:
                err = sc.graph.add_edge(src_id, target.node.id)
                if err:
                    self.win.set_status(err)
                else:
                    warn = sc.graph.link_warning(src_id, target.node.id)
                    sc.rebuild()
                    self.win.set_status(
                        f"Linked {sc.graph.nodes[src_id].name} → {target.node.name}.")
                    if warn:
                        QMessageBox.warning(self, "Duplicate link", warn)
            event.accept()
            return
        if self._panning:
            self._panning = False
            self.setCursor(Qt.ArrowCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)
        # a drag that moved node(s) updates their x/y without a rebuild — record it
        # for undo (no-op if nothing actually changed).
        self.win._record_history()

    def mouseDoubleClickEvent(self, event):
        node = self.scene().node_at_scene(self._scene_pos(event))
        if node is not None:
            err = open_config_dialog(self, node.node)
            self.scene().rebuild()
            if err:
                QMessageBox.warning(self, "Invalid value", err)
            return
        edge = self.scene().edge_at_scene(self._scene_pos(event))
        if edge is not None:                          # double-click a link to configure
            self.win.configure_edge(edge.edge)
            return
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            self.win.delete_selection()
            return
        if event.matches(QKeySequence.SelectAll):     # Ctrl+A → select every node
            self.win.select_all()
            return
        if event.matches(QKeySequence.Copy):          # Ctrl+C → copy selected node(s)
            self.win.copy_selection()
            return
        if event.matches(QKeySequence.Paste):         # Ctrl+V → paste a copy
            self.win.paste_clipboard()
            return
        super().keyPressEvent(event)

    def contextMenuEvent(self, event):
        sc = self.scene()
        pt = self.mapToScene(event.pos())
        node = sc.node_at_scene(pt)
        edge = None
        if node is None:
            for item in sc.items(pt):
                if isinstance(item, EdgeItem):
                    edge = item
                    break
        menu = QMenu(self)
        if node is not None:
            n = node.node
            menu.addAction(t("Configure '%s'...") % n.name,
                           lambda: self.win.configure(n))
            menu.addAction(t("Check Code"), lambda: self.win.check_node_code(n))
            if n.kind in AGENT_KINDS:
                sub = menu.addMenu(t("Add && link a module"))
                kinds = (["llm"] if n.kind == "router"
                         else ["llm", "tool", "skill", "prompt", "rag", "memory", "mcp"])
                kinds += ["eval", "gui", "schedule"]
                for k in kinds:
                    sub.addAction(t("Add %s") % t(KIND_LABELS[k]),
                                  lambda checked=False, kk=k: self.win.add_linked(n, kk))
                menu.addSeparator()
            menu.addAction(t("Delete module"), lambda: self.win.delete_node(n))
            menu.addAction(t("Delete all its links"), lambda: self.win.unlink_node(n))
        elif edge is not None:
            menu.addAction(t("Configure link..."),
                           lambda: self.win.configure_edge(edge.edge))
            menu.addAction(t("Delete link"), lambda: self.win.delete_edge(edge.edge))
        else:
            for k, label in KIND_LABELS.items():
                menu.addAction(t("Add %s here") % t(label),
                               lambda checked=False, kk=k, p=pt:
                               self.win.add_node(kk, int(p.x()), int(p.y())))
        menu.exec(event.globalPos())


# ── debug-run bridge (worker thread → GUI thread) ───────────────────────────
class _DebugBridge(QObject):
    trace = Signal(dict)
    status = Signal(str)
    finished = Signal(object, object)
    turn_done = Signal(object, object)   # (result, error) for a chat turn (#4)
    ask_review = Signal(str, str, object)   # (prompt, content, choices|None) — route mode
    ask_confirm = Signal(str, object)   # (tool_name, args) high-risk tool confirm
    notify = Signal(str, str)        # (title, message) → QMessageBox on GUI thread
    designed_graph = Signal(object, object)  # (Graph, name) from the Designer agent thread

    def __init__(self):
        super().__init__()
        self._review_box = {}
        self._review_event = threading.Event()
        self._confirm_box = {}
        self._confirm_event = threading.Event()

    def request_review(self, prompt, content, choices=None):
        self._review_box = {}
        self._review_event.clear()
        self.ask_review.emit(prompt, content, choices)
        self._review_event.wait()
        # a route-mode default (first branch) vs a gate default (approve)
        return self._review_box or {"decision": (choices[0] if choices else "approve"),
                                    "content": content, "feedback": ""}

    def request_confirm(self, tool_name, args):
        """Called on the worker thread; pops a confirm dialog on the GUI thread and
        blocks for the outcome dict {decision, args, remember}."""
        self._confirm_box = {}
        self._confirm_event.clear()
        self.ask_confirm.emit(tool_name, args)
        self._confirm_event.wait()
        return self._confirm_box or {"decision": "deny"}


# ── main window ──────────────────────────────────────────────────────────────
class CanvasWindow(QWidget):
    def __init__(self, open_path: str | None = None):
        super().__init__()
        _i18n_set(get_language())            # honor the saved UI language (welcome sets it too)
        self.setWindowTitle(t("Visual Agent Designer (Qt)"))
        self.resize(1180, 760)
        self.graph = Graph()
        self._path: str | None = None      # current project/graph file (shown in title)
        self.generated_dir: str | None = None
        self._agent_name = "my_pipeline"   # last name entered in the Generate prompt
        self._debug_running = False
        self._debug_mod = None
        self._debug_cancel_pending = False
        self._debug_seq = 0
        self._debug_api_key = ""           # session-cached key for Debug Run (not persisted)
        # Chat run (#4): a persisted agent module reused across turns so HISTORY
        # accumulates; regenerated only when the graph changes.
        self._chat_mod = None
        self._chat_active_mod = None       # the module the in-flight turn runs on
        self._chat_dir: str | None = None
        self._chat_graph_sig: str | None = None
        self._chat_running = False
        self._autofit = True               # keep the whole graph in view by default
        self._hint_labels = []             # muted labels recolored on theme switch
        # Match the canvas colors to the saved theme before building the scene/view
        # (the app palette is applied app-wide at startup by welcome.run()).
        set_canvas_theme(get_theme())

        self.scene = DesignerScene(self.graph, self.set_status)
        self.view = DesignerView(self.scene, self)
        # Auto-fit after any structural rebuild (add/delete/link/load/insert…).
        # Wired after construction so the scene's initial rebuild doesn't fit
        # before the view is sized; drags don't rebuild, so they're never fought.
        self.scene.on_rebuilt = self._after_rebuild
        # Selecting a single module drills into it in the run-trace inspector.
        self.scene.selectionChanged.connect(self._on_selection_changed)

        # CanvasWindow is a QWidget (not a QMainWindow). Add the menu bar as the
        # top row of a vertical layout rather than via QLayout.setMenuBar(): the
        # latter proved fragile here — the submenu's C++ object was getting
        # deleted on show(), silently emptying the menu.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.menubar = self._build_menubar()
        outer.addWidget(self.menubar)

        # Palette | workspace live in a draggable QSplitter so the user can resize
        # (or shrink) the left module palette. Panes can't be collapsed to zero.
        # This top-level splitter stays TWO-paned (palette | workspace); the run
        # panels go in a nested side dock so the canvas never loses this divider.
        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(6)
        self.splitter.addWidget(self._build_palette())

        # Canvas | side dock: a nested splitter inside the workspace pane. The
        # side dock stacks the run panels (chat over trace), each independently
        # hideable and hidden until used, so the canvas keeps full width.
        self.workspace_splitter = QSplitter(Qt.Horizontal)
        self.workspace_splitter.setChildrenCollapsible(False)
        self.workspace_splitter.setHandleWidth(6)
        self.workspace_splitter.addWidget(self.view)

        self.side_dock = QSplitter(Qt.Vertical)
        self.side_dock.setChildrenCollapsible(False)
        self.side_dock.setHandleWidth(6)
        self.chat_panel = ChatPanel()        # #4 multi-turn chat run
        self.trace_panel = TracePanel()      # #1/#2 timeline + inspector
        self.side_dock.addWidget(self.chat_panel)
        self.side_dock.addWidget(self.trace_panel)
        self.chat_panel.setVisible(False)
        self.trace_panel.setVisible(False)
        self.side_dock.setVisible(False)
        self.workspace_splitter.addWidget(self.side_dock)
        self.workspace_splitter.setStretchFactor(0, 1)   # canvas absorbs space
        self.workspace_splitter.setStretchFactor(1, 0)   # side dock keeps width

        # Replay transport (#3) lives inside the trace panel.
        self._replay_stage_names: list[str] = []
        self.replay_bar = ReplayBar(self._replay_reset, self._replay_feed,
                                    self._replay_after)
        self.trace_panel.attach_replay_bar(self.replay_bar)

        # Chat panel signals → run orchestration.
        self.chat_panel.send.connect(self._chat_send)
        self.chat_panel.stop.connect(self._chat_stop)
        self.chat_panel.new_session.connect(self._chat_new_session)
        self.chat_panel.load_session.connect(self._chat_load_session)

        right_widget = QWidget()
        right = QVBoxLayout(right_widget)
        right.setContentsMargins(0, 0, 0, 0)
        right.addWidget(self.workspace_splitter, 1)
        self.status_label = QLabel(HINT)
        self.status_label.setStyleSheet(
            f"color:{canvas_colors()['status']}; padding:4px;")
        self.status_label.setWordWrap(True)
        right.addWidget(self.status_label, 0)
        self.splitter.addWidget(right_widget)

        self.splitter.setStretchFactor(0, 0)   # palette keeps its width on resize
        self.splitter.setStretchFactor(1, 1)   # workspace absorbs extra space
        self.splitter.setSizes([250, 950])      # initial split

        body = QHBoxLayout()
        body.setContentsMargins(8, 4, 8, 8)
        body.addWidget(self.splitter)
        outer.addLayout(body, 1)
        self._restyle_theme_labels()       # color muted labels for the active theme

        self._bridge = _DebugBridge()
        self._bridge.designed_graph.connect(self._apply_designed_graph)
        self._bridge.trace.connect(self._on_trace)
        self._bridge.status.connect(lambda s: self.set_status(str(s)[:90]))
        self._bridge.finished.connect(self._debug_done)
        self._bridge.turn_done.connect(self._chat_turn_done)
        self._bridge.ask_review.connect(self._show_review)
        self._bridge.ask_confirm.connect(self._show_confirm)
        self._bridge.notify.connect(
            lambda title, msg: QMessageBox.information(self, title, msg))

        if open_path:
            self.load_path(open_path)

        # Snapshot of the last saved/loaded graph; closeEvent compares against it
        # to decide whether to offer to save unsaved work.
        self._clean_snapshot = self._snapshot()
        self._update_title()
        self._reset_history()          # undo/redo baseline = the initial graph

    # ── unsaved-changes tracking ─────────────────────────────────────────────
    def _snapshot(self) -> str:
        """A canonical serialization of the current graph (incl. node positions
        and props) for cheap dirty-detection."""
        return json.dumps(self.graph.to_dict(), sort_keys=True)

    def _is_dirty(self) -> bool:
        return self._snapshot() != getattr(self, "_clean_snapshot", None)

    def _update_title(self) -> None:
        """Window title = the current project/graph name (its filename, or
        'Untitled' when never saved) + a '•' when there are unsaved changes."""
        name = (os.path.splitext(os.path.basename(self._path))[0]
                if self._path else "Untitled")
        dirty = "  •" if self._is_dirty() else ""
        self.setWindowTitle(f"{name}{dirty} — Visual Agent Designer")

    # ── undo / redo history ──────────────────────────────────────────────────
    # A stack of full-graph snapshots (json of graph.to_dict()); the LAST entry is
    # always the current state. Structural edits record via _after_rebuild; node
    # drags record on mouse-release. Cheap and robust (whole-graph snapshots) —
    # fine for graph-scale sizes.
    def _reset_history(self) -> None:
        """Start a fresh history baseline at the current state (after load/new)."""
        self._undo = [self._snapshot()]
        self._redo = []
        self._sync_undo_actions()

    def _record_history(self) -> None:
        """Append the current state if it changed; clears the redo stack. No-op while
        restoring, or before the baseline is set."""
        if getattr(self, "_history_lock", False):
            return
        undo = getattr(self, "_undo", None)
        if undo is None:
            return
        cur = self._snapshot()
        if undo and undo[-1] == cur:
            return
        undo.append(cur)
        if len(undo) > 200:            # bound memory; drop the oldest
            undo.pop(0)
        self._redo = []
        self._sync_undo_actions()

    def _restore_snapshot(self, snap: str) -> None:
        """Replace the live graph with a snapshot and repaint (mirrors load_path's
        in-place field copy). Guarded so the rebuild it triggers records no history."""
        self._history_lock = True
        try:
            loaded = Graph.from_dict(json.loads(snap))
            self.graph.nodes = loaded.nodes
            self.graph.edges = loaded.edges
            self.graph.state_schema = loaded.state_schema
            self.graph.type_defs = dict(getattr(loaded, "type_defs", {}) or {})
            self.graph.recursion_limit = loaded.recursion_limit
            self.graph.storage = dict(getattr(loaded, "storage", {}) or {})
            self.graph._counter = loaded._counter
            self.scene.graph = self.graph
            self.scene.rebuild()
            self._update_title()
        finally:
            self._history_lock = False

    def on_undo(self) -> None:
        undo = getattr(self, "_undo", [])
        if len(undo) < 2:
            self.set_status(t("Nothing to undo.")); return
        self._redo.append(undo.pop())      # move current onto the redo stack
        self._restore_snapshot(undo[-1])   # restore the previous state
        self._sync_undo_actions()
        self.set_status(t("Undo."))

    def on_redo(self) -> None:
        redo = getattr(self, "_redo", [])
        if not redo:
            self.set_status(t("Nothing to redo.")); return
        snap = redo.pop()
        self._undo.append(snap)
        self._restore_snapshot(snap)
        self._sync_undo_actions()
        self.set_status(t("Redo."))

    def _sync_undo_actions(self) -> None:
        u = getattr(self, "act_undo", None)
        r = getattr(self, "act_redo", None)
        if u is not None:
            u.setEnabled(len(getattr(self, "_undo", [])) >= 2)
        if r is not None:
            r.setEnabled(bool(getattr(self, "_redo", [])))

    def closeEvent(self, event) -> None:
        if not self._is_dirty():
            super().closeEvent(event)
            return
        box = QMessageBox(self)
        box.setWindowTitle("Save changes?")
        box.setText("Save your agent design before closing?")
        box.setInformativeText("Your unsaved changes will be lost otherwise.")
        box.setStandardButtons(
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
        box.setDefaultButton(QMessageBox.Save)
        ans = box.exec()
        if ans == QMessageBox.Cancel:
            event.ignore()
            return
        if ans == QMessageBox.Save and not self.on_save():
            # user backed out of the file dialog (or the save failed) — don't
            # silently discard their work
            event.ignore()
            return
        super().closeEvent(event)

    # ── menu bar ─────────────────────────────────────────────────────────────
    def _build_menubar(self) -> QMenuBar:
        mb = QMenuBar(self)
        # Keep a Python reference to each QMenu: QMenuBar.addMenu() hands the
        # wrapper back to Python, and without a live reference it gets garbage
        # collected (taking the C++ menu with it) once GC runs in the event
        # loop — the menu would silently vanish at runtime.
        self._graph_menu = mb.addMenu(t("&Graph"))
        act_save = self._graph_menu.addAction(t("&Save..."))
        act_save.setShortcut("Ctrl+S")
        act_save.triggered.connect(self.on_save)
        act_load = self._graph_menu.addAction(t("&Load..."))
        act_load.setShortcut("Ctrl+O")
        act_load.triggered.connect(self.on_load)
        act_merge = self._graph_menu.addAction(t("&Merge graph from..."))
        act_merge.setToolTip("Load another graph/bundle and merge it onto the "
                             "current canvas.")
        act_merge.triggered.connect(self.on_add_graph)
        self._graph_menu.addSeparator()
        act_state = self._graph_menu.addAction(t("Edit Shared &State..."))
        act_state.setToolTip("Declare typed shared-state fields the agent stages "
                             "can read and write (saved with the graph).")
        act_state.triggered.connect(self.on_edit_state_schema)
        act_types = self._graph_menu.addAction(t("Define &Types..."))
        act_types.setToolTip("Define custom / nested state types (JSON-Schema records "
                             "+ how they merge). Use them as a shared-state field's "
                             "type or list[Type] (saved with the graph).")
        act_types.triggered.connect(self.on_edit_types)
        act_storage = self._graph_menu.addAction(t("Storage / &Persistence..."))
        act_storage.setToolTip("Choose where the generated agent stores memory "
                               "(chat sessions) + checkpoints: disk, SQLite, or "
                               "PostgreSQL (saved with the graph).")
        act_storage.triggered.connect(self.on_edit_storage)

        self._edit_menu = mb.addMenu(t("&Edit"))
        self.act_undo = self._edit_menu.addAction(t("&Undo"))
        self.act_undo.setShortcut("Ctrl+Z")
        self.act_undo.triggered.connect(self.on_undo)
        self.act_redo = self._edit_menu.addAction(t("&Redo"))
        self.act_redo.setShortcuts(["Ctrl+Y", "Ctrl+Shift+Z"])
        self.act_redo.triggered.connect(self.on_redo)
        self._sync_undo_actions()

        self._gen_menu = mb.addMenu(t("&Generate"))
        # Code-style submenu: single-file (portable, the legacy default on Ctrl+G)
        # or a Python package (runtime/ modules + a thin agent.py). Same agent
        # behaviour either way — purely how the code is laid out on disk.
        _gen_sub = self._gen_menu.addMenu(t("&Generate Code"))
        act_gen = _gen_sub.addAction(t("as &Single File (portable)"))
        act_gen.setShortcut("Ctrl+G")
        act_gen.setToolTip("One self-contained agent.py with the runtime inlined "
                           "— the portable default.")
        act_gen.triggered.connect(lambda *_: self.on_generate("single"))
        act_gen_pkg = _gen_sub.addAction(t("as Python &Package (modules)"))
        act_gen_pkg.setToolTip("Split the runtime into a runtime/ package + a thin "
                               "agent.py engine — a conventional, editable project.")
        act_gen_pkg.triggered.connect(lambda *_: self.on_generate("package"))
        self.act_run = self._gen_menu.addAction(t("&Run GUI Agent"))
        self.act_run.triggered.connect(self.on_run)
        self.act_run_sched = self._gen_menu.addAction(t("Run &Scheduler (ambient)"))
        self.act_run_sched.setToolTip("Launch scheduler.py in its own console — the real "
                                      "ambient runner (needs a Schedule module linked to "
                                      "the entry agent; set the API key in config.json).")
        self.act_run_sched.triggered.connect(self.on_run_scheduler)
        self.act_debug_sched = self._gen_menu.addAction(t("Debug Sc&heduler (live overlay)"))
        self.act_debug_sched.setToolTip("Preview each Schedule job ONCE in the canvas with "
                                        "the live trace overlay (uses your debug API key).")
        self.act_debug_sched.triggered.connect(self.on_debug_scheduler)
        self.act_debug = self._gen_menu.addAction(t("&Debug Run (live overlay)"))
        self.act_debug.triggered.connect(self.on_debug_run)
        self.act_chat = self._gen_menu.addAction(t("&Chat Run (multi-turn)..."))
        self.act_chat.setToolTip(
            "Open a conversational run panel: multi-turn chat against the agent, "
            "saved sessions, and ↑/↓ input recall.")
        self.act_chat.triggered.connect(self.on_chat_run)
        self.act_replay = self._gen_menu.addAction(t("Replay &Trace..."))
        self.act_replay.setToolTip(
            "Open a saved traces/*.jsonl run and re-animate it on the canvas "
            "with play / step / scrub controls.")
        self.act_replay.triggered.connect(self.on_open_trace)
        self._gen_menu.addSeparator()
        self._gen_menu.addAction(t("&Compile (PyInstaller)")).triggered.connect(
            self.on_compile)
        self._gen_menu.addAction(t("&Open Output Folder")).triggered.connect(
            self.on_open_folder)
        self._gen_menu.addAction(t("&Dump System Prompts...")).triggered.connect(
            self.on_dump_prompts)

        self._tools_menu = mb.addMenu(t("&AI Assistant"))
        self.act_tool_gen = self._tools_menu.addAction(t("&Tool Generator..."))
        self.act_tool_gen.setShortcut("Ctrl+T")
        self.act_tool_gen.setToolTip("Chat with the coding agent to write & save "
                                     "Python tools into the shared library.")
        self.act_tool_gen.triggered.connect(self.on_open_tool_generator)
        self.act_designer = self._tools_menu.addAction(t("&Designer Agent..."))
        self.act_designer.setShortcut("Ctrl+D")
        self.act_designer.setToolTip("Chat with the graph Designer agent — describe an "
                                     "agent system and it designs + renders the graph on "
                                     "the canvas (separate sessions from the Tool Generator).")
        self.act_designer.triggered.connect(self.on_open_designer)

        # Patterns: one action per preset — picking it inserts that pattern
        # directly (selection + insert combined into a single click).
        self._patterns_menu = mb.addMenu(t("&Patterns"))
        self._patterns_menu.setToolTipsVisible(True)
        self.pattern_actions = {}
        for pid, spec in patterns.PATTERNS.items():
            act = self._patterns_menu.addAction(t(spec["label"]))
            act.setToolTip(spec["description"])
            act.triggered.connect(
                lambda checked=False, pid=pid: self.insert_pattern(pid))
            self.pattern_actions[pid] = act

        self._view_menu = mb.addMenu(t("&View"))
        self.act_autofit = self._view_menu.addAction(t("&Auto-fit view"))
        self.act_autofit.setCheckable(True)
        self.act_autofit.setChecked(self._autofit)
        self.act_autofit.setToolTip(
            "Keep the whole graph in view automatically (on resize and edits). "
            "Zooming or panning by hand turns this off.")
        self.act_autofit.toggled.connect(self.set_autofit)
        act_fit = self._view_menu.addAction(t("&Fit to view now"))
        act_fit.setShortcut("Ctrl+0")
        act_fit.triggered.connect(self.fit_view)
        self._view_menu.addSeparator()
        self.act_trace_panel = self._view_menu.addAction(t("Show &run trace panel"))
        self.act_trace_panel.setCheckable(True)
        self.act_trace_panel.setChecked(False)
        self.act_trace_panel.setToolTip(
            "Show the live run-trace panel: a step-by-step event timeline plus a "
            "per-module inspector. Opens automatically on a Debug Run.")
        self.act_trace_panel.toggled.connect(self._toggle_trace_panel)
        self.act_chat_panel = self._view_menu.addAction(t("Show &chat run panel"))
        self.act_chat_panel.setCheckable(True)
        self.act_chat_panel.setChecked(False)
        self.act_chat_panel.setToolTip(
            "Show the multi-turn chat run panel. Opens automatically on a Chat Run.")
        self.act_chat_panel.toggled.connect(self._toggle_chat_panel)

        # Estimation: read-only design review. Estimate Prompts / Graph / Tool /
        # All. Phase 0: Estimate Graph is deterministic (analyze + metrics); the
        # others arrive in later phases (LLM-judged prompt/tool checks).
        self._estimation_menu = mb.addMenu(t("&Estimation"))
        self._estimation_menu.setToolTipsVisible(True)
        est_prompts = self._estimation_menu.addAction(t("Estimate &Prompts"))
        est_prompts.setToolTip("Check each agent's system prompt for clarity and "
                               "internal contradictions (coming in a later phase).")
        est_prompts.triggered.connect(self.on_estimate_prompts)
        est_graph = self._estimation_menu.addAction(t("Estimate &Graph"))
        est_graph.setToolTip("Review the graph's structure, topology and cost "
                             "shape (errors, warnings, metrics).")
        est_graph.triggered.connect(self.on_estimate_graph)
        est_tool = self._estimation_menu.addAction(t("Estimate &Tool"))
        est_tool.setToolTip("Review the linked tools' docstrings and usage "
                            "(coming in a later phase).")
        est_tool.triggered.connect(self.on_estimate_tools)
        est_all = self._estimation_menu.addAction(t("Estimate &All"))
        est_all.setToolTip("Run every estimate and summarise (coming in a later phase).")
        est_all.triggered.connect(self.on_estimate_all)

        # Configure: app settings (LLM key, Theme). Presented as a right-aligned
        # gear button in the menu bar's corner rather than a text menu — the
        # familiar "settings" affordance, and it keeps the left side for the
        # workflow menus. The menu itself is built standalone and popped up by the
        # gear (a live ref on self keeps both from being GC'd, like the menus above).
        self._config_menu = QMenu(self)
        act_llm = self._config_menu.addAction(t("&LLM API Key / Model / URL..."))
        act_llm.setToolTip("Set the LLM used by the Tool Generator and Estimation: "
                           "API key, model, and base URL.")
        act_llm.triggered.connect(self.on_edit_llm_settings)
        theme_menu = self._config_menu.addMenu(t("&Theme"))
        self._theme_group = QActionGroup(self)
        self._theme_group.setExclusive(True)
        self.theme_actions = {}
        saved = get_theme()
        for label, tname in (("&Dark", "dark"), ("&Light (bright)", "light")):
            act = theme_menu.addAction(t(label))
            act.setCheckable(True)
            act.setChecked(tname == saved)
            self._theme_group.addAction(act)
            act.triggered.connect(lambda checked=False, n=tname: self.set_theme(n))
            self.theme_actions[tname] = act

        gear = QToolButton(mb)
        gear.setText("⚙")            # ⚙ — a monochrome glyph that follows the theme
        gf = gear.font()
        gf.setPointSize(14)
        gear.setFont(gf)
        gear.setToolTip(t("Configure — LLM settings & theme"))
        gear.setMenu(self._config_menu)
        gear.setPopupMode(QToolButton.InstantPopup)
        gear.setAutoRaise(True)           # flat until hovered — a modern, chrome-less look
        gear.setCursor(Qt.PointingHandCursor)
        # drop the little popup arrow InstantPopup adds so only the gear shows
        gear.setStyleSheet("QToolButton { border: none; padding: 1px 8px; }"
                           " QToolButton::menu-indicator { image: none; width: 0; }")
        self._config_gear = gear
        mb.setCornerWidget(gear, Qt.TopRightCorner)
        return mb

    def on_open_tool_generator(self) -> None:
        # Lazy import: keeps the coding agent + its LLM deps off the canvas
        # startup path until the Tool Generator is actually opened.
        from canvas_qt.tool_generator import open_tool_generator
        open_tool_generator()

    def on_open_designer(self) -> None:
        # The graph Designer agent: separate agent/sessions; renders onto THIS canvas.
        from canvas_qt.tool_generator import open_designer
        open_designer(self)

    # ── Estimation menu ────────────────────────────────────────────────────
    # Each action runs the estimate on a worker thread (LLM calls must not freeze
    # the canvas) with a Cancel dialog. use_llm=True adds the grounded LLM layer
    # when an API key is set; without a key each estimate degrades to its
    # deterministic checks plus a "skipped — no API key" note.
    def _run_estimation(self, make_fn) -> None:
        # Non-modal streaming window: results appear bit-by-bit and the canvas
        # stays fully editable while the estimate (incl. LLM calls) runs. The
        # 'Fix with AI…' flow proposes prompt rewrites (HITL-confirmed) and applies
        # them through _apply_estimation_fix (which self-checks + reverts).
        import estimation
        from canvas_qt.estimation_ui import run_estimation
        self._ensure_llm_configured()
        jumpable = {n.name for n in self.graph.nodes.values()}
        run_estimation(
            self, make_fn, on_jump=self._select_node_by_name, jumpable=jumpable,
            proposer=lambda findings, c: estimation.propose_fixes(
                self.graph, findings, cancel_event=c),
            applier=self._apply_estimation_fix,
            fixable=lambda f: estimation.is_fixable(self.graph, f))

    def _apply_estimation_fix(self, proposal):
        """Apply an AI-proposed fix to the live graph (with analyze() self-check +
        auto-revert), then rebuild the canvas. Returns (ok, message)."""
        import estimation
        ok, msg = estimation.apply_fix(self.graph, proposal)
        self.scene.rebuild()          # reflect the edit (or the revert) on canvas
        self.set_status(msg)
        return ok, msg

    def _open_llm_settings(self) -> bool:
        """Open the LLM settings dialog (API key / model / base URL — the same
        config.json the Tool Generator and Estimation use). Returns True if the
        user saved. Shared by the Configure menu and the estimation no-key prompt."""
        from PySide6.QtWidgets import QDialog
        from canvas_qt.welcome import SettingsDialog
        dlg = SettingsDialog(self)
        if dlg.exec() == QDialog.Accepted:
            dlg.save()
            return True
        return False

    def on_edit_llm_settings(self) -> None:
        if self._open_llm_settings():
            self.set_status("LLM settings saved (API key / model / base URL).")

    def _ensure_llm_configured(self) -> None:
        """The estimation LLM layer needs the API key / base URL (config.json, the
        same settings the Tool Generator uses). If none is set, open the settings
        dialog so the user can enter it before the run; if they decline, the
        estimate runs deterministic-only and notes the LLM pass was skipped."""
        import estimation
        if not estimation.llm_available():
            self._open_llm_settings()

    def _select_node_by_name(self, name: str) -> None:
        """Jump-to-node from an estimation finding: select the named node and
        centre the view on it (brings the canvas forward from the report)."""
        node = next((n for n in self.graph.nodes.values() if n.name == name), None)
        item = self.scene.node_items.get(node.id) if node else None
        if item is None:
            return
        self.scene.clearSelection()
        item.setSelected(True)
        self.view.centerOn(item)
        self.raise_()
        self.activateWindow()

    def on_estimate_graph(self) -> None:
        from estimation import estimate_graph
        self._run_estimation(
            lambda c, e: estimate_graph(self.graph, emit=e, cancel_event=c, use_llm=True))

    def on_estimate_prompts(self) -> None:
        from estimation import estimate_prompts
        self._run_estimation(
            lambda c, e: estimate_prompts(self.graph, emit=e, cancel_event=c, use_llm=True))

    def on_estimate_tools(self) -> None:
        from estimation import estimate_tools
        self._run_estimation(
            lambda c, e: estimate_tools(self.graph, emit=e, cancel_event=c, use_llm=True))

    def on_estimate_all(self) -> None:
        from estimation import estimate_all
        self._run_estimation(
            lambda c, e: estimate_all(self.graph, emit=e, cancel_event=c, use_llm=True))

    def on_edit_state_schema(self) -> None:
        if open_state_schema_dialog(self, self.graph):
            n = len(self.graph.state_schema)
            self.set_status(f"Shared state: {n} field(s) defined."
                            if n else "Shared state: no fields defined.")

    def on_edit_types(self) -> None:
        if open_type_defs_dialog(self, self.graph):
            n = len(self.graph.type_defs)
            self.set_status(f"Custom types: {n} defined."
                            if n else "Custom types: none defined.")

    def on_edit_storage(self) -> None:
        if open_storage_dialog(self, self.graph):
            b = (self.graph.storage or {}).get("backend") or "disk"
            self.set_status(f"Storage backend: {b}.")

    # ── palette ──────────────────────────────────────────────────────────────
    def _build_palette(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        # Resizable via the splitter (not a fixed width); keep a sensible minimum
        # so the buttons stay usable when the user drags the divider in.
        scroll.setMinimumWidth(170)
        host = QWidget()
        v = QVBoxLayout(host)

        def section(text):
            lbl = QLabel(text)
            f = lbl.font()
            f.setBold(True)
            lbl.setFont(f)
            # subtle full-width divider under each section header (theme-agnostic)
            lbl.setStyleSheet("QLabel { padding-top:8px; padding-bottom:3px;"
                              " border-bottom:1px solid rgba(128,128,128,0.45); }")
            v.addWidget(lbl)

        section(t("Add module:"))
        grid = QGridLayout()
        for i, (kind, label) in enumerate(KIND_LABELS.items()):
            b = QPushButton(t(label))
            c = QColor(KIND_COLORS[kind])
            b.setStyleSheet(
                "QPushButton {{ background-color:{bg}; border:1px solid {bd};"
                " border-radius:6px; padding:6px 8px; color:#1a1a1a;"
                " font-weight:bold; }}"
                "QPushButton:hover {{ background-color:{hv}; }}"
                "QPushButton:pressed {{ background-color:{pr}; }}".format(
                    bg=c.name(), bd=c.darker(128).name(),
                    hv=c.lighter(106).name(), pr=c.darker(112).name()))
            # every button shows its node's silhouette (the real shape_path scaled
            # down) so the palette doubles as a legend for the shape language
            pm = QPixmap(22, 12)
            pm.fill(Qt.transparent)
            pp = QPainter(pm)
            pp.setRenderHint(QPainter.Antialiasing, True)
            pen = QPen(c.darker(170), 1)
            pen.setCosmetic(True)                # crisp 1px outline despite scaling
            pp.setPen(pen)
            pp.setBrush(QBrush(c.darker(118)))
            pp.translate(1, 1)
            pp.scale(20.0 / NODE_W, 10.0 / NODE_H)
            pp.drawPath(shape_path(KIND_SHAPE.get(kind, "rect"),
                                   QRectF(0, 0, NODE_W, NODE_H)))
            pp.end()
            b.setIcon(QIcon(pm))
            b.setIconSize(QSize(20, 11))
            b.clicked.connect(lambda checked=False, k=kind: self.add_node(k))
            grid.addWidget(b, i // 2, i % 2)
        v.addLayout(grid)

        section("Pattern presets:")
        pat_hint = QLabel(
            "Pick a starter pattern from the Patterns menu (top) — it replaces "
            "the canvas with that preset.")
        pat_hint.setWordWrap(True)
        self._hint_labels.append(pat_hint)
        v.addWidget(pat_hint)

        section("Graph:")
        graph_hint = QLabel(
            "Use the Graph menu (top) to save, load or merge graphs. View → "
            "Auto-fit keeps the whole graph in view (Ctrl+0 fits once).")
        graph_hint.setWordWrap(True)
        self._hint_labels.append(graph_hint)
        v.addWidget(graph_hint)

        section("Generate:")
        gen_hint = QLabel(
            "Use the Generate menu (top) to generate, run, debug or compile. "
            "Add a GUI module and link it to the entry agent to get a PySide6 "
            "desktop GUI; otherwise the agent is headless (CLI).")
        gen_hint.setWordWrap(True)
        self._hint_labels.append(gen_hint)
        v.addWidget(gen_hint)

        section("Links (auto-styled):")
        for text, color in (("→ uses  (resource → agent)", "#616161"),
                            ("→ flows to  (agent → agent)", "#1565C0"),
                            ("↺ revise loop  (back-edge)", "#D32F2F")):
            lbl = QLabel(text)
            lbl.setStyleSheet(f"color:{color}; font-size:11px;")
            v.addWidget(lbl)
        v.addStretch(1)
        scroll.setWidget(host)
        return scroll

    # ── status / edits ───────────────────────────────────────────────────────
    def set_status(self, msg: str) -> None:
        self.status_label.setText(msg)

    def fit_view(self) -> None:
        # Re-entrancy guard: fitInView can toggle scrollbars, which resizes the
        # viewport and re-fires resizeEvent -> autofit_now -> fit_view. Without
        # this guard those nested calls oscillate (runaway zoom).
        if getattr(self, "_fitting", False):
            return
        rect = self.scene.itemsBoundingRect().adjusted(-60, -60, 60, 60)
        if rect.isEmpty():
            return
        self._fitting = True
        try:
            self.view.fitInView(rect, Qt.KeepAspectRatio)
        finally:
            self._fitting = False

    def autofit_now(self) -> None:
        """Fit the whole graph into view, but only while auto-fit is enabled AND
        the user isn't mid-drag. Refitting during a node drag caused a runaway:
        dragging grows the scene -> scrollbar toggles -> resizeEvent -> refit
        zooms out -> the node chases the cursor -> repeat (node 'flies away')."""
        if not getattr(self, "_autofit", False):
            return
        if self.scene.mouseGrabberItem() is not None:
            return                          # a node is being dragged — don't refit
        self.fit_view()

    def set_autofit(self, on: bool) -> None:
        """Turn auto-fit on/off. Turning it on fits immediately; the View-menu
        toggle is kept in sync (without re-triggering this). Manual zoom/pan
        calls set_autofit(False) so the user's view isn't snapped back."""
        self._autofit = bool(on)
        act = getattr(self, "act_autofit", None)
        if act is not None and act.isChecked() != self._autofit:
            act.blockSignals(True)
            act.setChecked(self._autofit)
            act.blockSignals(False)
        if self._autofit:
            self.fit_view()

    def _after_rebuild(self) -> None:
        self.autofit_now()
        self._update_title()          # refresh the unsaved-changes '•' on edits
        self._record_history()        # structural edits (add/delete/link/config) → undo

    # ── run-trace panel (#1/#2) ──────────────────────────────────────────────
    def _on_selection_changed(self) -> None:
        """Drill the inspector into the single selected module (or show the run
        summary when zero/multiple modules are selected)."""
        if not hasattr(self, "trace_panel"):
            return                         # selection changed before panel exists
        try:
            nodes = [it for it in self.scene.selectedItems()
                     if isinstance(it, NodeItem)]
        except RuntimeError:
            return                         # scene's C++ object torn down (shutdown)
        node = nodes[0].node if len(nodes) == 1 else None
        self.trace_panel.show_node(node, self.scene.overlay)

    def _toggle_trace_panel(self, on: bool) -> None:
        self._set_trace_panel_visible(on)

    def _reveal_trace_panel(self) -> None:
        if not self.trace_panel.isVisible():
            self._set_trace_panel_visible(True)

    def _set_trace_panel_visible(self, on: bool) -> None:
        """Show/hide the trace panel and sync its View-menu toggle."""
        on = bool(on)
        self.trace_panel.setVisible(on)
        act = getattr(self, "act_trace_panel", None)
        if act is not None and act.isChecked() != on:
            act.blockSignals(True)
            act.setChecked(on)
            act.blockSignals(False)
        self._update_side_dock()

    def _toggle_chat_panel(self, on: bool) -> None:
        self._set_chat_panel_visible(on)

    def _reveal_chat_panel(self) -> None:
        if not self.chat_panel.isVisible():
            self._set_chat_panel_visible(True)

    def _set_chat_panel_visible(self, on: bool) -> None:
        """Show/hide the chat panel and sync its View-menu toggle."""
        on = bool(on)
        self.chat_panel.setVisible(on)
        act = getattr(self, "act_chat_panel", None)
        if act is not None and act.isChecked() != on:
            act.blockSignals(True)
            act.setChecked(on)
            act.blockSignals(False)
        self._update_side_dock()

    def _update_side_dock(self) -> None:
        """Show the side dock iff a run panel inside it is meant to be shown, and
        give it a sensible width the first time it appears (without squashing the
        canvas). Uses isHidden() (explicit state) rather than isVisible(), which
        depends on ancestors — the dock and its children would otherwise deadlock
        each other into staying hidden."""
        want = (not self.chat_panel.isHidden()) or (not self.trace_panel.isHidden())
        if want == (not self.side_dock.isHidden()):
            return
        self.side_dock.setVisible(want)
        if want:
            sizes = self.workspace_splitter.sizes()
            if len(sizes) == 2 and sizes[1] < 80:
                total = sum(sizes) or 1000
                self.workspace_splitter.setSizes([max(360, total - 360), 360])

    # ── trace replay (#3) ────────────────────────────────────────────────────
    def on_open_trace(self) -> None:
        """Open a saved traces/*.jsonl run and re-animate it on the canvas."""
        if self._debug_running or self._chat_running:
            QMessageBox.information(
                self, "Run in progress",
                "Wait for the current run to finish before replaying a trace.")
            return
        # Default to the most recent generated agent's traces folder, if any.
        start_dir = BASE_DIR
        if self.generated_dir and os.path.isdir(self.generated_dir):
            tdir = os.path.join(self.generated_dir, "traces")
            start_dir = tdir if os.path.isdir(tdir) else self.generated_dir
        path, _ = QFileDialog.getOpenFileName(
            self, "Replay trace (JSONL)", start_dir,
            "Trace JSONL (*.jsonl);;All files (*)")
        if not path:
            return
        try:
            records = self._read_trace(path)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Cannot read trace", f"Could not read:\n{e}")
            return
        if not records:
            self.set_status("That trace file has no records to replay.")
            return
        # Light up nodes that exist on the current canvas; unknown names from the
        # trace are still tracked by the overlay (just not painted).
        self._replay_stage_names = [n.name for n in self.graph.nodes.values()
                                    if n.kind in FLOW_KINDS]
        self._reveal_trace_panel()
        self.trace_panel.show_replay_bar(True)
        self.set_status(f"Replaying {os.path.basename(path)} "
                        f"({len(records)} events) — use the transport controls.")
        self.replay_bar.load(records)

    def _read_trace(self, path: str) -> list:
        records = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue        # skip a partial/corrupt line, keep the rest
        return records

    def _stop_replay(self) -> None:
        bar = getattr(self, "replay_bar", None)
        if bar is not None:
            bar.stop()
        if getattr(self, "trace_panel", None) is not None:
            self.trace_panel.show_replay_bar(False)

    # Callbacks driven by ReplayBar — they reuse the live-overlay pipeline.
    def _replay_reset(self) -> None:
        self.scene.overlay = RuntimeOverlay(self._replay_stage_names)
        self.trace_panel.set_overlay(self.scene.overlay)
        self.trace_panel.clear()

    def _replay_feed(self, rec: dict) -> None:
        if self.scene.overlay is None:
            return
        self.scene.overlay.consume(rec)
        self.trace_panel.on_trace(rec)

    def _replay_after(self) -> None:
        self.scene.refresh_overlay()
        if self.scene.overlay is not None and self.scene.overlay.last:
            self.set_status("replay · " + self.scene.overlay.last)

    # ── chat run (#4) ────────────────────────────────────────────────────────
    def on_chat_run(self) -> None:
        """Open the multi-turn chat run panel for the current graph."""
        if self._debug_running:
            QMessageBox.information(
                self, "Debug run active",
                "Stop the current Debug Run before starting a chat run.")
            return
        if self._chat_running:
            # A turn is in flight — just surface the panel; don't regenerate the
            # module or replay HISTORY (that would wipe the live turn's transcript).
            self._reveal_chat_panel()
            self.chat_panel.set_status("A chat turn is in progress — please wait.")
            return
        self._stop_replay()
        try:
            mod = self._chat_ensure_module()
        except ValueError as e:
            QMessageBox.warning(self, "Cannot start chat",
                                f"The graph is not ready:\n\n{e}")
            return
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Error", f"Code generation failed:\n{e}")
            return
        self._reveal_chat_panel()
        self._reveal_trace_panel()          # watch the graph light up alongside
        self._refresh_chat_sessions()
        self.chat_panel.replay_history(getattr(mod, "HISTORY", []))
        self.set_status("Chat run ready — type a message and press Ctrl+Enter.")

    def _chat_ensure_module(self):
        """Generate + import the agent once and reuse it across turns so HISTORY
        accumulates; regenerate only when the graph changed since last time."""
        sig = self._snapshot()
        if self._chat_mod is not None and sig == self._chat_graph_sig:
            return self._chat_mod
        out_dir = graph_codegen.generate_from_graph(
            self.graph, self._agent_name, gui=False)
        self._debug_seq += 1
        modname = f"_qt_chat_agent_{self._debug_seq}"
        spec = importlib.util.spec_from_file_location(
            modname, os.path.join(out_dir, "agent.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.set_trace_sink(lambda rec: self._bridge.trace.emit(rec))
        if hasattr(mod, "set_review_handler"):
            mod.set_review_handler(self._bridge.request_review)
        if hasattr(mod, "set_confirm_handler"):
            mod.set_confirm_handler(self._bridge.request_confirm)
        self._chat_mod = mod
        self._chat_dir = out_dir
        self.generated_dir = out_dir
        self._chat_graph_sig = sig
        return mod

    def _refresh_chat_sessions(self) -> None:
        mod = self._chat_mod
        if mod is None:
            return
        try:
            sessions = mod.list_sessions()
            active = mod.current_session() if hasattr(mod, "current_session") else None
        except Exception:  # noqa: BLE001
            sessions, active = [], None
        self.chat_panel.set_sessions(sessions, active)

    def _chat_send(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        if self._debug_running:
            self.chat_panel.set_status("Stop the Debug Run before chatting.")
            return
        if self._chat_running:
            return                          # a turn is already in flight
        self._stop_replay()
        try:
            mod = self._chat_ensure_module()
        except ValueError as e:
            self.chat_panel.set_status(f"Graph not ready: {e}")
            return
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Error", f"Code generation failed:\n{e}")
            return
        self.chat_panel.clear_input()
        self.chat_panel.append("You", text)
        # Fresh overlay + timeline for this turn so the canvas animates it.
        stage_names = [n.name for n in self.graph.nodes.values()
                       if n.kind in FLOW_KINDS]
        self.scene.overlay = RuntimeOverlay(stage_names)
        self.trace_panel.set_overlay(self.scene.overlay)
        self.trace_panel.clear()
        self.scene.refresh_overlay()
        self._chat_running = True
        self._chat_active_mod = mod        # Stop must cancel THIS module
        self.chat_panel.set_busy(True)
        threading.Thread(target=self._chat_worker, args=(mod, text),
                         daemon=True).start()

    def _chat_worker(self, mod, text: str) -> None:
        try:
            result = mod.run(text, emit=lambda s: self._bridge.status.emit(str(s)),
                             on_token=lambda _t: None)
            self._bridge.turn_done.emit(result, None)
        except Exception as e:  # noqa: BLE001
            self._bridge.trace.emit({"kind": "run_error", "error": str(e)})
            self._bridge.turn_done.emit(None, str(e))

    def _chat_turn_done(self, result, error) -> None:
        self._chat_running = False
        self._chat_active_mod = None
        self.chat_panel.set_busy(False)
        # run() catches its own failures and RETURNS a sentinel string rather than
        # raising, so detect those prefixes here (mirroring the debug path) instead
        # of presenting them as a normal reply under a green "Ready." status.
        text = result.strip() if isinstance(result, str) else ""
        if error:
            self.chat_panel.append("Error", error)
            self.chat_panel.set_status("Error — check the LLM key/endpoint.")
        elif text.startswith("[cancelled]"):
            self.chat_panel.note("stopped by the user")
            self.chat_panel.set_status("Stopped.")
        elif text.startswith("[error]"):
            self.chat_panel.append("Error", result)
            self.chat_panel.set_status("Error — check the LLM key/endpoint.")
        else:
            self.chat_panel.append("Agent", result)
            self.chat_panel.set_status("Ready.")
        self._refresh_chat_sessions()

    def _chat_stop(self) -> None:
        # Cancel the module the live turn actually runs on (it may differ from
        # self._chat_mod if the graph was regenerated since the turn started).
        mod = self._chat_active_mod or self._chat_mod
        if mod is not None and hasattr(mod, "request_cancel"):
            mod.request_cancel()
        self.chat_panel.set_status("Stopping…  (stops at the next step)")

    def _chat_new_session(self) -> None:
        mod = self._chat_mod
        if mod is None or self._chat_running:
            return
        try:
            mod.new_session()
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "New session",
                                f"Could not start a new session:\n{e}")
            return
        self.chat_panel.clear_transcript()
        self.chat_panel.note("new session")
        self._refresh_chat_sessions()

    def _chat_load_session(self, sid: str) -> None:
        mod = self._chat_mod
        if mod is None or not sid or self._chat_running:
            return
        try:
            ok = bool(mod.load_session(sid))
        except Exception:  # noqa: BLE001
            ok = False
        if ok:
            self.chat_panel.replay_history(getattr(mod, "HISTORY", []))
            self._refresh_chat_sessions()

    # ── theme ──────────────────────────────────────────────────────────────────
    def _restyle_theme_labels(self) -> None:
        """Recolor the muted hint/status labels for the active theme (their text
        color is hardcoded-muted, so it must follow dark↔light)."""
        c = canvas_colors()
        for lbl in self._hint_labels:
            lbl.setStyleSheet(f"color:{c['hint']}; font-size:11px;")
        self.status_label.setStyleSheet(f"color:{c['status']}; padding:4px;")

    def set_theme(self, name: str) -> None:
        """Switch the UI theme ('dark' | 'light') live across the whole app and
        persist it. Repaints this window's canvas and recolors its muted labels."""
        app = QApplication.instance()
        if app is not None:
            apply_theme(app, name)          # whole-app palette: menus, dialogs, sidebar
        set_canvas_theme(name)              # canvas bg/grid globals (BG/GRID)
        persist_theme(name)                 # remember for next launch
        self.scene.setBackgroundBrush(QBrush(QColor(BG)))
        self.view.setBackgroundBrush(QBrush(QColor(BG)))
        self._restyle_theme_labels()
        self.scene.update()
        self.view.viewport().update()
        self.trace_panel.restyle()
        self.chat_panel.restyle()
        act = getattr(self, "theme_actions", {}).get(name)
        if act is not None and not act.isChecked():
            act.setChecked(True)            # keep the menu in sync if set in code
        self.set_status(f"Theme: {name}.")

    def add_node(self, kind: str, x: int | None = None, y: int | None = None) -> None:
        if x is None:
            count = len(self.graph.nodes)
            x, y = 40 + (count % 4) * 200, 40 + (count // 4) * 90
        node = self.graph.new_node(kind, x, y)
        self.scene.rebuild()
        self.scene.node_items[node.id].setSelected(True)
        self.set_status(f"Added {node.name}. Double-click it to configure.")

    def configure(self, node: Node) -> None:
        err = open_config_dialog(self, node)
        self.scene.rebuild()
        if err:
            QMessageBox.warning(self, "Invalid value", err)

    def configure_edge(self, edge: Edge) -> None:
        err = open_edge_config_dialog(self, edge, self.graph)
        self.scene.rebuild()
        if err:
            QMessageBox.warning(self, "Invalid value", err)

    def check_node_code(self, node: Node) -> None:
        """'Check Code' (VB-style): show the full generated agent.py / config.json
        with this node's contributed regions highlighted."""
        from code_view import code_for_node
        from canvas_qt.code_view_ui import show_code_view
        result = code_for_node(self.graph, node.id)
        if "error" in result:
            QMessageBox.warning(self, "Check Code", result["error"])
            return
        show_code_view(self, result)

    def _fresh_id(self, kind: str) -> str:
        nid = f"{kind}_{next(self.graph._counter)}"
        while nid in self.graph.nodes:
            nid = f"{kind}_{next(self.graph._counter)}"
        return nid

    def add_linked(self, agent: Node, kind: str) -> None:
        existing = sum(1 for e in self.graph.edges if e.dst == agent.id)
        nx = max(10, agent.x - 210)
        ny = max(10, agent.y + existing * 70)
        node = self.graph.new_node(kind, nx, ny)
        err = self.graph.add_edge(node.id, agent.id)
        if err:
            self.graph.remove_node(node.id)
            QMessageBox.warning(self, "Cannot add module", err)
            return
        warn = self.graph.link_warning(node.id, agent.id)
        self.scene.rebuild()
        err2 = open_config_dialog(self, node)
        self.scene.rebuild()
        if err2:
            QMessageBox.warning(self, "Invalid value", err2)
        self.set_status(warn or f"Added {KIND_LABELS[kind]} '{node.name}' and "
                                f"linked it to {agent.name}.")

    def delete_node(self, node: Node) -> None:
        self.graph.remove_node(node.id)
        self.scene.rebuild()
        self.set_status(f"Deleted {node.name}.")

    def unlink_node(self, node: Node) -> None:
        self.graph.edges = [e for e in self.graph.edges
                            if node.id not in (e.src, e.dst)]
        self.scene.rebuild()

    def delete_edge(self, edge: Edge) -> None:
        self.graph.remove_edge(edge)
        self.scene.rebuild()
        self.set_status("Link deleted.")

    def delete_selection(self) -> None:
        for item in self.scene.selectedItems():
            if isinstance(item, NodeItem):
                self.graph.remove_node(item.node.id)
            elif isinstance(item, EdgeItem):
                self.graph.remove_edge(item.edge)
        self.scene.rebuild()

    def select_all(self) -> None:
        """Select every node (Ctrl+A) so the whole graph can be dragged at once —
        grab any selected node and all selected nodes move together."""
        for item in self.scene.node_items.values():
            item.setSelected(True)
        n = len(self.scene.node_items)
        if n:
            self.set_status(f"Selected {n} module(s) — drag any one to move "
                            "them together.")

    # ── copy / paste ──────────────────────────────────────────────────────────
    @staticmethod
    def _unique_name(base: str, taken: set) -> str:
        """A '<base>_copy' name not already in `taken` (then _copy2, _copy3, ...)."""
        cand = f"{base}_copy"
        i = 2
        while cand in taken:
            cand = f"{base}_copy{i}"
            i += 1
        return cand

    def copy_selection(self) -> None:
        """Ctrl+C: copy the selected node(s) — kind, name and full configuration
        (props) — to the in-app clipboard, plus any links BETWEEN the copied nodes
        so a copied group keeps its internal wiring."""
        global _NODE_CLIPBOARD
        nodes = [it.node for it in self.scene.selectedItems()
                 if isinstance(it, NodeItem)]
        if not nodes:
            self.set_status("Nothing selected — click a module (or box-select "
                            "several), then Ctrl+C.")
            return
        ids = {n.id for n in nodes}
        _NODE_CLIPBOARD = {
            "nodes": [{"id": n.id, "kind": n.kind, "name": n.name,
                       "x": n.x, "y": n.y, "props": copy.deepcopy(n.props)}
                      for n in nodes],
            "edges": [(e.src, e.dst, copy.deepcopy(e.props))
                      for e in self.graph.edges
                      if e.src in ids and e.dst in ids],
        }
        self.set_status(f"Copied {len(nodes)} module(s) — Ctrl+V to paste.")

    def paste_clipboard(self) -> None:
        """Ctrl+V: paste the clipboard's node(s) as fresh modules — same config,
        new ids, unique names, offset so they don't sit on the originals — and
        re-create the links among them. The pasted nodes become the new selection
        (so they can be dragged away together)."""
        clip = _NODE_CLIPBOARD
        if not clip or not clip.get("nodes"):
            self.set_status("Clipboard is empty — select a module and Ctrl+C first.")
            return
        dx = dy = 28
        taken = {n.name for n in self.graph.nodes.values()}
        id_map: dict[str, str] = {}
        for spec in clip["nodes"]:
            node = self.graph.new_node(spec["kind"], spec["x"] + dx, spec["y"] + dy)
            node.props = copy.deepcopy(spec["props"])
            node.name = self._unique_name(spec["name"], taken)
            taken.add(node.name)
            id_map[spec["id"]] = node.id
        for rec in clip.get("edges", []):
            src, dst = rec[0], rec[1]
            eprops = rec[2] if len(rec) > 2 else {}      # older clips: no props
            if src in id_map and dst in id_map:
                if self.graph.add_edge(id_map[src], id_map[dst]) is None and eprops:
                    # carry the link's contract/branch props onto the new edge
                    ne = next((e for e in self.graph.edges
                               if e.src == id_map[src] and e.dst == id_map[dst]), None)
                    if ne is not None:
                        ne.props = copy.deepcopy(eprops)
        # Cascade: a repeated Ctrl+V lands a little further down-right each time.
        for spec in clip["nodes"]:
            spec["x"] += dx
            spec["y"] += dy
        self.scene.rebuild()
        self.scene.clearSelection()
        for nid in id_map.values():
            item = self.scene.node_items.get(nid)
            if item is not None:
                item.setSelected(True)
        self.set_status(f"Pasted {len(id_map)} module(s).")

    # ── pattern presets ──────────────────────────────────────────────────────
    def insert_pattern(self, pid: str) -> None:
        """Replace the canvas with a pattern preset (chosen from the Patterns
        menu). Confirms first only when there's existing work to discard."""
        if self.graph.nodes and QMessageBox.question(
            self, "Insert pattern",
            f"Replace the current canvas with the "
            f"'{patterns.PATTERNS[pid]['label']}' pattern preset?"
        ) != QMessageBox.Yes:
            return
        preset = patterns.build_pattern_graph(
            pid,
            llm_props=dict(provider=PROVIDERS[0],
                           model=PROVIDER_DEFAULTS[PROVIDERS[0]][0], api_key="",
                           base_url=PROVIDER_DEFAULTS[PROVIDERS[0]][1]),
            tool_files=codegen.list_tools()[:1])
        self.graph.nodes = preset.nodes
        self.graph.edges = preset.edges
        self.graph.state_schema = preset.state_schema      # replace = clean slate,
        self.graph.type_defs = dict(getattr(preset, "type_defs", {}) or {})
        self.graph.recursion_limit = preset.recursion_limit  # drop any stale state
        self.graph.storage = dict(getattr(preset, "storage", {}) or {})
        self.graph._counter = preset._counter
        self.scene.graph = self.graph
        self.scene.rebuild()
        # Presets are laid out centered on the origin; center the view there too
        # so the pattern appears in the middle of the canvas.
        self.view.centerOn(0, 0)
        self.fit_view()
        self.set_status(f"Pattern '{patterns.PATTERNS[pid]['label']}' inserted — "
                        "double-click the LLM modules to set API keys.")

    # ── save / load ──────────────────────────────────────────────────────────
    def on_save(self) -> bool:
        """Save to .json/.mta. Returns True on success, False if the user
        cancelled the dialog or the save failed (closeEvent relies on this)."""
        os.makedirs(GRAPHS_DIR, exist_ok=True)
        # Default to the self-contained .mta bundle (it carries tools + prompts, so
        # it's the shareable/runnable artifact); .json is the graph-only alternative.
        # The .mta filter is listed first AND passed as the initial filter, and the
        # suggested filename gets a .mta extension, so it's the default target format.
        mta_filter = "Self-contained bundle (*.mta)"
        json_filter = "Graph JSON (*.json)"
        default_name = "untitled.mta"
        if self._path:
            default_name = os.path.splitext(os.path.basename(self._path))[0] + ".mta"
        path, flt = QFileDialog.getSaveFileName(
            self, "Save graph", os.path.join(GRAPHS_DIR, default_name),
            f"{mta_filter};;{json_filter}", mta_filter)
        if not path:
            return False
        low = path.lower()
        as_mta = low.endswith(".mta") or (not low.endswith(".json") and "*.mta" in flt)
        try:
            if as_mta:
                if not low.endswith(".mta"):
                    path += ".mta"
                save_mta(self.graph, path, TOOLS_DIR)
                self.set_status(f"Bundle saved: {os.path.basename(path)}")
            else:
                if not low.endswith(".json"):
                    path += ".json"
                self.graph.save(path)
                self.set_status(f"Graph saved: {os.path.basename(path)}")
            add_recent_project(path)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Error", f"Save failed:\n{e}")
            return False
        self._path = path                         # remember for the window title
        self._clean_snapshot = self._snapshot()   # this state is now saved
        self._update_title()
        return True

    def on_load(self) -> None:
        os.makedirs(GRAPHS_DIR, exist_ok=True)
        path, _ = QFileDialog.getOpenFileName(
            self, "Load graph or bundle", GRAPHS_DIR,
            "Graph or bundle (*.json *.mta);;Graph JSON (*.json);;MetaAgent bundle (*.mta)")
        if path:
            self.load_path(path)

    def load_designed_graph(self, g, name: str = "") -> None:
        """Render a graph the Designer agent produced. Thread-safe: the agent's
        write_graph runs on a worker thread, so marshal onto the GUI thread via
        the bridge signal before touching the scene."""
        self._bridge.designed_graph.emit(g, name)

    def _apply_designed_graph(self, g, name: str) -> None:
        self.graph.nodes = g.nodes
        self.graph.edges = g.edges
        self.graph.state_schema = g.state_schema
        self.graph.type_defs = dict(getattr(g, "type_defs", {}) or {})
        self.graph.recursion_limit = g.recursion_limit
        self.graph.storage = dict(getattr(g, "storage", {}) or {})
        self.graph._counter = getattr(g, "_counter", self.graph._counter)
        self.scene.graph = self.graph
        self.scene.rebuild()
        self.fit_view()
        self.raise_(); self.activateWindow()
        self.set_status(f"Designer rendered '{name}' ({len(self.graph.nodes)} modules).")
        self._reset_history()          # the rendered graph is the new undo baseline

    def load_path(self, path: str) -> bool:
        try:
            loaded, extra = self._open_graph_file(path)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Load failed", f"Could not load:\n{e}")
            return False
        self._path = path                                  # for the window title
        self.graph.nodes = loaded.nodes
        self.graph.edges = loaded.edges
        self.graph.state_schema = loaded.state_schema      # graph-level: shared state
        self.graph.type_defs = dict(getattr(loaded, "type_defs", {}) or {})  # custom types
        self.graph.recursion_limit = loaded.recursion_limit  # and the loop guard
        self.graph.storage = dict(getattr(loaded, "storage", {}) or {})  # storage backend
        self.graph._counter = loaded._counter
        self.scene.graph = self.graph
        self.scene.rebuild()
        self.fit_view()
        self.set_status(f"Loaded {os.path.basename(path)}{extra}")
        add_recent_project(path)
        self._clean_snapshot = self._snapshot()   # freshly loaded = clean
        self._update_title()
        self._reset_history()                     # new baseline; can't undo past a load
        return True

    def _open_graph_file(self, path: str):
        if path.lower().endswith(".mta"):
            graph, info = load_mta(path, TOOLS_DIR)
            bits = []
            if info["restored"]:
                bits.append(f"restored {len(info['restored'])} tool file(s)")
            warn = []
            if info["conflicts"]:
                warn.append(
                    "These tool file(s) already exist in your tools/ folder and "
                    "DIFFER from the copy saved inside this bundle, so your local "
                    "version was kept and the graph will use it: "
                    + ", ".join(info["conflicts"])
                    + ".\n\nThis is usually fine — it just means the tool was edited "
                    "after the bundle was saved (or the bundle came from another "
                    "machine). To use the bundle's version instead, delete or rename "
                    "your local tools/ file and load again.")
            if info["missing"]:
                warn.append("Tool file(s) missing from the bundle: "
                            + ", ".join(info["missing"]))
            if warn:
                QMessageBox.warning(self, "Tool files", "\n\n".join(warn))
                bits.append(f"{len(info['conflicts']) + len(info['missing'])} "
                            "tool warning(s)")
            return graph, ("  [" + "; ".join(bits) + "]" if bits else "")
        return Graph.load(path), ""

    def on_add_graph(self) -> None:
        os.makedirs(GRAPHS_DIR, exist_ok=True)
        path, _ = QFileDialog.getOpenFileName(
            self, "Merge graph from file (onto current canvas)", GRAPHS_DIR,
            "Graph or bundle (*.json *.mta)")
        if not path:
            return
        try:
            incoming, _extra = self._open_graph_file(path)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Load failed", f"Could not load:\n{e}")
            return
        if not incoming.nodes:
            self.set_status("That graph has no modules to add.")
            return
        added = self._merge_graph(incoming)
        self.set_status(f"Added {added} module(s) from {os.path.basename(path)} — "
                        "drag from a module's right port to link them.")

    def _merge_graph(self, incoming: Graph) -> int:
        existing_max_y = max((n.y for n in self.graph.nodes.values()), default=None)
        inc_min_y = min((n.y for n in incoming.nodes.values()), default=0)
        dy = (existing_max_y + 110 - inc_min_y) if existing_max_y is not None else 0
        names = {n.name for n in self.graph.nodes.values()}

        def uniq(base: str) -> str:
            base = base or "node"
            if base not in names:
                names.add(base)
                return base
            i = 2
            while f"{base}_{i}" in names:
                i += 1
            names.add(f"{base}_{i}")
            return f"{base}_{i}"

        id_map = {}
        for old_id, n in incoming.nodes.items():
            nid = self._fresh_id(n.kind)
            id_map[old_id] = nid
            self.graph.nodes[nid] = Node(id=nid, kind=n.kind, name=uniq(n.name),
                                         x=max(10, n.x + 30), y=max(10, n.y + dy),
                                         props=copy.deepcopy(n.props))
        for e in incoming.edges:
            if e.src in id_map and e.dst in id_map:
                self.graph.edges.append(Edge(id_map[e.src], id_map[e.dst],
                                             props=copy.deepcopy(e.props)))
        # Carry graph-level shared state: add incoming fields whose name isn't
        # already declared, and adopt the incoming loop guard if we have none.
        have = {f.get("name") for f in self.graph.state_schema}
        for f in incoming.state_schema:
            if f.get("name") not in have:
                self.graph.state_schema.append(copy.deepcopy(f))
                have.add(f.get("name"))
        # carry custom types the merged fields may reference (don't clobber existing)
        for tn, td in (getattr(incoming, "type_defs", {}) or {}).items():
            self.graph.type_defs.setdefault(tn, copy.deepcopy(td))
        if not self.graph.recursion_limit and incoming.recursion_limit:
            self.graph.recursion_limit = incoming.recursion_limit
        if not self.graph.storage and getattr(incoming, "storage", None):
            self.graph.storage = dict(incoming.storage)
        self.scene.rebuild()
        return len(id_map)

    # ── generate / run / compile ─────────────────────────────────────────────
    def on_generate(self, code_style: str = "single") -> None:
        # Validate first so we don't prompt for a name on a graph that can't build.
        info = graph_codegen.analyze(self.graph)
        if info["errors"]:
            QMessageBox.warning(self, "Cannot generate",
                                "The graph is not ready:\n\n- " + "\n- ".join(info["errors"]))
            return
        name, ok = QInputDialog.getText(self, "Generate Code", "Agent name:",
                                        text=self._agent_name)
        name = name.strip()
        if not ok or not name:
            return
        self._agent_name = name
        try:
            # gui is derived from the graph: a GUI node linked to the entry agent.
            self.generated_dir = graph_codegen.generate_from_graph(
                self.graph, name, code_style=code_style)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Error", f"Code generation failed:\n{e}")
            return
        gui = os.path.exists(os.path.join(self.generated_dir, "gui.py"))
        how = ("Run it with:  python gui.py   (or python agent.py for console)"
               if gui else "Run it with:  python agent.py \"your task\"")
        layout = ("runtime/ package + thin agent.py" if code_style == "package"
                  else "single self-contained agent.py")
        msg = f"Agent generated ({layout}):\n{self.generated_dir}\n\n{how}"
        warnings = info.get("warnings", [])
        if warnings:
            msg += "\n\nWarnings:\n- " + "\n- ".join(warnings)
        (QMessageBox.warning if warnings else QMessageBox.information)(
            self, "Generated with warnings" if warnings else "Generated", msg)

    def on_run(self) -> None:
        """Run the generated GUI agent — a Qt-native port of
        runner.try_run_generated (so the Qt process never imports wx)."""
        import runner  # lazy-loaded; runner is Qt-native (no wx) since the port
        folder = self.generated_dir
        if not folder or not os.path.isdir(folder):
            QMessageBox.information(self, "No output yet", "Generate an agent first.")
            return
        if not os.path.isfile(os.path.join(folder, "gui.py")):
            QMessageBox.information(
                self, "No GUI",
                "This agent was generated without a GUI.\nAdd a GUI module to the "
                "canvas and link it to the entry agent, then regenerate — or run "
                "it in a console:\n"
                f"    python {os.path.join(folder, 'agent.py')}")
            return
        missing = runner.missing_modules(folder)
        if missing:
            if QMessageBox.question(
                self, "Missing modules",
                "The agent needs Python modules that are not installed:\n\n    "
                + ", ".join(missing)
                + "\n\nInstall them all now (pip install -r requirements.txt)?"
            ) != QMessageBox.Yes:
                return
            self.set_status("Installing requirements…")
            threading.Thread(target=self._install_then_launch,
                             args=(folder,), daemon=True).start()
            return
        # NOTE: no API-key nag here. Launching the GUI is for the agent's END-USERS,
        # who set their own key in the generated app's Settings (it shows a "no key"
        # nudge + guards Run). The designer only needs a key for a Debug Run.
        self._launch_generated(folder)

    def _launch_generated(self, folder: str, entry: str = "gui.py") -> None:
        import subprocess

        import runner
        py = runner._python_exe()
        if py is None:
            QMessageBox.warning(self, "Cannot run agent", runner._NO_PYTHON_MSG)
            self.set_status("No Python found.")
            return
        # a scheduler is a long-running console program — give it its own visible
        # console on Windows so its per-tick output isn't lost (a GUI needs none).
        _flags = (subprocess.CREATE_NEW_CONSOLE
                  if entry == "scheduler.py" and hasattr(subprocess, "CREATE_NEW_CONSOLE")
                  else 0)
        # clean env so a FROZEN designer doesn't point the child system Python at
        # the bundle (else its imports fail / installed libs look missing).
        subprocess.Popen([py, entry], cwd=folder, creationflags=_flags,
                         env=runner._child_env())
        self.set_status(f"Launched: {os.path.join(folder, entry)}")

    def on_run_scheduler(self) -> None:
        """Launch the generated scheduler.py — the ambient interval runner. Mirrors
        on_run but for a Schedule module (scheduler.py instead of gui.py)."""
        import runner
        folder = self.generated_dir
        if not folder or not os.path.isdir(folder):
            QMessageBox.information(self, "No output yet", "Generate an agent first.")
            return
        if not os.path.isfile(os.path.join(folder, "scheduler.py")):
            QMessageBox.information(
                self, "No scheduler",
                "This agent was generated without a schedule.\nAdd a Schedule module "
                "to the canvas, link it to the entry agent, then regenerate.")
            return
        if not self._ensure_scheduler_key(folder):   # the subprocess reads its key from config.json
            return
        missing = runner.missing_modules(folder)
        if missing:
            if QMessageBox.question(
                self, "Missing modules",
                "The agent needs Python modules that are not installed:\n\n    "
                + ", ".join(missing)
                + "\n\nInstall them all now (pip install -r requirements.txt)?"
            ) != QMessageBox.Yes:
                return
            self.set_status("Installing requirements…")
            threading.Thread(target=self._install_then_launch,
                             args=(folder, "scheduler.py"), daemon=True).start()
            return
        self._launch_generated(folder, "scheduler.py")

    def _ensure_scheduler_key(self, folder: str) -> bool:
        """The ambient scheduler runs as a SEPARATE process, so — unlike a Debug Run
        (in-memory key) — it can only read the API key from config.json. If the config
        has a keyless LLM, remind the designer and offer to COPY their key (incl. from
        the coding agent, via the same dialog) into this LOCAL config.json so the run
        can call the LLM. It's a local working copy (regeneration clears it; the .mta
        bundle is scrubbed), and the operator normally sets it on the deployment.
        Returns False only if the designer cancels the whole launch."""
        cfg_path = os.path.join(folder, "config.json")
        try:
            with open(cfg_path, encoding="utf-8") as f:
                cfg = json.load(f)
        except (OSError, json.JSONDecodeError):
            return True                     # nothing to gate on; let the run surface it
        llms = cfg.get("llms")
        if not isinstance(llms, dict):
            return True
        if not any(not c.get("api_key") for cfgs in llms.values() for c in cfgs):
            return True                     # already keyed — nothing to do
        if not self._debug_api_key:         # prompt once (has 'Copy from coding agent')
            if not self._ensure_debug_key(folder):
                return False                # designer cancelled -> don't launch
        key = self._debug_api_key
        if not key:
            return True                     # proceeding without a key (e.g. local endpoint)
        if QMessageBox.question(
            self, "API key for the scheduler",
            "The scheduler runs as its own process and reads its API key from "
            "config.json, which is currently empty.\n\nWrite your key into this agent's "
            "config.json so the ambient run can call the LLM?\n\n(Local working copy "
            "only — regeneration clears it and the .mta bundle is scrubbed; the operator "
            "normally sets the key on the deployment.)") != QMessageBox.Yes:
            return True                     # launch anyway; they'll set it themselves
        for cfgs in llms.values():          # fill every keyless LLM
            for c in cfgs:
                if not c.get("api_key"):
                    c["api_key"] = key
        try:
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
            self.set_status("Wrote your API key into config.json for the scheduler run.")
        except OSError as e:
            QMessageBox.warning(self, "Could not write key",
                                f"Couldn't write config.json:\n{e}")
        return True

    def _install_then_launch(self, folder: str, entry: str = "gui.py") -> None:
        """Worker thread: pip install -r, then launch. GUI updates go through
        the bridge so they happen on the GUI thread."""
        import subprocess

        import runner
        py = runner._python_exe()
        if py is None:
            self._bridge.notify.emit("Cannot install", runner._NO_PYTHON_MSG)
            return
        cenv = runner._child_env()   # frozen-safe env for the child Python
        result = subprocess.run(
            [py, "-m", "pip", "install", "-r", "requirements.txt"],
            cwd=folder, capture_output=True, text=True, env=cenv)
        if result.returncode != 0:
            self._bridge.status.emit("Install failed.")
            self._bridge.notify.emit("Install failed",
                                     "pip install failed:\n" + result.stderr[-800:])
            return
        still = runner.missing_modules(folder)
        if still:
            self._bridge.status.emit("Install incomplete.")
            self._bridge.notify.emit("Install incomplete",
                                     "Still missing: " + ", ".join(still))
            return
        _flags = (subprocess.CREATE_NEW_CONSOLE
                  if entry == "scheduler.py" and hasattr(subprocess, "CREATE_NEW_CONSOLE")
                  else 0)
        subprocess.Popen([py, entry], cwd=folder, creationflags=_flags, env=cenv)
        self._bridge.status.emit(f"Launched: {os.path.join(folder, entry)}")

    def _ensure_debug_key(self, out_dir: str) -> bool:
        """A Debug Run executes the graph in-canvas, so EVERY LLM needs a key. If the
        freshly-generated config has ANY keyless LLM, prompt the designer once (with a
        'Copy from coding agent' shortcut) and cache it for the session. The key is
        injected IN-MEMORY at run time (see _inject_debug_key) — never written to the
        generated config.json, so it can't leak into the shippable build artifact.
        Returns False if the designer cancels. (GUI launches are NOT gated — that's the
        end-user's key, set in the generated app's own Settings.)"""
        cfg_path = os.path.join(out_dir, "config.json")
        try:
            with open(cfg_path, encoding="utf-8") as f:
                cfg = json.load(f)
        except (OSError, json.JSONDecodeError):
            return True                     # nothing to gate on; let the run surface it
        llms = cfg.get("llms")
        if not isinstance(llms, dict):
            return True                     # unknown shape — don't block
        # ANY keyless entry needs a key — a mixed config (only some LLMs keyed) would
        # otherwise fail on the keyless ones at run time.
        needs_key = any(not c.get("api_key") for cfgs in llms.values() for c in cfgs)
        if not needs_key or self._debug_api_key:
            return True
        from PySide6.QtWidgets import QDialog
        from canvas_qt.dialogs import DebugKeyDialog
        dlg = DebugKeyDialog(self)
        if dlg.exec() != QDialog.Accepted:
            self.set_status("Debug run cancelled — no API key.")
            return False
        self._debug_api_key = dlg.key()     # non-empty (dialog gates OK); injected at run
        return True

    def _inject_debug_key(self, mod) -> None:
        """Put the session debug key into any KEYLESS LLM of the freshly-imported agent
        module, IN MEMORY only (not on disk) — so the debug run works but the key never
        lands in the generated config.json. Keys the designer set explicitly are kept."""
        key = self._debug_api_key
        cfg = getattr(mod, "CONFIG", None)
        if not key or not isinstance(cfg, dict):
            return
        for cfgs in (cfg.get("llms") or {}).values():
            for c in (cfgs or []):
                if not c.get("api_key"):
                    c["api_key"] = key
        if isinstance(getattr(mod, "_clients", None), dict):
            mod._clients.clear()            # drop cached clients so the key takes effect

    def on_open_folder(self) -> None:
        if self.generated_dir and os.path.isdir(self.generated_dir):
            os.startfile(self.generated_dir)
        else:
            QMessageBox.information(self, "No output yet", "Generate an agent first.")

    def on_compile(self) -> None:
        if not self.generated_dir or not os.path.isdir(self.generated_dir):
            QMessageBox.information(self, "No output yet", "Generate an agent first.")
            return
        if QMessageBox.question(self, "Compile",
                                "Run PyInstaller now? This can take a few minutes."
                                ) != QMessageBox.Yes:
            return
        self.set_status("Compiling with PyInstaller…")

        def worker():
            import runner  # compile_agent is wx-free, but runner is lazy-loaded
            ok, msg = runner.compile_agent(self.generated_dir)
            self._bridge.finished.emit(("compile", ok), msg)
        threading.Thread(target=worker, daemon=True).start()

    def on_dump_prompts(self) -> None:
        try:
            dump = graph_codegen.system_prompts_for_graph(self.graph)
        except ValueError as e:
            QMessageBox.warning(self, "Cannot dump prompts",
                                f"The graph is not ready:\n\n{e}")
            return
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Error", f"Could not build prompts:\n{e}")
            return
        if not dump.get("agents"):
            self.set_status("No agents on the canvas to dump.")
            return
        os.makedirs(GRAPHS_DIR, exist_ok=True)
        default = os.path.join(GRAPHS_DIR,
                               (self._agent_name or "agent")
                               + "_system_prompts.json")
        path, _ = QFileDialog.getSaveFileName(self, "Save system prompts (JSON)",
                                              default, "JSON (*.json)")
        if not path:
            return
        if not path.lower().endswith(".json"):
            path += ".json"
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(dump, f, indent=2, ensure_ascii=False)
        except OSError as e:
            QMessageBox.critical(self, "Error", f"Save failed:\n{e}")
            return
        self.set_status(f"Wrote {len(dump['agents'])} system prompt(s) to "
                        f"{os.path.basename(path)}")

    # ── debug run ────────────────────────────────────────────────────────────
    def on_debug_run(self) -> None:
        if self._debug_running:
            self._stop_debug()
            return
        if self._chat_running:
            QMessageBox.information(self, "Chat run active",
                                    "Wait for the chat turn to finish first.")
            return
        self._stop_replay()        # live run and replay are mutually exclusive
        try:
            out_dir = graph_codegen.generate_from_graph(
                self.graph, self._agent_name, gui=False)
        except ValueError as e:
            QMessageBox.warning(self, "Cannot debug", f"The graph is not ready:\n\n{e}")
            return
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Error", f"Code generation failed:\n{e}")
            return
        self.generated_dir = out_dir
        question, ok = QInputDialog.getText(
            self, "Debug Run",
            "Input for the debug run (the graph will light up as it runs):")
        if not ok or not question.strip():
            return
        if not self._ensure_debug_key(out_dir):   # debug needs the designer's key
            return
        stage_names = [n.name for n in self.graph.nodes.values()
                       if n.kind in FLOW_KINDS]
        self.scene.overlay = RuntimeOverlay(stage_names)
        self.scene.refresh_overlay()
        self.trace_panel.set_overlay(self.scene.overlay)
        self.trace_panel.clear()
        self._reveal_trace_panel()
        self._debug_running = True
        self._debug_mod = None
        self._debug_cancel_pending = False
        self.act_debug.setText("Stop Debug Run")
        self.set_status("Debug run started…  (click Stop Debug Run to cancel)")
        threading.Thread(target=self._debug_worker, args=(out_dir, question),
                         daemon=True).start()

    def _stop_debug(self) -> None:
        self._debug_cancel_pending = True
        mod = self._debug_mod
        if mod is not None and hasattr(mod, "request_cancel"):
            mod.request_cancel()
        self.set_status("Stopping debug run...  (stops at the next step)")

    def _debug_worker(self, out_dir: str, question: str) -> None:
        self._debug_seq += 1
        modname = f"_qt_debug_agent_{self._debug_seq}"
        try:
            spec = importlib.util.spec_from_file_location(
                modname, os.path.join(out_dir, "agent.py"))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            self._debug_mod = mod
            self._inject_debug_key(mod)     # in-memory key for the debug run (not to disk)
            mod.set_trace_sink(lambda rec: self._bridge.trace.emit(rec))
            if hasattr(mod, "set_review_handler"):
                mod.set_review_handler(self._bridge.request_review)
            if hasattr(mod, "set_confirm_handler"):
                mod.set_confirm_handler(self._bridge.request_confirm)
            if self._debug_cancel_pending:
                self._bridge.finished.emit("[cancelled] stopped by the user", None)
                return
            result = mod.run(question,
                             emit=lambda s: self._bridge.status.emit(str(s)),
                             on_token=lambda _t: None)
            self._bridge.finished.emit(result, None)
        except Exception as e:  # noqa: BLE001
            self._bridge.trace.emit({"kind": "run_error", "error": str(e)})
            self._bridge.finished.emit(None, str(e))

    def on_debug_scheduler(self) -> None:
        """Preview the schedule in-canvas: run each Schedule job's task ONCE, in-process,
        with the SAME live overlay + trace panel as a Debug Run (and the debug API key,
        copyable from the coding agent). Jobs run sequentially so the overlay stays clean;
        the real recurring/concurrent behaviour is `python scheduler.py`."""
        if self._debug_running:
            self._stop_debug()
            return
        if self._chat_running:
            QMessageBox.information(self, "Chat run active",
                                    "Wait for the chat turn to finish first.")
            return
        self._stop_replay()
        # each job carries the agent it DRIVES (edge dst) so the preview starts the
        # run there too (A: separate schedules → separate agents), matching scheduler.py.
        jobs = []
        for n in self.graph.nodes.values():
            if n.kind != "schedule":
                continue
            tgt = next((self.graph.nodes[e.dst].name for e in self.graph.edges
                        if e.src == n.id and self.graph.nodes.get(e.dst)
                        and self.graph.nodes[e.dst].kind in AGENT_KINDS), None)
            if tgt is not None:
                jobs.append((n.name, (n.props.get("initial_task") or "").strip(), tgt))
        if not jobs:
            QMessageBox.information(
                self, "No schedules",
                "Add a Schedule node and link it to the agent it should drive first.")
            return
        try:
            out_dir = graph_codegen.generate_from_graph(
                self.graph, self._agent_name, gui=False)
        except ValueError as e:
            QMessageBox.warning(self, "Cannot debug", f"The graph is not ready:\n\n{e}")
            return
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Error", f"Code generation failed:\n{e}")
            return
        self.generated_dir = out_dir
        if not self._ensure_debug_key(out_dir):    # copy-from-coding-agent key dialog
            return
        stage_names = [n.name for n in self.graph.nodes.values()
                       if n.kind in FLOW_KINDS]
        self.scene.overlay = RuntimeOverlay(stage_names)
        self.scene.refresh_overlay()
        self.trace_panel.set_overlay(self.scene.overlay)
        self.trace_panel.clear()
        self._reveal_trace_panel()
        self._debug_running = True
        self._debug_mod = None
        self._debug_cancel_pending = False
        self.act_debug.setText("Stop Debug Run")
        self.set_status(f"Debug scheduler: previewing {len(jobs)} job(s) once each…")
        threading.Thread(target=self._debug_scheduler_worker, args=(out_dir, jobs),
                         daemon=True).start()

    def _debug_scheduler_worker(self, out_dir: str, jobs: list) -> None:
        self._debug_seq += 1
        modname = f"_qt_debug_sched_{self._debug_seq}"
        try:
            spec = importlib.util.spec_from_file_location(
                modname, os.path.join(out_dir, "agent.py"))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            self._debug_mod = mod
            self._inject_debug_key(mod)      # in-memory key (not persisted)
            mod.set_trace_sink(lambda rec: self._bridge.trace.emit(rec))
            if hasattr(mod, "set_review_handler"):
                mod.set_review_handler(self._bridge.request_review)
            if hasattr(mod, "set_confirm_handler"):
                mod.set_confirm_handler(self._bridge.request_confirm)
            last = ""
            for name, task, target in jobs:  # each job's task once, sequentially
                if self._debug_cancel_pending:
                    break
                # start the run at the agent this schedule drives (A); None (or the
                # graph entry) runs the whole graph, matching scheduler.py's _job.
                entry = target if (target and target != getattr(mod, "ENTRY", None)) else None
                _via = f" -> {entry}" if entry else ""
                self._bridge.status.emit(f"=== [schedule] {name}{_via} ===")
                last = mod.run(task or "Run your scheduled task.",
                               emit=lambda s: self._bridge.status.emit(str(s)),
                               on_token=lambda _t: None, entry=entry)
            self._bridge.finished.emit(last or "[scheduler preview done]", None)
        except Exception as e:  # noqa: BLE001
            self._bridge.trace.emit({"kind": "run_error", "error": str(e)})
            self._bridge.finished.emit(None, str(e))

    def _show_review(self, prompt: str, content: str, choices=None) -> None:
        dlg = ReviewDialog(self, prompt, content, choices)
        dlg.exec()
        self._bridge._review_box = dlg.result()
        self._bridge._review_event.set()

    def _show_confirm(self, tool_name: str, args) -> None:
        dlg = ToolConfirmDialog(self, tool_name, args if isinstance(args, dict) else {})
        dlg.exec()
        self._bridge._confirm_box = dlg.outcome()
        self._bridge._confirm_event.set()

    def _on_trace(self, rec: dict) -> None:
        if self.scene.overlay is None:
            return
        self.scene.overlay.consume(rec)
        if self.scene.overlay.last:
            self.set_status("debug · " + self.scene.overlay.last)
        self.scene.refresh_overlay()
        self.trace_panel.on_trace(rec)

    def _debug_done(self, result, error) -> None:
        # shared by debug runs and the compile worker (tagged tuple)
        if isinstance(result, tuple) and result and result[0] == "compile":
            ok = result[1]
            self.set_status("Ready." if ok else "Compile failed.")
            (QMessageBox.information if ok else QMessageBox.critical)(
                self, "Compile result", error)
            return
        self._debug_running = False
        self._debug_mod = None
        self._debug_cancel_pending = False
        self.act_debug.setText("&Debug Run (live overlay)")
        if error:
            self.set_status("Debug run failed.")
            QMessageBox.critical(self, "Debug Run",
                                 f"The debug run failed:\n\n{error}\n\n"
                                 "Check the LLM key/endpoint in the LLM module(s).")
        elif isinstance(result, str) and result.strip().startswith("[cancelled]"):
            self.set_status("Debug run stopped.")
        else:
            self.set_status("Debug run finished.")
            QMessageBox.information(self, "Debug Run finished", f"Result:\n\n{result}")


def run(open_path: str | None = None):
    app = QApplication.instance() or QApplication([])
    if app.windowIcon().isNull():          # standalone launch: set our app icon
        from canvas_qt.welcome import app_icon
        app.setWindowIcon(app_icon())
    apply_theme(app, get_theme())
    win = CanvasWindow(open_path=open_path)
    win.show()
    app.exec()


if __name__ == "__main__":
    import sys
    run(sys.argv[1] if len(sys.argv) > 1 else None)
