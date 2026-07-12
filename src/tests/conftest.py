"""Shared pytest setup: put the MetaAgent project root on sys.path so tests in
this folder can import the app modules (codegen, graph_codegen, graph_model, …).
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
