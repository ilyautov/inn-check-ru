#!/usr/bin/env python3
"""
registries_refresh.py — офлайн-сверка по бесплатным дампам: ЕРКНМ (плановые
проверки, 248-ФЗ) и РНП (реестр недобросовестных поставщиков). Только stdlib.

Паттерн sanctions_check.py: дампы скачиваются один раз в кэш
~/.cache/inn-check-ru/, сверка идёт локально. Каждый реестр обновляется
и деградирует независимо.

Использование:
    python3 registries_refresh.py --refresh        # скачать/обновить дампы
    python3 registries_refresh.py --inn 7707083893 # сверка по кэшам

СТАТУС ИСТОЧНИКОВ (проверено 19.09.2026 с не-РФ IP):
    ЕРКНМ  proverki.gov.ru/portal/public-open-data — страница дампов отдаёт
           400 (public-search заблокирован); скачивание дампов — с РФ-IP
           вручную: https://proverki.gov.ru/portal/public-open-data
           (ежедневные data-YYYYMMDD-*.zip), распаковать в кэш ниже.
    РНП    zakupki.gov.ru/epz/opendata — таймаут с не-РФ IP; дампы
           DishonestSupplier — с РФ-IP: https://zakupki.gov.ru/epz/opendata

Формат дампов заранее не известен точно -> сверка схемно-агностичная:
ИНН ищется в тексте дампа, в ответ уходит сырой контекст записи (±400
символов) — честнее, чем выдуманная структура. Кэш старше 30 дней ->
предупреждение «дамп устарел».
"""

import json
import os
import re
import ssl
import sys
import time
import urllib.request

CACHE_DIR = os.path.expanduser(os.path.join("~", ".cache", "inn-check-ru"))
CACHE_TTL_DAYS = 30
TIMEOUT = 120
UA = "inn-check-ru/1.4 (registries_refresh)"

REGISTRIES = {
    "еркнм": {
        "название": "ЕРКНМ — плановые контрольные (надзорные) мероприятия (248-ФЗ)",
        "dir": os.path.join(CACHE_DIR, "erknm"),
        "portal": "https://proverki.gov.ru/portal/public-open-data",
        # кандидат прямого URL дампа — НЕ верифицирован (400 с не-РФ IP)
        "urls": ["https://proverki.gov.ru/portal/public-open-data/"
                 "data-%s-str.zip" % time.strftime("%Y%m%d")],
    },
    "рнп": {
        "название": "РНП — реестр недобросовестных поставщиков (44-ФЗ/223-ФЗ)",
        "dir": os.path.join(CACHE_DIR, "rnp"),
        "portal": "https://zakupki.gov.ru/epz/opendata",
        # кандидат прямого URL дампа — НЕ верифицирован (таймаут с не-РФ IP)
        "urls": [("https://zakupki.gov.ru/epz/opendata/dishonestsupplier/"
                  "export.xml.zip")],
    },
}

INN_RE_FLAGS = re.DOTALL


def _out(obj, code=0):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")
    return code


def _cache_age_days(reg, cache_dir=None):
    d = cache_dir or REGISTRIES[reg]["dir"]
    try:
        files = [os.path.join(d, f) for f in os.listdir(d)]
        if not files:
            return None
        return (time.time() - max(os.path.getmtime(f) for f in files)) / 86400.0
    except OSError:
        return None


def _download(reg):
    """Пытается скачать дамп реестра. Источники гео-блокируются с не-РФ IP —
    при неудаче честная инструкция для ручного скачивания."""
    info = REGISTRIES[reg]
    os.makedirs(info["dir"], exist_ok=True)
    ctx = ssl.create_default_context()
    errors = []
    for url in info["urls"]:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as resp:
                data = resp.read()
            if len(data) < 100:
                errors.append("%s: %d байт" % (url, len(data)))
                continue
            name = os.path.basename(url.split("?")[0]) or "dump.bin"
            path = os.path.join(info["dir"], name)
            with open(path, "wb") as fh:
                fh.write(data)
            return {"реестр": info["название"], "статус": "ок",
                    "файл": path, "байт": len(data),
                    "примечание": "zip-дамп распакуйте в тот же каталог, "
                                  "если сверка не находит записи"}
        except Exception as e:
            errors.append("%s: %s" % (url, type(e).__name__))
    return {"реестр": info["название"], "статус": "не скачан",
            "причина": "; ".join(errors),
            "что_делать": "скачайте дамп вручную с РФ-IP: %s — распакуйте "
                          "в %s и повторите сверку" % (info["portal"], info["dir"])}


def refresh():
    результаты = [_download(reg) for reg in REGISTRIES]
    ок = sum(1 for r in результаты if r["статус"] == "ок")
    return _out({"статус": "обновлено %d/%d реестров" % (ок, len(REGISTRIES)),
                 "результаты": результаты}, 0 if ок else 1)


def lookup(реестр, inn, cache_dir=None):
    """Сверка ИНН по локальному кэшу дампа. Возвращает (dict|None, заметка).

    Схемно-агностично: ИНН ищется в тексте дампов (.xml/.csv/.txt/.json —
    zip надо распаковать заранее), в «записи» уходит сырой контекст ±400
    символов. Честнее сырой контекст, чем выдуманная структура.
    """
    info = REGISTRIES[реестр]
    d = cache_dir or info["dir"]
    try:
        files = [os.path.join(d, f) for f in sorted(os.listdir(d))
                 if os.path.splitext(f)[1].lower()
                 in (".xml", ".csv", ".txt", ".json")]
    except OSError:
        files = []
    if not files:
        return None, ("кэш отсутствует (%s) — сначала registries_refresh.py "
                      "--refresh с РФ-IP или скачайте дамп вручную: %s"
                      % (d, info["portal"]))

    inn_s = str(inn)
    записи = []
    for path in files:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        for m in re.finditer(re.escape(inn_s), text, INN_RE_FLAGS):
            записи.append({
                "файл": os.path.basename(path),
                "контекст": text[max(0, m.start() - 400):m.end() + 400].strip(),
                "примечание": "сырой контекст записи — схема дампа не разобрана "
                              "(defensive), перепроверьте по первоисточнику",
            })
            if len(записи) >= 5:
                break
        if len(записи) >= 5:
            break

    предупреждения = []
    age = _cache_age_days(реестр, cache_dir)
    if age is not None and age > CACHE_TTL_DAYS:
        предупреждения.append(
            "дамп устарел (%.0f дн., норма %d) — обновите: --refresh"
            % (age, CACHE_TTL_DAYS))
    return {
        "реестр": info["название"],
        "в_реестре": bool(записи),
        "записей": len(записи),
        "записи": записи,
        "возраст_кэша_дней": round(age, 1) if age is not None else None,
        "предупреждения": предупреждения,
        "источник": info["portal"],
        "дата_проверки": time.strftime("%Y-%m-%d"),
    }, "ok"


def check(inn):
    по_реестрам = {}
    for reg in REGISTRIES:
        res, note = lookup(reg, inn)
        по_реестрам[reg] = res if res is not None else {
            "реестр": REGISTRIES[reg]["название"],
            "статус": "не проверено", "причина": note}
        if res is not None:
            по_реестрам[reg]["статус"] = "проверено"
    проверено = any(b["статус"] == "проверено" for b in по_реестрам.values())
    return _out({
        "статус": "проверено" if проверено else "не проверено",
        "запрос": {"инн": inn},
        "по_реестрам": по_реестрам,
        "дата_проверки": time.strftime("%Y-%m-%d"),
    }, 0 if проверено else 1)


def main(argv):
    args = argv[1:]
    if not args:
        sys.stderr.write(__doc__)
        return 2
    if "--refresh" in args:
        return refresh()
    if "--inn" in args:
        i = args.index("--inn")
        if i + 1 >= len(args):
            sys.stderr.write("После --inn нужен ИНН\n")
            return 2
        return check(args[i + 1])
    sys.stderr.write("Укажите --refresh или --inn <ИНН>\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
