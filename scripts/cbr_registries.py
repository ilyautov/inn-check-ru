#!/usr/bin/env python3
"""
cbr_registries.py — госреестры Банка России (МФО, кредитные потребительские
кооперативы, сельхозкооперативы, ломбарды) по ИНН, офлайн. Только stdlib.

ЦБ выкладывает реестры XLSX-файлами со страницы
https://www.cbr.ru/microfinance/registry/ (ссылки со страницы, без капчи). Живьём
28.09.2026:

    list_MFO.xlsx      МФО: листы «Действующие» (817) и «Исключенные» (9 486,
                       с датой исключения); «Действующие МФК/МКК» — подвыборки
    list_KPK_gov.xlsx  КПК: «Действующие кооперативы» / «Недействующие кооперативы»
    list_skpk.xlsx     СКПК: действующие / в стадии ликвидации или реорганизации /
                       ликвидированные
    list_PS.xlsx       ломбарды (не платёжные системы, как можно подумать по имени):
                       «Действующие ломбарды» / «Недействующие ломбарды»

Паттерн дампов: скачать в ~/.cache/inn-check-ru/cbr_reg/, построить индекс по ИНН,
сверка локальная. Колонки находятся по тексту заголовка, а не по букве столбца;
незнакомый лист или заголовок без ИНН — «схема:», старый индекс цел.

В индекс не попадают ФИО (у СКПК есть колонка с ФИО руководителя), адреса,
телефоны и e-mail — только ИНН, ОГРН, вид, даты, статус и ограничения.

    python3 cbr_registries.py --refresh
    python3 cbr_registries.py --inn 7325081622

XLSX разбирается regex'ом, а не ElementTree: на части машин pyexpat сломан
(см. opendata_refresh.py), а ячейки XLSX — плоские и однозначные.
"""

import datetime as dt
import gzip
import html
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

CACHE_DIR = os.path.expanduser(os.path.join("~", ".cache", "inn-check-ru", "cbr_reg"))
CACHE_DIR_ВИДИМЫЙ = "~/.cache/inn-check-ru/cbr_reg"
ИНДЕКС = "индекс.json.gz"
TIMEOUT = 180
UA = "inn-check-ru (+https://github.com/ilyautov/inn-check-ru)"
СТРАНИЦА = "https://www.cbr.ru/microfinance/registry/"
ПОРТАЛ = "https://www.cbr.ru/vfs/finmarkets/files/supervision"
# Лист -> статус записи. Лист не из списка и не из «пропустить» — схема сменилась.
РЕЕСТРЫ = {
    "мфо": {"файл": "list_MFO.xlsx", "название": "Государственный реестр МФО",
            "листы": {"Действующие": "действует", "Исключенные": "исключена"},
            "пропустить": ("Действующие МФК", "Действующие МКК")},
    "кпк": {"файл": "list_KPK_gov.xlsx",
            "название": "Государственный реестр кредитных потребительских кооперативов",
            "листы": {"Действующие кооперативы": "действует",
                      "Недействующие кооперативы": "исключён"}},
    "скпк": {"файл": "list_skpk.xlsx",
             "название": "Государственный реестр сельскохозяйственных кредитных "
                         "потребительских кооперативов",
             "листы": {"Действующие кооперативы": "действует",
                       "В стадии ликвид-и, реорган-и": "ликвидация_или_реорганизация",
                       "Ликвидированные;без статусаСКПК": "исключён"}},
    "ломбарды": {"файл": "list_PS.xlsx", "название": "Государственный реестр ломбардов",
                 "листы": {"Действующие ломбарды": "действует",
                           "Недействующие ломбарды": "исключён"}},
}
# Заголовок колонки -> поле индекса (по началу текста, без регистра). ФИО, адреса и
# контакты сюда не входят намеренно.
КОЛОНКИ = (
    ("инн", ("инн", "идентификационный номер налогоплательщика")),
    ("огрн", ("огрн", "основной государственный регистрационный номер")),
    ("вид", ("вид микрофинансовой организации",)),
    ("рег_номер", ("регистрационный номер записи",)),
    ("включён", ("дата внесения сведений о юридическом лице в государственный",
                 "дата начала действия права",
                 "дата внесения банком россии сведений о юридич")),
    ("исключён", ("дата внесения банком россии записи об исключении",
                  "дата прекращения действия права",
                  "дата исключения юридического лица из реестра")),
    ("ограничения", ("сведения об ограничении деятельности",)),
)
ЛИМИТ_ФАЙЛА = 50 << 20
ЛИМИТ_XML = 300 << 20
ДОЛЯ_ПАДЕНИЯ = 0.5
ДОЛЯ_БЕЗ_ИНН = 0.02          # больше 2 % строк без ИНН — схема не та
СВЕЖЕСТЬ_ДНЕЙ = 14
БЛОКИРОВКА_ЧАСОВ = 2


def _сегодня():
    return dt.datetime.now(dt.timezone.utc).astimezone().date().isoformat()


def _ssl_context():
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from fetch_counterparty import _build_ssl_context
        return _build_ssl_context()
    except Exception:
        return ssl.create_default_context()


def _get(url, лимит):
    """GET файла реестра: только с портала ЦБ, Referer — страница реестров (ссылки
    на файлы стоят на ней), честный UA, лимит размера."""
    if not url.startswith(ПОРТАЛ + "/"):
        raise ValueError("адрес вне реестров ЦБ: %s" % url[:80])
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": СТРАНИЦА})
    буфер, n = io.BytesIO(), 0
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=_ssl_context()) as r:
        while True:
            кусок = r.read(1 << 20)
            if not кусок:
                break
            n += len(кусок)
            if n > лимит:
                raise ValueError("ответ больше лимита %d МБ" % (лимит >> 20))
            буфер.write(кусок)
    return буфер.getvalue()


# --- XLSX -------------------------------------------------------------------

def _xml(z, имя):
    info = z.getinfo(имя)
    if info.file_size > ЛИМИТ_XML:
        raise ValueError("схема: %s больше лимита" % имя)
    return z.read(имя).decode("utf-8")


def _текст(фрагмент):
    return html.unescape("".join(re.findall(r"<t(?:\s[^>]*)?>(.*?)</t>", фрагмент, re.DOTALL)))


def листы(данные):
    """XLSX (bytes) -> {имя листа: [строки как {буква: значение}]}."""
    try:
        z = zipfile.ZipFile(io.BytesIO(данные))
    except zipfile.BadZipFile:
        # вместо файла — HTML (заглушка, капча, ошибка): не угадываем
        raise ValueError("схема: ответ не XLSX (%r…)" % данные[:40])
    имена = set(z.namelist())
    if not {"xl/workbook.xml", "xl/_rels/workbook.xml.rels"} <= имена:
        raise ValueError("схема: в XLSX нет workbook.xml")
    общие = ([_текст(si) for si in re.findall(r"<si>(.*?)</si>",
                                             _xml(z, "xl/sharedStrings.xml"), re.DOTALL)]
             if "xl/sharedStrings.xml" in имена else [])
    связи = {}
    for тег in re.findall(r"<Relationship\b[^>]*>", _xml(z, "xl/_rels/workbook.xml.rels")):
        i, цель = re.search(r'Id="([^"]+)"', тег), re.search(r'Target="([^"]+)"', тег)
        if i and цель:
            путь = цель.group(1).lstrip("/")
            связи[i.group(1)] = путь if путь.startswith("xl/") else "xl/" + путь
    out = {}
    for тег in re.findall(r"<sheet\b[^>]*>", _xml(z, "xl/workbook.xml")):
        имя = html.unescape(re.search(r'name="([^"]*)"', тег).group(1))
        rid = re.search(r'r:id="([^"]+)"', тег).group(1)
        строки = []
        for тело in re.findall(r"<row\b[^>]*>(.*?)</row>", _xml(z, связи[rid]), re.DOTALL):
            ячейки = {}
            for кол, атр, вн in re.findall(r'<c r="([A-Z]+)\d+"([^>]*?)(?:/>|>(.*?)</c>)',
                                            тело, re.DOTALL):
                тип = re.search(r't="(\w+)"', атр)
                тип = тип.group(1) if тип else None
                v = re.search(r"<v>(.*?)</v>", вн or "", re.DOTALL)
                if тип == "s" and v:
                    знач = общие[int(v.group(1))]
                elif тип == "inlineStr":
                    знач = _текст(вн or "")
                else:
                    знач = html.unescape(v.group(1)) if v else ""
                ячейки[кол] = знач.strip()
            строки.append(ячейки)
        out[имя] = строки
    return out


def _дата(s):
    """«ДД.ММ.ГГГГ» или серийный номер Excel -> ISO; пусто -> None; иное — схема."""
    s = (s or "").strip()
    if not s or s == "-":
        return None
    m = re.fullmatch(r"(\d{2})\.(\d{2})\.(\d{4})", s)
    if m:
        return dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
    if re.fullmatch(r"\d{5}(\.0+)?", s):
        return (dt.date(1899, 12, 30) + dt.timedelta(days=int(float(s)))).isoformat()
    raise ValueError("схема: дата %r" % s[:20])


def _колонки(строка):
    """Строка заголовка -> {поле: буква}. Первое совпадение по началу текста."""
    out = {}
    for буква, текст in строка.items():
        т = " ".join(текст.lower().replace("ё", "е").split())
        for поле, начала in КОЛОНКИ:
            if поле not in out and any(т == н or т.startswith(н + " ") or
                                       (len(н) > 20 and т.startswith(н)) for н in начала):
                out[поле] = буква
                break
    return out


def записи_реестра(реестр, данные):
    """XLSX реестра -> [(инн, запись)]. ValueError — схема не та."""
    опис = РЕЕСТРЫ[реестр]
    книга = листы(данные)
    лишние = set(книга) - set(опис["листы"]) - set(опис.get("пропустить", ()))
    нет = set(опис["листы"]) - set(книга)
    if лишние or нет:
        raise ValueError("схема: листы %s — незнакомые %s, нет %s"
                         % (опис["файл"], sorted(лишние), sorted(нет)))
    out = []
    for лист, статус in опис["листы"].items():
        строки = книга[лист]
        шапка = next((i for i, с in enumerate(строки[:10])
                      if "инн" in _колонки(с) and "огрн" in _колонки(с)), None)
        if шапка is None:
            raise ValueError("схема: на листе «%s» нет заголовка с ИНН и ОГРН" % лист)
        кол = _колонки(строки[шапка])
        if статус != "действует" and "исключён" not in кол and реестр != "скпк":
            raise ValueError("схема: на листе «%s» нет даты исключения" % лист)
        без_инн, n = 0, 0
        for с in строки[шапка + 1:]:
            if not any(с.values()):
                continue
            n += 1
            инн = с.get(кол["инн"], "")
            if not re.fullmatch(r"\d{10}|\d{12}", инн):
                без_инн += 1
                continue
            з = {"реестр": реестр, "статус": статус}
            for поле in ("огрн", "вид", "рег_номер", "ограничения"):
                if поле in кол and с.get(кол[поле]) and с.get(кол[поле]) != "-":
                    з[поле] = с[кол[поле]]
            for поле in ("включён", "исключён"):
                if поле in кол:
                    з[поле] = _дата(с.get(кол[поле]))
            out.append((инн, з))
        if n and без_инн > n * ДОЛЯ_БЕЗ_ИНН:
            raise ValueError("схема: на листе «%s» без ИНН %d строк из %d" % (лист, без_инн, n))
    return out


# --- индекс -----------------------------------------------------------------

def построить_индекс(файлы, путь, прежние=None, сегодня=None):
    """файлы: {реестр: (bytes, url)} -> индекс на диск. Всё или ничего."""
    по_инн, реестры = {}, {}
    сегодня = сегодня or _сегодня()
    for реестр, (данные, url) in файлы.items():
        зап = записи_реестра(реестр, данные)
        было = ((прежние or {}).get(реестр) or {}).get("записей") or 0
        if not зап or len(зап) < было * ДОЛЯ_ПАДЕНИЯ:
            raise ValueError("схема: в реестре %s %d записей (было %d)"
                             % (реестр, len(зап), было))
        for инн, з in зап:
            по_инн.setdefault(инн, []).append(з)
        реестры[реестр] = {"название": РЕЕСТРЫ[реестр]["название"], "url": url,
                           "записей": len(зап), "скачан": сегодня}
    папка = os.path.dirname(путь)
    fd, tmp = tempfile.mkstemp(dir=папка, prefix=".индекс-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as сырой, gzip.GzipFile(fileobj=сырой, mode="wb") as gz:
            gz.write(json.dumps({"построен": сегодня, "реестры": реестры, "по_инн": по_инн},
                                ensure_ascii=False).encode("utf-8"))
        os.replace(tmp, путь)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return реестры


def _прочитать(путь):
    with gzip.open(путь, "rt", encoding="utf-8") as fh:
        return json.load(fh)


class _Блокировка:
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


def refresh(cache_dir=None, получить=_get, сегодня=None):
    """Скачивает все реестры и пересобирает индекс; сбой любого — старый индекс цел.
    Файлы держатся в памяти (до 1,5 МБ каждый) и на диск не пишутся."""
    cache_dir = cache_dir or CACHE_DIR
    os.makedirs(cache_dir, exist_ok=True)
    путь = os.path.join(cache_dir, ИНДЕКС)
    try:
        блок = _Блокировка(cache_dir).__enter__()
    except FileExistsError:
        return {"статус": "индекс не обновлён: другой refresh уже идёт"}
    try:
        файлы, итог = {}, {}
        for реестр, опис in РЕЕСТРЫ.items():
            url = "%s/%s" % (ПОРТАЛ, опис["файл"])
            try:
                файлы[реестр] = (получить(url, ЛИМИТ_ФАЙЛА), url)
                итог[реестр] = "скачан"
            except Exception as e:
                итог[реестр] = "не скачан: %s: %s" % (type(e).__name__, str(e)[:160])
        if len(файлы) != len(РЕЕСТРЫ):
            return {"статус": "индекс не обновлён: скачаны не все реестры", "реестры": итог}
        try:
            прежние = _прочитать(путь).get("реестры") if os.path.exists(путь) else None
        except Exception:
            прежние = None
        try:
            реестры = построить_индекс(файлы, путь, прежние, сегодня)
        except Exception as e:
            return {"статус": "индекс не обновлён: %s" % str(e)[:200], "реестры": итог}
        return {"статус": "обновлено", "реестры": реестры}
    finally:
        блок.__exit__(None, None, None)


def устарел(индекс, сегодня):
    """Файлы ЦБ без срока годности: индекс старше СВЕЖЕСТЬ_ДНЕЙ от скачивания."""
    скачан = min((р.get("скачан") or "1970-01-01") for р in индекс["реестры"].values())
    if (dt.date.fromisoformat(сегодня) - dt.date.fromisoformat(скачан)).days > СВЕЖЕСТЬ_ДНЕЙ:
        return ["реестры ЦБ скачаны %s, старше %d дней — обновите: cbr_registries.py "
                "--refresh" % (скачан, СВЕЖЕСТЬ_ДНЕЙ)]
    return []


def lookup(inn, cache_dir=None, сегодня=None):
    """-> (dict|None, заметка). None — индекса нет."""
    cache_dir = cache_dir or CACHE_DIR
    путь = os.path.join(cache_dir, ИНДЕКС)
    try:
        индекс = _прочитать(путь)
    except FileNotFoundError:
        return None, ("индекса нет (%s) — сначала cbr_registries.py --refresh"
                      % (CACHE_DIR_ВИДИМЫЙ if cache_dir == CACHE_DIR else путь))
    сегодня = сегодня or _сегодня()
    записи = индекс["по_инн"].get(str(inn), [])
    return {
        "в_реестрах": bool(записи),
        "действует_в": sorted({з["реестр"] for з in записи if з["статус"] == "действует"}),
        "записи": записи,
        "охват": "госреестры ЦБ: МФО, кредитные потребительские кооперативы, "
                 "сельхозкооперативы, ломбарды — действующие и исключённые",
        "скачан": {k: р.get("скачан") for k, р in индекс["реестры"].items()},
        "предупреждения": устарел(индекс, сегодня),
        "источник": СТРАНИЦА,
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
