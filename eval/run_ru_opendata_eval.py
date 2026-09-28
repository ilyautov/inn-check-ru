#!/usr/bin/env python3
"""
run_ru_opendata_eval.py — реестры ФТС, РКН и РАР (fts_registries.py,
rkn_registries.py, rar_licenses.py) и общий dump_tools.py офлайн, на
синтетических выгрузках той же формы, что живые 28.09.2026.

Проверяется: разбор и статусы (коды IDEL ФТС, статусы РКН и РАР как в реестре),
в индексе нет наименований, адресов, телефонов, e-mail; схема (HTML вместо
данных, нет колонки, чужой статус, мало ИНН, падение объёма) и сбой скачивания
не затирают индекс; обрыв по Content-Length; прокси (socks5 и негодный URL);
блоки движка ok / пусто / не проверено при устаревшей выгрузке; даты выгрузки
не меняют хеш снимка. PASS/FAIL, stdlib, CI.
"""

import gzip
import importlib.util
import io
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
СЕГОДНЯ = "2026-09-28"
ФИО = "ФИО УБРАНО ИЗ ФИКСТУРЫ"
ТЕЛЕФОН = "+7 (000) 000-00-00"
ПОЧТА = "info@example.invalid"
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


def сырой_индекс(путь):
    with gzip.open(путь) as fh:
        return fh.read().decode("utf-8")


# --- ФТС ----------------------------------------------------------------------

def csv_(строки):
    return "\n".join(";".join('"%s"' % x for x in с) for с in строки).encode("utf-8")


def фтс_файлы(подмена=None):
    файлы = {
        "reesrtampredst": csv_([
            ["ST", "РЕГ НОМЕР", "ДАТА ВКЛЮЧЕНИЯ В РЕЕСТР", "НАИМЕНОВАНИЕ ТАМОЖ. ПРЕД", "ADROWN",
             "TELEFON", "EMAIL", "INN", "KPP"],
            ["RU", "0003", "2010.09.01", "ООО КАРГО", АДРЕС, ТЕЛЕФОН, ПОЧТА, "7702664252",
             "771501001"]]),
        "reestrtamperev": csv_([
            ["ST", "TYP_DOC", "NLIC", "PR_PER", "DBEGIN", "DEND", "IDEL", "OWNER", "ADRROWN",
             "EMAIL", "OGRN", "INN"],
            ["RU", "2", "10000/0001", "А", "2015.01.01", "2030.01.01", "0", "ООО", АДРЕС, ПОЧТА,
             "1", "7708503727"],
            ["RU", "2", "10000/0002", "", "2010.01.01", "2014.01.01", "0", "ООО", АДРЕС, ПОЧТА,
             "1", "7708503727"],
            ["RU", "2", "10000/0003", "", "2010.01.01", "2012.01.01", "3", "ООО", АДРЕС, ПОЧТА,
             "1", "7708503727"],
            ["RU", "2", "10000/0004", "", "2010.01.01", "2012.01.01", "7", "ООО", АДРЕС, ПОЧТА,
             "1", "7708503727"],
            # иностранный перевозчик: российского ИНН нет, не берётся
            ["BY", "3", "00001", "", "2015.01.01", "2015.08.24", "0", "OOO", АДРЕС, ПОЧТА, "",
             "09413758"]]),
        "revlsvrhr": csv_([
            ["ST", "KODT", "NLIC", "TYP_DOC", "DBEGIN", "DEND", "IDEL", "OWNER", "ADROWN",
             "TELEFON", "D_ISL", "OGRN", "INN"],
            ["RU", "10502070", "10502/300813/10058/5", "2", "2022.09.30", "", "9", "ПАО",
             АДРЕС, ТЕЛЕФОН, "", "1", "7708503727"]]),
        "reestrvtamskl": xlsx_like_csv(
            ["№ п/п", "Страна", "Реквизиты документа, подтверждающего включение",
             "Дата включения юридического лица в реестр владельцев ТС",
             "Организационно-правовая форма, наименование владельца ТС",
             "Местонахождение, почтовый адрес владельца ТС", "ИНН"],
            [["1", "РФ", "10216/009/А", "20.03.2015", "АО", АДРЕС, "7805076598"]]),
        "reestrmbt": xlsx_like_csv(
            ["№ п/п", "Таможня", "Номер свидетельства о включении в реестр",
             "Дата включения юридического лица в реестр", "Наименование", "Место нахождения",
             "Идентификационный номер налогоплательщика"],
            [["1", "Хабаровская таможня", "10703/002/003", "22.11.2011", "ООО", АДРЕС,
              "7705606435"]]),
    }
    файлы.update(подмена or {})
    return файлы


def xlsx_like_csv(шапка, строки):
    """Как живые файлы складов и МБТ: преамбула, шапка, нумерация колонок, раздел."""
    return csv_([["", "", "Приложение"], ["РЕЕСТР"], шапка,
                 [str(i + 1) for i in range(len(шапка))], ["Северо-Западное таможенное управление"]]
                + строки)


def фтс_get(fts, файлы, сломать=None):
    def get(url, referer, лимит):
        for набор, данные in файлы.items():
            if url.endswith("/opendata/7730176610-" + набор):
                return ('<a href="https://customs.gov.ru/storage/opendata/7730176610-%s/'
                        'data-20260101T0000-structure-1.csv">старая</a>'
                        '<a href="https://customs.gov.ru/storage/opendata/7730176610-%s/'
                        'data-20260926T0000-structure-1.csv">новая</a>' % (набор, набор)
                        ).encode()
            if "7730176610-%s/data-20260926" % набор in url:
                check_referer.append(referer.endswith(набор))
                if набор == сломать:
                    raise OSError("сеть")
                return данные
            if "7730176610-%s/data-20260101" % набор in url:
                raise AssertionError("взята не самая свежая выгрузка")
        raise AssertionError("неожиданный url %s" % url)
    return get


check_referer = []


def case_фтс(fts):
    errors = []
    with tempfile.TemporaryDirectory() as td:
        res = fts.refresh(cache_dir=td, get=фтс_get(fts, фтс_файлы()), сегодня=СЕГОДНЯ)
        check(errors, res["статус"] == "обновлено" and {k: v["записей"] for k, v in
              res["реестры"].items()} == {"таможенные_представители": 1,
                                         "таможенные_перевозчики": 4, "свх": 1,
                                         "таможенные_склады": 1,
                                         "магазины_беспошлинной_торговли": 1},
              "refresh ФТС: %r" % res)
        check(errors, check_referer and all(check_referer), "Referer — страница набора")
        сырое = сырой_индекс(os.path.join(td, fts.ИНДЕКС))
        for лишнее in (АДРЕС, ТЕЛЕФОН, ПОЧТА, "ООО КАРГО"):
            check(errors, лишнее not in сырое, "ФТС: в индексе %r" % лишнее)
        r, _ = fts.lookup("7708503727", cache_dir=td, сегодня=СЕГОДНЯ)
        статусы = sorted((з["реестр"], з["номер"], з["статус"]) for з in r["записи"])
        check(errors, статусы == [
            ("свх", "10502/300813/10058/5", "действует"),
            ("таможенные_перевозчики", "10000/0001А", "действует"),
            ("таможенные_перевозчики", "10000/0002", "срок истёк"),
            ("таможенные_перевозчики", "10000/0003", "аннулирована"),
            ("таможенные_перевозчики", "10000/0004", "статус неизвестен (код 7)")],
            "статусы IDEL: %r" % статусы)
        check(errors, r["действует_в"] == ["свх", "таможенные_перевозчики"], "действует_в")
        r, _ = fts.lookup("7805076598", cache_dir=td, сегодня=СЕГОДНЯ)
        check(errors, r["записи"] == [{"реестр": "таможенные_склады", "статус": "действует",
                                       "номер": "10216/009/А", "включён": "2015-03-20"}],
              "таможенный склад: %r" % r["записи"])
        r, _ = fts.lookup("7705606435", cache_dir=td, сегодня=СЕГОДНЯ)
        check(errors, r["действует_в"] == ["магазины_беспошлинной_торговли"], "МБТ: %r" % r)
        check(errors, fts.lookup("7700000000", cache_dir=td, сегодня=СЕГОДНЯ)[0]["в_реестрах"]
              is False, "нет записи")
        # срок истёк после сборки индекса — «действует» пересчитывается на дату запроса
        r, _ = fts.lookup("7708503727", cache_dir=td, сегодня="2030-01-02")
        check(errors, {з["номер"]: з["статус"] for з in r["записи"]}["10000/0001А"]
              == "срок истёк" and "таможенные_перевозчики" not in r["действует_в"],
              "истёкший после сборки: %r" % r["записи"])
        check(errors, fts.lookup("7702664252", cache_dir=td, сегодня="2026-10-29")[0]
              ["предупреждения"] and not fts.lookup("7702664252", cache_dir=td,
                                                    сегодня="2026-10-28")[0]["предупреждения"],
              "свежесть 30 дней")
        путь = os.path.join(td, fts.ИНДЕКС)
        было = Path(путь).read_bytes()
        много_без_инн = csv_([["ST", "РЕГ НОМЕР", "ДАТА ВКЛЮЧЕНИЯ В РЕЕСТР", "INN"]]
                             + [["RU", "1", "2010.09.01", "7702664252"]] * 10
                             + [["RU", "1", "2010.09.01", "н/д"]])
        плохие = {
            "HTML вместо CSV": {"revlsvrhr": b"<html>captcha</html>"},
            "нет колонки": {"reestrtamperev": csv_([["ST", "NLIC", "INN"], ["RU", "1",
                                                                          "7708503727"]])},
            "строки без ИНН": {"reesrtampredst": много_без_инн},
            "плохая дата": {"reesrtampredst": csv_([
                ["ST", "РЕГ НОМЕР", "ДАТА ВКЛЮЧЕНИЯ В РЕЕСТР", "INN"],
                ["RU", "1", "01-09-2010", "7702664252"]])},
            "нет шапки склада": {"reestrvtamskl": csv_([["Реестр"], ["1", "2"]])},
            # строка с ИНН, но номер в незнакомом формате — не пропускается молча
            "склад: номер без формата": {"reestrvtamskl": xlsx_like_csv(
                ["№", "Страна", "Реквизиты документа", "Дата включения", "Наим", "Адрес",
                 "ИНН"], [["1", "РФ", "б/н", "01.01.2020", "АО", АДРЕС, "7805076598"],
                          ["2", "РФ", "10216/1/А", "01.01.2020", "АО", АДРЕС, "7805076598"]])},
        }
        for что, подмена in плохие.items():
            res = fts.refresh(cache_dir=td, get=фтс_get(fts, фтс_файлы(подмена)),
                              сегодня=СЕГОДНЯ)
            if что == "склад: номер без формата":
                с = [з for з in fts.lookup("7805076598", cache_dir=td, сегодня=СЕГОДНЯ)[0]
                     ["записи"]]
                check(errors, res["статус"] == "обновлено" and len(с) == 2,
                      "%s: строка с ИНН пропущена: %r" % (что, с))
                было = Path(путь).read_bytes()
                continue
            check(errors, res["статус"].startswith("индекс не обновлён: схема:")
                  and Path(путь).read_bytes() == было, "ФТС %s: %r" % (что, res["статус"]))
        res = fts.refresh(cache_dir=td, get=фтс_get(fts, фтс_файлы(), сломать="reestrmbt"),
                          сегодня=СЕГОДНЯ)
        check(errors, "не все" in res["статус"] and Path(путь).read_bytes() == было,
              "ФТС частичное скачивание: %r" % res["статус"])
        # падение вдвое
        данные = {р: (фтс_файлы()[о["набор"].split("-", 1)[1]], "u")
                  for р, о in fts.РЕЕСТРЫ.items()}
        try:
            fts.построить_индекс(данные, путь, {"таможенные_перевозчики": {"записей": 100}},
                                 СЕГОДНЯ)
            errors.append("ФТС: падение вдвое принято")
        except ValueError as e:
            check(errors, "было 100" in str(e) and Path(путь).read_bytes() == было,
                  "ФТС падение: %s" % e)
    return "фтс", errors


# --- РКН ----------------------------------------------------------------------

НС_ЛИЦ = "http://rsoc.ru/opendata/7705846236-LicComm"
НС_ОРИ = "http://rsoc.ru/opendata/7705846236-InformationDistributor"


def ркн_лицензии(записи=None):
    записи = записи if записи is not None else [
        ("7740000076", "Л030-1", "действующая", "2026-09-21", "2031-09-21", "Подвижная связь"),
        ("7740000076", "Л030-2", "недействующая", "2026-09-25", "2026-09-26", "Телематика"),
        ("7740000076", "Л030-3", "приостановленная", "2020-01-01", "2025-01-01", "Каналы"),
        ("500100732259", "Л030-4", "действующая", "2021-01-01", "2026-01-01", "Телематика"),
        (None, "5", "недействующая", "1991-05-15", "1994-05-15", "Старая лицензия"),
    ]
    тело = "".join(
        "<rkn:record>\n<rkn:name>%s</rkn:name><rkn:addr_legal>%s</rkn:addr_legal>%s"
        "<rkn:licence_num>%s</rkn:licence_num><rkn:lic_status_name>%s</rkn:lic_status_name>"
        "<rkn:date_start>%s</rkn:date_start><rkn:date_end>%s</rkn:date_end>"
        "<rkn:service_name>%s</rkn:service_name></rkn:record>\n"
        % (ФИО, АДРЕС, "<rkn:inn>%s</rkn:inn>" % инн if инн else "", н, с, д1, д2, у)
        for инн, н, с, д1, д2, у in записи)
    # 20 лицензий с ИНН на одну без — доля ИНН проходит порог
    тело += "".join("<rkn:record><rkn:inn>77100000%02d</rkn:inn><rkn:licence_num>X%d"
                    "</rkn:licence_num><rkn:lic_status_name>недействующая</rkn:lic_status_name>"
                    "</rkn:record>" % (i, i) for i in range(20))
    return ('<?xml version="1.0"?>\n<rkn:register xmlns:rkn="%s">\n%s</rkn:register>'
            % (НС_ЛИЦ, тело)).encode("utf-8")


def ркн_ори():
    return ('<?xml version="1.0"?><rkn:register xmlns:rkn="%s"><rkn:record>'
            "<rkn:entryNum>1-PP</rkn:entryNum><rkn:entryDate>2014-09-12</rkn:entryDate>"
            "<rkn:distributorName>ООО &quot;Яндекс&quot;</rkn:distributorName>"
            "<rkn:distributorINN>7736207543</rkn:distributorINN>"
            "<rkn:distributorEmail>%s</rkn:distributorEmail>"
            "<rkn:distributorPersons>%s</rkn:distributorPersons><rkn:services>"
            "<rkn:service><rkn:domain>mail.example</rkn:domain></rkn:service>"
            "<rkn:service><rkn:domain>disk.example</rkn:domain></rkn:service></rkn:services>"
            "</rkn:record><rkn:record><rkn:entryNum>2-PP</rkn:entryNum>"
            "<rkn:entryDate>2015-01-01</rkn:entryDate><rkn:distributorName>Foreign Ltd"
            "</rkn:distributorName></rkn:record></rkn:register>"
            % (НС_ОРИ, ПОЧТА, ФИО)).encode("utf-8")


def ркн_get(подмена=None, сломать=None):
    файлы = {"7705846236-LicComm": ркн_лицензии(), "7705846236-InformationDistributor": ркн_ори()}
    файлы.update(подмена or {})

    def get(url, referer, лимит, в_файл=None):
        for набор, данные in файлы.items():
            if url == "https://rkn.gov.ru/opendata/%s/" % набор:
                return ('<a href="https://rkn.gov.ru/opendata/%s/data-20260927T0000-structure'
                        '-1.xml"></a><a href="https://rkn.gov.ru/opendata/%s/data-20260928T0000'
                        '-structure-1.xml"></a>' % (набор, набор)).encode()
            if url.startswith("https://rkn.gov.ru/opendata/%s/data-20260928" % набор):
                if набор == сломать:
                    raise OSError("обрыв")
                Path(в_файл).write_bytes(данные)
                return None
        raise AssertionError("неожиданный url %s" % url)
    return get


def case_ркн(rkn):
    errors = []
    with tempfile.TemporaryDirectory() as td:
        res = rkn.refresh(cache_dir=td, get=ркн_get(), сегодня=СЕГОДНЯ)
        check(errors, res["статус"] == "обновлено"
              and res["реестры"]["лицензии_связи"]["записей"] == 25
              and res["реестры"]["ори"]["записей"] == 2
              and res["реестры"]["лицензии_связи"]["выгрузка"] == "2026-09-28",
              "refresh РКН: %r" % res)
        check(errors, sorted(os.listdir(td)) == [rkn.ИНДЕКС], "в кэше лишнее: %r" % os.listdir(td))
        сырое = сырой_индекс(os.path.join(td, rkn.ИНДЕКС))
        for лишнее in (ФИО, АДРЕС, ПОЧТА, "Яндекс"):
            check(errors, лишнее not in сырое, "РКН: в индексе %r" % лишнее)
        r, _ = rkn.lookup("7740000076", cache_dir=td, сегодня=СЕГОДНЯ)
        л = r["лицензии_связи"]
        check(errors, (л["всего"], л["действующих"], л["приостановленных"]) == (3, 1, 1)
              and [x["номер"] for x in л["показаны"]] == ["Л030-1", "Л030-2", "Л030-3"],
              "лицензии связи: %r" % л)
        r, _ = rkn.lookup("7736207543", cache_dir=td, сегодня=СЕГОДНЯ)
        check(errors, r["ори"] == [{"номер": "1-PP", "включён": "2014-09-12",
                                    "домены": ["disk.example", "mail.example"]}],
              "ОРИ: %r" % r["ори"])
        check(errors, rkn.lookup("7700000000", cache_dir=td, сегодня=СЕГОДНЯ)[0]["в_реестрах"]
              is False, "нет записи")
        check(errors, rkn.lookup("7740000076", cache_dir=td, сегодня="2026-10-29")[0]
              ["предупреждения"], "свежесть 30 дней")
        путь = os.path.join(td, rkn.ИНДЕКС)
        было = Path(путь).read_bytes()
        плохие = {
            "чужой статус": {"7705846236-LicComm": ркн_лицензии().replace(
                "приостановленная".encode(), "аннулирована".encode())},
            "чужой набор": {"7705846236-LicComm": ркн_лицензии().replace(
                НС_ЛИЦ.encode(), b"http://rsoc.ru/opendata/7705846236-Other")},
            "мало ИНН": {"7705846236-LicComm": ркн_лицензии(
                [(None, str(i), "недействующая", "", "", "") for i in range(30)])},
            "HTML": {"7705846236-InformationDistributor": b"<html>captcha</html>"},
            "падение объёма": {"7705846236-LicComm": ркн_лицензии([])},
            "плохая дата": {"7705846236-LicComm": ркн_лицензии(
                [("7740000076", "1", "действующая", "21.09.2026", "", "x")])},
            # поток без Content-Length оборвался на границе записи
            "обрыв без Content-Length": {"7705846236-LicComm": ркн_лицензии().replace(
                b"</rkn:register>", b"")},
        }
        for что, подмена in плохие.items():
            res = rkn.refresh(cache_dir=td, get=ркн_get(подмена), сегодня=СЕГОДНЯ)
            check(errors, res["статус"].startswith("индекс не обновлён")
                  and Path(путь).read_bytes() == было, "РКН %s: %r" % (что, res["статус"]))
        res = rkn.refresh(cache_dir=td, get=ркн_get(сломать="7705846236-LicComm"),
                          сегодня=СЕГОДНЯ)
        check(errors, "обрыв" in res["статус"] and Path(путь).read_bytes() == было
              and sorted(os.listdir(td)) == [rkn.ИНДЕКС], "РКН обрыв: %r" % res["статус"])
    # запись, разрезанная границей куска, собирается целиком
    куски = [ркн_ори().decode()[:150], ркн_ори().decode()[150:]]
    check(errors, len(list(rkn.записи_xml(iter(куски)))) == 2, "запись на границе кусков")
    return "ркн", errors


# --- РАР ----------------------------------------------------------------------

_П = "организации_сельскохозяйственного_товаропроизводителя_"


def рар_строка(инн, номер, статус="действующая", выдана="14 авг 2017", вид="Розничная продажа"):
    return ("<row><Полное_и_сокращенное_наименование_%sс_указанием_ее_ОПФ>ООО"
            "</Полное_и_сокращенное_наименование_%sс_указанием_ее_ОПФ>"
            "<ИНН_%s>%s</ИНН_%s><Адрес__место_нахождения___%s>%s</Адрес__место_нахождения___%s>"
            "<Адрес_электронной_почты_%s>%s</Адрес_электронной_почты_%s>"
            "<Вид_лицензируемой_деятельности_организации>%s"
            "</Вид_лицензируемой_деятельности_организации>"
            "<Дата_выдачи_лицензии>%s</Дата_выдачи_лицензии>"
            "<Дата_окончания_действия_лицензии>13 авг 2028</Дата_окончания_действия_лицензии>"
            "<Номер_лицензии__соответствующий_номеру_записи_в_реестре>%s"
            "</Номер_лицензии__соответствующий_номеру_записи_в_реестре>"
            "<Сведения_о_действии_лицензии>%s</Сведения_о_действии_лицензии>"
            "<Дата_изменения_сведений_о_действии_лицензии>15 июл 2024"
            "</Дата_изменения_сведений_о_действии_лицензии>"
            "<Основание_изменения_сведений_о_действии_лицензии>решение суда"
            "</Основание_изменения_сведений_о_действии_лицензии>"
            "<Координаты>Широта - 50° 33'</Координаты></row>"
            % (_П, _П, _П, инн, _П, _П, АДРЕС, _П, _П, ПОЧТА, _П, вид, выдана, номер, статус))


def рар_zip(строки=None, лишний_xml=False):
    строки = строки if строки is not None else (
        [рар_строка("7825706086", "78РПА0001"), рар_строка("7825706086", "78РПА0001"),
         рар_строка("7825706086", "78РПА0002", "аннулирована", "01 янв 2015"),
         рар_строка("500100732259", "50РПО0003", "истекла", "2016-02-03")]
        + [рар_строка("77100000%02d" % i, "N%d" % i, "прекращена") for i in range(20)])
    буфер = io.BytesIO()
    with zipfile.ZipFile(буфер, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("data.xml", '﻿<?xml version="1.0" encoding="utf-8"?><data>%s</data>'
                   % "".join(строки))
        if лишний_xml:
            z.writestr("data2.xml", "<data/>")
    return буфер.getvalue()


def обрезанный_zip():
    """ZIP целый (CRC сходится), а XML внутри оборван на границе строки."""
    буфер = io.BytesIO()
    with zipfile.ZipFile(буфер, "w") as z:
        z.writestr("data.xml", "<data>" + "".join(
            рар_строка("77100000%02d" % i, "N%d" % i) for i in range(30)))
    return буфер.getvalue()


def case_рар_дубли(rar):
    errors = []
    строки = [рар_строка("7825706086", "1", "прекращена"),
              рар_строка("7825706086", "1", "переоформлена"),
              рар_строка("7825706086", "1", "прекращена"),
              рар_строка("7825706086", "2", "действующая"),
              рар_строка("7825706086", "2", "аннулирована").replace("15 июл 2024",
                                                                     "16 июл 2024")]
    по_инн, всего, _ = rar.разобрать(rar.строки_xml(["<data>" + "".join(строки) + "</data>"]))
    л = по_инн["7825706086"]
    check(errors, л["1"][1] == "разные сведения: переоформлена; прекращена" and л["1"][6] == 3,
          "равная дата — оба статуса: %r" % л["1"])
    check(errors, л["2"][1] == "аннулирована" and л["2"][4] == "2024-07-16" and л["2"][6] == 2,
          "более позднее изменение побеждает: %r" % л["2"])
    return "рар-дубли", errors


def рар_get(данные=None, дата="20260701", сломать=False, счётчик=None):
    def get(url, referer, лимит, в_файл=None):
        if url == "https://fsrar.gov.ru/opendata/7710747640-reestr":
            return ("Гиперссылка (URL) на набор https://fsrar.gov.ru/opendata/7710747640-reestrlic"
                    "/data-%st0000-structure-20190918t0000.zip Формат" % дата).encode()
        if "/7710747640-reestrlic/data-%s" % дата in url:
            if счётчик is not None:
                счётчик.append(url)
            if сломать:
                raise OSError("обрыв")
            Path(в_файл).write_bytes(данные if данные is not None else рар_zip())
            return None
        raise AssertionError("неожиданный url %s" % url)
    return get


def case_рар(rar):
    errors = []
    with tempfile.TemporaryDirectory() as td:
        res = rar.refresh(cache_dir=td, get=рар_get(), сегодня=СЕГОДНЯ)
        check(errors, res["статус"] == "обновлено" and res["строк"] == 24
              and res["организаций"] == 22 and res["выгрузка"] == "2026-07-01",
              "refresh РАР: %r" % res)
        check(errors, sorted(os.listdir(td)) == [rar.ИНДЕКС], "в кэше лишнее: %r" % os.listdir(td))
        сырое = сырой_индекс(os.path.join(td, rar.ИНДЕКС))
        for лишнее in (АДРЕС, ПОЧТА, "Широта"):
            check(errors, лишнее not in сырое, "РАР: в индексе %r" % лишнее)
        r, _ = rar.lookup("7825706086", cache_dir=td, сегодня=СЕГОДНЯ)
        check(errors, r["лицензий"] == 2 and r["действующих"] == 1
              and r["по_статусам"] == {"действующая": 1, "аннулирована": 1}
              and r["показаны"][0] == {"номер": "78РПА0001", "вид": "Розничная продажа",
                                       "статус": "действующая", "выдана": "2017-08-14",
                                       "окончание": "2028-08-13",
                                       "статус_изменён": "2024-07-15",
                                       "основание": "решение суда", "мест": 2},
              "РАР лицензии: %r" % r)
        r, _ = rar.lookup("500100732259", cache_dir=td, сегодня=СЕГОДНЯ)
        check(errors, r["показаны"][0]["выдана"] == "2016-02-03", "ISO-дата: %r" % r)
        check(errors, rar.lookup("7700000000", cache_dir=td, сегодня=СЕГОДНЯ)[0]["в_реестре"]
              is False, "нет записи")
        check(errors, rar.lookup("7825706086", cache_dir=td, сегодня="2026-11-28")[0]
              ["предупреждения"] == [] and rar.lookup("7825706086", cache_dir=td,
                                                      сегодня="2026-11-29")[0]["предупреждения"],
              "свежесть 150 дней от выгрузки")
        # та же выгрузка — не качается заново
        счёт = []
        res = rar.refresh(cache_dir=td, get=рар_get(счётчик=счёт), сегодня="2026-09-30")
        check(errors, "уже в индексе" in res["статус"] and not счёт, "повторная: %r" % res)
        путь = os.path.join(td, rar.ИНДЕКС)
        было = Path(путь).read_bytes()
        плохие = {
            "чужой статус": рар_zip([рар_строка("7825706086", "1", "отозвана")]
                                    + [рар_строка("77100000%02d" % i, "N%d" % i) for i in range(30)]),
            "два XML": рар_zip(лишний_xml=True),
            "не ZIP": b"<html>captcha</html>",
            "падение": рар_zip([рар_строка("7825706086", "1")]),
            "плохая дата": рар_zip([рар_строка("7825706086", "1", выдана="14/08/2017")]),
            "без ИНН": рар_zip([рар_строка("н/д", str(i)) for i in range(30)]),
            "обрыв XML": обрезанный_zip(),
        }
        for что, данные in плохие.items():
            res = rar.refresh(cache_dir=td, get=рар_get(данные, дата="20261001"),
                              сегодня=СЕГОДНЯ)
            check(errors, res["статус"].startswith("индекс не обновлён")
                  and Path(путь).read_bytes() == было and sorted(os.listdir(td)) == [rar.ИНДЕКС],
                  "РАР %s: %r" % (что, res["статус"]))
        res = rar.refresh(cache_dir=td, get=рар_get(дата="20261001", сломать=True),
                          сегодня=СЕГОДНЯ)
        check(errors, "обрыв" in res["статус"] and Path(путь).read_bytes() == было,
              "РАР обрыв: %r" % res["статус"])
    return "рар", errors


# --- сеть и движок --------------------------------------------------------------

class _Ответ(io.BytesIO):
    def __init__(self, данные, длина):
        super().__init__(данные)
        self.headers = {"Content-Length": str(длина)}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Опенер:
    def __init__(self, данные, длина):
        self.данные, self.длина, self.запрос = данные, длина, None

    def open(self, req, timeout=None):
        self.запрос = req
        return _Ответ(self.данные, self.длина)


def case_сеть():
    errors = []
    dt_ = load("dump_tools")
    оп = _Опенер(b"12345", 5)
    check(errors, dt_.get("https://x.example/a", "https://x.example/", 100, op=оп) == b"12345",
          "get: целый ответ")
    check(errors, оп.запрос.get_header("Referer") == "https://x.example/"
          and "inn-check-ru" in оп.запрос.get_header("User-agent"), "get: Referer и UA")
    for что, оп, лимит in (("обрыв", _Опенер(b"123", 5), 100),
                           ("лимит", _Опенер(b"12345", 5), 3)):
        try:
            dt_.get("https://x.example/a", "r", лимит, op=оп)
            errors.append("get: %s не пойман" % что)
        except ValueError:
            pass
    with tempfile.TemporaryDirectory() as td:
        f = os.path.join(td, "x")
        dt_.get("https://x.example/a", "r", 100, в_файл=f, op=_Опенер(b"abc", 3))
        check(errors, Path(f).read_bytes() == b"abc", "get в файл")
    прокси = load("proxy")
    for url in ("http://логин:секрет@", "socks5://логин:секрет@", "http://логин:секрет@h:99999"):
        конф = прокси.настройка(url, env={})
        check(errors, конф["ошибка"] and "секрет" not in конф["ошибка"]
              and "секрет" not in (конф["маска"] or ""), "пароль в ошибке: %r" % конф)
    # NO_PROXY не пускает мимо http-прокси; ftp/file через прокси — отказ
    import urllib.request
    os.environ["NO_PROXY"] = "*"
    try:
        оп = urllib.request.build_opener(*прокси.обработчики("http://rf.example:3128", None))
        req = urllib.request.Request("http://customs.gov.ru/x")
        ph = next(h for h in оп.handlers if isinstance(h, urllib.request.ProxyHandler))
        ph.proxy_open(req, "http://логин:секрет@rf.example:3128", "http")
        check(errors, req.host == "rf.example:3128"
              and req.get_header("Proxy-authorization"), "NO_PROXY обошёл прокси: %r" % req.host)
        for url in ("ftp://example.invalid/x", "file:///etc/hosts"):
            try:
                оп.open(url, timeout=2)
                errors.append("%s через прокси ушёл напрямую" % url)
            except Exception as e:
                check(errors, "только http" in str(e), "%s: не отказ, а %r" % (url, e))
    finally:
        os.environ.pop("NO_PROXY", None)
    s5 = load("socks5")
    оп = s5.opener("socks5://логин:секрет@127.0.0.1:1")
    for url in ("ftp://example.invalid/x", "file:///etc/hosts"):
        try:
            оп.open(url, timeout=2)
            errors.append("%s через socks5 ушёл напрямую" % url)
        except Exception as e:
            check(errors, "только https" in str(e), "socks5 %s: %r" % (url, e))
    старое = {k: os.environ.pop(k, None) for k in ("INN_CHECK_PROXY", "HTTPS_PROXY", "https_proxy")}
    try:
        os.environ["INN_CHECK_PROXY"] = "socks5://логин:секрет@rf.example:1080"
        оп = dt_.opener()
        check(errors, any(type(h).__name__ == "SocksHTTPSHandler" for h in оп.handlers),
              "opener без socks5: %r" % оп.handlers)
        os.environ["INN_CHECK_PROXY"] = "socks4://rf.example:1080"
        try:
            dt_.opener()
            errors.append("негодный прокси: пошли напрямую")
        except ValueError as e:
            check(errors, "прокси" in str(e), "текст отказа: %s" % e)
    finally:
        os.environ.pop("INN_CHECK_PROXY", None)
        os.environ.update({k: v for k, v in старое.items() if v is not None})
    return "сеть", errors


def case_движок():
    errors = []
    дом = os.environ.get("HOME")
    with tempfile.TemporaryDirectory() as td:
        os.environ["HOME"] = td
        try:
            fc = load("fetch_counterparty")
            for блок, fetch in (("реестры_фтс", fc.fetch_fts_registries),
                                ("реестры_ркн", fc.fetch_rkn_registries),
                                ("лицензии_рар", fc.fetch_rar_licenses)):
                данные, av = fetch(None, "7700000000")
                check(errors, данные is None and av["причина"].startswith("кэш:")
                      and "INN_CHECK_PROXY" in av["причина"], "%s без индекса: %r" % (блок, av))
                check(errors, блок in fc.FETCHERS and fc.SOURCES[блок]["deal_killer"] is False
                      and fc.SOURCES[блок]["пусто_с_оговоркой"], "%s: регистрация" % блок)
            fts, rkn, rar = load("fts_registries"), load("rkn_registries"), load("rar_licenses")
            fts.refresh(cache_dir=fts.CACHE_DIR, get=фтс_get(fts, фтс_файлы()))
            rkn.refresh(cache_dir=rkn.CACHE_DIR, get=ркн_get())
            rar.refresh(cache_dir=rar.CACHE_DIR, get=рар_get(дата=fts_дата()))
            for блок, fetch, инн in (("реестры_фтс", fc.fetch_fts_registries, "7702664252"),
                                     ("реестры_ркн", fc.fetch_rkn_registries, "7740000076"),
                                     ("лицензии_рар", fc.fetch_rar_licenses, "7825706086")):
                данные, av = fetch(None, инн)
                check(errors, av["состояние"] == "ok" and данные, "%s ok: %r" % (блок, av))
                пусто, av = fetch(None, "7700000000")
                check(errors, пусто is None and av["состояние"] == "пусто"
                      and fc.SOURCES[блок]["пусто_с_оговоркой"] in av["причина"],
                      "%s пусто: %r" % (блок, av))
                snap = load("snapshot")
                а = {"инн": инн, блок: данные, "_доступность": {блок: av}}
                б = json.loads(json.dumps(а, ensure_ascii=False))
                б[блок]["скачан"] = "2026-10-05"
                б[блок]["выгрузка"] = "2026-10-05"
                б[блок]["предупреждения"] = ["x"]
                check(errors, snap.хеш_остального(а) == snap.хеш_остального(б),
                      "%s: даты выгрузки в хеше снимка" % блок)
            # устаревшая выгрузка — не проверено в обе стороны
            путь = os.path.join(rkn.CACHE_DIR, rkn.ИНДЕКС)
            и = rkn.dump_tools.прочитать_индекс(путь)
            for р in и["реестры"].values():
                р["скачан"] = "2026-01-01"
            rkn.dump_tools.записать_индекс(путь, и)
            for инн in ("7740000076", "7700000000"):
                _, av = fc.fetch_rkn_registries(None, инн)
                check(errors, av["состояние"] == "не проверено" and "старше" in av["причина"],
                      "устаревший РКН %s: %r" % (инн, av))
        finally:
            if дом is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = дом
    return "блок-движка", errors


def fts_дата():
    """Выгрузка РАР «сегодняшняя» для блока движка: иначе lookup с настоящей
    датой счёл бы июльскую выгрузку устаревшей к декабрю и тест стал бы зависеть
    от календаря."""
    import datetime as dt
    return dt.datetime.now(dt.timezone.utc).astimezone().strftime("%Y%m%d")


def main():
    failed = 0
    кейсы = (lambda: case_фтс(load("fts_registries")), lambda: case_ркн(load("rkn_registries")),
             lambda: case_рар(load("rar_licenses")),
             lambda: case_рар_дубли(load("rar_licenses")), case_сеть, case_движок)
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
