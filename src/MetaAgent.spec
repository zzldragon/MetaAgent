# -*- mode: python ; coding: utf-8 -*-
#
# Optimized onedir spec for the MetaAgent DESIGNER app.
#
# The designer imports only PySide6 + openai + stdlib + its own modules. It does
# NOT import numpy/pandas/scipy/torch/etc. -- runtime/*.py (which lazily reference
# faiss/chromadb/sentence-transformers/numpy) is bundled as DATA and inlined into
# GENERATED agents as text; the designer never imports it. So we EXCLUDE the whole
# ML / scientific / Jupyter / test stack. On a fat build env this cuts _internal
# from ~800 MB to ~150-200 MB (PySide6 is the bulk); on a clean venv the excludes
# are simply no-ops.
#
# BUILD WITH THIS SPEC (so the excludes are honoured):
#     .\build-venv\Scripts\python.exe -m PyInstaller --noconfirm --clean MetaAgent.spec
# Do NOT run `pyinstaller main.py ...` -- that REGENERATES this file and drops the
# excludes.  Verify the log line `Python environment:` points at build-venv.
#
# LIMITATION: a compiled designer can DESIGN / GENERATE / COMPILE data-/RAG-/vision
# agents, but Debug-Running one IN-PROCESS needs its libs (pandas/PIL/numpy/...),
# which are excluded here -- same as a clean-venv build. Run those agents from their
# own generated folder (they ship their own requirements.txt).
#
# To add extra libs for in-process Debug Run WITHOUT rebuilding, drop a `pylibs/`
# folder next to the exe and `pip install --target pylibs <pkg>` — main.py appends it
# to sys.path at startup.

from PyInstaller.utils.hooks import copy_metadata

datas = [('templates', 'templates'), ('runtime', 'runtime'), ('assets', 'assets'),
         ('graphs', 'graphs'), ('tools', 'tools'),
         # the Designer agent reads the design skill (SKILL.md + ConfigTable.md)
         # at runtime — bundle it or read_config_table / its system prompt are empty.
         ('.claude/skills', '.claude/skills')]
datas += copy_metadata('openai')

# Heavy packages the DESIGNER never imports. If a built exe ever errors with
# "No module named X", remove X from this list and rebuild.
EXCLUDES = [
    # deep-learning / inference
    'torch', 'torchvision', 'torchaudio', 'tensorflow', 'tensorboard',
    'onnxruntime', 'onnx', 'transformers', 'sentence_transformers', 'fastembed',
    'sympy', 'numba', 'llvmlite',
    # scientific / data
    'scipy', 'pandas', 'numpy', 'pyarrow', 'matplotlib', 'sklearn',
    'statsmodels', 'tables', 'h5py', 'xarray',
    # vector stores (generated-agent RAG only)
    'faiss', 'chromadb', 'hnswlib',
    # Jupyter / IPython / REPL tooling
    'IPython', 'ipykernel', 'ipywidgets', 'jedi', 'parso', 'nbformat',
    'nbconvert', 'notebook', 'jupyter', 'jupyter_client', 'jupyter_core',
    'zmq', 'tornado', 'traitlets',
    # docs / images / office (generated-agent tools only)
    'PIL', 'cv2', 'fitz', 'lxml', 'openpyxl', 'bs4',
    # the retired wx host + test framework
    'wx', 'pytest', '_pytest', 'py',
    # stdlib GUI we don't use
    'tkinter',
]

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='MetaAgent',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['assets\\MetaAgent.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='MetaAgent',
)
