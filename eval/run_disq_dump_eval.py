#!/usr/bin/env python3
"""
run_disq_dump_eval.py — реестр дисквалифицированных из выгрузки ФНС (disq_dump.py)
офлайн: сверка по ФИО руководителя (точная, ё/регистр), сроки действия, история
компании по ИНН организации, в индексе нет ФИО и дат рождения, CSV не остаётся
на диске; обновление всё или ничего (заголовок, дата, падение вдвое, чужой
домен, блокировка); блок движка и сигнал «дисквалификация_руководителя».
Фикстуры синтетические: ФИО — выдуманные «ФИКТИВНЫЙ …». PASS/FAIL, stdlib, CI.
"""

import gzip
import importlib.util
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
СЕГОДНЯ = "2026-09-28"
ЗАГОЛОВОК = ",".join("G%d" % i for i in range(1, 15))
URL_ДАННЫХ = ("https://data.nalog.ru/opendata/7707329152-registerdisqualified/"
              "data-20260913-structure-20150624.csv")


def load(имя):
    sys.path.insert(0, str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(имя, ROOT / "scripts" / ("%s.py" % имя))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check(errors, cond, msg):
    if not cond:
        errors.append(msg)


def строка(номер, фио, инн_орг, с, по, орг='ООО "УСЛОВНАЯ КОМПАНИЯ"'):
    return ",".join([номер, фио, "01.01.1970", "Г. УСЛОВНЫЙ", '"%s"' % орг.replace('"', '""'),
                     инн_орг, "ГЕНЕРАЛЬНЫЙ ДИРЕКТОР", "Ч.5 СТ. 14.25 КОАП РФ",
                     "МИФНС № 0 ПО УСЛОВНОЙ ОБЛАСТИ", "УСЛОВНЫЙ С С", "МИРОВОЙ СУДЬЯ",
                     "1 г 0 м 0 д", с, по])


ЗАПИСИ = [
    строка("100000000001", "ФИКТИВНЫЙ РУКОВОДИТЕЛЬ ОДИН", "7707083893", "01.01.2026", "01.01.2027"),
    # истёк
    строка("100000000002", "ФИКТИВНЫЙ РУКОВОДИТЕЛЬ ДВА", "7707083893", "01.01.2024", "01.01.2025"),
    # начнётся в будущем
    строка("100000000003", "ФИКТИВНЫЙ РУКОВОДИТЕЛЬ ТРИ", "", "01.01.2027", "01.01.2028"),
    # тот же человек, другая компания без ИНН
    строка("100000000004", "ФИКТИВНЫЙ РУКОВОДИТЕЛЬ ОДИН", "", "01.02.2026", "01.02.2027"),
    # ё в ФИО
    строка("100000000005", "ФИКТИВНЫЙ ЁЖИКОВ ПЯТЬ", "7736050003", "01.01.2026", "01.01.2030"),
]


def выгрузка(записи=None, заголовок=ЗАГОЛОВОК):
    return ("﻿" + "\r\n".join([заголовок] + (ЗАПИСИ if записи is None else записи))
            + "\r\n").encode("utf-8")


def meta(valid="20261004", url=URL_ДАННЫХ):
    return ("property,value\nidentifier,7707329152-registerdisqualified\nvalid,%s\n"
            "data-20260906-structure-20150624.csv,https://data.nalog.ru/opendata/"
            "7707329152-registerdisqualified/data-20260906-structure-20150624.csv\n"
            "data-20260913-structure-20150624.csv,%s\n" % (valid, url)).encode("utf-8")


def получатель(данные=None, valid="20261004", url=URL_ДАННЫХ, сломать=False):
    def получить(адрес, лимит):
        if сломать:
            raise OSError("сеть")
        if адрес.endswith("/meta.csv"):
            return meta(valid, url)
        if "20260906" in адрес:
            raise AssertionError("взята не последняя выгрузка")
        return выгрузка() if данные is None else данные
    return получить


def case_индекс(dd):
    errors = []
    with tempfile.TemporaryDirectory() as td:
        res = dd.refresh(cache_dir=td, получить=получатель())
        check(errors, res["статус"] == "обновлено" and res["записей"] == 5, "refresh: %r" % res)
        # на диске — только индекс и ключ; ключ 600; ФИО и даты рождения в индексе нет
        check(errors, sorted(os.listdir(td)) == sorted([dd.ИНДЕКС, dd.КЛЮЧ]),
              "в кэше лишнее: %r" % os.listdir(td))
        check(errors, stat.S_IMODE(os.stat(os.path.join(td, dd.КЛЮЧ)).st_mode) == 0o600,
              "права ключа")
        with gzip.open(os.path.join(td, dd.ИНДЕКС)) as fh:
            сырой = fh.read().decode("utf-8")
        for лишнее in ("ФИКТИВНЫЙ", "01.01.1970", "1970-01-01", "УСЛОВНЫЙ С С", "Г. УСЛОВНЫЙ"):
            check(errors, лишнее not in сырой, "в индексе %r" % лишнее)

        def сверка(инн, фио):
            return dd.lookup(инн, фио, cache_dir=td, сегодня=СЕГОДНЯ)[0]
        r = сверка("7707083893", "Фиктивный  Руководитель Один")
        check(errors, r["дисквалификация_руководителя"] is True
              and [z["номер_записи"] for z in r["совпадения_по_фио"]]
              == ["100000000001", "100000000004"]
              and [z["та_же_компания"] for z in r["совпадения_по_фио"]] == [True, False],
              "действующая, регистр и пробелы: %r" % r["совпадения_по_фио"])
        check(errors, [(z["номер_записи"], z["фио_как_у_руководителя"])
                       for z in r["история_компании"]]
              == [("100000000001", True), ("100000000002", False)],
              "история компании: %r" % r["история_компании"])
        r = сверка("7707083893", "ФИКТИВНЫЙ РУКОВОДИТЕЛЬ ДВА")
        check(errors, r["дисквалификация_руководителя"] is False
              and r["совпадения_по_фио"][0]["действует"] is False, "истёкшая: %r" % r)
        r = сверка("7700000000", "ФИКТИВНЫЙ РУКОВОДИТЕЛЬ ТРИ")
        check(errors, r["дисквалификация_руководителя"] is False, "будущая: %r" % r)
        r = сверка("7736050003", "Фиктивный ёжиков Пять")
        check(errors, r["дисквалификация_руководителя"] is True, "ё/е: %r" % r)
        r = сверка("7707083893", "ФИКТИВНЫЙ РУКОВОДИТЕЛЬ")
        check(errors, r["дисквалификация_руководителя"] is False, "подстрока ФИО засчитана")
        r = сверка("7707083893", None)
        check(errors, r["дисквалификация_руководителя"] is None
              and len(r["история_компании"]) == 2
              and r["история_компании"][0]["фио_как_у_руководителя"] is None,
              "без ФИО: %r" % r)
        # последний день срока — ещё действует, следующий — нет
        check(errors, dd.lookup("7707083893", "ФИКТИВНЫЙ РУКОВОДИТЕЛЬ ДВА", cache_dir=td,
                                сегодня="2025-01-01")[0]["дисквалификация_руководителя"]
              is True, "последний день срока")
        check(errors, dd.lookup("7707083893", "ФИКТИВНЫЙ РУКОВОДИТЕЛЬ ОДИН", cache_dir=td,
                                сегодня="2027-02-02")[0]["дисквалификация_руководителя"]
              is False, "после срока")
    with tempfile.TemporaryDirectory() as td:
        r, note = dd.lookup("7707083893", cache_dir=td)
        check(errors, r is None and "--refresh" in note, "нет индекса: %r" % note)
    return "индекс", errors


def case_обновление(dd):
    errors = []
    with tempfile.TemporaryDirectory() as td:
        dd.refresh(cache_dir=td, получить=получатель())
        путь = os.path.join(td, dd.ИНДЕКС)
        было = Path(путь).read_bytes()
        плохие = {
            "сеть": получатель(сломать=True),
            "заголовок": получатель(выгрузка(заголовок="A,B,C")),
            "колонки": получатель(выгрузка(ЗАПИСИ + ["1,2,3"])),
            "лишняя колонка": получатель(выгрузка(ЗАПИСИ + [ЗАПИСИ[0] + ",лишнее"])),
            "дата": получатель(выгрузка([ЗАПИСИ[0].replace("01.01.2027", "2027-01-01")])),
            "падение вдвое": получатель(выгрузка(ЗАПИСИ[:2])),
            "пусто": получатель(выгрузка([])),
            "чужой домен": получатель(url="https://evil.example/data.csv"),
        }
        for что, п in плохие.items():
            if что == "чужой домен":
                continue
            res = dd.refresh(cache_dir=td, получить=п)
            check(errors, res["статус"].startswith("индекс не обновлён")
                  and Path(путь).read_bytes() == было, "%s: %r" % (что, res["статус"]))
        # ключ переживает неудачное обновление: иначе старые хеши не сверились бы
        # и сверка тихо отвечала бы «нет»
        check(errors, dd.lookup("7707083893", "ФИКТИВНЫЙ РУКОВОДИТЕЛЬ ОДИН", cache_dir=td,
                                сегодня=СЕГОДНЯ)[0]["дисквалификация_руководителя"] is True,
              "после неудачных обновлений сверка сломана")
        # чужой домен — настоящий _get отказывает до запроса
        try:
            dd._get("https://evil.example/data.csv", 1)
            errors.append("_get скачал чужой домен")
        except ValueError:
            pass
        res = dd.refresh(cache_dir=td, получить=lambda адрес, лимит: dd._get(адрес, лимит)
                         if not адрес.endswith("meta.csv")
                         else meta(url="https://evil.example/data.csv"))
        check(errors, "вне набора" in res["статус"] and Path(путь).read_bytes() == было,
              "чужой домен в meta: %r" % res["статус"])
        замок = os.path.join(td, ".refresh.lock")
        Path(замок).write_text("")
        res = dd.refresh(cache_dir=td, получить=получатель())
        check(errors, "уже идёт" in res["статус"], "блокировка: %r" % res)
        os.unlink(замок)
        check(errors, not [f for f in os.listdir(td) if f.endswith(".tmp")],
              "временные файлы: %r" % os.listdir(td))
    # первая выгрузка пустая (прежнего индекса нет) — индекс не создаётся
    with tempfile.TemporaryDirectory() as td:
        res = dd.refresh(cache_dir=td, получить=получатель(выгрузка([])))
        check(errors, res["статус"].startswith("индекс не обновлён")
              and not os.path.exists(os.path.join(td, dd.ИНДЕКС)), "пустая первая: %r" % res)
    # valid не распознан — свежесть от даты выгрузки (13.09)
    with tempfile.TemporaryDirectory() as td:
        dd.refresh(cache_dir=td, получить=получатель(valid="нет"))
        и = dd._прочитать(os.path.join(td, dd.ИНДЕКС))
        check(errors, not dd.устарел(и, "2026-09-27") and dd.устарел(и, "2026-09-28"),
              "свежесть без valid")
    return "обновление", errors


def case_движок():
    errors = []
    дом = os.environ.get("HOME")
    with tempfile.TemporaryDirectory() as td:
        os.environ["HOME"] = td
        try:
            fc = load("fetch_counterparty")
            profiles = load("profiles")
            контекст = {"егрюл": {"руководитель": "ГЕНЕРАЛЬНЫЙ ДИРЕКТОР: Фиктивный "
                                                  "Руководитель Один"}}
            данные, av = fc.fetch_disq_dump(None, "7707083893", контекст)
            check(errors, данные is None and av["состояние"] == "не проверено"
                  and av["причина"].startswith("кэш:"), "нет индекса: %r" % av)
            dd = load("disq_dump")
            dd.refresh(cache_dir=dd.CACHE_DIR, получить=получатель(valid="20991231"))
            данные, av = fc.fetch_disq_dump(None, "7707083893", контекст)
            check(errors, av["состояние"] == "ok" and данные["дисквалификация_руководителя"]
                  is True and данные["руководитель"] == "ФИО руководителя из ЕГРЮЛ",
                  "найден: %r %r" % (av, данные and данные.get("руководитель")))
            # сигнал каталога: 🔴 дисквалификация_руководителя из этого блока
            fetch = {"инн": "7707083893", "дисквалифицированные": данные,
                     "_доступность": {"дисквалифицированные": av}}
            сиг = {s["id"]: s for s in profiles.extract_signals(fetch)}
            д = сиг["дисквалификация_руководителя"]
            check(errors, д["статус"] == "найден" and д["источник_id"] == "дисквалифицированные",
                  "сигнал: %r" % д)
            # управляющая организация вместо ФИО — признак null, история есть
            данные, av = fc.fetch_disq_dump(None, "7707083893", {"егрюл": {
                "руководитель": 'Управляющая организация: АО "УСЛОВНАЯ"'}})
            check(errors, av["состояние"] == "ok" and данные["дисквалификация_руководителя"]
                  is None and данные["руководитель"].startswith("не сверялся")
                  and len(данные["история_компании"]) == 2, "УК: %r" % данные)
            # устаревший индекс: «нет» — не проверено, «да» — остаётся
            dd.refresh(cache_dir=dd.CACHE_DIR, получить=получатель(valid="20200101"))
            данные, av = fc.fetch_disq_dump(None, "7700000000", {"егрюл": {
                "руководитель": "ДИРЕКТОР: Фиктивный Кто То"}})
            check(errors, данные is None and av["состояние"] == "не проверено"
                  and "устарела" in av["причина"], "устаревший, нет: %r" % av)
            данные, av = fc.fetch_disq_dump(None, "7707083893", контекст)
            check(errors, av["состояние"] == "ok" and данные["предупреждения"],
                  "устаревший, есть: %r" % av)
            # снимок: дата выгрузки вне хеша, признак — в отслеживаемых
            snap = load("snapshot")
            а = {"инн": "7707083893", "дисквалифицированные": данные,
                 "_доступность": {"дисквалифицированные": av}}
            б = json.loads(json.dumps(а, ensure_ascii=False))
            б["дисквалифицированные"]["дата_выгрузки"] = "2026-09-20"
            б["дисквалифицированные"]["предупреждения"] = []
            check(errors, snap.хеш_остального(а) == snap.хеш_остального(б),
                  "дата выгрузки в хеше снимка")
            check(errors, "дисквалифицированные" in fc.FETCHERS
                  and "дисквалифицированные" in fc.CONTEXT_FETCHERS
                  and fc.SOURCES["дисквалифицированные"]["deal_killer"] is False,
                  "источник не зарегистрирован")
        finally:
            if дом is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = дом
    return "блок-движка", errors


def main():
    dd = load("disq_dump")
    failed = 0
    for имя, errors in (case_индекс(dd), case_обновление(dd), case_движок()):
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
