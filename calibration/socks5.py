#!/usr/bin/env python3
"""
socks5.py — HTTPS через SOCKS5 на чистом stdlib (инструмент калибровки).

Зачем отдельно. Движок (scripts/proxy.py) SOCKS5 сознательно НЕ принимает: в
stdlib его нет, а тихий откат на прямое соединение хуже отказа. Калибровке нужен
РФ-выход к ЕФРСБ, а у сопровождающего он — SOCKS5. Этот модуль в пакет не едет
и движок не трогает.

Протокол: RFC 1928 (CONNECT, имя хоста отдаётся прокси — DNS на его стороне,
как socks5h) + RFC 1929 (логин/пароль). Поверх туннеля — обычный TLS с SNI и
проверкой сертификата.

    opener = socks5.opener("socks5://логин:пароль@host:port")
    opener.open("https://bankrot.fedresurs.ru/backend/...", timeout=30)
"""

import http.client
import socket
import ssl
import struct
import urllib.error
import urllib.parse
import urllib.request


class ОшибкаSocks(OSError):
    pass


_ОТВЕТЫ = {
    1: "общий сбой сервера", 2: "соединение запрещено правилами",
    3: "сеть недоступна", 4: "хост недоступен", 5: "в соединении отказано",
    6: "TTL истёк", 7: "команда не поддерживается", 8: "тип адреса не поддерживается",
}


def разобрать(url):
    """socks5[h]://логин:пароль@host:port -> (host, port, логин|None, пароль|None)."""
    p = urllib.parse.urlsplit(url or "")
    if p.scheme not in ("socks5", "socks5h") or not p.hostname or not p.port:
        raise ValueError("нужен socks5://[логин:пароль@]host:port")
    логин = urllib.parse.unquote(p.username) if p.username else None
    пароль = urllib.parse.unquote(p.password) if p.password else None
    return p.hostname, p.port, логин, пароль


def маска(url):
    """Для логов: пароль не печатается никогда."""
    try:
        host, port, логин, _ = разобрать(url)
    except ValueError:
        return "<негодный адрес прокси>"
    return "socks5://%s%s:%d" % ("%s:***@" % логин if логин else "", host, port)


def _читать(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ОшибкаSocks("прокси закрыл соединение посреди рукопожатия")
        buf += chunk
    return buf


def туннель(proxy_url, host, port, timeout=30):
    """TCP-сокет до host:port через SOCKS5-прокси."""
    phost, pport, логин, пароль = разобрать(proxy_url)
    sock = socket.create_connection((phost, pport), timeout=timeout)
    try:
        методы = b"\x00\x02" if логин is not None else b"\x00"
        sock.sendall(b"\x05" + bytes([len(методы)]) + методы)
        ver, метод = _читать(sock, 2)
        if ver != 5:
            raise ОшибкаSocks("это не SOCKS5-прокси (версия %d)" % ver)
        if метод == 2:
            if логин is None:
                raise ОшибкаSocks("прокси требует логин/пароль")
            л, п = логин.encode(), (пароль or "").encode()
            sock.sendall(b"\x01" + bytes([len(л)]) + л + bytes([len(п)]) + п)
            if _читать(sock, 2)[1] != 0:
                raise ОшибкаSocks("прокси отверг логин/пароль")
        elif метод != 0:
            raise ОшибкаSocks("прокси не принял ни один способ входа")
        h = host.encode("idna")
        sock.sendall(b"\x05\x01\x00\x03" + bytes([len(h)]) + h + struct.pack(">H", port))
        _, код, _, atyp = _читать(sock, 4)
        if код != 0:
            raise ОшибкаSocks("прокси: %s" % _ОТВЕТЫ.get(код, "код %d" % код))
        _читать(sock, {1: 4, 4: 16}.get(atyp) or _читать(sock, 1)[0])
        _читать(sock, 2)
        return sock
    except BaseException:
        sock.close()
        raise


def _класс_соединения(proxy_url):
    class HTTPSчерезSocks(http.client.HTTPSConnection):
        def connect(self):
            sock = туннель(proxy_url, self.host, self.port, self.timeout)
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
    return HTTPSчерезSocks


class SocksHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, proxy_url, context=None):
        разобрать(proxy_url)            # негодный адрес — отказ сразу, а не на запросе
        super().__init__(context=context or ssl.create_default_context())
        self._класс = _класс_соединения(proxy_url)

    def https_open(self, req):
        return self.do_open(self._класс, req, context=self._context)


class _ТолькоHTTPS(urllib.request.BaseHandler):
    """http:// через этот opener пошёл бы напрямую, мимо прокси. Отказ.
    handler_order меньше стандартного (500): иначе HTTPHandler успевает первым."""
    handler_order = 100

    def http_open(self, req):
        raise urllib.error.URLError("через SOCKS5-opener разрешён только https://")


def opener(proxy_url):
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), SocksHTTPSHandler(proxy_url), _ТолькоHTTPS())
