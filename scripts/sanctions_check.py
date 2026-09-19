#!/usr/bin/env python3
"""
sanctions_check.py — офлайн-сверка компании/директора с санкционными
перечнями: Росфинмониторинг (террористы/экстремисты), OFAC SDN (США),
EU Consolidated list (ЕС). Только стандартная библиотека.

Списки скачиваются ОДИН раз в кэш ~/.cache/inn-check-ru/ — дальше сверка
идёт локально за секунды, без сети. Каждый список обновляется и деградирует
независимо: недоступен -> честное «не проверено» по этому списку, остальные
работают.

Использование:
    python3 sanctions_check.py --refresh              # скачать/обновить все списки
    python3 sanctions_check.py --inn 7707083893       # точная сверка по ИНН
    python3 sanctions_check.py --name "Иванов Иван Иванович"

Сверка по ИНН — точная (РФМ; у OFAC/EU ИНН в данных нет — там только имя).
Сверка по имени — точное совпадение нормализованной строки (lower, ё→е,
схлопывание пробелов), НЕ нечёткая: однофамильцы — частое ложное срабатывание,
поэтому любое совпадение по имени помечается «требует ручной сверки по дате
рождения». Имена в OFAC/EU — латиницей, сравнивайте латиницу к латинице как
есть; транслитератор осознанно не встроен (источник ложных совпадений).

Источники:
    РФМ  https://www.fedsfm.ru/documents/terrorists-list-portal-simple
    OFAC https://www.treasury.gov/ofac/downloads/sdn.csv
    EU   https://webgate.ec.europa.eu/fsd/fsf/public/files/xmlFullSanctionsList/content?token=dG9rZW4tMjAxNw

TLS-проверка включена всегда, редиректы urllib следует сам (аналог curl -L).
Кэш старше 7 дней -> предупреждение «перечень устарел, обновите».
"""

import csv
import json
import os
import ssl
import sys
import time
import urllib.request


def _ssl_context():
    """Общий TLS-контекст движка: fetch_counterparty._build_ssl_context() подхватывает
    корень УЦ Минцифры (scripts/install_ca.py) и COUNTERPARTY_CA_BUNDLE; без него
    fedsfm.ru и rosstat.gov.ru падают на верификации. Верификация всегда включена."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from fetch_counterparty import _build_ssl_context
        return _build_ssl_context()
    except Exception:
        return ssl.create_default_context()

import xml.etree.ElementTree as ET

CACHE_DIR = os.path.expanduser(os.path.join("~", ".cache", "inn-check-ru"))
CACHE_TTL_DAYS = 7
TIMEOUT = 120
UA = "inn-check-ru/1.2 (sanctions_check; offline cache refresh)"

SOURCES = {
    "рфм": {
        "название": "Росфинмониторинг (террористы/экстремисты)",
        "url": "https://www.fedsfm.ru/documents/terrorists-list-portal-simple",
        "file": os.path.join(CACHE_DIR, "terrorists-list.xml"),
    },
    "ofac": {
        "название": "OFAC SDN (США)",
        "url": "https://www.treasury.gov/ofac/downloads/sdn.csv",
        "file": os.path.join(CACHE_DIR, "sdn.csv"),
    },
    "eu": {
        "название": "EU Consolidated list (ЕС)",
        "url": ("https://webgate.ec.europa.eu/fsd/fsf/public/files/"
                "xmlFullSanctionsList/content?token=dG9rZW4tMjAxNw"),
        "file": os.path.join(CACHE_DIR, "eu-sanctions.xml"),
    },
}


def _out(obj, code=0):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")
    return code


def _normalize(text):
    return " ".join((text or "").lower().replace("ё", "е").split())


def _meta_path(src):
    return SOURCES[src]["file"] + ".meta.json"


def _cache_age_days(src):
    try:
        return (time.time() - os.path.getmtime(SOURCES[src]["file"])) / 86400.0
    except OSError:
        return None


def _download(src):
    """Скачивает один список в кэш. TLS включён. Не падает при сбое сети."""
    info = SOURCES[src]
    os.makedirs(CACHE_DIR, exist_ok=True)
    ctx = _ssl_context()
    req = urllib.request.Request(info["url"], headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as resp:
            data = resp.read()
    except Exception as e:
        return {"список": info["название"], "статус": "не скачан",
                "причина": "%s — скачайте вручную: %s и положите в %s"
                           % (type(e).__name__, info["url"], info["file"])}
    if len(data) < 1000:
        return {"список": info["название"], "статус": "не скачан",
                "причина": "источник вернул %d байт — структура изменилась "
                           "или блокировка" % len(data)}
    with open(info["file"], "wb") as fh:
        fh.write(data)
    meta = {"скачан": time.strftime("%Y-%m-%d %H:%M:%S"),
            "источник": info["url"], "байт": len(data)}
    with open(_meta_path(src), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)
    return {"список": info["название"], "статус": "ок",
            "файл": info["file"], **meta}


def refresh():
    результаты = [_download(src) for src in SOURCES]
    ок = sum(1 for r in результаты if r["статус"] == "ок")
    return _out({"статус": "обновлено %d/%d списков" % (ок, len(SOURCES)),
                 "результаты": результаты}, 0 if ок else 1)


def _local(tag):
    return tag.rsplit("}", 1)[-1].lower()


def _entries_rfm(path):
    """Перечень РФМ (XML) -> [{фио, инн[], дата_рождения}]. Схема исторически
    менялась — парсинг defensive. НЕ ВЕРИФИЦИРОВАНО на живом файле (с не-РФ IP
    перечень не скачивался 19.09.2026)."""
    try:
        root = ET.parse(path).getroot()
    except Exception:
        return None
    nodes = [el for el in root.iter()
             if _local(el.tag) in ("terrorist", "subject", "record", "entity",
                                   "individual", "organization", "item")]
    if not nodes:
        nodes = list(root)
    entries = []
    for node in nodes:
        fio, birth, inns = None, None, []
        for el in node.iter():
            name, text = _local(el.tag), (el.text or "").strip()
            if not text:
                continue
            if name in ("fio", "name", "fullname") and fio is None:
                fio = text
            elif "inn" in name:
                inns.append(text)
            elif name in ("birthdate", "birth", "dob", "datbirth") and birth is None:
                birth = text
        if fio or inns:
            entries.append({"имя": fio, "инн": inns, "дата_рождения": birth})
    return entries


def _entries_ofac(path):
    """OFAC sdn.csv (CSV без заголовка: поле 1 = id, поле 2 = имя CAPS).
    ИНН в файле нет — сверка только по имени. Схема проверена на живом
    файле 19.09.2026."""
    entries = []
    try:
        with open(path, encoding="utf-8", errors="replace", newline="") as fh:
            for row in csv.reader(fh):
                if len(row) < 2 or not row[1].strip():
                    continue
                entries.append({"имя": row[1].strip(), "инн": [],
                                "подробности": row[-1].strip() or None,
                                "id_списка": row[0].strip()})
    except OSError:
        return None
    return entries


def _entries_eu(path):
    """EU Consolidated list (XML, ~20 МБ): sanctionEntity -> nameAlias
    (атрибут wholeName) + identification (атрибут number). Схема проверена
    на живом файле 19.09.2026. Файл может быть обрезан недокачанным —
    iterparse + ParseError: сохраняем разобранное, помечаем предупреждением."""
    entries = []
    truncated = False
    current = None
    try:
        for _, el in ET.iterparse(path, events=("end",)):
            name = _local(el.tag)
            if name == "sanctionentity":
                if current and (current["имена"] or current["номера"]):
                    entries.append(current)
                current = None
                el.clear()
            elif name == "namealias" and current is not None:
                whole = (el.get("wholeName") or "").strip()
                if whole:
                    current["имена"].append(whole)
                el.clear()
            elif name == "identification" and current is not None:
                num = (el.get("number") or "").strip()
                if num:
                    current["номера"].append(num)
                el.clear()
            elif name == "subjecttype" and current is None:
                current = {"имена": [], "номера": [],
                           "тип": el.get("code") or None}
                el.clear()
    except Exception:
        truncated = bool(entries)
        if not entries:
            return None, False
    out = [{"имя": e["имена"][0] if e["имена"] else None,
            "все_имена": e["имена"], "инн": [], "номера": e["номера"],
            "тип": e["тип"]} for e in entries]
    return out, truncated


def _load(src):
    path = SOURCES[src]["file"]
    if not os.path.exists(path):
        return None, "кэш отсутствует"
    if src == "рфм":
        entries = _entries_rfm(path)
        return (entries, None if entries is not None else "кэш не читается")
    if src == "ofac":
        entries = _entries_ofac(path)
        return (entries, None if entries is not None else "кэш не читается")
    entries, truncated = _entries_eu(path)
    if entries is None:
        return None, "кэш не читается"
    if truncated:
        return entries, "файл обрезан (недокачан) — разобрана только часть, обновите: --refresh"
    return entries, None


def check(inn=None, name=None):
    inn_q = (inn or "").strip()
    name_q = _normalize(name)
    совпадения = []
    по_спискам = {}

    for src, info in SOURCES.items():
        entries, проблема = _load(src)
        age = _cache_age_days(src)
        блок = {"список": info["название"], "url": info["url"],
                "возраст_кэша_дней": round(age, 1) if age is not None else None,
                "предупреждения": []}
        if age is not None and age > CACHE_TTL_DAYS:
            блок["предупреждения"].append(
                "перечень устарел (%.0f дн., норма %d) — обновите: --refresh"
                % (age, CACHE_TTL_DAYS))
        if entries is None:
            блок["статус"] = "не проверено"
            блок["причина"] = ("%s — скачайте вручную с подходящего IP: %s "
                               "и положите в %s, затем повторите сверку"
                               % (проблема, info["url"], info["file"]))
        else:
            блок["статус"] = "проверено"
            блок["записей"] = len(entries)
            if проблема:
                блок["предупреждения"].append(проблема)
            for e in entries:
                hit = None
                if inn_q and inn_q in [i.strip() for i in e.get("инн", [])]:
                    hit = "по ИНН (точное)"
                elif inn_q and inn_q in e.get("номера", []):
                    hit = "по номеру идентификации (точное)"
                elif name_q:
                    имена = e.get("все_имена") or [e.get("имя")]
                    if any(_normalize(n) == name_q for n in имена if n):
                        hit = "по имени (точное, нормализованное)"
                if hit:
                    совпадения.append({
                        "список": info["название"],
                        "совпадение": hit,
                        "имя": e.get("имя"),
                        "дата_рождения": e.get("дата_рождения"),
                        "вера": ("высокая — идентификатор уникален, но сверьте "
                                 "имя перед выводами" if "ИНН" in hit or
                                 "номеру" in hit else
                                 "требует ручной сверки по дате рождения — "
                                 "однофамильцы в перечнях часты, это НЕ "
                                 "доказательство"),
                    })
        по_спискам[src] = блок

    проверено_хотя_бы_один = any(
        b["статус"] == "проверено" for b in по_спискам.values())
    return _out({
        "статус": "проверено" if проверено_хотя_бы_один else "не проверено",
        "запрос": {"инн": inn or None, "имя": name or None},
        "совпадений": len(совпадения),
        "совпадения": совпадения,
        "по_спискам": по_спискам,
        "дата_проверки": time.strftime("%Y-%m-%d"),
        "примечание": "совпадение в списках ЕС/США — риск вторичных санкций "
                      "и валютных платежей для экспортных сделок",
    }, 0 if проверено_хотя_бы_один else 1)


def main(argv):
    args = argv[1:]
    if not args:
        sys.stderr.write(__doc__)
        return 2
    if "--refresh" in args:
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
