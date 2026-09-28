#!/usr/bin/env python3
"""
uc_mintsifry.py — аккредитованные удостоверяющие центры (Минцифры) по ИНН, офлайн.
Только stdlib.

Страница digital.gov.ru/activity/gos-uslugi/akkreditacziya-udostoveryayushhih-czentrov —
SPA: ссылки на перечни она берёт из /api/digital/v2/page-data?path=<путь страницы>.
Оттуда же их берёт и этот модуль — по тексту ссылки, адрес файла не угадывается
(в имени файла дата и случайный префикс). Живьём 28.09.2026:

    «Перечень аккредитованных удостоверяющих центров на 21.09.2026»          43 УЦ
    «…деятельность которых прекращена на 21.09.2026»                          341
    «…аккредитация которых приостановлена на 30.04.2026»   — .xls (BIFF), stdlib его
                                                            не читает; не берётся
    «…досрочно прекращена на 06.06.2022»                   — .xls, не берётся

ИНН стоит в тексте ячейки «(ИНН …, ОГРН …)». По ИНН — в каком перечне, приказ,
статус и примечание Минцифры (история приостановок и прекращений). Наименования и
адреса в индекс не попадают. Сайт не отвечает не-РФ адресам: скачивание через сеть
пользователя (INN_CHECK_PROXY, в т. ч. iface://en0).

    python3 uc_mintsifry.py --refresh      # нужен РФ-IP или INN_CHECK_PROXY
    python3 uc_mintsifry.py --inn 7605016030
"""

import datetime as dt
import html
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cbr_registries
import dump_tools

CACHE_DIR = os.path.expanduser(os.path.join("~", ".cache", "inn-check-ru", "uc"))
CACHE_DIR_ВИДИМЫЙ = "~/.cache/inn-check-ru/uc"
ИНДЕКС = "индекс.json.gz"
СТРАНИЦА = "https://digital.gov.ru/activity/gos-uslugi/akkreditacziya-udostoveryayushhih-czentrov"
ДАННЫЕ_СТРАНИЦЫ = ("https://digital.gov.ru/api/digital/v2/page-data?path="
                   "/activity/gos-uslugi/akkreditacziya-udostoveryayushhih-czentrov")
ПЕРЕЧНИ = {
    "действующие": r"Перечень аккредитованных удостоверяющих центров на (\d{2}\.\d{2}\.\d{4})",
    "прекращённые": (r"Перечень аккредитованных удостоверяющих центров, деятельность которых "
                     r"прекращена на (\d{2}\.\d{2}\.\d{4})"),
}
ХОСТ_ФАЙЛОВ = "https://adm.digital.gov.ru/app/uploads/"
ЛИМИТ_СТРАНИЦЫ = 10 << 20
ЛИМИТ_XLSX = 20 << 20
ДОЛЯ_ПАДЕНИЯ = 0.8
# живьём 43 и 341; меньше — обрезанный файл даже при первом refresh (ревью Codex)
МИН_СТРОК = {"действующие": 20, "прекращённые": 200}
# действующий перечень с датой старше — «аккредитован сейчас» не утверждается
ПЕРЕЧЕНЬ_СТАРШЕ_ДНЕЙ = 180
СВЕЖЕСТЬ_ДНЕЙ = 45
ПРИМЕЧАНИЕ_СИМВОЛОВ = 2000


def _дата(s):
    д, м, г = s.split(".")
    return dt.date(int(г), int(м), int(д)).isoformat()


def ссылки(данные_страницы):
    """JSON page-data -> {перечень: (url xlsx, на_дату ISO)}. Нет ссылки или она
    не xlsx на adm.digital.gov.ru — схема."""
    текст = json.dumps(json.loads(данные_страницы), ensure_ascii=False)
    текст = текст.replace("\\/", "/").replace('\\"', '"')
    найдено = {}
    for href, подпись in re.findall(r'<a\b[^>]*?href="([^"]+)"[^>]*>(.*?)</a>', текст, re.DOTALL):
        подпись = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", подпись)).split())
        for перечень, образец in ПЕРЕЧНИ.items():
            m = re.fullmatch(образец, подпись)
            if m:
                if перечень in найдено:
                    raise ValueError("схема: на странице две ссылки «%s»" % перечень)
                if not (href.startswith(ХОСТ_ФАЙЛОВ) and href.endswith(".xlsx")):
                    raise ValueError("схема: перечень «%s» не xlsx на adm.digital.gov.ru (%r)"
                                     % (перечень, href[:80]))
                найдено[перечень] = (href, _дата(m.group(1)))
    нет = [п for п in ПЕРЕЧНИ if п not in найдено]
    if нет:
        raise ValueError("схема: на странице нет ссылок на перечни: %s" % ", ".join(нет))
    return найдено


def разобрать(перечень, данные):
    """XLSX перечня -> ({инн: [записи]}, строк, без_инн). Шапка ищется по
    «Название организации»; колонки — по тексту шапки."""
    листы = [с for имя, с in cbr_registries.листы(данные).items() if имя and с]
    if len(листы) != 1:
        raise ValueError("схема: в перечне %d непустых листов" % len(листы))
    строки = листы[0]
    шапка_i = next((i for i, r in enumerate(строки[:10])
                    if any(в.lower().startswith("название организации") for в in r.values())), None)
    if шапка_i is None:
        raise ValueError("схема: в перечне нет шапки «Название организации…»")
    кол = {}
    for буква, текст in строки[шапка_i].items():
        т = " ".join(текст.lower().split())
        for поле, начало in (("название", "название организации"), ("приказ", "№ приказа"),
                             ("статус", "аккредитация"), ("примечание", "примечание")):
            if т.startswith(начало):
                кол[поле] = буква
    нужны = ("название", "приказ", "примечание") + (("статус",) if перечень == "действующие" else ())
    if any(п not in кол for п in нужны):
        raise ValueError("схема: в шапке перечня нет колонок %s" % [п for п in нужны if п not in кол])
    по_инн, всего, без = {}, 0, 0
    for r in строки[шапка_i + 1:]:
        if not any(r.values()):
            continue
        всего += 1
        инн = re.findall(r"ИНН\s*:?\s*(\d{12}|\d{10})(?!\d)", r.get(кол["название"], ""))
        if len(set(инн)) != 1:
            без += 1
            continue
        статус = (" ".join(r.get(кол["статус"], "").split()).lower()
                  if перечень == "действующие" else "прекращена")
        if not статус:
            raise ValueError("схема: строка перечня без статуса аккредитации")
        примечание = " ".join(r.get(кол["примечание"], "").split())
        if len(примечание) > ПРИМЕЧАНИЕ_СИМВОЛОВ:
            примечание = примечание[:ПРИМЕЧАНИЕ_СИМВОЛОВ - 1] + "…"
        по_инн.setdefault(инн[0], []).append({
            "перечень": перечень, "статус": статус,
            "приказ": " ".join(r.get(кол["приказ"], "").split()) or None,
            "примечание": примечание or None})
    if not всего:
        raise ValueError("схема: в перечне нет строк")
    if без > max(1, всего // 50):
        raise ValueError("схема: без одного ИНН %d строк из %d" % (без, всего))
    return по_инн, всего, без


def refresh(cache_dir=None, get=None, сегодня=None):
    """Качает page-data и два перечня, пересобирает индекс; сбой — старый цел.
    get(url, referer, лимит) — для тестов."""
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
            return dump_tools.get(url, referer, лимит, timeout=300, в_файл=в_файл, op=op)
    try:
        блок = dump_tools.Блокировка(cache_dir).__enter__()
    except FileExistsError:
        return {"статус": "индекс не обновлён: другой refresh уже идёт"}
    try:
        try:
            прежний = dump_tools.прочитать_индекс(путь) if os.path.exists(путь) else {}
        except Exception:
            прежний = {}
        try:
            найдено = ссылки(get(ДАННЫЕ_СТРАНИЦЫ, СТРАНИЦА, ЛИМИТ_СТРАНИЦЫ).decode("utf-8"))
            по_инн, перечни = {}, {}
            for перечень, (url, на_дату) in найдено.items():
                часть, всего, без = разобрать(перечень, get(url, СТРАНИЦА, ЛИМИТ_XLSX))
                было = ((прежний.get("перечни") or {}).get(перечень) or {}).get("строк") or 0
                if всего < max(было * ДОЛЯ_ПАДЕНИЯ, МИН_СТРОК[перечень]):
                    raise ValueError("схема: в перечне «%s» %d строк (было %d)"
                                     % (перечень, всего, было))
                перечни[перечень] = {"на_дату": на_дату, "строк": всего, "без_инн": без,
                                     "url": url}
                for инн, записи in часть.items():
                    по_инн.setdefault(инн, []).extend(
                        dict(з, на_дату=на_дату) for з in записи)
        except Exception as e:
            return {"статус": "индекс не обновлён: %s: %s" % (type(e).__name__, str(e)[:200])}
        dump_tools.записать_индекс(путь, {"скачан": сегодня, "перечни": перечни,
                                          "по_инн": по_инн})
        return {"статус": "обновлено", "перечни": перечни, "организаций": len(по_инн)}
    finally:
        блок.__exit__(None, None, None)


def устарел(индекс, сегодня):
    if dump_tools.старше(индекс["скачан"], сегодня, СВЕЖЕСТЬ_ДНЕЙ):
        return ["перечни УЦ скачаны %s — старше %d дней; обновите: uc_mintsifry.py --refresh "
                "(нужен РФ-IP или INN_CHECK_PROXY)" % (индекс["скачан"], СВЕЖЕСТЬ_ДНЕЙ)]
    return []


def lookup(inn, cache_dir=None, сегодня=None):
    """-> (dict|None, заметка). None — индекса нет."""
    cache_dir = cache_dir or CACHE_DIR
    try:
        индекс = dump_tools.прочитать_индекс(os.path.join(cache_dir, ИНДЕКС))
    except FileNotFoundError:
        return None, ("индекса нет (%s) — сначала uc_mintsifry.py --refresh (нужен РФ-IP или "
                      "INN_CHECK_PROXY)" % (CACHE_DIR_ВИДИМЫЙ if cache_dir == CACHE_DIR
                                            else cache_dir))
    сегодня = сегодня or dump_tools.сегодня()
    записи = индекс["по_инн"].get(str(inn), [])
    на_дату = {п: д["на_дату"] for п, д in индекс["перечни"].items()}
    сейчас = any(з["перечень"] == "действующие" and з["статус"] == "действующая" for з in записи)
    if сейчас and (dump_tools.старше(на_дату["действующие"], сегодня, ПЕРЕЧЕНЬ_СТАРШЕ_ДНЕЙ)
                   or any(з["перечень"] == "прекращённые" for з in записи)
                   and на_дату["прекращённые"] > на_дату["действующие"]):
        # перечень давний или прекращение опубликовано позже — «сейчас» не известно
        сейчас = None
    return {
        "в_перечнях": bool(записи),
        "аккредитован_сейчас": сейчас,
        "записи": записи,
        "перечни": {п: {"на_дату": д["на_дату"], "строк": д["строк"], "без_инн": д["без_инн"]}
                    for п, д in индекс["перечни"].items()},
        "оговорка": "перечни приостановленных и досрочно прекращённых (2022) УЦ Минцифры "
                    "публикует в старом формате xls — они не читаются; приостановка видна "
                    "только в примечании Минцифры; строки с ошибкой в ИНН пропущены "
                    "(перечни.<…>.без_инн)",
        "скачан": индекс["скачан"],
        "предупреждения": устарел(индекс, сегодня),
        "источник": СТРАНИЦА,
    }, "ok"


def main(argv):
    args = argv[1:]
    if args == ["--refresh"]:
        res = refresh()
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res["статус"].startswith("обновлено") else 1
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
