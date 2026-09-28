#!/usr/bin/env python3
"""
run_uc_eval.py — офлайн-eval аккредитованных УЦ Минцифры (uc_mintsifry.py):
ссылки на перечни из page-data страницы (по тексту ссылки, чужой хост и дубли —
отказ), разбор xlsx (синтетика по образцу живых перечней 21.09.2026: ИНН в тексте
«(ИНН …, ОГРН …)», опечатка в ИНН), обвал строк — индекс цел, lookup, блок
«уц_минцифры» движка и снимок. Наименований и адресов в индексе нет. PASS/FAIL,
stdlib, CI.
"""

import datetime as dt
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
СЕГОДНЯ = "2026-09-28"
ФИО = "ФИО УБРАНО ИЗ ФИКСТУРЫ"
АДРЕС = "г. Условный, ул. Условная, 1"


def load(имя, папка="scripts"):
    sys.path.insert(0, str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(имя, ROOT / папка / ("%s.py" % имя))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


xlsx = load("run_cbr_registries_eval", "eval").xlsx


def check(errors, cond, msg):
    if not cond:
        errors.append(msg)


URL_Д = "https://adm.digital.gov.ru/app/uploads/2026/09/0a5013_perechen-na-21.09.2026.xlsx"
ПОДПИСЬ_Д = "Перечень аккредитованных удостоверяющих центров на 21.09.2026"
ПОДПИСЬ_П = ("Перечень аккредитованных удостоверяющих центров, деятельность которых "
             "прекращена на 21.09.2026")
ПОДПИСЬ_ПРИОСТ = ("Перечень аккредитованных удостоверяющих центров, аккредитация которых "
                  "приостановлена на 30.04.2026")
URL_П = "https://adm.digital.gov.ru/app/uploads/2026/09/b17839_prekrashhena-na-21.09.2026.xlsx"


def страница(ссылки=None):
    ссылки = ссылки if ссылки is not None else [
        (URL_Д, ПОДПИСЬ_Д),
        (URL_П, ПОДПИСЬ_П),
        ("https://adm.digital.gov.ru/app/uploads/2026/04/x.xls",
         ПОДПИСЬ_ПРИОСТ)]
    текст = "".join('<li><a class="text-ink" href="%s">%s</a></li>' % с for с in ссылки)
    return json.dumps({"blocks": [{"data": {"title": "Где посмотреть", "text": текст}}]},
                      ensure_ascii=False).replace("/", "\\/").encode()


def действующие(строки=None):
    return xlsx({"Лист1": [
        ["Перечень аккредитованных удостоверяющих центров", "", "", "", "", ""],
        ["N п/п", "Название организации/удостоверяющего центра, ИНН, ОГРН", "№ Приказа",
         "аккредитация", "Адрес  места нахождения организации", "Примечание"],
    ] + (строки if строки is not None else [
        ["1", "ООО «%s»\r\n(ИНН 7605016030, ОГРН 1027600787994)" % ФИО, "Приказ № 164",
         "действующая", АДРЕС, "Аккредитация приостановлена с 13.10.2017. \r\n Возобновлена."],
        ["2", "АО «Б» (ИНН 7714313724, ОГРН 1)", "Приказ № 354", "действующая", АДРЕС, ""],
    ] + [[str(i), "АО (ИНН 7800000%03d, ОГРН 1)" % i, "П", "действующая", АДРЕС, ""]
         for i in range(10, 30)])})


def прекращённые(строки=None):
    return xlsx({"Лист1": [
        ["Перечень …, деятельность которых прекращена", "", "", "", ""],
        ["N п/п", "Название организации/удостоверяющего центра, ИНН, ОГРН", "№ Приказа",
         "Адрес  места нахождения организации", "Примечание"],
    ] + (строки if строки is not None else [
        ["1", "ООО «УЦ» (ИНН 7604094283; ОГРН 1067604081710)", "Приказ № 170", АДРЕС,
         "Аккредитация прекращена 14.09.2026"],
        ["2", "ООО «В» (ИНН 7714313724, ОГРН 1)", "Приказ № 1", АДРЕС, "Прекращена 23.02.2026"],
        ["3", "ООО «КРИПТО» (ИНН 616518035, ОГРН 1)", "Приказ № 259", АДРЕС, "опечатка в ИНН"],
        ["4", "ООО (ИНН 76050160301, ОГРН 1)", "П", АДРЕС, "11 цифр"],
        ["5", "ООО (ИНН 7605016030) и ООО (ИНН 7714313724)", "П", АДРЕС, "два ИНН"],
    ] + [[str(i), "ООО (ИНН 7700000%03d, ОГРН 1)" % i, "П", АДРЕС, ""]
         for i in range(10, 260)]), "Лист2": []})


class Сеть:
    def __init__(self, ун, стр=None, д=None, п=None):
        self.ун, self.запросы = ун, []
        self.ответы = {ун.ДАННЫЕ_СТРАНИЦЫ: стр or страница(), URL_Д: д or действующие(),
                       URL_П: п or прекращённые()}

    def __call__(self, url, referer, лимит, в_файл=None):
        self.запросы.append((url, referer))
        return self.ответы[url]


def case_ссылки(ун):
    errors = []
    н = ун.ссылки(страница().decode())
    check(errors, н == {"действующие": (URL_Д, "2026-09-21"), "прекращённые": (URL_П, "2026-09-21")},
          н)
    for метка, ссылки in (
            ("чужой хост", [("https://example.com/app/uploads/a.xlsx",
                             ПОДПИСЬ_Д),
                            (URL_П, ПОДПИСЬ_П)]),
            ("нет прекращённых", [(URL_Д, ПОДПИСЬ_Д)]),
            ("две действующих", [(URL_Д, ПОДПИСЬ_Д)] * 2 + [
                (URL_П, ПОДПИСЬ_П)])):
        try:
            ун.ссылки(страница(ссылки).decode())
            errors.append("%s: нет отказа" % метка)
        except ValueError:
            pass
    return "ссылки", errors


def case_разбор(ун):
    errors = []
    по_инн, всего, без = ун.разобрать("действующие", действующие())
    check(errors, всего == 22 and без == 0 and по_инн["7605016030"] == [{
        "перечень": "действующие", "статус": "действующая", "приказ": "Приказ № 164",
        "примечание": "Аккредитация приостановлена с 13.10.2017. Возобновлена."}], по_инн)
    по_инн, всего, без = ун.разобрать("прекращённые", прекращённые())
    check(errors, всего == 255 and без == 3 and "7605016030" not in по_инн and по_инн["7604094283"][0]["статус"] == "прекращена"
          and "616518035" not in json.dumps(по_инн), (всего, без))
    сырое = json.dumps([ун.разобрать("действующие", действующие())[0],
                        ун.разобрать("прекращённые", прекращённые())[0]], ensure_ascii=False)
    check(errors, ФИО not in сырое and "Условн" not in сырое, "наименование или адрес в индексе")
    for метка, перечень, данные, ждём in (
            ("нет шапки", "действующие", xlsx({"Лист1": [["a", "b"], ["1", "ИНН 7605016030"]]}),
             "шапки"),
            ("нет статуса в шапке", "действующие", xlsx({"Лист1": [
                ["N п/п", "Название организации", "№ Приказа", "Примечание"],
                ["1", "(ИНН 7605016030)", "П", ""]]}), "колонок"),
            ("пустой статус", "действующие", действующие([["1", "(ИНН 7605016030)", "П", "", "",
                                                          ""]]), "статуса"),
            ("много без ИНН", "прекращённые", прекращённые([["1", "ООО", "П", АДРЕС, ""]] * 5),
             "без одного ИНН"),
            ("пусто", "прекращённые", прекращённые([]), "нет строк"),
            ("два листа", "прекращённые", xlsx({"Лист1": [["Название организации"]],
                                                 "Лист2": [["x"]]}), "листов")):
        try:
            ун.разобрать(перечень, данные)
            errors.append("%s: нет отказа" % метка)
        except ValueError as e:
            check(errors, ждём in str(e), "%s: %s" % (метка, e))
    return "разбор", errors


def case_refresh(ун):
    errors = []
    with tempfile.TemporaryDirectory() as td:
        сеть = Сеть(ун)
        рез = ун.refresh(cache_dir=td, get=сеть, сегодня=СЕГОДНЯ)
        check(errors, рез["статус"] == "обновлено" and рез["организаций"] == 273, рез)
        check(errors, [r for _, r in сеть.запросы] == [ун.СТРАНИЦА] * 3, сеть.запросы)
        res, _ = ун.lookup("7714313724", cache_dir=td, сегодня=СЕГОДНЯ)
        check(errors, res["аккредитован_сейчас"] and {з["перечень"] for з in res["записи"]}
              == {"действующие", "прекращённые"} and res["записи"][0]["на_дату"] == "2026-09-21",
              res)
        res, _ = ун.lookup("7604094283", cache_dir=td, сегодня=СЕГОДНЯ)
        check(errors, res["в_перечнях"] and not res["аккредитован_сейчас"], res)
        res, _ = ун.lookup("7700000000", cache_dir=td, сегодня=СЕГОДНЯ)
        check(errors, not res["в_перечнях"] and not res["предупреждения"], res)
        res, _ = ун.lookup("7700000000", cache_dir=td, сегодня="2026-11-20")
        check(errors, res["предупреждения"], "устаревание")
        # действующий перечень давний — «сейчас» не утверждается
        res, _ = ун.lookup("7605016030", cache_dir=td, сегодня="2027-03-21")
        check(errors, res["аккредитован_сейчас"] is None, res["аккредитован_сейчас"])
        res, _ = ун.lookup("7605016030", cache_dir=td, сегодня="2027-03-20")
        check(errors, res["аккредитован_сейчас"] is True, "граница 180 дней")
        до = ун.dump_tools.прочитать_индекс(os.path.join(td, ун.ИНДЕКС))
        рез = ун.refresh(cache_dir=td, get=Сеть(ун, п=прекращённые([
            ["1", "ООО (ИНН 7604094283, ОГРН 1)", "П", АДРЕС, ""]])), сегодня="2026-09-29")
        check(errors, "не обновлён" in рез["статус"] and "было 255" in рез["статус"]
              and ун.dump_tools.прочитать_индекс(os.path.join(td, ун.ИНДЕКС)) == до, рез)
    with tempfile.TemporaryDirectory() as td:
        # прекращение опубликовано позже действующего перечня
        стр = страница([(URL_Д, ПОДПИСЬ_Д), (URL_П, ПОДПИСЬ_П.replace("21.09.2026", "25.09.2026"))])
        ун.refresh(cache_dir=td, get=Сеть(ун, стр=стр), сегодня=СЕГОДНЯ)
        res, _ = ун.lookup("7714313724", cache_dir=td, сегодня=СЕГОДНЯ)
        check(errors, res["аккредитован_сейчас"] is None, res["аккредитован_сейчас"])
        res, _ = ун.lookup("7605016030", cache_dir=td, сегодня=СЕГОДНЯ)
        check(errors, res["аккредитован_сейчас"] is True, "без прекращения — действует")
    with tempfile.TemporaryDirectory() as td:
        # первый refresh: обрезанный перечень не принимается и без прежнего индекса
        рез = ун.refresh(cache_dir=td, get=Сеть(ун, п=прекращённые([
            ["1", "ООО (ИНН 7604094283, ОГРН 1)", "П", АДРЕС, ""]])), сегодня=СЕГОДНЯ)
        check(errors, "не обновлён" in рез["статус"]
              and not os.path.exists(os.path.join(td, ун.ИНДЕКС)), рез)
    with tempfile.TemporaryDirectory() as td:
        res, note = ун.lookup("7605016030", cache_dir=td)
        check(errors, res is None and "--refresh" in note, note)
    return "refresh-lookup", errors


def case_движок():
    errors = []
    дом = os.environ.get("HOME")
    with tempfile.TemporaryDirectory() as td:
        os.environ["HOME"] = td
        try:
            fc = load("fetch_counterparty")
            блок = "уц_минцифры"
            данные, av = fc.FETCHERS[блок](None, "7605016030")
            check(errors, данные is None and "uc_mintsifry.py --refresh" in av["причина"], av)
            ун = load("uc_mintsifry")
            день = dt.datetime.now(dt.timezone.utc).astimezone().date()
            дмг = день.strftime("%d.%m.%Y")
            стр = страница([(URL_Д, ПОДПИСЬ_Д.replace("21.09.2026", дмг)),
                            (URL_П, ПОДПИСЬ_П.replace("21.09.2026", дмг))])
            check(errors, ун.refresh(get=Сеть(ун, стр=стр), сегодня=день.isoformat())["статус"]
                  == "обновлено", "refresh")
            данные, av = fc.FETCHERS[блок](None, "7605016030")
            check(errors, av["состояние"] == "ok" and данные["аккредитован_сейчас"], av)
            пусто, av_пусто = fc.FETCHERS[блок](None, "7700000000")
            check(errors, пусто is None and av_пусто["состояние"] == "пусто"
                  and fc.SOURCES[блок]["пусто_с_оговоркой"] in av_пусто["причина"], av_пусто)
            snap = load("snapshot")
            а = {"инн": "7605016030", блок: данные, "_доступность": {блок: av}}
            б = json.loads(json.dumps(а, ensure_ascii=False))
            б[блок]["скачан"], б[блок]["перечни"], б[блок]["предупреждения"] = "x", {}, ["x"]
            check(errors, snap.хеш_остального(а) == snap.хеш_остального(б),
                  "даты перечней в хеше снимка")
        finally:
            if дом is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = дом
    return "блок-движка", errors


def main():
    ун = load("uc_mintsifry")
    failed = 0
    cases = [case_ссылки(ун), case_разбор(ун), case_refresh(ун), case_движок()]
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
