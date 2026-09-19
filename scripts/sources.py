#!/usr/bin/env python3
"""
sources.py — реестр источников inn-check-ru. Один дескриптор на источник, plain dict без
callable: реестр импортируется и probe-скриптом (check_access.py), и сетевым движком
(fetch_counterparty.py, где FETCHERS сопоставляет id -> функция сбора).

Контракт дескриптора — docs/superpowers/specs/2026-09-19-wave1-sources-registry-design.md, §2:
    название      — человекочитаемое имя и хост
    tier          — 🟢 бесплатно скриптом | 🟡 кэш/ключ | 🔴 браузер | ⚪ не покрыто
    требует       — сеть | кэш | браузер | ключ
    deal_killer   — участвует в _итог_проверки (проверка «состоялась» / «не состоялась»)
    профили       — "*" или список id из data/profiles_ru.json
    probe         — лёгкий запрос для check_access.py: url, method, ok_http, ожидаем
    контракт      — поля сырой записи, без которых блок НЕ может быть «ok» (схема изменилась)
    канарейка     — ИНН с известным непустым ответом + какие распарсенные поля обязаны быть непустыми
    при_отказе    — не проверено | пропустить
    документация  — ссылка на первоисточник

Добавить источник = одна запись здесь + одна функция в fetch_counterparty.FETCHERS + фикстура
в eval/fixtures/net/. Никакой другой правки main() не требуется.

Живой снимок контрактов — 19.09.2026, не-РФ IP (см. спек, §0).
"""

SOURCES = {
    "егрюл": {
        "название": "ЕГРЮЛ/ЕГРИП — карточка (egrul.nalog.ru)",
        "tier": "🟢",
        "требует": "сеть",
        "deal_killer": True,
        "профили": "*",
        "probe": {"url": "https://egrul.nalog.ru/", "method": "GET",
                  "ok_http": [200, 307], "ожидаем": "html"},
        # сырой ряд search-result/<t>.rows[0]; полей a/e/tp в ответе НЕТ (снято 19.09.2026)
        "контракт": ["n", "o", "i", "g", "r", "k"],
        "канарейка": {"инн": "7707083893",
                      "ожидаем_непустые": ["наименование_полное", "огрн", "руководитель"]},
        "при_отказе": "не проверено",
        "документация": "https://egrul.nalog.ru/",
    },
    "риски": {
        "название": "Прозрачный бизнес — риск-флаги ФНС (pb.nalog.ru)",
        "tier": "🟢",
        "требует": "сеть",
        "deal_killer": True,
        "профили": "*",
        "probe": {"url": "https://pb.nalog.ru/search-proc.json?mode=search-ul&queryUl=7707083893",
                  "method": "GET", "ok_http": [200], "ожидаем": "json"},
        # сырой ряд ul.data[0] поиска; детальные флаги (долг, массовость, ССЧ, спецрежим)
        # лежат за token в company-proc.json — get-response даёт 500 с не-РФ IP
        "контракт": ["inn", "sulst_name_ex", "pr_liq", "invalid", "okved2main", "dtreg"],
        "канарейка": {"инн": "7707083893",
                      "ожидаем_непустые": ["статус", "недостоверность_сведений", "оквэд"]},
        "при_отказе": "не проверено",
        "документация": "https://pb.nalog.ru/",
    },
    "финансы": {
        "название": "ГИР БО — бухотчётность (bo.nalog.gov.ru)",
        "tier": "🟢",
        "требует": "сеть",
        "deal_killer": False,
        "профили": ["нейтрально", "отсрочка", "предоплата", "подрядчик", "доля",
                    "самопроверка"],
        "probe": {"url": "https://bo.nalog.gov.ru/advanced-search/organizations/search"
                         "?query=7707083893&page=0",
                  "method": "GET", "ok_http": [200], "ожидаем": "json"},
        "контракт": ["id", "inn", "shortName"],
        # 5036045205 — три года с реальными строками 2110/1300 (снято 19.09.2026)
        "канарейка": {"инн": "5036045205",
                      "ожидаем_непустые": ["отчётность_по_годам"]},
        "при_отказе": "не проверено",
        "документация": "https://bo.nalog.gov.ru/",
    },
    "мсп": {
        "название": "Единый реестр МСП (rmsp.nalog.ru)",
        "tier": "🟢",
        "требует": "сеть",
        "deal_killer": False,
        "профили": "*",
        "probe": {"url": "https://rmsp.nalog.ru/search-proc.json?query=7707083893",
                  "method": "GET", "ok_http": [200], "ожидаем": "json"},
        "контракт": ["inn", "category", "is_active", "nptype"],
        "канарейка": {"инн": "504110181262",
                      "ожидаем_непустые": ["статус_мсп", "категория"]},
        "при_отказе": "не проверено",
        "документация": "https://rmsp.nalog.ru/",
    },
    "нпд": {
        "название": "Статус самозанятого — НПД (npd.nalog.ru check-status)",
        "tier": "🟢",
        "требует": "сеть",
        "deal_killer": False,
        "профили": "*",
        "probe": {"url": "https://npd.nalog.ru/check-status/", "method": "GET",
                  "ok_http": [200], "ожидаем": "html"},
        "контракт": ["status"],
        "канарейка": None,  # живой прогон только с РФ-IP (406/403 с не-РФ)
        "при_отказе": "не проверено",
        "документация": "https://npd.nalog.ru/check-status/",
    },
    "спецреестры": {
        "название": "Спецреестры ФНС: дисквалификация, задолженность, недостоверность "
                    "(service.nalog.ru)",
        "tier": "🟢",
        "требует": "сеть",
        "deal_killer": True,
        "профили": "*",
        "probe": {"url": "https://service.nalog.ru/disqualified.do", "method": "GET",
                  "ok_http": [200], "ожидаем": "html"},
        "контракт": ["t"],
        "канарейка": None,  # эндпоинт не верифицирован (POST не возвращает token)
        "при_отказе": "не проверено",
        "документация": "https://service.nalog.ru/",
    },
    "еркнм": {
        "название": "ЕРКНМ — плановые проверки (proverki.gov.ru, кэш дампов)",
        "tier": "🟡",
        "требует": "кэш",
        "deal_killer": False,
        "профили": ["нейтрально", "предоплата", "подрядчик", "самопроверка"],
        "probe": {"url": "https://proverki.gov.ru/portal/public-open-data", "method": "GET",
                  "ok_http": [200], "ожидаем": "html"},
        "контракт": [],
        "канарейка": None,
        "при_отказе": "не проверено",
        "документация": "https://proverki.gov.ru/portal/public-open-data",
    },
    "рнп": {
        "название": "РНП — недобросовестные поставщики (zakupki.gov.ru, кэш дампов)",
        "tier": "🟡",
        "требует": "кэш",
        "deal_killer": False,
        "профили": ["нейтрально", "предоплата", "подрядчик", "самопроверка"],
        "probe": {"url": "https://zakupki.gov.ru/epz/opendata", "method": "GET",
                  "ok_http": [200], "ожидаем": "html"},
        "контракт": [],
        "канарейка": None,
        "при_отказе": "не проверено",
        "документация": "https://zakupki.gov.ru/epz/opendata",
    },
    "санкции": {
        "название": "Перечни РФМ / OFAC SDN / EU (sanctions_check.py, кэш)",
        "tier": "🟡",
        "требует": "кэш",
        "deal_killer": True,
        "профили": "*",
        "probe": {"url": "https://www.fedsfm.ru/documents/terrorists-catalog-portal-act",
                  "method": "GET", "ok_http": [200], "ожидаем": "html"},
        "контракт": [],
        "канарейка": None,
        "при_отказе": "не проверено",
        "документация": "https://www.fedsfm.ru/",
    },
    "фссп": {
        "название": "ФССП — исполнительные производства (fssp.gov.ru)",
        "tier": "🔴",
        "требует": "браузер",
        "deal_killer": True,
        "профили": "*",
        "probe": {"url": "https://fssp.gov.ru/", "method": "GET",
                  "ok_http": [200], "ожидаем": "html"},
        "контракт": [],
        "канарейка": None,
        "при_отказе": "не проверено",
        "документация": "https://fssp.gov.ru/",
    },
    "суды": {
        "название": "Картотека арбитражных дел (kad.arbitr.ru)",
        "tier": "🔴",
        "требует": "браузер",
        "deal_killer": True,
        "профили": "*",
        "probe": {"url": "https://kad.arbitr.ru/", "method": "GET",
                  "ok_http": [200], "ожидаем": "html"},
        "контракт": [],
        "канарейка": None,
        "при_отказе": "не проверено",
        "документация": "https://kad.arbitr.ru/",
    },
    "банкротство": {
        "название": "ЕФРСБ — банкротства (bankrot.fedresurs.ru)",
        "tier": "🔴",
        "требует": "браузер",
        "deal_killer": True,
        "профили": "*",
        "probe": {"url": "https://bankrot.fedresurs.ru/", "method": "GET",
                  "ok_http": [200], "ожидаем": "html"},
        "контракт": [],
        "канарейка": None,
        "при_отказе": "не проверено",
        "документация": "https://bankrot.fedresurs.ru/",
    },
}

# Источники с TLS-цепочкой Национального УЦ Минцифры — probe различает «tls» от «гео»,
# check_access подсказывает scripts/install_ca.py. Не входят в сбор fetch_counterparty.
TLS_MINCIFRY_PROBES = {
    "fedsfm": "https://www.fedsfm.ru/documents/terrorists-catalog-portal-act",
    "rosstat": "https://rosstat.gov.ru/opendata",
    "npchk": "https://npchk.nalog.ru/",
}

PROFILE_IDS = ("нейтрально", "отсрочка", "предоплата", "подрядчик", "доля",
               "клиент_115фз", "самопроверка")


def deal_killer_ids():
    return [k for k, v in SOURCES.items() if v.get("deal_killer")]


def sources_for_profile(profile_id):
    """id источников, нужных профилю; "*" — во всех профилях."""
    out = []
    for k, v in SOURCES.items():
        p = v.get("профили", "*")
        if p == "*" or profile_id in p:
            out.append(k)
    return out


def validate():
    """Самопроверка реестра: обязательные ключи и допустимые значения."""
    errors = []
    req = ("название", "tier", "требует", "deal_killer", "профили", "probe",
           "контракт", "канарейка", "при_отказе", "документация")
    for sid, d in SOURCES.items():
        for k in req:
            if k not in d:
                errors.append("%s: нет ключа %s" % (sid, k))
        if d.get("tier") not in ("🟢", "🟡", "🔴", "⚪"):
            errors.append("%s: tier %r" % (sid, d.get("tier")))
        if d.get("требует") not in ("сеть", "кэш", "браузер", "ключ"):
            errors.append("%s: требует %r" % (sid, d.get("требует")))
        p = d.get("профили")
        if p != "*" and not (isinstance(p, list) and all(x in PROFILE_IDS for x in p)):
            errors.append("%s: профили %r" % (sid, p))
        if d.get("при_отказе") not in ("не проверено", "пропустить"):
            errors.append("%s: при_отказе %r" % (sid, d.get("при_отказе")))
    return errors


if __name__ == "__main__":
    import json
    import sys
    errs = validate()
    if errs:
        sys.stderr.write("\n".join(errs) + "\n")
        sys.exit(1)
    sys.stdout.write(json.dumps(
        {"источников": len(SOURCES), "deal_killer": deal_killer_ids(),
         "профили": list(PROFILE_IDS)}, ensure_ascii=False, indent=2) + "\n")
