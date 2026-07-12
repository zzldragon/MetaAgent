"""Compile MetaAgent's CORE modules to .pyd (Nuitka) so they ship without source.

Why this instead of moving files into a package: a Nuitka `--module` build produces
`graph_codegen.pyd`, a DROP-IN replacement for `graph_codegen.py`. Every existing
`import graph_codegen` keeps working with zero import rewrites, so the whole shell /
test suite is untouched. We compile in place and back up the originals, fully
reversible.

Usage (run from the repo root):
    python release/protect_core.py status      # show the open-core classification
    python release/protect_core.py build       # compile CORE -> .pyd, move .py to backup
    python release/protect_core.py verify       # run the regression suite on the .pyd build
    python release/protect_core.py restore      # put the original .py back, delete .pyd

Requires: pip install nuitka   (and a C compiler; on Windows Nuitka fetches MinGW).
Templates / runtime fragments are deliberately NOT compiled -- their source is
inlined into generated agents and cannot be hidden (see release/core_manifest.py).
"""

import os
import shutil
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
BACKUP = os.path.join(_HERE, "_py_backup")

sys.path.insert(0, _HERE)
import core_manifest as manifest  # noqa: E402


def _have_nuitka():
    import importlib.util
    return importlib.util.find_spec("nuitka") is not None


def _pyd_for(name):
    """Any compiled extension produced for `name` (e.g. name.pyd / name.cp311-*.pyd)."""
    import glob
    hits = glob.glob(os.path.join(ROOT, name + ".pyd"))
    hits += glob.glob(os.path.join(ROOT, name + ".*.pyd"))
    return hits


def cmd_status():
    print(manifest.summary())
    print()
    built = [n for n in manifest.CORE if _pyd_for(n)]
    src = [n for n in manifest.CORE if os.path.isfile(os.path.join(ROOT, n + ".py"))]
    print("compiled (.pyd present):", ", ".join(built) or "(none)")
    print("source (.py present):   ", ", ".join(src) or "(none)")
    print("backup dir:", BACKUP, "(exists)" if os.path.isdir(BACKUP) else "(none)")


def cmd_build():
    if not _have_nuitka():
        sys.exit("[ERROR] Nuitka not installed. Run: pip install nuitka")
    if os.path.isdir(BACKUP) and os.listdir(BACKUP):
        sys.exit(f"[ERROR] backup dir not empty: {BACKUP}\n"
                 "        run 'restore' first, or clear it manually.")
    os.makedirs(BACKUP, exist_ok=True)

    for name in manifest.CORE:
        src = os.path.join(ROOT, name + ".py")
        if not os.path.isfile(src):
            print(f"[skip] {name}.py not found")
            continue
        print(f"[nuitka] compiling {name} ...")
        r = subprocess.run(
            [sys.executable, "-m", "nuitka", "--module", src,
             f"--output-dir={ROOT}", "--remove-output", "--assume-yes-for-downloads"],
            cwd=ROOT,
        )
        if r.returncode != 0 or not _pyd_for(name):
            sys.exit(f"[ERROR] Nuitka failed for {name} -- aborting, "
                     "originals still in place. Fix and re-run.")
        shutil.move(src, os.path.join(BACKUP, name + ".py"))
        # Nuitka drops a .pyi stub next to the .pyd; keep the tree tidy.
        stub = os.path.join(ROOT, name + ".pyi")
        if os.path.isfile(stub):
            os.remove(stub)
        print(f"  ok -> {os.path.basename(_pyd_for(name)[0])} (source moved to backup)")

    print("\n[done] CORE compiled. Now run: python release/protect_core.py verify")


def cmd_verify():
    """Prove the .pyd build still passes the regression suite."""
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONIOENCODING="utf-8")
    tests = ["tests/test_generate_matrix.py", "tests/test_marker_guard.py",
             "tests/test_codegen_format.py"]
    print("[verify] running regression on the compiled build ...")
    r = subprocess.run([sys.executable, "-m", "pytest", *tests, "-q"],
                       cwd=ROOT, env=env)
    if r.returncode != 0:
        sys.exit("[ERROR] regression FAILED on the .pyd build. "
                 "Run 'restore' to get your source back, then investigate.")
    print("[verify] PASS -- the compiled core behaves identically.")


def cmd_restore():
    if not os.path.isdir(BACKUP):
        sys.exit(f"[ERROR] no backup dir: {BACKUP}")
    n = 0
    for name in manifest.CORE:
        bak = os.path.join(BACKUP, name + ".py")
        if os.path.isfile(bak):
            shutil.move(bak, os.path.join(ROOT, name + ".py"))
            n += 1
        for pyd in _pyd_for(name):
            os.remove(pyd)
    if not os.listdir(BACKUP):
        os.rmdir(BACKUP)
    print(f"[restore] restored {n} source file(s), removed .pyd builds.")


_CMDS = {"status": cmd_status, "build": cmd_build,
         "verify": cmd_verify, "restore": cmd_restore}

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd not in _CMDS:
        sys.exit(f"unknown command '{cmd}'. one of: {', '.join(_CMDS)}")
    _CMDS[cmd]()
