#!/usr/bin/env python3
"""
rkn_registries.py — реестры Роскомнадзора по ИНН, офлайн. Только stdlib.

Открытые данные РКН (rkn.gov.ru/opendata) — XML со страниц наборов. Сайт закрыт
для не-РФ адресов: скачивание идёт через прокси пользователя (INN_CHECK_PROXY, в
т.ч. socks5; см. dump_tools.py). Живьём 28.09.2026:

    7705846236-LicComm                 реестр лицензий в области связи: 195 681
                                       лицензия (19 290 действующих), ИНН у 191 479;
                                       ~245 МБ, обновляется ежедневно
    7705846236-InformationDistributor  реестр организаторов распространения
                                       информации (ОРИ): 471 запись, ИНН у 303
                                       (остальные — иностранные компании)

По ИНН — лицензии связи со статусом («действующая», «недействующая»,
«приостановленная» — как в реестре), сроками и услугой; запись в реестре ОРИ с
номером, датой и доменами сервисов. В реестре ОРИ исключённых нет — «записи нет»
значит «сейчас не в реестре».

В индекс не попадают наименования (у ИП это ФИО), адреса, e-mail и ФИО
ответственных — только ИНН, номера, даты, статусы, услуги и домены.

XML разбирается regex'ом по записям, потоком: pyexpat на части машин сломан, а
записи плоские и однозначные. Файл лицензий качается во временный файл кэша и
после разбора удаляется.

    python3 rkn_registries.py --refresh      # нужен РФ-IP или INN_CHECK_PROXY
    python3 rkn_registries.py --inn 7736207543
"""

import html
import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dump_tools

CACHE_DIR = os.path.expanduser(os.path.join("~", ".cache", "inn-check-ru", "rkn"))
CACHE_DIR_ВИДИМЫЙ = "~/.cache/inn-check-ru/rkn"
ИНДЕКС = "индекс.json.gz"
САЙТ = "https://rkn.gov.ru"
РЕЕСТРЫ = {
    "лицензии_связи": {"набор": "7705846236-LicComm",
                       "название": "Реестр лицензий в области связи",
                       "лимит": 1 << 30},
    "ори": {"набор": "7705846236-InformationDistributor",
            "название": "Реестр организаторов распространения информации в сети «Интернет»",
            "лимит": 50 << 20},
}
СТАТУСЫ = {"действующая", "недействующая", "приостановленная"}
ДОЛЯ_ПАДЕНИЯ = 0.9          # реестры только растут: меньше 90 % прежнего — обрыв/схема
ДОЛЯ_ИНН_ЛИЦЕНЗИЙ = 0.9     # ИНН у 98 % лицензий; меньше 90 % — схема не та
СВЕЖЕСТЬ_ДНЕЙ = 30
ПОКАЗАТЬ = 20


def страница(реестр):
    return "%s/opendata/%s/" % (САЙТ, РЕЕСТРЫ[реестр]["набор"])


def ссылка_на_данные(html_страницы, реестр):
    набор = re.escape(РЕЕСТРЫ[реестр]["набор"])
    ссылки = set(re.findall(r'href="(https://rkn\.gov\.ru/opendata/%s/'
                            r'data-(\d{8})T\d{4}-structure-[^"/]+\.xml)"' % набор,
                            html_страницы))
    if not ссылки:
        raise ValueError("схема: на странице набора %s нет ссылки на data-*.xml" % реестр)
    return max(ссылки, key=lambda с: с[1])


# --- разбор -----------------------------------------------------------------

def записи_xml(куски):
    """Итератор текстовых кусков -> тела <rkn:record>…</rkn:record> по одному.
    Без закрывающего </rkn:register> в конце — обрыв (ревью Codex: поток без
    Content-Length иначе прошёл бы как полный)."""
    хвост = ""
    for кусок in куски:
        хвост += кусок
        while True:
            конец = хвост.find("</rkn:record>")
            if конец < 0:
                break
            начало = хвост.rfind("<rkn:record>", 0, конец)
            if начало < 0:
                raise ValueError("схема: </rkn:record> без открывающего тега")
            yield хвост[начало + len("<rkn:record>"):конец]
            хвост = хвост[конец + len("</rkn:record>"):]
        if len(хвост) > 50 << 20:
            raise ValueError("схема: запись больше 50 МБ — не тот формат")
    if not хвост.rstrip().endswith("</rkn:register>"):
        raise ValueError("обрыв: нет закрывающего </rkn:register>")


def _поле(запись, тег):
    m = re.search(r"<rkn:%s>(.*?)</rkn:%s>" % (тег, тег), запись, re.DOTALL)
    return html.unescape(m.group(1)).strip() if m else None


def _дата(s):
    s = (s or "").strip()
    if not s:
        return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        raise ValueError("схема: дата %r" % s[:20])
    return s


def _инн(s):
    s = (s or "").strip()
    return s if re.fullmatch(r"\d{10}|\d{12}", s) else None


def разобрать_лицензии(записи):
    """-> ({инн: [[номер, статус, начало, окончание, услуга]]}, всего, без_инн)."""
    по_инн, всего, без_инн = {}, 0, 0
    for з in записи:
        всего += 1
        статус = _поле(з, "lic_status_name")
        if статус not in СТАТУСЫ:
            raise ValueError("схема: статус лицензии %r" % (статус or "")[:40])
        номер = _поле(з, "licence_num")
        if not номер:
            raise ValueError("схема: лицензия без номера")
        инн = _инн(_поле(з, "inn"))
        if not инн:
            без_инн += 1
            continue
        по_инн.setdefault(инн, []).append([
            номер, статус, _дата(_поле(з, "date_start")), _дата(_поле(з, "date_end")),
            _поле(з, "service_name") or ""])
    return по_инн, всего, без_инн


def разобрать_ори(записи):
    """-> ({инн: [{номер, включён, домены}]}, всего, без_инн)."""
    по_инн, всего, без_инн = {}, 0, 0
    for з in записи:
        всего += 1
        номер = _поле(з, "entryNum")
        if not номер:
            raise ValueError("схема: запись ОРИ без номера")
        инн = _инн(_поле(з, "distributorINN"))
        if not инн:
            без_инн += 1               # иностранные ОРИ: российского ИНН нет
            continue
        домены = sorted({html.unescape(д).strip() for д in
                         re.findall(r"<rkn:domain>(.*?)</rkn:domain>", з, re.DOTALL)} - {""})
        по_инн.setdefault(инн, []).append({"номер": номер,
                                           "включён": _дата(_поле(з, "entryDate")),
                                           "домены": домены})
    return по_инн, всего, без_инн


def _куски_файла(путь):
    with open(путь, encoding="utf-8") as fh:
        while True:
            кусок = fh.read(8 << 20)
            if not кусок:
                return
            yield кусок


def разобрать_файл(реестр, путь):
    """XML реестра на диске -> (по_инн, всего). ValueError — схема не та."""
    with open(путь, "rb") as fh:
        начало = fh.read(2000).decode("utf-8", "replace")
    if "rsoc.ru/opendata/%s" % РЕЕСТРЫ[реестр]["набор"] not in начало:
        raise ValueError("схема: файл не похож на набор %s (%r…)" % (реестр, начало[:60]))
    if реестр == "лицензии_связи":
        по_инн, всего, без_инн = разобрать_лицензии(записи_xml(_куски_файла(путь)))
        if всего and (всего - без_инн) < всего * ДОЛЯ_ИНН_ЛИЦЕНЗИЙ:
            raise ValueError("схема: ИНН только у %d лицензий из %d" % (всего - без_инн, всего))
    else:
        по_инн, всего, _ = разобрать_ори(записи_xml(_куски_файла(путь)))
    if not всего:
        raise ValueError("схема: в наборе %s нет записей" % реестр)
    return по_инн, всего


# --- индекс -----------------------------------------------------------------

def refresh(cache_dir=None, get=None, сегодня=None):
    """Скачивает оба набора и пересобирает индекс; сбой любого — старый индекс цел.
    get(url, referer, лимит, в_файл=None) — для тестов."""
    cache_dir = cache_dir or CACHE_DIR
    os.makedirs(cache_dir, exist_ok=True)
    путь = os.path.join(cache_dir, ИНДЕКС)
    сегодня = сегодня or dump_tools.сегодня()
    if get is None:
        try:
            op = dump_tools.opener()
        except ValueError as e:
            return {"статус": "индекс не обновлён: %s" % e}

        def get(url, referer, лимит, в_файл=None):
            return dump_tools.get(url, referer, лимит, timeout=600, в_файл=в_файл, op=op)
    try:
        блок = dump_tools.Блокировка(cache_dir).__enter__()
    except FileExistsError:
        return {"статус": "индекс не обновлён: другой refresh уже идёт"}
    try:
        try:
            прежние = (dump_tools.прочитать_индекс(путь).get("реестры")
                       if os.path.exists(путь) else None) or {}
        except Exception:
            прежние = {}
        реестры, данные = {}, {}
        for реестр, опис in РЕЕСТРЫ.items():
            fd, tmp = tempfile.mkstemp(dir=cache_dir, prefix=".выгрузка-", suffix=".xml")
            os.close(fd)
            try:
                стр = страница(реестр)
                url, дата = ссылка_на_данные(
                    get(стр, САЙТ + "/opendata/", 5 << 20).decode("utf-8", "replace"), реестр)
                get(url, стр, опис["лимит"], в_файл=tmp)
                по_инн, всего = разобрать_файл(реестр, tmp)
            except Exception as e:
                return {"статус": "индекс не обновлён: %s: %s" % (реестр, str(e)[:200])}
            finally:
                os.unlink(tmp)
            было = (прежние.get(реестр) or {}).get("записей") or 0
            if всего < было * ДОЛЯ_ПАДЕНИЯ:
                return {"статус": "индекс не обновлён: в наборе %s %d записей (было %d)"
                                  % (реестр, всего, было)}
            данные[реестр] = по_инн
            реестры[реестр] = {"название": опис["название"], "url": url, "записей": всего,
                               "скачан": сегодня,
                               "выгрузка": "%s-%s-%s" % (дата[:4], дата[4:6], дата[6:])}
        dump_tools.записать_индекс(путь, {"построен": сегодня, "реестры": реестры,
                                          "данные": данные})
        return {"статус": "обновлено", "реестры": реестры}
    finally:
        блок.__exit__(None, None, None)


def устарел(индекс, сегодня):
    скачан = min((р.get("скачан") or "1970-01-01") for р in индекс["реестры"].values())
    if dump_tools.старше(скачан, сегодня, СВЕЖЕСТЬ_ДНЕЙ):
        return ["реестры РКН скачаны %s, старше %d дней — обновите: rkn_registries.py "
                "--refresh (нужен РФ-IP или INN_CHECK_PROXY)" % (скачан, СВЕЖЕСТЬ_ДНЕЙ)]
    return []


def lookup(inn, cache_dir=None, сегодня=None):
    """-> (dict|None, заметка). None — индекса нет."""
    cache_dir = cache_dir or CACHE_DIR
    путь = os.path.join(cache_dir, ИНДЕКС)
    try:
        индекс = dump_tools.прочитать_индекс(путь)
    except FileNotFoundError:
        return None, ("индекса нет (%s) — сначала rkn_registries.py --refresh (нужен РФ-IP "
                      "или INN_CHECK_PROXY)"
                      % (CACHE_DIR_ВИДИМЫЙ if cache_dir == CACHE_DIR else путь))
    сегодня = сегодня or dump_tools.сегодня()
    inn = str(inn)
    лиц = индекс["данные"]["лицензии_связи"].get(inn, [])
    # сначала действующие, внутри — по дате начала, новые первыми
    лиц = sorted(sorted(лиц, key=lambda л: л[2] or "", reverse=True),
                 key=lambda л: л[1] != "действующая")
    ори = индекс["данные"]["ори"].get(inn, [])
    return {
        "в_реестрах": bool(лиц or ори),
        "лицензии_связи": {
            "всего": len(лиц),
            "действующих": sum(1 for л in лиц if л[1] == "действующая"),
            "приостановленных": sum(1 for л in лиц if л[1] == "приостановленная"),
            "показаны": [{"номер": л[0], "статус": л[1], "начало": л[2], "окончание": л[3],
                          "услуга": л[4]} for л in лиц[:ПОКАЗАТЬ]],
        },
        "ори": ори,
        "охват": "реестр лицензий в области связи (все, включая недействующие) и реестр "
                 "организаторов распространения информации (текущий) Роскомнадзора",
        "скачан": {k: р.get("скачан") for k, р in индекс["реестры"].items()},
        "выгрузка": {k: р.get("выгрузка") for k, р in индекс["реестры"].items()},
        "предупреждения": устарел(индекс, сегодня),
        "источник": САЙТ + "/opendata/",
    }, "ok"


def main(argv):
    args = argv[1:]
    if args == ["--refresh"]:
        res = refresh()
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res["статус"] == "обновлено" else 1
    if len(args) == 2 and args[0] == "--inn":
        res, note = lookup(args[1])
        print(json.dumps(res if res is not None else {"статус": "не проверено",
                                                      "причина": note},
                         ensure_ascii=False, indent=2))
        return 0 if res is not None else 1
    sys.stderr.write(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
