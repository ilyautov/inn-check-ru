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
    фаза          — quick | досье (волна 2, §1.1): quick — источники, способные дать
                    deal-killer дёшево; досье — всё остальное. Браузерные источники
                    стоят в quick: они не ходят в сеть, а их «не покрыто» обязано
                    попасть в _итог_проверки уже после быстрой фазы
    зависит_от    — (необязательно) id источников, чьи данные нужны сборщику как
                    контекст; внутри фазы такие источники собираются вторым заходом
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
        "фаза": "quick",
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
        "фаза": "quick",
        "профили": "*",
        "probe": {"url": "https://pb.nalog.ru/search-proc.json?mode=search-ul&queryUl=7707083893",
                  "method": "GET", "ok_http": [200], "ожидаем": "json"},
        # сырой ряд ul.data[0] поиска; детальные флаги (долг, массовость, ССЧ, спецрежим)
        # лежат за token в company-proc.json — get-response даёт 500 с не-РФ IP
        "контракт": ["inn", "sulst_name_ex", "pr_liq", "invalid", "okved2main", "dtreg"],
        # ИП (mode=search-ip, ряд ip.data[0]) — ДРУГОЙ набор полей: sulst_name_ex/pr_liq/
        # invalid/dtreg не отдаются (снято 19.09.2026, фикстура риски_504110181262)
        "контракт_ип": ["inn", "namec", "okved2main", "dtogrn"],
        "канарейка": {"инн": "7707083893",
                      "ожидаем_непустые": ["статус", "оквэд"]},
        "канарейка_ип": {"инн": "504110181262",
                         "ожидаем_непустые": ["наименование", "оквэд"]},
        "при_отказе": "не проверено",
        "документация": "https://pb.nalog.ru/",
    },
    "финансы": {
        "название": "ГИР БО — бухотчётность (bo.nalog.gov.ru)",
        "tier": "🟢",
        "требует": "сеть",
        "deal_killer": False,
        "фаза": "досье",
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
        "фаза": "досье",
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
        "фаза": "досье",
        "профили": "*",
        "probe": {"url": "https://npd.nalog.ru/check-status/", "method": "GET",
                  "ok_http": [200], "ожидаем": "html"},
        "контракт": ["status"],
        "канарейка": None,  # живой прогон только с РФ-IP (406/403 с не-РФ)
        "при_отказе": "не проверено",
        "документация": "https://npd.nalog.ru/check-status/",
    },
    "спецреестры": {
        "название": "Реестр дисквалифицированных лиц ФНС (service.nalog.ru/disqualified.do)",
        "tier": "🟢",
        "требует": "сеть",
        "deal_killer": True,
        "фаза": "quick",
        "профили": "*",
        # Реестр ищется ПО ФИО/наименованию, а не по ИНН (см. «разведка» ниже), поэтому
        # сборщику нужен руководитель из блока «егрюл»: внутри quick-фазы источник
        # собирается вторым заходом, после того как егрюл отдал карточку.
        "зависит_от": ["егрюл"],
        "probe": {"url": "https://service.nalog.ru/disqualified.do", "method": "GET",
                  "ok_http": [200], "ожидаем": "html"},
        # Поля записи ответа disqualified-proc.json (снято живьём 19.09.2026).
        "контракт": ["ФИО", "ДатаРожд", "НаимОрг", "Должность", "КвалификацияТекст",
                     "ДатаНачДискв", "ДатаКонДискв"],
        # Канарейка по ИНН невозможна (реестр ИНН не индексирует), а канарейка по ФИО
        # протухает вместе со сроком дисквалификации. Вместо неё — ВСТРОЕННАЯ
        # самопроверка в сетевом слое: на пустом результате делается запрос с пустым
        # query, который обязан вернуть весь реестр (rowCount > 0, 19.09.2026 — 8188).
        # Ноль там означает «эндпоинт/схема сломались», а не «дисквалификации нет».
        "канарейка": None,
        "при_отказе": "не проверено",
        "документация": "https://service.nalog.ru/disqualified.do",
        # Живая разведка 19.09.2026 со шведского IP (волна 2, §1.4). Дословно: что
        # запрашивали и что ответило. Ничего не додумано.
        "разведка": {
            "https://service.nalog.ru/disqualified.do":
                "HTTP 200, HTML; форма frmList method=post action=disqualified-proc.json, "
                "поля query/page/pageSize/m/fam/nam/otch/bd/bp, CAPTCHA_REQUIRED=false — РАБОТАЕТ",
            "https://service.nalog.ru/disqualified-proc.json":
                "HTTP 200 application/json сразу, без token и поллинга: "
                "{data:[…], rowCount, pageCount, pageSize, validPageSizes, rowLimit, "
                "queryHash, queryTime, dtQueryBegin, dtQueryEnd}; запись — "
                "ROW_NUM, ДатаФорм, КолЗап, НомЗап, ФИО, ДатаРожд, МестоРожд, НаимОрг, "
                "Должность, КвалификацияТекст, НаимОргПрот, ФИОСуд, ДолжностьСуд, "
                "ДисквСрок, ДатаНачДискв, ДатаКонДискв, row_cnt",
            "поиск по ИНН ЮЛ":
                "НЕ РАБОТАЕТ, хотя placeholder формы обещает «ИНН ЮЛ»: проверено на пяти "
                "организациях, чей действующий руководитель есть в реестре "
                "(5836898322 ООО «ТЕКСПРОМ» — Иванов А. Б.; 2511110744; 6317140840; "
                "2616010031; 1655217944) — во всех случаях rowCount=0. "
                "Поиск по ФИО и по наименованию организации при этом находит те же записи. "
                "Значит, ИНН в индексе реестра не заполнен — сверка идёт по ФИО",
            "https://service.nalog.ru/zd.do":
                "HTTP 200, HTML: «Сервис \u00abСведения о юридических лицах, имеющих "
                "задолженность по уплате налогов и/или не представляющих налоговую "
                "отчётность более года\u00bb выведен из эксплуатации. Информация доступна "
                "в сервисе \u00abПрозрачный бизнес\u00bb» — покрывается блоком «риски»",
            "https://service.nalog.ru/invalid-addresses.do":
                "HTTP 200 после редиректа на https://service.nalog.ru/payment/ — "
                "такого сервиса нет; признак недостоверности сведений отдаёт pb "
                "(поле invalid) и он уже разбирается в блоке «риски»",
            "https://service.nalog.ru/mri.do": "редирект на /payment/ — сервиса нет",
            "https://service.nalog.ru/mru.do":
                "редирект на https://pb.nalog.ru/ — массовые руководители/учредители "
                "переехали в «Прозрачный бизнес» (за token в company-proc.json)",
            "https://service.nalog.ru/addrfind.do": "редирект на https://pb.nalog.ru/",
            "https://service.nalog.ru/baddr.do":
                "редирект на /service-closed.html?svc=baddr — сервис закрыт",
            "https://service.nalog.ru/svl.do":
                "HTTP 200, HTML: «Сервис выведен из эксплуатации с 09.06.2023»",
            "https://service.nalog.ru/uwsfind.do":
                "HTTP 200, форма action=uwsfind-proc.json (документы, поданные на "
                "госрегистрацию, в т. ч. Р15016 «ликвидация»), но поля captcha/captchaToken "
                "обязательны — скриптом не берётся",
            "старая схема <path>/proc.json + search-result/<t>":
                "неверна: она у egrul.nalog.ru, а не у service.nalog.ru; "
                "service.nalog.ru/dismissal|zd|invalid/proc.json отдавали HTML 200",
        },
    },
    "еркнм": {
        "название": "ЕРКНМ — плановые проверки (proverki.gov.ru, кэш дампов)",
        "tier": "🟡",
        "требует": "кэш",
        "deal_killer": False,
        "фаза": "досье",
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
        "фаза": "досье",
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
        "фаза": "quick",
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
        "фаза": "quick",
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
        "фаза": "quick",
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
        "фаза": "quick",
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

# Фазы сбора (волна 2, §1.1). Порядок значим: quick собирается первой, и только если
# после неё нет deal-killer'а, движок идёт в досье-фазу.
PHASES = ("quick", "досье")


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


def sources_for_phase(фаза):
    """id источников указанной фазы, в порядке SOURCES."""
    return [k for k, v in SOURCES.items() if v.get("фаза") == фаза]


def depends_on(source_id):
    """id источников, чьи данные нужны сборщику как контекст (пустой список — нет)."""
    return list(SOURCES.get(source_id, {}).get("зависит_от") or [])


def validate():
    """Самопроверка реестра: обязательные ключи и допустимые значения."""
    errors = []
    req = ("название", "tier", "требует", "deal_killer", "фаза", "профили", "probe",
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
        if d.get("фаза") not in PHASES:
            errors.append("%s: фаза %r (допустимы: %s)"
                          % (sid, d.get("фаза"), ", ".join(PHASES)))
        for dep in d.get("зависит_от") or []:
            if dep not in SOURCES:
                errors.append("%s: зависит_от %r — нет такого источника" % (sid, dep))
            elif SOURCES[dep].get("фаза") != d.get("фаза"):
                errors.append("%s: зависит_от %r из другой фазы (%r vs %r) — "
                              "контекст не успеет собраться"
                              % (sid, dep, SOURCES[dep].get("фаза"), d.get("фаза")))
            elif dep == sid:
                errors.append("%s: зависит_от самого себя" % sid)
    # Цикл зависимостей внутри фазы: сбор идёт двумя заходами, длиннее цепочки нет.
    for sid, d in SOURCES.items():
        for dep in d.get("зависит_от") or []:
            if dep in SOURCES and (SOURCES[dep].get("зависит_от") or []):
                errors.append("%s: зависит от %s, который сам зависим — "
                              "цепочки длиннее одного шага сбор не поддерживает"
                              % (sid, dep))
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
         "профили": list(PROFILE_IDS),
         "фазы": {ф: sources_for_phase(ф) for ф in PHASES}},
        ensure_ascii=False, indent=2) + "\n")
