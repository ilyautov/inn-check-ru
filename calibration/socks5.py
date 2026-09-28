"""Совместимость: SOCKS5-клиент переехал в scripts/socks5.py (его теперь берёт и
движок через scripts/proxy.py). Здесь — тот же модуль, без копии кода."""

import importlib.util
import os

_путь = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "scripts",
                     "socks5.py")
_spec = importlib.util.spec_from_file_location("_socks5_scripts", _путь)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
globals().update({k: v for k, v in vars(_mod).items() if not k.startswith("__")})
