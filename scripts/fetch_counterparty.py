#!/usr/bin/env python3
"""
fetch_counterparty.py — проверка российского контрагента по ИНН через
бесплатные открытые JSON-эндпоинты ФНС. Только стандартная библиотека.

Использование:
    python3 fetch_counterparty.py <ИНН> [--save]

--save дополнительно сохраняет снимок в
~/.cache/inn-check-ru/snapshots/<ИНН>/<дата>_<время>.json — для мониторинга
изменений через diff_counterparty.py.

Вывод: единый JSON в stdout (UTF-8) со структурой:
    {
      "инн": "...",
      "тип":    "юрлицо|ип",  # по длине ИНН (10/12 знаков)
      "егрюл":   {...},   # карточка из ЕГРЮЛ/ЕГРИП (название, ОГРН, директор, адрес, статус)
      "риски":   {...},   # риск-флаги «Прозрачного бизнеса» (ИП — через mode=search-ip)
      "финансы": {...},   # бухотчётность из ГИР БО (выручка, прибыль, активы, строки)
      "мсп":     {...},   # Реестр МСП — ВЕРИФИЦИРОВАН живым запросом 19.09.2026
      "нпд":     {...},   # статус самозанятого (npd.nalog.ru check-status, офиц. API)
      "спецреестры": {...},  # service.nalog.ru: дисквалификация/задолженность/
                             # недостоверность — эндпоинты НЕ верифицированы
      "еркнм":   {...},   # плановые проверки — офлайн-сверка по кэшу дампов
      "рнп":     {...},   # недобросовестный поставщик — офлайн-сверка по кэшу
      "фссп":        {...},  # НЕ собирается: официальный API мёртв с 10.03.2022
      "суды":        {...},  # НЕ собирается скриптом — явная заглушка со ссылкой на каскад
      "банкротство": {...},  # НЕ собирается скриптом — явная заглушка со ссылкой на каскад
      "_доступность": {...}  # что реально отдал каждый источник из текущей среды
    }
Блоки фссп/суды/банкротство скрипт не покрывает (нужен браузер/токен) — они
возвращаются с явным статусом «не собрано», чтобы досье из трёх источников ФНС
не принималось за полное: долги и суды — ключевые deal-killer-сигналы каскада.

Три источника ФНС:
  1. ЕГРЮЛ        — egrul.nalog.ru     (требует РФ-IP/браузер, см. ниже)
  2. Прозрачный   — pb.nalog.ru        (id получаем, детали за captcha/JS-стеной)
     бизнес
  3. ГИР БО       — bo.nalog.gov.ru    (РАБОТАЕТ: финотчётность)

ВАЖНО про среду исполнения (проверено эмпирически из песочницы Cowork):
  - ГИР БО (bo.nalog.gov.ru)  — пробивается полностью, отдаёт финансы.
  - Прозрачный бизнес (pb)    — стартовый id отдаёт (captchaRequired:false),
                                но детальный результат поиска по id из
                                песочницы не вытягивается (server-side
                                ошибка / нужен браузерный JS-флоу или РФ-IP).
                                Флаги помечены как требующие добивки.
  - ЕГРЮЛ (egrul.nalog.ru)    — из песочницы недоступен (TCP timeout,
                                вероятно гео-/allowlist-блок). Заглушка с TODO.
  При запуске с российского IP в обычном окружении egrul и pb-детали
  должны отрабатывать (эндпоинты публичные, captcha не требуется по факту
  стартового ответа).

Без eval/shell. Defensive-парсинг: нет поля -> null, не краш.
"""

import http.cookiejar
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 25
RETRIES = 2
POLL_TRIES = 6
POLL_DELAY = 1.5
# Общий бюджет времени на весь скрипт (сек): без него при недоступной сети
# последовательный обход источников с поллингом висел бы 5+ минут.
DEADLINE = float(os.environ.get("COUNTERPARTY_DEADLINE", "120"))
_START = time.monotonic()
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " \
     "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"


class DeadlineExceeded(Exception):
    """Исчерпан общий бюджет времени COUNTERPARTY_DEADLINE."""


def _time_left():
    return DEADLINE - (time.monotonic() - _START)


def _build_ssl_context():
    """TLS-контекст по умолчанию ПРОВЕРЯЕТ сертификат (защита от MITM).

    Сайты ФНС иногда используют сертификаты Национального УЦ Минцифры,
    которых нет в системном хранилище. Для этого случая —
    переменная COUNTERPARTY_CA_BUNDLE с путём к доверенному CA-бандлу
    (например, russian_trusted_root_ca.cer), верификация остаётся включённой.

    Крайний случай — COUNTERPARTY_INSECURE=1 полностью отключает проверку.
    Делать так НЕ рекомендуется: открывает канал для подмены данных
    контрагента. Используйте только в изолированной отладке.
    """
    ctx = ssl.create_default_context()
    ca_bundle = os.environ.get("COUNTERPARTY_CA_BUNDLE")
    if ca_bundle:
        ctx.load_verify_locations(cafile=ca_bundle)
    if os.environ.get("COUNTERPARTY_INSECURE") == "1":
        sys.stderr.write(
            "ВНИМАНИЕ: COUNTERPARTY_INSECURE=1 — проверка TLS отключена, "
            "данные контрагента можно подменить. Используйте только для отладки.\n"
        )
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


_SSL = _build_ssl_context()


def _make_opener():
    cj = http.cookiejar.CookieJar()
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=_SSL),
        urllib.request.HTTPCookieProcessor(cj),
    )


def _http_get(opener, url, referer=None, accept="application/json, text/plain, */*"):
    headers = {"User-Agent": UA, "Accept": accept}
    if referer:
        headers["Referer"] = referer
        headers["X-Requested-With"] = "XMLHttpRequest"
    last_err = None
    for attempt in range(RETRIES + 1):
        left = _time_left()
        if left <= 0:
            raise DeadlineExceeded("бюджет времени %.0f с исчерпан" % DEADLINE)
        req = urllib.request.Request(url, headers=headers)
        try:
            resp = opener.open(req, timeout=min(TIMEOUT, max(1.0, left)))
            data = resp.read()
            return resp.status, data.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            # 429/5xx — временные (rate-limit/перегрузка): ретраим с backoff;
            # остальные коды (403, 404 и т.п.) ретраить бессмысленно.
            if e.code in (429, 500, 502, 503, 504) and attempt < RETRIES:
                last_err = e
                time.sleep(0.8 * (2 ** attempt))
                continue
            return e.code, ""
        except Exception as e:
            last_err = e
            time.sleep(0.8 * (2 ** attempt))
    raise last_err if last_err else RuntimeError("unknown http error")


def _safe_json(text):
    if not text:
        return None
    stripped = text.lstrip()
    if stripped.startswith("<"):
        return None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _g(obj, *path, default=None):
    cur = obj
    for key in path:
        if isinstance(cur, dict):
            cur = cur.get(key)
        elif isinstance(cur, (list, tuple)) and isinstance(key, int) \
                and -len(cur) <= key < len(cur):
            cur = cur[key]
        else:
            return default
        if cur is None:
            return default
    return cur if cur is not None else default


def fetch_egrul(opener, inn):
    base = "https://egrul.nalog.ru/"
    body = urllib.parse.urlencode({
        "vyp3CaptchaToken": "",
        "page": "",
        "query": str(inn),
        "region": "",
        "PreventChromeAutocomplete": "",
    }).encode("utf-8")
    headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": base,
    }
    left = _time_left()
    if left <= 0:
        return None, "пропущен: бюджет времени исчерпан (COUNTERPARTY_DEADLINE)"
    try:
        req = urllib.request.Request(base, data=body, headers=headers, method="POST")
        resp = opener.open(req, timeout=min(TIMEOUT, max(1.0, left)))
        post_json = _safe_json(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return None, "недоступен (HTTP %s на POST)" % e.code
    except Exception as e:
        return None, "недоступен (%s)" % type(e).__name__

    token = _g(post_json, "t")
    if not token:
        cap = _g(post_json, "captchaRequired")
        if cap:
            return None, "требуется captcha"
        return None, "POST не вернул token (структура изменилась)"

    result = None
    for _ in range(POLL_TRIES):
        if _time_left() <= POLL_DELAY:
            return None, "token получен, но бюджет времени исчерпан (COUNTERPARTY_DEADLINE)"
        time.sleep(POLL_DELAY)
        try:
            status, text = _http_get(
                opener,
                "https://egrul.nalog.ru/search-result/%s" % token,
                referer=base,
            )
        except Exception as e:
            return None, "недоступен на poll (%s)" % type(e).__name__
        j = _safe_json(text)
        rows = _g(j, "rows")
        if rows:
            result = rows[0] if isinstance(rows, list) else rows
            break

    if not result:
        return None, "token получен, карточка не пришла (пусто/таймаут)"

    card = {
        "наименование_полное": _g(result, "n"),
        "наименование_краткое": _g(result, "c"),
        "огрн": _g(result, "o"),
        "инн": _g(result, "i"),
        "кпп": _g(result, "p"),
        "адрес": _g(result, "a"),
        "руководитель": _g(result, "g"),
        "дата_регистрации": _g(result, "r"),
        "дата_прекращения": _g(result, "e"),
        "статус": _g(result, "k"),
        "вид": _g(result, "tp"),
    }
    return card, "ok"


def fetch_risks(opener, inn):
    # Прозрачный бизнес: двухшаговый флоу.
    # Шаг 1 — стартовый поиск. Рабочая минимальная форма (проверено на live):
    #   search-proc.json?mode=search-ul&queryUl=<ИНН>
    #   -> {"id":"<uuid>","captchaRequired":false}  (HTTP 200)
    # Лишний параметр text= и page/pageSize ломали запрос в HTTP 400
    # с pbSearchCaptcha. Их убрали.
    # Для 12-значного ИНН (ИП) — отдельный режим search-ip (дефект v1.3.1:
    # search-ul по ИП ничего не находил), по ИП отдаются спецрежим и ССЧ.
    inn_q = urllib.parse.quote(str(inn))
    mode, qparam = _pb_mode(inn)
    referer = "https://pb.nalog.ru/search.html"
    try:
        _http_get(opener, referer, accept="text/html,application/xhtml+xml,*/*")
    except Exception:
        pass

    search_url = (
        "https://pb.nalog.ru/search-proc.json"
        "?mode=%s&%s=%s" % (mode, qparam, inn_q)
    )
    try:
        status, text = _http_get(opener, search_url, referer=referer)
    except Exception as e:
        return _empty_risks(), "недоступен (%s)" % type(e).__name__

    j = _safe_json(text)
    if j is None:
        return _empty_risks(), "поиск вернул не-JSON (HTTP %s)" % status
    # Captcha может прийти и на стартовом шаге (зависит от IP/частоты).
    errors = _g(j, "ERRORS")
    if _g(j, "captchaRequired") is True or (errors and "pbSearchCaptcha" in errors):
        return _empty_risks(), "требуется captcha на стартовом запросе"
    search_id = _g(j, "id")
    if not search_id:
        return _empty_risks(), "поиск не вернул id (HTTP %s)" % status

    # Шаг 2 — забор результата по id через тот же search-proc.json,
    # но с mode=<mode>-result. Это реальный result-эндпоинт фронта pb.
    result_json = None
    last_status = None
    captcha_on_result = False
    result_url = (
        "https://pb.nalog.ru/search-proc.json"
        "?id=%s&method=get-response&mode=%s-result" % (search_id, mode)
    )
    for _ in range(POLL_TRIES):
        if _time_left() <= POLL_DELAY:
            break
        time.sleep(POLL_DELAY)
        try:
            status, text = _http_get(opener, result_url, referer=referer)
        except DeadlineExceeded:
            break
        except Exception:
            continue
        last_status = status
        rj = _safe_json(text)
        if rj is None:
            continue
        rerrors = _g(rj, "ERRORS")
        if _g(rj, "captchaRequired") is True or (
            rerrors and "pbSearchCaptcha" in rerrors
        ):
            captcha_on_result = True
            break
        # Готовый результат содержит блок ul/yul (юрлица) или ip (ИП) с данными.
        if _g(rj, "ul") is not None or _g(rj, "yul") is not None \
                or _g(rj, "ip") is not None:
            result_json = rj
            break

    if result_json is None:
        risks = _empty_risks()
        risks["_id_поиска"] = search_id
        if captcha_on_result:
            note = (
                "id получен (captchaRequired:false на старте), но шаг "
                "результата требует ввода капчи — нужен РФ-IP/браузерный "
                "флоу. Параметры запроса исправлены."
            )
        else:
            note = (
                "id получен, но результат не пришёл (HTTP %s/таймаут) — "
                "вероятно гео/частотный лимит. Параметры запроса исправлены, "
                "должно отрабатывать с РФ-IP." % last_status
            )
        return risks, note

    ul = (
        _g(result_json, "ul", "data", 0)
        or _g(result_json, "yul", "data", 0)
        or _g(result_json, "ul")
        or _g(result_json, "ip", "data", 0)
        or _g(result_json, "ip")
        or _g(result_json, "data", 0)
        or {}
    )
    risks = _map_pb_flags(ul)
    return risks, "ok"


def _empty_risks():
    return {
        "налоговая_задолженность": None,
        "дисквалификация_руководителя": None,
        "массовый_адрес": None,
        "массовый_руководитель": None,
        "недостоверность_сведений": None,
        "численность_сотрудников": None,
        "спецрежим": None,
        "среднесписочная_за_год": None,
    }


def _map_pb_flags(ul):
    r = _empty_risks()
    if not isinstance(ul, dict):
        return r
    r["налоговая_задолженность"] = _g(ul, "taxDebt") or _g(ul, "debt")
    r["дисквалификация_руководителя"] = _g(ul, "disqualified")
    r["массовый_адрес"] = _g(ul, "massAddress") or _g(ul, "addressMass")
    r["массовый_руководитель"] = _g(ul, "massHead") or _g(ul, "headMass")
    r["недостоверность_сведений"] = _g(ul, "invalid") or _g(ul, "unreliable")
    r["численность_сотрудников"] = _g(ul, "employeeCount") or _g(ul, "ssch")
    r["спецрежим"] = _g(ul, "taxMode") or _g(ul, "specialRegime")
    r["среднесписочная_за_год"] = _g(ul, "sschYear")
    return r


# Маркер честности для эндпоинтов, не прогнанных вживую.
NOTE_UNVERIFIED = "эндпоинт не верифицирован (разведка с не-РФ IP 19.09.2026)"

МСП_КАТЕГОРИИ = {1: "микропредприятие", 2: "малое предприятие",
                3: "среднее предприятие"}


def _parse_msp_row(row):
    """Разбор записи rmsp.nalog.ru search-proc.json. Формат верифицирован
    живым запросом 19.09.2026: inn, category (1/2/3), dtregistry, is_active,
    nptype (UL/IP), okved1, okved1name, cityname, od2_sschr."""
    if not isinstance(row, dict):
        return None
    return {
        "статус_мсп": ("в реестре" if row.get("is_active") == 1
                       else "исключена из реестра"),
        "категория": МСП_КАТЕГОРИИ.get(row.get("category"),
                                      row.get("category")),
        "код_категории": row.get("category"),
        "дата_включения": row.get("dtregistry"),
        "оквэд": row.get("okved1"),
        "оквэд_наименование": row.get("okved1name"),
        "город": row.get("cityname"),
        "численность": row.get("od2_sschr"),
        "тип": "ип" if row.get("nptype") == "IP" else "юрлицо",
    }


def fetch_msp(opener, inn):
    """Реестр МСП (rmsp.nalog.ru) — ВЕРИФИЦИРОВАН живым запросом 19.09.2026:
    search-proc.json?query=<ИНН> -> data[] (работает с любого IP)."""
    inn_q = urllib.parse.quote(str(inn))
    url = "https://rmsp.nalog.ru/search-proc.json?query=%s" % inn_q
    try:
        status, text = _http_get(opener, url, referer="https://rmsp.nalog.ru/")
    except Exception as e:
        return None, "недоступен (%s)" % type(e).__name__
    j = _safe_json(text)
    if j is None:
        return None, "ответ не-JSON (HTTP %s)" % status
    rows = j.get("data") or []
    if not isinstance(rows, list):
        rows = []
    row = None
    for item in rows:
        if isinstance(item, dict) and str(item.get("inn") or "") == str(inn):
            row = item
            break
    if row is None and rows:
        row = rows[0]
    if not isinstance(row, dict):
        return {"статус_мсп": "не в реестре",
                "пояснение": "не найдена в Едином реестре МСП (крупная "
                             "компания либо исключена) — для МСБ-контрагента "
                             "это стоп-сигнал"}, "ok: записи нет (не в реестре)"
    out = _parse_msp_row(row)
    if out is None:
        return None, "запись неожиданного формата"
    return out, "ok (верифицировано живым запросом 19.09.2026)"


def _parse_npd(j, inn):
    """Разбор ответа npd.nalog.ru check-status. Схема по официальному
    описанию API сервиса (npd.nalog.ru/check-status); живой прогон отложен —
    с не-РФ IP сервис отвечает 406/403 (проверено 19.09.2026)."""
    if not isinstance(j, dict):
        return None
    status = j.get("status")
    if status is None:
        status = j.get("самозанятый")
    return {
        "инн": j.get("inn", str(inn)),
        "статус_нпд": status,
        "сообщение": j.get("message"),
        "дата_проверки": j.get("date") or time.strftime("%d.%m.%Y"),
    }


def fetch_npd(opener, inn):
    """Статус самозанятого (плательщика НПД) — единственный сервис ФНС
    с официальным публичным API. Graceful: недоступен -> «не проверено»."""
    note = ("схема по официальному описанию API (npd.nalog.ru/check-status), "
            "живой прогон — с РФ-IP (с не-РФ IP 406/403, проверено 19.09.2026)")
    url = "https://npd.nalog.ru/api/v1/status"
    body = json.dumps({"inn": str(inn),
                       "date": time.strftime("%d.%m.%Y")}).encode("utf-8")
    headers = {"User-Agent": UA, "Accept": "application/json",
               "Content-Type": "application/json",
               "Referer": "https://npd.nalog.ru/check-status/"}
    try:
        req = urllib.request.Request(url, data=body, headers=headers,
                                     method="POST")
        resp = opener.open(req, timeout=min(TIMEOUT, max(1.0, _time_left())))
        j = _safe_json(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return None, "%s; HTTP %s" % (note, e.code)
    except Exception as e:
        return None, "%s; недоступен (%s)" % (note, type(e).__name__)
    out = _parse_npd(j, inn)
    if out is None:
        return None, "%s; ответ не-JSON" % note
    return out, "ok; " + note


def _load_registries_module():
    """registries_refresh.py через importlib (паттерн diff_counterparty)."""
    import importlib.util
    try:
        spec = importlib.util.spec_from_file_location(
            "registries_refresh",
            os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "registries_refresh.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


def fetch_registry_cache(реестр, inn):
    """Офлайн-сверка по локальному кэшу дампов (ЕРКНМ/РНП). Без кэша —
    честное «не проверено» с инструкцией; сеть здесь не дёргается."""
    mod = _load_registries_module()
    if mod is None:
        return {"статус": "не проверено",
                "причина": "registries_refresh.py не найден рядом со скриптом"}
    try:
        res, note = mod.lookup(реестр, inn)
    except Exception as e:
        return {"статус": "не проверено",
                "причина": "сбой сверки (%s)" % type(e).__name__}
    if res is None:
        return {"статус": "не проверено", "причина": note}
    res["статус"] = "проверено"
    return res


def _тип_контрагента(inn):
    return "ип" if len(str(inn)) == 12 else "юрлицо"


def _pb_mode(inn):
    """pb.nalog.ru разделяет поиск юрлиц и ИП разными mode (проверено на live
    для search-ul; search-ip — по той же схеме, пометка верификации в выводе)."""
    if _тип_контрагента(inn) == "ип":
        return "search-ip", "queryIp"
    return "search-ul", "queryUl"


def _service_nalog_query(opener, path, inn):
    """Спецреестры service.nalog.ru — двухшаговый флоу proc.json +
    search-result/{token} по образцу egrul.nalog.ru. НЕ ВЕРИФИЦИРОВАН."""
    base = "https://service.nalog.ru/%s/" % path
    body = urllib.parse.urlencode({
        "captcha": "",
        "captchaToken": "",
        "query": str(inn),
        "page": "1",
        "pageSize": "10",
    }).encode("utf-8")
    headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": base,
    }
    if _time_left() <= 0:
        return None, "пропущен: бюджет времени исчерпан (COUNTERPARTY_DEADLINE)"
    try:
        req = urllib.request.Request(base + "proc.json", data=body,
                                     headers=headers, method="POST")
        resp = opener.open(req, timeout=min(TIMEOUT, max(1.0, _time_left())))
        post_json = _safe_json(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return None, "недоступен (HTTP %s на POST)" % e.code
    except Exception as e:
        return None, "недоступен (%s)" % type(e).__name__

    token = _g(post_json, "t")
    if not token:
        return None, "POST не вернул token (структура изменилась)"

    for _ in range(POLL_TRIES):
        if _time_left() <= POLL_DELAY:
            return None, "token получен, бюджет времени исчерпан"
        time.sleep(POLL_DELAY)
        try:
            status, text = _http_get(
                opener, base + "search-result/%s" % token, referer=base)
        except Exception as e:
            return None, "недоступен на poll (%s)" % type(e).__name__
        j = _safe_json(text)
        if j is not None:
            return j, "ok"
    return None, "token получен, результат не пришёл"


def fetch_special_registries(opener, inn):
    """Спецреестры ФНС (service.nalog.ru): дисквалификация руководителя,
    налоговая задолженность >1000 ₽, недостоверность сведений ЕГРЮЛ.

    НЕ ВЕРИФИЦИРОВАНО (разведка с не-РФ IP 19.09.2026). Каждый подблок
    деградирует независимо: недоступен -> «не проверено» + причина.
    """
    checks = (
        ("дисквалификация_руководителя", "dismissal"),
        ("налоговая_задолженность", "zd"),
        ("недостоверность_сведений", "invalid"),
    )
    out, notes = {}, []
    for key, path in checks:
        data, av = _service_nalog_query(opener, path, inn)
        if data is None:
            out[key] = {"статус": "не проверено", "причина": av}
            notes.append("%s: %s" % (key, av))
        else:
            out[key] = {"статус": "проверено", "данные": data}
            notes.append("%s: ok" % key)
    return out, NOTE_UNVERIFIED + " | " + "; ".join(notes)


def fetch_finance(opener, inn):
    ref = "https://bo.nalog.gov.ru/"
    inn_q = urllib.parse.quote(str(inn))
    search_url = (
        "https://bo.nalog.gov.ru/advanced-search/organizations/search"
        "?query=%s&page=0" % inn_q
    )
    try:
        status, text = _http_get(opener, search_url, referer=ref)
    except Exception as e:
        return None, "недоступен (%s)" % type(e).__name__

    j = _safe_json(text)
    if j is None:
        return None, "поиск вернул не-JSON (HTTP %s)" % status

    content = _g(j, "content", default=[])
    if not content:
        # Эндпоинт и параметр (?query=<ИНН>&page=0) — рабочие: на ИНН,
        # который реально сдаёт отчётность, тот же запрос отдаёт content.
        # Пустой ответ = организация под этим ИНН не публикует отдельную
        # бухотчётность в ГИР БО: банк/страховщик/НПФ (своя форма ЦБ),
        # спецрежимник, бюджетник, КГН/консолидация на головную структуру,
        # ИП, либо отчётность ещё не загружена. Это факт данных, не баг кода.
        return None, ("в ГИР БО нет отдельной бухотчётности по этому ИНН "
                      "(банк/страховщик/спецрежим/КГН/ИП/нет публикации) — "
                      "это корректное поведение источника, не ошибка запроса")

    org = None
    for item in content:
        item_inn = (_strip_tags(_g(item, "inn")) or "")
        if item_inn == str(inn):
            org = item
            break
    if org is None:
        org = content[0]
    org_id = _g(org, "id")
    if org_id is None:
        return None, "найдена запись без id"

    finance = {
        "наименование": _strip_tags(_g(org, "shortName")),
        "огрн": _strip_tags(_g(org, "ogrn")),
        "инн": _strip_tags(_g(org, "inn")),
        "регион": _strip_tags(_g(org, "region")),
        "okved2": _strip_tags(_g(org, "okved2")),
        "id_гирбо": org_id,
        "отчётность_по_годам": [],
    }

    try:
        status, text = _http_get(
            opener, "https://bo.nalog.gov.ru/nbo/organizations/%s/bfo/" % org_id,
            referer=ref,
        )
        bfo_list = _safe_json(text) or []
    except Exception:
        bfo_list = []

    if not isinstance(bfo_list, list):
        bfo_list = []

    def _period_key(rec):
        try:
            return int(_g(rec, "period", default=0))
        except (TypeError, ValueError):
            return 0

    bfo_list = sorted(bfo_list, key=_period_key, reverse=True)[:3]

    for rec in bfo_list:
        year = {
            "год": _g(rec, "period"),
            "выручка": _g(rec, "gainSum"),         # из списка bfo (может быть null)
            "активы": _g(rec, "actives"),          # баланс, стр.1600
            "прибыль_убыток": None,                # чистая прибыль, стр.2400
            "дата_отчётности": _g(rec, "actualBfoDate"),
            "строки": {},                          # сырые строки баланса (для fin_scoring)
        }
        bfo_id = _g(rec, "id")
        if bfo_id is not None:
            detail = _fetch_financials_detail(opener, bfo_id, ref)
            # Добираем из детальной формы то, чего нет/null в списке.
            if year["прибыль_убыток"] is None:
                year["прибыль_убыток"] = detail.get("прибыль")
            if year["выручка"] is None:
                year["выручка"] = detail.get("выручка")
            if year["активы"] is None:
                year["активы"] = detail.get("активы")
            year["строки"] = detail.get("строки") or {}
        finance["отчётность_по_годам"].append(year)

    if not finance["отчётность_по_годам"]:
        return finance, "найдена организация, но список отчётностей пуст"
    return finance, "ok"


def _fetch_financials_detail(opener, bfo_id, ref):
    """Детальная форма бухотчётности из ГИР БО.

    Эндпоинт: /nbo/bfo/<bfo_id>/details -> список форм, форма[0] содержит
    блоки balance и financialResult (проверено на live, org 12482424):
      - financialResult.current2400 — чистая прибыль/убыток (стр.2400 ОФР)
      - financialResult.current2110 — выручка (стр.2110 ОФР)
      - balance.current1600         — итог актива баланса (стр.1600)
    Тысячи рублей. Возвращает dict с ключами прибыль/выручка/активы (или None).
    """
    out = {"прибыль": None, "выручка": None, "активы": None, "строки": {}}
    try:
        status, text = _http_get(
            opener, "https://bo.nalog.gov.ru/nbo/bfo/%s/details" % bfo_id,
            referer=ref,
        )
    except Exception:
        return out
    j = _safe_json(text)
    if not isinstance(j, list) or not j:
        return out
    form = j[0]
    out["прибыль"] = (
        _g(form, "financialResult", "current2400")
        or _g(form, "finresult", "current2400")
    )
    out["выручка"] = (
        _g(form, "financialResult", "current2110")
        or _g(form, "finresult", "current2110")
    )
    out["активы"] = (
        _g(form, "balance", "current1600")
        or _g(form, "balance", "current1700")
    )
    # Сырые строки баланса для fin_scoring.py (traceability): капитал и
    # резервы, оборотные активы, денежные средства, обязательства.
    for code in ("1300", "1200", "1250", "1400", "1500"):
        val = _g(form, "balance", "current%s" % code)
        if val is not None:
            out["строки"][code] = val
    return out


def _strip_tags(s):
    if not isinstance(s, str):
        return s
    return s.replace("<strong>", "").replace("</strong>", "").strip()


def _inn_checksum_ok(inn):
    """Контрольные числа ИНН по алгоритму ФНС — ловит опечатки до сетевых запросов."""
    def ctrl(digits, weights):
        return sum(int(d) * w for d, w in zip(digits, weights)) % 11 % 10
    if len(inn) == 10:
        return ctrl(inn, (2, 4, 10, 3, 5, 9, 4, 6, 8)) == int(inn[9])
    n11 = ctrl(inn, (7, 2, 4, 10, 3, 5, 9, 4, 6, 8))
    n12 = ctrl(inn, (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8))
    return n11 == int(inn[10]) and n12 == int(inn[11])


def validate_inn(inn):
    inn = str(inn).strip()
    if not inn.isdigit() or len(inn) not in (10, 12):
        return None
    if not _inn_checksum_ok(inn):
        return None
    return inn


def save_snapshot(inn, result):
    """Снимок проверки для мониторинга (diff_counterparty.py).

    ~/.cache/inn-check-ru/snapshots/<ИНН>/<дата>_<время>.json — по снимку на
    запуск; сравниваются два последних. Не падает: сбой записи — предупреждение
    в stderr, JSON в stdout всё равно уходит.
    """
    try:
        snap_dir = os.path.join(
            os.path.expanduser(os.path.join("~", ".cache", "inn-check-ru")),
            "snapshots", inn)
        os.makedirs(snap_dir, exist_ok=True)
        name = time.strftime("%Y-%m-%d_%H-%M-%S") + ".json"
        path = os.path.join(snap_dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
        sys.stderr.write("снимок сохранён: %s\n" % path)
        return path
    except OSError as e:
        sys.stderr.write("ВНИМАНИЕ: снимок не сохранён (%s)\n" % e)
        return None


def main(argv):
    args = [a for a in argv[1:] if a != "--save"]
    do_save = len(args) != len(argv) - 1
    if len(args) != 1:
        sys.stderr.write("Использование: python3 fetch_counterparty.py <ИНН> [--save]\n")
        return 2
    inn = validate_inn(args[0])
    if inn is None:
        out = {"ошибка": "Некорректный ИНН: ожидается 10 или 12 цифр с верным "
                         "контрольным числом (алгоритм ФНС) — вероятна опечатка",
               "ввод": args[0]}
        sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
        return 1

    opener = _make_opener()

    egrul_data, egrul_av = fetch_egrul(opener, inn)
    risks_data, risks_av = fetch_risks(opener, inn)
    fin_data, fin_av = fetch_finance(opener, inn)
    msp_data, msp_av = fetch_msp(opener, inn)
    spec_data, spec_av = fetch_special_registries(opener, inn)
    npd_data, npd_av = fetch_npd(opener, inn)
    # ЕРКНМ/РНП — офлайн-сверка по локальному кэшу дампов (сеть не дёргается)
    erknm_data = fetch_registry_cache("еркнм", inn)
    rnp_data = fetch_registry_cache("рнп", inn)

    # Блоки, которые скрипт НЕ собирает, но которые обязательны для решения о
    # сделке — возвращаем явно, чтобы досье не выглядело полным без них.
    # ФССП: официальный API (api-ip.fssp.gov.ru) отключён с 10.03.2022 и не
    # восстановлен — слой только браузерный; для ИП обязательно ДВА поиска
    # (ИП как юрлицо по ИНН + физлицо по ФИО+дата рождения+регион).
    ne_sobrano = ("не собрано скриптом — обязательный шаг каскада, "
                  "см. SKILL.md (браузер/агрегаторы)")
    fssp_note = ("официальный API ФССП отключён с 10.03.2022 — поиск вручную "
                 "на fssp.gov.ru или через агрегатор"
                 + ("; для ИП — ДВА поиска: как юрлицо по ИНН и как физлицо "
                    "по ФИО+дата рождения+регион"
                    if _тип_контрагента(inn) == "ип" else ""))
    result = {
        "инн": inn,
        "тип": _тип_контрагента(inn),
        "егрюл": egrul_data,
        "риски": risks_data,
        "финансы": fin_data,
        "мсп": msp_data if msp_data is not None
               else {"статус": "не проверено", "причина": msp_av},
        "нпд": npd_data if npd_data is not None
               else {"статус": "не проверено", "причина": npd_av},
        "спецреестры": spec_data,
        "еркнм": erknm_data,
        "рнп": rnp_data,
        "фссп": {"статус": ne_sobrano, "примечание": fssp_note,
                 "источник": "fssp.gov.ru — исполнительные производства (долги)"},
        "суды": {"статус": ne_sobrano,
                 "источник": "kad.arbitr.ru — картотека арбитражных дел"},
        "банкротство": {"статус": ne_sobrano,
                        "источник": "bankrot.fedresurs.ru — ЕФРСБ"
                        + (" (для ИП — включая внесудебное банкротство физлиц)"
                           if _тип_контрагента(inn) == "ип" else "")},
        "_доступность": {
            "егрюл": egrul_av,
            "риски": risks_av,
            "финансы": fin_av,
            "мсп": msp_av,
            "нпд": npd_av,
            "спецреестры": spec_av,
            "еркнм": erknm_data.get("статус") if isinstance(erknm_data, dict)
                     else "не проверено",
            "рнп": rnp_data.get("статус") if isinstance(rnp_data, dict)
                   else "не проверено",
            "фссп": "скриптом не покрыто (официальный API мёртв с 2022)",
            "суды": "скриптом не покрыто",
            "банкротство": "скриптом не покрыто",
        },
    }
    if do_save:
        save_snapshot(inn, result)
    sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
