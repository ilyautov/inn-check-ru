#!/usr/bin/env python3
"""
rzn_licenses.py — лицензии Росздравнадзора по ИНН из открытых данных. Только stdlib.

Росздравнадзор еженедельно выкладывает реестры лицензий XML-файлами
(roszdravnadzor.gov.ru/opendata, стандарт opendata 3.0, без капчи):

    ls_licenses    фармацевтическая деятельность
    nark_licenses  оборот наркотических средств и психотропных веществ
    md_licenses    техобслуживание медицинских изделий

Лицензий на МЕДИЦИНСКУЮ деятельность (клиники) в открытых данных нет — только
статистика; по клиникам «записи нет» не значит «лицензии нет».

Паттерн дампов ФНС: скачивание один раз в кэш ~/.cache/inn-check-ru/rzn/,
из XML строится компактный индекс по ИНН (индекс.json.gz), сверка локальная —
ИНН никуда не уходит.

    python3 rzn_licenses.py --refresh          # скачать наборы, построить индекс
    python3 rzn_licenses.py --inn 2309137766   # сверка по индексу

Что значит «записи нет»: выгрузка — это действующие лицензии; прекращённые из
неё в основном выпадают (живой снимок 27.09.2026: 4 прекращённых из 31 541).
Поэтому «записи нет» = «в выгрузке нет», а не «лицензии никогда не было».

Состояние лицензии считает код по полям выгрузки на дату проверки. Живая
выгрузка 27.09.2026: события приостановки — только «Приостановлено: … от
ГГГГ-ММ-ДД» и «Возобновлено: … ГГГГ-ММ-ДД» через «/» (44 события), прекращение —
только «Причина: Прекращение лицензии» без даты; поля termination и
date_termination пусты у всех.
    прекращена       — сведения о прекращении без даты или с датой не позже
                       сегодняшней;
    приостановлена   — последнее наступившее (дата ≤ сегодня) событие —
                       приостановка; «Возобновлено» с датой в будущем (так в живой
                       выгрузке: плановый конец срока) ещё не наступило;
    неясно           — событие без даты или незнакомой формы: не угадываем;
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
import tempfile
import time
import urllib.request
import zipfile

CACHE_DIR = os.path.expanduser(os.path.join("~", ".cache", "inn-check-ru", "rzn"))
CACHE_DIR_ВИДИМЫЙ = "~/.cache/inn-check-ru/rzn"     # в причинах — без имени пользователя
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
# Лимиты: живьём 27.09.2026 архив 14,5 МБ, XML 175 МБ — запас на порядок
ЛИМИТ_META = 1 << 20
ЛИМИТ_АРХИВА = 300 << 20
ЛИМИТ_XML = 2 << 30
# Набор, в котором записей стало вдвое меньше прежнего, — сломанная выгрузка
# или сменившаяся схема, а не закрытие половины аптек страны за неделю
ДОЛЯ_ПАДЕНИЯ = 0.5
# Индекс без распознанного срока годности считается устаревшим через 14 дней
СВЕЖЕСТЬ_ДНЕЙ = 14
БЛОКИРОВКА_ЧАСОВ = 2
_ДАТА = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


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


def _get(url, лимит, куда=None):
    """GET с лимитом размера. куда=None -> bytes в памяти, иначе поток на диск."""
    if not url.startswith(ПОРТАЛ + "/"):
        raise ValueError("адрес вне открытых данных Росздравнадзора: %s" % url[:80])
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    буфер, n = (io.BytesIO() if куда is None else куда), 0
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=_ssl_context()) as r:
        while True:
            кусок = r.read(1 << 20)
            if not кусок:
                break
            n += len(кусок)
            if n > лимит:
                raise ValueError("ответ больше лимита %d МБ" % (лимит >> 20))
            буфер.write(кусок)
    return буфер.getvalue() if куда is None else n


def _мета(набор, получить):
    """meta.csv набора -> (url последних данных, дата выгрузки ГГГГ-ММ-ДД, valid)."""
    текст = получить("%s/7710537160-%s/meta.csv" % (ПОРТАЛ, набор),
                     ЛИМИТ_META).decode("utf-8-sig")
    строки = {r[0]: r[1] for r in csv.reader(io.StringIO(текст)) if len(r) >= 2}
    данные = sorted((k, v) for k, v in строки.items() if k.startswith("data-"))
    if not данные:
        raise ValueError("в meta.csv нет ссылок на данные")
    ключ, url = данные[-1]                      # data-ГГГГММДД-structure-… — по дате
    m = re.match(r"data-(\d{4})(\d{2})(\d{2})", ключ)
    выгрузка = "%s-%s-%s" % m.groups() if m else None
    v = re.match(r"(\d{4})(\d{2})(\d{2})$", строки.get("valid", ""))
    return url, выгрузка, ("%s-%s-%s" % v.groups() if v else None)


def _дата(текст):
    m = _ДАТА.search(текст or "")
    return "%s-%s-%s" % m.groups() if m else None


def события(текст):
    """Строка приостановок -> [(дата|None, вид|None, частично)] в порядке записи.
    Разделитель «/» — только перед «Приостановлено:»/«Возобновлено:» (в номере
    приказа «/» бывает)."""
    out = []
    for сегмент in re.split(r"/(?=\s*(?:приостановлено|возобновлено)\s*:)", текст or "",
                            flags=re.IGNORECASE):
        с = сегмент.strip()
        if not с:
            continue
        низ = с.lower()
        вид = ("приостановка" if низ.startswith("приостановлено") else
               "возобновление" if низ.startswith("возобновлено") else None)
        out.append((_дата(с), вид, "в части" in низ))
    return out


def состояние_лицензии(запись, сегодня=None):
    """-> (состояние, пояснение). Только по полям выгрузки, без догадок."""
    сегодня = сегодня or _сегодня()
    прекр = " ".join(filter(None, (запись.get("information_cancellation"),
                                   запись.get("termination"),
                                   запись.get("date_termination")))).strip()
    план = None
    if прекр:
        д = _дата(запись.get("date_termination")) or _дата(прекр)
        if д is None or д <= сегодня:
            return "прекращена", прекр
        план = "прекращение назначено на %s" % д      # ещё не наступило
    соб = события(запись.get("information_suspension_resumption"))
    if any(д is None or вид is None for д, вид, _ in соб):
        return "неясно", ("сведения о приостановке не разобраны: %s"
                          % (запись.get("information_suspension_resumption") or "")[:200])
    # по дате, при равной — по порядку записи
    наступившие = sorted((д, i, вид, част) for i, (д, вид, част) in enumerate(соб)
                         if д <= сегодня)
    if наступившие and наступившие[-1][2] == "приостановка":
        д, i, _, частично = наступившие[-1]
        будущие = sorted(дв for дв, вв, _ in соб if вв == "возобновление" and дв > сегодня)
        до = " до %s" % будущие[0] if будущие else ""
        return ("приостановлена_частично" if частично else "приостановлена",
                "с %s%s" % (д, до) + ("; %s" % план if план else ""))
    return "действует", план


def _записи_xml(поток):
    """Потоковый разбор XML выгрузки: <licenses> — одна лицензия. Корень чистится
    после каждой записи, чтобы память не росла на чужих элементах."""
    import xml.etree.ElementTree as ET  # у сломанного pyexpat — ImportError
    корень = None
    for событие, el in ET.iterparse(поток, events=("start", "end")):
        if событие == "start":
            if корень is None:
                корень = el
                if el.tag != "licenses_list":        # namespace или другая схема
                    raise ValueError("схема: корень %r вместо licenses_list" % el.tag[:60])
            continue
        if el.tag == "licenses":
            запись = {п: (el.findtext(п) or "").strip() for п in ПОЛЯ}
            запись["мест_деятельности"] = len(el.findall("work_address_list/address_place"))
            yield запись
            корень.clear()


def _сжать(набор, з):
    """Запись индекса: без адресов и наименования лицензиата (у ИП это ФИО) —
    ИНН и так известен."""
    return {
        "набор": набор,
        "вид": з["activity_type"] or НАБОРЫ[набор],
        "номер": з["number"],
        "дата_регистрации": з["date_register"],
        "лицензирующий_орган": з["name"],
        "мест_деятельности": з["мест_деятельности"],
        "прекращение": " ".join(filter(None, (з["information_cancellation"],
                                              з["termination"]))),
        "дата_прекращения": з["date_termination"],
        "приостановка": з["information_suspension_resumption"],
    }


def построить_индекс(источники, путь, прежние=None, сегодня=None):
    """источники: {набор: (xml-поток, дата_выгрузки, valid, url)} -> индекс на диск.
    ValueError — схема не та или записей подозрительно мало (старый индекс цел)."""
    по_инн, наборы = {}, {}
    for набор, (поток, выгрузка, valid, url) in источники.items():
        n = 0
        for з in _записи_xml(поток):
            if not з["number"]:
                raise ValueError("схема: у записи нет номера лицензии")
            инн = з["inn"]
            if not re.fullmatch(r"\d{10}|\d{12}", инн):
                continue
            n += 1
            по_инн.setdefault(инн, []).append(_сжать(набор, з))
        было = ((прежние or {}).get(набор) or {}).get("записей") or 0
        if n == 0 or n < было * ДОЛЯ_ПАДЕНИЯ:
            raise ValueError("схема: в наборе %s %d записей (было %d)" % (набор, n, было))
        наборы[набор] = {"название": НАБОРЫ[набор], "дата_выгрузки": выгрузка,
                         "действителен_до": valid, "url": url, "записей": n}
    папка = os.path.dirname(путь)
    os.makedirs(папка, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=папка, prefix=".индекс-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as сырой, gzip.GzipFile(fileobj=сырой, mode="wb") as gz:
            gz.write(json.dumps({"построен": сегодня or _сегодня(), "наборы": наборы,
                                 "по_инн": по_инн}, ensure_ascii=False).encode("utf-8"))
        os.replace(tmp, путь)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return наборы


def _прочитать(путь):
    with gzip.open(путь, "rt", encoding="utf-8") as fh:
        return json.load(fh)


class _Блокировка:
    """Один refresh на каталог: файл O_EXCL; брошенная блокировка старше
    БЛОКИРОВКА_ЧАСОВ снимается."""

    def __init__(self, папка):
        self.путь = os.path.join(папка, ".refresh.lock")

    def __enter__(self):
        try:
            if time.time() - os.path.getmtime(self.путь) > БЛОКИРОВКА_ЧАСОВ * 3600:
                os.unlink(self.путь)
        except OSError:
            pass
        self.fd = os.open(self.путь, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        return self

    def __exit__(self, *exc):
        os.close(self.fd)
        os.unlink(self.путь)


def refresh(cache_dir=None, получить=_get):
    """Скачивает все наборы и пересобирает индекс. Скачались не все, схема не та,
    записей подозрительно мало — старый индекс остаётся (частичный или пустой
    дал бы ложное «записи нет»)."""
    cache_dir = cache_dir or CACHE_DIR
    os.makedirs(cache_dir, exist_ok=True)
    путь = os.path.join(cache_dir, ИНДЕКС)
    try:
        блок = _Блокировка(cache_dir).__enter__()
    except FileExistsError:
        return {"статус": "индекс не обновлён: другой refresh уже идёт"}
    архивы, итог = [], {}
    try:
        источники = {}
        for набор in НАБОРЫ:
            try:
                url, выгрузка, valid = _мета(набор, получить)
                fd, файл = tempfile.mkstemp(dir=cache_dir, prefix=".%s-" % набор,
                                            suffix=".zip")
                архивы.append(файл)
                with os.fdopen(fd, "wb") as fh:
                    получить(url, ЛИМИТ_АРХИВА, fh)
                z = zipfile.ZipFile(файл)
                xml = [i for i in z.infolist() if i.filename.lower().endswith(".xml")]
                if len(xml) != 1:
                    raise ValueError("в архиве %d XML" % len(xml))
                if xml[0].file_size > ЛИМИТ_XML:
                    raise ValueError("XML больше лимита")
                источники[набор] = (z.open(xml[0]), выгрузка, valid, url)
                итог[набор] = {"статус": "скачан", "дата_выгрузки": выгрузка}
            except Exception as e:
                итог[набор] = {"статус": "не скачан", "причина": "%s: %s" % (
                    type(e).__name__, str(e)[:160])}
        if len(источники) != len(НАБОРЫ):
            return {"статус": "индекс не обновлён: скачаны не все наборы", "наборы": итог}
        try:
            прежние = _прочитать(путь).get("наборы") if os.path.exists(путь) else None
        except Exception:
            прежние = None
        try:
            наборы = построить_индекс(источники, путь, прежние)
        except ImportError:
            return {"статус": "индекс не обновлён: в этом Python не работает XML-парсер "
                              "(pyexpat) — запустите другим интерпретатором", "наборы": итог}
        except Exception as e:
            return {"статус": "индекс не обновлён: %s" % str(e)[:200], "наборы": итог}
        return {"статус": "обновлено", "наборы": наборы}
    finally:
        for файл in архивы:
            try:
                os.unlink(файл)
            except OSError:
                pass
        блок.__exit__(None, None, None)


def устарел(индекс, сегодня):
    """-> список предупреждений об устаревании (пустой — индекс свежий)."""
    out = []
    for н in индекс["наборы"].values():
        срок = н.get("действителен_до")
        if срок is None:
            # возраст — от даты самой выгрузки: пересборка старой выгрузки его не сбрасывает
            от = н.get("дата_выгрузки") or "1970-01-01"
            if (dt.date.fromisoformat(сегодня) - dt.date.fromisoformat(от)).days \
                    > СВЕЖЕСТЬ_ДНЕЙ:
                out.append("срок годности выгрузки «%s» не распознан, выгрузка старше %d "
                           "дней — обновите: rzn_licenses.py --refresh"
                           % (н["название"], СВЕЖЕСТЬ_ДНЕЙ))
        elif срок < сегодня:
            out.append("выгрузка «%s» устарела: действительна до %s — обновите: "
                       "rzn_licenses.py --refresh" % (н["название"], срок))
    return out


def lookup(inn, cache_dir=None, сегодня=None):
    """-> (dict|None, заметка). None — индекса нет (блок «не проверено»)."""
    путь = os.path.join(cache_dir or CACHE_DIR, ИНДЕКС)
    try:
        индекс = _прочитать(путь)
    except FileNotFoundError:
        return None, ("индекса нет (%s) — сначала rzn_licenses.py --refresh"
                      % (CACHE_DIR_ВИДИМЫЙ if cache_dir is None else путь))
    сегодня = сегодня or _сегодня()
    лицензии = []
    for з in индекс["по_инн"].get(str(inn), []):
        сост, пояснение = состояние_лицензии({
            "information_cancellation": з.get("прекращение"),
            "date_termination": з.get("дата_прекращения"),
            "information_suspension_resumption": з.get("приостановка")}, сегодня)
        лицензии.append(dict(з, состояние=сост, **({"состояние_пояснение": пояснение}
                                                  if пояснение else {})))
    return {
        "в_реестре": bool(лицензии),
        "лицензии": лицензии,
        "дата_выгрузки": {k: н.get("дата_выгрузки") for k, н in индекс["наборы"].items()},
        "охват": "фармацевтика, оборот наркотических средств, техобслуживание "
                 "медизделий; медицинской деятельности (клиник) в открытых данных нет; "
                 "выгрузка — действующие лицензии, прекращённые из неё выпадают",
        "предупреждения": устарел(индекс, сегодня),
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
