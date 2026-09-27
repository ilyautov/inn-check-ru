#!/usr/bin/env python3
"""
install_native_host.py — ставит хост native messaging для расширения
inn-check-ru (extension/): запускатель и манифест хоста в папке браузера.

    python3 scripts/install_native_host.py --id <ID расширения>
    python3 scripts/install_native_host.py --id <ID> --браузер brave
    python3 scripts/install_native_host.py --удалить

ID расширения виден на chrome://extensions после «Загрузить распакованное»
(32 буквы a–p). Манифест разрешает хост ТОЛЬКО этому расширению
(allowed_origins). macOS и Linux; Windows — не в этой версии (там манифест
регистрируется в реестре).
"""

import json
import os
import re
import shlex
import sys
from pathlib import Path

ИМЯ = "ru.inn_check_ru.host"
ХОСТ = Path(__file__).resolve().parent / "native_host.py"
БРАУЗЕРЫ = {
    "darwin": {
        "chrome": "Library/Application Support/Google/Chrome",
        "chromium": "Library/Application Support/Chromium",
        "brave": "Library/Application Support/BraveSoftware/Brave-Browser",
        "edge": "Library/Application Support/Microsoft Edge",
    },
    "linux": {
        "chrome": ".config/google-chrome",
        "chromium": ".config/chromium",
        "brave": ".config/BraveSoftware/Brave-Browser",
        "edge": ".config/microsoft-edge",
    },
}
ID = re.compile(r"^[a-p]{32}$")


def пути(браузер, дом=None, платформа=None):
    """-> (манифест, запускатель). ValueError — платформа или браузер не поддержаны."""
    платформа = платформа or ("darwin" if sys.platform == "darwin"
                              else "linux" if sys.platform.startswith("linux") else sys.platform)
    if платформа not in БРАУЗЕРЫ:
        raise ValueError("платформа %s не поддержана: только macOS и Linux" % платформа)
    if браузер not in БРАУЗЕРЫ[платформа]:
        raise ValueError("браузер %r: %s" % (браузер, ", ".join(БРАУЗЕРЫ[платформа])))
    дом = Path(дом or os.path.expanduser("~"))
    манифест = дом / БРАУЗЕРЫ[платформа][браузер] / "NativeMessagingHosts" / (ИМЯ + ".json")
    # свой запускатель у каждого браузера: удаление для Chrome не ломает Brave
    запускатель = дом / ".local" / "share" / "inn-check-ru" / ("native-host-" + браузер)
    return манифест, запускатель


def установить(id_расширения, браузер="chrome", дом=None, платформа=None, python=None):
    if not ID.match(str(id_расширения or "")):
        raise ValueError("ID расширения — 32 буквы a–p (chrome://extensions)")
    манифест, запускатель = пути(браузер, дом, платформа)
    запускатель.parent.mkdir(parents=True, exist_ok=True)
    # Chrome запускает хост без оболочки пользователя: абсолютные пути, без PATH
    запускатель.write_text("#!/bin/sh\nexec %s %s\n" % (
        shlex.quote(python or sys.executable), shlex.quote(str(ХОСТ))), encoding="utf-8")
    запускатель.chmod(0o755)
    манифест.parent.mkdir(parents=True, exist_ok=True)
    манифест.write_text(json.dumps({
        "name": ИМЯ,
        "description": "inn-check-ru: проверка ИНН и ручные блоки из браузера",
        "path": str(запускатель),
        "type": "stdio",
        "allowed_origins": ["chrome-extension://%s/" % id_расширения],
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return манифест, запускатель


def удалить(браузер="chrome", дом=None, платформа=None):
    манифест, запускатель = пути(браузер, дом, платформа)
    удалено = [str(p) for p in (манифест, запускатель) if p.exists()]
    манифест.unlink(missing_ok=True)
    запускатель.unlink(missing_ok=True)
    return удалено


def main(argv):
    args = argv[1:]
    браузер = "chrome"
    if "--браузер" in args:
        i = args.index("--браузер")
        браузер = args[i + 1] if i + 1 < len(args) else ""
        del args[i:i + 2]
    try:
        if args == ["--удалить"]:
            print("удалено: %s" % (", ".join(удалить(браузер)) or "нечего"))
            return 0
        if len(args) == 2 and args[0] == "--id":
            манифест, запускатель = установить(args[1], браузер)
            print("манифест: %s\nзапускатель: %s\nПерезапустите браузер." % (
                манифест, запускатель))
            return 0
    except ValueError as e:
        sys.stderr.write("не установлено: %s\n" % e)
        return 2
    sys.stderr.write(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
