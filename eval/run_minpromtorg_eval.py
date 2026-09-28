#!/usr/bin/env python3
"""
run_minpromtorg_eval.py — офлайн-eval реестров Минпромторга (minpromtorg.py):
паспорта наборов, 404 у перечисленной версии, разбор трёх CSV (синтетика по
образцу живых выгрузок 28.09.2026), смена схемы и обвал числа строк — индекс
цел, lookup, блок «минпромторг» движка и снимок. Наименований, адресов и ссылок
на PDF (в имени файла — наименование) в индексе нет. PASS/FAIL, stdlib, CI.
"""

import csv
import datetime as dt
import importlib.util
import io
import json
import os
import sys
import tempfile
import urllib.error
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


def в_csv(строки):
    буф = io.StringIO()
    csv.writer(буф, lineterminator="\r\n").writerows(строки)
    return буф.getvalue().encode("utf-8")


ШАПКА_ЛС = ["Полное наименование лицензиата", "Краткое наименование лицензиата", "ОГРН", "ИНН",
            "Место нахождения юридического лица", "Дата внесения сведений в реестр",
            "Регистрационный номер лицензии", "Регистрационный номер лицензии (до 01.03.2022)",
            "Статус лицензии", "Адрес места осуществления выполняемых работ",
            "Дата начала действия", "Дата решения", "Номер решения",
            "Наименования выполняемых работ", "Комментарий к выполняемым работам", "Проверки"]


def лс(инн, номер, статус="Действующая", работы="Производство &quot;газов&quot;   медицинских"):
    return [ФИО, ФИО, "1", инн, АДРЕС, "20-04-2017", номер, "1-ЛС-П", статус, АДРЕС,
            "06-02-2013", "31-02-2013", "156", работы, "", ""]


def наборы(лекарства=None, заключения=None, производители=None):
    return {
        "лекарства": в_csv([ШАПКА_ЛС] + (лекарства if лекарства is not None else [
            лс("0721009345", "Л012-1"),
            лс("0721009345", "Л012-2", "Приостановлена до 18-12-2026", ";"),
            лс("504110181262", "Л012-3")])),
        "производители": в_csv([["Nameoforg", "INN", "OGRN", "Address"]] + (
            производители if производители is not None else
            [[ФИО, "0721009345", "1", "-"], [ФИО, "7702664252", "1", "-"]])),
        "заключения": в_csv([["Dateofcon", "Numberofcon", "Expirationdate", "Document",
                              "Nameoforg", "INN", "OGRN"]] + (
            заключения if заключения is not None else [
                ["2016-04-04", "12906/07", "2019-04-04", "https://gisp.gov.ru/x/%s.pdf" % ФИО,
                 ФИО, "0721009345", "1"],
                # старая строка того же заключения идёт раньше новой
                ["2023-02-15", "14609/19", "2026-02-14", "https://gisp.gov.ru/y.pdf", ФИО,
                 "0721009345", "1"],
                ["2024-02-15", "14609/19", "2027-02-14", "https://gisp.gov.ru/y.pdf", ФИО,
                 "0721009345", "1"],
                ["2022-01-10", "", "2025-01-09", "-", ФИО, "0721009345", "1"],
                ["2023-06-30", "", "2026-06-27", "-", ФИО, "0721009345", "1"],
                ["2023-06-30", "", "2026-06-27", "-", ФИО, "0721009345", "1"],
                ["2024-06-30", "", "2027-06-27", "-", ФИО, "7702664252", "1"]])),
    }


def паспорт(набор, даты):
    return "".join('<a href="/opendata/%s/data-%s-structure-20170720.csv">v</a>' % (набор, д)
                   for д in даты).encode()


class Сеть:
    def __init__(self, мп, данные, даты=("20260928", "20260925"), нет=()):
        self.мп, self.данные, self.даты, self.нет, self.запросы = мп, данные, даты, нет, []

    def __call__(self, url, referer, лимит, в_файл=None):
        self.запросы.append((url, referer))
        for имя, д in self.мп.НАБОРЫ.items():
            код = д["набор"]
            if url == self.мп.страница(имя):
                return паспорт(код, self.даты)
            if url.startswith("%s/opendata/%s/data-" % (self.мп.САЙТ, код)):
                дата = url.split("/data-")[1][:8]
                if дата in self.нет:
                    raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b""))
                return self.данные[имя]
        raise AssertionError("неожиданный URL %s" % url)


def case_паспорт(мп):
    errors = []
    html = паспорт("1000000011-ReestrOrgs", ["20260925", "20260928", "20260924"]).decode()
    в = мп.версии(html + html, "производители")
    check(errors, [д for д, _ in в] == ["2026-09-28", "2026-09-25", "2026-09-24"]
          and в[0][1] == "https://minpromtorg.gov.ru/opendata/1000000011-ReestrOrgs/"
                         "data-20260928-structure-20170720.csv", в)
    for плохой in ("<html></html>", паспорт("1000000010-resolutions", ["20260928"]).decode()):
        try:
            мп.версии(плохой, "производители")
            errors.append("паспорт без ссылок набора: нет отказа")
        except ValueError:
            pass
    # одна дата, две структуры: обе в списке версий
    две = ('<a href="/opendata/1000000011-ReestrOrgs/data-20260928-structure-20210405.csv">'
           '<a href="/opendata/1000000011-ReestrOrgs/data-20260928-structure-20170720.csv">')
    check(errors, [u.rsplit("-", 1)[1] for _, u in мп.версии(две, "производители")]
          == ["20210405.csv", "20170720.csv"],
          мп.версии(две, "производители"))
    сеть = Сеть(мп, наборы(), нет=("20260928",))
    дата, _, пропущены = мп.скачать("лекарства", сеть)
    check(errors, дата == "2026-09-25" and пропущены == ["2026-09-28"], (дата, пропущены))
    check(errors, сеть.запросы[0][1] == мп.ОТКРЫТЫЕ_ДАННЫЕ
          and сеть.запросы[1][1] == мп.страница("лекарства"), сеть.запросы)
    try:
        мп.скачать("лекарства", Сеть(мп, наборы(), нет=("20260928", "20260925")))
        errors.append("все версии 404: нет отказа")
    except ValueError as e:
        check(errors, "404" in str(e), str(e))

    class Отказ(Сеть):
        def __call__(self, url, referer, лимит, в_файл=None):
            if "/data-" in url:
                self.запросы.append((url, referer))
                raise urllib.error.HTTPError(url, 403, "Forbidden", {}, io.BytesIO(b""))
            return super().__call__(url, referer, лимит)
    отказ = Отказ(мп, наборы())
    try:
        мп.скачать("лекарства", отказ)
        errors.append("403: нет исключения")
    except urllib.error.HTTPError:
        check(errors, len([u for u, _ in отказ.запросы if "/data-" in u]) == 1,
              "после 403 пошли за другой версией")
    return "паспорт-версии", errors


def case_разбор(мп):
    errors = []
    д = наборы()
    по_инн, строк, без = мп.разобрать("лекарства", д["лекарства"])
    check(errors, строк == 3 and без == 0 and len(по_инн["0721009345"]) == 2, по_инн)
    л = по_инн["0721009345"]
    check(errors, л[0] == {"номер": "Л012-1", "прежний_номер": "1-ЛС-П", "статус": "действующая",
                           "внесена": "2017-04-20", "действует_с": "2013-02-06",
                           "решение": "156", "решение_от": None,
                           "работы": 'Производство "газов" медицинских'}, л[0])
    check(errors, л[1]["статус"] == "приостановлена до 18-12-2026" and л[1]["работы"] is None, л[1])
    по_инн, _, _ = мп.разобрать("производители", д["производители"])
    check(errors, по_инн == {"0721009345": True, "7702664252": True}, по_инн)
    по_инн, строк, _ = мп.разобрать("заключения", д["заключения"])
    з = sorted(по_инн["0721009345"], key=lambda x: (x["номер"] or "", x["дата"]))
    check(errors, строк == 7 and len(з) == 4
          and з[3] == {"номер": "14609/19", "дата": "2024-02-15", "действует_до": "2027-02-14"}
          and з[0]["номер"] is None, з)
    # разные заключения без номера с одинаковыми датами не склеиваются
    по_инн, _, _ = мп.разобрать("заключения", наборы(заключения=[
        ["2023-06-30", "", "2026-06-27", "https://gisp.gov.ru/a.pdf", ФИО, "0721009345", "1"],
        ["2023-06-30", "", "2026-06-27", "https://gisp.gov.ru/b.pdf", ФИО, "0721009345", "1"]])
        ["заключения"])
    check(errors, len(по_инн["0721009345"]) == 2 and "_ключ" not in по_инн["0721009345"][0],
          по_инн)
    сырое = json.dumps([мп.разобрать(н, д[н])[0] for н in д], ensure_ascii=False)
    check(errors, ФИО not in сырое and "Условн" not in сырое and "gisp" not in сырое,
          "в индекс попали наименование, адрес или ссылка на PDF")
    for метка, набор, данные, ждём in (
            ("чужой статус", "лекарства", наборы([лс("0721009345", "1", "Прекращена")])["лекарства"],
             "статус"),
            ("без номера лицензии", "лекарства", наборы([лс("0721009345", "")])["лекарства"],
             "без номера"),
            ("нет колонки", "производители", в_csv([["Name", "ИНН"], ["x", "0721009345"]]), "INN"),
            ("пусто", "производители", в_csv([["Nameoforg", "INN", "OGRN", "Address"]]), "нет строк"),
            ("незакрытая кавычка", "производители",
             b'Nameoforg,INN,OGRN,Address\r\nx,0721009345,1,"-\r\ny,7702664252,1,-\r\n', "битый"),
            ("строка не по шапке", "производители",
             в_csv([["Nameoforg", "INN", "OGRN", "Address"], ["x", "0721009345"]]), "шапке"),
            ("без ИНН", "производители", в_csv([["Nameoforg", "INN", "OGRN", "Address"]]
                                              + [["x", "", "1", "-"]] * 2
                                              + [["x", "0721009345", "1", "-"]] * 50), "без ИНН")):
        try:
            мп.разобрать(набор, данные)
            errors.append("%s: нет отказа" % метка)
        except ValueError as e:
            check(errors, ждём in str(e), "%s: %s" % (метка, e))
    return "разбор", errors


def case_refresh(мп):
    errors = []
    with tempfile.TemporaryDirectory() as td:
        рез = мп.refresh(cache_dir=td, get=Сеть(мп, наборы()), сегодня=СЕГОДНЯ)
        check(errors, рез["статус"] == "обновлено"
              and рез["наборы"]["заключения"]["строк"] == 7, рез)
        res, _ = мп.lookup("0721009345", cache_dir=td, сегодня=СЕГОДНЯ)
        check(errors, res["в_реестрах"] and len(res["лицензии_лекарства"]) == 2
              and res["производитель_пп719"] and res["заключений_пп719"] == 4
              and res["действующих_заключений"] == 1
              and res["заключения_показаны"][0]["номер"] == "14609/19", res)
        check(errors, res["выгрузки"]["лекарства"] == {"дата": "2026-09-28", "пропущены_404": []},
              res["выгрузки"])
        res, _ = мп.lookup("7702664252", cache_dir=td, сегодня=СЕГОДНЯ)
        check(errors, res["в_реестрах"] and not res["лицензии_лекарства"]
              and res["действующих_заключений"] == 1, res)
        res, _ = мп.lookup("7700000000", cache_dir=td, сегодня=СЕГОДНЯ)
        check(errors, res["в_реестрах"] is False, res)
    with tempfile.TemporaryDirectory() as td:
        # срок не распознан — не «недействующее», а отдельный счётчик
        мп.refresh(cache_dir=td, get=Сеть(мп, наборы(заключения=[
            ["2016-11-01", "69490/03", "-", "-", ФИО, "0721009345", "1"]])), сегодня=СЕГОДНЯ)
        res, _ = мп.lookup("0721009345", cache_dir=td, сегодня=СЕГОДНЯ)
        check(errors, res["заключений_пп719"] == 1 and res["действующих_заключений"] == 0
              and res["заключений_срок_неизвестен"] == 1, res)
    with tempfile.TemporaryDirectory() as td:
        мп.refresh(cache_dir=td, get=Сеть(мп, наборы()), сегодня=СЕГОДНЯ)
        res, _ = мп.lookup("7702664252", cache_dir=td, сегодня="2026-11-01")
        check(errors, len(res["предупреждения"]) == 3, res["предупреждения"])
        # обвал строк или сломанный набор — старый индекс цел
        до = мп.dump_tools.прочитать_индекс(os.path.join(td, мп.ИНДЕКС))
        for метка, данные in (
                ("обвал", наборы(заключения=[["2024-02-15", "1", "2027-02-14", "-", ФИО,
                                             "0721009345", "1"]])),
                ("схема", наборы(лекарства=[лс("0721009345", "1", "???")]))):
            рез = мп.refresh(cache_dir=td, get=Сеть(мп, данные), сегодня="2026-09-29")
            check(errors, рез["статус"].startswith("индекс не обновлён")
                  and мп.dump_tools.прочитать_индекс(os.path.join(td, мп.ИНДЕКС)) == до,
                  "%s: %r" % (метка, рез))
        open(os.path.join(td, ".refresh.lock"), "w").close()
        check(errors, "уже идёт" in мп.refresh(cache_dir=td, get=Сеть(мп, наборы()))["статус"],
              "блокировка")
    with tempfile.TemporaryDirectory() as td:
        res, note = мп.lookup("0721009345", cache_dir=td)
        check(errors, res is None and "--refresh" in note, note)
    return "refresh-lookup", errors


def case_движок():
    errors = []
    дом = os.environ.get("HOME")
    with tempfile.TemporaryDirectory() as td:
        os.environ["HOME"] = td
        try:
            fc = load("fetch_counterparty")
            блок = "минпромторг"
            данные, av = fc.FETCHERS[блок](None, "0721009345")
            check(errors, данные is None and "minpromtorg.py --refresh" in av["причина"], av)
            мп = load("minpromtorg")
            сегодня = dt.datetime.now(dt.timezone.utc).astimezone().date()
            рез = мп.refresh(cache_dir=мп.CACHE_DIR, get=Сеть(мп, наборы(), даты=(
                сегодня.strftime("%Y%m%d"),)), сегодня=сегодня.isoformat())
            check(errors, рез["статус"] == "обновлено", рез)
            данные, av = fc.FETCHERS[блок](None, "0721009345")
            check(errors, av["состояние"] == "ok" and данные["лицензии_лекарства"], av)
            пусто, av = fc.FETCHERS[блок](None, "7700000000")
            check(errors, пусто is None and av["состояние"] == "пусто"
                  and fc.SOURCES[блок]["пусто_с_оговоркой"] in av["причина"], av)
            snap = load("snapshot")
            данные, av = fc.FETCHERS[блок](None, "0721009345")
            а = {"инн": "0721009345", блок: данные, "_доступность": {блок: av}}
            б = json.loads(json.dumps(а, ensure_ascii=False))
            б[блок]["скачан"], б[блок]["выгрузки"], б[блок]["предупреждения"] = "x", {}, ["x"]
            check(errors, snap.хеш_остального(а) == snap.хеш_остального(б),
                  "даты выгрузки в хеше снимка")
        finally:
            if дом is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = дом
    return "блок-движка", errors


def main():
    мп = load("minpromtorg")
    failed = 0
    cases = [case_паспорт(мп), case_разбор(мп), case_refresh(мп), case_движок()]
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
