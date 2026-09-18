#!/usr/bin/env python3
"""
sanctions_check.py — офлайн-сверка компании/директора с перечнем
Росфинмониторинга (террористы и экстремисты). Только стандартная библиотека.

Перечень скачивается ОДИН раз (XML, несколько МБ) и кладётся в кэш
~/.cache/inn-check-ru/ — дальше сверка идёт локально за секунды, без сети.

Использование:
    python3 sanctions_check.py --refresh              # скачать/обновить кэш
    python3 sanctions_check.py --inn 7707083893       # точная сверка по ИНН
    python3 sanctions_check.py --name "Иванов Иван Иванович"

Сверка по ИНН — точная. Сверка по ФИО — точное совпадение нормализованной
строки (lower, ё→е, схлопывание пробелов), НЕ нечёткая: однофамильцы — частое
ложное срабатывание, поэтому любое ФИО-совпадение помечается
«требует ручной сверки по дате рождения».

Источник: https://www.fedsfm.ru/documents/terrorists-list-portal-simple
Если перечень недоступен (с не-РФ IP сайт РФМ часто не открывается) —
честное «не проверено» с инструкцией скачать вручную, скрипт не падает.

TLS-проверка включена всегда. Кэш старше 7 дней → предупреждение
«перечень устарел, обновите».
"""

import json
import os
import ssl
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET

LIST_URL = "https://www.fedsfm.ru/documents/terrorists-list-portal-simple"
CACHE_DIR = os.path.expanduser(os.path.join("~", ".cache", "inn-check-ru"))
CACHE_FILE = os.path.join(CACHE_DIR, "terrorists-list.xml")
CACHE_META = os.path.join(CACHE_DIR, "terrorists-list.meta.json")
CACHE_TTL_DAYS = 7
TIMEOUT = 60
UA = "inn-check-ru/1.1 (sanctions_check; offline cache refresh)"


def _out(obj, code=0):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")
    return code


def _не_проверено(причина):
    return {
        "статус": "не проверено",
        "причина": причина,
        "что_делать": ("скачайте перечень вручную с РФ-IP: %s "
                       "и положите в %s, затем повторите сверку"
                       % (LIST_URL, CACHE_FILE)),
        "источник": LIST_URL,
    }


def refresh():
    """Скачивает перечень РФМ в кэш. TLS включён. Не падает при сбое сети."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    ctx = ssl.create_default_context()
    req = urllib.request.Request(LIST_URL, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as resp:
            data = resp.read()
    except Exception as e:
        return _out(_не_проверено(
            "перечень не скачался (%s) — сайт Росфинмониторинга недоступен "
            "из этой сети (с не-РФ IP часто блокируется)" % type(e).__name__), 1)
    if len(data) < 1000 or b"<" not in data[:100]:
        return _out(_не_проверено(
            "источник вернул не-XML (%d байт) — структура ответа изменилась"
            % len(data)), 1)
    with open(CACHE_FILE, "wb") as fh:
        fh.write(data)
    meta = {"скачан": time.strftime("%Y-%m-%d %H:%M:%S"),
            "источник": LIST_URL, "байт": len(data)}
    with open(CACHE_META, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)
    return _out({"статус": "ок", "кэш": CACHE_FILE, **meta})


def _cache_age_days():
    try:
        return (time.time() - os.path.getmtime(CACHE_FILE)) / 86400.0
    except OSError:
        return None


def _normalize(text):
    return " ".join((text or "").lower().replace("ё", "е").split())


def _load_entries():
    """Парсит кэшированный XML в список записей {фио, инн[], дата_рождения}.

    Формат перечня РФМ исторически менялся, поэтому парсинг defensive:
    узлом записи считается элемент с потомком-ФИО или потомком-ИНН;
    теги ищем по окончанию имени без учёта регистра/namespace.
    """
    try:
        tree = ET.parse(CACHE_FILE)
    except Exception:
        # OSError/ParseError — битый кэш; ImportError — сломанный expat в среде.
        return None
    root = tree.getroot()
    entries = []

    def local(tag):
        return tag.rsplit("}", 1)[-1].lower()

    # Кандидаты-контейнеры записей: типичные имена узлов перечня.
    nodes = [el for el in root.iter()
             if local(el.tag) in ("terrorist", "subject", "record", "entity",
                                  "individual", "organization", "item")]
    if not nodes:  # неизвестная схема — берём прямых потомков корня
        nodes = list(root)
    for node in nodes:
        fio, birth, inns = None, None, []
        for el in node.iter():
            name = local(el.tag)
            text = (el.text or "").strip()
            if not text:
                continue
            if name in ("fio", "name", "fullname") and fio is None:
                fio = text
            elif "inn" in name or name == "инн":
                inns.append(text)
            elif name in ("birthdate", "birth", "dob", "datbirth") and birth is None:
                birth = text
        if fio or inns:
            entries.append({"фио": fio, "инн": inns, "дата_рождения": birth})
    return entries


def check(inn=None, name=None):
    if not os.path.exists(CACHE_FILE):
        return _out(_не_проверено(
            "кэш перечня отсутствует (%s) — сначала запустите --refresh "
            "с РФ-IP или скачайте вручную" % CACHE_FILE), 1)

    предупреждения = []
    age = _cache_age_days()
    if age is not None and age > CACHE_TTL_DAYS:
        предупреждения.append(
            "перечень устарел (%.0f дн., норма %d) — обновите: --refresh"
            % (age, CACHE_TTL_DAYS))

    entries = _load_entries()
    if entries is None:
        return _out(_не_проверено(
            "кэш повреждён или формат не читается — удалите %s и повторите "
            "--refresh" % CACHE_FILE), 1)

    найдено = []
    inn_q = (inn or "").strip()
    name_q = _normalize(name)
    for e in entries:
        if inn_q and inn_q in [i.strip() for i in e["инн"]]:
            найдено.append({**e, "совпадение": "по ИНН (точное)",
                            "вера": "высокая — ИНН уникален, но сверьте ФИО "
                                    "и дату рождения перед выводами"})
        elif name_q and e["фио"] and _normalize(e["фио"]) == name_q:
            найдено.append({**e, "совпадение": "по ФИО (точное, нормализованное)",
                            "вера": "требует ручной сверки по дате рождения — "
                                    "однофамильцы в перечне часты, это НЕ "
                                    "доказательство"})

    return _out({
        "статус": "проверено",
        "запрос": {"инн": inn or None, "фио": name or None},
        "совпадений": len(найдено),
        "совпадения": найдено,
        "записей_в_перечне": len(entries),
        "возраст_кэша_дней": round(age, 1) if age is not None else None,
        "предупреждения": предупреждения,
        "источник": LIST_URL,
        "дата_источника": time.strftime("%Y-%m-%d"),
    })


def main(argv):
    args = argv[1:]
    if not args or "--refresh" in args:
        if not args:
            sys.stderr.write(__doc__)
            return 2
        return refresh()
    inn = name = None
    for i, a in enumerate(args):
        if a == "--inn" and i + 1 < len(args):
            inn = args[i + 1]
        elif a == "--name" and i + 1 < len(args):
            name = args[i + 1]
    if inn is None and name is None:
        sys.stderr.write(
            "Укажите --inn <ИНН> и/или --name \"ФИО\" (или --refresh)\n")
        return 2
    return check(inn=inn, name=name)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
