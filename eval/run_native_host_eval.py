#!/usr/bin/env python3
"""
run_native_host_eval.py — native_host.py офлайн: протокол кадров (длина, обрыв,
не UTF-8, лимиты), граница доверия (белый список команд и полей, ИНН, id операции,
сайт источника, symlink), запись ручного блока и снимка в пакет, повторы без
дублей, живой процесс по stdin/stdout. PASS/FAIL, stdlib, CI.
"""

import base64
import importlib.util
import io
import json
import os
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ИНН = "7707083893"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def load():
    sys.path.insert(0, str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location("native_host",
                                                  ROOT / "scripts" / "native_host.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check(errors, cond, msg):
    if not cond:
        errors.append(msg)


def кадр(данные):
    тело = данные if isinstance(данные, bytes) else json.dumps(
        данные, ensure_ascii=False).encode("utf-8")
    return struct.pack("=I", len(тело)) + тело


def блок_суды(**kw):
    m = {"команда": "ручной_блок", "операция": "op-00000001", "инн": ИНН, "блок": "суды",
         "url": "https://kad.arbitr.ru/Card/1", "дата": "2026-09-27", "итог": "проверено",
         "поля": {"ответчик_крупные": False, "ответчик_дел": 2}}
    m.update(kw)
    return m


def case_кадры(h):
    errors = []
    check(errors, h.читать_кадр(io.BytesIO(b"")) is None, "EOF не None")
    check(errors, h.читать_кадр(io.BytesIO(кадр({"a": 1}))) == {"a": 1}, "кадр не прочитан")
    for имя, байты, причина in (("обрыв длины", b"\x01\x00", "длине"),
                                ("обрыв тела", struct.pack("=I", 10) + b"{}", "оборван"),
                                # отказ по длине — до чтения тела, а не по обрыву
                                ("сверх лимита", struct.pack("=I", h.МАКС_ВХОД + 1)
                                 + b" " * 64, "лимита"),
                                ("не UTF-8", кадр(b"\xff\xfe"), "UTF-8"),
                                ("не JSON", кадр(b"{"), "JSON"),
                                ("не объект", кадр(b"[1]"), "объект")):
        try:
            h.читать_кадр(io.BytesIO(байты))
            errors.append("%s: принят" % имя)
        except h.Отказ as e:
            check(errors, причина in str(e), "%s: другая причина — %s" % (имя, e))
    буф = io.BytesIO()
    h.писать_кадр(буф, {"x": "я" * (h.МАКС_ОТВЕТ)})
    (длина,) = struct.unpack("=I", буф.getvalue()[:4])
    check(errors, длина <= h.МАКС_ОТВЕТ and "лимит" in буф.getvalue()[4:].decode(),
          "ответ сверх 1 МБ не заменён ошибкой (%d)" % длина)
    return errors


def case_граница(h, корень):
    errors = []
    for имя, сообщение, ждём in (
            ("чужая команда", {"команда": "sh", "cmd": "ls"}, "не поддерживается"),
            ("лишнее поле", блок_суды(путь="/etc/passwd"), "лишние поля"),
            ("не строка", блок_суды(инн=7707083893), "ожидается строка"),
            ("битый ИНН", блок_суды(инн="7707083894"), "контрольной"),
            ("id операции", блок_суды(операция="../x"), "операция"),
            ("не браузерный блок", блок_суды(блок="санкции"), "знает только"),
            ("http", блок_суды(url="http://kad.arbitr.ru/"), "сайта источника"),
            ("чужой хост", блок_суды(url="https://kad.arbitr.ru.evil.ru/"), "сайта источника"),
            ("хост в запросе", блок_суды(url="https://evil.ru/?kad.arbitr.ru"), "сайта источника"),
            ("хост в userinfo", блок_суды(url="https://kad.arbitr.ru@evil.ru/"), "сайта источника"),
            ("поля не объект", блок_суды(поля=["x"]), "поля"),
            ("обратная косая в хосте", блок_суды(url="https://evil.example\\.kad.arbitr.ru/"),
             "сайта источника"),
            ("чужой порт", блок_суды(url="https://kad.arbitr.ru:8443/"), "сайта источника"),
            ("чужой ИНН в параметрах", блок_суды(параметры="ИНН 7736050003"), "в параметрах"),
            ("без полей сигналов", блок_суды(поля={}), "без полей сигналов"),
            ("профиль", {"команда": "проверить", "инн": ИНН, "профиль": "x"}, "профиль")):
        ответ = h.обработать(сообщение, корень=корень, запуск=lambda *a: {})
        check(errors, ответ.get("ok") is False and ждём in str(ответ.get("ошибка")),
              "%s: %r" % (имя, ответ))
    check(errors, not (корень / ИНН).exists() or not any((корень / ИНН).rglob("*.json")),
          "отказ оставил файлы")
    return errors


def case_ручной_блок(h, корень):
    errors = []
    ответ = h.обработать(блок_суды(), корень=корень)
    путь = корень / ИНН / "ручные" / "суды.json"
    check(errors, ответ.get("ok") and ответ.get("состояние") == "ok" and путь.is_file(),
          "блок не записан/не засчитан: %r" % ответ)
    блок = json.loads(путь.read_text(encoding="utf-8"))
    check(errors, блок.get("ответчик_крупные") is False and блок.get("инн") == ИНН,
          "содержимое: %r" % блок)
    путь.write_text("{}", encoding="utf-8")
    повтор = h.обработать(блок_суды(), корень=корень)
    check(errors, повтор.get("повтор") is True and путь.read_text() == "{}",
          "повтор операции переписал файл: %r" % повтор)
    непров = h.обработать(блок_суды(операция="op-00000002", итог="не проверено", поля={},
                                    причина="капча не пройдена"), корень=корень)
    check(errors, непров.get("ok") and непров.get("состояние") == "не проверено",
          "«не проверено» не записано: %r" % непров)
    # symlink на пути данных — отказ
    чужая = корень / "чужая"
    чужая.mkdir()
    (корень / "7736050003").symlink_to(чужая)
    ответ = h.обработать(блок_суды(операция="op-00000003", инн="7736050003"), корень=корень)
    check(errors, ответ.get("ok") is False and "symlink" in ответ.get("ошибка", ""),
          "symlink на пути: %r" % ответ)
    check(errors, not any(чужая.iterdir()), "запись прошла через symlink")
    return errors


def case_доказательство(h, корень):
    errors = []
    сообщение = {"команда": "доказательство", "операция": "ev-00000001", "инн": ИНН,
                 "блок": "фссп", "url": "https://fssp.gov.ru/iss/ip", "дата": "2026-09-27",
                 "png": "data:image/png;base64," + base64.b64encode(PNG).decode(),
                 "найдено": "0 производств"}
    ответ = h.обработать(сообщение, корень=корень)
    check(errors, ответ.get("ok") is False and "пакета доказательств нет" in ответ["ошибка"],
          "без пакета: %r" % ответ)
    (корень / ИНН / "пакет").mkdir(parents=True, exist_ok=True)
    (корень / ИНН / "пакет" / "manifest.json").write_text("{}", encoding="utf-8")
    вызовы = []

    def добавить(пакет, файл, ист, url, дата, параметры=None, найдено=None):
        вызовы.append((пакет, Path(файл).read_bytes(), ист, url, дата, параметры, найдено))
        return {"версия": 2, "штамп": "не поставлен: сеть"}
    ответ = h.обработать(dict(сообщение, png=base64.b64encode(b"GIF89a").decode()),
                         корень=корень, добавить=добавить)
    check(errors, ответ.get("ok") is False and "не PNG" in ответ["ошибка"], "не PNG: %r" % ответ)
    ответ = h.обработать(сообщение, корень=корень, добавить=добавить)
    check(errors, ответ.get("ok") and ответ.get("статус") == "сохранено, штамп не поставлен"
          and ответ.get("версия") == 2, "снимок: %r" % ответ)
    check(errors, len(вызовы) == 1 and вызовы[0][1] == PNG and вызовы[0][2] == "фссп"
          and вызовы[0][5] == "ИНН " + ИНН, "аргументы добавления: %r" % вызовы[:1])
    check(errors, not any((корень / ".снимки").iterdir()), "временный PNG не удалён")
    повтор = h.обработать(сообщение, корень=корень, добавить=добавить)
    check(errors, повтор.get("повтор") is True and len(вызовы) == 1,
          "повтор операции добавил второе вложение")
    # хост упал после добавления, до записи итога в журнал: повтор не добавляет
    исходный = h.Журнал.запомнить

    def запомнить_и_упасть(self, операция, ответ):
        if ответ.get("ok"):
            raise OSError("диск")
        return исходный(self, операция, ответ)
    h.Журнал.запомнить = запомнить_и_упасть
    try:
        первый = h.обработать(dict(сообщение, операция="ev-00000002"), корень=корень,
                              добавить=добавить)
    finally:
        h.Журнал.запомнить = исходный
    второй = h.обработать(dict(сообщение, операция="ev-00000002"), корень=корень,
                          добавить=добавить)
    check(errors, первый.get("ok") is False and len(вызовы) == 2
          and второй.get("ok") is False and "не завершилась" in второй.get("ошибка", ""),
          "повтор после сбоя журнала: %r / %r, вызовов %d" % (первый, второй, len(вызовы)))
    # symlink на месте <ИНН>: пакет снаружи каталога хоста — отказ, без записи
    чужая = корень.parent / (корень.name + "-снаружи")
    (чужая / "пакет").mkdir(parents=True)
    (чужая / "пакет" / "manifest.json").write_text("{}", encoding="utf-8")
    (корень / "7736050003").symlink_to(чужая)
    ответ = h.обработать(dict(сообщение, операция="ev-00000003", инн="7736050003"),
                         корень=корень, добавить=добавить)
    check(errors, ответ.get("ok") is False and "symlink" in ответ.get("ошибка", "")
          and len(вызовы) == 2, "пакет через symlink: %r" % ответ)
    return errors


def case_проверить(h):
    errors = []
    argv = []

    def запуск(скрипт, args):
        argv.append((скрипт, args))
        return {"результаты": [{"инн": ИНН, "светофор": "🟢"}]}
    ответ = h.обработать({"команда": "проверить", "инн": ИНН, "профиль": "отсрочка"},
                         запуск=запуск)
    check(errors, ответ.get("ok") and ответ["результат"]["светофор"] == "🟢", "%r" % ответ)
    check(errors, argv == [("batch_check.py", ["--json", "--тихо", "--профиль", "отсрочка", ИНН])],
          "argv: %r" % argv)
    return errors


def case_процесс(корень):
    """Живой процесс: два кадра — два ответа, stdout только протокол."""
    errors = []
    вход = кадр({"команда": "нет"}) + кадр(блок_суды(операция="op-proc-0001"))
    дом = корень / "дом"
    (дом / ".config" / "inn-check-ru").mkdir(parents=True)
    (дом / ".config" / "inn-check-ru" / "host.json").write_text(
        json.dumps({"каталог": str(корень / "данные")}), encoding="utf-8")
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "native_host.py")],
                          input=вход, capture_output=True, timeout=60,
                          env=dict(os.environ, HOME=str(дом)), check=False)
    out, ответы = proc.stdout, []
    while out:
        (n,) = struct.unpack("=I", out[:4])
        ответы.append(json.loads(out[4:4 + n].decode("utf-8")))
        out = out[4 + n:]
    check(errors, proc.returncode == 0 and len(ответы) == 2, "код %s, ответов %d: %r"
          % (proc.returncode, len(ответы), proc.stderr[:300]))
    check(errors, ответы and ответы[0].get("ok") is False and ответы[-1].get("ok") is True,
          "ответы: %r" % ответы)
    check(errors, (корень / "данные" / ИНН / "ручные" / "суды.json").is_file(),
          "каталог из host.json не использован")
    return errors


def case_установка(корень):
    """Установщик: манифест только для своего расширения, запускатель с
    абсолютными путями реально поднимает хост; Windows и чужой ID — отказ."""
    errors = []
    spec = importlib.util.spec_from_file_location(
        "install_native_host", ROOT / "scripts" / "install_native_host.py")
    inst = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(inst)
    id_ = "a" * 32
    for плохо, kw in (("ID", {"id_расширения": "x" * 32}),
                      ("платформа", {"id_расширения": id_, "платформа": "win32"}),
                      ("браузер", {"id_расширения": id_, "браузер": "firefox"})):
        try:
            inst.установить(дом=корень, платформа=kw.pop("платформа", "linux"), **kw)
            errors.append("%s: установлено" % плохо)
        except ValueError:
            pass
    for платформа in ("darwin", "linux"):
        манифест, запускатель = inst.установить(id_, дом=корень / платформа,
                                                платформа=платформа)
        данные = json.loads(манифест.read_text(encoding="utf-8"))
        check(errors, данные["allowed_origins"] == ["chrome-extension://%s/" % id_]
              and данные["name"] == inst.ИМЯ and данные["type"] == "stdio"
              and Path(данные["path"]).is_absolute(), "%s: манифест %r" % (платформа, данные))
        check(errors, os.access(запускатель, os.X_OK), "%s: запускатель не исполняемый"
              % платформа)
    proc = subprocess.run([str(запускатель)], input=кадр({"команда": "нет"}),
                          capture_output=True, timeout=60, check=False)
    ответ = json.loads(proc.stdout[4:].decode("utf-8")) if len(proc.stdout) > 4 else {}
    check(errors, ответ.get("ok") is False and "не поддерживается" in ответ.get("ошибка", ""),
          "запускатель не поднял хост: %r %r" % (ответ, proc.stderr[:200]))
    _, brave = inst.установить(id_, "brave", дом=корень / "linux", платформа="linux")
    удалено = inst.удалить(дом=корень / "linux", платформа="linux")
    check(errors, len(удалено) == 2 and not манифест.exists(), "удаление: %r" % удалено)
    check(errors, brave.exists(), "удаление для Chrome сломало запускатель Brave")
    return errors


def main():
    h = load()
    with tempfile.TemporaryDirectory() as td:
        корень = Path(td).resolve()
        cases = {
            "кадры-и-лимиты": case_кадры(h),
            "граница-доверия": case_граница(h, корень / "г"),
            "ручной-блок-и-повторы": case_ручной_блок(h, _mk(корень / "р")),
            "снимок-в-пакет": case_доказательство(h, _mk(корень / "д")),
            "проверить-фиксированный-argv": case_проверить(h),
            "живой-процесс": case_процесс(корень / "п"),
            "установка-хоста": case_установка(_mk(корень / "у")),
        }
    failed = 0
    for name, errors in cases.items():
        if errors:
            failed += 1
            print("FAIL %s" % name)
            for e in errors:
                print("  - %s" % e)
        else:
            print("PASS %s" % name)
    if failed:
        print("FAIL: %d/%d кейсов упало" % (failed, len(cases)))
        return 1
    print("PASS: все %d кейсов зелёные" % len(cases))
    return 0


def _mk(p):
    p.mkdir(parents=True, exist_ok=True)
    return p


if __name__ == "__main__":
    sys.exit(main())
