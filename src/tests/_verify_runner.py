"""Verify runner.py: requirements parsing, missing-module detection, and a
real launch of a generated GUI agent."""

import os
import shutil
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import runner

# 1. requirements parsing + import-name mapping
tmp = os.path.join(BASE, "_runner_test")
os.makedirs(tmp, exist_ok=True)
with open(os.path.join(tmp, "requirements.txt"), "w", encoding="utf-8") as f:
    f.write("openai>=1.40.0\nPySide6>=6.5.0\nsome_fake_pkg_xyz>=1.0\n")
mods = runner._required_imports(tmp)
assert ("PySide6", "PySide6") in mods, mods        # generated GUIs use PySide6
assert ("openai", "openai") in mods, mods
assert runner.IMPORT_NAMES.get("wxpython") == "wx"  # legacy mapping still honored
missing = runner.missing_modules(tmp)
assert missing == ["some_fake_pkg_xyz"], missing   # installed ones not flagged
print("dependency detection ok:", missing)

# 1a. regression: a pip name whose import name differs (Pillow -> PIL) must not
# be reported perpetually missing — the "Still missing: Pillow after install"
# bug. IMPORT_NAMES is the inverse of codegen.PIP_NAMES, so it covers all of them.
import importlib.util as _u
assert runner.IMPORT_NAMES.get("pillow") == "PIL"
assert runner.IMPORT_NAMES.get("pymupdf") == "fitz"
assert runner.IMPORT_NAMES.get("opencv-python") == "cv2"
with open(os.path.join(tmp, "requirements.txt"), "w", encoding="utf-8") as f:
    f.write("Pillow>=10.0\n")
if _u.find_spec("PIL") is not None:                # Pillow present in this env
    assert runner.missing_modules(tmp) == [], runner.missing_modules(tmp)
# restore the requirements file the rest of the script expects
with open(os.path.join(tmp, "requirements.txt"), "w", encoding="utf-8") as f:
    f.write("openai>=1.40.0\nPySide6>=6.5.0\nsome_fake_pkg_xyz>=1.0\n")
print("Pillow import-name mapping ok (pip 'Pillow' -> import 'PIL')")

# folder without requirements.txt -> nothing missing, no crash
empty = os.path.join(tmp, "empty")
os.makedirs(empty, exist_ok=True)
assert runner.missing_modules(empty) == []
print("missing requirements.txt handled")

# 1b. interpreter resolution: from source it's the running Python
assert runner._python_exe() == sys.executable
# when MetaAgent is frozen (packaged), sys.executable is the app exe, so the
# runner must resolve a system Python instead (the 'Run GUI Agent' bug fix)
try:
    sys.frozen = True
    py = runner._python_exe()
    assert py and os.path.exists(py), py            # a real interpreter on PATH
    real_which = shutil.which
    shutil.which = lambda *_a, **_k: None           # simulate no Python on PATH
    try:
        assert runner._python_exe() is None         # → GUI warns, won't relaunch
    finally:
        shutil.which = real_which
    miss_frozen = runner.missing_modules(tmp)       # probes the target Python
    assert "some_fake_pkg_xyz" in miss_frozen, miss_frozen
finally:
    del sys.frozen
print("frozen interpreter resolution ok: system Python, None fallback, probe")

# 2. A freshly generated PySide6 GUI agent has gui.py + installed deps and
#    launches (offscreen, so no real window) — exactly like runner._launch does.
import graph_codegen
from graph_model import Graph

g = Graph()
a = g.new_node("agent", 0, 0); a.name = "runneragent"
llm = g.new_node("llm", 0, 0)
llm.props.update(provider="siliconflow", model="deepseek-ai/DeepSeek-V4-Flash",
                 api_key="sk-test", base_url="https://api.siliconflow.cn/v1")
g.add_edge(llm.id, a.id)
gen = graph_codegen.generate_from_graph(g, "runner_gui_smoke", gui=True)
try:
    assert os.path.isfile(os.path.join(gen, "gui.py"))
    assert runner.missing_modules(gen) == [], runner.missing_modules(gen)
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
    proc = subprocess.Popen([sys.executable, "gui.py"], cwd=gen,
                            stderr=subprocess.PIPE, text=True, env=env)
    try:
        time.sleep(4)
        if proc.poll() is not None:
            _, err = proc.communicate()
            raise AssertionError(
                f"GUI exited early (code {proc.returncode}):\n{err}")
        print("launch ok: generated PySide6 GUI is running")
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)
finally:
    shutil.rmtree(gen, ignore_errors=True)

shutil.rmtree(tmp)
print("\nRUNNER CHECKS PASSED")
