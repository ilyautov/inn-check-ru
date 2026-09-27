#!/usr/bin/env python3
"""
run_native_host_windows.py — живая установка хоста на Windows (CI, windows-latest):
настоящий ключ HKCU, .bat в OEM-кодировке, запуск так, как его запускает Chrome
(cmd.exe /d /c <bat> <origin>), двоичные кадры с байтами \\r и \\n в длине.
Вне Windows — SKIP (там установщик проверяет run_native_host_eval.py с
подменённым реестром). PASS/FAIL, код возврата.
"""

import importlib.util
import json
import os
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ID = "b" * 32


def check(errors, cond, msg):
    if not cond:
        errors.append(msg)


def кадр(obj):
    тело = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    return struct.pack("=I", len(тело)) + тело


def ответы(сырые):
    out = []
    while len(сырые) >= 4:
        n = struct.unpack("=I", сырые[:4])[0]
        out.append(json.loads(сырые[4:4 + n].decode("utf-8")))
        сырые = сырые[4 + n:]
    return out


def main():
    if sys.platform != "win32":
        print("SKIP: не Windows")
        return 0
    import winreg
    # вывод eval — в UTF-8 (у раннера cp1252); окружение не трогаем: хост должен
    # получить UTF-8 из самого .bat, а не от родителя
    sys.stdout.reconfigure(encoding="utf-8")
    spec = importlib.util.spec_from_file_location(
        "install_native_host", ROOT / "scripts" / "install_native_host.py")
    inst = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(inst)
    errors = []
    check(errors, os.environ.get("PYTHONUTF8") is None, "PYTHONUTF8 задан в окружении CI — тест .bat не честен")
    манифест, bat = inst.установить(ID, "chrome")
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, inst.ключ_реестра("chrome")) as k:
            значение, _ = winreg.QueryValueEx(k, "")
        check(errors, значение == str(манифест), "реестр: %r" % значение)
        данные = json.loads(манифест.read_text(encoding="utf-8"))
        check(errors, данные["path"] == str(bat), "манифест: %r" % данные)
        # Заголовки кадров с байтами, которые текстовый режим stdio Windows портит:
        # 0D 0A (пара схлопнулась бы — хост ждал бы байт вечно), 1A (Ctrl+Z — ложный
        # EOF), одиночные 0A и 0D. Ответы: хост повторяет имя команды, так что длины
        # ответов проходят все остатки по 256, включая 0A (в текстовом режиме — 0D 0A).
        пусто = len(json.dumps({"команда": "нет", "x": ""}, ensure_ascii=False).encode("utf-8"))
        сообщения = [{"команда": "нет", "x": "a" * (n - пусто)}
                     for n in (0x0A0D, 0x0A0D + 0x10000, 0x1A + 0x300, 0x030A, 0x050D)]
        сообщения += [{"команда": "x" * k} for k in range(1, 300)]
        заголовки = b"".join(кадр(m)[:4] for m in сообщения)
        check(errors, b"\r\n" in заголовки and b"\x1a" in заголовки,
              "во входе нет заголовков с 0D 0A и 1A")
        вход = b"".join(кадр(m) for m in сообщения)
        proc = subprocess.run(["cmd.exe", "/d", "/c", str(bat), "chrome-extension://%s/" % ID],
                              input=вход, capture_output=True, timeout=120, check=False)
        try:
            got = ответы(proc.stdout)
        except ValueError as e:              # рассинхронизация кадров ответа
            got = ["сбой разбора: %s" % e]
        длины = {len(json.dumps(r, ensure_ascii=False).encode("utf-8")) % 256
                 for r in got if isinstance(r, dict)}
        check(errors, len(got) == len(сообщения) and all(
            isinstance(r, dict) and r.get("ok") is False
            and "не поддерживается" in r.get("ошибка", "") for r in got),
            "ответов %d из %d: %r, stderr %r" % (len(got), len(сообщения), got[-3:],
                                                 proc.stderr[:300]))
        check(errors, 0x0A in длины, "среди ответов нет длины с байтом 0A")
        # UTF-8 для хоста и дочерних процессов: иначе на cp1252 кириллица
        # в JSON «проверить» (batch_check) не кодируется
        check(errors, 'set "PYTHONUTF8=1"' in bat.read_text(encoding="ascii", errors="replace"),
              ".bat не включает UTF-8")
    finally:
        удалено = inst.удалить("chrome")
    check(errors, len(удалено) == 3 and not bat.exists(), "удаление: %r" % удалено)
    try:
        winreg.OpenKey(winreg.HKEY_CURRENT_USER, inst.ключ_реестра("chrome")).Close()
        errors.append("ключ реестра остался после удаления")
    except FileNotFoundError:
        pass
    if errors:
        print("FAIL установка-хоста-windows")
        for e in errors:
            print("  -", e)
        return 1
    print("PASS установка-хоста-windows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
