#!/usr/bin/env python3
"""
run_fsa_eval.py — 7z-распаковщик (sevenzip.py) и сертификаты/декларации
Росаккредитации (fsa_registries.py) офлайн, на синтетических 7z той же формы,
что живые выгрузки 28.09.2026 (один CSV, LZMA2, обычный заголовок).

Архивы собирает сам тест (писатель 7z ниже, stdlib lzma). Проверяется:
распаковка обычного и сжатого заголовка, LZMA1; битый CRC, оборванный архив,
лишний файл, чужой кодек — ValueError; колонки по имени (сертификаты и
декларации называют их по-разному); более свежая выгрузка побеждает; роли
заявитель / изготовитель; пропуск месяца виден; в индексе нет наименований и
адресов; схема и сбой скачивания не затирают индекс; разобранный месяц не
качается второй раз, выпавший из окна — удаляется; блок движка ok / пусто /
не проверено при устаревших выгрузках.

Там же — живой блок «операторы_пд» (поиск реестра операторов персональных данных
Роскомнадзора): разбор таблицы, «пусто», чужие ИНН и смена вёрстки — «не
проверено», ровно один GET с Referer страницы, наименование (у ИП — ФИО) в
ответ не попадает. PASS/FAIL, stdlib, CI.
"""

import datetime as dt
import gzip
import importlib.util
import io
import json
import lzma
import os
import struct
import sys
import tempfile
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
СЕГОДНЯ = "2026-09-28"
ФИО = "ФИО УБРАНО ИЗ ФИКСТУРЫ"
АДРЕС = "г. Условный, ул. Условная, 1"


def load(имя):
    sys.path.insert(0, str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(имя, ROOT / "scripts" / ("%s.py" % имя))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check(errors, cond, msg):
    if not cond:
        errors.append(msg)


# --- писатель 7z (только для тестов) -----------------------------------------

def _число(v):
    """UINT64 7z: n старших единиц первого байта — n байт дальше (младшие
    вперёд), остаток первого байта — старшие биты значения."""
    for n in range(8):
        if v < 1 << (8 * n + 7 - n):
            первый = ((0xFF << (8 - n)) & 0xFF) | (v >> (8 * n))
            return bytes([первый]) + v.to_bytes(8, "little")[:n]
    return b"\xff" + v.to_bytes(8, "little")


def _потоки(поз, упаковано, распаковано, crc, lzma1=False, словарь=b"\x10", crc_блока=None):
    if lzma1:
        кодек = b"\x23\x03\x01\x01\x05" + bytes([3 + 9 * 0 + 45 * 2]) + (1 << 20).to_bytes(4, "little")
    else:
        кодек = b"\x21\x21\x01" + словарь              # LZMA2, 0x10 — словарь 1 МБ
    return (b"\x06" + _число(поз) + b"\x01\x09" + _число(упаковано) + b"\x00"
            + b"\x07\x0b\x01\x00\x01" + кодек + b"\x0c" + _число(распаковано)
            + (b"\x0a\x01" + struct.pack("<I", crc_блока) if crc_блока is not None else b"")
            + b"\x00"
            + (b"\x08\x0a\x01" + struct.pack("<I", crc) + b"\x00" if crc is not None else b"")
            + b"\x00")


def _сжать(данные, lzma1=False):
    if lzma1:
        ф = {"id": lzma.FILTER_LZMA1, "dict_size": 1 << 20, "lc": 3, "lp": 0, "pb": 2}
    else:
        ф = {"id": lzma.FILTER_LZMA2, "dict_size": 1 << 20}
    return lzma.compress(данные, format=lzma.FORMAT_RAW, filters=[ф])


def сделать_7z(имя, данные, сжатый_заголовок=False, lzma1=False, файлов=1, битый_crc=False,
               лишний_байт=0, без_конца=False, без_crc=False, словарь=b"\x10",
               раздуть_заголовок=False, crc_блока=None):
    упак = _сжать(данные, lzma1)
    if без_конца:
        упак = упак[:-1]                            # LZMA2 без маркера конца (0x00)
    crc = None if без_crc else zlib.crc32(данные) ^ (1 if битый_crc else 0)
    имена = b"".join(("%s%d" % (имя, i) if i else имя).encode("utf-16-le") + b"\x00\x00"
                     for i in range(файлов))
    заголовок = (b"\x01\x04" + _потоки(0, len(упак), len(данные) + лишний_байт, crc, lzma1,
                                     словарь, crc_блока)
                 + b"\x05" + _число(файлов) + b"\x11" + _число(len(имена) + 1) + b"\x00"
                 + имена + b"\x00\x00")
    тело = упак
    if сжатый_заголовок:
        з = _сжать(заголовок)
        тело = упак + з
        заголовок = b"\x17" + _потоки(len(упак), len(з), (100 << 20) if раздуть_заголовок
                                        else len(заголовок), zlib.crc32(заголовок))
    старт = struct.pack("<QQI", len(тело), len(заголовок), zlib.crc32(заголовок))
    return (b"7z\xbc\xaf\x27\x1c\x00\x04" + struct.pack("<I", zlib.crc32(старт)) + старт
            + тело + заголовок)


# --- выгрузки ------------------------------------------------------------------

ШАПКА_С = ["id", "Статус", "Номер СС", "Дата рег", "Срок действия", "Тип заявителя",
           "Заявитель", "Адрес заявителя", "ИНН заявителя", "Изготовитель",
           "Адрес изготовителя", "ИНН изготовителя", "Группа продукции",
           "Полное наименование продукции", "Дата приостановки", "Дата прекращения"]
ШАПКА_Д = ["id", "Статус", "Номер ДС", "Дата рег", "Срок действия", "Заявитель",
           "Адрес Заявителя", "ИНН Заявителя", "Изготовитель", "ИНН производителя",
           "Группа продукции", "Полное наименование", "Дата приостановки", "Дата прекращения"]


def строка_с(id_, статус, заявитель, изготовитель="", дата="03.08.2021", прекращен=""):
    return [id_, статус, "ЕАЭС RU С-RU.ТЕСТ.В.%s/21" % id_, дата, "02.08.2026",
            "Юридическое лицо", ФИО, АДРЕС, заявитель, ФИО, АДРЕС, изготовитель,
            "Вентиляторы промышленные", "Вентилятор " + "х" * 300, "", прекращен]


def строка_д(id_, статус, заявитель, изготовитель="", дата="27.08.2021"):
    return [id_, статус, "ЕАЭС N RU Д-RU.ТЕСТ.В.%s/21" % id_, дата, "25.08.2026", ФИО,
            АДРЕС, заявитель, ФИО, изготовитель, "", "Кастрюли \"Тест\"; стальные", "", ""]


def csv_(шапка, строки):
    def кв(x):
        return '"%s"' % x.replace('"', '""')
    return ("\n".join(";".join(кв(x) for x in с) for с in [шапка] + строки) + "\n").encode("utf-8")


def месяцы_по_умолчанию():
    """Сертификаты: июль и август; декларации: июнь и август (июля нет — пропуск)."""
    return {
        ("rss", "20260731"): ("rss_20260701_20260731.csv", csv_(ШАПКА_С, [
            строка_с("1", "Действует", "7702664252", "7702664252"),
            строка_с("2", "Действует", "7708503727", ""),
            строка_с("9", "Действует", "", "")])),
        ("rss", "20260831"): ("rss_20260801_20260831.csv", csv_(ШАПКА_С, [
            строка_с("2", "Прекращён", "7708503727", "", прекращен="15.08.2026"),
            строка_с("3", "Недействителен", "7708503727", "504110181262", дата="00.00.0000")])),
        ("rds", "20260630"): ("rds_20260601-20260630.csv", csv_(ШАПКА_Д, [
            строка_д("1", "Действует", "7702664252"),
            строка_д("8", "Действует", "7707083893")[:11] + ["Сковороды " + "ж" * 300]
            + строка_д("8", "Действует", "7707083893")[12:]])),
        ("rds", "20260831"): ("rds_20260801_20260831.csv", csv_(ШАПКА_Д, [
            строка_д("5", "Архивный", "7702664252", "7708503727")])),
    }


def страница(вид, даты, структура="20260101"):
    return ("<html>" + "".join(
        '<a href="https://fsa.gov.ru/opendata/7736638268-%s/data-%s-structure-%s.7z">'
        'x</a><a href="https://fsa.gov.ru/opendata/7736638268-%s/data-%s-structure-20250101.7z">'
        'старая структура</a>' % (вид, д, структура, вид, д) for д in даты) + "</html>").encode("utf-8")


def fsa_get(месяцы=None, счётчик=None, сломать=None, структура="20260101"):
    месяцы = месяцы_по_умолчанию() if месяцы is None else месяцы

    def get(url, referer, лимит, в_файл=None):
        if счётчик is not None:
            счётчик.append(url)
        if сломать == "сеть":
            raise OSError("обрыв соединения")
        for вид in ("rss", "rds"):
            if url == "https://fsa.gov.ru/opendata/7736638268-%s/" % вид:
                assert referer == "https://fsa.gov.ru/opendata/", referer
                return страница(вид, sorted({д for в, д in месяцы if в == вид}), структура)
            if url.startswith("https://fsa.gov.ru/opendata/7736638268-%s/data-" % вид):
                assert "structure-" + структура in url, url
                assert referer == "https://fsa.gov.ru/opendata/7736638268-%s/" % вид, referer
                имя, данные = месяцы[(вид, url.split("data-")[1][:8])]
                архив = сделать_7z(имя, данные)
                if сломать == "обрыв":
                    архив = архив[:len(архив) // 2]
                with open(в_файл, "wb") as fh:
                    fh.write(архив)
                return None
        raise AssertionError(url)
    return get


# --- кейсы ----------------------------------------------------------------------

def case_7z(sz):
    errors = []
    данные = ("строка;ячейка\n" * 50000).encode("utf-8")
    with tempfile.TemporaryDirectory() as td:
        def путь(имя, архив):
            п = os.path.join(td, имя)
            with open(п, "wb") as fh:
                fh.write(архив)
            return п
        # повтор на расстоянии 700 КБ: словарь из свойств кодека должен быть 1 МБ, а не меньше
        блок = b"".join(zlib.crc32(i.to_bytes(4, "little")).to_bytes(4, "little")
                        for i in range(175000))
        п = путь("словарь.7z", сделать_7z("x.csv", блок * 3))
        check(errors, b"".join(sz.читать(п)) == блок * 3, "словарь LZMA2 из свойств")
        for метка, арг in (("обычный", {}), ("CRC блока", {"crc_блока": zlib.crc32(данные)}),
                           ("только CRC блока", {"без_crc": True, "crc_блока": zlib.crc32(данные)}), ("сжатый заголовок", {"сжатый_заголовок": True}),
                           ("lzma1", {"lzma1": True})):
            п = путь(метка + ".7z", сделать_7z("данные.csv", данные, **арг))
            check(errors, sz.оглавление(п) == ("данные.csv", len(данные)), "%s: оглавление" % метка)
            check(errors, b"".join(sz.читать(п, кусок=4096)) == данные, "%s: данные" % метка)
            with sz.открыть(п) as fh:
                check(errors, fh.read() == данные, "%s: открыть()" % метка)
        пустой = путь("пустой.7z", сделать_7z("x.csv", b""))
        check(errors, b"".join(sz.читать(пустой)) == b"", "пустой файл")
        for метка, архив, ждём in (
                ("битый CRC", сделать_7z("x.csv", данные, битый_crc=True), "CRC"),
                ("два файла", сделать_7z("x.csv", данные, файлов=2), "файлов"),
                ("длина", сделать_7z("x.csv", данные, лишний_байт=1), "вместо"),
                ("больше заявленного", сделать_7z("x.csv", данные, лишний_байт=-1), "больше"),
                ("без маркера конца", сделать_7z("x.csv", данные, без_конца=True), "LZMA2"),
                ("без CRC", сделать_7z("x.csv", данные, без_crc=True), "нет CRC"),
                ("битый CRC блока", сделать_7z("x.csv", данные, crc_блока=zlib.crc32(данные) ^ 1),
                 "CRC"),
                ("заголовок 100 МБ", сделать_7z("x.csv", данные, сжатый_заголовок=True,
                                                раздуть_заголовок=True), "заголовок"),
                ("словарь 4 ГБ", сделать_7z("x.csv", данные, словарь=b"\x28"), "словарь"),
                ("битый заголовок", сделать_7z("x.csv", данные)[:-6] + b"y" + сделать_7z(
                    "x.csv", данные)[-5:], "оборван"),
                ("не 7z", b"PK\x03\x04" + b"\x00" * 60, "не 7z"),
                ("обрыв", сделать_7z("x.csv", данные)[:-10], "оборван"),
                ("обрыв данных", сделать_7z("x.csv", данные)[:40] + b"\x00" * 10, "оборван")):
            п = путь("плохой.7z", архив)
            try:
                b"".join(sz.читать(п))
                errors.append("%s: нет ошибки" % метка)
            except ValueError as e:
                check(errors, ждём in str(e), "%s: %s" % (метка, e))
        # чужой кодек (BCJ) — отказ, а не попытка угадать
        архив = bytearray(сделать_7z("x.csv", данные))
        архив = bytes(архив)
        заголовок_с = struct.unpack("<Q", архив[12:20])[0] + 32
        з = bytearray(архив[заголовок_с:])
        i = з.index(b"\x21\x21\x01\x10")
        з[i:i + 4] = b"\x21\x03\x01\x10"
        з = bytes(з)
        старт = struct.pack("<QQI", заголовок_с - 32, len(з), zlib.crc32(з))
        архив = архив[:8] + struct.pack("<I", zlib.crc32(старт)) + старт + архив[32:заголовок_с] + з
        try:
            sz.оглавление(путь("кодек.7z", архив))
            errors.append("чужой кодек: нет ошибки")
        except ValueError as e:
            check(errors, "кодек" in str(e), "чужой кодек: %s" % e)
        for v in (0, 1, 127, 128, 16383, 16384, 1 << 30, 1 << 40, (1 << 64) - 1):
            б = sz._Буфер(_число(v))
            check(errors, б.число() == v and б.i == len(б.b), "UINT64 %d" % v)
    return "7z", errors


def case_fsa(fsa):
    errors = []
    with tempfile.TemporaryDirectory() as td:
        счётчик = []
        рез = fsa.refresh(cache_dir=td, get=fsa_get(счётчик=счётчик), сегодня=СЕГОДНЯ)
        check(errors, рез["статус"] == "обновлено" and рез["скачано_выгрузок"] == 4, рез)
        охват = рез.get("охват", {})
        check(errors, охват.get("сертификат") == {"с": "2026-07-01", "по": "2026-08-31",
                                                 "выгрузок": 2, "пропуски": []}, охват)
        check(errors, охват.get("декларация", {}).get("пропуски")
              == [["2026-07-01", "2026-07-31"]], "пропуск июля: %r" % охват)
        сырой = gzip.decompress(Path(td, fsa.ИНДЕКС).read_bytes()).decode("utf-8")
        for лишнее in (ФИО, "Условная", "Вентилятор х"):
            check(errors, лишнее not in сырой, "в индексе %r" % лишнее)

        р, _ = fsa.lookup("7708503727", cache_dir=td, сегодня=СЕГОДНЯ)
        по_номеру = {д["номер"][-5:]: д for д in р["показаны"]}
        check(errors, р["документов"] == 3 and р["действующих"] == 0, р)
        д2 = по_номеру.get(".2/21", {})
        check(errors, д2.get("статус") == "прекращен" and д2.get("прекращен") == "2026-08-15"
              and д2.get("роль") == "заявитель", "свежая выгрузка побеждает: %r" % д2)
        д3 = по_номеру.get(".3/21", {})
        check(errors, д3.get("статус") == "недействителен" and д3.get("зарегистрирован") is None,
              "дата 00.00.0000 -> None: %r" % д3)
        д5 = по_номеру.get(".5/21", {})
        check(errors, д5.get("вид") == "декларация" and д5.get("роль") == "изготовитель"
              and д5.get("продукция") == 'Кастрюли "Тест"; стальные', "декларация: %r" % д5)
        check(errors, р["по_статусам"] == {"сертификат": {"прекращен": 1, "недействителен": 1},
                                          "декларация": {"архивный": 1}}, р["по_статусам"])

        р, _ = fsa.lookup("7702664252", cache_dir=td, сегодня=СЕГОДНЯ)
        роли = sorted((д["вид"], д["роль"]) for д in р["показаны"])
        check(errors, роли == [("декларация", "заявитель"), ("декларация", "заявитель"),
                               ("сертификат", "заявитель и изготовитель")], роли)
        check(errors, р["показаны"][0]["статус"] == "действует"
              and р["показаны"][-1]["статус"] == "архивный", "действующие первыми")
        р, _ = fsa.lookup("7707083893", cache_dir=td, сегодня=СЕГОДНЯ)
        пр = р["показаны"][0]["продукция"]
        check(errors, len(пр) == fsa.ПРОДУКЦИЯ_СИМВОЛОВ and пр.endswith("ж…"), "обрезка: %r" % пр)
        р, _ = fsa.lookup("504110181262", cache_dir=td, сегодня=СЕГОДНЯ)
        check(errors, р["документов"] == 1 and р["показаны"][0]["роль"] == "изготовитель", р)
        р, _ = fsa.lookup("7700000000", cache_dir=td, сегодня=СЕГОДНЯ)
        check(errors, р["в_выгрузках"] is False and not р["предупреждения"]
              and "не весь реестр" in р["оговорка"], р)
        р, _ = fsa.lookup("7700000000", cache_dir=td, сегодня="2026-12-01")
        check(errors, len(р["предупреждения"]) == 2 and "старше" in р["предупреждения"][0], р)

        # второй refresh — только страницы наборов, архивы не качаются
        счётчик.clear()
        рез = fsa.refresh(cache_dir=td, get=fsa_get(счётчик=счётчик), сегодня=СЕГОДНЯ)
        check(errors, рез.get("скачано_выгрузок") == 0 and len(счётчик) == 2, (рез, счётчик))
        # Росаккредитация переложила август с новой структурой: август качается заново,
        # и свежая версия документа 2 без ИНН вытесняет старую (у 7708503727 его нет)
        м = месяцы_по_умолчанию()
        м[("rss", "20260831")] = ("rss_20260801_20260831.csv", csv_(ШАПКА_С, [
            строка_с("2", "Прекращён", "", ""),
            строка_с("3", "Недействителен", "7708503727", "504110181262")]))
        счётчик.clear()
        рез = fsa.refresh(cache_dir=td, get=fsa_get(м, счётчик=счётчик, структура="20261001"),
                          сегодня=СЕГОДНЯ)
        check(errors, рез.get("скачано_выгрузок") == 4, ("новая структура", рез))
        р, _ = fsa.lookup("7708503727", cache_dir=td, сегодня=СЕГОДНЯ)
        check(errors, [д["номер"][-5:] for д in р["показаны"] if д["вид"] == "сертификат"]
              == [".3/21"],
              "свежая строка без ИНН вытесняет старую: %r" % р["показаны"])
        fsa.refresh(cache_dir=td, get=fsa_get(), сегодня=СЕГОДНЯ)
        # окно в 1 месяц: старые разобранные месяцы удаляются
        рез = fsa.refresh(cache_dir=td, get=fsa_get(), сегодня=СЕГОДНЯ, месяцев=1)
        check(errors, sorted(os.listdir(os.path.join(td, fsa.МЕСЯЦЫ_ПАПКА)))
              == ["rds-20260831.json.gz", "rss-20260831.json.gz"], os.listdir(os.path.join(td, fsa.МЕСЯЦЫ_ПАПКА)))
        р, _ = fsa.lookup("7708503727", cache_dir=td, сегодня=СЕГОДНЯ)
        check(errors, р["документов"] == 3 and р["охват"]["сертификат"]["с"] == "2026-08-01", р)

    # схема и сбои не затирают индекс
    плохие = {
        "нет колонки ИНН": {("rss", "20260831"): ("rss_20260801_20260831.csv",
                                                  csv_([к for к in ШАПКА_С if к != "ИНН заявителя"],
                                                       [строка_с("1", "Действует", "7702664252")[:8]
                                                        + строка_с("1", "Действует", "7702664252")[9:]]))},
        "HTML вместо CSV": {("rss", "20260831"): ("rss_20260801_20260831.csv",
                                                  b"<html>\xd0\x9e\xd1\x88\xd0\xb8\xd0\xb1\xd0\xba\xd0\xb0</html>\n")},
        "чужие статусы": {("rss", "20260831"): ("rss_20260801_20260831.csv", csv_(ШАПКА_С, [
            строка_с(str(i), "Черновик", "7702664252") for i in range(10)]))},
        "строки не по шапке": {("rss", "20260831"): ("rss_20260801_20260831.csv", csv_(ШАПКА_С, [
            строка_с("1", "Действует", "7702664252")[:5]]))},
        "нет периода в имени": {("rss", "20260831"): ("rss.csv", csv_(ШАПКА_С, [
            строка_с("1", "Действует", "7702664252")]))},
    }
    for метка, подмена in list(плохие.items()) + [("сеть", None), ("обрыв", None)]:
        with tempfile.TemporaryDirectory() as td:
            fsa.refresh(cache_dir=td, get=fsa_get(), сегодня=СЕГОДНЯ)
            было = Path(td, fsa.ИНДЕКС).read_bytes()
            месяцы = месяцы_по_умолчанию()
            месяцы[("rss", "20260930")] = (подмена[("rss", "20260831")] if подмена else (
                "rss_20260901_20260930.csv", csv_(ШАПКА_С, [строка_с("7", "Действует", "7702664252")])))
            сломать = метка if метка in ("сеть", "обрыв") else None
            рез = fsa.refresh(cache_dir=td, get=fsa_get(месяцы, сломать=сломать), сегодня=СЕГОДНЯ)
            check(errors, рез["статус"].startswith("индекс не обновлён")
                  and Path(td, fsa.ИНДЕКС).read_bytes() == было,
                  "%s: %r" % (метка, рез))
            check(errors, not [и for и in os.listdir(td) if и.startswith(".выгрузка-")],
                  "%s: временный файл остался" % метка)
    with tempfile.TemporaryDirectory() as td:
        р, note = fsa.lookup("7702664252", cache_dir=td)
        check(errors, р is None and "--refresh" in note, note)
    return "фса", errors


def case_движок():
    errors = []
    дом = os.environ.get("HOME")
    with tempfile.TemporaryDirectory() as td:
        os.environ["HOME"] = td
        try:
            fc = load("fetch_counterparty")
            блок = "сертификаты_фса"
            данные, av = fc.fetch_fsa_registries(None, "7702664252")
            check(errors, данные is None and av["причина"].startswith("кэш:")
                  and "INN_CHECK_PROXY" in av["причина"], "без индекса: %r" % av)
            check(errors, блок in fc.FETCHERS and fc.SOURCES[блок]["deal_killer"] is False
                  and fc.SOURCES[блок]["пусто_с_оговоркой"], "регистрация")
            fsa = load("fsa_registries")
            # выгрузки за прошлый месяц — иначе блок счёл бы их устаревшими по календарю
            конец = (dt.datetime.now(dt.timezone.utc).astimezone().date().replace(day=1)
                     - dt.timedelta(days=1))
            к, н = конец.strftime("%Y%m%d"), конец.replace(day=1).strftime("%Y%m%d")
            м = месяцы_по_умолчанию()
            свежие = {(вид, к): ("%s_%s_%s.csv" % (вид, н, к), м[(вид, "20260831")][1])
                      for вид in ("rss", "rds")}
            рез = fsa.refresh(cache_dir=fsa.CACHE_DIR, get=fsa_get(свежие))
            check(errors, рез["статус"] == "обновлено", рез)
            данные, av = fc.fetch_fsa_registries(None, "7702664252")
            check(errors, av["состояние"] == "ok" and данные and данные["документов"], av)
            пусто, av = fc.fetch_fsa_registries(None, "7700000000")
            check(errors, пусто is None and av["состояние"] == "пусто"
                  and fc.SOURCES[блок]["пусто_с_оговоркой"] in av["причина"], av)
            snap = load("snapshot")
            данные, av = fc.fetch_fsa_registries(None, "7702664252")
            а = {"инн": "7702664252", блок: данные, "_доступность": {блок: av}}
            б = json.loads(json.dumps(а, ensure_ascii=False))
            б[блок]["скачан"] = "2026-10-05"
            б[блок]["охват"] = {}
            б[блок]["предупреждения"] = ["x"]
            check(errors, snap.хеш_остального(а) == snap.хеш_остального(б),
                  "даты выгрузки в хеше снимка")
            путь = os.path.join(fsa.CACHE_DIR, fsa.ИНДЕКС)
            и = fsa.dump_tools.прочитать_индекс(путь)
            for о in и["охват"].values():
                о["по"] = "2026-01-31"
            fsa.dump_tools.записать_индекс(путь, и)
            for инн in ("7702664252", "7700000000"):
                _, av = fc.fetch_fsa_registries(None, инн)
                check(errors, av["состояние"] == "не проверено" and "старше" in av["причина"],
                      "устаревшие выгрузки %s: %r" % (инн, av))
        finally:
            if дом is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = дом
    return "блок-движка", errors


ПД_ШАПКА = """<form method="GET" action="./"></form>%s<br>
        <table width="100%%" cellspacing="0" id="ResList1" class="TblList">
        <thead><tr>
         <td valign="top">Регистр.<br>номер</td>
         <td valign="top">Наименование оператора / ИНН</td>
         <td valign="top">Основание включения в реестр</th>
         <td valign="top">Дата регистрации уведомления</td>
         <td valign="top">Дата начала обработки</td>
        </tr></thead>%s</table>"""


def пд_строка(инн, номер="77-00-000001", тип="юридическое лицо"):
    return """
            <tr class='clmn1'>
             <td width='42' valign='top'><nobr>%s</nobr><br></td>
             <td>
                <a href="?id=%s" valign='top' align='left'>%s</a><br/>ИНН: %s<br/>%s
             </td>
             <td valign='top'>Приказ&nbsp;№&nbsp;1 от&nbsp;24.12.2019</td>
             <td valign='top'>16.12.2019</td>
             <td valign='top'>18.09.2002</td>
            </tr>""" % (номер, номер, ФИО, инн, тип)


class _Ответ:
    def __init__(self, код, тело):
        self.status, self._тело = код, тело.encode("utf-8")

    def read(self, *a):
        return self._тело


class _Opener:
    def __init__(self, код, тело):
        self.код, self.тело, self.запросы = код, тело, []

    def open(self, req, timeout=None):
        import urllib.error
        self.запросы.append(req)
        if self.код != 200:
            raise urllib.error.HTTPError(req.full_url, self.код, "x", {}, io.BytesIO(b""))
        return _Ответ(self.код, self.тело)


def case_пд():
    errors = []
    fc = load("fetch_counterparty")
    блок = "операторы_пд"
    check(errors, блок in fc.FETCHERS and fc.SOURCES[блок]["deal_killer"] is False
          and fc.SOURCES[блок]["требует"] == "сеть", "регистрация")
    op = _Opener(200, ПД_ШАПКА % ("", пд_строка("7702664252") + пд_строка(
        "504110181262", "50-00-000002", "индивидуальный предприниматель")))
    данные, av = fc.fetch_pd_operators(op, "7702664252")
    check(errors, av["состояние"] == "ok" and данные["записи"] == [{
        "рег_номер": "77-00-000001", "тип_оператора": "юридическое лицо",
        "основание": "Приказ № 1 от 24.12.2019", "уведомление_от": "2019-12-16",
        "обработка_с": "2002-09-18"}], данные)
    check(errors, ФИО not in json.dumps(данные, ensure_ascii=False), "наименование в ответе")
    check(errors, len(op.запросы) == 1, "запросов: %d" % len(op.запросы))
    req = op.запросы[0]
    check(errors, req.full_url == fc.PD_СПИСОК + "?act=search&name_full=&inn=7702664252&regn="
          and req.get_header("Referer") == fc.PD_СПИСОК
          and req.get_header("User-agent") == fc.UA_ПРОЕКТА
          and not req.get_header("X-requested-with"), (req.full_url, req.headers))
    данные, av = fc.fetch_pd_operators(op, "504110181262")
    check(errors, данные and данные["записи"][0]["тип_оператора"]
          == "индивидуальный предприниматель", данные)
    for метка, тело, ждём in (
            ("пусто", ПД_ШАПКА % ("<p>Записей не найдено</p>", ""), "пусто"),
            ("чужие", ПД_ШАПКА % ("", пд_строка("7708503727")), "не проверено"),
            ("без таблицы", "<html>Подтвердите, что вы не робот</html>", "не проверено"),
            ("ни строк, ни надписи", ПД_ШАПКА % ("", ""), "не проверено"),
            ("4 колонки", ПД_ШАПКА % ("", пд_строка("7702664252").replace(
                "<td valign='top'>18.09.2002</td>", "")), "не проверено")):
        данные, av = fc.fetch_pd_operators(_Opener(200, тело), "7702664252")
        check(errors, av["состояние"] == ждём and (ждём == "пусто" or av["причина"].startswith("схема:")),
              "%s: %r" % (метка, av))
        if ждём == "пусто":
            check(errors, fc.SOURCES[блок]["пусто_с_оговоркой"] in av["причина"], av)
    op = _Opener(403, "")
    try:
        fc.fetch_pd_operators(op, "7702664252")
        errors.append("403: нет исключения")
    except fc.SourceUnavailable as e:
        check(errors, str(e).startswith("гео:") and len(op.запросы) == 1, (str(e), len(op.запросы)))
    return "операторы-пд", errors


def main():
    failed = 0
    кейсы = (lambda: case_7z(load("sevenzip")), lambda: case_fsa(load("fsa_registries")),
             case_движок, case_пд)
    for кейс in кейсы:
        имя, errors = кейс()
        if errors:
            failed += 1
            print("FAIL %s" % имя)
            for e in errors:
                print("  -", e)
        else:
            print("PASS %s" % имя)
    if failed:
        print("FAIL: %d/%d кейсов упало" % (failed, len(кейсы)))
        return 1
    print("PASS: все %d кейсов зелёные" % len(кейсы))
    return 0


if __name__ == "__main__":
    sys.exit(main())
