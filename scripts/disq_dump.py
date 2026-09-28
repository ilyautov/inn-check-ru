#!/usr/bin/env python3
"""
disq_dump.py — реестр дисквалифицированных лиц ФНС из открытых данных, офлайн.
Только stdlib.

ФНС еженедельно выкладывает реестр CSV-файлом (data.nalog.ru/opendata/
7707329152-registerdisqualified, стандарт opendata 3.0, без капчи). Живьём
13.09.2026: 8 202 записи, 14 колонок G1–G14. Смысл колонок проверен по данным,
а не угадан по названию набора:

    G1  номер записи (12 цифр; контрольную сумму ИНН проходят 79 из 8 202 —
        это НЕ ИНН человека)
    G2  ФИО            G3 дата рождения     G4 место рождения
    G5  организация    G6 ИНН организации (у 2 936 записей, все валидные)
    G7  должность      G8 статья КоАП       G9 орган, составивший протокол
    G10 судья          G11 должность судьи  G12 срок
    G13 начало         G14 окончание (ДД.ММ.ГГГГ)

    python3 disq_dump.py --refresh                       # скачать, построить индекс
    python3 disq_dump.py --inn 7707083893 --фио "…"      # сверка по индексу

Что даёт по сравнению с онлайн-поиском (блок «спецреестры», service.nalog.ru):
сверка локальная и работает, когда сервис недоступен; плюс история компании по
ИНН организации (G6) — были ли дисквалифицированы её руководители.

Персональные данные. Индекс хранит ФИО только как HMAC-SHA256 с ключом,
созданным на этой машине (файл «ключ», права 600); дату и место рождения,
судью и сам CSV не хранит — CSV удаляется после сборки. В ответе — номер
записи, организация, должность, статья и срок: по номеру запись находится в
реестре ФНС, если нужно сверить дату рождения (правило однофамильцев).
"""

import csv
import datetime as dt
import gzip
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import ssl
import sys
import tempfile
import time
import urllib.request

CACHE_DIR = os.path.expanduser(os.path.join("~", ".cache", "inn-check-ru", "disq"))
CACHE_DIR_ВИДИМЫЙ = "~/.cache/inn-check-ru/disq"
ИНДЕКС = "индекс.json.gz"
КЛЮЧ = "ключ"
TIMEOUT = 180
UA = "inn-check-ru (+https://github.com/ilyautov/inn-check-ru)"
ПОРТАЛ = "https://data.nalog.ru/opendata/7707329152-registerdisqualified"
ЗАГОЛОВОК = ["G%d" % i for i in range(1, 15)]
# живьём 13.09.2026 CSV 3,4 МБ — запас на порядок
ЛИМИТ_META = 1 << 20
ЛИМИТ_CSV = 64 << 20
ДОЛЯ_ПАДЕНИЯ = 0.5
СВЕЖЕСТЬ_ДНЕЙ = 14
БЛОКИРОВКА_ЧАСОВ = 2
ПРИМЕЧАНИЕ = ("совпадение по ФИО без даты и места рождения — не идентификация лица: "
              "сверьте запись по номеру в реестре ФНС (service.nalog.ru/disqualified.do) "
              "с данными руководителя")


def _сегодня():
    return dt.datetime.now(dt.timezone.utc).astimezone().date().isoformat()


def _ssl_context():
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from fetch_counterparty import _build_ssl_context
        return _build_ssl_context()
    except Exception:
        return ssl.create_default_context()


def _get(url, лимит):
    """GET с лимитом размера, только с портала набора."""
    if not url.startswith(ПОРТАЛ + "/"):
        raise ValueError("адрес вне набора открытых данных ФНС: %s" % url[:80])
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    буфер, n = io.BytesIO(), 0
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=_ssl_context()) as r:
        while True:
            кусок = r.read(1 << 20)
            if not кусок:
                break
            n += len(кусок)
            if n > лимит:
                raise ValueError("ответ больше лимита %d МБ" % (лимит >> 20))
            буфер.write(кусок)
    return буфер.getvalue()


def _мета(получить):
    """meta.csv -> (url последних данных, дата выгрузки ГГГГ-ММ-ДД, valid)."""
    текст = получить(ПОРТАЛ + "/meta.csv", ЛИМИТ_META).decode("utf-8-sig")
    строки = {r[0].strip(): r[1].strip() for r in csv.reader(io.StringIO(текст))
              if len(r) >= 2}
    данные = sorted((k, v) for k, v in строки.items() if re.match(r"data-\d{8}-", k))
    if not данные:
        raise ValueError("в meta.csv нет ссылок на данные")
    ключ, url = данные[-1]
    m = re.match(r"data-(\d{4})(\d{2})(\d{2})", ключ)
    v = re.match(r"(\d{4})(\d{2})(\d{2})$", строки.get("valid", ""))
    return url, "%s-%s-%s" % m.groups(), ("%s-%s-%s" % v.groups() if v else None)


def норм_фио(фио):
    return " ".join(str(фио or "").replace("ё", "е").replace("Ё", "Е").upper().split())


def _ключ(cache_dir):
    """Ключ HMAC этой машины: создаётся один раз, права 600, в репозиторий не едет."""
    путь = os.path.join(cache_dir, КЛЮЧ)
    try:
        with open(путь, "rb") as fh:
            k = fh.read()
        if len(k) == 32:
            return k
    except FileNotFoundError:
        pass
    k = secrets.token_bytes(32)
    fd = os.open(путь, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(k)
    return k


def _хеш(ключ, фио):
    return hmac.new(ключ, норм_фио(фио).encode("utf-8"), hashlib.sha256).hexdigest()


def _дата(s):
    """«ДД.ММ.ГГГГ» -> ISO; иное — ValueError (схема)."""
    m = re.fullmatch(r"(\d{2})\.(\d{2})\.(\d{4})", s.strip())
    if not m:
        raise ValueError("схема: дата %r не ДД.ММ.ГГГГ" % s[:20])
    return dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()


def построить_индекс(сырые, путь, ключ, выгрузка, valid, url, прежний=None, сегодня=None):
    """CSV (bytes) -> индекс на диск. ValueError — схема не та или записей
    подозрительно мало (старый индекс цел)."""
    строки = csv.reader(io.StringIO(сырые.decode("utf-8-sig")))
    заголовок = [h.strip() for h in next(строки, [])]
    if заголовок != ЗАГОЛОВОК:
        raise ValueError("схема: заголовок %r вместо G1..G14" % заголовок[:16])
    записи, по_фио, по_инн = [], {}, {}
    for r in строки:
        if not any(x.strip() for x in r):
            continue
        if len(r) != 14:
            raise ValueError("схема: строка из %d колонок" % len(r))
        r = [x.strip() for x in r]
        if not r[0] or not r[1]:
            raise ValueError("схема: запись без номера или ФИО")
        инн = r[5] if re.fullmatch(r"\d{10}|\d{12}", r[5]) else None
        i = len(записи)
        записи.append({"номер_записи": r[0], "организация": r[4], "инн_организации": инн,
                       "должность": r[6], "статья_коап": r[7], "срок": r[11],
                       "с": _дата(r[12]), "по": _дата(r[13])})
        по_фио.setdefault(_хеш(ключ, r[1]), []).append(i)
        if инн:
            по_инн.setdefault(инн, []).append(i)
    было = (прежний or {}).get("записей") or 0
    if not записи or len(записи) < было * ДОЛЯ_ПАДЕНИЯ:
        raise ValueError("схема: %d записей (было %d)" % (len(записи), было))
    мета = {"дата_выгрузки": выгрузка, "действителен_до": valid, "url": url,
            "записей": len(записи)}
    папка = os.path.dirname(путь)
    fd, tmp = tempfile.mkstemp(dir=папка, prefix=".индекс-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as сырой, gzip.GzipFile(fileobj=сырой, mode="wb") as gz:
            gz.write(json.dumps(dict(мета, построен=сегодня or _сегодня(), записи=записи,
                                     по_фио=по_фио, по_инн=по_инн),
                                ensure_ascii=False).encode("utf-8"))
        os.replace(tmp, путь)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return мета


def _прочитать(путь):
    with gzip.open(путь, "rt", encoding="utf-8") as fh:
        return json.load(fh)


class _Блокировка:
    def __init__(self, папка):
        self.путь = os.path.join(папка, ".refresh.lock")

    def __enter__(self):
        try:
            if time.time() - os.path.getmtime(self.путь) > БЛОКИРОВКА_ЧАСОВ * 3600:
                os.unlink(self.путь)
        except OSError:
            pass
        self.fd = os.open(self.путь, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        return self

    def __exit__(self, *exc):
        os.close(self.fd)
        os.unlink(self.путь)


def refresh(cache_dir=None, получить=_get):
    """Скачивает последнюю выгрузку и пересобирает индекс. Любой сбой — старый
    индекс остаётся. CSV держится только в памяти и на диск не пишется."""
    cache_dir = cache_dir or CACHE_DIR
    os.makedirs(cache_dir, mode=0o700, exist_ok=True)
    путь = os.path.join(cache_dir, ИНДЕКС)
    try:
        блок = _Блокировка(cache_dir).__enter__()
    except FileExistsError:
        return {"статус": "индекс не обновлён: другой refresh уже идёт"}
    try:
        try:
            url, выгрузка, valid = _мета(получить)
            сырые = получить(url, ЛИМИТ_CSV)
        except Exception as e:
            return {"статус": "индекс не обновлён: не скачано (%s: %s)"
                              % (type(e).__name__, str(e)[:160])}
        try:
            прежний = _прочитать(путь) if os.path.exists(путь) else None
        except Exception:
            прежний = None
        try:
            мета = построить_индекс(сырые, путь, _ключ(cache_dir), выгрузка, valid, url,
                                    прежний)
        except Exception as e:
            return {"статус": "индекс не обновлён: %s" % str(e)[:200]}
        return {"статус": "обновлено", **мета}
    finally:
        блок.__exit__(None, None, None)


def устарел(индекс, сегодня):
    срок = индекс.get("действителен_до")
    if срок is None:
        от = индекс.get("дата_выгрузки") or "1970-01-01"
        if (dt.date.fromisoformat(сегодня) - dt.date.fromisoformat(от)).days > СВЕЖЕСТЬ_ДНЕЙ:
            return ["срок годности выгрузки не распознан, выгрузка старше %d дней — "
                    "обновите: disq_dump.py --refresh" % СВЕЖЕСТЬ_ДНЕЙ]
        return []
    if срок < сегодня:
        return ["выгрузка устарела: действительна до %s — обновите: disq_dump.py "
                "--refresh" % срок]
    return []


def lookup(inn, фио=None, cache_dir=None, сегодня=None):
    """-> (dict|None, заметка). None — индекса нет.
    дисквалификация_руководителя: True — действующая запись с точно тем же ФИО;
    False — ФИО известно, действующих записей с ним нет; None — ФИО не дано."""
    cache_dir = cache_dir or CACHE_DIR
    путь = os.path.join(cache_dir, ИНДЕКС)
    try:
        индекс = _прочитать(путь)
    except FileNotFoundError:
        return None, ("индекса нет (%s) — сначала disq_dump.py --refresh"
                      % (CACHE_DIR_ВИДИМЫЙ if cache_dir == CACHE_DIR else путь))
    сегодня = сегодня or _сегодня()
    записи = индекс["записи"]

    def действует(з):
        return з["с"] <= сегодня <= з["по"]

    свои = set()
    if фио:
        # ключ читается, а не создаётся: без ключа индекса хеши не сверить
        with open(os.path.join(cache_dir, КЛЮЧ), "rb") as fh:
            свои = set(индекс["по_фио"].get(_хеш(fh.read(), фио), []))
    по_фио = [dict(записи[i], действует=действует(записи[i]),
                   та_же_компания=записи[i]["инн_организации"] == str(inn))
              for i in sorted(свои)]
    история = [dict(записи[i], действует=действует(записи[i]),
                    фио_как_у_руководителя=(i in свои) if фио else None)
               for i in индекс["по_инн"].get(str(inn), [])]
    return {
        "дисквалификация_руководителя": (any(з["действует"] for з in по_фио)
                                         if фио else None),
        "совпадения_по_фио": по_фио,
        "история_компании": история,
        "примечание": ПРИМЕЧАНИЕ,
        "дата_выгрузки": индекс.get("дата_выгрузки"),
        "предупреждения": устарел(индекс, сегодня),
        "источник": ПОРТАЛ,
    }, "ok"


def main(argv):
    args = argv[1:]
    if args == ["--refresh"]:
        res = refresh()
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res["статус"] == "обновлено" else 1
    if len(args) in (2, 4) and args[0] == "--inn" and (len(args) == 2 or args[2] == "--фио"):
        res, note = lookup(args[1], args[3] if len(args) == 4 else None)
        print(json.dumps(res if res is not None else {"статус": "не проверено",
                                                      "причина": note},
                         ensure_ascii=False, indent=2))
        return 0 if res is not None else 1
    sys.stderr.write(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
