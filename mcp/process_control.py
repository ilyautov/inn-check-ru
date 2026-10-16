"""Compatibility exports for the canonical shipped Windows process owner.

Implementation lives in scripts/process_windows.py, included in wheel and skill.
"""
import importlib.util
import sys
from pathlib import Path

try:
    from sibling_imports import load_sibling
except ModuleNotFoundError as error:
    if error.name != "sibling_imports":
        raise
    path = Path(__file__).resolve().parents[1] / "scripts" / "sibling_imports.py"
    spec = importlib.util.spec_from_file_location("sibling_imports", path)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    load_sibling = sys.modules.setdefault("sibling_imports", helper).load_sibling

_control = load_sibling("process_windows")
CREATE_SUSPENDED = _control.CREATE_SUSPENDED
WindowsJob = _control.WindowsJob

__all__ = ["CREATE_SUSPENDED", "WindowsJob"]
