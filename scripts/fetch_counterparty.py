#!/usr/bin/env python3
"""
fetch_counterparty.py — проверка российского контрагента по ИНН через
бесплатные открытые JSON-эндпоинты ФНС. Только стандартная библиотека.

Использование:
    python3 fetch_counterparty.py <ИНН> [--save] [--профиль <id>]
    python3 fetch_counterparty.py --канарейки        # парсеры против канареечных ИНН

--save сохраняет снимок в ~/.cache/inn-check-ru/snapshots/<ИНН>/<дата>_<время>.json
(мониторинг через diff_counterparty.py). --профиль <id> собирает только источники
профиля (id — sources.PROFILE_IDS, по умолчанию «нейтрально»); остальные —
«не проверено», причина «профиль: не требуется для <id>».

Архитектура (спек docs/superpowers/specs/2026-09-19-wave1-sources-registry-design.md):
  * реестр источников — scripts/sources.py (SOURCES, порядок обхода = порядок ключей);
  * сетевой слой raw_<id>(opener, inn) -> сырой JSON, бросает SourceUnavailable
    с причиной-префиксом (сеть:/tls:/гео:/капча:/дедлайн:/схема:);
  * парсер parse_<id>(raw, inn) -> (данные, _доступность) — ЧИСТАЯ функция, её
    гоняет eval/run_fetch_eval.py офлайн против eval/fixtures/net/;
  * FETCHERS[id] = raw + parse; run_source() добавляет профиль, probe-кэш,
    канарейку и перевод исключений в «не проверено».

Три состояния блока (§1.1), _доступность[<id>]:
    {"состояние": "ok" | "пусто" | "не проверено", "причина": str|null,
     "канарейка": "ok" | "провал" | "не запускалась", "tier", "требует", "дата"}
  ok            — ответ пришёл, ВСЕ поля контракта на месте, запись по ИНН есть;
  пусто         — ответ пришёл, записи по ИНН нет (подтверждённый факт);
  не проверено  — всё остальное. Блок данных при «пусто»/«не проверено» — null.
Правило «схема изменилась»: нет поля контракта в сырой записи -> «не проверено»,
«схема: не найдено поле <имя>». Никогда «ok» с пустыми целевыми полями.

_итог_проверки (§1.3): сколько deal-killer-источников не проверено; проверка
«состоялась», если не проверено не больше половины из них.

Вывод: единый JSON в stdout (UTF-8):
    {"инн", "тип", "профиль",
     "егрюл", "риски", "финансы", "мсп", "нпд", "спецреестры", "еркнм", "рнп",
     "санкции", "фссп", "суды", "банкротство",      # блоки в порядке SOURCES
     "_доступность": {...}, "_итог_проверки": {...}}

Что реально отдают источники (живой снимок 19.09.2026, не-РФ IP — см. спек §0):
  egrul.nalog.ru search-result: c,g,cnt,i,k,n,o,p,r,t,pg,tot,rn — адреса (a),
    даты прекращения (e) и статуса НЕТ; k — вид субъекта (ul/fl), не статус.
  pb.nalog.ru search-ul-result ul.data[0]: inn, ogrn, namec, namep, sulst_ex,
    sulst_name_ex, pr_liq, invalid, okved2main(+name), dtreg, dtogrn, regionname,
    okopf12, predo, token. Долг/массовость/ССЧ/спецрежим лежат за token в
    company-proc.json (get-response -> 500 с не-РФ IP) -> подблок «детали».
  pb.nalog.ru search-ip-result ip.data[0]: ogrn, namec, inn, okved2main(+name),
    dtogrn, pr_sipst, predo, token — статуса/ликвидации/недостоверности НЕТ.
  bo.nalog.gov.ru: search -> content[] {id, inn, shortName, ogrn, region, okved2,
    statusCode}; bfo/ -> [{id, period, gainSum, actives, actualBfoDate}];
    details -> [{balance.current1600.., financialResult.current2110/2400}].
  rmsp.nalog.ru search-proc.json -> data[] {inn, category, is_active, nptype, ...}.

Без eval/shell. TLS-верификация не отключается (см. _build_ssl_context).
"""

import datetime as _dt
import http.cookiejar
import importlib.util
import json
import os
import socket
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

CACHE_DIR = os.path.expanduser(os.path.join("~", ".cache", "inn-check-ru"))
ACCESS_CACHE = os.path.join(CACHE_DIR, "access.json")   # пишет check_access.py (§4)
ACCESS_CACHE_TTL_H = 24
# Состояния probe, при которых сеть к источнику не дёргается вовсе.
PROBE_BLOCKING = ("гео", "tls", "dns")


def _load_sibling(name):
    """Модуль из scripts/ рядом с этим файлом (паттерн diff_counterparty)."""
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(os.path.dirname(os.path.abspath(__file__)), name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sources = _load_sibling("sources")
SOURCES = sources.SOURCES


class DeadlineExceeded(Exception):
    """Исчерпан общий бюджет времени COUNTERPARTY_DEADLINE."""


class SourceUnavailable(Exception):
    """Сетевой слой не смог получить сырой ответ. str(e) — причина с префиксом §1.1
    (сеть:/tls:/гео:/капча:/дедлайн:/схема:)."""


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
    if not ca_bundle:
        # scripts/install_ca.py кладёт корень Национального УЦ Минцифры сюда —
        # подхватываем автоматически, верификация остаётся включённой.
        cached = os.path.join(CACHE_DIR, "ca", "russian_trusted_bundle.pem")
        if os.path.isfile(cached):
            ca_bundle = cached
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


def _http_post(opener, url, body, headers):
    """POST с переводом ошибок в SourceUnavailable; возвращает текст ответа."""
    left = _time_left()
    if left <= 0:
        raise DeadlineExceeded("бюджет времени %.0f с исчерпан" % DEADLINE)
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        resp = opener.open(req, timeout=min(TIMEOUT, max(1.0, left)))
        return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise SourceUnavailable(_http_status_reason(e.code, "POST"))
    except Exception as e:
        raise SourceUnavailable(_classify_exc(e))


def _http_status_reason(status, step=""):
    """HTTP-код -> причина §1.1. 403/451/406 с не-РФ IP — геоблок."""
    suffix = (" на %s" % step) if step else ""
    if status in (403, 451, 406):
        return "гео: HTTP %s%s (вероятно блок не-РФ IP)" % (status, suffix)
    return "сеть: HTTP %s%s" % (status, suffix)


def _classify_exc(e):
    """Исключение сетевого слоя -> причина с машинно-различимым префиксом."""
    if isinstance(e, (SourceUnavailable, DeadlineExceeded)):
        s = str(e)
        return s if ":" in s.split(" ")[0] else "дедлайн: " + s
    if isinstance(e, ssl.SSLCertVerificationError):
        return ("tls: сертификат не проверен (%s) — корень УЦ Минцифры: "
                "scripts/install_ca.py" % (e.verify_message or "verify failed"))
    if isinstance(e, ssl.SSLError):
        return "tls: %s" % (e.reason or type(e).__name__)
    if isinstance(e, urllib.error.HTTPError):
        return _http_status_reason(e.code)
    if isinstance(e, urllib.error.URLError):
        return _classify_exc(e.reason) if isinstance(e.reason, Exception) \
            else "сеть: %s" % e.reason
    if isinstance(e, socket.gaierror):
        return "сеть: dns (%s)" % e
    if isinstance(e, (socket.timeout, TimeoutError)):
        return "сеть: таймаут"
    return "сеть: %s" % type(e).__name__


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


def _strip_tags(s):
    if not isinstance(s, str):
        return s
    return s.replace("<strong>", "").replace("</strong>", "").strip()


def _тип_контрагента(inn):
    return "ип" if len(str(inn)) == 12 else "юрлицо"


def _pb_mode(inn):
    """pb.nalog.ru разделяет поиск юрлиц и ИП разными mode (оба проверены живьём
    19.09.2026: search-ul на 7707083893, search-ip на 504110181262)."""
    if _тип_контрагента(inn) == "ип":
        return "search-ip", "queryIp"
    return "search-ul", "queryUl"


# ---------------------------------------------------------------------------
# Три состояния: _доступность, контракт, сырая запись
# ---------------------------------------------------------------------------

def _av(source_id, состояние, причина=None, канарейка="не запускалась"):
    """Словарь _доступность фиксированной формы (§1.1)."""
    d = SOURCES.get(source_id, {})
    return {
        "состояние": состояние,
        "причина": причина,
        "канарейка": канарейка,
        "tier": d.get("tier", "⚪"),
        "требует": d.get("требует", "сеть"),
        "дата": time.strftime("%Y-%m-%d"),
    }


def _not_checked(source_id, причина):
    return None, _av(source_id, "не проверено", причина)


def contract_for(source_id, inn):
    """Поля сырой записи, без которых блок не может быть «ok». Для ИП у источника
    может быть отдельный контракт (контракт_ип) — ответ по ИП имеет другую форму."""
    d = SOURCES.get(source_id, {})
    if _тип_контрагента(inn) == "ип" and d.get("контракт_ип") is not None:
        return list(d["контракт_ип"])
    return list(d.get("контракт") or [])


def _schema_missing(source_id, inn, record):
    """Имя первого отсутствующего поля контракта или None."""
    for field in contract_for(source_id, inn):
        if field not in record:
            return field
    return None


def _canary_for(source_id, inn):
    d = SOURCES.get(source_id, {})
    if _тип_контрагента(inn) == "ип" and d.get("канарейка_ип"):
        return d["канарейка_ип"]
    return d.get("канарейка")


def _pick_by_inn(rows, inn, inn_key):
    """Запись с нужным ИНН из списка (иначе первая); None если список пуст."""
    if not isinstance(rows, list):
        return None
    dict_rows = [r for r in rows if isinstance(r, dict)]
    for r in dict_rows:
        if _strip_tags(str(r.get(inn_key) or "")) == str(inn):
            return r
    return dict_rows[0] if dict_rows else None


def _rec_egrul(raw, inn):
    return _pick_by_inn(_g(raw, "rows"), inn, "i")


def _rec_risks(raw, inn):
    if not isinstance(raw, dict):
        return None
    rows = _g(raw, "ul", "data")
    if rows is None:
        rows = _g(raw, "ip", "data")
    return _pick_by_inn(rows, inn, "inn")


def _rec_finance(raw, inn):
    return _pick_by_inn(_g(raw, "search", "content"), inn, "inn")


def _rec_msp(raw, inn):
    return _pick_by_inn(_g(raw, "data"), inn, "inn")


def _rec_npd(raw, inn):
    return raw if isinstance(raw, dict) else None


# id источника -> как из сырого ответа достать запись по ИНН (eval дрейфа схемы
# удаляет из неё поля контракта по очереди).
RAW_RECORD = {
    "егрюл": _rec_egrul,
    "риски": _rec_risks,
    "финансы": _rec_finance,
    "мсп": _rec_msp,
    "нпд": _rec_npd,
}


# ---------------------------------------------------------------------------
# ЕГРЮЛ — egrul.nalog.ru
# ---------------------------------------------------------------------------

EGRUL_BASE = "https://egrul.nalog.ru/"


def raw_egrul(opener, inn):
    """POST -> {t: token} -> поллинг search-result/<t> -> {rows: [...]}."""
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
        "Referer": EGRUL_BASE,
    }
    post_json = _safe_json(_http_post(opener, EGRUL_BASE, body, headers))
    if post_json is None:
        raise SourceUnavailable("схема: POST egrul вернул не-JSON")
    token = _g(post_json, "t")
    if not token:
        if _g(post_json, "captchaRequired"):
            raise SourceUnavailable("капча: egrul требует captcha на POST")
        raise SourceUnavailable("схема: POST egrul не вернул token (t)")

    last = None
    for _ in range(POLL_TRIES):
        if _time_left() <= POLL_DELAY:
            raise SourceUnavailable("дедлайн: token получен, карточка не успела прийти")
        time.sleep(POLL_DELAY)
        try:
            status, text = _http_get(opener, EGRUL_BASE + "search-result/%s" % token,
                                     referer=EGRUL_BASE)
        except Exception as e:
            raise SourceUnavailable(_classify_exc(e))
        if status != 200:
            raise SourceUnavailable(_http_status_reason(status, "search-result"))
        j = _safe_json(text)
        if isinstance(j, dict) and isinstance(j.get("rows"), list):
            return j          # rows может быть пустым — это «пусто», решает парсер
        last = j
    raise SourceUnavailable("дедлайн: search-result не отдал rows за %d попыток (%s)"
                            % (POLL_TRIES, "не-JSON" if last is None else "ожидание"))


def parse_egrul(raw, inn):
    """Сырой {rows: [...]} -> карточка. Контракт: n, o, i, g, r, k (§0)."""
    if not isinstance(raw, dict):
        return _not_checked("егрюл", "схема: ответ не JSON-объект")
    if not isinstance(raw.get("rows"), list):
        return _not_checked("егрюл", "схема: не найдено поле rows")
    rec = _rec_egrul(raw, inn)
    if rec is None:
        return None, _av("егрюл", "пусто", "в ЕГРЮЛ/ЕГРИП нет записи по этому ИНН")
    missing = _schema_missing("егрюл", inn, rec)
    if missing:
        return _not_checked("егрюл", "схема: не найдено поле %s" % missing)
    region = rec.get("rn")
    card = {
        "наименование_полное": rec.get("n"),
        "наименование_краткое": rec.get("c"),
        "огрн": rec.get("o"),
        "инн": rec.get("i"),
        "кпп": rec.get("p"),
        "руководитель": rec.get("g"),
        "дата_регистрации": rec.get("r"),
        "вид": rec.get("k"),                 # ul / fl — вид субъекта, НЕ статус
        "регион": region,
        # Поля, которых egrul search-result не отдаёт (снято 19.09.2026): явный null
        # с пометкой источника (§1.2), а не тихий null из несуществующих a/e/tp.
        "адрес": None,
        "адрес_источник": "не отдаётся egrul search-result; регион: %s" % region,
        "статус": None,
        "статус_источник": "не отдаётся egrul search-result (k — вид субъекта, не статус); "
                           "берётся из pb: риски.статус",
        "дата_прекращения": None,
        "дата_прекращения_источник": "не отдаётся egrul search-result; "
                                     "см. риски.ликвидация и риски.статус",
    }
    return card, _av("егрюл", "ok")


def fetch_egrul(opener, inn):
    return parse_egrul(raw_egrul(opener, inn), inn)


# ---------------------------------------------------------------------------
# Прозрачный бизнес — pb.nalog.ru
# ---------------------------------------------------------------------------

PB_REFERER = "https://pb.nalog.ru/search.html"
# Флаги, которые поисковый ответ pb НЕ отдаёт: они за token в company-proc.json.
PB_DETAIL_FIELDS = [
    "налоговая_задолженность", "дисквалификация_руководителя", "массовый_адрес",
    "массовый_руководитель", "численность_сотрудников", "спецрежим",
    "среднесписочная_за_год",
]
PB_DETAIL_NOT_COVERED = ("не покрыто: детальный эндпоинт company-proc.json отдаёт 500 "
                         "с не-РФ IP; token сохранён")


def _pb_captcha(j):
    errors = _g(j, "ERRORS")
    return _g(j, "captchaRequired") is True or bool(
        isinstance(errors, dict) and "pbSearchCaptcha" in errors)


def raw_risks(opener, inn):
    """Двухшаговый флоу pb: GET referer (cookie) -> search-proc.json?mode=search-ul
    &queryUl=<ИНН> -> {id} -> поллинг ?id=&method=get-response&mode=search-ul-result.
    Для ИП — search-ip/queryIp (дефект v1.3.1: search-ul по ИП ничего не находил)."""
    mode, qparam = _pb_mode(inn)
    try:
        _http_get(opener, PB_REFERER, accept="text/html,application/xhtml+xml,*/*")
    except Exception:
        pass  # cookie желательна, но не обязательна

    search_url = "https://pb.nalog.ru/search-proc.json?mode=%s&%s=%s" % (
        mode, qparam, urllib.parse.quote(str(inn)))
    try:
        status, text = _http_get(opener, search_url, referer=PB_REFERER)
    except Exception as e:
        raise SourceUnavailable(_classify_exc(e))
    if status != 200:
        raise SourceUnavailable(_http_status_reason(status, "search-proc"))
    j = _safe_json(text)
    if j is None:
        raise SourceUnavailable("схема: поиск pb вернул не-JSON")
    if _pb_captcha(j):
        raise SourceUnavailable("капча: pb требует captcha на стартовом запросе")
    search_id = _g(j, "id")
    if not search_id:
        raise SourceUnavailable("схема: поиск pb не вернул id")

    result_url = ("https://pb.nalog.ru/search-proc.json?id=%s&method=get-response"
                  "&mode=%s-result" % (search_id, mode))
    last_status = None
    for _ in range(POLL_TRIES):
        if _time_left() <= POLL_DELAY:
            break
        time.sleep(POLL_DELAY)
        try:
            status, text = _http_get(opener, result_url, referer=PB_REFERER)
        except DeadlineExceeded:
            break
        except Exception:
            continue
        last_status = status
        rj = _safe_json(text)
        if rj is None:
            continue
        if _pb_captcha(rj):
            raise SourceUnavailable("капча: pb требует captcha на шаге результата")
        if "ul" in rj or "ip" in rj:
            return rj
    if _time_left() <= POLL_DELAY:
        raise SourceUnavailable("дедлайн: id pb получен, результат не успел прийти")
    raise SourceUnavailable(
        "сеть: id pb получен, результат не пришёл (HTTP %s) — гео/частотный лимит"
        % last_status)


def parse_risks(raw, inn):
    """Сырой результат pb -> блок «риски». Парсится ТО, ЧТО РЕАЛЬНО ПРИХОДИТ в
    ul.data[0] (юрлицо) / ip.data[0] (ИП); никаких угаданных имён полей.
    Детальные флаги (долг, массовость, ССЧ, спецрежим) — подблок «детали»."""
    if not isinstance(raw, dict):
        return _not_checked("риски", "схема: ответ не JSON-объект")
    if _pb_captcha(raw):
        return _not_checked("риски", "капча: pb требует captcha")
    if "ul" not in raw and "ip" not in raw:
        return _not_checked("риски", "схема: не найдено поле ul/ip")
    block = raw.get("ul") if "ul" in raw else raw.get("ip")
    if not isinstance(_g(block, "data"), list):
        return _not_checked("риски", "схема: не найдено поле data")
    rec = _rec_risks(raw, inn)
    if rec is None:
        return None, _av("риски", "пусто", "pb.nalog.ru: записи по этому ИНН нет")
    missing = _schema_missing("риски", inn, rec)
    if missing:
        return _not_checked("риски", "схема: не найдено поле %s" % missing)

    details = {"статус": "не проверено", "причина": PB_DETAIL_NOT_COVERED,
               "поля": list(PB_DETAIL_FIELDS)}
    if _тип_контрагента(inn) == "ип":
        data = {
            "наименование": rec.get("namec"),
            "огрн": rec.get("ogrn"),
            "статус": None,
            "статус_источник": "search-ip не отдаёт статус, ликвидацию и недостоверность "
                               "(нет sulst_name_ex/pr_liq/invalid; снято 19.09.2026)",
            "ликвидация": None,
            "недостоверность_сведений": None,
            "оквэд": rec.get("okved2main"),
            "оквэд_наименование": rec.get("okved2mainname"),
            "дата_огрн": rec.get("dtogrn"),
            "pr_sipst": rec.get("pr_sipst"),
            "pr_sipst_примечание": "сырое поле search-ip, семантика ФНС не подтверждена — "
                                   "не интерпретируется",
            "детали": details,
            "_token_pb": rec.get("token"),
        }
        return data, _av("риски", "ok")

    data = {
        "наименование": rec.get("namep") or rec.get("namec"),
        "статус": rec.get("sulst_name_ex"),
        "код_статуса": rec.get("sulst_ex"),
        "ликвидация": _flag(rec.get("pr_liq")),
        "недостоверность_сведений": _flag(rec.get("invalid")),
        "оквэд": rec.get("okved2main"),
        "оквэд_наименование": rec.get("okved2mainname"),
        "дата_регистрации": rec.get("dtreg"),
        "дата_огрн": rec.get("dtogrn"),
        "регион": rec.get("regionname"),
        "окопф": rec.get("okopf12"),
        "детали": details,
        "_token_pb": rec.get("token"),
    }
    return data, _av("риски", "ok")


def _flag(v):
    """pb отдаёт признаки строками '0'/'1' -> bool; иные значения — как есть."""
    if v in ("1", 1, True):
        return True
    if v in ("0", 0, False):
        return False
    return v


def fetch_risks(opener, inn):
    return parse_risks(raw_risks(opener, inn), inn)


def _empty_risks():
    return {k: None for k in PB_DETAIL_FIELDS + ["недостоверность_сведений"]}


def _map_pb_flags(ul):
    """Устаревший маппер детальных флагов по именам из eval-фикстуры
    pb_ip_result.json (taxDebt, specialRegime, ssch, ...). В живых ответах
    search-ul/search-ip 19.09.2026 этих полей НЕТ, поэтому в блок «риски» он не
    подключён (см. PB_DETAIL_NOT_COVERED); сохранён для eval/run_registries_eval.py
    и для реализации company-proc.json после прогона с РФ-IP."""
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


# ---------------------------------------------------------------------------
# ГИР БО — bo.nalog.gov.ru
# ---------------------------------------------------------------------------

BO_REF = "https://bo.nalog.gov.ru/"
BO_YEARS = 3
# Семантика gainSum (проверено 19.09.2026 на 5036045205 и 2309085638): gainSum в
# списке bfo/ РАВЕН стр.2110 (current2110) детальной формы того же периода во всех
# 10 сверенных годах. Одинаковый gainSum 2309085638 за 2025 и 2024 (412 062) — это
# одинаковая выручка в самих формах (previous2110 формы 2025 = 412 062), а не дефект
# поля. Приоритет всё равно у детальной формы: она первичный документ, gainSum —
# производная витрины и запасной вариант, когда details не пришли.
FIN_FROM_2110 = "стр.2110 детальной формы (gainSum списка совпал)"
FIN_FROM_2110_DIFF = "стр.2110 детальной формы (gainSum списка = %s расходится)"
FIN_FROM_GAIN = "gainSum списка bfo (детальная форма не получена)"
FIN_EMPTY_REASON = ("в ГИР БО нет отдельной бухотчётности по этому ИНН "
                    "(банк/страховщик/спецрежим/КГН/ИП/нет публикации) — "
                    "это корректное поведение источника, не ошибка запроса")


def raw_finance(opener, inn):
    """search -> bfo/ -> details для последних BO_YEARS периодов.
    Возвращает {"search": ..., "bfo": [...], "details": {bfo_id: [...]}}."""
    search_url = ("https://bo.nalog.gov.ru/advanced-search/organizations/search"
                  "?query=%s&page=0" % urllib.parse.quote(str(inn)))
    try:
        status, text = _http_get(opener, search_url, referer=BO_REF)
    except Exception as e:
        raise SourceUnavailable(_classify_exc(e))
    if status != 200:
        raise SourceUnavailable(_http_status_reason(status, "search"))
    search = _safe_json(text)
    if search is None:
        raise SourceUnavailable("схема: поиск ГИР БО вернул не-JSON")
    raw = {"search": search, "bfo": [], "details": {}}

    org = _rec_finance(raw, inn)
    org_id = _g(org, "id")
    if org_id is None:
        return raw          # пусто / схема — решает парсер
    try:
        status, text = _http_get(
            opener, "https://bo.nalog.gov.ru/nbo/organizations/%s/bfo/" % org_id,
            referer=BO_REF)
        bfo = _safe_json(text) if status == 200 else None
    except Exception:
        bfo = None
    if not isinstance(bfo, list):
        return raw
    raw["bfo"] = bfo
    for rec in _top_periods(bfo):
        bfo_id = _g(rec, "id")
        if bfo_id is None or _time_left() <= 2:
            continue
        try:
            status, text = _http_get(
                opener, "https://bo.nalog.gov.ru/nbo/bfo/%s/details" % bfo_id,
                referer=BO_REF)
            detail = _safe_json(text) if status == 200 else None
        except Exception:
            detail = None
        if detail is not None:
            raw["details"][str(bfo_id)] = detail
    return raw


def _top_periods(bfo):
    def key(rec):
        try:
            return int(_g(rec, "period", default=0))
        except (TypeError, ValueError):
            return 0
    return sorted([r for r in bfo if isinstance(r, dict)], key=key, reverse=True)[:BO_YEARS]


def parse_finance(raw, inn):
    """{"search", "bfo", "details"} -> блок «финансы». Контракт по записи search:
    id, inn, shortName. Тысячи рублей (как в источнике)."""
    if not isinstance(raw, dict):
        return _not_checked("финансы", "схема: ответ не JSON-объект")
    search = raw.get("search")
    if not isinstance(search, dict):
        return _not_checked("финансы", "схема: не найдено поле search")
    if not isinstance(search.get("content"), list):
        return _not_checked("финансы", "схема: не найдено поле content")
    org = _rec_finance(raw, inn)
    if org is None:
        return None, _av("финансы", "пусто", FIN_EMPTY_REASON)
    missing = _schema_missing("финансы", inn, org)
    if missing:
        return _not_checked("финансы", "схема: не найдено поле %s" % missing)

    finance = {
        "наименование": _strip_tags(org.get("shortName")),
        "огрн": _strip_tags(org.get("ogrn")),
        "инн": _strip_tags(org.get("inn")),
        "регион": _strip_tags(org.get("region")),
        "okved2": _strip_tags(org.get("okved2")),
        "статус_гирбо": org.get("statusCode"),
        "id_гирбо": org.get("id"),
        "отчётность_по_годам": [],
    }
    bfo = raw.get("bfo") if isinstance(raw.get("bfo"), list) else []
    details = raw.get("details") if isinstance(raw.get("details"), dict) else {}
    for rec in _top_periods(bfo):
        detail = _detail_lines(details.get(str(_g(rec, "id"))))
        gain = rec.get("gainSum")
        year = {
            "год": rec.get("period"),
            "выручка": None,
            "выручка_источник": None,
            "активы": detail.get("активы", rec.get("actives")),
            "прибыль_убыток": detail.get("прибыль"),
            "дата_отчётности": rec.get("actualBfoDate"),
            "строки": detail.get("строки", {}),
        }
        if detail.get("выручка") is not None:
            year["выручка"] = detail["выручка"]
            year["выручка_источник"] = (
                FIN_FROM_2110 if _num_eq(gain, detail["выручка"])
                else FIN_FROM_2110_DIFF % gain)
        elif gain is not None:
            year["выручка"] = gain
            year["выручка_источник"] = FIN_FROM_GAIN
        if year["активы"] is None:
            year["активы"] = rec.get("actives")
        finance["отчётность_по_годам"].append(year)

    if not finance["отчётность_по_годам"]:
        return None, _av("финансы", "пусто",
                         "организация в ГИР БО есть, но опубликованной отчётности нет")
    return finance, _av("финансы", "ok")


def _num_eq(a, b):
    try:
        return a is not None and b is not None and float(a) == float(b)
    except (TypeError, ValueError):
        return False


def _detail_lines(detail):
    """Детальная форма /nbo/bfo/<id>/details -> {выручка, прибыль, активы, строки}.
    Форма[0]: financialResult.current2110 (выручка), current2400 (чистая прибыль),
    balance.current1600 (итог актива) + строки для fin_scoring.py."""
    out = {}
    form = detail[0] if isinstance(detail, list) and detail else detail
    if not isinstance(form, dict):
        return out
    fr = form.get("financialResult") or form.get("finresult") or {}
    bal = form.get("balance") or {}
    out["выручка"] = _g(fr, "current2110")
    out["прибыль"] = _g(fr, "current2400")
    out["активы"] = _g(bal, "current1600")
    if out["активы"] is None:
        out["активы"] = _g(bal, "current1700")
    lines = {}
    for code in ("2110", "2400"):
        val = _g(fr, "current%s" % code)
        if val is not None:
            lines[code] = val
    # Сырые строки баланса для fin_scoring.py (traceability): итог, капитал и
    # резервы, оборотные активы, денежные средства, обязательства.
    for code in ("1600", "1300", "1200", "1250", "1400", "1500"):
        val = _g(bal, "current%s" % code)
        if val is not None:
            lines[code] = val
    out["строки"] = lines
    return out


def fetch_finance(opener, inn):
    return parse_finance(raw_finance(opener, inn), inn)


# ---------------------------------------------------------------------------
# Реестр МСП — rmsp.nalog.ru
# ---------------------------------------------------------------------------

МСП_КАТЕГОРИИ = {1: "микропредприятие", 2: "малое предприятие",
                3: "среднее предприятие"}
MSP_EMPTY_REASON = ("не найдена в Едином реестре МСП (крупная компания либо "
                    "исключена) — для МСБ-контрагента это стоп-сигнал")


def _parse_msp_row(row):
    """Разбор записи rmsp.nalog.ru search-proc.json. Формат верифицирован
    живым запросом 19.09.2026: inn, category (1/2/3), dtregistry, is_active,
    nptype (UL/IP), okved1, okved1name, cityname, od2_sschr, name_ex, ogrn."""
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
        "наименование": row.get("name_ex"),
        "огрн": row.get("ogrn"),
    }


def raw_msp(opener, inn):
    url = "https://rmsp.nalog.ru/search-proc.json?query=%s" % urllib.parse.quote(str(inn))
    try:
        status, text = _http_get(opener, url, referer="https://rmsp.nalog.ru/")
    except Exception as e:
        raise SourceUnavailable(_classify_exc(e))
    if status != 200:
        raise SourceUnavailable(_http_status_reason(status))
    j = _safe_json(text)
    if j is None:
        raise SourceUnavailable("схема: rmsp вернул не-JSON")
    return j


def parse_msp(raw, inn):
    """{data: [...]} -> блок «мсп». Контракт: inn, category, is_active, nptype."""
    if not isinstance(raw, dict):
        return _not_checked("мсп", "схема: ответ не JSON-объект")
    if not isinstance(raw.get("data"), list):
        return _not_checked("мсп", "схема: не найдено поле data")
    rec = _rec_msp(raw, inn)
    if rec is None:
        return None, _av("мсп", "пусто", MSP_EMPTY_REASON)
    missing = _schema_missing("мсп", inn, rec)
    if missing:
        return _not_checked("мсп", "схема: не найдено поле %s" % missing)
    return _parse_msp_row(rec), _av("мсп", "ok")


def fetch_msp(opener, inn):
    return parse_msp(raw_msp(opener, inn), inn)


# ---------------------------------------------------------------------------
# НПД — npd.nalog.ru
# ---------------------------------------------------------------------------

NPD_NOTE = ("схема по официальному описанию API (npd.nalog.ru/check-status); "
            "живой прогон — с РФ-IP (с не-РФ IP 406/403, проверено 19.09.2026)")


def _parse_npd(j, inn):
    """Разбор ответа npd.nalog.ru check-status (совместимость: dict или None)."""
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


def raw_npd(opener, inn):
    url = "https://npd.nalog.ru/api/v1/status"
    body = json.dumps({"inn": str(inn),
                       "date": time.strftime("%d.%m.%Y")}).encode("utf-8")
    headers = {"User-Agent": UA, "Accept": "application/json",
               "Content-Type": "application/json",
               "Referer": "https://npd.nalog.ru/check-status/"}
    j = _safe_json(_http_post(opener, url, body, headers))
    if j is None:
        raise SourceUnavailable("схема: npd вернул не-JSON")
    return j


def parse_npd(raw, inn):
    """Контракт: status. false — явный факт «не плательщик НПД» (ok), не «пусто»."""
    if not isinstance(raw, dict):
        return _not_checked("нпд", "схема: ответ не JSON-объект")
    missing = _schema_missing("нпд", inn, raw)
    if missing:
        return _not_checked("нпд", "схема: не найдено поле %s" % missing)
    return _parse_npd(raw, inn), _av("нпд", "ok")


def fetch_npd(opener, inn):
    return parse_npd(raw_npd(opener, inn), inn)


# ---------------------------------------------------------------------------
# Спецреестры ФНС — service.nalog.ru (НЕ верифицированы)
# ---------------------------------------------------------------------------

NOTE_UNVERIFIED = "эндпоинт не верифицирован (разведка с не-РФ IP 19.09.2026)"


def _service_nalog_query(opener, path, inn):
    """Спецреестры service.nalog.ru — двухшаговый флоу proc.json +
    search-result/{token} по образцу egrul.nalog.ru. НЕ ВЕРИФИЦИРОВАН: живьём
    19.09.2026 proc.json отдаёт HTML (HTTP 200) вместо JSON с token."""
    base = "https://service.nalog.ru/%s/" % path
    body = urllib.parse.urlencode({
        "captcha": "", "captchaToken": "", "query": str(inn),
        "page": "1", "pageSize": "10",
    }).encode("utf-8")
    headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": base,
    }
    post_json = _safe_json(_http_post(opener, base + "proc.json", body, headers))
    if post_json is None:
        raise SourceUnavailable("схема: service.nalog.ru/%s/proc.json отдаёт HTML вместо "
                                "JSON — %s" % (path, NOTE_UNVERIFIED))
    token = _g(post_json, "t")
    if not token:
        raise SourceUnavailable("схема: не найдено поле t (token) в ответе %s — %s"
                                % (path, NOTE_UNVERIFIED))
    for _ in range(POLL_TRIES):
        if _time_left() <= POLL_DELAY:
            raise SourceUnavailable("дедлайн: token %s получен, результат не успел" % path)
        time.sleep(POLL_DELAY)
        try:
            status, text = _http_get(opener, base + "search-result/%s" % token, referer=base)
        except Exception as e:
            raise SourceUnavailable(_classify_exc(e))
        j = _safe_json(text)
        if j is not None:
            return j
    raise SourceUnavailable("дедлайн: token %s получен, результат не пришёл" % path)


def fetch_special_registries(opener, inn):
    """Дисквалификация руководителя (dismissal), налоговая задолженность >1000 ₽ (zd),
    недостоверность сведений (invalid). Каждый подзапрос деградирует независимо;
    блок «ok» только если все три отдали JSON — иначе «не проверено» с причиной
    первого сбоя (сырые ответы не интерпретируются: схема не верифицирована)."""
    checks = (("дисквалификация_руководителя", "dismissal"),
              ("налоговая_задолженность", "zd"),
              ("недостоверность_сведений", "invalid"))
    out, failures = {}, []
    for key, path in checks:
        try:
            out[key] = {"статус": "получен сырой ответ", "данные_сырые":
                        _service_nalog_query(opener, path, inn)}
        except (SourceUnavailable, DeadlineExceeded) as e:
            failures.append((key, _classify_exc(e)))
    if failures:
        key, reason = failures[0]
        return _not_checked("спецреестры", "%s (подзапрос %s%s)" % (
            reason, key, "; ещё %d" % (len(failures) - 1) if len(failures) > 1 else ""))
    return out, _av("спецреестры", "ok")


# ---------------------------------------------------------------------------
# Кэш дампов — ЕРКНМ / РНП (registries_refresh.py), перечни — sanctions_check.py
# ---------------------------------------------------------------------------

def fetch_registry_cache(реестр, inn):
    """Офлайн-сверка по локальному кэшу дампов (ЕРКНМ/РНП). Сеть не дёргается.
    в_реестре True -> ok, False -> пусто, кэша нет -> «не проверено»."""
    try:
        mod = _load_sibling("registries_refresh")
    except Exception:
        return _not_checked(реестр, "не покрыто: registries_refresh.py не найден рядом "
                                    "со скриптом")
    try:
        res, note = mod.lookup(реестр, inn)
    except Exception as e:
        return _not_checked(реестр, "схема: сбой сверки по кэшу (%s)" % type(e).__name__)
    if res is None:
        return _not_checked(реестр, "не покрыто: %s" % note)
    if not res.get("в_реестре"):
        warn = "; ".join(res.get("предупреждения") or [])
        return None, _av(реестр, "пусто", "в кэше дампа записи по ИНН нет" +
                         (" (%s)" % warn if warn else ""))
    return res, _av(реестр, "ok")


def fetch_sanctions(opener, inn):
    """«санкции» отражает готовность sanctions_check.py: fetch сверку НЕ запускает
    (нужны ФИО/наименование), а смотрит mtime кэшей перечней (§1.3). Все перечни
    свежие -> ok с указанием команды сверки; иначе «не проверено»."""
    try:
        sc = _load_sibling("sanctions_check")
    except Exception:
        return _not_checked("санкции", "не покрыто: sanctions_check.py не найден рядом "
                                      "со скриптом")
    ttl = getattr(sc, "CACHE_TTL_DAYS", 7)
    перечни, problems = {}, []
    for src, info in sc.SOURCES.items():
        age = sc._cache_age_days(src)
        fresh = age is not None and age <= ttl
        перечни[src] = {"название": info.get("название"),
                        "возраст_дней": round(age, 1) if age is not None else None,
                        "свежий": fresh}
        if age is None:
            problems.append("%s: нет файла" % src)
        elif not fresh:
            problems.append("%s: устарел (%.0f дн., норма %d)" % (src, age, ttl))
    if problems:
        return _not_checked("санкции", "не покрыто: кэш перечней %s — обновите: "
                                      "sanctions_check.py --refresh" % "; ".join(problems))
    ages = ", ".join("%s %.0f дн." % (k, v["возраст_дней"]) for k, v in перечни.items())
    return ({"статус": "кэш перечней свежий; сверка по ИНН/ФИО — sanctions_check.py "
                       "--inn %s [--name <ФИО/наименование>]" % inn,
             "перечни": перечни},
            _av("санкции", "ok", "кэш: перечни свежие (%s); сверку делает "
                                  "sanctions_check.py" % ages))


# ---------------------------------------------------------------------------
# Реестр fetcher'ов, run_source, probe-кэш, канарейка, итог
# ---------------------------------------------------------------------------

# id источника -> callable(opener, inn) -> (данные, _доступность).
# Источники «требует: браузер» fetcher'а не имеют: run_source даёт «не покрыто».
FETCHERS = {
    "егрюл": fetch_egrul,
    "риски": fetch_risks,
    "финансы": fetch_finance,
    "мсп": fetch_msp,
    "нпд": fetch_npd,
    "спецреестры": fetch_special_registries,
    "еркнм": lambda opener, inn: fetch_registry_cache("еркнм", inn),
    "рнп": lambda opener, inn: fetch_registry_cache("рнп", inn),
    "санкции": fetch_sanctions,
}

# id источника -> чистый парсер (raw, inn) -> (данные, _доступность); гоняет eval.
PARSERS = {
    "егрюл": parse_egrul,
    "риски": parse_risks,
    "финансы": parse_finance,
    "мсп": parse_msp,
    "нпд": parse_npd,
}

# Кэш канареек в процессе: (id, канареечный ИНН) -> ("ok"|"провал"|"не запускалась", причина)
_CANARY_CACHE = {}


def _browser_note(source_id, inn):
    ип = _тип_контрагента(inn) == "ип"
    if source_id == "фссп":
        # Официальный API ФССП (api-ip.fssp.gov.ru) отключён с 10.03.2022 и не
        # восстановлен — слой только браузерный.
        return ("официальный API ФССП отключён с 10.03.2022 — поиск вручную на "
                "fssp.gov.ru или через агрегатор"
                + ("; для ИП — ДВА поиска: как юрлицо по ИНН и как физлицо "
                   "по ФИО+дата рождения+регион" if ип else ""))
    if source_id == "суды":
        return ("kad.arbitr.ru — только браузер (JS/капча); обязательный шаг "
                "каскада, см. SKILL.md")
    if source_id == "банкротство":
        return ("bankrot.fedresurs.ru (ЕФРСБ) — только браузер; обязательный шаг "
                "каскада" + (" (для ИП — включая внесудебное банкротство физлиц)"
                             if ип else ""))
    return "%s — скриптом не покрыто" % SOURCES.get(source_id, {}).get("название", source_id)


def _load_access_cache():
    """Кэш probe (check_access.py, §4): ~/.cache/inn-check-ru/access.json, TTL 24 ч.
    Нет файла / устарел / COUNTERPARTY_IGNORE_ACCESS_CACHE=1 -> None."""
    if os.environ.get("COUNTERPARTY_IGNORE_ACCESS_CACHE") == "1":
        return None
    try:
        with open(ACCESS_CACHE, encoding="utf-8") as fh:
            cache = json.load(fh)
        stamp = _dt.datetime.fromisoformat(str(cache.get("дата")))
        if stamp.tzinfo is None:
            stamp = stamp.astimezone()      # check_access пишет локальное время без зоны
        age_h = (_dt.datetime.now(_dt.timezone.utc) - stamp).total_seconds() / 3600.0
        if age_h < 0 or age_h > ACCESS_CACHE_TTL_H:
            return None
        return cache if isinstance(cache.get("источники"), dict) else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def _probe_block(source_id):
    cache = _load_access_cache()
    st = _g(cache, "источники", source_id, "состояние")
    return st if st in PROBE_BLOCKING else None


def _non_empty(v):
    return v not in (None, "", [], {})


def _run_canary(source_id, canary, opener):
    """Один раз за процесс: fetcher на канареечном ИНН дескриптора canary. «ok» —
    состояние ok и все ожидаем_непустые непустые; «провал» — пусто/пустые поля;
    сеть/капча/дедлайн — «не запускалась» (о парсере ничего не говорит)."""
    key = (source_id, str(canary["инн"]))
    if key in _CANARY_CACHE:
        return _CANARY_CACHE[key]
    if _time_left() <= POLL_DELAY * 2:
        result = ("не запускалась", "дедлайн: бюджет времени исчерпан")
        _CANARY_CACHE[key] = result
        return result
    try:
        data, av = FETCHERS[source_id](opener, canary["инн"])
    except (SourceUnavailable, DeadlineExceeded) as e:
        result = ("не запускалась", _classify_exc(e))
    except Exception as e:
        result = ("провал", "схема: исключение парсера на канарейке (%s)" % type(e).__name__)
    else:
        st = av.get("состояние")
        if st == "ok":
            empty = [f for f in canary.get("ожидаем_непустые", [])
                     if not _non_empty((data or {}).get(f))]
            result = ("ok", None) if not empty else (
                "провал", "схема: канарейка без полей %s" % ", ".join(empty))
        elif st == "пусто":
            result = ("провал", "схема: канарейка пуста — парсер сломан")
        else:
            result = ("не запускалась", av.get("причина"))
    _CANARY_CACHE[key] = result
    return result


def run_source(source_id, inn, opener=None, профиль=None):
    """Один источник по реестру: профиль -> браузер -> probe-кэш -> fetcher ->
    канарейка. Всегда (данные|None, _доступность); исключения — в «не проверено»."""
    d = SOURCES[source_id]
    if профиль and source_id not in sources.sources_for_profile(профиль):
        return _not_checked(source_id, "профиль: не требуется для %s" % профиль)
    if d.get("требует") == "браузер" or source_id not in FETCHERS:
        return _not_checked(source_id, "не покрыто: %s" % _browser_note(source_id, inn))
    if d.get("требует") == "сеть":
        blocked = _probe_block(source_id)
        if blocked:
            return _not_checked(source_id, "probe: %s" % blocked)
        if opener is None:
            opener = _make_opener()
    try:
        data, av = FETCHERS[source_id](opener, inn)
    except (SourceUnavailable, DeadlineExceeded) as e:
        return _not_checked(source_id, _classify_exc(e))
    except Exception as e:
        return _not_checked(source_id, "схема: непредвиденное исключение %s: %s"
                            % (type(e).__name__, e))
    if av.get("состояние") == "ok" and not data:
        return _not_checked(source_id, "схема: парсер дал «ok» без данных")
    if av.get("состояние") == "пусто":
        canary = _canary_for(source_id, inn)
        if canary and str(canary.get("инн")) != str(inn):
            verdict, why = _run_canary(source_id, canary, opener)
            if verdict == "провал":
                return None, _av(source_id, "не проверено", why, канарейка="провал")
            av["канарейка"] = verdict
        data = None
    if av.get("состояние") == "не проверено":
        data = None
    return data, av


def build_summary(av_map):
    """_итог_проверки (§1.3): проверка состоялась, если не проверено не больше
    половины deal-killer-источников."""
    states = {sid: (av or {}).get("состояние") for sid, av in av_map.items()}
    dk = sources.deal_killer_ids()
    dk_nc = [sid for sid in dk if states.get(sid) != "ok" and states.get(sid) != "пусто"]
    ok = len(dk_nc) * 2 <= len(dk)
    if ok:
        вывод = ("проверка состоялась: %d из %d deal-killer-источников не проверено%s"
                 % (len(dk_nc), len(dk),
                    " (%s)" % ", ".join(dk_nc) if dk_nc else ""))
    else:
        вывод = ("проверка НЕ состоялась: %d из %d deal-killer-источников не проверено "
                 "— светофор 🟢 недопустим" % (len(dk_nc), len(dk)))
    return {
        "источников": len(av_map),
        "ok": sum(1 for s in states.values() if s == "ok"),
        "пусто": sum(1 for s in states.values() if s == "пусто"),
        "не_проверено": sum(1 for s in states.values() if s == "не проверено"),
        "deal_killer_источники": dk,
        "deal_killer_не_проверено": dk_nc,
        "проверка_состоялась": ok,
        "вывод": вывод,
    }


def collect(inn, профиль=None, opener=None):
    """Обход SOURCES по порядку -> полный результат (без записи снимка)."""
    if opener is None:
        opener = _make_opener()
    result = {"инн": inn, "тип": _тип_контрагента(inn), "профиль": профиль or "нейтрально"}
    av_map = {}
    for sid in SOURCES:
        data, av = run_source(sid, inn, opener=opener, профиль=профиль)
        result[sid] = data
        av_map[sid] = av
    # egrul не отдаёт статус (k — вид субъекта): текстовый статус — из pb.
    egrul, risks = result.get("егрюл"), result.get("риски")
    if isinstance(egrul, dict) and isinstance(risks, dict) and risks.get("статус"):
        egrul["статус"] = risks["статус"]
        egrul["статус_источник"] = "pb.nalog.ru (sulst_name_ex)"
    result["_доступность"] = av_map
    result["_итог_проверки"] = build_summary(av_map)
    return result


def run_canaries(opener=None):
    """--канарейки: каждый источник с канарейкой против её ИНН (еженедельный CI)."""
    if opener is None:
        opener = _make_opener()
    out, failed = {}, 0
    for sid, d in SOURCES.items():
        for key in ("канарейка", "канарейка_ип"):
            canary = d.get(key)
            if not canary or sid not in FETCHERS:
                continue
            _CANARY_CACHE.clear()
            verdict, why = _run_canary(sid, canary, opener)
            out["%s%s" % (sid, " (ип)" if key == "канарейка_ип" else "")] = {
                "инн": canary["инн"], "канарейка": verdict, "причина": why}
            failed += verdict == "провал"
    return out, failed


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
        snap_dir = os.path.join(CACHE_DIR, "snapshots", inn)
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


USAGE = ("Использование: python3 fetch_counterparty.py <ИНН> [--save] [--профиль <id>]\n"
         "               python3 fetch_counterparty.py --канарейки\n"
         "Профили: %s\n" % ", ".join(sources.PROFILE_IDS))


def _parse_args(argv):
    args, do_save, профиль, canaries = [], False, None, False
    it = iter(argv[1:])
    for a in it:
        if a == "--save":
            do_save = True
        elif a == "--канарейки":
            canaries = True
        elif a in ("--профиль", "--profile"):
            профиль = next(it, None)
        elif a.startswith("--профиль="):
            профиль = a.split("=", 1)[1]
        else:
            args.append(a)
    return args, do_save, профиль, canaries


def main(argv):
    args, do_save, профиль, canaries = _parse_args(argv)
    if canaries:
        out, failed = run_canaries()
        sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
        return 1 if failed else 0
    if len(args) != 1:
        sys.stderr.write(USAGE)
        return 2
    if профиль is not None and профиль not in sources.PROFILE_IDS:
        sys.stdout.write(json.dumps(
            {"ошибка": "неизвестный профиль %r; допустимые: %s"
                       % (профиль, ", ".join(sources.PROFILE_IDS))},
            ensure_ascii=False, indent=2) + "\n")
        return 2
    inn = validate_inn(args[0])
    if inn is None:
        out = {"ошибка": "Некорректный ИНН: ожидается 10 или 12 цифр с верным "
                         "контрольным числом (алгоритм ФНС) — вероятна опечатка",
               "ввод": args[0]}
        sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
        return 1

    result = collect(inn, профиль=профиль)
    if do_save:
        save_snapshot(inn, result)
    sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))


def cli():
    """console_script `inn-check-ru` (pyproject.toml) — та же main(sys.argv)."""
    return sys.exit(main(sys.argv))
