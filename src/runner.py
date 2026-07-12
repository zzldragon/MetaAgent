"""Launch a generated agent's GUI from the designers.

Checks the agent's requirements.txt against the current Python first; if
modules are missing, offers to install them all (pip install -r
requirements.txt), then launches gui.py as a separate process.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading

# NOTE: PySide6 is imported lazily inside the functions that actually show
# dialogs. This keeps `import runner` cheap so pure-logic consumers can reuse
# the helpers (_python_exe, missing_modules, compile_agent) without dragging in
# Qt. The host process already runs a QApplication, so QMessageBox is safe to
# use here. The host no longer imports wx anywhere; generated agents' GUIs now
# use PySide6 too (the IMPORT_NAMES mapping below is about parsing a generated
# agent's requirements.txt, not the host — wxpython is kept for any older agent).

# pip package name (lowercased) → import name, where they differ. Used to VERIFY
# a generated agent's requirements.txt is satisfied. This is the INVERSE of
# codegen.PIP_NAMES (import → pip): derive it from there so the two never drift
# (e.g. requirements lists "Pillow" but its module is "PIL" — checking
# find_spec("pillow") would report it perpetually missing). GUI deps that aren't
# tool-scanned are added on top; a hardcoded mirror is the fallback if codegen
# isn't importable (trimmed/frozen deploy).
def _import_name_map() -> dict:
    names = {"wxpython": "wx", "pyside6": "PySide6"}
    try:
        from codegen import PIP_NAMES                  # {import_name: pip_name}
        names.update({pip.lower(): imp for imp, pip in PIP_NAMES.items()})
    except Exception:
        names.update({                                 # mirror of codegen.PIP_NAMES
            "pillow": "PIL", "beautifulsoup4": "bs4", "pyyaml": "yaml",
            "scikit-learn": "sklearn", "opencv-python": "cv2",
            "python-docx": "docx", "python-pptx": "pptx", "pymupdf": "fitz",
            "python-dotenv": "dotenv", "python-dateutil": "dateutil"})
    return names


IMPORT_NAMES = _import_name_map()

_NO_PYTHON_MSG = (
    "No Python interpreter was found on your PATH.\n\n"
    "MetaAgent is running as a packaged app, so it can't host a generated "
    "agent itself. Install Python from python.org (tick 'Add to PATH'), or "
    "make the 'py' launcher available, then try again — or run the agent's "
    "own compiled .exe from its dist\\ folder.")


def _child_env() -> dict | None:
    """Environment for spawning a REAL system Python from MetaAgent.

    When MetaAgent is frozen (PyInstaller), the running process has Python env
    vars (PYTHONHOME/PYTHONPATH/…) and a PATH pointing at the bundle so its OWN
    embedded interpreter works. If we spawn a system `python`/`py` and let it
    INHERIT that, the child interpreter is pointed at the frozen bundle instead of
    its own installation — it can't find its stdlib/site-packages, so an installed
    third-party lib reads as MISSING (the bug: 'install X' even though X is there).
    Strip those vars + the bundle dir from PATH so the child uses ITS OWN Python.
    Returns None (inherit unchanged) when MetaAgent runs from source."""
    if not getattr(sys, "frozen", False):
        return None
    env = dict(os.environ)
    for var in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONSAFEPATH"):
        env.pop(var, None)
    for k in list(env):
        if k.startswith("_PYI_") or k == "_MEIPASS":
            env.pop(k, None)
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        mp = os.path.normcase(os.path.abspath(meipass))
        keep = [p for p in env.get("PATH", "").split(os.pathsep)
                if p and not os.path.normcase(os.path.abspath(p)).startswith(mp)]
        env["PATH"] = os.pathsep.join(keep)
    return env


def _python_exe() -> str | None:
    """A REAL Python interpreter to run/build generated agents with.

    When MetaAgent runs from source, sys.executable is that Python — use it.
    When MetaAgent is frozen (PyInstaller), sys.executable is the MetaAgent
    .exe (not Python!), so launching '[sys.executable, "gui.py"]' would just
    relaunch MetaAgent — find a system Python on PATH instead."""
    if not getattr(sys, "frozen", False):
        return sys.executable
    candidates = (("py", "python", "python3") if sys.platform == "win32"
                  else ("python3", "python"))
    for cand in candidates:
        path = shutil.which(cand)
        if path:
            return path
    return None


def _required_imports(folder: str) -> list[tuple[str, str]]:
    """[(pip_name, import_name)] parsed from the agent's requirements.txt."""
    path = os.path.join(folder, "requirements.txt")
    mods = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                pkg = re.split(r"[<>=!~\s#]", line.strip(), 1)[0]
                if pkg:
                    mods.append((pkg, IMPORT_NAMES.get(pkg.lower(),
                                                       pkg.lower())))
    except FileNotFoundError:
        pass
    return mods


def missing_modules(folder: str) -> list[str]:
    """pip names from requirements.txt not importable by the interpreter that
    will actually run the agent (this Python from source; a system Python when
    MetaAgent is frozen, since the frozen app's bundled modules aren't it)."""
    reqs = _required_imports(folder)
    if not reqs:
        return []
    if not getattr(sys, "frozen", False):
        # Drop cached path listings so a package pip-installed moments ago (the
        # post-install re-check runs in this same long-lived host process) is
        # actually discovered instead of read as still-missing.
        importlib.invalidate_caches()
        return [pkg for pkg, imp in reqs
                if importlib.util.find_spec(imp) is None]
    py = _python_exe()
    if py is None:
        return [pkg for pkg, _ in reqs]   # no interpreter → treat all as missing
    code = ("import importlib.util, sys\n"
            "print('\\n'.join(m for m in sys.argv[1:] "
            "if importlib.util.find_spec(m) is None))")
    try:
        r = subprocess.run([py, "-c", code, *[imp for _, imp in reqs]],
                           capture_output=True, text=True, timeout=30,
                           env=_child_env())
        gone = set(r.stdout.split())
        return [pkg for pkg, imp in reqs if imp in gone]
    except Exception:
        return [pkg for pkg, _ in reqs]


_GUI_RELAY = None


def _on_gui_thread(fn) -> None:
    """Run fn() on the Qt GUI (main) thread — the analogue of wx.CallAfter.

    The install/launch step runs on a worker thread, but QMessageBox and the
    status callback may only touch the GUI from the main thread. If we're
    already there (or no QApplication exists yet) we call fn directly; otherwise
    we marshal it through a relay QObject parked on the GUI thread, whose queued
    `run` signal invokes the callable in that thread's event loop."""
    from PySide6.QtCore import QThread
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None or QThread.currentThread() is app.thread():
        fn()
        return
    global _GUI_RELAY
    if _GUI_RELAY is None:
        from PySide6.QtCore import QObject, Qt, Signal

        class _Relay(QObject):
            run = Signal(object)

            def __init__(self) -> None:
                super().__init__()
                self.run.connect(self._invoke, Qt.QueuedConnection)

            def _invoke(self, f) -> None:
                f()

        _GUI_RELAY = _Relay()
        _GUI_RELAY.moveToThread(app.thread())   # deliver to the GUI thread
    _GUI_RELAY.run.emit(fn)


def _warn_empty_keys(folder: str) -> None:
    from PySide6.QtWidgets import QMessageBox
    try:
        with open(os.path.join(folder, "config.json"), encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError):
        return
    llms = cfg.get("llms")
    if isinstance(llms, dict):  # pipeline agent: {agent: [cfg, ...]}
        empty = any(not c.get("api_key")
                    for cfgs in llms.values() for c in cfgs)
    else:                       # single agent: flat config
        empty = "api_key" in cfg and not cfg.get("api_key")
    if empty:
        QMessageBox.warning(
            None, "Empty API key",
            "Heads-up: at least one LLM in config.json has an empty API key — "
            "the agent will error on send until you fill it in "
            "(or use the GUI's LLM menu to switch).")


def _launch(folder: str, set_status) -> None:
    from PySide6.QtWidgets import QMessageBox
    py = _python_exe()
    if py is None:
        set_status("No Python found.")
        QMessageBox.warning(None, "Cannot run agent", _NO_PYTHON_MSG)
        return
    subprocess.Popen([py, "gui.py"], cwd=folder, env=_child_env())
    set_status(f"Launched: {os.path.join(folder, 'gui.py')}")


def _install_then_launch(folder: str, set_status) -> None:
    from PySide6.QtWidgets import QMessageBox
    py = _python_exe()
    if py is None:
        def no_python():
            QMessageBox.warning(None, "Cannot install", _NO_PYTHON_MSG)
            set_status("No Python found.")
        _on_gui_thread(no_python)
        return
    result = subprocess.run(
        [py, "-m", "pip", "install", "-r", "requirements.txt"],
        cwd=folder, capture_output=True, text=True, env=_child_env())

    def done():
        if result.returncode != 0:
            set_status("Install failed.")
            QMessageBox.critical(None, "Install failed",
                                 "pip install failed:\n" + result.stderr[-800:])
            return
        still = missing_modules(folder)
        if still:
            set_status("Install incomplete.")
            QMessageBox.critical(None, "Install incomplete",
                                 "Still missing after install: "
                                 + ", ".join(still))
            return
        _warn_empty_keys(folder)
        _launch(folder, set_status)

    _on_gui_thread(done)


def compile_agent(folder: str) -> tuple[bool, str]:
    """PyInstaller build inside a CLEAN venv (.buildenv) so only the agent's
    requirements get bundled — building with a fat Python pulls optional
    imports (pandas→scipy→torch...) and produces 200MB+ exes.
    Blocking; call from a worker thread."""
    py = _python_exe()
    if py is None:
        return False, _NO_PYTHON_MSG
    venv_py = os.path.join(folder, ".buildenv", "Scripts", "python.exe")
    cenv = _child_env()          # clean env so a frozen host doesn't corrupt the child
    try:
        if not os.path.exists(venv_py):
            subprocess.run([py, "-m", "venv", ".buildenv"],
                           cwd=folder, capture_output=True, text=True, env=cenv)
        if not os.path.exists(venv_py):
            # some Python distributions ship without the venv module
            subprocess.run([py, "-m", "pip", "install", "virtualenv"],
                           cwd=folder, capture_output=True, text=True, env=cenv)
            r = subprocess.run([py, "-m", "virtualenv", ".buildenv"],
                               cwd=folder, capture_output=True, text=True, env=cenv)
            if not os.path.exists(venv_py):
                return False, ("Could not create a build venv (no venv or "
                               "virtualenv available):\n"
                               + (r.stderr or "")[-800:])
        r = subprocess.run(
            [venv_py, "-m", "pip", "install", "-r", "requirements.txt",
             "pyinstaller"],
            cwd=folder, capture_output=True, text=True, env=cenv)
        if r.returncode != 0:
            return False, "pip install failed:\n" + (r.stderr or "")[-800:]
        name = os.path.basename(folder.rstrip("\\/"))
        # --onedir (folder, not one giant exe): faster cold start, and the
        # config/state JSON files sit visibly next to the exe.
        builds = [(name, ["--onedir", "--noconfirm", "--name", name,
                          "agent.py"])]
        if os.path.exists(os.path.join(folder, "gui.py")):
            builds.append((name + "_gui",
                           ["--onedir", "--windowed", "--noconfirm",
                            "--name", name + "_gui", "gui.py"]))
        for out_name, args in builds:
            r = subprocess.run([venv_py, "-m", "PyInstaller", *args],
                               cwd=folder, capture_output=True, text=True, env=cenv)
            if r.returncode != 0:
                return False, (f"PyInstaller failed on {args[-1]}:\n"
                               + (r.stderr or "")[-800:])
            dist_dir = os.path.join(folder, "dist", out_name)
            if os.path.isdir(dist_dir):
                shutil.copy(os.path.join(folder, "config.json"), dist_dir)
        return True, (f"Compiled in a clean venv (onedir).\n"
                      f"Run {folder}\\dist\\{name}\\{name}.exe — config.json "
                      "is already next to the exe.")
    except Exception as e:
        return False, f"Compile error: {e}"


def try_run_generated(folder: str | None, set_status=lambda s: None) -> None:
    """The 'Run GUI Agent' button: dependency check → (install) → launch."""
    from PySide6.QtWidgets import QMessageBox
    if not folder or not os.path.isdir(folder):
        QMessageBox.information(None, "No output yet", "Generate an agent first.")
        return
    if not os.path.isfile(os.path.join(folder, "gui.py")):
        QMessageBox.information(
            None, "No GUI",
            "This agent was generated without a GUI.\n"
            "Add a GUI module to the canvas and link it to the entry agent, then "
            "regenerate — or run it in a console:\n"
            f"    python {os.path.join(folder, 'agent.py')}")
        return

    missing = missing_modules(folder)
    if missing:
        if QMessageBox.question(
            None, "Missing modules",
            "The agent needs Python modules that are not installed:\n\n    "
            + ", ".join(missing)
            + "\n\nInstall them all now (pip install -r requirements.txt)?",
        ) != QMessageBox.Yes:
            return
        set_status("Installing requirements...")
        threading.Thread(target=_install_then_launch,
                         args=(folder, set_status), daemon=True).start()
        return

    _warn_empty_keys(folder)
    _launch(folder, set_status)
