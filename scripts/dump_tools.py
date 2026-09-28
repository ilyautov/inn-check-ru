#!/usr/bin/env python3
"""
dump_tools.py — общее для кэш-источников, которые скачивают выгрузки с сайтов,
закрытых для не-РФ адресов (ФТС, РКН, РАР). Только stdlib.

Сеть идёт через прокси пользователя (scripts/proxy.py: --прокси >
INN_CHECK_PROXY > HTTPS_PROXY; http/https или socks5). Без прокси — прямое
соединение: из РФ сайты открыты и так, из-за рубежа скачивание честно падает, и
старый индекс остаётся цел. Битый URL прокси — отказ, а не тихий прямой выход.

Индекс пишется «всё или ничего»: временный файл рядом и os.replace. Второй
refresh параллельно не идёт (блокировка O_EXCL, протухает через 2 часа).
"""

import contextlib
import datetime as dt
import gzip
import json
import os
import ssl
import sys
import tempfile
import time
import urllib.request

UA = "inn-check-ru (+https://github.com/ilyautov/inn-check-ru)"
БЛОКИРОВКА_ЧАСОВ = 2


def сегодня():
    return dt.datetime.now(dt.timezone.utc).astimezone().date().isoformat()


def _ssl_context():
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from fetch_counterparty import _build_ssl_context
        return _build_ssl_context()
    except Exception:
        return ssl.create_default_context()


def opener():
    """urllib-opener через прокси пользователя (или напрямую, если не задан)."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import proxy
    конф = proxy.настройка()
    if конф["ошибка"]:
        raise ValueError("прокси: " + конф["ошибка"])
    return urllib.request.build_opener(*proxy.обработчики(конф["url"], _ssl_context()))


def get(url, referer, лимит, timeout=180, в_файл=None, op=None):
    """GET с честным UA и Referer страницы, где стоит ссылка. Больше лимита —
    отказ; меньше Content-Length — обрыв (отказ). в_файл — писать на диск
    потоком (для сотен мегабайт), иначе вернуть bytes."""
    op = op or opener()
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": referer})
    n = 0
    части = []
    with contextlib.ExitStack() as стек:
        куда = стек.enter_context(open(в_файл, "wb")) if в_файл else None
        r = стек.enter_context(op.open(req, timeout=timeout))
        ожидаем = r.headers.get("Content-Length")
        while True:
            кусок = r.read(1 << 20)
            if not кусок:
                break
            n += len(кусок)
            if n > лимит:
                raise ValueError("ответ больше лимита %d МБ" % (лимит >> 20))
            if куда:
                куда.write(кусок)
            else:
                части.append(кусок)
    if ожидаем and ожидаем.isdigit() and n != int(ожидаем):
        raise ValueError("обрыв: получено %d байт из %s" % (n, ожидаем))
    return None if в_файл else b"".join(части)


def записать_индекс(путь, объект):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(путь), prefix=".индекс-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as сырой, gzip.GzipFile(fileobj=сырой, mode="wb") as gz:
            gz.write(json.dumps(объект, ensure_ascii=False).encode("utf-8"))
        os.replace(tmp, путь)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def прочитать_индекс(путь):
    with gzip.open(путь, "rt", encoding="utf-8") as fh:
        return json.load(fh)


class Блокировка:
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


def старше(дата_iso, сегодня_iso, дней):
    return (dt.date.fromisoformat(сегодня_iso) - dt.date.fromisoformat(дата_iso)).days > дней
