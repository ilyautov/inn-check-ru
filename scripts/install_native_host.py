#!/usr/bin/env python3
"""
install_native_host.py — ставит хост native messaging для расширения
inn-check-ru (extension/): запускатель и манифест хоста в папке браузера.

    python3 scripts/install_native_host.py --id <ID расширения>
    python3 scripts/install_native_host.py --id <ID> --браузер brave
    python3 scripts/install_native_host.py --удалить

ID расширения виден на chrome://extensions после «Загрузить распакованное»
(32 буквы a–p). Манифест разрешает хост ТОЛЬКО этому расширению
(allowed_origins). macOS, Linux и Windows. На Windows манифест и запускатель
(.bat) лежат в %LOCALAPPDATA%\\inn-check-ru, а путь к манифесту пишется в ключ
HKCU\\Software\\<браузер>\\NativeMessagingHosts\\ru.inn_check_ru.host.
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
    # Windows: не папка браузера, а ключ реестра в HKEY_CURRENT_USER
    "win32": {
        "chrome": r"Software\Google\Chrome",
        "chromium": r"Software\Chromium",
        "brave": r"Software\BraveSoftware\Brave-Browser",
        "edge": r"Software\Microsoft\Edge",
    },
}
ID = re.compile(r"^[a-p]{32}$")


def _платформа(платформа=None):
    платформа = платформа or ("darwin" if sys.platform == "darwin"
                              else "linux" if sys.platform.startswith("linux") else sys.platform)
    if платформа not in БРАУЗЕРЫ:
        raise ValueError("платформа %s не поддержана: только macOS, Linux и Windows" % платформа)
    return платформа


def ключ_реестра(браузер):
    """Ключ HKCU, где Chromium на Windows ищет путь к манифесту хоста."""
    return БРАУЗЕРЫ["win32"][браузер] + "\\NativeMessagingHosts\\" + ИМЯ


class _Реестр:
    """HKCU через winreg; в тестах подменяется словарём с тем же интерфейсом."""

    def записать(self, ключ, значение):
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, ключ) as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, значение)

    def удалить(self, ключ):
        import winreg
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, ключ)
            return True
        except FileNotFoundError:
            return False


def _bat(python, хост, запускатель):
    # cmd раскрывает %, ^ и ! даже в кавычках, а кавычку в пути не экранировать:
    # такие пути не пишем в .bat, а отказываем. Путь самого .bat — тоже: Chrome
    # запускает его через cmd.exe /c (ревью Codex)
    for путь in (python, хост, запускатель):
        if any(c in str(путь) for c in '"%^!&<>|\r\n'):
            raise ValueError("путь с символом, опасным для cmd: %s" % путь)
    # PYTHONUTF8: иначе на cp1252 хост и batch_check (дочерний процесс) не
    # кодируют кириллицу JSON в stdout (ревью Codex)
    return '@echo off\r\nset "PYTHONUTF8=1"\r\n"%s" "%s" %%*\r\n' % (python, хост)


def пути(браузер, дом=None, платформа=None):
    """-> (манифест, запускатель). ValueError — платформа или браузер не поддержаны."""
    платформа = _платформа(платформа)
    if браузер not in БРАУЗЕРЫ[платформа]:
        raise ValueError("браузер %r: %s" % (браузер, ", ".join(БРАУЗЕРЫ[платформа])))
    if платформа == "win32":
        # дом здесь — %LOCALAPPDATA%; свой манифест и .bat у каждого браузера
        дом = Path(дом or os.environ.get("LOCALAPPDATA")
                   or Path(os.path.expanduser("~")) / "AppData" / "Local")
        папка = дом / "inn-check-ru" / "native-host"
        return (папка / ("%s-%s.json" % (ИМЯ, браузер)),
                папка / ("native-host-%s.bat" % браузер))
    дом = Path(дом or os.path.expanduser("~"))
    манифест = дом / БРАУЗЕРЫ[платформа][браузер] / "NativeMessagingHosts" / (ИМЯ + ".json")
    # свой запускатель у каждого браузера: удаление для Chrome не ломает Brave
    запускатель = дом / ".local" / "share" / "inn-check-ru" / ("native-host-" + браузер)
    return манифест, запускатель


def установить(id_расширения, браузер="chrome", дом=None, платформа=None, python=None,
               реестр=None):
    if not ID.match(str(id_расширения or "")):
        raise ValueError("ID расширения — 32 буквы a–p (chrome://extensions)")
    платформа = _платформа(платформа)
    манифест, запускатель = пути(браузер, дом, платформа)
    # Chrome запускает хост без оболочки пользователя: абсолютные пути, без PATH
    python = python or sys.executable
    if платформа == "win32":
        # cmd читает .bat в OEM-кодировке консоли (cp866 у русской Windows), не
        # в UTF-8: иначе кириллица в пути (имя пользователя) ломает запуск.
        # Всё проверяется до записи файлов.
        кодировка = "oem" if sys.platform == "win32" else "utf-8"
        try:
            bat = _bat(python, ХОСТ, запускатель).encode(кодировка)
        except UnicodeEncodeError:
            raise ValueError("путь к Python или хосту не представим в кодировке "
                             "консоли — перенесите репозиторий в путь латиницей") from None
        запускатель.parent.mkdir(parents=True, exist_ok=True)
        запускатель.write_bytes(bat)
    else:
        запускатель.parent.mkdir(parents=True, exist_ok=True)
        запускатель.write_text("#!/bin/sh\nexec %s %s\n" % (
            shlex.quote(python), shlex.quote(str(ХОСТ))), encoding="utf-8")
        запускатель.chmod(0o755)
    манифест.parent.mkdir(parents=True, exist_ok=True)
    манифест.write_text(json.dumps({
        "name": ИМЯ,
        "description": "inn-check-ru: проверка ИНН и ручные блоки из браузера",
        "path": str(запускатель),
        "type": "stdio",
        "allowed_origins": ["chrome-extension://%s/" % id_расширения],
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if платформа == "win32":
        try:
            (_Реестр() if реестр is None else реестр).записать(
                ключ_реестра(браузер), str(манифест))
        except OSError as e:
            # без ключа браузер хост не найдёт — файлы не оставляем
            манифест.unlink(missing_ok=True)
            запускатель.unlink(missing_ok=True)
            raise ValueError("ключ реестра не записан (%s)" % e) from None
    return манифест, запускатель


def удалить(браузер="chrome", дом=None, платформа=None, реестр=None):
    платформа = _платформа(платформа)
    манифест, запускатель = пути(браузер, дом, платформа)
    удалено = [str(p) for p in (манифест, запускатель) if p.exists()]
    манифест.unlink(missing_ok=True)
    запускатель.unlink(missing_ok=True)
    if платформа == "win32" and (_Реестр() if реестр is None else реестр).удалить(ключ_реестра(браузер)):
        удалено.append("HKCU\\" + ключ_реестра(браузер))
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
    except (ValueError, OSError) as e:
        sys.stderr.write("не установлено: %s\n" % e)
        return 2
    sys.stderr.write(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
