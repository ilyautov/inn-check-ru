#!/usr/bin/env python3
"""
run_svetofor_eval.py — офлайн-eval измеренного светофора (волна 5, блок F):
классы финансовой части, исключения, отчёт воспроизводится из когорты, таблица
в references/kalibrovka.md — ровно рендер отчёта. PASS/FAIL, stdlib, CI.
"""

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CAL = ROOT / "calibration"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check(errors, cond, msg):
    if not cond:
        errors.append(msg)


def _строка(**kw):
    r = {"флаги": "", "автономия": "0.5", "ликвидность": "1.5", "падение_выручки": "0.1",
         "ча_отрицательные_2года": "False", "убыток_2года": "False",
         "okved": "47.1", "окопф": "12300", "класс": "выжил", "год_дела": "", "вес": "9.8"}
    r.update(kw)
    return r


def case_классы(ms):
    errors = []
    for имя, r, ждём in (
            ("всё посчитано, флагов нет", _строка(), "нет сигналов"),
            ("нет ликвидности — не «нет сигналов»", _строка(ликвидность=""), "не измерено"),
            ("красный побеждает", _строка(флаги="коэффициент_автономии;ча_отрицательные_2года"),
             "🔴"),
            ("жёлтый при пропуске показателя", _строка(флаги="убыток_2года", ликвидность=""),
             "🟡"),
            ("чужой флаг не в счёт", _строка(флаги="ча_ниже_уставного_капитала"),
             "нет сигналов")):
        check(errors, ms.класс(r) == ждём, "%s: %r" % (имя, ms.класс(r)))
    for имя, r, ждём in (
            ("банк", _строка(okved="64.19"), "банк"),
            ("страховая", _строка(okved="65.12"), "страх"),
            ("ТСЖ", _строка(окопф="20716"), "некоммерч"),
            ("АНО", _строка(окопф="71400"), "некоммерч"),
            ("ООО", _строка(), None),
            ("унитарное", _строка(окопф="65243"), None),
            ("лизинг 64.91 — не банк", _строка(okved="64.91"), None),
            ("старый ОКОПФ 53 (КФХ) — не НКО", _строка(окопф="53"), None)):
        п = ms.исключена(r)
        check(errors, (п is None) if ждём is None else (п or "").startswith(ждём),
              "%s: %r" % (имя, п))
    строки, выпало = ms.строки_варианта(
        [_строка(класс="банкрот", год_дела="2022", вес="1"),
         _строка(класс="банкрот", год_дела="2023", вес="1"), _строка()], False)
    check(errors, len(строки) == 2 and sum(выпало.values()) == 1,
          "строгий вариант не исключил дело 2022: %r %r" % (строки, выпало))
    строки, _ = ms.строки_варианта([_строка(класс="банкрот", год_дела="2022", вес="1")], True)
    check(errors, len(строки) == 1, "вариант с 2022 потерял дело")
    m = ms.метрики([{"случай": True, "вес": 1.0, "класс": "нет сигналов"},
                    {"случай": True, "вес": 1.0, "класс": "не измерено"},
                    {"случай": False, "вес": 8.0, "класс": "нет сигналов"}])
    check(errors, m["p_нет_сигналов_при_деле"] == 0.5
          and m["p_нет_сигналов_при_деле_среди_измеренных"] == 1.0
          and abs(m["покрытие"] - 0.9) < 1e-9, "метрики: %r" % m)
    return errors


def case_отчёт(ms):
    """Отчёт в репозитории — ровно пересчёт по сохранённым параметрам, с ДИ."""
    errors = []
    сохранён = json.loads((CAL / "svetofor_report.json").read_text(encoding="utf-8"))
    свежий = ms.отчёт(ms.прочитать(), сохранён["бутстреп"]["повторы"],
                      сохранён["бутстреп"]["seed"])
    свежий = json.loads(json.dumps(свежий, ensure_ascii=False))
    for ключ in set(свежий) | set(сохранён):
        check(errors, свежий.get(ключ) == сохранён.get(ключ),
              "%s: отчёт устарел — перегенерируйте calibration/measured_svetofor.py" % ключ)
    for ключ, с in сохранён["варианты"].items():
        check(errors, с["p_нет_сигналов_при_деле"] <= с["p_без_сигналов_при_деле"],
              "%s: узкая доля больше широкой" % ключ)
        # качественные утверждения раздела в kalibrovka.md
        check(errors, с["классы"]["нет сигналов"]["p_дело"] < с["базовая_частота"],
              "%s: «нет сигналов» не ниже базовой частоты — текст раздела неверен" % ключ)
        check(errors, с["p_нет_сигналов_при_деле"] > 0,
              "%s: ложно-зелёных ноль — текст раздела неверен" % ключ)
    текст = (ROOT / "references" / "kalibrovka.md").read_text(encoding="utf-8")
    до, _, хвост = текст.partition(ms.МАРКЕР_НАЧАЛО)
    внутри, найден, _ = хвост.partition(ms.МАРКЕР_КОНЕЦ)
    check(errors, найден and внутри.strip() == ms.таблица_md(сохранён).strip(),
          "раздел kalibrovka.md не совпадает с рендером отчёта — "
          "measured_svetofor.py --справочник")
    return errors


def main():
    ms = load_module("measured_svetofor", CAL / "measured_svetofor.py")
    cases = {"классы, исключения, метрики": case_классы(ms),
             "отчёт и справочник воспроизводятся": case_отчёт(ms)}
    failed = 0
    for name, errors in cases.items():
        if errors:
            failed += 1
            print("FAIL %s" % name)
            for e in errors:
                print("  - %s" % e)
        else:
            print("PASS %s" % name)
    if failed:
        print("FAIL: %d/%d кейсов упало" % (failed, len(cases)))
        return 1
    print("PASS: все %d кейсов зелёные" % len(cases))
    return 0


if __name__ == "__main__":
    sys.exit(main())
