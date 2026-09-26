#!/usr/bin/env python3
"""
run_access_eval.py — офлайн-eval probe-скрипта check_access.py (волна 1, поток B).

Сеть не используется: вместо реального HTTP-запроса подставляется фейковый
«requester», который отдаёт заданный код/тело или бросает заданное исключение.
Проверяется таблица классификации из спека §4, формат кэша ~/.cache/inn-check-ru/
access.json (ровно {"дата", "ip_класс", "сеть", "источники"}), TTL, пользовательский
прокси (§18.2: отметка сети, отказ вместо тихого прямого соединения) и --канарейки на
фейковом модуле fetch_counterparty (с FETCHERS и без). PASS/FAIL, чистый stdlib.
"""

import contextlib
import datetime as dt
import importlib.util
import io
import json
import os
import socket
import ssl
import sys
import tempfile
import types
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check(errors, cond, msg):
    if not cond:
        errors.append(msg)


def _target(sid="тест", ok_http=(200,), ожидаем="html", url="https://example.invalid/"):
    return {"id": sid, "url": url, "method": "GET", "ok_http": list(ok_http),
            "ожидаем": ожидаем, "группа": "источник"}


def _responder(status, body=""):
    """Фейковый requester: всегда отдаёт (status, body)."""
    def requester(url, method, timeout):
        return status, body
    return requester


def _raiser(exc):
    """Фейковый requester: всегда бросает exc."""
    def requester(url, method, timeout):
        raise exc
    return requester


def case_classifier(ca):
    """Таблица §4: доступен / tls / гео / dns / капча / ошибка."""
    errors = []
    rows = [
        # (описание, target, requester, ожидаемое состояние, ожидаемый http)
        ("200 в ok_http", _target(ok_http=(200,)), _responder(200, "<html>ok</html>"),
         "доступен", 200),
        ("307 в ok_http (egrul)", _target(ok_http=(200, 307)), _responder(307, ""),
         "доступен", 307),
        ("307 вне ok_http", _target(ok_http=(200,)), _responder(307, ""), "ошибка", 307),
        ("SSLCertVerificationError", _target(),
         _raiser(ssl.SSLCertVerificationError(1, "certificate verify failed")), "tls", None),
        ("SSLCertVerificationError внутри URLError", _target(),
         _raiser(urllib.error.URLError(
             ssl.SSLCertVerificationError(1, "certificate verify failed"))), "tls", None),
        ("TimeoutError", _target(), _raiser(TimeoutError("timed out")), "гео", None),
        ("socket.timeout (алиас TimeoutError с 3.10)", _target(),
         _raiser(socket.timeout("timed out")), "гео", None),  # noqa: UP041
        ("timeout внутри URLError", _target(),
         _raiser(urllib.error.URLError(TimeoutError("timed out"))), "гео", None),
        ("HTTP 403", _target(), _responder(403, "forbidden"), "гео", 403),
        ("HTTP 451", _target(), _responder(451, ""), "гео", 451),
        ("HTTP 503", _target(), _responder(503, ""), "гео", 503),
        ("socket.gaierror", _target(),
         _raiser(socket.gaierror(8, "nodename nor servname provided")), "dns", None),
        ("gaierror внутри URLError", _target(),
         _raiser(urllib.error.URLError(socket.gaierror(8, "no such host"))), "dns", None),
        ("200 + g-recaptcha в html", _target(),
         _responder(200, '<div class="g-recaptcha" data-sitekey="x"></div>'), "капча", 200),
        ("200 + smartcaptcha в html", _target(),
         _responder(200, "<script src=\"https://smartcaptcha.cloud.yandex.ru/captcha.js\">"),
         "капча", 200),
        ("200 + captchaRequired:true в json", _target(ожидаем="json"),
         _responder(200, '{"id":"abc","captchaRequired":true}'), "капча", 200),
        # pb.nalog.ru отдаёт captchaRequired:false — это НЕ капча
        ("200 + captchaRequired:false в json", _target(ожидаем="json"),
         _responder(200, '{"id":"abc","captchaRequired":false}'), "доступен", 200),
        ("HTTP 401 (bankrot)", _target(), _responder(401, ""), "ошибка", 401),
        ("HTTP 500", _target(), _responder(500, ""), "ошибка", 500),
        ("ConnectionResetError", _target(), _raiser(ConnectionResetError("reset")),
         "ошибка", None),
        ("прочий SSLError (не верификация)", _target(),
         _raiser(ssl.SSLError(1, "unexpected eof")), "ошибка", None),
    ]
    for name, target, requester, want_state, want_http in rows:
        res = ca.probe_one(target, requester=requester, timeout=1)
        check(errors, res.get("состояние") == want_state,
              "%s: состояние %r, ожидалось %r" % (name, res.get("состояние"), want_state))
        check(errors, res.get("http") == want_http,
              "%s: http %r, ожидалось %r" % (name, res.get("http"), want_http))
        check(errors, isinstance(res.get("мс"), int) and res["мс"] >= 0,
              "%s: мс должно быть int >= 0, есть %r" % (name, res.get("мс")))
        if want_state == "доступен":
            check(errors, res.get("причина") is None,
                  "%s: у доступного причина должна быть null, есть %r"
                  % (name, res.get("причина")))
        else:
            check(errors, isinstance(res.get("причина"), str) and res["причина"],
                  "%s: у недоступного причина должна быть непустой строкой, есть %r"
                  % (name, res.get("причина")))
    # исключение с точным именем класса — в причине, чтобы можно было грепать
    res = ca.probe_one(_target(), requester=_raiser(TimeoutError("timed out")), timeout=1)
    check(errors, "TimeoutError" in (res.get("причина") or ""),
          "причина таймаута не содержит имя класса: %r" % res.get("причина"))
    return errors


def case_hints(ca):
    errors = []
    tls = ca.hint_for({"состояние": "tls", "причина": "x"}, {"требует": "сеть"})
    check(errors, "install_ca.py" in tls, "подсказка tls без install_ca.py: %r" % tls)
    geo = ca.hint_for({"состояние": "гео", "причина": "x"}, {"требует": "сеть"})
    check(errors, "HTTPS_PROXY" in geo and "РФ" in geo,
          "подсказка гео без HTTPS_PROXY/РФ-IP: %r" % geo)
    ok = ca.hint_for({"состояние": "доступен", "причина": None}, {"требует": "браузер"})
    check(errors, "браузер" in ok,
          "для источника с требует=браузер подсказка должна напоминать про браузер: %r" % ok)
    return errors


def case_targets(ca, src):
    """Каждый дескриптор SOURCES и каждый TLS-хост даёт ровно одну цель."""
    errors = []
    targets = ca.probe_targets()
    want = len(src.SOURCES) + len(src.TLS_MINCIFRY_PROBES)
    check(errors, len(targets) == want,
          "целей %d, ожидалось %d" % (len(targets), want))
    ids = [t["id"] for t in targets]
    check(errors, len(ids) == len(set(ids)), "id целей не уникальны: %s" % ids)
    for t in targets:
        check(errors, t["url"].startswith("https://"), "%s: не https: %s" % (t["id"], t["url"]))
        check(errors, isinstance(t["ok_http"], list) and t["ok_http"],
              "%s: ok_http пуст" % t["id"])
        check(errors, t["ожидаем"] in ("html", "json"),
              "%s: ожидаем %r" % (t["id"], t["ожидаем"]))
    for sid in src.SOURCES:
        check(errors, sid in ids, "источник %s не попал в цели" % sid)
    for sid in src.TLS_MINCIFRY_PROBES:
        check(errors, sid in ids, "TLS-хост %s не попал в цели" % sid)
    # Referer из дескриптора доходит до requester'а (ЕФРСБ без него отвечает 403)
    ефрсб = [t for t in targets if t["id"] == "банкротство"]
    check(errors, ефрсб and ефрсб[0].get("referer") == "https://bankrot.fedresurs.ru/",
          "банкротство: probe без Referer: %r" % ефрсб)
    if ефрсб:
        принято = []

        def requester(url, method, timeout, referer=None, ua=None):
            принято.append((referer, ua))
            return 200, '{"pageData": [], "total": 0}'
        строка = ca.probe_one(ефрсб[0], requester)
        check(errors, принято == [("https://bankrot.fedresurs.ru/", src.UA_ПРОЕКТА)],
              "probe_one: Referer и UA проекта: %r" % принято)
        check(errors, строка["состояние"] == "доступен", "ЕФРСБ JSON: %r" % строка)
        for статус, тело, ждём in ((403, "", "антибот"), (429, "", "антибот"),
                                   (200, "<html>qrator</html>", "антибот"),
                                   (451, "", "гео")):
            строка = ca.probe_one(ефрсб[0], lambda *a, _с=статус, _т=тело, **k: (_с, _т))
            check(errors, строка["состояние"] == ждём,
                  "ЕФРСБ probe %d %r: %r, ждали %s" % (статус, тело[:10], строка, ждём))
        os.environ["INN_CHECK_BEZ_REFERER"] = "1"
        try:
            ids = [t["id"] for t in ca.probe_targets()]
        finally:
            os.environ.pop("INN_CHECK_BEZ_REFERER", None)
        check(errors, "банкротство" not in ids and "егрюл" in ids,
              "INN_CHECK_BEZ_REFERER=1: probe ЕФРСБ не должен уходить: %r" % ids)
    # и сам requester кладёт его в заголовок (а без referer — не кладёт)
    заголовки = []

    class Ответ:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=-1):
            return b"{}"

    class Opener:
        def open(self, req, timeout=None):
            заголовки.append((req.get_header("Referer"), req.get_header("User-agent")))
            return Ответ()
    orig = ca.urllib.request.build_opener
    ca.urllib.request.build_opener = lambda *a, **k: Opener()
    try:
        req = ca.make_requester(ctx=object())
        req("https://bankrot.fedresurs.ru/x", "GET", 5, referer="https://bankrot.fedresurs.ru/",
            ua=src.UA_ПРОЕКТА)
        req("https://egrul.nalog.ru/", "GET", 5)
    finally:
        ca.urllib.request.build_opener = orig
    check(errors, заголовки == [("https://bankrot.fedresurs.ru/", src.UA_ПРОЕКТА),
                                (None, ca.UA)],
          "requester: заголовки Referer/UA %r" % заголовки)
    return errors


def case_cache_format(ca):
    """Кэш ровно в формате §4 и переживает запись/чтение."""
    errors = []
    targets = [_target("а", ok_http=(200, 307)), _target("б"), _target("в")]

    def requester(url, method, timeout):
        return 200, "ok"

    results = ca.run_probes(targets, requester=requester, timeout=1)
    report = ca.build_report(results, ip_class="неизвестно")
    check(errors, set(report.keys()) == {"дата", "ip_класс", "сеть", "источники"},
          "ключи отчёта: %s" % sorted(report.keys()))
    check(errors, (report.get("сеть") or {}).get("отпечаток") == "прямое",
          "без прокси отчёт должен быть помечен «прямое»: %r" % report.get("сеть"))
    check(errors, report["ip_класс"] in ("РФ", "не-РФ", "неизвестно"),
          "ip_класс %r" % report["ip_класс"])
    try:
        dt.datetime.fromisoformat(report["дата"])
    except (TypeError, ValueError):
        errors.append("дата не ISO-8601: %r" % report.get("дата"))
    check(errors, "T" in report["дата"] and "." not in report["дата"],
          "дата должна быть в секундах вида 2026-09-19T14:00:00: %r" % report["дата"])
    check(errors, list(report["источники"].keys()) == ["а", "б", "в"],
          "порядок источников должен совпадать с порядком целей: %s"
          % list(report["источники"].keys()))
    for sid, row in report["источники"].items():
        check(errors, set(row.keys()) == {"состояние", "http", "мс", "причина"},
              "%s: ключи строки %s" % (sid, sorted(row.keys())))
        check(errors, row["состояние"] == "доступен" and row["http"] == 200
              and row["причина"] is None, "%s: строка %r" % (sid, row))
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "sub" / "access.json"
        ca.write_cache(report, path)
        check(errors, path.is_file(), "кэш не записан в %s" % path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        check(errors, raw == report, "кэш после записи не равен отчёту")
        check(errors, "\\u" not in path.read_text(encoding="utf-8"),
              "кэш должен быть в UTF-8 без \\u-экранирования")
        loaded = ca.load_cache(path, ttl_hours=24)
        check(errors, loaded == report, "load_cache не вернул свежий кэш")
        check(errors, ca.load_cache(Path(tmp) / "нет.json", ttl_hours=24) is None,
              "load_cache для отсутствующего файла должен дать None")
        (Path(tmp) / "битый.json").write_text("{не json", encoding="utf-8")
        check(errors, ca.load_cache(Path(tmp) / "битый.json", ttl_hours=24) is None,
              "load_cache для битого JSON должен дать None")
    return errors


def case_ttl(ca):
    errors = []
    now = dt.datetime.fromisoformat("2026-09-19T14:00:00")

    def rep(hours_ago):
        stamp = (now - dt.timedelta(hours=hours_ago)).isoformat(timespec="seconds")
        return {"дата": stamp, "ip_класс": "не-РФ", "источники": {}}

    check(errors, ca.is_fresh(rep(1), 24, now=now) is True, "1 ч при TTL 24 — свежий")
    check(errors, ca.is_fresh(rep(23.9), 24, now=now) is True, "23.9 ч при TTL 24 — свежий")
    check(errors, ca.is_fresh(rep(24.1), 24, now=now) is False, "24.1 ч при TTL 24 — устарел")
    check(errors, ca.is_fresh(rep(0), 0, now=now) is False, "TTL 0 — всегда устарел")
    check(errors, ca.is_fresh(rep(1), 2, now=now) is True, "1 ч при TTL 2 — свежий")
    check(errors, ca.is_fresh(rep(-5), 24, now=now) is False,
          "дата из будущего (сбитые часы) — не считать свежим")
    check(errors, ca.is_fresh({"дата": "вчера"}, 24, now=now) is False, "битая дата — устарел")
    check(errors, ca.is_fresh({}, 24, now=now) is False, "нет даты — устарел")
    check(errors, ca.is_fresh(None, 24, now=now) is False, "None — устарел")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "access.json"
        ca.write_cache(rep(30), path)
        check(errors, ca.load_cache(path, ttl_hours=24, now=now) is None,
              "load_cache обязан отбросить кэш старше TTL")
        check(errors, ca.load_cache(path, ttl_hours=48, now=now) is not None,
              "load_cache с TTL 48 обязан принять кэш возрастом 30 ч")
    return errors


def case_canaries(ca, src):
    """--канарейки на фейковом fetch_counterparty: без FETCHERS — «не проверено», код 0."""
    errors = []
    # 1) FETCHERS отсутствует (другой поток ещё дописывает) — не падать
    fc_old = types.SimpleNamespace(_make_opener=lambda: object())
    res, code = ca.run_canaries(fc_old, src.SOURCES)
    check(errors, code == 0, "без FETCHERS код выхода %r, ожидался 0" % code)
    check(errors, "FETCHERS отсутствует" in json.dumps(res, ensure_ascii=False),
          "без FETCHERS ожидалась пометка «не проверено: FETCHERS отсутствует»: %r" % res)

    # 2) FETCHERS есть: одна канарейка ok, одна — провал (пустое поле), одна — исключение
    canary_ids = [k for k, v in src.SOURCES.items() if v.get("канарейка")]
    check(errors, len(canary_ids) >= 3, "в SOURCES меньше трёх канареек: %s" % canary_ids)
    good, bad, boom = canary_ids[:3]

    def fetch_good(opener, inn):
        fields = src.SOURCES[good]["канарейка"]["ожидаем_непустые"]
        # False/0 — явные значения признаков (§1.2), считаются НЕпустыми
        return {f: (False if i == 0 else "x") for i, f in enumerate(fields)}, "ok"

    def fetch_bad(opener, inn):
        fields = src.SOURCES[bad]["канарейка"]["ожидаем_непустые"]
        data = dict.fromkeys(fields, "x")
        data[fields[-1]] = None
        return data, {"состояние": "ok", "причина": None}

    def fetch_boom(opener, inn):
        raise RuntimeError("сеть упала")

    fetchers = {good: fetch_good, bad: fetch_bad, boom: fetch_boom}
    fc_new = types.SimpleNamespace(FETCHERS=fetchers, _make_opener=lambda: object())
    res, code = ca.run_canaries(fc_new, src.SOURCES)
    check(errors, code == 1, "при провале канарейки код выхода %r, ожидался 1" % code)
    check(errors, res[good]["результат"] == "ok", "%s: %r" % (good, res[good]))
    check(errors, res[good]["инн"] == src.SOURCES[good]["канарейка"]["инн"],
          "%s: ИНН канарейки не пробросился" % good)
    check(errors, res[bad]["результат"] == "провал"
          and src.SOURCES[bad]["канарейка"]["ожидаем_непустые"][-1] in res[bad]["причина"],
          "%s: провал должен называть пустое поле: %r" % (bad, res[bad]))
    check(errors, res[boom]["результат"] == "провал" and "RuntimeError" in res[boom]["причина"],
          "%s: исключение fetcher'а должно стать провалом с именем класса: %r"
          % (boom, res[boom]))
    # источники без канарейки и без fetcher'а — «не запускалась», не провал
    for sid in src.SOURCES:
        if sid in fetchers:
            continue
        check(errors, res[sid]["результат"] == "не запускалась",
              "%s: без канарейки/fetcher ожидалось «не запускалась», есть %r"
              % (sid, res[sid]))
    check(errors, set(res.keys()) == set(src.SOURCES.keys()),
          "в отчёте канареек должны быть все источники")

    # 3) все канарейки ok -> код 0
    fc_ok = types.SimpleNamespace(FETCHERS={good: fetch_good}, _make_opener=lambda: object())
    res, code = ca.run_canaries(fc_ok, src.SOURCES)
    check(errors, code == 0, "все канарейки ok — код выхода %r" % code)
    return errors


def case_nonempty(ca):
    errors = []
    check(errors, ca.is_nonempty(False) and ca.is_nonempty(0) and ca.is_nonempty("нет"),
          "False/0/'нет' — явные значения, непустые (§1.2)")
    for v in (None, "", [], {}):
        check(errors, not ca.is_nonempty(v), "%r должно считаться пустым" % (v,))
    return errors


def _сеть(report):
    """report["сеть"] как словарь: отсутствие блока — тоже ответ, не падение."""
    return (report.get("сеть") or {}) if isinstance(report, dict) else {}


def case_proxy_marks_report(ca):
    """Отчёт помнит, через что снят: прямой и прокси-прогон не путаются."""
    errors = []
    targets = [_target("а")]
    results = ca.run_probes(targets, requester=_responder(200, "ok"), timeout=1)

    прямой = ca.build_report(results, ip_class="не-РФ")
    через = ca.build_report(results, ip_class="РФ",
                            конф=ca.proxy.настройка("http://u:p@rf.example:3128"))
    check(errors, _сеть(прямой).get("отпечаток") == "прямое",
          "прямой прогон: %r" % _сеть(прямой))
    check(errors, str(_сеть(через).get("отпечаток")).startswith("прокси:"),
          "прогон через прокси: %r" % _сеть(через))
    check(errors, _сеть(прямой).get("отпечаток") != _сеть(через).get("отпечаток"),
          "отпечатки прямого и прокси-прогона обязаны различаться")
    # Пароль в кэш не попадает: файл лежит в ~/.cache и читается чем угодно.
    сырое = json.dumps(через, ensure_ascii=False)
    check(errors, "p@rf" not in сырое and ":p@" not in сырое,
          "пароль прокси не должен попадать в отчёт: %r" % _сеть(через))
    check(errors, "rf.example" in сырое, "хост прокси стоит показать: %r" % _сеть(через))
    return errors


def case_proxy_hard_refusal(ca):
    """Негодный прокси — отказ, а не тихий уход напрямую."""
    errors = []
    for url, что in (("socks5://host:1080", "socks"),
                     ("ftp://host", "чужая схема"),
                     ("http://", "нет хоста")):
        конф = ca.proxy.настройка(url, env={})
        check(errors, конф["ошибка"], "%s (%s) должен быть отвергнут" % (url, что))
        check(errors, конф["url"] is None,
              "%s: при ошибке url обязан быть None, иначе пойдём через мусор" % url)
        check(errors, конф["отпечаток"] == "прямое",
              "%s: отпечаток при ошибке" % url)
    конф = ca.proxy.настройка("socks5://host:1080", env={})
    текст = конф["ошибка"] or ""
    check(errors, "socks" in текст and "http" in текст,
          "про socks надо сказать прямо и подсказать замену: %r" % конф["ошибка"])
    # Главное: код возврата, а не «ну ладно, сходим напрямую».
    буфер = io.StringIO()
    with contextlib.redirect_stdout(буфер):
        код = ca.main(["check_access.py", "--json", "--прокси", "socks5://host:1080"])
    check(errors, "ошибка" in буфер.getvalue(),
          "отказ должен быть виден в stdout: %r" % буфер.getvalue()[:120])
    check(errors, код == 2, "main с негодным прокси должен вернуть 2, вернул %r" % код)
    return errors


def case_proxy_cache_for_network(ca):
    """Кэш probe принадлежит той сети, из которой снят, — и только ей."""
    errors = []
    прямо = ca.proxy.настройка(None, env={})
    нода = ca.proxy.настройка("http://rf.example:3128", env={})
    прямой_кэш = {"дата": "2026-09-22T10:00:00", "ip_класс": "не-РФ",
                  "сеть": {"отпечаток": "прямое"}, "источники": {}}
    кэш_ноды = dict(прямой_кэш, ip_класс="РФ",
                    сеть={"отпечаток": нода["отпечаток"]})
    старый = {"дата": "2026-09-22T10:00:00", "ip_класс": "РФ", "источники": {}}

    отчёт, причина = ca.кэш_для_этой_сети(прямой_кэш, прямо)
    check(errors, отчёт is прямой_кэш and причина is None,
          "свой же кэш обязан читаться: %r" % причина)
    отчёт, причина = ca.кэш_для_этой_сети(прямой_кэш, нода)
    check(errors, отчёт is None and причина,
          "кэш прямого прогона не про прокси-прогон: %r" % отчёт)
    отчёт, причина = ca.кэш_для_этой_сети(кэш_ноды, прямо)
    check(errors, отчёт is None and причина,
          "кэш прокси-прогона не про прямой: %r" % отчёт)
    отчёт, причина = ca.кэш_для_этой_сети(кэш_ноды, нода)
    check(errors, отчёт is кэш_ноды, "кэш той же ноды обязан читаться")
    отчёт, причина = ca.кэш_для_этой_сети(старый, прямо)
    check(errors, отчёт is None and "не отмечена" in (причина or ""),
          "кэш без отметки сети: %r / %r" % (отчёт, причина))
    отчёт, причина = ca.кэш_для_этой_сети(None, прямо)
    check(errors, отчёт is None and причина is None,
          "отсутствие кэша — не повод для причины: %r" % причина)
    return errors


def case_proxy_precedence(ca):
    """--прокси > INN_CHECK_PROXY > HTTPS_PROXY, и видно, откуда взято."""
    errors = []
    env = {"INN_CHECK_PROXY": "http://свой:3128", "HTTPS_PROXY": "http://общий:8080"}
    конф = ca.proxy.настройка("http://флаг:1080", env=env)
    check(errors, конф["источник"] == "--прокси" and "флаг" in конф["url"],
          "флаг должен побеждать: %r" % конф)
    конф = ca.proxy.настройка(None, env=env)
    check(errors, конф["источник"] == "INN_CHECK_PROXY" and "свой" in конф["url"],
          "переменная проекта должна побеждать HTTPS_PROXY: %r" % конф)
    конф = ca.proxy.настройка(None, env={"HTTPS_PROXY": "http://общий:8080"})
    check(errors, конф["источник"] == "HTTPS_PROXY", "HTTPS_PROXY как запасной: %r" % конф)
    конф = ca.proxy.настройка(None, env={})
    check(errors, конф["url"] is None and конф["отпечаток"] == "прямое" and not конф["ошибка"],
          "без настройки — прямое соединение без ошибки: %r" % конф)
    # Пустая строка в окружении — это «не задано», а не «прокси ''».
    конф = ca.proxy.настройка(None, env={"INN_CHECK_PROXY": "  ", "HTTPS_PROXY": "http://о:1"})
    check(errors, конф["источник"] == "HTTPS_PROXY",
          "пустая переменная не должна перекрывать следующую: %r" % конф)
    return errors


def main():
    src = load_module("sources", ROOT / "scripts" / "sources.py")
    ca = load_module("check_access", ROOT / "scripts" / "check_access.py")
    cases = {
        "классификатор-§4": case_classifier(ca),
        "подсказки": case_hints(ca),
        "цели-из-реестра": case_targets(ca, src),
        "формат-кэша": case_cache_format(ca),
        "ttl": case_ttl(ca),
        "канарейки-офлайн": case_canaries(ca, src),
        "непустые-поля": case_nonempty(ca),
        "прокси-отмечен-в-отчёте": case_proxy_marks_report(ca),
        "прокси-негодный-это-отказ": case_proxy_hard_refusal(ca),
        "прокси-приоритет-источников": case_proxy_precedence(ca),
        "прокси-кэш-принадлежит-сети": case_proxy_cache_for_network(ca),
    }
    failed = 0
    for name, errs in cases.items():
        if errs:
            failed += 1
            print("FAIL %s" % name)
            for e in errs:
                print("  - %s" % e)
        else:
            print("PASS %s" % name)
    if failed:
        print("FAIL: %d/%d кейсов упало" % (failed, len(cases)))
        return 1
    print("PASS: все %d кейсов зелёные" % len(cases))
    return 0


if __name__ == "__main__":
    sys.exit(main())
