#!/usr/bin/env python3
"""
run_erknm_eval.py — офлайн-eval ЕРКНМ (erknm.py): разбор помесячной выгрузки
proverki.gov.ru (синтетический XML в ZIP по образцу живой выгрузки 28.09.2026),
выбор месяцев, проверка паспорта, refresh с поддельной сетью (устоявшийся месяц
не качается второй раз, сбой не портит индекс), индекс частями, lookup, блок
«еркнм» движка и снимок. Наименований, инспекторов и текстов предостережений в
индексе нет. PASS/FAIL, stdlib, CI.
"""

import datetime as dt
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
ТЕКСТ = "ТЕКСТ ПРЕДОСТЕРЕЖЕНИЯ УБРАН ИЗ ФИКСТУРЫ"


def load(имя):
    sys.path.insert(0, str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(имя, ROOT / "scripts" / ("%s.py" % имя))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check(errors, cond, msg):
    if not cond:
        errors.append(msg)


def мероприятие(erpid, инн, класс="КНМ", статус="Завершено", вид="Выездная проверка",
                тип="Внеплановое КНМ", начало="2026-09-04", тип_лица="ЮЛ", ещё_инн=None):
    субъекты = "".join(
        '<SUBJECT INN="%s" OGRN="1" NAME="%s" MSP_CODE="Микропредприятие" TYPE="%s" GUID="g"/>'
        % (и, ФИО, тип_лица) for и in [инн] + (ещё_инн or []))
    тип_attr = ' TYPE_NAME="%s"' % тип if тип else ""
    return """
    <INSPECTION CLASSIFICATION="%s" CREATION_SOURCE="x" STATUS="%s" STATUS_KEY="K" ERPID="%s" KO_LEVEL="REGIONAL" SUPERVISION_ID="x"%s START_DATE="%s" STOP_DATE="2026-09-16">
        <PROSECUTOR_OFFICE NAME="Прокуратура"/>
        <KIND_CONTROL DICT_ID="a" DICT_VERSION_ID="b" VALUE="Региональный   жилищный &quot;контроль&quot;"/>
        <KIND_KNM DICT_ID="a" DICT_VERSION_ID="b" VALUE="%s"/>
        %s
        <OBJECT ADDRESS="г. Условный" GUID="o"><RISK_CATEGORY DICT_ID="a" DICT_VERSION_ID="b" VALUE="Высокий риск"/></OBJECT>
        <INSPECTOR INSPECTOR_FULL_NAME="%s" GUID="i"/>
        <KNO_ORGANIZATION DICT_ID="a" DICT_VERSION_ID="b" VALUE="УПРАВЛЕНИЕ  НАДЗОРА"/>
        <DECISION FIO_SIGNER="%s"/>
        <WARNING_INFO><CAPTION>%s</CAPTION></WARNING_INFO>
    </INSPECTION>""" % (класс, статус, erpid, тип_attr, начало, вид, субъекты, ФИО, ФИО, ТЕКСТ)


def выгрузка_xml(год, месяц, тела, конец=True):
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><ns2:INSPECTIONS '
            'YEAR="%d" MONTH="%d" xmlns="u" xmlns:ns2="u2">' % (год, месяц)
            + "".join(тела) + ("\n</ns2:INSPECTIONS>" if конец else ""))


def в_zip(xml, имя="data.xml", ещё=None):
    буф = io.BytesIO()
    with zipfile.ZipFile(буф, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(имя, xml)
        if ещё:
            z.writestr(ещё, "x")
    return буф.getvalue()


def тела_по_умолчанию():
    return [мероприятие("10260000000000000001", "7702664252"),
            мероприятие("10260000000000000002", "7702664252", "ПМ", "Предостережение объявлено",
                        "Объявление предостережения", None),
            мероприятие("10260000000000000003", "504110181262", "ПМ", "Завершено",
                        "Профилактический визит", None),
            мероприятие("10260000000000000004", "500000000000", тип_лица="ФЛ"),
            мероприятие("10260000000000000005", "7708503727", ещё_инн=["7702664252"])]


class Сеть:
    """Поддельная сеть refresh: список месяцев, паспорта, ZIP."""

    def __init__(self, месяцы, zip_по_месяцу, дата="20260928"):
        self.месяцы, self.zip, self.дата, self.запросы = месяцы, zip_по_месяцу, дата, []

    def url_zip(self, г, м):
        return ("https://proverki.gov.ru//blob/erknm-opendata/%d/%d/data-%s-structure-"
                "20220125.zip" % (г, м, self.дата))

    def __call__(self, url, referer, лимит, в_файл=None):
        self.запросы.append((url, referer))
        if url.endswith("/list?isFederalLaw248=true"):
            годы = {}
            for г, м in self.месяцы:
                годы.setdefault(str(г), []).append(м)
            годы.setdefault("2027", []).append(2)                # будущий план
            return json.dumps({"years": [2026], "months": годы}).encode()
        for г, м in self.месяцы:
            if url.endswith("/opendata/%d/%d?isFederalLaw248=true" % (г, м)):
                имя = self.url_zip(г, м).rsplit("/", 1)[1]
                return json.dumps({"dataZipUrl": self.url_zip(г, м), "dataZipName": имя}).encode()
            if url == self.url_zip(г, м):
                with open(в_файл, "wb") as fh:
                    fh.write(self.zip[(г, м)])
                return None
        raise AssertionError("неожиданный URL %s" % url)


def case_разбор(er):
    errors = []
    xml = выгрузка_xml(2026, 9, тела_по_умолчанию())
    куски = [xml[i:i + 37] for i in range(0, len(xml), 37)]
    записи, всего, битых, без = er.разобрать(er.мероприятия_xml(iter(куски), 2026, 9))
    check(errors, (всего, битых, без) == (5, 0, 1) and len(записи) == 4, (всего, битых, без))
    check(errors, записи[0] == ["10260000000000000001", "КНМ", "Выездная проверка",
                                "Внеплановое КНМ", "завершено", "2026-09-04", "2026-09-16",
                                "УПРАВЛЕНИЕ НАДЗОРА", 'Региональный жилищный "контроль"',
                                "высокий риск", "regional", ["7702664252"]], записи[0])
    check(errors, записи[-1][-1] == ["7708503727", "7702664252"], записи[-1])
    # «>» в значении атрибута законен: ИНН после такого имени не теряется, а
    # гражданин с TYPE после имени не попадает в индекс
    хитрые = [
        мероприятие("10260000000000000011", "7702664252").replace(
            'OGRN="1" NAME="%s"' % ФИО, 'NAME="А &gt; Б > В" OGRN="1"').replace(
            '<SUBJECT INN="7702664252" NAME="А &gt; Б > В" OGRN="1"',
            '<SUBJECT NAME="А &gt; Б > В" OGRN="1" INN="7702664252"'),
        ('<INSPECTION CLASSIFICATION="КНМ" STATUS="Завершено" ERPID="10260000000000000012" '
         'SUPERVISION_ID="a > b" START_DATE="2026-09-01"><SUBJECT INN="500000000001" '
         'NAME="x > y" TYPE="ФЛ"/><KIND_KNM VALUE="Выездная проверка"/>')]
    xml_х = выгрузка_xml(2026, 9, хитрые).replace(
        хитрые[1], хитрые[1] + "</INSPECTION>")
    з_х, всего_х, битых_х, без_х = er.разобрать(er.мероприятия_xml(iter([xml_х]), 2026, 9))
    check(errors, (всего_х, битых_х, без_х) == (2, 0, 1) and з_х[0][-1] == ["7702664252"]
          and з_х[0][0] == "10260000000000000011", (з_х, всего_х, битых_х, без_х))
    сырое = json.dumps(записи, ensure_ascii=False)
    check(errors, ФИО not in сырое and ТЕКСТ not in сырое and "Условный" not in сырое,
          "в индекс попали имя, адрес или текст")
    check(errors, "500000000000" not in сырое, "гражданин (ФЛ) попал в индекс")
    for метка, xml_, ждём in (
            ("обрыв", выгрузка_xml(2026, 9, тела_по_умолчанию(), конец=False), "обрыв"),
            ("чужой месяц", выгрузка_xml(2026, 8, тела_по_умолчанию()), "корень"),
            ("пусто", выгрузка_xml(2026, 9, []), "нет мероприятий"),
            ("без номера", выгрузка_xml(2026, 9, [мероприятие("", "7702664252")] * 3
                                        + тела_по_умолчанию()), "без номера"),
            ("чужие статусы", выгрузка_xml(2026, 9, [мероприятие(
                "1026000000000000009%d" % i, "7702664252", статус="Новый статус")
                for i in range(5)]), "статусов"),
            ("не тот корень", "<?xml?><ROWS>" + "x" * (2 << 20), "корня")):
        try:
            er.разобрать(er.мероприятия_xml(iter([xml_]), 2026, 9))
            errors.append("%s: нет отказа" % метка)
        except ValueError as e:
            check(errors, ждём in str(e), "%s: %s" % (метка, e))
    with tempfile.TemporaryDirectory() as td:
        путь = os.path.join(td, "a.zip")
        with open(путь, "wb") as fh:
            fh.write(в_zip(выгрузка_xml(2026, 9, тела_по_умолчанию()), ещё="лишний.txt"))
        try:
            er.разобрать_zip(путь, 2026, 9)
            errors.append("два файла в архиве: нет отказа")
        except ValueError as e:
            check(errors, "файлов" in str(e), str(e))
    return "разбор", errors


def case_месяцы(er):
    errors = []
    список = {"months": {"2025": [11, 12], "2026": [1, 2, 3, 9, 10], "2027": [2]}}
    check(errors, er.месяцы_к_скачиванию(список, СЕГОДНЯ, 3) == [(2026, 9), (2026, 3), (2026, 2)],
          er.месяцы_к_скачиванию(список, СЕГОДНЯ, 3))
    check(errors, er.пропуски([(2025, 11), (2026, 2), (2025, 12)]) == ["2026-01"],
          er.пропуски([(2025, 11), (2026, 2), (2025, 12)]))
    for плохой in ({}, {"months": {"2026": [13]}}, {"months": {"x": [1]}}):
        try:
            er.месяцы_к_скачиванию(плохой, СЕГОДНЯ, 3)
            errors.append("список %r: нет отказа" % плохой)
        except ValueError:
            pass
    хороший = {"dataZipName": "data-20260928-structure-20220125.zip",
               "dataZipUrl": "https://proverki.gov.ru//blob/erknm-opendata/2026/9/"
                             "data-20260928-structure-20220125.zip"}
    check(errors, er.выгрузка_месяца(хороший, 2026, 9) == (хороший["dataZipUrl"], "2026-09-28"),
          "паспорт")
    for метка, паспорт in (
            ("чужой хост", dict(хороший, dataZipUrl=хороший["dataZipUrl"].replace(
                "proverki.gov.ru", "proverki.gov.ru.example.com"))),
            ("чужой месяц", dict(хороший, dataZipUrl=хороший["dataZipUrl"].replace("/9/", "/8/"))),
            ("имя не то", dict(хороший, dataZipName="data.zip")),
            ("имя и URL расходятся", dict(хороший, dataZipName="data-20260927-structure-"
                                                               "20220125.zip"))):
        try:
            er.выгрузка_месяца(паспорт, 2026, 9)
            errors.append("%s: нет отказа" % метка)
        except ValueError:
            pass
    check(errors, er.осел(2026, 7, "2026-09-14", СЕГОДНЯ)
          and not er.осел(2026, 7, "2026-09-13", СЕГОДНЯ), "граница устоявшегося месяца: 45 дней")
    check(errors, er.осел(2026, 7, "2026-09-14", "2027-03-13")
          and not er.осел(2026, 7, "2026-09-14", "2027-03-14"), "перепроверка раз в 180 дней")
    return "месяцы-паспорт", errors


def case_refresh(er):
    errors = []
    with tempfile.TemporaryDirectory() as td:
        сентябрь = в_zip(выгрузка_xml(2026, 9, тела_по_умолчанию()))
        июль = в_zip(выгрузка_xml(2026, 7, [мероприятие(
            "10260000000000000001", "7702664252", начало="2026-07-30"),
            мероприятие("10260000000000000007", "7702664252", "ПМ", "Предостережение объявлено",
                        "Объявление предостережения", None, начало="2026-07-01")]))
        сеть = Сеть([(2026, 9), (2026, 7)], {(2026, 9): сентябрь, (2026, 7): июль})
        рез = er.refresh(cache_dir=td, get=сеть, сегодня=СЕГОДНЯ, месяцев=12)
        check(errors, рез["статус"] == "обновлено" and рез["скачано_выгрузок"] == 2
              and рез["охват"]["пропуски"] == ["2026-08"]
              and рез["охват"]["с"] == "2026-07-01" and рез["охват"]["по"] == "2026-09-30", рез)
        check(errors, all(r in (er.СТРАНИЦА, er.СТРАНИЦА + "/check/2026/9?isFederalLaw248=true",
                                er.СТРАНИЦА + "/check/2026/7?isFederalLaw248=true")
                          for _, r in сеть.запросы), "Referer не страницы")
        папки = [и for и in os.listdir(td) if и.startswith("индекс-")]
        check(errors, len(папки) == 1 and len(os.listdir(os.path.join(td, папки[0])))
              == er.ЧАСТЕЙ + 1, папки)
        res, _ = er.lookup("7702664252", cache_dir=td, сегодня=СЕГОДНЯ)
        check(errors, res["в_реестре"] and res["мероприятий"] == 4 and res["проверок"] == 2
              and res["внеплановых_проверок"] == 2 and res["предостережений"] == 2, res)
        # один ERPID в двух месяцах — берётся из свежего
        check(errors, [м["номер"] for м in res["показаны"]].count("10260000000000000001") == 1
              and res["показаны"][0]["начало"] == "2026-09-04", res["показаны"])
        check(errors, res["показаны"][0]["вид_контроля"] == 'Региональный жилищный "контроль"'
              and res["по_видам"]["Объявление предостережения"] == 2, res["показаны"][0])
        check(errors, ФИО not in json.dumps(res, ensure_ascii=False), "имя в ответе")
        res, _ = er.lookup("504110181262", cache_dir=td, сегодня=СЕГОДНЯ)
        check(errors, res["мероприятий"] == 1 and res["показаны"][0]["тип"] is None, res)
        res, _ = er.lookup("7700000000", cache_dir=td, сегодня=СЕГОДНЯ)
        check(errors, res["в_реестре"] is False and res["мероприятий"] == 0
              and any("2026-08" in п for п in res["предупреждения"]), res)
        res, _ = er.lookup("7702664252", cache_dir=td, сегодня=СЕГОДНЯ)
        check(errors, res["предупреждения"] == [], "дыра в месяцах мешает положительному ответу")
        res, _ = er.lookup("500000000000", cache_dir=td, сегодня=СЕГОДНЯ)
        check(errors, res["в_реестре"] is False, "гражданин в индексе")
        res, _ = er.lookup("7702664252", cache_dir=td, сегодня="2026-11-08")
        check(errors, res["предупреждения"] and "--refresh" in res["предупреждения"][0],
              res["предупреждения"])
        # тот же день: паспорт сентября спрошен, ZIP тот же — не качается
        сеть1 = Сеть([(2026, 9), (2026, 7)], {(2026, 9): сентябрь, (2026, 7): июль})
        рез = er.refresh(cache_dir=td, get=сеть1, сегодня=СЕГОДНЯ, месяцев=12)
        check(errors, рез["скачано_выгрузок"] == 0
              and any("/opendata/2026/9?" in u for u, _ in сеть1.запросы), сеть1.запросы)
        # июль выгружен 28.09 — через 59 дней после конца месяца, устоялся: ни
        # паспорта, ни ZIP; сентябрь (выгрузка 28.09) ещё дописывается — заново
        сеть2 = Сеть([(2026, 9), (2026, 7)], {(2026, 9): сентябрь, (2026, 7): июль}, "20261117")
        рез = er.refresh(cache_dir=td, get=сеть2, сегодня="2026-11-17", месяцев=12)
        скачаны = [u for u, _ in сеть2.запросы if u.endswith(".zip")]
        check(errors, рез["скачано_выгрузок"] == 1 and len(скачаны) == 1 and "/2026/9/" in скачаны[0]
              and not any("/2026/7" in u for u, _ in сеть2.запросы), сеть2.запросы)
        # сентябрь теперь от 17.11 — 48 дней после конца месяца: устоялся
        сеть3 = Сеть([(2026, 9), (2026, 7)], {(2026, 9): сентябрь, (2026, 7): июль}, "20261118")
        рез = er.refresh(cache_dir=td, get=сеть3, сегодня="2026-11-18", месяцев=12)
        check(errors, рез["скачано_выгрузок"] == 0 and len(сеть3.запросы) == 1, сеть3.запросы)
        # сбой на битом ZIP: индекс прежний, временных файлов нет
        до = dump(er, td)
        битый = Сеть([(2026, 10)], {(2026, 10): в_zip(выгрузка_xml(2026, 10, тела_по_умолчанию(),
                                                                    конец=False))}, "20261119")
        рез = er.refresh(cache_dir=td, get=битый, сегодня="2026-11-19", месяцев=12)
        check(errors, рез["статус"].startswith("индекс не обновлён") and "обрыв" in рез["статус"]
              and dump(er, td) == до, рез)
        check(errors, not [и for и in os.listdir(td) if и.startswith(".выгрузка-")],
              "временный ZIP остался")
        # месяц выпал из окна — удалён из кэша
        рез = er.refresh(cache_dir=td, get=Сеть([(2026, 9)], {(2026, 9): сентябрь}, "20261120"),
                         сегодня="2026-11-20", месяцев=1)
        поколения = [и for и in os.listdir(td) if и.startswith("индекс-")]
        check(errors, sorted(os.listdir(os.path.join(td, er.МЕСЯЦЫ_ПАПКА))) == ["2026-09.json.gz"]
              and len(поколения) == 2 and dump(er, td)["папка"] in поколения, (рез, поколения))
        # читатель со старым манифестом: предыдущее поколение ещё на диске
        прежнее = next(п for п in поколения if п != dump(er, td)["папка"])
        check(errors, os.path.exists(os.path.join(td, прежнее, er.часть("7702664252") + ".json.gz")),
              "предыдущее поколение удалено")
        # блокировка: второй refresh не идёт
        open(os.path.join(td, ".refresh.lock"), "w").close()
        рез = er.refresh(cache_dir=td, get=сеть, сегодня=СЕГОДНЯ)
        check(errors, "уже идёт" in рез["статус"], рез)
    with tempfile.TemporaryDirectory() as td:
        res, note = er.lookup("7702664252", cache_dir=td)
        check(errors, res is None and "--refresh" in note, note)
    return "refresh-lookup", errors


def dump(er, td):
    return er.dump_tools.прочитать_индекс(os.path.join(td, er.МАНИФЕСТ))


def case_движок():
    errors = []
    дом = os.environ.get("HOME")
    with tempfile.TemporaryDirectory() as td:
        os.environ["HOME"] = td
        try:
            fc = load("fetch_counterparty")
            блок = "еркнм"
            данные, av = fc.FETCHERS[блок](None, "7702664252")
            check(errors, данные is None and av["причина"].startswith("кэш:")
                  and "erknm.py --refresh" in av["причина"], "без индекса: %r" % av)
            er = load("erknm")
            сегодня = dt.datetime.now(dt.timezone.utc).astimezone().date()
            г, м = сегодня.year, сегодня.month
            сеть = Сеть([(г, м)], {(г, м): в_zip(выгрузка_xml(г, м, тела_по_умолчанию()))},
                        сегодня.strftime("%Y%m%d"))
            рез = er.refresh(cache_dir=er.CACHE_DIR, get=сеть, сегодня=сегодня.isoformat())
            check(errors, рез["статус"] == "обновлено", рез)
            данные, av = fc.FETCHERS[блок](None, "7702664252")
            check(errors, av["состояние"] == "ok" and данные["в_реестре"], av)
            пусто, av = fc.FETCHERS[блок](None, "7700000000")
            check(errors, пусто is None and av["состояние"] == "пусто"
                  and fc.SOURCES[блок]["пусто_с_оговоркой"] in av["причина"], av)
            snap = load("snapshot")
            а = {"инн": "7702664252", блок: данные, "_доступность": {блок: av}}
            б = json.loads(json.dumps(а, ensure_ascii=False))
            б[блок]["скачан"], б[блок]["охват"], б[блок]["предупреждения"] = "2026-10-05", {}, ["x"]
            check(errors, snap.хеш_остального(а) == snap.хеш_остального(б),
                  "даты выгрузки в хеше снимка")
        finally:
            if дом is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = дом
    return "блок-движка", errors


def main():
    er = load("erknm")
    failed = 0
    cases = [case_разбор(er), case_месяцы(er), case_refresh(er), case_движок()]
    for имя, errors in cases:
        if errors:
            failed += 1
            print("FAIL %s" % имя)
            for e in errors:
                print("  - %s" % e)
        else:
            print("PASS %s" % имя)
    if failed:
        print("FAIL: %d/%d кейсов упало" % (failed, len(cases)))
        return 1
    print("PASS: все %d кейсов зелёные" % len(cases))
    return 0


if __name__ == "__main__":
    sys.exit(main())
