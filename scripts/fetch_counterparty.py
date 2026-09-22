#!/usr/bin/env python3
"""
fetch_counterparty.py — проверка российского контрагента по ИНН через
бесплатные открытые JSON-эндпоинты ФНС. Только стандартная библиотека.

Использование:
    python3 fetch_counterparty.py <ИНН> [--save] [--профиль <id>]
                                        [--режим quick|полный|всё]
                                        [--прокси http://host:3128]
    python3 fetch_counterparty.py --канарейки        # парсеры против канареечных ИНН

Прокси (scripts/proxy.py, спек v2 §18.2): --прокси > INN_CHECK_PROXY > HTTPS_PROXY.
Свой узел в РФ открывает источники, закрытые для не-РФ IP. Узел при этом видит,
какие ИНН вы проверяете, поэтому — только своя нода: общего пула у проекта нет и
не будет. Битый URL — отказ, а не тихий переход на прямое соединение. Блок «_сеть»
в выводе говорит, через что шли; probe-кэш, снятый из другой сети, не берётся.

--save сохраняет снимок в ~/.cache/inn-check-ru/snapshots/<ИНН>/<дата>_<время>.json
(мониторинг через diff_counterparty.py). --профиль <id> собирает только источники
профиля (id — sources.PROFILE_IDS, по умолчанию «нейтрально»); остальные —
«не проверено», причина «профиль: не требуется для <id>».

Фазы и ранний выход (волна 2, §1): источники разнесены по фазам
(sources.SOURCES[*]["фаза"]). quick-фаза — то, что дёшево даёт deal-killer
(егрюл, риски, спецреестры, санкции + браузерные с их «не покрыто»); досье-фаза —
финансы, мсп, нпд, еркнм, рнп. `--режим quick` останавливается после быстрой фазы,
`--режим полный` (по умолчанию) после неё спрашивает profiles.resolve() и, если
светофор 🔴, досье не собирает; `--режим всё` собирает всё без раннего выхода.
Источники одной фазы собираются параллельно (максимум 4 потока, свой opener на
поток, общий дедлайн); COUNTERPARTY_SEQUENTIAL=1 возвращает последовательный обход.
Блок `_сбор` в выводе говорит, что именно произошло.

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
     "_доступность": {...}, "_итог_проверки": {...}, "_сбор": {...}}

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

import concurrent.futures
import datetime as _dt
import http.cookiejar
import importlib.util
import json
import os
import socket
import ssl
import sys
import threading
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
proxy = _load_sibling("proxy")

# Пользовательский прокси (scripts/proxy.py): --прокси > INN_CHECK_PROXY > HTTPS_PROXY.
# Разбирается один раз при импорте; --прокси доставляется через установить_прокси().
_ПРОКСИ = proxy.настройка()
# Почему probe-кэш не пригодился: пусто — пригодился или его просто нет.
_КЭШ_ДОСТУПА_ОТКЛОНЁН = None


def установить_прокси(явный=None):
    """Пересобрать настройку прокси с учётом флага. Возвращает текст ошибки или None."""
    global _ПРОКСИ
    _ПРОКСИ = proxy.настройка(явный)
    return _ПРОКСИ["ошибка"]


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
    # Битый URL прокси — отказ, а не прямое соединение: пользователь прячет свой
    # IP намеренно, и «тихо пойдём напрямую» раскрыло бы его именно тогда, когда
    # этого не ждут. main() ловит то же самое раньше и печатает JSON с ошибкой;
    # здесь — страховка для всех остальных входов в модуль.
    if _ПРОКСИ["ошибка"]:
        raise ValueError("прокси: " + _ПРОКСИ["ошибка"])
    cj = http.cookiejar.CookieJar()
    return urllib.request.build_opener(
        # ProxyHandler задаётся явно всегда: пустой отключает подхват переменных
        # окружения, и прямое соединение остаётся прямым, а не «как повезёт».
        proxy.handler(_ПРОКСИ["url"]),
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


# Машинно-различимые префиксы причин «не проверено» (§1.1 волны 1 + §1.2 волны 2).
# Двухсловные («не покрыто:», «ранний выход:») тоже полноценные префиксы, поэтому
# проверка идёт по списку, а не по «первому слову с двоеточием».
ПРЕФИКСЫ_ПРИЧИН = ("сеть:", "tls:", "гео:", "капча:", "дедлайн:", "схема:",
                   "не покрыто:", "профиль:", "probe:", "режим:", "ранний выход:")


def _есть_префикс(s):
    return str(s).startswith(ПРЕФИКСЫ_ПРИЧИН)


def _classify_exc(e):
    """Исключение сетевого слоя -> причина с машинно-различимым префиксом."""
    if isinstance(e, (SourceUnavailable, DeadlineExceeded)):
        s = str(e)
        return s if _есть_префикс(s) else "дедлайн: " + s
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
# Реестр дисквалифицированных лиц ФНС — service.nalog.ru/disqualified-proc.json
# ---------------------------------------------------------------------------
#
# Живая разведка 19.09.2026 (волна 2, §1.4; дословные ответы — в sources.py,
# дескриптор «спецреестры», ключ «разведка»):
#   * disqualified.do  -> форма POST disqualified-proc.json, капчи нет;
#     proc.json отдаёт JSON СРАЗУ (ни token, ни поллинга, в отличие от egrul);
#   * zd.do            -> «сервис выведен из эксплуатации», данные в «Прозрачном
#     бизнесе» -> покрывается блоком «риски»;
#   * invalid-addresses.do / mri.do / mn.do -> редирект на /payment/ (сервиса нет);
#     недостоверность сведений отдаёт pb (поле invalid) -> блок «риски»;
#   * mru.do / addrfind.do -> редирект на pb.nalog.ru; baddr.do -> service-closed;
#     svl.do -> выведен из эксплуатации с 09.06.2023.
# Старая схема <path>/proc.json + search-result/<t> принадлежит egrul.nalog.ru и к
# service.nalog.ru отношения не имеет — отсюда и HTML 200 вместо JSON.
#
# ГЛАВНОЕ ОГРАНИЧЕНИЕ: реестр НЕ ищется по ИНН (проверено на пяти организациях с
# дисквалифицированным действующим руководителем — rowCount=0), хотя форма это
# обещает. Рабочий ключ — ФИО. Поэтому источник зависит от блока «егрюл»
# (sources.SOURCES["спецреестры"]["зависит_от"]) и сверяет ФИО руководителя.

DISQ_PAGE = "https://service.nalog.ru/disqualified.do"
DISQ_PROC = "https://service.nalog.ru/disqualified-proc.json"
DISQ_NOTE_NAME = ("совпадение по ФИО без даты и места рождения — юридически это НЕ "
                  "идентификация лица: сверьте ДатаРожд/МестоРожд записи с паспортными "
                  "данными руководителя (правило однофамильцев, KNOWN_LIMITS)")
DISQ_NO_HEAD = ("не покрыто: реестр дисквалифицированных ищется по ФИО, а не по ИНН "
                "(проверено 19.09.2026 на пяти ИНН — rowCount=0). %s")
# Подреестры, которых больше нет как отдельных сервисов (дословно — см. «разведка»).
DISQ_MOVED = {
    "налоговая_задолженность": {
        "статус": "не покрыто",
        "причина": "service.nalog.ru/zd.do выведен из эксплуатации (проверено "
                   "19.09.2026); задолженность по налогам отдаёт «Прозрачный бизнес» "
                   "— см. блок «риски», подблок «детали» (нужен РФ-IP)",
    },
    "недостоверность_сведений": {
        "статус": "не покрыто",
        "причина": "service.nalog.ru/invalid-addresses.do редиректит на /payment/ — "
                   "отдельного сервиса нет (проверено 19.09.2026); признак отдаёт pb "
                   "(поле invalid) — см. блок «риски», поле «недостоверность_сведений»",
    },
    "массовый_адрес_руководитель": {
        "статус": "не покрыто",
        "причина": "service.nalog.ru/mru.do и /addrfind.do редиректят на pb.nalog.ru, "
                   "/baddr.do закрыт (проверено 19.09.2026); массовость — за token в "
                   "company-proc.json, см. блок «риски», подблок «детали»",
    },
}


# Маркеры того, что «руководитель» из ЕГРЮЛ — организация, а не человек.
# Реальный случай (5036045205 АО «ДИКСИ ЮГ»): «Управляющая организация:
# АКЦИОНЕРНОЕ ОБЩЕСТВО "ДИКСИ ГРУПП"». Искать такое в реестре дисквалифицированных
# ФИЗЛИЦ бессмысленно, а «пусто» было бы тихой ложью.
_ORG_ПОДСТРОКИ = ("организац", "общество", "товарищество", "кооператив", "компани",
                  "предприят", "фонд", "учреждени", "партнёрств", "партнерств",
                  "филиал", "представительств", "банк", "корпорац")
_ORG_СЛОВА = {"ооо", "оао", "зао", "пао", "ао", "нао", "нко", "гк", "ук", "нп", "ано"}


def _похоже_на_фио(s):
    """Строка выглядит как ФИО физлица (2–4 слова из букв), а не как организация."""
    if not isinstance(s, str) or not s.strip():
        return False
    if any(ch in s for ch in '"«»()0123456789'):
        return False
    токены = s.split()
    if not 2 <= len(токены) <= 4:
        return False
    if any(not t.replace("-", "").replace("'", "").isalpha() for t in токены):
        return False
    low = s.lower()
    if any(m in low for m in _ORG_ПОДСТРОКИ):
        return False
    return not any(t.lower().strip(".") in _ORG_СЛОВА for t in токены)


def _disq_head_name(контекст):
    """(ФИО в верхнем регистре, откуда взято, причина отказа).

    egrul отдаёт руководителя строкой «ДОЛЖНОСТЬ: Фамилия Имя Отчество»; у ИП
    руководителя нет, но наименование карточки и есть ФИО. Регистр реестру
    безразличен (проверено живьём), поэтому приводим к верхнему для сверки.
    Если руководитель — управляющая организация, ФИО не возвращается: источник
    честно скажет «не проверено» и подскажет, что делать дальше.
    """
    if not isinstance(контекст, dict) or not isinstance(контекст.get("егрюл"), dict):
        return None, None, ("блок «егрюл» не собран, ФИО руководителя взять неоткуда "
                            "(реестр ищет по ФИО, ИНН он не индексирует)")
    егрюл = контекст["егрюл"]
    g = егрюл.get("руководитель")
    if isinstance(g, str) and g.strip():
        g = " ".join(g.split())
        фио = " ".join((g.split(":", 1)[1] if ":" in g else g).split())
        if _похоже_на_фио(фио):
            return фио.upper(), "ФИО руководителя из ЕГРЮЛ (%s)" % g, None
        return None, None, (
            "руководитель в ЕГРЮЛ — не физлицо, а «%s»: реестр дисквалифицированных "
            "ведётся по ФИО должностных лиц. Проверьте руководителя управляющей "
            "организации отдельным прогоном по её ИНН" % g)
    if егрюл.get("вид") == "fl":
        имя = егрюл.get("наименование_полное") or егрюл.get("наименование_краткое")
        if _похоже_на_фио(" ".join(str(имя or "").split())):
            return " ".join(str(имя).split()).upper(), "ФИО ИП из ЕГРИП", None
    return None, None, ("блок «егрюл» не отдал ФИО руководителя (поле «руководитель» "
                        "пустое), а по ИНН реестр не ищется")


def _disq_post(opener, query):
    """Один POST disqualified-proc.json. Возвращает разобранный JSON."""
    body = urllib.parse.urlencode({
        "query": query, "page": "1", "pageSize": "25",
        "m": "", "fam": "", "nam": "", "otch": "", "bd": "", "bp": "",
    }).encode("utf-8")
    headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": DISQ_PAGE,
    }
    j = _safe_json(_http_post(opener, DISQ_PROC, body, headers))
    if j is None:
        raise SourceUnavailable("схема: disqualified-proc.json вернул не-JSON "
                                "(ожидался {data, rowCount})")
    return j


def raw_special_registries(opener, inn, контекст=None):
    """Запрос по ФИО руководителя + встроенная самопроверка реестра.

    Возвращает {"запрос": {...}, "ответ": {...}, "самопроверка": {...}|None}.
    Самопроверка делается ТОЛЬКО когда записей не нашлось: пустой query обязан
    вернуть весь реестр (19.09.2026 — 8188 записей). Ноль там означает, что
    сломался эндпоинт, а не что руководитель чист, — и парсер обязан сказать
    «не проверено», а не «пусто». Это замена канарейки: канарейка по ИНН
    невозможна (реестр ИНН не индексирует), по ФИО — протухает вместе со сроком.
    """
    фио, откуда, отказ = _disq_head_name(контекст)
    if not фио:
        raise SourceUnavailable(DISQ_NO_HEAD % отказ)
    ответ = _disq_post(opener, фио)
    raw = {"запрос": {"тип": "ФИО", "значение": фио, "откуда": откуда},
           "ответ": ответ, "самопроверка": None}
    rows = ответ.get("data") if isinstance(ответ, dict) else None
    if isinstance(rows, list) and not rows:
        проба = _disq_post(opener, "")
        raw["самопроверка"] = {
            "запрос": "пустой query — весь реестр",
            "записей_в_реестре": _g(проба, "rowCount"),
        }
    return raw


def _rec_special(raw, inn):
    """Первая запись ответа реестра (по ней проверяется контракт §1.1)."""
    rows = _g(raw, "ответ", "data")
    if not isinstance(rows, list):
        return None
    dict_rows = [r for r in rows if isinstance(r, dict)]
    return dict_rows[0] if dict_rows else None


def _disq_дата(v):
    """«25.08.2026 00:00:00» -> «25.08.2026»; иное значение — как есть."""
    if isinstance(v, str) and len(v) >= 10 and v[2] == "." and v[5] == ".":
        return v[:10]
    return v


def _дата_dmy(v):
    """«25.08.2026» или «25.08.2026 00:00:00» -> datetime.date; иначе None."""
    s = _disq_дата(v)
    if not isinstance(s, str) or len(s) != 10:
        return None
    try:
        return _dt.date(int(s[6:10]), int(s[3:5]), int(s[0:2]))
    except ValueError:
        return None


def _disq_действует(запись, сегодня=None):
    """Действует ли дисквалификация на сегодня. None — даты не разобрались.

    Реестр хранит только действующие записи (19.09.2026: все 8188 с датой
    окончания в будущем), но полагаться на это нельзя — считаем по датам.
    """
    d_кон = _дата_dmy(запись.get("ДатаКонДискв"))
    if d_кон is None:
        return None
    сегодня = сегодня or _dt.datetime.now(tz=_dt.timezone.utc).date()
    d_нач = _дата_dmy(запись.get("ДатаНачДискв"))
    return d_кон >= сегодня and (d_нач is None or d_нач <= сегодня)


def parse_special_registries(raw, inn):
    """Ответ реестра дисквалифицированных -> блок «спецреестры». ЧИСТАЯ функция.

    «ok»    — записи по ФИО руководителя найдены (это deal-killer-сигнал);
    «пусто» — записей нет И самопроверка подтвердила, что реестр живой;
    «не проверено» — всё остальное, включая пустой ответ при нулевой самопроверке.
    """
    if not isinstance(raw, dict):
        return _not_checked("спецреестры", "схема: ответ не JSON-объект")
    ответ = raw.get("ответ")
    if not isinstance(ответ, dict):
        return _not_checked("спецреестры", "схема: не найдено поле ответ")
    rows = ответ.get("data")
    if not isinstance(rows, list):
        return _not_checked("спецреестры", "схема: не найдено поле data")
    запрос = raw.get("запрос") if isinstance(raw.get("запрос"), dict) else {}
    подписи = dict(DISQ_MOVED)

    if not rows:
        всего = _g(raw, "самопроверка", "записей_в_реестре")
        try:
            живой = int(всего) > 0
        except (TypeError, ValueError):
            живой = False
        if not живой:
            return _not_checked(
                "спецреестры",
                "схема: реестр дисквалифицированных вернул пусто И самопроверка "
                "пустым запросом дала %r вместо всего реестра — «дисквалификации нет» "
                "утверждать нельзя" % (всего,))
        return None, _av("спецреестры", "пусто",
                         "в реестре дисквалифицированных нет записей по ФИО «%s» "
                         "(реестр живой: %s записей); задолженность и недостоверность "
                         "этот сервис больше не отдаёт — см. блок «риски»"
                         % (запрос.get("значение"), всего))

    missing = _schema_missing("спецреестры", inn, _rec_special(raw, inn))
    if missing:
        return _not_checked("спецреестры", "схема: не найдено поле %s" % missing)

    записи = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        записи.append({
            "фио": r.get("ФИО"),
            "дата_рождения": _disq_дата(r.get("ДатаРожд")),
            "место_рождения": r.get("МестоРожд"),
            "организация": r.get("НаимОрг"),
            "должность": r.get("Должность"),
            "статья_коап": r.get("КвалификацияТекст"),
            "дисквалификация_с": _disq_дата(r.get("ДатаНачДискв")),
            "дисквалификация_по": _disq_дата(r.get("ДатаКонДискв")),
            "срок": r.get("ДисквСрок"),
            "орган": r.get("НаимОргПрот"),
            "номер_записи": r.get("НомЗап"),
            "действует": _disq_действует(r),
        })
    точные = [z for z in записи
              if str(z.get("фио") or "").strip().upper()
              == str(запрос.get("значение") or "").strip().upper()]
    действующие = [z for z in (точные or записи) if z.get("действует")]
    # Форма подблока продиктована profiles.py (_истина): {"статус", "данные"} —
    # «статус» не начинается с «не» -> сигнал берётся из «данные», а непустой
    # список «записи» внутри и означает «сигнал найден».
    подписи["дисквалификация_руководителя"] = {
        "статус": "проверено",
        "совпадение": "точное по ФИО" if точные else "нестрогое (подстрока ФИО)",
        "запрос": запрос,
        "примечание": DISQ_NOTE_NAME,
        "данные": {
            "записей": len(записи),
            "точных_совпадений_фио": len(точные),
            "действующих": len(действующие),
            "записи": записи,
        },
    }
    return подписи, _av("спецреестры", "ok")


def fetch_special_registries(opener, inn, контекст=None):
    return parse_special_registries(raw_special_registries(opener, inn, контекст), inn)


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


def fetch_opendata_dumps(opener, inn):
    """Поиск по ИНН в локальных индексах дампов ФНС (opendata_index.py). Сеть не
    дёргается.

    Зачем отдельный блок, а не подмешивание в «риски»: pb отвечает про сегодня,
    а дамп — про дату выгрузки (snr на 22.09.2026 — состояние на 01.08.2026,
    sshr — на 31.12.2025). Подмешать датированное в живое значило бы выдать
    прошлое за настоящее, и никакой потребитель этого уже не заметил бы.

    Состояния: хоть один датасет ответил -> ok; все ответили «записи нет» ->
    пусто; индексов нет или они неполные -> «не проверено» с причиной.
    """
    try:
        oi = _load_sibling("opendata_index")
    except Exception:
        return _not_checked("дампы_фнс", "не покрыто: opendata_index.py не найден "
                                         "рядом со скриптом")
    try:
        по_датасетам = oi.найти_всё(inn)
    except Exception as e:
        return _not_checked("дампы_фнс",
                            "схема: сбой поиска по индексу (%s)" % type(e).__name__)

    данные = {"по_датасетам": по_датасетам,
              "дата_выгрузки": {ид: r.get("дата_выгрузки")
                                for ид, r in по_датасетам.items()}}
    ссч = по_датасетам.get("sshr2019") or {}
    снр = по_датасетам.get("snr") or {}
    нал = по_датасетам.get("paytax") or {}
    if ссч.get("состояние") == "ок":
        данные["численность_сотрудников"] = ссч.get("ссч")
        данные["численность_год"] = ссч.get("год")
    if снр.get("состояние") == "ок":
        данные["спецрежимы"] = снр.get("режимы")
        данные["спецрежим_на_дату"] = снр.get("дата_выгрузки")
    if нал.get("состояние") == "ок":
        данные["налоги_руб"] = {
            "всего": нал.get("налогов_всего_руб"),
            "ндс": нал.get("ндс_руб"),
            "налог_на_прибыль": нал.get("налог_на_прибыль_руб"),
            "усн": нал.get("усн_руб"),
        }
        данные["налоги_год"] = нал.get("год")

    состояния = [r.get("состояние") for r in по_датасетам.values()]
    if "ок" in состояния:
        # Часть датасетов могла промолчать — это НЕ повод считать блок целиком
        # проверенным: непроверенные датасеты перечисляются прямо в данных.
        неготовые = {ид: r.get("причина") for ид, r in по_датасетам.items()
                     if r.get("состояние") == "не проверено"}
        if неготовые:
            данные["не_проверено"] = неготовые
        return данные, _av("дампы_фнс", "ok")
    if all(с == "пусто" for с in состояния):
        return None, _av("дампы_фнс", "пусто",
                         "во всех выгрузках ФНС записи по этому ИНН нет; часть "
                         "налогоплательщиков в открытые дампы не попадает — "
                         "это не значит «сведений не существует»")
    причины = "; ".join(sorted({r.get("причина") or "" for r in по_датасетам.values()
                                if r.get("состояние") == "не проверено"}))
    return _not_checked("дампы_фнс", причины or "кэш: индексы дампов недоступны")


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
    "дампы_фнс": fetch_opendata_dumps,
}

# id источника -> чистый парсер (raw, inn) -> (данные, _доступность); гоняет eval.
PARSERS = {
    "егрюл": parse_egrul,
    "риски": parse_risks,
    "финансы": parse_finance,
    "мсп": parse_msp,
    "нпд": parse_npd,
    "спецреестры": parse_special_registries,
}

# Источники, чей fetcher принимает третьим аргументом контекст — данные уже
# собранных источников (sources.SOURCES[id]["зависит_от"]). Остальные fetcher'ы
# вызываются по-старому, двумя аргументами: check_access.py --канарейки и внешние
# потребители FETCHERS ничего не замечают.
CONTEXT_FETCHERS = {"спецреестры"}

# Запись сырого ответа для источника «спецреестры» — как и для остальных,
# из неё eval дрейфа схемы удаляет поля контракта по очереди.
RAW_RECORD["спецреестры"] = _rec_special

# Кэш канареек в процессе: (id, канареечный ИНН) -> ("ok"|"провал"|"не запускалась", причина)
# Разделяется потоками параллельного сбора, поэтому ходит под замком: канарейка
# одного источника обязана запуститься не больше одного раза за процесс.
_CANARY_CACHE = {}
_CANARY_LOCK = threading.Lock()
_CANARY_KEY_LOCKS = {}


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
        # Probe, снятый из другой сети, к этой не относится: прогон через РФ-ноду
        # и прогон напрямую видят разные источники. Подставить один вместо
        # другого — это ровно «гео: недоступно» под видом «доступно» (или
        # наоборот). Кэш без отметки о сети — от версии до 1.11.0, чем он снят,
        # неизвестно, поэтому тоже не годится.
        чем_снят = _g(cache, "сеть", "отпечаток")
        if чем_снят != _ПРОКСИ["отпечаток"]:
            global _КЭШ_ДОСТУПА_ОТКЛОНЁН
            _КЭШ_ДОСТУПА_ОТКЛОНЁН = (
                "снят из другой сети (%s), сейчас %s — источники пробуются живьём"
                % (чем_снят or "сеть не отмечена, версия до 1.11.0",
                   _ПРОКСИ["отпечаток"]))
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


def _canary_key_lock(key):
    """Замок на конкретную канарейку: разные источники не ждут друг друга."""
    with _CANARY_LOCK:
        return _CANARY_KEY_LOCKS.setdefault(key, threading.Lock())


def _canary_cached(key):
    with _CANARY_LOCK:
        return _CANARY_CACHE.get(key)


def _call_fetcher(source_id, opener, inn, контекст=None):
    """Вызов FETCHERS[id] с учётом того, принимает ли он контекст (CONTEXT_FETCHERS).

    Подменённые в eval fetcher'ы двух аргументов остаются рабочими: третий
    аргумент передаётся только источникам из CONTEXT_FETCHERS.
    """
    if source_id in CONTEXT_FETCHERS:
        return FETCHERS[source_id](opener, inn, контекст)
    return FETCHERS[source_id](opener, inn)


def _run_canary(source_id, canary, opener):
    """Один раз за процесс: fetcher на канареечном ИНН дескриптора canary. «ok» —
    состояние ok и все ожидаем_непустые непустые; «провал» — пусто/пустые поля;
    сеть/капча/дедлайн — «не запускалась» (о парсере ничего не говорит).

    Потокобезопасно: при параллельном сборе (§1.3) несколько потоков могут
    одновременно упереться в «пусто» одного источника — сеть на канарейку обязана
    уйти ровно один раз, поэтому проверка кэша и сам прогон идут под замком ключа.
    """
    key = (source_id, str(canary["инн"]))
    cached = _canary_cached(key)
    if cached is not None:
        return cached
    with _canary_key_lock(key):
        cached = _canary_cached(key)          # пока ждали замок, сосед мог посчитать
        if cached is not None:
            return cached
        if _time_left() <= POLL_DELAY * 2:
            result = ("не запускалась", "дедлайн: бюджет времени исчерпан")
        else:
            try:
                data, av = _call_fetcher(source_id, opener, canary["инн"],
                                         canary.get("контекст"))
            except (SourceUnavailable, DeadlineExceeded) as e:
                result = ("не запускалась", _classify_exc(e))
            except Exception as e:
                result = ("провал",
                          "схема: исключение парсера на канарейке (%s)" % type(e).__name__)
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
        with _CANARY_LOCK:
            _CANARY_CACHE[key] = result
        return result


def run_source(source_id, inn, opener=None, профиль=None, контекст=None):
    """Один источник по реестру: профиль -> браузер -> probe-кэш -> fetcher ->
    канарейка. Всегда (данные|None, _доступность); исключения — в «не проверено».

    `контекст` — уже собранные блоки (id источника -> данные) для источников с
    `зависит_от` в дескрипторе; остальным он не передаётся.
    """
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
        data, av = _call_fetcher(source_id, opener, inn, контекст)
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


# ---------------------------------------------------------------------------
# Фазы сбора, параллелизм, ранний выход (волна 2, §1)
# ---------------------------------------------------------------------------

РЕЖИМЫ = ("quick", "полный", "всё")
MAX_WORKERS = 4                 # уважение к ФНС: не больше четырёх запросов разом
_THREAD_LOCAL = threading.local()


def _sequential():
    """COUNTERPARTY_SEQUENTIAL=1 — последовательный обход (отладка, воспроизводимость)."""
    return os.environ.get("COUNTERPARTY_SEQUENTIAL") == "1"


def reset_deadline():
    """Перезапустить общий бюджет времени.

    DEADLINE отсчитывается от загрузки модуля, а collect() могут звать несколько раз
    подряд (батч, MCP-сервер) — тогда второй вызов стартовал бы с уже потраченным
    бюджетом. Сбрасывается один раз на collect(), поэтому внутри сбора дедлайн
    остаётся ОБЩИМ на обе фазы и на все потоки, как требует §1.3.
    """
    global _START
    _START = time.monotonic()


def _thread_opener():
    """Свой opener на поток: http.cookiejar.CookieJar не потокобезопасен (§1.3)."""
    op = getattr(_THREAD_LOCAL, "opener", None)
    if op is None:
        op = _THREAD_LOCAL.opener = _make_opener()
    return op


def _run_wave(ids, inn, профиль, контекст, opener):
    """Одна волна источников -> [(id, (данные, _доступность))] в порядке ids.

    Параллельно (ThreadPoolExecutor, максимум MAX_WORKERS потоков) — источники
    независимы; при COUNTERPARTY_SEQUENTIAL=1 или одном источнике — в лоб.
    Ни один поток не переживает исчерпание бюджета: все сетевые вызовы внутри
    ходят через _http_get/_http_post, которые сверяются с общим _time_left()
    перед каждым запросом и ограничивают им socket timeout.
    """
    if not ids:
        return []
    if len(ids) == 1 or _sequential():
        return [(sid, run_source(sid, inn, opener=opener or _make_opener(),
                                 профиль=профиль, контекст=контекст))
                for sid in ids]

    def задача(sid):
        # opener берётся ленивым per-thread: источникам «кэш»/«браузер» он не нужен.
        return run_source(sid, inn, opener=_thread_opener(), профиль=профиль,
                          контекст=контекст)

    with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(MAX_WORKERS, len(ids)),
            thread_name_prefix="источник") as pool:
        futures = {sid: pool.submit(задача, sid) for sid in ids}
        out = []
        for sid in ids:
            try:
                out.append((sid, futures[sid].result()))
            except Exception as e:          # run_source сам не бросает; страховка
                out.append((sid, _not_checked(
                    sid, "схема: сбой параллельного сбора %s: %s" % (type(e).__name__, e))))
    return out


def _collect_phase(фаза, inn, профиль, result, av_map, opener=None):
    """Собирает источники одной фазы в result/av_map. -> список собранных id.

    Внутри фазы два захода: сначала независимые источники, затем те, у кого в
    дескрипторе есть «зависит_от» (им нужен контекст уже собранных блоков —
    так «спецреестры» получают ФИО руководителя из «егрюл»).
    """
    ids = [sid for sid in SOURCES if SOURCES[sid].get("фаза") == фаза]
    волны = ([sid for sid in ids if not sources.depends_on(sid)],
             [sid for sid in ids if sources.depends_on(sid)])
    собрано = []
    for волна in волны:
        контекст = {sid: result.get(sid) for sid in собрано}
        for sid, (data, av) in _run_wave(волна, inn, профиль, контекст, opener):
            result[sid] = data
            av_map[sid] = av
            собрано.append(sid)
    return собрано


def _skip_phase(фаза, причина, result, av_map):
    """Источники несобранной фазы -> «не проверено» с общей причиной."""
    for sid in SOURCES:
        if SOURCES[sid].get("фаза") == фаза:
            result[sid], av_map[sid] = _not_checked(sid, причина)


def _светофор_после_quick(result, av_map, профиль):
    """profiles.resolve() на собранной quick-фазе. -> (светофор, сигналы, причина сбоя).

    fin_json не считается: финансы живут в досье-фазе, а резолвер принимает None
    (финансовые сигналы просто станут «не проверен»). Резолвер импортируется
    мягко: нет модуля или он упал — раннего выхода просто не происходит.
    """
    try:
        profiles = _load_sibling("profiles")
    except Exception as e:
        return None, [], "резолвер профилей не импортируется (%s)" % type(e).__name__
    частичный = dict(result)
    частичный["_доступность"] = dict(av_map)
    частичный["_итог_проверки"] = build_summary(av_map)
    try:
        вердикт = profiles.resolve(частичный, None, профиль or "нейтрально")
    except Exception as e:
        return None, [], "резолвер профилей упал (%s: %s)" % (type(e).__name__, e)
    if not isinstance(вердикт, dict):
        return None, [], "резолвер профилей вернул не-словарь"
    сигналы = [s.get("сигнал") for s in (вердикт.get("поднят_сигналами") or [])
               if isinstance(s, dict)]
    return вердикт.get("светофор"), [x for x in сигналы if x], None


def collect(inn, профиль=None, режим="полный", opener=None):
    """Сбор по фазам -> полный результат (без записи снимка).

    режим (§1.2):
      quick  — только quick-фаза, затем стоп;
      полный — quick-фаза, проверка на deal-killer, затем досье-фаза;
      всё    — обе фазы без раннего выхода (снимки мониторинга: диффу нужны все поля).

    Профиль «нейтрально» светофора не выдаёт (формулировка «факты»), поэтому
    ранний выход в нём не срабатывает никогда — это осознанно: нейтральный режим
    показывает факты, а не отсеивает.
    """
    if режим is None:
        режим = "полный"
    if режим not in РЕЖИМЫ:
        режим = "полный"
    reset_deadline()
    начало = time.monotonic()
    if opener is None and _sequential():
        opener = _make_opener()
    result = {"инн": inn, "тип": _тип_контрагента(inn), "профиль": профиль or "нейтрально"}
    # Ключи блоков заводятся заранее, в порядке SOURCES: фазы наполняют их вразнобой,
    # а формат вывода (и его порядок) остаётся тем же, что до волны 2.
    for sid in SOURCES:
        result[sid] = None
    av_map = {}

    собрано = _collect_phase("quick", inn, профиль, result, av_map, opener)
    фаза_остановки, ранний_выход, причина = "quick", False, None

    if режим == "quick":
        причина = "режим quick: досье-фаза не запрашивалась"
        _skip_phase("досье", "режим: quick — досье-фаза не собиралась", result, av_map)
    else:
        светофор, сигналы, сбой = (None, [], None)
        if режим == "полный":
            светофор, сигналы, сбой = _светофор_после_quick(result, av_map, профиль)
        if режим == "полный" and светофор == "🔴":
            ранний_выход = True
            причина = "ранний выход: deal-killer найден на quick-фазе"
            if сигналы:
                причина += " (%s)" % ", ".join(сигналы)
            _skip_phase("досье", "ранний выход: deal-killer найден на quick-фазе",
                        result, av_map)
        else:
            собрано += _collect_phase("досье", inn, профиль, result, av_map, opener)
            фаза_остановки = "досье"
            причина = ("обе фазы собраны" if режим != "всё"
                       else "режим всё: ранний выход отключён, обе фазы собраны")
            if сбой:
                причина += " (проверка на deal-killer не делалась: %s)" % сбой

    # egrul не отдаёт статус (k — вид субъекта): текстовый статус — из pb.
    egrul, risks = result.get("егрюл"), result.get("риски")
    if isinstance(egrul, dict) and isinstance(risks, dict) and risks.get("статус"):
        egrul["статус"] = risks["статус"]
        egrul["статус_источник"] = "pb.nalog.ru (sulst_name_ex)"
    result["_доступность"] = av_map
    result["_итог_проверки"] = build_summary(av_map)
    result["_сбор"] = {
        "режим": режим,
        "фаза_остановки": фаза_остановки,
        "секунд": round(time.monotonic() - начало, 2),
        "источников_собрано": len(собрано),
        "ранний_выход": ранний_выход,
        "причина_остановки": причина,
        "параллельно": not _sequential(),
    }
    result["_сеть"] = proxy.описание(
        _ПРОКСИ, insecure=os.environ.get("COUNTERPARTY_INSECURE") == "1")
    if _КЭШ_ДОСТУПА_ОТКЛОНЁН:
        result["_сеть"]["кэш_доступа"] = "отклонён: " + _КЭШ_ДОСТУПА_ОТКЛОНЁН
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


def save_snapshot(inn, result, полный=False):
    """Снимок проверки для мониторинга (diff_counterparty.py).

    По умолчанию сохраняется нормализованный ОТПЕЧАТОК (scripts/snapshot.py):
    поля, по которым идёт дифф, плюс состояния источников и SHA-256 остального.
    Он на порядок меньше и не тащит в хранилище чужие персданные, которые для
    мониторинга не нужны. `--снимок полный` возвращает старое поведение (весь
    JSON) — на случай, когда нужен исходник целиком.

    ~/.cache/inn-check-ru/snapshots/<ИНН>/<дата>_<время>.json — по снимку на
    запуск; сравниваются два последних (diff читает оба формата). Не падает:
    сбой записи — предупреждение в stderr, JSON в stdout всё равно уходит.
    """
    if not полный:
        try:
            snapshot = _load_sibling("snapshot")
            путь = snapshot.save(inn, result)
            if путь:
                sys.stderr.write("отпечаток сохранён: %s\n" % путь)
            return путь
        except Exception as e:
            sys.stderr.write(
                "ВНИМАНИЕ: отпечаток не сделан (%s) — сохраняю полный снимок\n"
                % type(e).__name__)
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


USAGE = ("Использование: python3 fetch_counterparty.py <ИНН> [--save [--снимок полный]]\n"
         "                                            [--профиль <id>]\n"
         "                                            [--режим quick|полный|всё]\n"
         "                                            [--прокси http://host:3128]\n"
         "               python3 fetch_counterparty.py --канарейки\n"
         "Профили: %s\n"
         "Режимы: quick — только быстрая фаза (егрюл, риски, спецреестры, санкции);\n"
         "        полный — quick, и если deal-killer не найден, досье-фаза "
         "(по умолчанию);\n"
         "        всё — обе фазы без раннего выхода (снимки мониторинга).\n"
         "        --quick — синоним --режим quick.\n"
         "--save: отпечаток для мониторинга (--режим всё подставляется сам);\n"
         "        --снимок полный — сохранить весь JSON, как раньше.\n"
         "--прокси: свой http/https-прокси (или INN_CHECK_PROXY / HTTPS_PROXY).\n"
         "        Узел видит, какие ИНН вы проверяете, — только своя нода.\n"
         % ", ".join(sources.PROFILE_IDS))


def _parse_args(argv):
    args, do_save, профиль, canaries, режим = [], False, None, False, None
    снимок = None
    прокси = None
    it = iter(argv[1:])
    for a in it:
        if a in ("--прокси", "--proxy"):
            прокси = next(it, None)
        elif a.startswith(("--прокси=", "--proxy=")):
            прокси = a.split("=", 1)[1]
        elif a == "--save":
            do_save = True
        elif a in ("--снимок", "--snapshot"):
            снимок = next(it, None)
        elif a.startswith(("--снимок=", "--snapshot=")):
            снимок = a.split("=", 1)[1]
        elif a == "--канарейки":
            canaries = True
        elif a in ("--профиль", "--profile"):
            профиль = next(it, None)
        elif a.startswith(("--профиль=", "--profile=")):
            профиль = a.split("=", 1)[1]
        elif a == "--quick":
            режим = "quick"
        elif a in ("--режим", "--mode"):
            режим = next(it, None)
        elif a.startswith(("--режим=", "--mode=")):
            режим = a.split("=", 1)[1]
        else:
            args.append(a)
    return args, do_save, профиль, canaries, режим, снимок, прокси


def main(argv):
    args, do_save, профиль, canaries, режим, снимок, прокси = _parse_args(argv)
    ошибка_прокси = установить_прокси(прокси)
    if ошибка_прокси:
        sys.stdout.write(json.dumps(
            {"ошибка": "прокси: " + ошибка_прокси}, ensure_ascii=False, indent=2) + "\n")
        return 2
    if _ПРОКСИ["url"]:
        sys.stderr.write("сеть: через прокси %s (%s)\n"
                         % (_ПРОКСИ["маска"], _ПРОКСИ["источник"]))
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
    if режим is not None and режим not in РЕЖИМЫ:
        sys.stdout.write(json.dumps(
            {"ошибка": "неизвестный режим %r; допустимые: %s"
                       % (режим, ", ".join(РЕЖИМЫ))},
            ensure_ascii=False, indent=2) + "\n")
        return 2
    inn = validate_inn(args[0])
    if inn is None:
        out = {"ошибка": "Некорректный ИНН: ожидается 10 или 12 цифр с верным "
                         "контрольным числом (алгоритм ФНС) — вероятна опечатка",
               "ввод": args[0]}
        sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
        return 1

    if снимок is not None and снимок not in ("отпечаток", "полный"):
        sys.stdout.write(json.dumps(
            {"ошибка": "неизвестный вид снимка %r; допустимые: отпечаток, полный"
                       % снимок}, ensure_ascii=False, indent=2) + "\n")
        return 2
    # Снимок мониторинга должен быть полным: дифф сравнивает поля, а ранний
    # выход оставил бы половину блоков «не проверено» — следующий прогон
    # показал бы не изменения контрагента, а разницу режимов сбора.
    if do_save and режим is None:
        режим = "всё"
    elif do_save and режим != "всё":
        sys.stderr.write(
            "ВНИМАНИЕ: снимок в режиме %r неполный — для мониторинга "
            "используйте --режим всё\n" % режим)

    result = collect(inn, профиль=профиль, режим=режим or "полный")
    if do_save:
        save_snapshot(inn, result, полный=(снимок == "полный"))
    sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))


def cli():
    """console_script `inn-check-ru` (pyproject.toml) — та же main(sys.argv)."""
    return sys.exit(main(sys.argv))
