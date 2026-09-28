#!/usr/bin/env python3
"""
rospatent_tz.py — товарные знаки по ИНН правообладателя из открытого реестра
Роспатента, офлайн. Только stdlib.

Роспатент раз в месяц выкладывает открытый реестр товарных знаков одним CSV
(rospatent.gov.ru/opendata/7730176088-tz, стандарт opendata 3.0, без капчи).
Живьём 02.09.2026: 693 МБ, UTF-8 с BOM, разделитель «,», 58 колонок; заголовки —
с пробелами («right holder inn»), паспорт meta.csv — в cp1251 через «;».

ИНН правообладателя заполнен не у всех записей: у первых регистраций (номера до
~20 000) — у 2 из 792, у знаков 2019 года — у 81 % российских, у знаков 2026 года —
почти у всех. Поэтому «записи нет» не значит «знаков нет».

    python3 rospatent_tz.py --refresh          # ~700 МБ потоком, на диск не пишется
    python3 rospatent_tz.py --inn 7707083893

Индекс хранит по ИНН только номер регистрации, даты регистрации и окончания и
признак действия правовой охраны (actual). Наименование правообладателя (у ИП и
физлиц — ФИО) и адреса не хранятся.
"""

import csv
import datetime as dt
import gzip
import io
import json
import os
import re
import ssl
import sys
import tempfile
import time
import urllib.request

CACHE_DIR = os.path.expanduser(os.path.join("~", ".cache", "inn-check-ru", "tz"))
CACHE_DIR_ВИДИМЫЙ = "~/.cache/inn-check-ru/tz"
ИНДЕКС = "индекс.json.gz"
TIMEOUT = 300
UA = "inn-check-ru (+https://github.com/ilyautov/inn-check-ru)"
ПОРТАЛ = "https://rospatent.gov.ru/opendata/7730176088-tz"
ПОЛЯ = {"номер": "registration number", "дата": "registration date",
        "срок": "expiration date", "инн": "right holder inn", "действует": "actual"}
ЛИМИТ_META = 1 << 20
ЛИМИТ_CSV = 4 << 30          # живьём 693 МБ
# Реестр знаков только растёт: прекращённые остаются с actual=false. Меньше 98 %
# прежних строк — оборванная или неполная выгрузка (ревью Codex, P1)
ДОЛЯ_ПАДЕНИЯ = 0.98
СВЕЖЕСТЬ_ДНЕЙ = 45           # выгрузка раз в месяц
БЛОКИРОВКА_ЧАСОВ = 3
ПОКАЗАТЬ = 20                # в ответе — последние знаки, счёт — по всем


def _сегодня():
    return dt.datetime.now(dt.timezone.utc).astimezone().date().isoformat()


# rospatent.gov.ru не отдаёт промежуточный сертификат (проверено 28.09.2026:
# openssl «unable to verify the first certificate»). Браузеры и curl на macOS
# достраивают цепочку сами, Python — нет. Промежуточный GlobalSign GCC R3 DV TLS CA
# 2020 взят по ссылке AIA из сертификата сайта и сверен с корнем GlobalSign R3
# системного хранилища; проверка TLS остаётся полной.
ПРОМЕЖУТОЧНЫЙ = "globalsign_gcc_r3_dv_tls_ca_2020.pem"


def _промежуточный():
    рядом = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "data",
                         "ca", ПРОМЕЖУТОЧНЫЙ)
    if os.path.isfile(рядом):
        return рядом
    try:                                   # установка колесом: пакет данных
        from importlib import resources
        путь = resources.files("inn_check_ru_data").joinpath("ca", ПРОМЕЖУТОЧНЫЙ)
        return str(путь) if путь.is_file() else None
    except Exception:
        return None


def _ssl_context():
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from fetch_counterparty import _build_ssl_context
        ctx = _build_ssl_context()
    except Exception:
        ctx = ssl.create_default_context()
    пром = _промежуточный()
    if пром:
        ctx.load_verify_locations(cafile=пром)
        # Python 3.13+ по умолчанию принимает частичную цепочку: промежуточный стал
        # бы якорем доверия. Снимаем флаг — цепочка обязана дойти до системного
        # корня (ревью Codex)
        ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_PARTIAL_CHAIN", 0)
    return ctx


class _Счётчик(io.RawIOBase):
    """Поток ответа с лимитом размера: больше лимита — ValueError посреди чтения."""

    def __init__(self, поток, лимит, ожидаем=None):
        self.поток, self.лимит, self.n, self.ожидаем = поток, лимит, 0, ожидаем

    def readable(self):
        return True

    def readinto(self, b):
        кусок = self.поток.read(len(b))
        if not кусок and self.ожидаем is not None and self.n != self.ожидаем:
            # сервер закрыл поток раньше Content-Length — выгрузка неполная
            raise ValueError("поток оборван: %d из %d байт" % (self.n, self.ожидаем))
        self.n += len(кусок)
        if self.n > self.лимит:
            raise ValueError("ответ больше лимита %d МБ" % (self.лимит >> 20))
        b[:len(кусок)] = кусок
        return len(кусок)


def _открыть(url, лимит):
    """GET только с портала набора -> бинарный поток с лимитом."""
    if not url.startswith(ПОРТАЛ + "/"):
        raise ValueError("адрес вне набора открытых данных Роспатента: %s" % url[:80])
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    ответ = urllib.request.urlopen(req, timeout=TIMEOUT, context=_ssl_context())
    длина = ответ.headers.get("Content-Length")
    return io.BufferedReader(_Счётчик(ответ, лимит, int(длина) if длина and
                                      длина.isdigit() else None), 1 << 20)


def _мета(открыть):
    """meta.csv (cp1251 или UTF-8, «;» или «,») -> (url данных, дата выгрузки)."""
    with открыть(ПОРТАЛ + "/meta.csv", ЛИМИТ_META) as fh:
        сырые = fh.read()
    try:
        текст = сырые.decode("utf-8-sig")
    except UnicodeDecodeError:
        текст = сырые.decode("cp1251")
    раздел = ";" if текст.count(";") > текст.count(",") else ","
    строки = {r[0].strip(): r[1].strip() for r in csv.reader(io.StringIO(текст),
                                                              delimiter=раздел) if len(r) >= 2}
    данные = sorted((k, v) for k, v in строки.items() if re.match(r"data-\d{8}-", k))
    if not данные:
        raise ValueError("в meta.csv нет ссылок на данные")
    ключ, url = данные[-1]
    m = re.match(r"data-(\d{4})(\d{2})(\d{2})", ключ)
    return url, "%s-%s-%s" % m.groups()


def _дата(s):
    s = (s or "").strip()
    if not s:
        return None
    if not re.fullmatch(r"\d{8}", s):
        raise ValueError("схема: дата %r не ГГГГММДД" % s[:20])
    return "%s-%s-%s" % (s[:4], s[4:6], s[6:])


def разобрать(поток_текста):
    """CSV реестра -> (по_инн, всего_записей, с_инн). ValueError — схема."""
    csv.field_size_limit(64 << 20)
    чтец = csv.reader(поток_текста)
    шапка = [h.replace("﻿", "").strip().lower().replace("_", " ")
             for h in next(чтец, [])]
    нет = [п for п in ПОЛЯ.values() if п not in шапка]
    if нет:
        raise ValueError("схема: в заголовке нет %s" % ", ".join(нет))
    кол = {k: шапка.index(v) for k, v in ПОЛЯ.items()}
    по_инн, всего, с_инн = {}, 0, 0
    for r in чтец:
        if not r:
            continue
        if len(r) != len(шапка):
            raise ValueError("схема: строка из %d колонок вместо %d" % (len(r), len(шапка)))
        всего += 1
        инн = r[кол["инн"]].strip()
        if not инн:
            continue
        if not re.fullmatch(r"\d{10}|\d{12}", инн):
            continue                    # иностранный или испорченный — не ИНН РФ
        действует = r[кол["действует"]].strip().lower()
        if действует not in ("true", "false"):
            raise ValueError("схема: actual %r" % действует[:20])
        с_инн += 1
        по_инн.setdefault(инн, []).append([
            r[кол["номер"]].replace("﻿", "").strip(), _дата(r[кол["дата"]]),
            _дата(r[кол["срок"]]), действует == "true"])
    return по_инн, всего, с_инн


def построить_индекс(поток_текста, путь, выгрузка, url, прежний=None, сегодня=None):
    по_инн, всего, с_инн = разобрать(поток_текста)
    было = (прежний or {}).get("записей") or 0
    if not с_инн or всего < было * ДОЛЯ_ПАДЕНИЯ:
        raise ValueError("схема: записей %d, с ИНН %d (было %d)" % (всего, с_инн, было))
    мета = {"дата_выгрузки": выгрузка, "url": url, "записей": всего, "с_инн": с_инн,
            "правообладателей": len(по_инн)}
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(путь), prefix=".индекс-", suffix=".tmp")
    try:
        # потоком: без полной копии индекса строкой в памяти (ревью Codex)
        with os.fdopen(fd, "wb") as сырой, gzip.GzipFile(fileobj=сырой, mode="wb") as gz, \
                io.TextIOWrapper(gz, encoding="utf-8") as текст:
            json.dump(dict(мета, построен=сегодня or _сегодня(), по_инн=по_инн), текст,
                      ensure_ascii=False, separators=(",", ":"))
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


def refresh(cache_dir=None, открыть=_открыть):
    """Потоковое скачивание и разбор; обрыв, схема, падение вдвое — старый индекс цел."""
    cache_dir = cache_dir or CACHE_DIR
    os.makedirs(cache_dir, exist_ok=True)
    путь = os.path.join(cache_dir, ИНДЕКС)
    try:
        блок = _Блокировка(cache_dir).__enter__()
    except FileExistsError:
        return {"статус": "индекс не обновлён: другой refresh уже идёт"}
    try:
        try:
            прежний = _прочитать(путь) if os.path.exists(путь) else None
        except Exception:
            прежний = None
        try:
            url, выгрузка = _мета(открыть)
            if прежний and прежний.get("дата_выгрузки") == выгрузка:
                return {"статус": "обновлено: выгрузка %s уже в индексе" % выгрузка}
            with открыть(url, ЛИМИТ_CSV) as сырой:
                текст = io.TextIOWrapper(сырой, encoding="utf-8-sig", newline="")
                мета = построить_индекс(текст, путь, выгрузка, url, прежний)
        except Exception as e:
            return {"статус": "индекс не обновлён: %s: %s" % (type(e).__name__, str(e)[:200])}
        return {"статус": "обновлено", **мета}
    finally:
        блок.__exit__(None, None, None)


def устарел(индекс, сегодня):
    от = индекс.get("дата_выгрузки") or "1970-01-01"
    if (dt.date.fromisoformat(сегодня) - dt.date.fromisoformat(от)).days > СВЕЖЕСТЬ_ДНЕЙ:
        return ["выгрузка товарных знаков от %s старше %d дней — обновите: "
                "rospatent_tz.py --refresh" % (от, СВЕЖЕСТЬ_ДНЕЙ)]
    return []


def lookup(inn, cache_dir=None, сегодня=None):
    """-> (dict|None, заметка). None — индекса нет."""
    cache_dir = cache_dir or CACHE_DIR
    путь = os.path.join(cache_dir, ИНДЕКС)
    try:
        индекс = _прочитать(путь)
    except FileNotFoundError:
        return None, ("индекса нет (%s) — сначала rospatent_tz.py --refresh"
                      % (CACHE_DIR_ВИДИМЫЙ if cache_dir == CACHE_DIR else путь))
    сегодня = сегодня or _сегодня()
    знаки = []
    for номер, дата, срок, actual in индекс["по_инн"].get(str(inn), []):
        # охрана по реестру и срок не истёк на сегодня; без срока — неизвестно
        знаки.append({"номер": номер, "дата_регистрации": дата, "срок_до": срок,
                      "действует": (None if срок is None else
                                    bool(actual) and срок >= сегодня)})
    знаки.sort(key=lambda з: (з["дата_регистрации"] or "", з["номер"]), reverse=True)
    return {
        "в_реестре": bool(знаки),
        "знаков": len(знаки),
        "действующих": sum(1 for з in знаки if з["действует"] is True),
        "последние": знаки[:ПОКАЗАТЬ],
        "ссылка": "https://www1.fips.ru/registers-web/ — поиск по номеру регистрации",
        "дата_выгрузки": индекс.get("дата_выгрузки"),
        "предупреждения": устарел(индекс, сегодня),
        "источник": ПОРТАЛ,
    }, "ok"


def main(argv):
    args = argv[1:]
    if args == ["--refresh"]:
        res = refresh()
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res["статус"].startswith("обновлено") else 1
    if len(args) == 2 and args[0] == "--inn":
        res, note = lookup(args[1])
        print(json.dumps(res if res is not None else {"статус": "не проверено",
                                                      "причина": note},
                         ensure_ascii=False, indent=2))
        return 0 if res is not None else 1
    sys.stderr.write(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
