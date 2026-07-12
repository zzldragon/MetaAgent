"""Warm, childish custom GUI for the PictureBook agent — a storybook-maker.

A cosy, crayon-coloured control panel: type a story idea, press the big button,
and watch the agent write the story, paint every page, and bind a PDF. Drives the
generated agent ONLY via the contract (import agent as core; core.run(task, emit=,
on_token=) on a worker thread; core.request_cancel(); core.set_trace_sink(...)).
See prototype/custom_gui/CONTRACT.md.
"""

import copy
import os
import re
import sys
import threading

# Make `import agent` work under a -P / PYTHONSAFEPATH launch (script dir dropped).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QDialogButtonBox,
                               QFormLayout, QFrame, QGridLayout, QHBoxLayout, QLabel,
                               QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
                               QPushButton, QTextEdit, QVBoxLayout, QWidget)

core = None  # the generated agent, imported lazily off the UI thread

# ── warm, storybook palette ──────────────────────────────────────────────────
BG = "#FFF3E0"        # warm cream page
BG2 = "#FFE3C2"       # peach
CARD = "#FFFDF8"
INK = "#5B3A29"       # cocoa-brown text
MUTED = "#B08968"
ACCENT = "#F97316"    # vivid orange (buttons, white text on it) — clearly orange
ACCENT_HI = "#C2410C"  # burnt/deep orange — high-contrast for orange TEXT on the light page
GREEN = "#7FB069"     # done (border/accent)
GREEN_BG = "#E4F1DA"  # pale green fill — the words stay dark & readable on top
GREEN_TXT = "#3E7A2E" # dark green text for a done chip
PINK = "#FF9AA2"
BLUE = "#6EC6E6"
YELLOW = "#FFD166"

# the pipeline stage NAMES (order matches the graph); labels come from _I18N
STAGE_NAMES = ["author", "illustrator", "checker", "bookbinder"]

# ── UI-language strings (the interface chrome; the book's OWN language is a separate
#    dropdown). Default = Chinese; a Language menu switches to English. ─────────────
_I18N = {
    "zh": {
        "win_title": "📚  绘本制作器",
        "title": "📚  绘本制作器",
        "subtitle": "告诉我一个故事点子，我来写故事、画好每一页，并装订成书！",
        "tag": "我们来做个什么故事呢？✨",
        "prompt_ph": "例如：一只怕黑的小恐龙，遇到了一只友善的萤火虫……",
        "book_lang": "绘本语言：",
        "platform": "平台：",
        "set_key": "🔑  设置 API 密钥",
        "make": "✨  制作我的绘本！",
        "stop": "停止",
        "open_pdf": "📖  打开 PDF",
        "credit": "由 MetaAgent 制作",
        "menu_history": "历史(&H)",
        "act_clear": "清除历史(&C)",
        "menu_language": "语言(&L)",
        "menu_book": "绘本(&B)",
        "act_resume": "继续上次的绘本(&R)",
        "stage_author": "✍️  编写故事",
        "stage_illustrator": "🎨  绘制每一页",
        "stage_checker": "🔎  检查每一页",
        "stage_bookbinder": "📖  装订成书",
        "st_waking": "正在唤醒故事精灵…… 🧚",
        "st_ready": "准备好啦！今天想做什么故事呢？🖍️",
        "st_making": "正在制作你的绘本…… ✏️🎨",
        "st_stopping": "正在停止……",
        "st_cleared": "已清除！可以开始新故事啦。🖍️",
        "st_all_done": "全部完成！🌈",
        "st_busy_clear": "绘本制作中，暂时无法清除。✋",
        "need_key": "🔑  请先设置 %s 平台的 API 密钥（上方按钮）。",
        "painted": "🎨  已画好 %d / %d 页",
        "result_ready": "🎉  你的绘本做好啦！",
        "result_partial": "📖  绘本已生成，但有几页需要重试：",
        "pdf_line": "\n📄  %s",
        "missing_line": "\n⚠  仍缺少的页：%s （再按一次 ✨ 重试）",
        "no_idea_title": "先讲个故事吧！",
        "no_idea_body": "请先输入一个故事点子。✨",
        "keydlg_title": "%s API 密钥",
        "keydlg_hint": "%s 密钥 —— 用于写作 LLM 和图像生成。",
        "keydlg_key": "API 密钥：",
        "keydlg_base": "接口地址：",
        "keydlg_proxy": "代理（可选）：",
        "keydlg_proxy_ph": "留空 = 直连 / 系统代理；例如 http://host:port",
        "keydlg_imgkey": "图像密钥：",
        "keydlg_imgkey_ph": "该平台无图像接口，图像用 SiliconFlow Kolors 生成（填 SiliconFlow sk- 密钥）",
        "key_saved": "%s 密钥已保存！可以开始做绘本啦。🖍️",
        "plat_ok": "平台：%s ✓（%s）",
        "plat_need_key": "平台：%s —— 请设置该平台的 API 密钥 🔑",
        "plat_switched": "🔑  已切换到 %s，请设置该平台的 API 密钥（上方按钮）。",
    },
    "en": {
        "win_title": "📚  My Picture Book Maker",
        "title": "📚  My Picture Book Maker",
        "subtitle": "Tell me a story idea and I'll write it, paint every page, and make a book!",
        "tag": "What story should we make? ✨",
        "prompt_ph": "e.g.  A tiny dragon who is afraid of the dark and finds a friendly firefly…",
        "book_lang": "Language:",
        "platform": "Platform:",
        "set_key": "🔑  Set API Key",
        "make": "✨  Make my picture book!",
        "stop": "Stop",
        "open_pdf": "📖  Open PDF",
        "credit": "Designed by MetaAgent",
        "menu_history": "&History",
        "act_clear": "&Clear History",
        "menu_language": "&Language",
        "menu_book": "&Book",
        "act_resume": "&Resume last book",
        "stage_author": "✍️  Writing the story",
        "stage_illustrator": "🎨  Painting the pages",
        "stage_checker": "🔎  Checking every page",
        "stage_bookbinder": "📖  Binding your book",
        "st_waking": "Waking up the story elves… 🧚",
        "st_ready": "Ready! What shall we make today? 🖍️",
        "st_making": "Making your book… ✏️🎨",
        "st_stopping": "Stopping…",
        "st_cleared": "Cleared! Ready for a new story. 🖍️",
        "st_all_done": "All done! 🌈",
        "st_busy_clear": "Can't clear while a book is being made. ✋",
        "need_key": "🔑  Please set the API key for the %s platform (button above).",
        "painted": "🎨  Painted %d of %d pages",
        "result_ready": "🎉  Your picture book is ready!",
        "result_partial": "📖  Book made — but some pages need another try:",
        "pdf_line": "\n📄  %s",
        "missing_line": "\n⚠  Pages still missing: %s  (press ✨ again to retry them)",
        "no_idea_title": "Tell me a story!",
        "no_idea_body": "Please type a story idea first. ✨",
        "keydlg_title": "%s API Key",
        "keydlg_hint": "%s key — used for the writer LLM and the image generator.",
        "keydlg_key": "API key:",
        "keydlg_base": "Base URL:",
        "keydlg_proxy": "Proxy (optional):",
        "keydlg_proxy_ph": "blank = direct / system proxy;  e.g. http://host:port",
        "keydlg_imgkey": "Image key:",
        "keydlg_imgkey_ph": "this platform has no image API — images use SiliconFlow Kolors (enter a SiliconFlow sk- key)",
        "key_saved": "%s key saved! Ready to make books. 🖍️",
        "plat_ok": "Platform: %s ✓ (%s)",
        "plat_need_key": "Platform: %s — set its API key 🔑",
        "plat_switched": "🔑  Switched to %s. Please set its API key (button above).",
    },
}


class _Bridge(QObject):
    line = Signal(str)          # emit/trace log line (one line per call)
    token = Signal(str)         # streamed answer token (inserted INLINE, no line break)
    stage = Signal(str)         # agent name that just started
    pages = Signal(int, int)    # (illustrated, total)
    done = Signal(str, str)     # (result, error)
    ready = Signal(bool, str)


class StoryMaker(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("📚  My Picture Book Maker")
        self.resize(980, 760)
        self._running = False
        self._pdf_path = None
        self._missing = []
        self._chips = {}
        self._suppress_platform = True        # ignore combo signals until seeded
        self._ui_lang = "zh"                  # interface language (default Chinese)
        self._b = _Bridge()
        self._b.line.connect(self._log)
        self._b.token.connect(self._append_token)
        self._b.stage.connect(self._activate)
        self._b.pages.connect(self._on_pages)
        self._b.done.connect(self._finish)
        self._b.ready.connect(self._on_ready)
        self._build()
        self.setStyleSheet(_QSS)
        threading.Thread(target=self._load_core, daemon=True).start()

    # ── i18n ────────────────────────────────────────────────────────────────
    def _t(self, key):
        return _I18N.get(self._ui_lang, _I18N["en"]).get(key, _I18N["en"].get(key, key))

    # ── layout ────────────────────────────────────────────────────────────
    def _build(self):
        # menu bar. Keep refs on self so the QMenu/QAction wrappers aren't garbage-
        # collected (a known PySide quirk that silently empties the menu).
        self._hist_menu = self.menuBar().addMenu(self._t("menu_history"))
        self._clear_act = self._hist_menu.addAction(self._t("act_clear"))
        self._clear_act.setShortcut("Ctrl+L")
        self._clear_act.triggered.connect(self.on_clear_history)
        # Language menu: switch the INTERFACE language (the book's own language is the
        # dropdown below). Default is Chinese.
        self._lang_menu = self.menuBar().addMenu(self._t("menu_language"))
        self._act_zh = self._lang_menu.addAction("中文")
        self._act_en = self._lang_menu.addAction("English")
        self._act_zh.setCheckable(True); self._act_en.setCheckable(True)
        self._act_zh.triggered.connect(lambda: self._apply_ui_language("zh"))
        self._act_en.triggered.connect(lambda: self._apply_ui_language("en"))

        root = QWidget(); self.setCentralWidget(root)
        v = QVBoxLayout(root); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(0)

        header = QFrame(); header.setObjectName("header"); header.setFixedHeight(96)
        h = QVBoxLayout(header); h.setContentsMargins(28, 10, 28, 10); h.setSpacing(0)
        self._title_lbl = QLabel(self._t("title")); self._title_lbl.setObjectName("title")
        self._sub_lbl = QLabel(self._t("subtitle")); self._sub_lbl.setObjectName("subtitle")
        h.addStretch(1); h.addWidget(self._title_lbl); h.addWidget(self._sub_lbl); h.addStretch(1)
        v.addWidget(header)

        body = QWidget(); bl = QVBoxLayout(body); bl.setContentsMargins(28, 20, 28, 20); bl.setSpacing(16)

        # story input card
        idea = QFrame(); idea.setObjectName("card"); il = QVBoxLayout(idea)
        self._tag_lbl = self._tag(self._t("tag")); il.addWidget(self._tag_lbl)
        self.prompt = QPlainTextEdit(); self.prompt.setObjectName("prompt")
        self.prompt.setPlaceholderText(self._t("prompt_ph"))
        self.prompt.setFixedHeight(84)
        il.addWidget(self.prompt)
        ctl = QHBoxLayout()
        self._booklang_lbl = QLabel(self._t("book_lang")); ctl.addWidget(self._booklang_lbl)
        self.lang = QComboBox(); self.lang.addItems(["English", "简体中文", "Español", "Français", "日本語"])
        self.lang.setObjectName("lang")
        self.lang.setCurrentText("简体中文")   # default the BOOK language to Chinese too
        ctl.addWidget(self.lang)
        self._platform_lbl = QLabel(self._t("platform")); ctl.addWidget(self._platform_lbl)
        self.platform = QComboBox(); self.platform.setObjectName("lang")
        self._plat_keys = []                  # combo index -> platform key
        self.platform.currentIndexChanged.connect(self._on_platform_change)
        ctl.addWidget(self.platform); ctl.addStretch(1)
        self.key_btn = QPushButton(self._t("set_key")); self.key_btn.setObjectName("ghost")
        self.key_btn.clicked.connect(self.on_set_key); ctl.addWidget(self.key_btn)
        self.make_btn = QPushButton(self._t("make")); self.make_btn.setObjectName("make")
        self.make_btn.setEnabled(False); self.make_btn.clicked.connect(self.on_make)
        ctl.addWidget(self.make_btn)
        self.stop_btn = QPushButton(self._t("stop")); self.stop_btn.setObjectName("stop")
        self.stop_btn.setEnabled(False); self.stop_btn.clicked.connect(self.on_stop)
        ctl.addWidget(self.stop_btn)
        il.addLayout(ctl)
        bl.addWidget(idea)

        # progress chips
        chips = QFrame(); chips.setObjectName("card"); cg = QGridLayout(chips)
        cg.setContentsMargins(14, 12, 14, 12); cg.setHorizontalSpacing(10)
        self._chip_labels = {n: self._t("stage_" + n) for n in STAGE_NAMES}
        for i, name in enumerate(STAGE_NAMES):
            c = QLabel("○  " + self._chip_labels[name]); c.setObjectName("chip")
            cg.addWidget(c, 0, i); self._chips[name] = c
        self.pages_lbl = QLabel(""); self.pages_lbl.setObjectName("pages")
        cg.addWidget(self.pages_lbl, 1, 0, 1, 4)
        bl.addWidget(chips)

        # live log
        self.log = QTextEdit(); self.log.setObjectName("log"); self.log.setReadOnly(True)
        bl.addWidget(self.log, 1)

        # result card
        self.result = QFrame(); self.result.setObjectName("result"); self.result.setVisible(False)
        rl = QHBoxLayout(self.result)
        self.result_lbl = QLabel(self._t("result_ready")); self.result_lbl.setObjectName("resulttext")
        self.result_lbl.setWordWrap(True)
        rl.addWidget(self.result_lbl, 1)
        self.open_btn = QPushButton(self._t("open_pdf")); self.open_btn.setObjectName("open")
        self.open_btn.clicked.connect(self._open_pdf); rl.addWidget(self.open_btn)
        bl.addWidget(self.result)

        self._credit_lbl = QLabel(self._t("credit")); self._credit_lbl.setObjectName("credit")
        self._credit_lbl.setAlignment(Qt.AlignHCenter)
        bl.addWidget(self._credit_lbl)

        v.addWidget(body, 1)
        self.status = self.statusBar(); self.status.showMessage(self._t("st_waking"))
        self._retranslate()

    def _retranslate(self):
        """Re-apply every visible label/menu in the current UI language."""
        self.setWindowTitle(self._t("win_title"))
        self._title_lbl.setText(self._t("title"))
        self._sub_lbl.setText(self._t("subtitle"))
        self._tag_lbl.setText(self._t("tag"))
        self.prompt.setPlaceholderText(self._t("prompt_ph"))
        self._booklang_lbl.setText(self._t("book_lang"))
        self._platform_lbl.setText(self._t("platform"))
        self.key_btn.setText(self._t("set_key"))
        self.make_btn.setText(self._t("make"))
        self.stop_btn.setText(self._t("stop"))
        self.open_btn.setText(self._t("open_pdf"))
        self._credit_lbl.setText(self._t("credit"))
        self._hist_menu.setTitle(self._t("menu_history"))
        self._clear_act.setText(self._t("act_clear"))
        self._lang_menu.setTitle(self._t("menu_language"))
        self._act_zh.setChecked(self._ui_lang == "zh")
        self._act_en.setChecked(self._ui_lang == "en")
        # progress chips (keep each chip's done/pending state)
        for name in STAGE_NAMES:
            self._chip_labels[name] = self._t("stage_" + name)
            c = self._chips.get(name)
            if c is not None:
                mark = "✓  " if c.property("on") == "yes" else "○  "
                c.setText(mark + self._chip_labels[name])
        if not self.result.isVisible():
            self.result_lbl.setText(self._t("result_ready"))

    def _apply_ui_language(self, lang, save=True):
        if lang not in _I18N:
            return
        self._ui_lang = lang
        self._retranslate()
        if save and core is not None and hasattr(core, "save_config"):
            try:
                new = copy.deepcopy(core.CONFIG); new["ui_language"] = lang
                core.save_config(new)
            except Exception:  # noqa: BLE001
                pass

    def _tag(self, text):
        lab = QLabel(text); lab.setObjectName("tag"); return lab

    # ── core lifecycle ──────────────────────────────────────────────────────
    def _load_core(self):
        global core
        try:
            import agent as c
            core = c
            self._b.ready.emit(True, "")
        except Exception as e:  # noqa: BLE001
            self._b.ready.emit(False, str(e))

    def _on_ready(self, ok, err):
        if not ok:
            self._log("😟  Could not load the runtime: " + err + "\nRun: pip install -r requirements.txt")
            self.status.showMessage("Runtime failed"); return
        if hasattr(core, "set_trace_sink"):
            core.set_trace_sink(lambda rec: self._on_trace(rec))
        # apply the persisted interface language (default stays Chinese)
        saved = (core.CONFIG.get("ui_language") or "").strip()
        if saved in _I18N and saved != self._ui_lang:
            self._apply_ui_language(saved, save=False)
        self._init_platforms()
        self.make_btn.setEnabled(True)
        self.status.showMessage(self._t("st_ready"))
        if not self._has_key():
            self._log(self._t("need_key")
                      % self._profile(core.CONFIG, self._active_platform()).get("label", ""))

    # ── LLM platform (SiliconFlow / NVIDIA) ─────────────────────────────────────
    def _active_platform(self):
        return (core.CONFIG.get("platform") or "siliconflow") if core else "siliconflow"

    def _profile(self, cfg, pkey):
        """Built-in defaults for pkey + any saved override under config['platforms']."""
        prof = dict(core._PB.platform_defaults().get(pkey, {}))
        prof.update((cfg.get("platforms") or {}).get(pkey, {}) or {})
        return prof

    def _init_platforms(self):
        """Seed config['platforms'] (defaults + migrate an existing key), then fill
        the Platform dropdown and select the active one."""
        defaults = core._PB.platform_defaults()
        new = copy.deepcopy(core.CONFIG)
        plats = new.setdefault("platforms", {})
        changed = False
        # code-owned fields track the built-in defaults (so a new model/endpoint takes
        # effect on launch); user-owned fields (keys, proxy, base URL) are preserved.
        _code = {"label", "provider", "chat_model", "image_url", "image_model", "edit_model"}
        for k, d in defaults.items():
            entry = plats.setdefault(k, {})
            for field, val in d.items():
                if field in _code:
                    if entry.get(field) != val:
                        entry[field] = val; changed = True
                elif field not in entry:
                    entry[field] = val; changed = True
        # migrate: seed the current platform's key from any existing LLM key
        cur = new.get("platform") or "siliconflow"
        if not plats.get(cur, {}).get("api_key"):
            existing = next((c.get("api_key") for cfgs in (new.get("llms") or {}).values()
                             for c in (cfgs or []) if c.get("api_key")), "")
            if existing:
                plats[cur]["api_key"] = existing; changed = True
        if "platform" not in new:
            new["platform"] = "siliconflow"; changed = True
        # re-apply the ACTIVE platform to the live LLM configs so a refreshed default
        # (e.g. a faster chat model) takes effect without a manual platform re-switch.
        prof = dict(defaults.get(cur, {})); prof.update(plats.get(cur, {}) or {})
        for cfgs in (new.get("llms") or {}).values():
            for lc in (cfgs or []):
                if (lc.get("model") != prof.get("chat_model")
                        or lc.get("base_url") != prof.get("chat_base_url")
                        or lc.get("provider") != prof.get("provider")):
                    lc["provider"] = prof.get("provider", "")
                    lc["model"] = prof.get("chat_model", "")
                    lc["base_url"] = prof.get("chat_base_url", "")
                    lc["api_key"] = prof.get("api_key", "")
                    lc["proxy"] = prof.get("proxy", "")
                    changed = True
        if changed:
            try:
                core.save_config(new)
            except Exception:  # noqa: BLE001
                pass
        self._suppress_platform = True
        self.platform.clear(); self._plat_keys = []
        for k in defaults:
            self.platform.addItem(self._profile(core.CONFIG, k).get("label", k))
            self._plat_keys.append(k)
        active = self._active_platform()
        if active in self._plat_keys:
            self.platform.setCurrentIndex(self._plat_keys.index(active))
        self._suppress_platform = False

    def _on_platform_change(self, _idx=0):
        if self._suppress_platform or core is None or self._running:
            return
        idx = self.platform.currentIndex()
        if idx < 0 or idx >= len(self._plat_keys):
            return
        pkey = self._plat_keys[idx]
        new = copy.deepcopy(core.CONFIG)
        prof = self._profile(new, pkey)
        # point every LLM node at this platform (base URL + model + its key)
        for cfgs in (new.get("llms") or {}).values():
            for lc in (cfgs or []):
                lc["provider"] = prof.get("provider", "")
                lc["base_url"] = prof.get("chat_base_url", "")
                lc["model"] = prof.get("chat_model", "")
                lc["api_key"] = prof.get("api_key", "")
                lc["proxy"] = prof.get("proxy", "")
        new["platform"] = pkey
        try:
            core.save_config(new)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Switch failed", str(e)); return
        label = prof.get("label", pkey)
        if prof.get("api_key"):
            self.status.showMessage(self._t("plat_ok") % (label, prof.get("chat_model", "")))
        else:
            self.status.showMessage(self._t("plat_need_key") % label)
            self._log(self._t("plat_switched") % label)

    def _on_trace(self, rec):
        k = rec.get("kind")
        if k == "stage_start":
            self._b.stage.emit(rec.get("agent", ""))
        elif k == "state":
            st = rec.get("state") or {}
            up = rec.get("updates") or {}
            if "missing_pages" in up:
                self._missing = list(st.get("missing_pages") or [])
            if "pages_illustrated" in up or "page_count" in up or "missing_pages" in up:
                self._b.pages.emit(int(st.get("pages_illustrated", 0) or 0),
                                   int(st.get("page_count", 0) or 0))

    def _reset_panel(self):
        """Wipe the on-screen story log / result / progress back to a clean slate."""
        for name, c in self._chips.items():
            c.setText("○  " + self._chip_labels[name]); c.setProperty("on", "no")
            c.setStyle(c.style())
        self.log.clear(); self.result.setVisible(False); self.pages_lbl.setText("")
        self._pdf_path = None; self._missing = []

    def on_clear_history(self):
        """Clear the story log and result (History → Clear History / Ctrl+L). Each
        book is made independently, so there's no saved conversation to wipe — this
        just gives you a fresh, empty panel for the next story."""
        if self._running:
            self.status.showMessage(self._t("st_busy_clear")); return
        self._reset_panel()
        self.status.showMessage(self._t("st_cleared"))

    # ── run ─────────────────────────────────────────────────────────────────
    def on_make(self):
        if self._running or core is None:
            return
        idea = self.prompt.toPlainText().strip()
        if not idea:
            QMessageBox.information(self, self._t("no_idea_title"), self._t("no_idea_body")); return
        if not self._has_key():
            self.on_set_key(); return
        self._reset_panel()
        self._running = True
        self.make_btn.setEnabled(False); self.stop_btn.setEnabled(True)
        self.status.showMessage(self._t("st_making"))
        task = ("Please create a children's picture book in %s. Story idea: %s"
                % (self.lang.currentText(), idea))
        threading.Thread(target=self._run, args=(task,), daemon=True).start()

    def _run(self, task):
        try:
            result = core.run(task, emit=lambda s: self._b.line.emit(str(s)),
                              on_token=lambda t: self._b.token.emit(str(t)))
            self._b.done.emit(result or "(no result)", "")
        except Exception as e:  # noqa: BLE001
            self._b.done.emit("", "%s: %s" % (type(e).__name__, e))

    def on_stop(self):
        if core is not None:
            try:
                core.request_cancel()
            except Exception:  # noqa: BLE001
                pass
        self.status.showMessage(self._t("st_stopping"))

    def _append_token(self, tok):
        # streamed answer tokens go inline (append() would put each on its own line)
        self.log.moveCursor(QTextCursor.End)
        self.log.insertPlainText(tok)
        self.log.moveCursor(QTextCursor.End)

    def _log(self, line):
        self.log.append(line)
        self.log.moveCursor(QTextCursor.End)

    def _activate(self, name):
        c = self._chips.get(name)
        if c and c.property("on") != "yes":
            c.setText("✓  " + self._chip_labels.get(name, ""))   # keep the full label
            c.setProperty("on", "yes"); c.setStyle(c.style())

    def _on_pages(self, done, total):
        if total:
            self.pages_lbl.setText(self._t("painted") % (done, total))

    def _finish(self, result, err):
        self._running = False
        self.make_btn.setEnabled(True); self.stop_btn.setEnabled(False)
        if err:
            self._log("⚠  " + err); self.status.showMessage("⚠")
            return
        for name, c in self._chips.items():
            c.setText("✓  " + self._chip_labels[name])
            c.setProperty("on", "yes"); c.setStyle(c.style())
        m = re.search(r'([^\s"\']+\.pdf)', result)
        self._pdf_path = m.group(1) if m else None
        head = self._t("result_ready") if not self._missing else self._t("result_partial")
        note = (self._t("pdf_line") % self._pdf_path) if self._pdf_path else ""
        if self._missing:
            note += self._t("missing_line") % ", ".join(str(p) for p in self._missing)
        self.result_lbl.setText(head + note)
        self.open_btn.setVisible(bool(self._pdf_path and os.path.isfile(self._resolve(self._pdf_path))))
        self.result.setVisible(True)
        self.status.showMessage(self._t("st_all_done"))

    def _resolve(self, p):
        if os.path.isabs(p) and os.path.exists(p):
            return p
        for base in (os.getcwd(),) + tuple(
                core.get_workspace() if core and hasattr(core, "get_workspace") else ()):
            cand = os.path.join(base, p)
            if os.path.exists(cand):
                return cand
        return p

    def _open_pdf(self):
        p = self._resolve(self._pdf_path or "")
        if not os.path.isfile(p):
            QMessageBox.information(self, "Not found", "Couldn't find the PDF at:\n" + p); return
        try:
            if sys.platform.startswith("win"):
                os.startfile(p)  # noqa: S606
            else:
                import webbrowser; webbrowser.open("file://" + os.path.abspath(p))
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Open failed", str(e))

    # ── settings ──────────────────────────────────────────────────────────────
    def _has_key(self):
        if core is None:
            return False
        cfg = core.CONFIG
        p = self._active_platform()
        if ((cfg.get("platforms") or {}).get(p) or {}).get("api_key"):
            return True
        llms = cfg.get("llms") or {}
        return any(c.get("api_key") for cfgs in llms.values() for c in (cfgs or []))

    def on_set_key(self):
        """Set the API key for the CURRENTLY SELECTED platform (used for both the
        writer LLM and the image generator on that platform)."""
        if core is None or not hasattr(core, "save_config"):
            QMessageBox.information(self, "Please wait", "Still waking up…"); return
        p = self._active_platform()
        prof = self._profile(core.CONFIG, p)
        label = prof.get("label", p)
        dlg = QDialog(self); dlg.setWindowTitle(self._t("keydlg_title") % label); dlg.resize(540, 180)
        f = QFormLayout(dlg)
        f.addRow(QLabel(self._t("keydlg_hint") % label))
        key = QLineEdit(prof.get("api_key", ""))
        key.setPlaceholderText("nvapi-…" if p == "nvidia" else "sk-…")
        base = QLineEdit(prof.get("chat_base_url", ""))
        base.setPlaceholderText("chat base URL")
        proxy = QLineEdit(prof.get("proxy", core.CONFIG.get("proxy", "")))
        proxy.setPlaceholderText(self._t("keydlg_proxy_ph"))
        f.addRow(self._t("keydlg_key"), key); f.addRow(self._t("keydlg_base"), base)
        f.addRow(self._t("keydlg_proxy"), proxy)
        # Hybrid image provider: if this platform's image endpoint is a DIFFERENT host
        # than its chat endpoint (e.g. NVIDIA chat + SiliconFlow Kolors), offer a
        # separate image key. Prefill from any saved image key or the SiliconFlow key.
        _chat_host = (prof.get("chat_base_url") or "").split("/v1")[0]
        _img_host = (prof.get("image_url") or "").split("/v1")[0]
        imgkey = None
        if _chat_host and _img_host and _chat_host != _img_host:
            _seed = (prof.get("image_api_key")
                     or ((core.CONFIG.get("platforms") or {}).get("siliconflow") or {}).get("api_key", ""))
            imgkey = QLineEdit(_seed); imgkey.setPlaceholderText(self._t("keydlg_imgkey_ph"))
            f.addRow(self._t("keydlg_imgkey"), imgkey)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept); bb.rejected.connect(dlg.reject); f.addRow(bb)
        if dlg.exec() != QDialog.Accepted:
            return
        new = copy.deepcopy(core.CONFIG)
        pentry = new.setdefault("platforms", {}).setdefault(p, {})
        pentry["api_key"] = key.text().strip()
        pentry["proxy"] = proxy.text().strip()   # blank = direct/env; used by chat + image
        if imgkey is not None:
            pentry["image_api_key"] = imgkey.text().strip()
        if base.text().strip():
            pentry["chat_base_url"] = base.text().strip()
        # this platform is active -> push key/base/proxy into the live LLM configs too
        for cfgs in (new.get("llms") or {}).values():
            for lc in (cfgs or []):
                lc["api_key"] = key.text().strip()
                lc["proxy"] = proxy.text().strip()
                if base.text().strip():
                    lc["base_url"] = base.text().strip()
        try:
            core.save_config(new)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Save failed", str(e)); return
        self.status.showMessage(self._t("key_saved") % label)


_QSS = f"""
QMainWindow, QWidget {{ background: {BG}; color: {INK};
    font-family: 'Comic Sans MS','Segoe Print','Chalkboard SE','Microsoft YaHei',sans-serif;
    font-size: 14px; }}
#header {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 #FFB454, stop:0.5 {ACCENT}, stop:1 {ACCENT_HI}); }}
/* dark cocoa title — white was unreadable on the light-orange side of the header */
#title {{ color: #4A2617; font-size: 28px; font-weight: 800; }}
#subtitle {{ color: #7A3B12; font-size: 14px; font-weight: 600; }}
#card {{ background: {CARD}; border: 2px solid {BG2}; border-radius: 18px; }}
#tag {{ color: {ACCENT_HI}; font-size: 15px; font-weight: 700; }}
QLabel {{ color: {INK}; }}
#prompt {{ background: #fffdf9; border: 2px solid {BG2}; border-radius: 14px; padding: 10px;
    font-size: 15px; }}
#prompt:focus {{ border: 2px solid {ACCENT}; }}
#lang {{ background: #fffdf9; border: 2px solid {BG2}; border-radius: 10px; padding: 5px 10px; }}
#make {{ background: {ACCENT}; color: #fff; font-size: 16px; font-weight: 800; padding: 11px 26px;
    border: none; border-radius: 22px; }}
#make:hover {{ background: {ACCENT_HI}; }}
#make:disabled {{ background: #f0c9a8; color: #fff6ee; }}
#stop {{ background: #fff; color: {INK}; border: 2px solid {BG2}; border-radius: 18px; padding: 9px 16px; }}
#stop:disabled {{ color: {MUTED}; }}
#ghost {{ background: #fff; color: {ACCENT_HI}; border: 2px solid {ACCENT}; border-radius: 18px;
    padding: 9px 14px; font-weight: 700; }}
#chip {{ color: {MUTED}; font-size: 14px; font-weight: 700; padding: 8px 10px;
    background: #fff7ee; border: 2px dashed {BG2}; border-radius: 14px; }}
#chip[on="yes"] {{ color: {GREEN_TXT}; background: {GREEN_BG}; border: 2px solid {GREEN}; }}
#pages {{ color: {ACCENT_HI}; font-size: 14px; font-weight: 700; padding: 4px 8px; }}
#log {{ background: #fffdf9; border: 2px solid {BG2}; border-radius: 14px; padding: 10px;
    font-family: 'Comic Sans MS','Segoe UI',sans-serif; font-size: 13px; color: {INK}; }}
#result {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #fff6e5, stop:1 #ffe8cc);
    border: 2px solid {GREEN}; border-radius: 18px; }}
#resulttext {{ color: {INK}; font-size: 16px; font-weight: 800; padding: 6px; }}
#open {{ background: {GREEN}; color: #fff; font-weight: 800; padding: 10px 20px;
    border: none; border-radius: 20px; }}
QStatusBar {{ background: {BG2}; color: {INK}; font-weight: 700; }}
#credit {{ color: {MUTED}; font-size: 12px; font-weight: 600; letter-spacing: 0.5px;
    padding: 2px 0 4px 0; }}
/* Menu: the app-wide font makes the default QMenu draw the shortcut on top of the
   label — give items explicit padding so the label (left) and shortcut (right) get
   a clear gap. */
QMenuBar {{ background: {BG2}; color: {INK}; font-weight: 700; }}
QMenuBar::item {{ padding: 6px 12px; background: transparent; }}
QMenuBar::item:selected {{ background: {ACCENT}; color: #fff; border-radius: 8px; }}
QMenu {{ background: {CARD}; color: {INK}; border: 2px solid {BG2}; padding: 4px; }}
QMenu::item {{ padding: 7px 40px 7px 20px; border-radius: 8px; }}
QMenu::item:selected {{ background: {GREEN_BG}; color: {GREEN_TXT}; }}
"""


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    win = StoryMaker()
    win.show()
    app.exec()


if __name__ == "__main__":
    main()
