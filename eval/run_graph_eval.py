#!/usr/bin/env python3
"""
run_graph_eval.py — офлайн-прогон affiliates_graph + droblenie_check
по синтетическим ответам checko. PASS/FAIL, чистый stdlib, гоняется в CI.

Сценарий группы: 4 компании, общий директор/учредитель, единый адрес, УСН,
выручка 4×18 млн = 72 млн при пороге НДС 20 млн -> >=4 признаков, 🔴.
Контрольная «здоровая»: одиночная компания без связей -> 0-1 признак, 🟢.
"""

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "eval" / "fixtures" / "graph"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_fixture_fetcher():
    index = {}
    for f in sorted(FIXTURES.glob("checko_*.json")):
        j = json.loads(f.read_text(encoding="utf-8"))
        data = j.get("data", {})
        for key in (data.get("ОГРН"), data.get("ИНН")):
            if key:
                index[str(key)] = data

    def fetcher(kind, value):
        data = index.get(str(value))
        if data is None:
            return None, "в фикстурах нет %s=%s" % (kind, value)
        return data, "ok"
    return fetcher


def check(errors, cond, msg):
    if not cond:
        errors.append(msg)


def case_group(ag, dc, canon):
    errors = []
    graph = ag.build_graph(make_fixture_fetcher(), "7700000001", pause=0)
    check(errors, graph.get("статус") == "ок", "граф: статус %r" % graph.get("статус"))
    check(errors, graph.get("узлов") == 4, "узлов %r != 4" % graph.get("узлов"))
    типы = {r["тип"] for r in graph.get("рёбра", [])}
    for t in ("общий_директор", "общий_учредитель", "адрес"):
        check(errors, t in типы, "нет рёбер типа %s (есть: %s)" % (t, типы))

    revenues = json.loads((FIXTURES / "revenues_group.json").read_text())
    out = dc.score(graph, выручка_map=revenues, canon=canon)
    ids = {p["id"] for p in out.get("признаки", [])}
    check(errors, out.get("признаков_совпало", 0) >= 4,
          "признаков %r < 4 (%s)" % (out.get("признаков_совпало"), ids))
    for fid in ("общий_директор", "единый_адрес", "все_на_усн",
                "одинаковый_оквэд", "группа_над_порогом_ндс"):
        check(errors, fid in ids, "нет признака %s (есть: %s)" % (fid, ids))
    check(errors, out.get("severity") == "🔴",
          "severity %r != 🔴" % out.get("severity"))
    check(errors, "совпадающих с типовыми доводами ФНС" in out.get("оценка", ""),
          "оценка не по формулировочной дисциплине: %r" % out.get("оценка"))
    check(errors, "дробление подтверждено" not in json.dumps(out, ensure_ascii=False),
          "запрещённая формулировка «дробление подтверждено»")
    vg = out.get("выручка_группы", {})
    check(errors, vg.get("сумма") == 72000000,
          "выручка группы %r != 72000000" % vg.get("сумма"))
    check(errors, vg.get("покрыто_компаний") == "по 4 из 4 компаний",
          "покрытие %r" % vg.get("покрыто_компаний"))
    return errors


def case_healthy(ag, dc, canon):
    errors = []
    graph = ag.build_graph(make_fixture_fetcher(), "7700000099", pause=0)
    check(errors, graph.get("статус") == "ок", "граф: статус %r" % graph.get("статус"))
    check(errors, graph.get("узлов") == 1, "узлов %r != 1" % graph.get("узлов"))
    revenues = json.loads((FIXTURES / "revenues_healthy.json").read_text())
    out = dc.score(graph, выручка_map=revenues, canon=canon)
    check(errors, out.get("признаков_совпало", 9) <= 1,
          "признаков %r > 1" % out.get("признаков_совпало"))
    check(errors, out.get("severity") == "🟢",
          "severity %r != 🟢" % out.get("severity"))
    return errors


def case_no_canon(ag, dc):
    errors = []
    graph = ag.build_graph(make_fixture_fetcher(), "7700000001", pause=0)
    revenues = json.loads((FIXTURES / "revenues_group.json").read_text())
    out = dc.score(graph, выручка_map=revenues, canon=None)
    статус = out.get("выручка_группы", {}).get("статус", "")
    check(errors, "не проверено" in статус,
          "без канона выручка должна быть «не проверено», есть %r" % статус)
    return errors


def main():
    ag = load_module("affiliates_graph", ROOT / "scripts" / "affiliates_graph.py")
    dc = load_module("droblenie_check", ROOT / "scripts" / "droblenie_check.py")
    canon = dc._load_canon()
    if canon is None:
        print("FAIL: канон data/canon_ru.json не читается")
        return 1
    cases = {
        "группа-4-компании": case_group(ag, dc, canon),
        "здоровая-контрольная": case_healthy(ag, dc, canon),
        "канон-отсутствует": case_no_canon(ag, dc),
    }
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
