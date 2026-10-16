"""Import flat engine modules normally, or from this fixed sibling directory.

The fallback uses Python's module lock/loader so lazy circular imports, failed
initialization and competing threads obey the same rules as normal imports.
"""
import importlib
import importlib.util
import sys
from importlib import _bootstrap
from pathlib import Path


def load_sibling(name):
    if not isinstance(name, str) or not name.isidentifier():
        raise ValueError("invalid_sibling_module")
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as error:
        if error.name != name:
            raise
    # Importlib's per-name lock also coordinates with concurrent normal imports;
    # a separate application lock could deadlock against the interpreter's lock.
    module_lock = getattr(_bootstrap, "_ModuleLockManager", None)
    module_load = getattr(_bootstrap, "_load", None)
    if module_lock is None or module_load is None:
        raise ImportError("sibling fallback requires Python importlib module locks")
    with module_lock(name):
        if name in sys.modules:
            return importlib.import_module(name)
        path = Path(__file__).resolve().parent / (name + ".py")
        spec = importlib.util.spec_from_file_location(name, path)
        return module_load(spec)
