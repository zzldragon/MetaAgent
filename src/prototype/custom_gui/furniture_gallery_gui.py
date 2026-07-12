"""Custom, stylish gallery GUI for the Furniture Ad Collector agent.

A dark, modern desktop front-end (loaded into the graph's GUI node): paste a furniture
site URL (or pick a preset), hit 抓取, and the agent scrapes → classifies → files the
ad images; the gallery below then shows them as thumbnails grouped by category.

Drives the agent ONLY through the generated runtime (`import agent as core`), per
prototype/custom_gui/CONTRACT.md. @AGENT_NAME@ is substituted at generation time.
"""
import os
import re
import sys
import json
import threading

if not getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QPlainTextEdit, QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget,
)

# Base dir = next to the exe (frozen) or next to this script — where config.json,
# gui_settings.json and collected_images live, so double-click works from anywhere.
_BASE = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
         else os.path.dirname(os.path.abspath(__file__)))
_SETTINGS = os.path.join(_BASE, "gui_settings.json")
_CONFIG = os.path.join(_BASE, "config.json")
_VLM_MODELS = ["Qwen/Qwen3-VL-30B-A3B-Instruct", "Qwen/Qwen3-VL-32B-Instruct",
               "zai-org/GLM-4.5V", "Qwen/Qwen3-VL-8B-Instruct"]
_URL_SPLIT = re.compile(r"[\s,，、;；]+")

# NOTE: `import agent as core` (the generated runtime) is done LAZILY inside the worker
# thread below — the runtime is a large single file and importing it can take ~10s on
# Windows (Defender scan), which would otherwise freeze the window before it appears.

# category slug -> display name (matches vision_classify_tools / image_organize_tools)
CATS = [
    ("sofa", "沙发 Sofa"), ("bed", "床具 Bed"), ("dining", "餐桌椅 Dining"),
    ("table", "桌几 Table"), ("storage", "收纳 Storage"), ("lighting", "灯具 Lighting"),
    ("decor", "软装 Decor"), ("rug", "地毯 Rug"), ("outdoor", "户外 Outdoor"),
    ("office", "办公 Office"), ("promo", "促销海报 Promo"), ("other", "其他 Other"),
]
PRESETS = [
    ("Article", "https://www.article.com"),
    ("Castlery US", "https://www.castlery.com/us"),
    ("Castlery SG", "https://www.castlery.com/sg"),
    ("Maisons du Monde", "https://www.maisonsdumonde.com/US/en"),
    ("MADE", "https://www.made.com"),
    ("H&M Home", "https://www2.hm.com/en_us/home.html"),
]
_IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")
_root = lambda: os.environ.get("FURNITURE_DIR") or os.path.join(_BASE, "collected_images")

STYLE = """
QMainWindow, QWidget { background:#0f1115; color:#e6e8ee; font-size:13px;
    font-family:'Segoe UI','Microsoft YaHei',sans-serif; }
QLineEdit, QComboBox { background:#1b1e26; border:1px solid #2b2f3a; border-radius:8px;
    padding:8px 10px; selection-background-color:#4f7cff; }
QLineEdit:focus, QComboBox:focus { border:1px solid #4f7cff; }
QPushButton { background:#4f7cff; color:white; border:none; border-radius:8px;
    padding:8px 18px; font-weight:600; }
QPushButton:hover { background:#5f89ff; }
QPushButton:disabled { background:#333a4a; color:#8a90a0; }
QPushButton#ghost { background:#1b1e26; border:1px solid #2b2f3a; color:#c7cbd6; }
QPushButton#ghost:hover { border:1px solid #4f7cff; }
QLabel#h1 { font-size:20px; font-weight:700; }
QLabel#sub { color:#8a90a0; }
QLabel#cat { font-size:15px; font-weight:700; color:#dfe3ee; padding:6px 2px; }
QLabel#count { color:#8a90a0; }
QFrame#card { background:#161922; border:1px solid #232734; border-radius:12px; }
QFrame#sep { background:#232734; max-height:1px; }
QPlainTextEdit { background:#0b0d12; border:1px solid #232734; border-radius:8px;
    color:#93c5fd; font-family:Consolas,monospace; font-size:12px; }
QScrollArea { border:none; }
"""


class Thumb(QLabel):
    def __init__(self, path):
        super().__init__()
        self.setFixedSize(168, 132)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet("background:#0b0d12;border:1px solid #232734;border-radius:10px;")
        self.setToolTip(os.path.basename(path))
        try:
            pm = QPixmap(path)
            if not pm.isNull():
                self.setPixmap(pm.scaled(QSize(164, 128), Qt.KeepAspectRatio,
                                         Qt.SmoothTransformation))
            else:
                self.setText("· img ·")
        except Exception:  # noqa: BLE001
            self.setText("· img ·")


class FurnitureGallery(QMainWindow):
    _log = Signal(str)
    _done = Signal()

    def __init__(self):
        super().__init__()
        self.setWindowTitle("@AGENT_NAME@ · 家具广告图采集台")
        self.resize(1080, 760)
        self.setStyleSheet(STYLE)
        self._log.connect(self._append)
        self._done.connect(self._on_done)

        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(20, 18, 20, 16)
        outer.setSpacing(12)

        # header
        title = QLabel("家具广告图采集台")
        title.setObjectName("h1")
        sub = QLabel("粘贴家具网站链接（可多个）→ 一键抓取广告图 → AI 自动分门别类（SiliconFlow 视觉模型）")
        sub.setObjectName("sub")
        outer.addWidget(title)
        outer.addWidget(sub)

        # settings row: SiliconFlow key + vision model (saved locally, no env var needed)
        cfg = QHBoxLayout()
        cfg.setSpacing(10)
        klab = QLabel("🔑 SiliconFlow Key")
        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.Password)
        self.key.setPlaceholderText("sk-...  （本地保存，用于文本+视觉模型）")
        self.showkey = QPushButton("显示")
        self.showkey.setObjectName("ghost")
        self.showkey.setCheckable(True)
        self.showkey.toggled.connect(
            lambda on: self.key.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password))
        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.addItems(_VLM_MODELS)
        self.savekey = QPushButton("保存密钥")
        self.savekey.clicked.connect(self._save_settings)
        cfg.addWidget(klab, 0)
        cfg.addWidget(self.key, 1)
        cfg.addWidget(self.showkey, 0)
        cfg.addWidget(QLabel("视觉模型"), 0)
        cfg.addWidget(self.model, 0)
        cfg.addWidget(self.savekey, 0)
        outer.addLayout(cfg)

        # control bar
        bar = QHBoxLayout()
        bar.setSpacing(10)
        self.preset = QComboBox()
        self.preset.addItem("预设站点…", "")
        for name, u in PRESETS:
            self.preset.addItem(name, u)
        self.preset.currentIndexChanged.connect(self._pick_preset)
        self.url = QLineEdit()
        self.url.setPlaceholderText("多个网站用逗号/空格分隔，如： article.com, castlery.com/us   （回车开始）")
        self.url.returnPressed.connect(self.on_scrape)
        self.count = QSpinBox()
        self.count.setRange(1, 30)
        self.count.setValue(12)
        self.count.setToolTip("每个网站最多抓取的图片数（1–30）")
        self.btn = QPushButton("开始抓取")
        self.btn.clicked.connect(self.on_scrape)
        self.stop = QPushButton("停止")
        self.stop.setObjectName("ghost")
        self.stop.setEnabled(False)
        self.stop.clicked.connect(self.on_stop)
        self.refresh = QPushButton("刷新画廊")
        self.refresh.setObjectName("ghost")
        self.refresh.clicked.connect(self.reload_gallery)
        bar.addWidget(self.preset, 0)
        bar.addWidget(self.url, 1)
        bar.addWidget(QLabel("每站张数"), 0)
        bar.addWidget(self.count, 0)
        bar.addWidget(self.btn, 0)
        bar.addWidget(self.stop, 0)
        bar.addWidget(self.refresh, 0)
        outer.addLayout(bar)

        sep = QFrame()
        sep.setObjectName("sep")
        outer.addWidget(sep)

        self._load_settings()

        # gallery (scrollable)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.gallery = QWidget()
        self.gallery_v = QVBoxLayout(self.gallery)
        self.gallery_v.setContentsMargins(2, 4, 2, 4)
        self.gallery_v.setSpacing(14)
        self.gallery_v.addStretch(1)
        self.scroll.setWidget(self.gallery)
        outer.addWidget(self.scroll, 1)

        # log
        self.logbox = QPlainTextEdit()
        self.logbox.setReadOnly(True)
        self.logbox.setFixedHeight(120)
        outer.addWidget(self.logbox)

        self.reload_gallery()

    # ── controls ──────────────────────────────────────────────────────────
    def _pick_preset(self, i):
        u = self.preset.itemData(i)
        if not u:
            return
        cur = self.url.text().strip()
        # append (so you can queue several presets), avoid duplicates
        parts = [p for p in _URL_SPLIT.split(cur) if p]
        if u not in parts:
            parts.append(u)
        self.url.setText(", ".join(parts))

    def _append(self, text):
        self.logbox.appendPlainText(text)

    # ── settings: API key + model (local file, applied to env + config.json) ──
    def _load_settings(self):
        data = {}
        try:
            if os.path.isfile(_SETTINGS):
                data = json.load(open(_SETTINGS, "r", encoding="utf-8"))
        except Exception:  # noqa: BLE001
            data = {}
        # fall back to an existing key already in config.json / env
        key = data.get("api_key") or os.environ.get("SILICONFLOW_API_KEY", "")
        if not key:
            try:
                c = json.load(open(_CONFIG, "r", encoding="utf-8"))
                for lst in (c.get("llms") or {}).values():
                    if lst and lst[0].get("api_key"):
                        key = lst[0]["api_key"]
                        break
            except Exception:  # noqa: BLE001
                pass
        self.key.setText(key)
        model = data.get("vlm_model") or _VLM_MODELS[0]
        self.model.setCurrentText(model)
        self._apply_settings(key, model, persist=False, announce=False)

    def _save_settings(self):
        self._apply_settings(self.key.text().strip(), self.model.currentText().strip(),
                             persist=True, announce=True)

    def _apply_settings(self, key, model, persist=True, announce=True):
        model = model or _VLM_MODELS[0]
        # 1) env vars — read by the vision/scrape tools at run time
        if key:
            os.environ["SILICONFLOW_API_KEY"] = key
        os.environ["SILICONFLOW_VLM_MODEL"] = model
        os.environ.setdefault("FURNITURE_DIR", _root())
        os.environ.setdefault("PLAYWRIGHT_DOWNLOAD_HOST",
                              "https://cdn.npmmirror.com/binaries/playwright")
        # 2) write the key into config.json so the agent LLMs (Collector/Curator) use it
        if key:
            try:
                c = json.load(open(_CONFIG, "r", encoding="utf-8"))
                for lst in (c.get("llms") or {}).values():
                    for entry in lst:
                        entry["api_key"] = key
                json.dump(c, open(_CONFIG, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
                core = sys.modules.get("agent")   # if already imported, hot-reload
                if core and hasattr(core, "reload_config"):
                    core.reload_config()
            except Exception as e:  # noqa: BLE001
                if announce:
                    self._append(f"[警告] 写入 config.json 失败：{e}")
        # 3) persist to sidecar so next launch is zero-setup
        if persist:
            try:
                json.dump({"api_key": key, "vlm_model": model},
                          open(_SETTINGS, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
            except Exception:  # noqa: BLE001
                pass
        if announce:
            self._append("✓ 密钥/模型已保存并生效" + ("" if key else "（Key 为空！请填入 sk-...）"))

    def on_scrape(self):
        urls = [u for u in _URL_SPLIT.split(self.url.text().strip()) if u]
        if not urls:
            self._append("[提示] 先填一个或多个网站链接（逗号/空格分隔），或从预设里选。")
            return
        if not self.key.text().strip():
            self._append("[提示] 请先填入 SiliconFlow Key 并点「保存密钥」。")
            return
        # apply latest key/model even if the user didn't click save
        self._apply_settings(self.key.text().strip(), self.model.currentText().strip(),
                             persist=True, announce=False)
        self.btn.setEnabled(False)
        self.btn.setText("抓取中…")
        self.stop.setEnabled(True)
        sites = "、".join(urls)
        n = self.count.value()
        self._append(f"▶ 开始抓取 {len(urls)} 个站点（每站最多 {n} 张）：{sites}")
        task = (f"依次抓取以下家具网站的广告/产品图并自动归类保存：{sites}。"
                f"对【每一个】站点调用 scrape_ad_images，并传参 max_images={n}；"
                f"再对抓到的【每一张】图先用 classify_image 判类、用 organize_image 归档；"
                f"最后调用 list_collected 汇总。")
        threading.Thread(target=self._run, args=(task,), daemon=True).start()

    def _run(self, task):
        try:
            self._log.emit("… 正在载入 Agent 运行时（首次约需 10 秒）")
            import agent as core  # lazy: keeps the window responsive at startup
            result = core.run(task, emit=lambda s: self._log.emit(s.rstrip()))
            self._log.emit(f"✓ 完成：{str(result)[:200]}")
        except Exception as e:  # noqa: BLE001
            self._log.emit(f"[error] {e}")
        finally:
            self._done.emit()

    def on_stop(self):
        core = sys.modules.get("agent")  # lazily imported during a run
        if core and hasattr(core, "request_cancel"):
            core.request_cancel()
            self.stop.setEnabled(False)
            self._append("■ 已请求停止，正在结束当前步骤…")
        else:
            self._append("[提示] 当前没有正在进行的抓取。")

    def _on_done(self):
        self.btn.setEnabled(True)
        self.btn.setText("开始抓取")
        self.stop.setEnabled(False)
        self.reload_gallery()

    # ── gallery rendering ────────────────────────────────────────────────
    def reload_gallery(self):
        # clear existing category cards
        while self.gallery_v.count() > 1:  # keep the trailing stretch
            item = self.gallery_v.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        root = _root()
        any_shown = False
        for slug, label in CATS:
            d = os.path.join(root, slug)
            if not os.path.isdir(d):
                continue
            files = [os.path.join(d, f) for f in sorted(os.listdir(d))
                     if f.lower().endswith(_IMG_EXTS)]
            if not files:
                continue
            any_shown = True
            self.gallery_v.insertWidget(self.gallery_v.count() - 1,
                                        self._category_card(label, files))
        if not any_shown:
            hint = QLabel("还没有采集到图片。填入一个家具网站链接，点「开始抓取」试试 👆")
            hint.setObjectName("sub")
            hint.setAlignment(Qt.AlignCenter)
            self.gallery_v.insertWidget(0, hint)

    def _category_card(self, label, files):
        card = QFrame()
        card.setObjectName("card")
        v = QVBoxLayout(card)
        v.setContentsMargins(14, 10, 14, 14)
        head = QHBoxLayout()
        name = QLabel(label)
        name.setObjectName("cat")
        cnt = QLabel(f"{len(files)} 张")
        cnt.setObjectName("count")
        head.addWidget(name)
        head.addStretch(1)
        head.addWidget(cnt)
        v.addLayout(head)
        grid = QGridLayout()
        grid.setSpacing(10)
        for i, path in enumerate(files[:18]):  # cap per row-section for speed
            grid.addWidget(Thumb(path), i // 5, i % 5)
        v.addLayout(grid)
        return card


if __name__ == "__main__":
    app = QApplication.instance() or QApplication(sys.argv)
    # keep a real reference — a bare `FurnitureGallery().show()` gets garbage-collected
    # right after show(), the window vanishes and the app quits ("闪现").
    win = FurnitureGallery()
    win.show()
    win.raise_()
    win.activateWindow()
    sys.exit(app.exec())
