#!/usr/bin/env python3
"""
run_calibration_eval.py — офлайн-eval инструмента калибровки (задача 5).

Калибровка публикует цифры точности, поэтому проверяются места, где выборка
тихо врёт, а не «считается ли что-то»:

  (а) год дела берётся из номера («А65-37758/2017» -> 2017), без года — None;
  (б) дело подано до/в отчётном году -> «исключён», а не «банкрот»: иначе в
      случаи попадают компании, уже бывшие в суде на дату отчёта;
  (в) нечёткий поиск ЕФРСБ: чужой ИНН в выдаче не делает компанию банкротом;
  (г) запись без номера дела -> «не проверено», а не «выжил»;
  (д) периоды позже отчётного года из сырых данных вырезаются (утечка будущего);
  (е) признаки считает код движка: флаги fin_scoring и непрерывные показатели
      совпадают на известной отчётности;
  (ж) веса фазы 2: точность и лифт на известной матрице, контроль с весом 1/f;
  (з) показатель не посчитан -> строка вне знаменателя и видна в «покрытии»;
  (и) когорта: только организации с периодом 2021 и ИНН; последовательность
      случайных id воспроизводима по seed, без повторов, в пределах [1, верх];
  (к) SOCKS5-opener не выпускает http:// мимо прокси и маскирует пароль.

PASS/FAIL, чистый stdlib, сети не требует.
"""

import importlib.util
import itertools
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CAL = ROOT / "calibration"
sys.path.insert(0, str(CAL))
sys.path.insert(0, str(ROOT / "scripts"))


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check(errors, cond, msg):
    if not cond:
        errors.append(msg)


ИНН = "7713357944"


def _ефрсб(*записи):
    return {"pageData": list(записи), "total": len(записи)}


def _запись(inn, номер, code="Tender", date="2023-05-01T00:00:00"):
    return {"inn": inn, "lastLegalCase": {"number": номер,
                                          "status": {"code": code, "date": date}}}


def case_a_case_year(lb):
    e = []
    check(e, lb.год_дела("А65-37758/2017") == 2017, "А65-37758/2017 -> 2017")
    check(e, lb.год_дела("А40-1/2023 ") == 2023, "пробел в конце не мешает")
    check(e, lb.год_дела("А40-12345") is None, "без года -> None")
    check(e, lb.год_дела(None) is None, "None -> None")
    check(e, lb.год_дела("А40-2017/12") is None, "«2017» не в конце — не год дела")
    return e


def case_b_already_in_court(lb):
    e = []
    for номер, ждём in (("А40-1/2020", "исключён"), ("А40-1/2021", "исключён"),
                        ("А40-1/2022", "банкрот"), ("А40-1/2024", "банкрот"),
                        ("А40-1/2025", "выжил")):
        # дата стадии — в окне, но решает год дела, а не дата стадии
        к = lb.исход(ИНН, _ефрсб(_запись(ИНН, номер, date="2022-10-01T00:00:00")))["класс"]
        check(e, к == ждём, "%s: ждали %r, получили %r" % (номер, ждём, к))
    return e


def case_c_fuzzy_search(lb):
    e = []
    к = lb.исход(ИНН, _ефрсб(_запись("7713357945", "А40-1/2022")))["класс"]
    check(e, к == "выжил", "чужой ИНН в выдаче сделал компанию %r" % к)
    к = lb.исход(ИНН, _ефрсб())["класс"]
    check(e, к == "выжил", "пустая выдача -> %r" % к)
    к = lb.исход(ИНН, {"ошибка": 1})["класс"]
    check(e, к == "не проверено", "ответ без pageData -> %r" % к)
    return e


def case_d_no_case_number(lb):
    e = []
    к = lb.исход(ИНН, _ефрсб(_запись(ИНН, None)))["класс"]
    check(e, к == "не проверено", "запись без номера дела -> %r (а надо «не проверено»)" % к)
    к = lb.исход(ИНН, _ефрсб(_запись(ИНН, "А40-1/2022"), _запись(ИНН, "А40-2/2019")))
    check(e, к["класс"] == "исключён" and к["год_дела"] == 2019,
          "два дела: решает самое раннее (2019) -> %r" % к)
    return e


def _гирбо(годы):
    """годы: {год: {код: значение}} -> сырые ответы в формате raw_finance."""
    bfo, details = [], {}
    for i, (год, строки) in enumerate(sorted(годы.items())):
        bid = 100 + i
        bfo.append({"id": bid, "period": str(год), "gainSum": строки.get("2110"),
                    "actualBfoDate": "%d-03-30" % (год + 1)})
        details[str(bid)] = [{
            "financialResult": {"current%s" % c: v for c, v in строки.items()
                                if c.startswith("2")},
            "balance": {"current%s" % c: v for c, v in строки.items()
                        if c.startswith("1")},
        }]
    return {"search": {"content": [{"id": 1, "inn": ИНН, "shortName": "ООО «Тест»"}]},
            "bfo": bfo, "details": details}


ПЛОХАЯ_2021 = {"2110": 100, "2400": -10, "1600": 1000, "1300": -50,
               "1200": 300, "1500": 600}
ПЛОХАЯ_2020 = {"2110": 400, "2400": -5, "1600": 900, "1300": -20,
               "1200": 300, "1500": 500}
ХОРОШАЯ_2022 = {"2110": 5000, "2400": 900, "1600": 9000, "1300": 8000,
                "1200": 6000, "1500": 500}


def case_e_no_future(ev):
    e = []
    raw = _гирбо({2020: ПЛОХАЯ_2020, 2021: ПЛОХАЯ_2021, 2022: ХОРОШАЯ_2022})
    срез = ev.только_до(raw)
    check(e, sorted(r["period"] for r in срез["bfo"]) == ["2020", "2021"],
          "периоды после 2021 не вырезаны: %r" % [r["period"] for r in срез["bfo"]])
    п = ev.признаки(raw, ИНН)
    check(e, "нет" not in п, "признаки не посчитались: %r" % п)
    check(e, п.get("выручка") == 100, "выручка взята не за 2021: %r" % п.get("выручка"))
    return e


def case_f_engine_features(ev):
    e = []
    п = ev.признаки(_гирбо({2020: ПЛОХАЯ_2020, 2021: ПЛОХАЯ_2021, 2022: ХОРОШАЯ_2022}), ИНН)
    ждём = {"ча_отрицательные_2года", "коэффициент_автономии", "текущая_ликвидность",
            "падение_выручки", "убыток_2года"}
    check(e, set(п.get("флаги", [])) == ждём, "флаги движка: %r" % п.get("флаги"))
    check(e, abs(п["автономия"] - (-0.05)) < 1e-9, "автономия %r" % п["автономия"])
    check(e, abs(п["ликвидность"] - 0.5) < 1e-9, "ликвидность %r" % п["ликвидность"])
    check(e, abs(п["падение_выручки"] - 0.75) < 1e-9, "падение %r" % п["падение_выручки"])
    check(e, п["ча_отрицательные_2года"] is True and п["убыток_2года"] is True,
          "двухлетние признаки не посчитаны")
    только21 = ev.признаки(_гирбо({2021: ПЛОХАЯ_2021}), ИНН)
    check(e, только21.get("падение_выручки") is None and только21.get("убыток_2года") is None,
          "без 2020 двухлетние признаки должны быть None, а не False: %r" % только21)
    нет21 = ev.признаки(_гирбо({2019: ПЛОХАЯ_2020, 2022: ХОРОШАЯ_2022}), ИНН)
    check(e, "нет" in нет21, "без отчётности за 2021 строка должна выпасть: %r" % нет21)
    return e


def case_l_previous_columns(ev):
    """/bfo/ отдаёт 5 последних периодов: в 2026 формы за 2020 нет, её цифры —
    в колонках previous* формы 2021. Признаки должны совпасть с отдельной формой."""
    e = []
    с_колонками = _гирбо({2021: ПЛОХАЯ_2021, 2022: ХОРОШАЯ_2022})
    форма = с_колонками["details"]["100"][0]
    for c, v in ПЛОХАЯ_2020.items():
        форма["financialResult" if c.startswith("2") else "balance"]["previous%s" % c] = v
    п = ev.признаки(с_колонками, ИНН)
    эталон = ev.признаки(_гирбо({2020: ПЛОХАЯ_2020, 2021: ПЛОХАЯ_2021}), ИНН)
    for k in ("флаги", "падение_выручки", "ча_отрицательные_2года", "убыток_2года"):
        check(e, п.get(k) == эталон.get(k),
              "%s из previous*: %r, из формы 2020: %r" % (k, п.get(k), эталон.get(k)))
    # Отдельная форма за 2020 есть — previous* формы 2021 не подмешивается.
    обе = _гирбо({2020: ПЛОХАЯ_2020, 2021: ПЛОХАЯ_2021})
    обе["details"]["101"][0]["financialResult"]["previous2110"] = 999999
    срез = ev.только_до(обе)
    check(e, sorted(r["period"] for r in срез["bfo"]) == ["2020", "2021"],
          "лишний период при живой форме 2020: %r" % [r["period"] for r in срез["bfo"]])
    return e


def case_m_reference_tables(ev):
    """Таблицы references/kalibrovka.md — ровно рендер calibration/report.json."""
    e = []
    rep = json.loads((CAL / "report.json").read_text(encoding="utf-8"))
    текст = (CAL.parent / "references" / "kalibrovka.md").read_text(encoding="utf-8")
    _, _, хвост = текст.partition(ev.МАРКЕР_НАЧАЛО)
    раздел, найден, _ = хвост.partition(ev.МАРКЕР_КОНЕЦ)
    check(e, найден, "в references/kalibrovka.md нет маркеров таблиц")
    check(e, раздел.strip() == ev.таблицы_md(rep).strip(),
          "таблицы references/kalibrovka.md разошлись с calibration/report.json — "
          "перегенерировать: python3 calibration/evaluate.py --справочник")
    return e


def case_g_weights(ev):
    e = []
    # 10 случаев (6 флагнуты), 100 контролей в фазе 2 = 10% от 1000 выживших (10 флагнуты).
    строки = ([{"случай": True, "вес": 1.0, "ф": i < 6} for i in range(10)]
              + [{"случай": False, "вес": 10.0, "ф": i < 10} for i in range(100)])
    m = ev.метрики(строки, lambda r: r["ф"])
    # генеральная: TP=6, FP=100 (10*10), FN=4, TN=900
    check(e, abs(m["полнота"] - 0.6) < 1e-9, "полнота %r" % m["полнота"])
    check(e, abs(m["точность"] - 6 / 106) < 1e-9, "точность %r (ждали 6/106)" % m["точность"])
    check(e, abs(m["базовая_частота"] - 10 / 1010) < 1e-9, "база %r" % m["базовая_частота"])
    check(e, abs(m["лифт"] - (6 / 106) / (10 / 1010)) < 1e-9, "лифт %r" % m["лифт"])
    check(e, abs(m["ложные_срабатывания"] - 0.1) < 1e-9, "FPR %r" % m["ложные_срабатывания"])
    ди = ev.бутстреп(строки, lambda r: r["ф"], 200, 1)
    check(e, "полнота" in ди and ди["полнота"][0] <= 0.6 <= ди["полнота"][1],
          "ДИ полноты не накрывает точечную оценку: %r" % ди.get("полнота"))
    return e


def case_h_coverage(ev):
    e = []
    строки = [{"случай": True, "вес": 1.0, "v": 0.05}, {"случай": True, "вес": 1.0, "v": None},
              {"случай": False, "вес": 1.0, "v": 0.5}, {"случай": False, "вес": 1.0, "v": None}]
    m = ev.метрики(строки, lambda r: None if r["v"] is None else r["v"] < 0.1)
    check(e, abs(m["покрытие"] - 0.5) < 1e-9, "покрытие %r" % m["покрытие"])
    check(e, m["полнота"] == 1.0, "не посчитанный случай попал в знаменатель: %r" % m["полнота"])
    check(e, ev.метрики([{"случай": False, "вес": 1.0}], lambda r: True) is None,
          "без случаев метрики должны быть None, а не деление на ноль")
    # Флаг движка без исходного показателя — вне расчёта, а не «не флагнут».
    нет_строк = {"случай": False, "вес": 1.0, "флаги": [], "ликвидность": None}
    есть = {"случай": True, "вес": 1.0, "флаги": ["текущая_ликвидность"], "ликвидность": 0.4}
    m = ev.метрики([нет_строк, есть], ev._флаг_движка("текущая_ликвидность"))
    check(e, m and abs(m["покрытие"] - 0.5) < 1e-9,
          "ликвидность без строк 1200/1500 посчитана как «не флагнута»: %r" % m)
    return e


def case_i_cohort_row(co):
    e = []
    info = {"inn": "5036045205", "fullName": "АО «Тест»", "okved2_id": "47.11", "okopf_id": 12200}
    bfo = [{"id": 1, "period": "2023", "organizationInfo": info},
           {"id": 2, "period": "2021", "organizationInfo": info}]
    row = co.строка_когорты(77, bfo)
    check(e, row and row["inn"] == "5036045205" and row["id"] == 77 and row["bfo"] == bfo,
          "строка когорты: %r" % row)
    check(e, co.строка_когорты(77, [dict(bfo[0])]) is None,
          "без периода 2021 компания попала в когорту")
    check(e, co.строка_когорты(77, []) is None, "пустой /bfo/ попал в когорту")
    check(e, co.строка_когорты(77, [{"id": 2, "period": "2021", "organizationInfo": {}}]) is None,
          "без ИНН компания попала в когорту")
    a = list(itertools.islice(co.последовательность_id(7, верх=1000), 500))
    b = list(itertools.islice(co.последовательность_id(7, верх=1000), 500))
    ids = a
    check(e, a == b, "последовательность id не воспроизводится по seed")
    check(e, len(set(ids)) == len(ids), "в последовательности id есть повторы")
    check(e, min(ids) >= 1 and max(ids) <= 1000, "id вне [1, верх]")
    полная = list(itertools.islice(co.последовательность_id(3, верх=50), 50))
    check(e, sorted(полная) == list(range(1, 51)), "последовательность не покрывает весь диапазон")
    return e


def case_k_socks(s5):
    e = []
    url = "socks5://user:secret@127.0.0.1:1"
    check(e, "secret" not in s5.маска(url) and "***" in s5.маска(url),
          "маска показывает пароль: %r" % s5.маска(url))
    try:
        s5.разобрать("http://127.0.0.1:3128")
        e.append("http:// принят как SOCKS5")
    except ValueError:
        pass
    op = s5.opener(url)
    try:
        op.open("http://example.invalid/", timeout=2)
        e.append("http:// ушёл мимо SOCKS5-прокси")
    except Exception as ex:
        check(e, "только https" in str(ex), "http:// упал не отказом, а %r" % ex)
    return e


def main():
    lb = load_module("label", CAL / "label.py")
    s5 = load_module("socks5", CAL / "socks5.py")
    co = load_module("collect", CAL / "collect.py")
    ev = load_module("evaluate", CAL / "evaluate.py")
    cases = [
        ("год-дела-из-номера", lambda: case_a_case_year(lb)),
        ("уже-в-суде-исключён", lambda: case_b_already_in_court(lb)),
        ("нечёткий-поиск-ефрсб", lambda: case_c_fuzzy_search(lb)),
        ("нет-номера-не-проверено", lambda: case_d_no_case_number(lb)),
        ("без-утечки-будущего", lambda: case_e_no_future(ev)),
        ("признаки-кодом-движка", lambda: case_f_engine_features(ev)),
        ("год-назад-из-колонок-previous", lambda: case_l_previous_columns(ev)),
        ("веса-двухфазной-выборки", lambda: case_g_weights(ev)),
        ("покрытие-не-в-знаменателе", lambda: case_h_coverage(ev)),
        ("строка-когорты-и-id", lambda: case_i_cohort_row(co)),
        ("socks5-без-утечки", lambda: case_k_socks(s5)),
        ("справочник-из-отчёта", lambda: case_m_reference_tables(ev)),
    ]
    failed = 0
    for name, fn in cases:
        errors = fn()
        if errors:
            failed += 1
            print("FAIL %s" % name)
            for er in errors:
                print("  - %s" % er)
        else:
            print("PASS %s" % name)
    if failed:
        print("FAIL: %d/%d кейсов упало" % (failed, len(cases)))
        return 1
    print("PASS: все %d кейсов зелёные" % len(cases))
    return 0


if __name__ == "__main__":
    sys.exit(main())
