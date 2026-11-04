"""Tests for the OXP3Deck backend. Run from decky/: python3 -m unittest discover -s tests -t . -v"""
import os
import sys

# main.py does the same at import; tests import the package directly.
_PY_MODULES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "py_modules")
if _PY_MODULES not in sys.path:
    sys.path.insert(0, _PY_MODULES)
