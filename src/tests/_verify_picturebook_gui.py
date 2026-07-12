"""Verify the PictureBook custom GUI: Chinese-by-default interface + a Language menu
that switches to English (and back), with full retranslation of the visible chrome.
Offscreen Qt; skips cleanly if PySide6 isn't installed."""
import importlib.util
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUI = os.path.join(BASE, "prototype", "picturebook_gui.py")

try:
    from PySide6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    print("PySide6 not available — skipping picturebook GUI check")
    sys.exit(0)

app = QApplication.instance() or QApplication([])
spec = importlib.util.spec_from_file_location("pbgui_test", GUI)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

# both languages define the same keys (no missing translations)
assert set(m._I18N["zh"]) == set(m._I18N["en"]), \
    set(m._I18N["zh"]) ^ set(m._I18N["en"])

w = m.StoryMaker()

# 1. defaults to Chinese: interface AND the book-language dropdown
assert w._ui_lang == "zh", w._ui_lang
assert "绘本" in w._title_lbl.text(), w._title_lbl.text()
assert w.lang.currentText() == "简体中文", w.lang.currentText()
assert [a.text() for a in w._lang_menu.actions()] == ["中文", "English"]
assert w._act_zh.isChecked() and not w._act_en.isChecked()
print("ok 1: interface defaults to Chinese; Language menu offers 中文 / English")

# 2. switch to English retranslates the chrome + menus + chips
w._apply_ui_language("en", save=False)
assert w._title_lbl.text() == "📚  My Picture Book Maker"
assert w.make_btn.text().strip().endswith("picture book!")
assert w._hist_menu.title() == "&History"
assert "Writing" in w._chips["author"].text()
assert w._act_en.isChecked() and not w._act_zh.isChecked()
print("ok 2: switching to English retranslates title/buttons/menus/progress chips")

# 3. switch back to Chinese
w._apply_ui_language("zh", save=False)
assert "编写故事" in w._chips["author"].text()
assert w.make_btn.text() == "✨  制作我的绘本！"
print("ok 3: switching back to Chinese retranslates everything")

# 4. platform seeding refreshes code-owned fields (a stale chat model updates to the
#    current default) and re-applies it to the live LLM configs — with a fake core.
import copy as _copy

sys.path.insert(0, BASE)
_ts = importlib.util.spec_from_file_location(
    "pbtools_test", os.path.join(BASE, "tools", "picturebook_tools.py"))
_tm = importlib.util.module_from_spec(_ts)
_ts.loader.exec_module(_tm)


class _FakeCore:
    _PB = _tm._PB
    CONFIG = {"platform": "nvidia",
              "platforms": {"nvidia": {"api_key": "nvapi-x", "chat_model": "deepseek-ai/deepseek-v4-pro"}},
              "llms": {"author": [{"provider": "nvidia", "model": "deepseek-ai/deepseek-v4-pro",
                                   "base_url": "https://integrate.api.nvidia.com/v1", "api_key": "nvapi-x"}]}}

    @staticmethod
    def save_config(new): _FakeCore.CONFIG = _copy.deepcopy(new)

    @staticmethod
    def set_trace_sink(fn): pass


m.core = _FakeCore          # reuse the existing window (a 2nd window + thread is flaky)
w._init_platforms()
_nv = _FakeCore.CONFIG["platforms"]["nvidia"]
assert _nv["chat_model"] == "meta/llama-3.1-70b-instruct", _nv["chat_model"]
_lc = _FakeCore.CONFIG["llms"]["author"][0]
assert _lc["model"] == "meta/llama-3.1-70b-instruct"
assert _lc["provider"] == "nvidia", "provider should match the platform, got %s" % _lc["provider"]
assert _nv["api_key"] == "nvapi-x", "user key preserved"
print("ok 4: a stale NVIDIA chat model is refreshed to the current default on launch "
      "and re-applied to the live LLM configs (key preserved)")

print("ALL PICTUREBOOK-GUI CHECKS PASSED")
