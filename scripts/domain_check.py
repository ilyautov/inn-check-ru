#!/usr/bin/env python3
"""
domain_check.py — домен контрагента по WHOIS реестра .ru/.su/.рф. Только stdlib.

    python3 scripts/domain_check.py example.ru
    python3 scripts/domain_check.py пример.рф --дата-регистрации 2018-03-12

Что видно (whois.tcinet.ru, порт 43, живьём 27.09.2026): зарегистрирован ли
домен, кто регистрант по типу — организация (строка «org:») или частное лицо
(«person: Private Person»), дата регистрации, срок оплаты, регистратор, состояние
(делегирован, верифицирован).

Чего не видно: ИНН и название организации — поля «org» и «taxpayer-id» реестр
отдаёт пустыми у всех проверенных доменов. Поэтому принадлежность домена
компании WHOIS не подтверждает и не опровергает: скрипт этого и не утверждает.
Факты для человека: домен на частное лицо у юрлица, домен моложе компании на
годы, срок оплаты истекает.

Имя частного лица реестр маскирует («Private Person»); скрипт его не выводит
вовсе — только тип регистранта. Один запрос, без повторов.
"""

import argparse
import datetime
import json
import re
import socket
import sys

WHOIS = ("whois.tcinet.ru", 43)
ЗОНЫ = ("ru", "su", "xn--p1ai")          # .рф — xn--p1ai
TIMEOUT = 15
МЕТКА = re.compile(r"^(?=.{1,63}$)[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")


def нормализовать(ввод):
    """«https://www.Пример.рф/путь» -> «xn--e1afmkfd.xn--p1ai». ValueError — не домен."""
    v = str(ввод or "").strip().lower()
    v = re.sub(r"^[a-z]+://", "", v).split("/", 1)[0].split("?", 1)[0].rstrip(".")
    if v.startswith("www."):
        v = v[4:]
    try:
        v = v.encode("idna").decode("ascii")
    except UnicodeError as e:
        raise ValueError("не домен: %s" % e)
    метки = v.split(".")
    if len(метки) < 2 or not all(МЕТКА.match(m) for m in метки):
        raise ValueError("не домен: %r" % ввод)
    return v


def разобрать(текст):
    """Ответ WHOIS -> {ключ: [значения]}; строки-комментарии «%» пропускаются."""
    поля = {}
    for строка in текст.splitlines():
        if строка.startswith("%") or ":" not in строка:
            continue
        ключ, _, значение = строка.partition(":")
        поля.setdefault(ключ.strip().lower(), []).append(значение.strip())
    return поля


def _дата(v):
    try:
        return datetime.date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def оценить(домен, текст, сегодня=None, дата_регистрации=None):
    """Разобранный WHOIS -> (данные | None, состояние, причина)."""
    сегодня = сегодня or datetime.date.today()
    if re.search(r"(?im)^no entries found", текст):
        return None, "пусто", "домен %s в реестре не зарегистрирован" % домен
    поля = разобрать(текст)
    имя = (поля.get("domain") or [""])[0].lower()
    if имя != домен:
        return None, "не проверено", "схема: в ответе WHOIS нет строки domain: %s" % домен
    создан = _дата((поля.get("created") or [""])[0])
    if создан is None:
        return None, "не проверено", "схема: нет даты created"
    состояние = [s.strip() for s in ",".join(поля.get("state") or []).split(",") if s.strip()]
    if "org" in поля:
        регистрант = "организация"
    elif "person" in поля:
        регистрант = "частное лицо"
    else:
        регистрант = None
    оплачен = _дата((поля.get("paid-till") or [""])[0])
    данные = {
        "домен": домен,
        "регистрант": регистрант,
        "создан": создан.isoformat(),
        "возраст_лет": round((сегодня - создан).days / 365.25, 1),
        "оплачен_до": оплачен.isoformat() if оплачен else None,
        "регистратор": (поля.get("registrar") or [None])[0],
        "состояние": состояние,
        # Живьём видели REGISTERED, DELEGATED, NOT DELEGATED, VERIFIED. Нет строки
        # state или незнакомые метки — не знаем (None), а не «нет»: отсутствие
        # положительной метки отрицания не доказывает.
        "делегирован": (True if "DELEGATED" in состояние
                        else False if "NOT DELEGATED" in состояние else None),
        "верифицирован": True if "VERIFIED" in состояние else None,
        "источник": "WHOIS %s (реестр .ru/.su/.рф)" % WHOIS[0],
        "оговорка": "ИНН и название организации реестр не раскрывает: принадлежность "
                    "домена компании этим не подтверждается и не опровергается",
    }
    if оплачен and оплачен < сегодня:
        данные["примечание"] = "срок оплаты домена (%s) уже прошёл" % оплачен.isoformat()
    elif оплачен and (оплачен - сегодня).days < 30:
        данные["примечание"] = "оплата домена истекает меньше чем через 30 дней"
    рег = _дата(дата_регистрации) if дата_регистрации else None
    if рег:
        данные["домен_моложе_компании_лет"] = round((создан - рег).days / 365.25, 1)
    return данные, "ok", None


def запросить(домен):
    """Один запрос к WHOIS -> текст. Сбой — OSError наружу."""
    with socket.create_connection(WHOIS, timeout=TIMEOUT) as s:
        s.sendall((домен + "\r\n").encode("ascii"))
        куски = []
        while True:
            кусок = s.recv(65536)
            if not кусок:
                break
            куски.append(кусок)
    return b"".join(куски).decode("utf-8", "replace")


def проверить(ввод, дата_регистрации=None):
    try:
        домен = нормализовать(ввод)
    except ValueError as e:
        return {"домен": str(ввод), "состояние": "не проверено", "причина": "ввод: %s" % e}
    if домен.rsplit(".", 1)[1] not in ЗОНЫ:
        return {"домен": домен, "состояние": "не проверено",
                "причина": "не покрыто: зона .%s — скрипт знает только реестр .ru/.su/.рф"
                           % домен.rsplit(".", 1)[1]}
    try:
        текст = запросить(домен)
    except OSError as e:
        return {"домен": домен, "состояние": "не проверено",
                "причина": "сеть: WHOIS %s — %s" % (WHOIS[0], type(e).__name__)}
    данные, состояние, причина = оценить(домен, текст, дата_регистрации=дата_регистрации)
    return {"домен": домен, "состояние": состояние, "причина": причина, "данные": данные}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("домен")
    ap.add_argument("--дата-регистрации", help="дата регистрации компании ГГГГ-ММ-ДД из ЕГРЮЛ")
    a = ap.parse_args(argv)
    рез = проверить(a.домен, a.дата_регистрации)
    print(json.dumps(рез, ensure_ascii=False, indent=2))
    return 0 if рез["состояние"] in ("ok", "пусто") else 2


if __name__ == "__main__":
    sys.exit(main())
