#!/usr/bin/env python3
"""
rzn_licenses.py — лицензии Росздравнадзора по ИНН из открытых данных. Только stdlib.

Росздравнадзор еженедельно выкладывает реестры лицензий XML-файлами
(roszdravnadzor.gov.ru/opendata, стандарт opendata 3.0, без капчи):

    ls_licenses    фармацевтическая деятельность
    nark_licenses  оборот наркотических средств и психотропных веществ
    md_licenses    техобслуживание медицинских изделий

Лицензий на МЕДИЦИНСКУЮ деятельность (клиники) в открытых данных нет — только
статистика; по клиникам блок честно «не покрыто», а не «лицензии нет».

Паттерн дампов ФНС: скачивание один раз в кэш ~/.cache/inn-check-ru/rzn/,
из XML строится компактный индекс по ИНН (индекс.json.gz), сверка локальная —
ИНН никуда не уходит.

    python3 rzn_licenses.py --refresh          # скачать наборы, построить индекс
    python3 rzn_licenses.py --inn 2309137766   # сверка по индексу

Что значит «записи нет»: выгрузка — это действующие лицензии; прекращённые из
неё в основном выпадают (живой снимок 27.09.2026: 4 прекращённых из 31 541).
Поэтому «записи нет» = «в выгрузке нет», а не «лицензии никогда не было».

Состояние лицензии считает код по полям выгрузки на дату проверки:
    прекращена       — есть сведения о прекращении/аннулировании;
    приостановлена   — последняя приостановка без возобновления ИЛИ с датой
                       возобновления позже сегодняшней (в выгрузке бывает
                       «Возобновлено: <дата в будущем>» — плановый конец срока);
    действует        — иначе.
"""

import csv
import datetime as dt
import gzip
import io
import json
import os
import re
import ssl
import sys
import urllib.request
import zipfile

CACHE_DIR = os.path.expanduser(os.path.join("~", ".cache", "inn-check-ru", "rzn"))
ИНДЕКС = "индекс.json.gz"
TIMEOUT = 180
UA = "inn-check-ru (+https://github.com/ilyautov/inn-check-ru)"
ПОРТАЛ = "https://roszdravnadzor.gov.ru/opendata"
НАБОРЫ = {
    "ls_licenses": "Фармацевтическая деятельность",
    "nark_licenses": "Оборот наркотических средств и психотропных веществ",
    "md_licenses": "Техническое обслуживание медицинских изделий",
}
ПОЛЯ = ("name", "activity_type", "inn", "number", "date_register", "termination",
        "date_termination", "information_suspension_resumption",
        "information_cancellation")
_ДАТА = r"(\d{4}-\d{2}-\d{2})"


def _сегодня():
    # местная дата пользователя, как в fetch_counterparty (aware now -> местная)
    return dt.datetime.now(dt.timezone.utc).astimezone().date().isoformat()


def _ssl_context():
    """TLS-контекст движка (корень УЦ Минцифры через install_ca.py); верификация
    всегда включена."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from fetch_counterparty import _build_ssl_context
        return _build_ssl_context()
    except Exception:
        return ssl.create_default_context()


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=_ssl_context()) as r:
        return r.read()


def _мета(набор, получить=_get):
    """meta.csv набора -> (url последних данных, дата выгрузки ГГГГ-ММ-ДД, valid)."""
    текст = получить("%s/7710537160-%s/meta.csv" % (ПОРТАЛ, набор)).decode("utf-8-sig")
    строки = {r[0]: r[1] for r in csv.reader(io.StringIO(текст)) if len(r) >= 2}
    данные = sorted((k, v) for k, v in строки.items() if k.startswith("data-"))
    if not данные:
        raise ValueError("в meta.csv нет ссылок на данные")
    ключ, url = данные[-1]                      # data-ГГГГММДД-structure-… — по дате
    m = re.match(r"data-(\d{4})(\d{2})(\d{2})", ключ)
    выгрузка = "%s-%s-%s" % m.groups() if m else None
    v = re.match(r"(\d{4})(\d{2})(\d{2})$", строки.get("valid", ""))
    return url, выгрузка, ("%s-%s-%s" % v.groups() if v else None)


def состояние_лицензии(запись, сегодня=None):
    """-> (состояние, пояснение). Только по полям выгрузки, без догадок."""
    сегодня = сегодня or _сегодня()
    прекр = " ".join(filter(None, (запись.get("information_cancellation"),
                                   запись.get("termination"),
                                   запись.get("date_termination"))))
    if прекр.strip():
        return "прекращена", прекр.strip()
    текст = запись.get("information_suspension_resumption") or ""
    приост = list(re.finditer(r"Приостановлено:[^/]*?" + _ДАТА, текст))
    if приост:
        последняя = приост[-1]
        хвост = текст[последняя.end():]
        возоб = re.search(r"Возобновлено:[^/]*?" + _ДАТА, хвост)
        частично = "в части" in текст[последняя.start():последняя.end() + 120]
        if возоб is None or возоб.group(1) > сегодня:
            до = " до %s" % возоб.group(1) if возоб else ""
            return ("приостановлена_частично" if частично else "приостановлена",
                    "с %s%s" % (последняя.group(1), до))
    return "действует", None


def _записи_xml(поток):
    """Потоковый разбор XML выгрузки: <licenses> — одна лицензия."""
    import xml.etree.ElementTree as ET  # у сломанного pyexpat — ImportError
    for _, el in ET.iterparse(поток, events=("end",)):
        if el.tag == "licenses":
            запись = {п: (el.findtext(п) or "").strip() for п in ПОЛЯ}
            запись["мест_деятельности"] = len(el.findall("work_address_list/address_place"))
            el.clear()
            yield запись


def построить_индекс(источники, путь, сегодня=None):
    """источники: {набор: (xml-поток, дата_выгрузки, valid, url)} -> индекс на диск.
    Храним только нужное для блока: без адресов и наименования лицензиата
    (у ИП это ФИО) — ИНН и так известен."""
    по_инн, наборы = {}, {}
    for набор, (поток, выгрузка, valid, url) in источники.items():
        n = 0
        for з in _записи_xml(поток):
            инн = з["inn"]
            if not re.fullmatch(r"\d{10}|\d{12}", инн):
                continue
            n += 1
            по_инн.setdefault(инн, []).append({
                "набор": набор,
                "вид": з["activity_type"] or НАБОРЫ[набор],
                "номер": з["number"],
                "дата_регистрации": з["date_register"],
                "лицензирующий_орган": з["name"],
                "мест_деятельности": з["мест_деятельности"],
                "прекращение": " ".join(filter(None, (
                    з["information_cancellation"], з["termination"],
                    з["date_termination"]))),
                "приостановка": з["information_suspension_resumption"],
            })
        наборы[набор] = {"название": НАБОРЫ[набор], "дата_выгрузки": выгрузка,
                         "действителен_до": valid, "url": url, "записей": n}
    os.makedirs(os.path.dirname(путь), exist_ok=True)
    tmp = путь + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        json.dump({"построен": сегодня or _сегодня(),
                   "наборы": наборы, "по_инн": по_инн}, fh, ensure_ascii=False)
    os.replace(tmp, путь)
    return наборы


def refresh(cache_dir=CACHE_DIR, получить=_get):
    """Скачивает все наборы; индекс строится, только если скачались все —
    иначе старый индекс остаётся (частичный дал бы ложное «записи нет»)."""
    источники, итог = {}, {}
    for набор in НАБОРЫ:
        try:
            url, выгрузка, valid = _мета(набор, получить)
            архив = zipfile.ZipFile(io.BytesIO(получить(url)))
            xml = [n for n in архив.namelist() if n.lower().endswith(".xml")]
            if len(xml) != 1:
                raise ValueError("в архиве %d XML" % len(xml))
            источники[набор] = (архив.open(xml[0]), выгрузка, valid, url)
            итог[набор] = {"статус": "скачан", "дата_выгрузки": выгрузка}
        except Exception as e:
            итог[набор] = {"статус": "не скачан", "причина": "%s: %s" % (
                type(e).__name__, str(e)[:160])}
    if len(источники) != len(НАБОРЫ):
        return {"статус": "индекс не обновлён: скачаны не все наборы", "наборы": итог}
    try:
        наборы = построить_индекс(источники, os.path.join(cache_dir, ИНДЕКС))
    except ImportError:
        return {"статус": "индекс не построен: в этом Python не работает XML-парсер "
                          "(pyexpat) — запустите другим интерпретатором", "наборы": итог}
    return {"статус": "обновлено", "наборы": наборы}


def lookup(inn, cache_dir=CACHE_DIR, сегодня=None):
    """-> (dict|None, заметка). None — индекса нет (блок «не проверено»)."""
    путь = os.path.join(cache_dir, ИНДЕКС)
    try:
        with gzip.open(путь, "rt", encoding="utf-8") as fh:
            индекс = json.load(fh)
    except FileNotFoundError:
        return None, ("индекса нет (%s) — сначала rzn_licenses.py --refresh" % путь)
    сегодня = сегодня or _сегодня()
    лицензии = []
    for з in индекс["по_инн"].get(str(inn), []):
        сост, пояснение = состояние_лицензии({
            "information_cancellation": з.get("прекращение"),
            "information_suspension_resumption": з.get("приостановка")}, сегодня)
        лицензии.append(dict(з, состояние=сост, **({"состояние_пояснение": пояснение}
                                                  if пояснение else {})))
    предупреждения = ["выгрузка «%s» устарела: действительна до %s — обновите: "
                      "rzn_licenses.py --refresh" % (н["название"], н["действителен_до"])
                      for н in индекс["наборы"].values()
                      if н.get("действителен_до") and н["действителен_до"] < сегодня]
    return {
        "в_реестре": bool(лицензии),
        "лицензии": лицензии,
        "дата_выгрузки": {k: н.get("дата_выгрузки") for k, н in индекс["наборы"].items()},
        "охват": "фармацевтика, оборот наркотических средств, техобслуживание "
                 "медизделий; медицинской деятельности (клиник) в открытых данных нет; "
                 "выгрузка — действующие лицензии, прекращённые из неё выпадают",
        "предупреждения": предупреждения,
        "источник": ПОРТАЛ,
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
