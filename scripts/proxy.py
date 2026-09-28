#!/usr/bin/env python3
"""
proxy.py — пользовательский прокси одной точкой настройки (спек v2, задача 18.2).

Зачем. Половина источников закрыта для не-РФ IP: kad.arbitr, zakupki, НПД,
детальный эндпоинт «Прозрачного бизнеса». У кого есть своя РФ-нода — пусть
ходит через неё. Проект при этом не становится посредником: узел ваш, доверие
ваше, ответственность ваша.

Чего здесь НЕТ и не будет — общего прокси-пула от сообщества. Узел в середине
видит, КОГО именно вы проверяете, а список проверяемых контрагентов — это
коммерческая тайна и готовая картина ваших сделок до их заключения. Инструмент
для проверки на доверие не может стоять на доверии к анонимному посреднику.

Что важно понимать про доверие к своему узлу:
  * прокси видит АДРЕСА запросов — то есть какие ИНН вы проверяете, даже если
    содержимое зашифровано;
  * содержимое он подменить не может, пока проверяется TLS-сертификат: https
    через прокси идёт CONNECT-туннелем, шифрование остаётся end-to-end;
  * а вот с COUNTERPARTY_INSECURE=1 может и подменить — и тогда «чисто»
    придёт от узла, а не от реестра. Такое сочетание кричит в stderr.

Настройка (первое непустое выигрывает):
    --прокси http://host:3128               # флаг скрипта
    INN_CHECK_PROXY=http://host:3128        # переменная проекта
    HTTPS_PROXY=http://host:3128            # общая переменная окружения

SOCKS5 тоже годится: INN_CHECK_PROXY=socks5://host:1080 (клиент на stdlib в
socks5.py, имя хоста резолвит прокси). Через SOCKS5 ходит только https.

Выход через сетевой интерфейс: INN_CHECK_PROXY=iface://en0 (или iface://<IPv4>).
Это не прокси, а привязка исходящего адреса к локальному интерфейсу — для случая,
когда весь трафик Мака уходит в VPN, а своя сеть выходит с РФ-IP: запросы идут
мимо VPN, напрямую через эту сеть (проверено 28.09.2026: V2Box в режиме TUN,
en0 с российским выходом). Промежуточного узла нет — ИНН видит только ваш
провайдер. Интерфейс без IPv4 — отказ, а не тихий уход в VPN.

С логином — http://<логин>:<пароль>@host:3128. Угловые скобки здесь не часть
синтаксиса, а защита от сканера секретов: образец вида «логин:пароль@хост» в
исходнике TruffleHog считает утечкой и роняет CI (проверено 22.09.2026).
Пароль нигде не показывается: см. маска().

Битый URL — это отказ, а не молчаливый переход на прямое соединение: если
прокси задан, значит IP скрывают намеренно, и «тихо пойдём напрямую» здесь
означает раскрыть его ровно в тот момент, когда этого не ждут.
"""

import base64
import functools
import hashlib
import http.client
import ipaddress
import os
import re
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

ПЕРЕМЕННЫЕ = ("INN_CHECK_PROXY", "HTTPS_PROXY", "https_proxy")
СХЕМЫ = ("http", "https", "socks5", "socks5h", "iface")
SOCKS = ("socks5", "socks5h")
# SOCKS5 — свой клиент на stdlib (socks5.py, DNS на стороне прокси). SOCKS4 не
# умеем: молча проигнорировать его нельзя — пользователь будет думать, что ходит
# через ноду, а трафик пойдёт напрямую с его IP.
СХЕМЫ_БЕЗ_ПОДДЕРЖКИ = ("socks4", "socks4a")


def маска(url):
    """URL без пароля: http://user:***@host:3128 — для логов и JSON."""
    if not url:
        return None
    try:
        p = urllib.parse.urlsplit(url)
    except ValueError:
        return "(URL не разобран)"
    if not p.hostname:
        # без хоста логин и пароль не отделить — URL целиком не показываем (ревью Codex)
        return "%s://(без хоста)" % p.scheme if p.scheme else "(URL без хоста)"
    try:
        порт = p.port
    except ValueError:
        порт = None
    хост = p.hostname + (":%d" % порт if порт else "")
    if p.username:
        хост = "%s:***@%s" % (p.username, хост) if p.password else "%s@%s" % (p.username, хост)
    return "%s://%s" % (p.scheme, хост)


def отпечаток(url):
    """Короткий хеш узла (схема+хост+порт, без логина и пароля).

    Им помечается кэш доступности: прогон через прокси и прогон напрямую дают
    разные картины, и подставлять один вместо другого нельзя.
    """
    if not url:
        return "прямое"
    try:
        p = urllib.parse.urlsplit(url)
        ключ = "%s://%s:%s" % (p.scheme, p.hostname or "", p.port or "")
    except ValueError:
        ключ = url
    return "прокси:" + hashlib.sha256(ключ.encode("utf-8")).hexdigest()[:12]


def _проверить(url):
    """None или текст ошибки."""
    try:
        p = urllib.parse.urlsplit(url)
    except ValueError as e:
        return "URL прокси не разобран: %s" % e
    if p.scheme in СХЕМЫ_БЕЗ_ПОДДЕРЖКИ:
        return ("схема %s не поддерживается: годятся http/https-прокси и socks5 "
                "(ssh -D даёт socks5, ssh -L — http)" % p.scheme)
    if p.scheme not in СХЕМЫ:
        return ("схема %r не годится: ожидается http://, https:// или socks5:// "
                "(получено %r)" % (p.scheme or "нет", маска(url)))
    if not p.hostname:
        return "в URL прокси нет хоста: %r" % маска(url)
    if p.scheme == "iface":
        if p.username or p.password or ":" in p.netloc or p.path not in ("", "/"):
            return "у iface:// только имя интерфейса или IPv4: %r" % маска(url)
        try:
            адрес_интерфейса(p.hostname)
        except ValueError as e:
            return str(e)
        return None
    try:
        порт = p.port
    except ValueError:
        return "порт прокси вне диапазона: %r" % маска(url)
    if p.scheme in SOCKS and not порт:
        return "у socks5-прокси нужен порт: %r" % маска(url)
    if порт is not None and not (0 < порт < 65536):
        return "порт прокси вне диапазона: %r" % маска(url)
    return None


def настройка(явный=None, env=None):
    """Откуда и какой прокси взят.

    {"url", "маска", "источник", "отпечаток", "ошибка"} — url и ошибка
    взаимоисключающи. Без настройки: url=None, отпечаток="прямое".
    """
    env = os.environ if env is None else env
    источник, url = None, None
    if явный:
        источник, url = "--прокси", явный.strip()
    else:
        for имя in ПЕРЕМЕННЫЕ:
            значение = (env.get(имя) or "").strip()
            if значение:
                источник, url = имя, значение
                break
    if not url:
        return {"url": None, "маска": None, "источник": None,
                "отпечаток": "прямое", "ошибка": None}
    ошибка = _проверить(url)
    if ошибка:
        return {"url": None, "маска": маска(url), "источник": источник,
                "отпечаток": "прямое",
                "ошибка": "%s (%s). Прокси задан, но не годится — идти напрямую "
                          "нельзя: это раскрыло бы ваш IP там, где вы его "
                          "намеренно скрываете" % (ошибка, источник)}
    return {"url": url, "маска": маска(url), "источник": источник,
            "отпечаток": отпечаток(url), "ошибка": None}


def handler(url):
    """urllib-хендлер прокси (или пустой ProxyHandler для прямого соединения).

    Пустой ProxyHandler({}) — не то же самое, что отсутствие хендлера: он
    ОТКЛЮЧАЕТ подхват переменных окружения. Нужен там, где прямое соединение
    выбрано осознанно.
    """
    if not url:
        return urllib.request.ProxyHandler({})
    if urllib.parse.urlsplit(url).scheme in SOCKS:
        # socks ProxyHandler'ом не выражается: см. обработчики()
        raise ValueError("socks5-прокси подключается через proxy.обработчики()")
    return _ProxyHandlerБезОбхода({"http": url, "https": url})


class _ProxyHandlerБезОбхода(urllib.request.ProxyHandler):
    """ProxyHandler без исключений NO_PROXY и системных исключений macOS: стандартный
    пускает такие хосты напрямую, и IP раскрылся бы там, где его прячут (ревью
    Codex). Остальное — как в stdlib: Basic-авторизация на прокси, CONNECT для https."""

    def proxy_open(self, req, proxy, type):
        исходный = req.type
        тип, логин, пароль, хост = urllib.request._parse_proxy(proxy)
        тип = тип or исходный
        if логин and пароль:
            пара = "%s:%s" % (urllib.parse.unquote(логин), urllib.parse.unquote(пароль))
            req.add_header("Proxy-authorization",
                           "Basic " + base64.b64encode(пара.encode()).decode("ascii"))
        req.set_proxy(urllib.parse.unquote(хост), тип)
        if исходный == тип or исходный == "https":
            return None
        return self.parent.open(req, timeout=req.timeout)


def адрес_интерфейса(имя, _запуск=subprocess.run):
    """Имя интерфейса (en0) или IPv4 -> IPv4 для source_address. ValueError, если
    адреса нет: идти тогда по умолчанию — значит молча уйти в VPN."""
    try:
        return str(ipaddress.IPv4Address(имя))
    except ValueError:
        pass
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]{0,31}", имя or ""):
        raise ValueError("iface: негодное имя интерфейса %r" % имя)
    for команда in (["ipconfig", "getifaddr", имя],               # macOS
                    ["ip", "-4", "-o", "addr", "show", "dev", имя]):  # Linux
        try:
            вывод = _запуск(команда, capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        m = re.search(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])", вывод or "")
        if m:
            return str(ipaddress.IPv4Address(m.group(1)))
    raise ValueError("iface: у интерфейса %s нет IPv4-адреса (сеть отключена?)" % имя)


IP_BOUND_IF = 25     # macOS, <netinet/in.h>: сокет ходит только через этот интерфейс


def _соединить(адрес, интерфейс, цель, timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
               source_address=None):
    """socket.create_connection, но только IPv4 и с привязкой к интерфейсу: на macOS
    — IP_BOUND_IF (как curl --interface), иначе только исходный адрес. Одной
    привязки адреса мало: при VPN в режиме TUN маршрут выбирается по таблице, и
    пакет мог бы уйти в туннель (ревью Codex)."""
    хост, порт = цель
    ошибка = None
    for *_, sa in socket.getaddrinfo(хост, порт, socket.AF_INET, socket.SOCK_STREAM):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if интерфейс and sys.platform == "darwin":
                s.setsockopt(socket.IPPROTO_IP, IP_BOUND_IF, socket.if_nametoindex(интерфейс))
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                s.settimeout(timeout)
            s.bind((адрес, 0))
            s.connect(sa)
            return s
        except OSError as e:
            ошибка = e
            s.close()
    raise ошибка or OSError("iface: у %s нет IPv4-адреса" % хост)


def _через_интерфейс(класс, адрес, интерфейс):
    def фабрика(host, **kw):
        соединение = класс(host, **kw)
        соединение._create_connection = functools.partial(_соединить, адрес, интерфейс)
        return соединение
    return фабрика


class _HTTPSчерезИнтерфейс(urllib.request.HTTPSHandler):
    def __init__(self, адрес, интерфейс, context):
        super().__init__(context=context)
        self._фабрика = _через_интерфейс(http.client.HTTPSConnection, адрес, интерфейс)

    def https_open(self, req):
        return self.do_open(self._фабрика, req, context=self._context)


class _HTTPчерезИнтерфейс(urllib.request.HTTPHandler):
    def __init__(self, адрес, интерфейс):
        super().__init__()
        self._фабрика = _через_интерфейс(http.client.HTTPConnection, адрес, интерфейс)

    def http_open(self, req):
        return self.do_open(self._фабрика, req)


class _ТолькоHTTPчерезПрокси(urllib.request.BaseHandler):
    """ftp://, file:// и data: через прокси не идут: редирект https -> ftp ушёл бы
    напрямую, мимо прокси (ревью Codex). handler_order меньше стандартного (500)."""
    handler_order = 100

    def _отказ(self, req):
        raise urllib.error.URLError("через прокси разрешены только http(s)://")

    ftp_open = file_open = data_open = _отказ


def обработчики(url, context):
    """Хендлеры urllib для прокси url (или прямого соединения) с TLS-контекстом.

    http/https-прокси — ProxyHandler + HTTPSHandler; socks5 — пустой
    ProxyHandler (переменные окружения не подхватываются), HTTPS через туннель
    SOCKS5 и отказ для http:// (иначе он ушёл бы напрямую, мимо прокси)."""
    if url and urllib.parse.urlsplit(url).scheme == "iface":
        имя = urllib.parse.urlsplit(url).hostname
        адрес = адрес_интерфейса(имя)
        интерфейс = None if адрес == имя else имя
        return [urllib.request.ProxyHandler({}), _HTTPSчерезИнтерфейс(адрес, интерфейс, context),
                _HTTPчерезИнтерфейс(адрес, интерфейс), _ТолькоHTTPчерезПрокси()]
    if url and urllib.parse.urlsplit(url).scheme in SOCKS:
        import socks5
        return [urllib.request.ProxyHandler({}), socks5.SocksHTTPSHandler(url, context),
                socks5._ТолькоHTTPS()]
    if url:
        return [handler(url), urllib.request.HTTPSHandler(context=context),
                _ТолькоHTTPчерезПрокси()]
    return [handler(url), urllib.request.HTTPSHandler(context=context)]


def описание(конф, insecure=False):
    """Блок «_сеть» для JSON-вывода: через что шли и что это значит."""
    if not конф.get("url"):
        return {"через": "прямое соединение", "источник_настройки": None,
                "отпечаток": "прямое"}
    оговорка = ("узел видит, какие ИНН вы проверяете (адреса запросов), но не "
                "содержимое: https идёт CONNECT-туннелем, TLS остаётся "
                "end-to-end")
    if конф["url"].startswith("iface://"):
        оговорка = ("не прокси: сокет привязан к локальному интерфейсу (на macOS — "
                    "IP_BOUND_IF, как curl --interface; на Linux — только исходный адрес, "
                    "маршрут решает система), узла посередине нет, адреса запросов "
                    "видит ваш провайдер")
    if insecure:
        оговорка = ("COUNTERPARTY_INSECURE=1 вместе с прокси: узел видит адреса "
                    "запросов И может подменить ответы — проверка TLS отключена. "
                    "Данные из такого прогона доказательством не являются")
    return {"через": конф["маска"], "источник_настройки": конф["источник"],
            "отпечаток": конф["отпечаток"], "оговорка": оговорка}
