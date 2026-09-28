#!/usr/bin/env python3
"""
run_rospatent_tz_eval.py — товарные знаки по ИНН (rospatent_tz.py) офлайн на
синтетическом CSV той же формы, что живой 02.09.2026 (UTF-8 с BOM, «,», заголовки
с пробелами; паспорт — cp1251 через «;»): индекс по ИНН без имён, «действует» —
actual и срок, схема и обрыв потока не затирают индекс, лимит размера, чужой
домен, блок движка. PASS/FAIL, stdlib, CI.
"""

import contextlib
import gzip
import importlib.util
import io
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
СЕГОДНЯ = "2026-09-28"
ИМЯ = "ФИО УБРАНО ИЗ ФИКСТУРЫ"
ШАПКА = ["registration number", "registration date", "application number",
         "expiration date", "right holder name", "right holder address",
         "right holder country code", "right holder ogrn", "right holder inn", "actual",
         "publication URL"]
URL = "https://rospatent.gov.ru/opendata/7730176088-tz/data-20260902-structure-20180828.csv"


def load(имя):
    sys.path.insert(0, str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(имя, ROOT / "scripts" / ("%s.py" % имя))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check(errors, cond, msg):
    if not cond:
        errors.append(msg)


def строка(номер, инн, дата="20200101", срок="20300101", actual="true", страна="RU",
           имя='ООО "УСЛОВНАЯ"'):
    return [номер, дата, "2019" + номер, срок, имя, "г. Условный, ул. Условная, 1", страна,
            "", инн, actual, "http://www1.fips.ru/x"]


СТРОКИ = [
    строка("80", ""),                                        # давняя, без ИНН
    строка("100001", "7707083893", дата="20190514"),
    строка("100002", "7707083893", дата="20210101", срок="20250101"),   # срок истёк
    строка("100003", "7707083893", дата="20220101", actual="false"),    # охрана прекращена
    строка("100004", "500100000000", имя=ИМЯ),              # ИП: имя не храним
    строка("100005", "US123", страна="US"),                  # не ИНН РФ
    строка("100006", "7736050003", дата=""),                 # без даты регистрации
]


def csv_байты(строки=None, шапка=None):
    буф = io.StringIO()
    import csv
    w = csv.writer(буф, lineterminator="\r\n")
    w.writerow(шапка or ШАПКА)
    for r in (СТРОКИ if строки is None else строки):
        w.writerow(r)
    return ("﻿" + буф.getvalue()).encode("utf-8")


def meta(дата="20260902", url=URL, кодировка="cp1251", раздел=";"):
    строки = ["property%svalue" % раздел, "identifier%s7730176088-tz" % раздел,
              'title%s"Открытый реестр товарных знаков"' % раздел,
              "data-20250101-structure-20180828%s%s" % (раздел, URL.replace("20260902",
                                                                              "20250101")),
              "data-%s-structure-20180828%s%s" % (дата, раздел, url)]
    return "\n".join(строки).encode(кодировка)


def открыватель(данные=None, мета=None, обрыв=False):
    def открыть(url, лимит):
        if url.endswith("/meta.csv"):
            return io.BufferedReader(io.BytesIO(мета or meta()))
        if "20250101" in url:
            raise AssertionError("взята не последняя выгрузка")
        сырые = csv_байты() if данные is None else данные
        if обрыв:
            class Обрыв(io.RawIOBase):
                def __init__(self):
                    self.б = io.BytesIO(сырые[:len(сырые) // 2])

                def readable(self):
                    return True

                def readinto(self, b):
                    n = self.б.readinto(b)
                    if n == 0:
                        raise OSError("соединение разорвано")
                    return n
            return io.BufferedReader(Обрыв())
        return io.BufferedReader(io.BytesIO(сырые))
    return открыть


def case_индекс(tz):
    errors = []
    with tempfile.TemporaryDirectory() as td:
        res = tz.refresh(cache_dir=td, открыть=открыватель())
        check(errors, res["статус"] == "обновлено" and res["записей"] == 7
              and res["с_инн"] == 5 and res["правообладателей"] == 3, "refresh: %r" % res)
        with gzip.open(os.path.join(td, tz.ИНДЕКС)) as fh:
            сырой = fh.read().decode("utf-8")
        for лишнее in (ИМЯ, "УСЛОВНАЯ", "Условный", "US123", "﻿"):
            check(errors, лишнее not in сырой, "в индексе %r" % лишнее)
        r = tz.lookup("7707083893", cache_dir=td, сегодня=СЕГОДНЯ)[0]
        check(errors, r["знаков"] == 3 and r["действующих"] == 1
              and [з["номер"] for з in r["последние"]] == ["100003", "100002", "100001"]
              and [з["действует"] for з in r["последние"]] == [False, False, True]
              and r["последние"][2]["дата_регистрации"] == "2019-05-14",
              "знаки: %r" % r["последние"])
        r = tz.lookup("500100000000", cache_dir=td, сегодня=СЕГОДНЯ)[0]
        check(errors, r["знаков"] == 1 and r["действующих"] == 1, "ИП: %r" % r)
        r = tz.lookup("7736050003", cache_dir=td, сегодня=СЕГОДНЯ)[0]
        check(errors, r["последние"][0]["дата_регистрации"] is None, "без даты: %r" % r)
        check(errors, tz.lookup("7700000000", cache_dir=td, сегодня=СЕГОДНЯ)[0]["в_реестре"]
              is False, "нет записи")
        # последний день срока — действует
        check(errors, tz.lookup("7707083893", cache_dir=td, сегодня="2030-01-01")[0]
              ["действующих"] == 1 and tz.lookup("7707083893", cache_dir=td,
                                                 сегодня="2030-01-02")[0]["действующих"] == 0,
              "граница срока")
        # свежесть 45 дней от выгрузки 02.09
        check(errors, not tz.lookup("7707083893", cache_dir=td, сегодня="2026-10-17")[0]
              ["предупреждения"] and tz.lookup("7707083893", cache_dir=td,
                                               сегодня="2026-10-18")[0]["предупреждения"],
              "свежесть")
        # та же выгрузка повторно — не качается
        res = tz.refresh(cache_dir=td, открыть=открыватель(данные=b"not csv"))
        check(errors, "уже в индексе" in res["статус"], "повтор выгрузки: %r" % res)
        # показ ограничен, счёт — по всем
        много = [строка(str(200000 + i), "7702070139", дата="2020%02d01" % (i % 12 + 1))
                 for i in range(tz.ПОКАЗАТЬ + 5)]
    with tempfile.TemporaryDirectory() as td:
        tz.refresh(cache_dir=td, открыть=открыватель(csv_байты(СТРОКИ + много)))
        r = tz.lookup("7702070139", cache_dir=td, сегодня=СЕГОДНЯ)[0]
        check(errors, r["знаков"] == tz.ПОКАЗАТЬ + 5 and len(r["последние"]) == tz.ПОКАЗАТЬ,
              "ограничение показа: %r" % r["знаков"])
    # паспорт в UTF-8 через «,» — тоже читается
    url, дата = tz._мета(открыватель(мета=meta(кодировка="utf-8", раздел=",")))
    check(errors, url == URL and дата == "2026-09-02", "meta UTF-8: %r" % url)
    with tempfile.TemporaryDirectory() as td:
        r, note = tz.lookup("7707083893", cache_dir=td)
        check(errors, r is None and "--refresh" in note, "нет индекса")
    return "индекс", errors


def case_схема(tz):
    errors = []
    with tempfile.TemporaryDirectory() as td:
        tz.refresh(cache_dir=td, открыть=открыватель(мета=meta(дата="20260801")))
        путь = os.path.join(td, tz.ИНДЕКС)
        было = Path(путь).read_bytes()
        плохие = {
            "нет колонки": открыватель(csv_байты(шапка=[h for h in ШАПКА
                                                       if h != "right holder inn"])),
            # плохая строка поверх полного набора: ловит именно проверка схемы
            "actual": открыватель(csv_байты(СТРОКИ + [строка("1", "7707083893",
                                                             actual="да")])),
            "дата": открыватель(csv_байты(СТРОКИ + [строка("1", "7707083893",
                                                           дата="2020-01-01")])),
            "колонки": открыватель(csv_байты(СТРОКИ + [["1", "2"]])),
            "лишняя колонка": открыватель(csv_байты(СТРОКИ + [строка("1", "7707083893")
                                                              + ["лишнее"]])),
            "падение вдвое": открыватель(csv_байты(СТРОКИ[1:3])),
            "обрыв потока": открыватель(обрыв=True),
        }
        for что, открыть in плохие.items():
            res = tz.refresh(cache_dir=td, открыть=открыть)
            check(errors, res["статус"].startswith("индекс не обновлён")
                  and Path(путь).read_bytes() == было, "%s: %r" % (что, res["статус"]))
        замок = os.path.join(td, ".refresh.lock")
        Path(замок).write_text("")
        check(errors, "уже идёт" in tz.refresh(cache_dir=td, открыть=открыватель())["статус"],
              "блокировка")
        os.unlink(замок)
        check(errors, not [f for f in os.listdir(td) if f.endswith(".tmp")], "временные файлы")
    # ни одной записи с ИНН, прежнего индекса нет — индекс не создаётся
    with tempfile.TemporaryDirectory() as td:
        res = tz.refresh(cache_dir=td, открыть=открыватель(csv_байты([строка(str(i), "")
                                                                      for i in range(9)])))
        check(errors, res["статус"].startswith("индекс не обновлён")
              and not os.path.exists(os.path.join(td, tz.ИНДЕКС)), "нет ИНН: %r" % res)
    try:
        tz._открыть("https://evil.example/data.csv", 1)
        errors.append("чужой домен открыт")
    except ValueError:
        pass
    # лимит размера срабатывает посреди чтения
    поток = io.BufferedReader(tz._Счётчик(io.BytesIO(b"x" * 5000), 1000), 256)
    try:
        поток.read()
        errors.append("лимит размера не сработал")
    except ValueError:
        pass
    return "схема", errors


def case_движок():
    errors = []
    дом = os.environ.get("HOME")
    with tempfile.TemporaryDirectory() as td:
        os.environ["HOME"] = td
        try:
            fc = load("fetch_counterparty")
            данные, av = fc.fetch_trademarks(None, "7707083893")
            check(errors, данные is None and av["причина"].startswith("кэш:"), "нет индекса")
            tz = load("rospatent_tz")
            with contextlib.redirect_stdout(io.StringIO()):
                tz.refresh(cache_dir=tz.CACHE_DIR, открыть=открыватель(
                    мета=meta(дата=СЕГОДНЯ.replace("-", ""))))
            данные, av = fc.fetch_trademarks(None, "7707083893")
            check(errors, av["состояние"] == "ok" and данные["знаков"] == 3, "ok: %r" % av)
            пусто, av2 = fc.fetch_trademarks(None, "7700000000")
            check(errors, пусто is None and av2["состояние"] == "пусто"
                  and "не у всех знаков" in av2["причина"], "пусто: %r" % av2)
            snap = load("snapshot")
            а = {"инн": "7707083893", "товарные_знаки": данные,
                 "_доступность": {"товарные_знаки": av}}
            б = json.loads(json.dumps(а, ensure_ascii=False))
            б["товарные_знаки"]["дата_выгрузки"] = "2026-10-02"
            б["товарные_знаки"]["предупреждения"] = ["x"]
            check(errors, snap.хеш_остального(а) == snap.хеш_остального(б),
                  "дата выгрузки в хеше снимка")
            б["товарные_знаки"]["действующих"] = 0
            check(errors, snap.хеш_остального(а) != snap.хеш_остального(б),
                  "изменение знаков не видно в снимке")
            # устаревший индекс: «нет» — не проверено
            with contextlib.redirect_stdout(io.StringIO()):
                os.unlink(os.path.join(tz.CACHE_DIR, tz.ИНДЕКС))
                tz.refresh(cache_dir=tz.CACHE_DIR, открыть=открыватель(
                    мета=meta(дата="20260101")))
            _, av = fc.fetch_trademarks(None, "7700000000")
            check(errors, av["состояние"] == "не проверено" and "старше" in av["причина"],
                  "устаревший: %r" % av)
            check(errors, "товарные_знаки" in fc.FETCHERS
                  and fc.SOURCES["товарные_знаки"]["deal_killer"] is False, "регистрация")
            # промежуточный сертификат Роспатента лежит в репозитории
            check(errors, tz._промежуточный() and Path(tz._промежуточный()).read_text()
                  .startswith("-----BEGIN CERTIFICATE-----"), "нет промежуточного сертификата")
        finally:
            if дом is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = дом
    return "блок-движка", errors


def main():
    tz = load("rospatent_tz")
    failed = 0
    for имя, errors in (case_индекс(tz), case_схема(tz), case_движок()):
        if errors:
            failed += 1
            print("FAIL %s" % имя)
            for e in errors:
                print("  -", e)
        else:
            print("PASS %s" % имя)
    if failed:
        print("FAIL: %d/3 кейсов упало" % failed)
        return 1
    print("PASS: все 3 кейсов зелёные")
    return 0


if __name__ == "__main__":
    sys.exit(main())
