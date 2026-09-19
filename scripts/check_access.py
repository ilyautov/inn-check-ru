#!/usr/bin/env python3
"""
check_access.py — probe доступности источников inn-check-ru из текущей сети.

Читает sources.SOURCES[*].probe и sources.TLS_MINCIFRY_PROBES, бьёт все цели
параллельно (threads, таймаут 10 с на запрос) и классифицирует ответ по спеку
docs/superpowers/specs/2026-09-19-wave1-sources-registry-design.md, §4:

    доступен  — HTTP в probe.ok_http
    tls       — ssl.SSLCertVerificationError (нужен корень УЦ Минцифры: install_ca.py)
    гео       — таймаут, 403, 451, 503 (нужен РФ-IP или HTTPS_PROXY)
    dns       — socket.gaierror
    капча     — 200 + маркер капчи в теле
    ошибка    — всё остальное

Использование:
    python3 scripts/check_access.py [--json] [--no-cache] [--ttl <часы>] [--канарейки]

    --json       только JSON в stdout (таблица в stderr не печатается)
    --no-cache   не читать и не писать ~/.cache/inn-check-ru/access.json
    --ttl N      считать кэш свежим N часов (по умолчанию 24): если access.json
                 моложе — вывести его без сетевого прогона; --ttl 0 — всегда бить сеть
    --канарейки  дополнительно прогнать fetch_counterparty.FETCHERS на канареечных
                 ИНН из SOURCES[*].канарейка и сверить ожидаем_непустые (еженедельный CI)

Вывод: таблица в stderr (источник, состояние, http, мс, подсказка), JSON в stdout,
кэш ~/.cache/inn-check-ru/access.json ровно в формате §4:

    {"дата": "2026-09-19T14:00:00", "ip_класс": "не-РФ|РФ|неизвестно",
     "источники": {"егрюл": {"состояние": "доступен", "http": 307, "мс": 350,
                             "причина": null}}}

fetch_counterparty.py читает этот кэш (TTL 24 ч) и не дёргает источники в состоянии
гео|tls|dns. Код выхода: 0 всегда, кроме --канарейки с провалом (1).

TLS-контекст берётся из fetch_counterparty._build_ssl_context(), чтобы корень УЦ
Минцифры подхватывался одинаково; если импорт не удался — ssl.create_default_context()
с предупреждением в stderr. Прокси: urllib уважает HTTPS_PROXY из окружения.
Только стандартная библиотека.
"""

import concurrent.futures
import datetime as dt
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sources  # реестр: plain dict, без сетевых модулей

TIMEOUT = 10          # секунд на один probe-запрос
IP_TIMEOUT = 5        # секунд на определение ip_класса
DEFAULT_TTL_HOURS = 24
MAX_WORKERS = 16
BODY_LIMIT = 256 * 1024
CACHE_PATH = os.path.expanduser(os.path.join("~", ".cache", "inn-check-ru", "access.json"))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Коды, по которым источник закрыт для не-РФ IP / прокси (спек §4).
GEO_HTTP = (403, 451, 503)

# Маркеры капчи в HTML. Голое слово «captcha» намеренно не берём: на страницах ФНС оно
# встречается в именах полей формы (vyp3CaptchaToken) и без реального челленджа.
CAPTCHA_MARKERS_HTML = (
    "g-recaptcha", "smartcaptcha", "h-captcha", "hcaptcha.com", "cf-chl-", "cf_chl_",
    "challenge-platform", "captcha-form", "captcha_form", "id=\"captcha\"",
    "подтвердите, что вы не робот", "проверка, что вы не робот", "введите код с картинки",
)

HINTS = {
    "tls": "нужен корень УЦ Минцифры: python3 scripts/install_ca.py",
    "гео": "нужен РФ-IP или HTTPS_PROXY (urllib уважает переменную окружения)",
    "dns": "хост не резолвится — проверьте DNS/сеть (или HTTPS_PROXY с удалённым DNS)",
    "капча": "источник отдаёт капчу — сбор только через браузер",
}

# Сервисы определения страны исполнения (лёгкий GET, первый ответивший).
IP_SERVICES = (
    ("https://ipinfo.io/json", "country"),
    ("https://ipapi.co/json/", "country_code"),
)


# ---------------------------------------------------------------- TLS-контекст

def build_ssl_context():
    """Тот же контекст, что у сетевого движка (корень УЦ подхватывается одинаково)."""
    try:
        import fetch_counterparty  # может переписываться другим потоком
        return fetch_counterparty._build_ssl_context()
    except Exception as e:
        sys.stderr.write("ВНИМАНИЕ: fetch_counterparty._build_ssl_context недоступен (%s: %s) — "
                         "используется ssl.create_default_context()\n"
                         % (type(e).__name__, e))
        return ssl.create_default_context()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Редиректы не следуем: 307 от egrul — самостоятельный сигнал (ok_http его знает)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def make_requester(ctx=None):
    """requester(url, method, timeout) -> (status, body_text); транспортные ошибки — наружу."""
    ctx = ctx or build_ssl_context()
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx), _NoRedirect())

    def requester(url, method, timeout):
        req = urllib.request.Request(url, method=method, headers={
            "User-Agent": UA, "Accept": "application/json, text/html, */*",
            "Accept-Language": "ru-RU,ru;q=0.9",
        })
        try:
            with opener.open(req, timeout=timeout) as resp:
                body = resp.read(BODY_LIMIT)
                return resp.status, body.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            try:
                body = e.read(BODY_LIMIT).decode("utf-8", "replace")
            except Exception:
                body = ""
            return e.code, body
    return requester


# ---------------------------------------------------------------- цели и классификация

def probe_targets():
    """Список целей из реестра: SOURCES[*].probe + TLS_MINCIFRY_PROBES (ok_http [200])."""
    out = []
    for sid, d in sources.SOURCES.items():
        p = d.get("probe") or {}
        out.append({
            "id": sid,
            "url": p.get("url"),
            "method": (p.get("method") or "GET").upper(),
            "ok_http": list(p.get("ok_http") or [200]),
            "ожидаем": p.get("ожидаем") or "html",
            "группа": "источник",
        })
    for sid, url in sources.TLS_MINCIFRY_PROBES.items():
        out.append({"id": sid, "url": url, "method": "GET", "ok_http": [200],
                    "ожидаем": "html", "группа": "tls-хост"})
    return out


def _unwrap(exc):
    """URLError оборачивает транспортную ошибку в .reason — классифицируем по ней."""
    seen = 0
    while isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, BaseException) \
            and seen < 5:
        exc = exc.reason
        seen += 1
    return exc


def _is_timeout(exc):
    if isinstance(exc, (TimeoutError, socket.timeout, concurrent.futures.TimeoutError)):
        return True
    # хендшейк/чтение по TLS иногда отдают SSLError с текстом «timed out»
    return isinstance(exc, ssl.SSLError) and "timed out" in str(exc).lower()


def classify_exception(exc):
    """Исключение транспорта -> (состояние, причина)."""
    inner = _unwrap(exc)
    name = type(inner).__name__
    text = str(inner).strip() or str(exc).strip()
    reason = "%s: %s" % (name, text) if text else name
    if isinstance(inner, ssl.SSLCertVerificationError):
        return "tls", "tls: " + reason
    if _is_timeout(inner):
        return "гео", "гео: таймаут %d с (%s)" % (TIMEOUT, reason)
    if isinstance(inner, socket.gaierror):
        return "dns", "dns: " + reason
    return "ошибка", "ошибка: " + reason


def has_captcha(body, ожидаем):
    """200 + маркер капчи. Для json — только явный captchaRequired:true."""
    if not body:
        return False
    if ожидаем == "json":
        try:
            j = json.loads(body)
        except ValueError:
            j = None
        if isinstance(j, dict):
            return bool(j.get("captchaRequired"))
        return False
    low = body.lower()
    return any(m in low for m in CAPTCHA_MARKERS_HTML)


def classify_response(status, body, target):
    """HTTP-ответ -> (состояние, причина)."""
    if status in target["ok_http"]:
        if has_captcha(body, target.get("ожидаем")):
            return "капча", "капча: HTTP %d с маркером капчи в теле" % status
        return "доступен", None
    if status in GEO_HTTP:
        return "гео", "гео: HTTP %d" % status
    return "ошибка", "ошибка: HTTP %d" % status


def probe_one(target, requester, timeout=TIMEOUT):
    """Один probe: {"состояние", "http", "мс", "причина"} — строка кэша §4."""
    t0 = time.monotonic()
    try:
        status, body = requester(target["url"], target.get("method", "GET"), timeout)
    except Exception as e:
        state, reason = classify_exception(e)
        status = None
    else:
        state, reason = classify_response(status, body, target)
    ms = int((time.monotonic() - t0) * 1000)
    return {"состояние": state, "http": status, "мс": ms, "причина": reason}


def run_probes(targets, requester, timeout=TIMEOUT):
    """Параллельный обход; результат — список (target, строка) в порядке targets."""
    if not targets:
        return []
    workers = max(1, min(MAX_WORKERS, len(targets)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(probe_one, t, requester, timeout) for t in targets]
        rows = [f.result() for f in futures]
    return list(zip(targets, rows))


def hint_for(row, descriptor):
    """Подсказка для таблицы: что делать с этим состоянием."""
    state = row.get("состояние")
    if state in HINTS:
        return HINTS[state]
    if state == "ошибка":
        return row.get("причина") or ""
    требует = (descriptor or {}).get("требует")
    if требует == "браузер":
        return "хост отвечает; сбор — только браузер (probe проверяет лишь доступность)"
    if требует == "кэш":
        return "хост отвечает; данные — из локального кэша дампов"
    return ""


# ---------------------------------------------------------------- ip_класс

def detect_ip_class(requester, timeout=IP_TIMEOUT):
    """«РФ» | «не-РФ» | «неизвестно» по стране исполнения (первый ответивший сервис)."""
    for url, key in IP_SERVICES:
        try:
            status, body = requester(url, "GET", timeout)
            if status != 200:
                continue
            country = json.loads(body).get(key)
        except Exception:
            continue
        if isinstance(country, str) and country:
            return "РФ" if country.upper() == "RU" else "не-РФ"
    return "неизвестно"


# ---------------------------------------------------------------- отчёт и кэш

def _now_local():
    """Локальное время без смещения — как в примере §4 (2026-09-19T14:00:00)."""
    return dt.datetime.now(dt.timezone.utc).astimezone().replace(tzinfo=None)


def _to_naive_local(d):
    return d.astimezone().replace(tzinfo=None) if d.tzinfo is not None else d


def build_report(results, ip_class, now=None):
    """Ровно формат §4: {"дата", "ip_класс", "источники"}."""
    now = _to_naive_local(now or _now_local())
    return {
        "дата": now.isoformat(timespec="seconds"),
        "ip_класс": ip_class,
        "источники": {t["id"]: row for t, row in results},
    }


def is_fresh(cache, ttl_hours, now=None):
    """Кэш моложе TTL (и не из будущего). TTL 0 — всегда устарел."""
    if not isinstance(cache, dict) or ttl_hours <= 0:
        return False
    try:
        stamp = dt.datetime.fromisoformat(cache.get("дата"))
    except (TypeError, ValueError):
        return False
    now = _to_naive_local(now or _now_local())
    age = now - _to_naive_local(stamp)
    return dt.timedelta(0) <= age <= dt.timedelta(hours=ttl_hours)


def load_cache(path=CACHE_PATH, ttl_hours=DEFAULT_TTL_HOURS, now=None):
    """Кэш, если он есть, читается и свежий; иначе None."""
    try:
        with open(path, encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        return None
    return cache if is_fresh(cache, ttl_hours, now=now) else None


def write_cache(report, path=CACHE_PATH):
    os.makedirs(os.path.dirname(str(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
        f.write("\n")


def print_table(report, out=sys.stderr):
    src = report.get("источники") or {}
    header = ("источник", "состояние", "http", "мс", "подсказка")
    lines = [header]
    for sid, row in src.items():
        lines.append((sid, row.get("состояние") or "", str(row.get("http") or "—"),
                      str(row.get("мс") if row.get("мс") is not None else "—"),
                      hint_for(row, sources.SOURCES.get(sid))))
    widths = [max(len(str(line[i])) for line in lines) for i in range(4)]
    out.write("ip_класс: %s · дата: %s · целей: %d\n"
              % (report.get("ip_класс"), report.get("дата"), len(src)))
    for n, line in enumerate(lines):
        out.write("  ".join(str(line[i]).ljust(widths[i]) for i in range(4))
                  + "  " + line[4] + "\n")
        if n == 0:
            out.write("  ".join("-" * w for w in widths) + "  " + "-" * 9 + "\n")
    counts = {}
    for row in src.values():
        counts[row.get("состояние")] = counts.get(row.get("состояние"), 0) + 1
    out.write("итого: " + ", ".join("%s %d" % kv for kv in counts.items()) + "\n")


# ---------------------------------------------------------------- канарейки

def is_nonempty(v):
    """False/0/«нет» — явные значения признака (§1.2), пустые только None/''/[]/{}."""
    if v is None:
        return False
    if isinstance(v, (str, list, dict, tuple, set)):
        return len(v) > 0
    return True


def run_canaries(fc, registry):
    """Гоняет fc.FETCHERS на канареечных ИНН -> ({id: {инн, результат, причина}}, код выхода).

    FETCHERS может ещё отсутствовать (другой поток дописывает) — тогда все источники
    «не запускалась» с причиной «не проверено: FETCHERS отсутствует», код 0.
    """
    fetchers = getattr(fc, "FETCHERS", None)
    report = {}
    if not isinstance(fetchers, dict):
        for sid, d in registry.items():
            report[sid] = {"инн": (d.get("канарейка") or {}).get("инн"),
                           "результат": "не запускалась",
                           "причина": "не проверено: FETCHERS отсутствует"}
        return report, 0

    opener = None
    failed = 0
    for sid, d in registry.items():
        canary = d.get("канарейка")
        inn = (canary or {}).get("инн")
        if not canary or not inn:
            report[sid] = {"инн": None, "результат": "не запускалась",
                           "причина": "канарейка не задана в реестре"}
            continue
        fetcher = fetchers.get(sid)
        if fetcher is None:
            report[sid] = {"инн": inn, "результат": "не запускалась",
                           "причина": "нет fetcher'а в FETCHERS"}
            continue
        if opener is None:
            make = getattr(fc, "_make_opener", None)
            opener = make() if callable(make) else None
        try:
            data, _av = fetcher(opener, inn)
        except Exception as e:
            failed += 1
            report[sid] = {"инн": inn, "результат": "провал",
                           "причина": "%s: %s" % (type(e).__name__, e)}
            continue
        if not isinstance(data, dict):
            failed += 1
            report[sid] = {"инн": inn, "результат": "провал",
                           "причина": "fetcher вернул не dict (%s): %s"
                                      % (type(data).__name__, _av)}
            continue
        missing = [f for f in canary.get("ожидаем_непустые", []) if not is_nonempty(data.get(f))]
        if missing:
            failed += 1
            report[sid] = {"инн": inn, "результат": "провал",
                           "причина": "пустые поля: %s" % ", ".join(missing)}
        else:
            report[sid] = {"инн": inn, "результат": "ok", "причина": None}
    return report, (1 if failed else 0)


def print_canaries(report, out=sys.stderr):
    out.write("\nканарейки (FETCHERS на известных ИНН):\n")
    w = max(len(k) for k in report) if report else 8
    for sid, row in report.items():
        out.write("%s  %-12s  %-14s  %s\n" % (sid.ljust(w), row.get("инн") or "—",
                                              row.get("результат"), row.get("причина") or ""))


# ---------------------------------------------------------------- CLI

def _parse_args(argv):
    opts = {"json": False, "no_cache": False, "ttl": DEFAULT_TTL_HOURS, "canaries": False}
    it = iter(argv[1:])
    for a in it:
        if a == "--json":
            opts["json"] = True
        elif a == "--no-cache":
            opts["no_cache"] = True
        elif a == "--канарейки":
            opts["canaries"] = True
        elif a == "--ttl":
            try:
                opts["ttl"] = float(next(it))
            except (StopIteration, ValueError):
                sys.stderr.write("--ttl ожидает число часов\n")
                return None
        elif a.startswith("--ttl="):
            try:
                opts["ttl"] = float(a.split("=", 1)[1])
            except ValueError:
                sys.stderr.write("--ttl ожидает число часов\n")
                return None
        elif a in ("-h", "--help"):
            sys.stderr.write(__doc__)
            return None
        else:
            sys.stderr.write("неизвестный флаг: %s\n" % a)
            return None
    return opts


def main(argv):
    opts = _parse_args(argv)
    if opts is None:
        return 0
    quiet = opts["json"]

    report = None if opts["no_cache"] else load_cache(CACHE_PATH, opts["ttl"])
    if report is not None:
        if not quiet:
            sys.stderr.write("кэш %s свежий (TTL %g ч) — сетевой прогон пропущен; "
                             "--ttl 0 или --no-cache для живого прогона\n"
                             % (CACHE_PATH, opts["ttl"]))
    else:
        requester = make_requester()
        targets = probe_targets()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            ip_future = pool.submit(detect_ip_class, requester)
            results = run_probes(targets, requester)
            ip_class = ip_future.result()
        report = build_report(results, ip_class)
        if not opts["no_cache"]:
            try:
                write_cache(report, CACHE_PATH)
            except OSError as e:
                sys.stderr.write("ВНИМАНИЕ: кэш не записан (%s)\n" % e)

    if not quiet:
        print_table(report)

    code = 0
    out = dict(report)
    if opts["canaries"]:
        try:
            import fetch_counterparty
        except Exception as e:
            fc = None
            sys.stderr.write("не проверено: fetch_counterparty не импортируется (%s: %s)\n"
                             % (type(e).__name__, e))
        else:
            fc = fetch_counterparty
        if fc is None:
            canaries = {sid: {"инн": (d.get("канарейка") or {}).get("инн"),
                              "результат": "не запускалась",
                              "причина": "не проверено: fetch_counterparty не импортируется"}
                        for sid, d in sources.SOURCES.items()}
        else:
            canaries, code = run_canaries(fc, sources.SOURCES)
            if not hasattr(fc, "FETCHERS"):
                sys.stderr.write("не проверено: FETCHERS отсутствует\n")
        out["канарейки"] = canaries
        if not quiet:
            print_canaries(canaries)

    sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
