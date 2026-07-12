"""Custom dashboard GUI for the Shanghai A-share TradingAgents system.

Loaded into the GUI node's `custom_gui` — drives the generated agent via
`import agent as core` + `core.run(...)` on a worker thread (see
prototype/custom_gui/CONTRACT.md). A single-file, good-looking Shanghai-themed
control panel: code input, live analyst progress, streaming trace, final call.
"""

import copy
import os
import sys
import threading
import traceback

# Add THIS file's folder to sys.path so `import agent` works even under a Python
# started with -P / PYTHONSAFEPATH (which drops the script dir), mirroring the
# built-in gui.py/server.py. Without it: "No module named agent".
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import Qt, Signal, QObject
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QFormLayout,
                               QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                               QMainWindow, QMessageBox, QPlainTextEdit, QPushButton,
                               QVBoxLayout, QWidget)

core = None  # the generated agent, imported lazily off the UI thread

# Palette: a deep INK header bar so the GOLD emblem + WHITE wordmark stand clearly
# apart from the background (the old all-red header made the logo blend in). Red is
# kept as the Shanghai accent (buttons, active chips, decision title).
RED = "#C7202B"
RED_DK = "#9B1620"
INK = "#1c1f26"
HEAD_1 = "#141a2e"      # header gradient (deep navy-ink) — distinct from red & body
HEAD_2 = "#20283f"
PANEL = "#ffffff"
BG = "#eef0f4"          # light body — clearly lighter than the dark header
MUTED = "#6b7280"
GOLD = "#E8B84B"

# The 3 "deep-think" agents (the rest use the "quick" model) — lets Settings expose
# ONE key + a quick-tier and deep-tier model instead of one row per the 13 agents.
DEEP_AGENTS = {"research_manager", "trader", "portfolio_manager"}


class SettingsDialog(QDialog):
    """为 config.json 中的所有 LLM 设置 API Key / 分层模型 / Base URL，保存后重载，
    下次运行生效。config.json 按 agent 存储 LLM（此处 13 个），但只有快/深两档，
    故统一应用一个 Key + Base URL + 快档/深档两个模型（而非 13 行）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("API Key / 模型设置")
        self.resize(560, 240)
        llms = core.CONFIG.get("llms") or {}
        any_cfg = next((c for cfgs in llms.values() for c in (cfgs or [])), {})
        quick_cfg = next((cfgs[0] for a, cfgs in llms.items()
                          if a not in DEEP_AGENTS and cfgs), any_cfg)
        deep_cfg = next((cfgs[0] for a, cfgs in llms.items()
                         if a in DEEP_AGENTS and cfgs), any_cfg)

        form = QFormLayout(self)
        form.addRow(QLabel("应用于 config.json 中每个 agent 的 LLM；保存后下次运行生效。"))
        self.api_key = QLineEdit(any_cfg.get("api_key") or "")
        self.api_key.setPlaceholderText("sk-…（SiliconFlow / OpenAI 兼容 Key）")
        form.addRow("API Key（全部）:", self.api_key)
        self.base_url = QLineEdit(any_cfg.get("base_url") or "")
        self.base_url.setPlaceholderText("留空 = 提供方默认")
        form.addRow("Base URL（全部）:", self.base_url)
        self.quick_model = QLineEdit(quick_cfg.get("model") or "")
        self.quick_model.setPlaceholderText("留空 = 保持不变")
        form.addRow("模型 — 分析师 / 辩论:", self.quick_model)
        self.deep_model = QLineEdit(deep_cfg.get("model") or "")
        self.deep_model.setPlaceholderText("留空 = 保持不变")
        form.addRow("模型 — 研判 / 交易 / 组合:", self.deep_model)

        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def apply_to_config(self):
        """Write the edits into a deep copy of CONFIG and persist via core.save_config
        (atomic write + reload_config, which clears the cached LLM clients)."""
        new = copy.deepcopy(core.CONFIG)
        key = self.api_key.text().strip()
        base = self.base_url.text().strip()
        qm = self.quick_model.text().strip()
        dm = self.deep_model.text().strip()
        for agent, cfgs in (new.get("llms") or {}).items():
            for lc in (cfgs or []):
                lc["api_key"] = key
                lc["base_url"] = base
                m = dm if agent in DEEP_AGENTS else qm
                if m:                        # blank model = leave as-is
                    lc["model"] = m
        core.save_config(new)                # atomic write + reload_config()

# the analyst pipeline stages shown as progress chips (match the graph agent names)
STAGES = [
    ("market_analyst", "行情/技术"), ("social_analyst", "股吧情绪"),
    ("news_analyst", "最新新闻"), ("fundamentals_analyst", "基本面"),
    ("bull_researcher", "多头"), ("bear_researcher", "空头"),
    ("research_manager", "研判"), ("trader", "交易计划"),
    ("portfolio_manager", "最终决策"),
]


class _Bridge(QObject):
    msg = Signal(str)          # trace line
    stage = Signal(str)        # agent name that just started
    done = Signal(str, str)    # (final_result, error)


class Dashboard(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("沪市 TradingAgents · Shanghai A-share Trading Desk")
        self.resize(1040, 720)
        self._bridge = _Bridge()
        self._bridge.msg.connect(self._append)
        self._bridge.stage.connect(self._activate)
        self._bridge.done.connect(self._finish)
        self._running = False
        self._chips = {}
        self._build()
        self.setStyleSheet(_QSS)
        threading.Thread(target=self._load_core, daemon=True).start()

    # ── layout ───────────────────────────────────────────────────────────
    def _build(self):
        root = QWidget(); self.setCentralWidget(root)
        v = QVBoxLayout(root); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(0)

        header = QFrame(); header.setObjectName("header"); header.setFixedHeight(84)
        h = QHBoxLayout(header); h.setContentsMargins(22, 0, 22, 0); h.setSpacing(14)
        badge = QLabel("沪"); badge.setObjectName("badge")
        badge.setFixedSize(50, 50); badge.setAlignment(Qt.AlignCenter)
        title = QLabel("TradingAgents · 沪市投研"); title.setObjectName("brand")
        sub = QLabel("上海证券市场 · 多智能体投研 · 全程联网取最新数据")
        sub.setObjectName("brandsub")
        col = QVBoxLayout(); col.setSpacing(2); col.addStretch(1)
        col.addWidget(title); col.addWidget(sub); col.addStretch(1)
        h.addWidget(badge); h.addLayout(col); h.addStretch(1)
        v.addWidget(header)

        body = QWidget(); bl = QVBoxLayout(body); bl.setContentsMargins(24, 18, 24, 18)
        bl.setSpacing(14)

        # control row
        ctl = QHBoxLayout(); ctl.setSpacing(10)
        lab = QLabel("股票代码 / 名称:"); lab.setObjectName("ctllabel")
        self.code = QLineEdit(); self.code.setPlaceholderText("如 600519 贵州茅台 / 601318 中国平安")
        self.code.setObjectName("code"); self.code.returnPressed.connect(self.on_run)
        self.settings_btn = QPushButton("⚙ API/模型设置"); self.settings_btn.setObjectName("settings")
        self.settings_btn.setEnabled(False)            # enabled once the runtime loads
        self.settings_btn.clicked.connect(self._open_settings)
        self.run_btn = QPushButton("开始投研"); self.run_btn.setObjectName("run")
        self.run_btn.clicked.connect(self.on_run)
        self.stop_btn = QPushButton("停止"); self.stop_btn.setObjectName("stop")
        self.stop_btn.setEnabled(False); self.stop_btn.clicked.connect(self._stop)
        ctl.addWidget(lab); ctl.addWidget(self.code, 1)
        ctl.addWidget(self.settings_btn); ctl.addWidget(self.run_btn); ctl.addWidget(self.stop_btn)
        bl.addLayout(ctl)

        # progress chips
        chips = QFrame(); chips.setObjectName("chips")
        g = QGridLayout(chips); g.setContentsMargins(14, 10, 14, 10); g.setSpacing(8)
        for i, (name, label) in enumerate(STAGES):
            c = QLabel("○ " + label); c.setObjectName("chip")
            g.addWidget(c, i // 5, i % 5)
            self._chips[name] = c
        bl.addWidget(chips)

        # live trace
        self.log = QPlainTextEdit(); self.log.setReadOnly(True); self.log.setObjectName("log")
        self.log.setPlaceholderText("投研过程将实时显示在此……")
        bl.addWidget(self.log, 1)

        # final decision card
        self.decision = QLabel("最终决策将显示在此。"); self.decision.setObjectName("decision")
        self.decision.setWordWrap(True); self.decision.setTextInteractionFlags(
            Qt.TextSelectableByMouse)
        dframe = QFrame(); dframe.setObjectName("dframe")
        dl = QVBoxLayout(dframe); dl.setContentsMargins(16, 12, 16, 12)
        dtitle = QLabel("投资决策 / Final Call"); dtitle.setObjectName("dtitle")
        dl.addWidget(dtitle); dl.addWidget(self.decision)
        bl.addWidget(dframe)

        v.addWidget(body, 1)
        self.status = self.statusBar(); self.status.showMessage("正在载入智能体……")

    # ── core lifecycle ───────────────────────────────────────────────────
    def _load_core(self):
        global core
        try:
            import agent as c
            core = c
            self._bridge.msg.emit("__ready__")
        except Exception as e:  # noqa: BLE001
            self._bridge.done.emit("", f"无法载入智能体: {e}")

    def _append(self, line):
        if line == "__ready__":
            self.settings_btn.setEnabled(hasattr(core, "save_config"))
            self.status.showMessage("就绪 — 输入股票代码后开始投研。")
            if not self._has_key():
                self.decision.setText("⚠ 尚未配置 API Key — 请点击“⚙ API/模型设置”填写后再开始投研。")
            return
        # detect which analyst just started (agent-stage trace lines)
        for name in self._chips:
            if name in line:
                self._bridge.stage.emit(name)
                break
        self.log.appendPlainText(line)
        self.log.moveCursor(QTextCursor.End)

    def _activate(self, name):
        c = self._chips.get(name)
        if c:
            c.setText("● " + c.text().split(" ", 1)[1])
            c.setProperty("on", True); c.setStyle(c.style())

    def on_run(self):
        if self._running:
            return
        if core is None:
            QMessageBox.information(self, "请稍候", "智能体仍在载入中。"); return
        code = self.code.text().strip()
        if not code:
            QMessageBox.information(self, "缺少代码", "请输入沪深股票代码或名称。"); return
        if not self._has_key():          # don't let a keyless run fail deep in the log
            r = QMessageBox.question(
                self, "未配置密钥",
                "尚未配置 API Key，缺少密钥将无法运行。\n\n是否现在打开设置填写？"
                "（选“否”可继续运行——例如本地无密钥端点。）",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
            if r == QMessageBox.Yes:
                self._open_settings()
                return
        for c in self._chips.values():
            c.setText("○ " + c.text().split(" ", 1)[1]); c.setProperty("on", False); c.setStyle(c.style())
        self.log.clear(); self.decision.setText("投研进行中……")
        self._running = True
        self.run_btn.setEnabled(False); self.stop_btn.setEnabled(True)
        self.settings_btn.setEnabled(False)            # no config reload mid-run
        self.status.showMessage("投研进行中……（点击停止可中断）")
        task = ("请对以下上海/深圳 A 股标的做完整多智能体投研，并给出明确的买入/持有/卖出决策"
                "与理由（务必调用工具与联网搜索获取最新行情、公告与新闻，不要凭记忆）：" + code)
        threading.Thread(target=self._run, args=(task,), daemon=True).start()

    def _run(self, task):
        try:
            result = core.run(task, emit=lambda s: self._bridge.msg.emit(str(s)))
            self._bridge.done.emit(result or "(无结果)", "")
        except Exception as e:  # noqa: BLE001
            self._bridge.done.emit("", f"运行出错: {e}")

    def _open_settings(self):
        if core is None or not hasattr(core, "save_config"):
            QMessageBox.information(self, "设置", "运行时尚未载入，请稍候再试。")
            return
        dlg = SettingsDialog(self)
        if dlg.exec() != QDialog.Accepted:
            return
        try:
            dlg.apply_to_config()
        except Exception:  # noqa: BLE001
            QMessageBox.critical(self, "设置保存失败", traceback.format_exc()[-1500:])
            return
        self.decision.setText(
            "设置已保存到 config.json 并重载，下次投研将使用新配置。"
            if self._has_key() else
            "设置已保存。⚠ 仍未填写 API Key —— 请通过“⚙ API/模型设置”补充后再开始。")

    def _stop(self):
        if core is not None:
            try:
                core.request_cancel()
            except Exception:  # noqa: BLE001
                pass
        self.status.showMessage("正在停止……")

    def _finish(self, result, err):
        self._running = False
        self.run_btn.setEnabled(True); self.stop_btn.setEnabled(False)
        self.settings_btn.setEnabled(core is not None and hasattr(core, "save_config"))
        if err:
            self.decision.setText("⚠ " + err); self.status.showMessage("出错")
            return
        for c in self._chips.values():
            c.setProperty("on", True); c.setStyle(c.style())
        self.decision.setText(result)
        self.status.showMessage("完成。")

    def _has_key(self):
        llms = (core.CONFIG.get("llms") or {}) if core is not None else {}
        if isinstance(llms, dict):
            return any(c.get("api_key") for cfgs in llms.values() for c in cfgs)
        return bool(core.CONFIG.get("api_key")) if core is not None else False


_QSS = f"""
QMainWindow, QWidget {{ background: {BG}; color: {INK};
    font-family: 'Microsoft YaHei','Segoe UI',sans-serif; font-size: 13px; }}
#header {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 {HEAD_1}, stop:1 {HEAD_2});
    border-bottom: 3px solid {GOLD}; }}
/* gold emblem badge — high contrast against the dark header AND the light body */
#badge {{ background: {GOLD}; color: {RED_DK}; font-size: 26px; font-weight: 900;
    border-radius: 25px; }}
#brand {{ color: #ffffff; font-size: 21px; font-weight: 800; letter-spacing: 1px; }}
#brandsub {{ color: {GOLD}; font-size: 12px; letter-spacing: 0.5px; }}
#ctllabel {{ font-weight: 600; }}
#code {{ padding: 9px 12px; border: 1px solid #d0d3d9; border-radius: 8px;
    background: {PANEL}; font-size: 14px; }}
#code:focus {{ border: 1px solid {RED}; }}
#settings {{ background: {PANEL}; color: {INK}; padding: 9px 16px;
    border: 1px solid #c8ccd4; border-radius: 8px; }}
#settings:hover {{ border: 1px solid {GOLD}; color: {RED_DK}; }}
#settings:disabled {{ color: #b6bac2; }}
#run {{ background: {RED}; color: #fff; font-weight: 700; padding: 9px 22px;
    border: none; border-radius: 8px; }}
#run:hover {{ background: {RED_DK}; }}
#run:disabled {{ background: #d9a1a5; }}
#stop {{ background: #fff; color: {INK}; padding: 9px 16px; border: 1px solid #d0d3d9;
    border-radius: 8px; }}
#stop:disabled {{ color: #b6bac2; }}
#chips {{ background: {PANEL}; border: 1px solid #e6e8ec; border-radius: 10px; }}
#chip {{ color: {MUTED}; font-size: 12px; padding: 5px 8px; }}
#chip[on="true"] {{ color: {RED}; font-weight: 700; }}
#log {{ background: #0f1117; color: #d7dbe0; border: 1px solid #e6e8ec;
    border-radius: 10px; padding: 10px; font-family: 'Consolas','Courier New',monospace;
    font-size: 12px; }}
#dframe {{ background: {PANEL}; border: 1px solid #e6e8ec; border-left: 4px solid {GOLD};
    border-radius: 10px; }}
#dtitle {{ color: {RED}; font-weight: 800; font-size: 14px; }}
#decision {{ font-size: 14px; line-height: 1.5em; }}
QStatusBar {{ background: {PANEL}; color: {MUTED}; }}
"""


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    win = Dashboard()
    win.show()
    app.exec()


if __name__ == "__main__":
    main()
