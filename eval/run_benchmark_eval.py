#!/usr/bin/env python3
"""
run_benchmark_eval.py — офлайн-eval бенчмарка (волна 5, блок G): категории
дефектов, маскирование ФИО, закоммиченный эталон воспроизводится из сырых
ответов и не содержит ФИО. PASS/FAIL, stdlib, CI.
"""

import copy
import importlib.util
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check(errors, cond, msg):
    if not cond:
        errors.append(msg)


def case_сравнение(b):
    errors = []
    эталон = {"записи": [{"инн": "1", "егрюл": {"огрн": "111", "наименование_полное": "ООО А",
                                               "дата_регистрации": "01.01.2020",
                                               "руководитель": b.хеш_фио("ДИРЕКТОР: X"),
                                               "дата_прекращения": None},
                          "банкротство": {"есть_запись": True, "номер_дела": "А40-1/2024",
                                          "стадия_код": "Tender"}}]}
    ok = {"егрюл": "ok", "банкротство": "ok"}

    def прогон(егрюл=None, банкротство=None, сост=ok, причины=None):
        return {"ответы": {"1": {
            "егрюл": егрюл if егрюл is not None else {
                "огрн": "111", "наименование_полное": "ООО А", "дата_регистрации": "01.01.2020",
                "руководитель": b.хеш_фио("ДИРЕКТОР: X"), "дата_прекращения": None},
            "банкротство": банкротство if банкротство is not None else {
                "дело": {"номер": "А40-1/2024", "стадия_код": "Tender"}},
            "_доступность": {k: {"состояние": v, "причина": (причины or {}).get(k)}
                             for k, v in сост.items()}}}}
    def вердикты(п):
        return {s["поле"]: s["вердикт"] for s in b.сравнить(эталон, п)}
    в = вердикты(прогон())
    check(errors, set(в.values()) == {"совпало"}, "всё совпало: %r" % в)
    в = вердикты(прогон(банкротство={"дело": {"номер": "А40-1/2024", "стадия_код": "Other"}}))
    check(errors, в["стадия_код"] == "ошибка факта", "ошибка факта: %r" % в)
    в = вердикты(прогон(банкротство={}, сост={"егрюл": "ok", "банкротство": "не проверено"},
                        причины={"банкротство": "антибот: 403"}))
    check(errors, в["номер_дела"] == "источник недоступен", "недоступен: %r" % в)
    в = вердикты(прогон(банкротство={}, сост={"егрюл": "ok", "банкротство": "не проверено"},
                        причины={"банкротство": "не покрыто: ИП"}))
    check(errors, в["номер_дела"] == "функция не поддерживается", "не покрыто: %r" % в)
    э2 = copy.deepcopy(эталон)
    э2["записи"][0]["банкротство"] = {"есть_запись": False, "номер_дела": None, "стадия_код": None}
    п = прогон(сост={"егрюл": "ok", "банкротство": "пусто"})
    п["ответы"]["1"]["банкротство"] = None
    в = {s["поле"]: s["вердикт"] for s in b.сравнить(э2, п)}
    check(errors, в["банкротство_есть_запись"] == "совпало" and в["номер_дела"] == "совпало",
          "пусто = нет записи: %r" % в)
    э2["записи"][0]["егрюл"] = None
    в = {s["поле"]: s["вердикт"] for s in b.сравнить(э2, п)}
    check(errors, в["огрн"] == "эталон не собран", "эталон не собран: %r" % в)
    # нет ответа плеча или состояния — не «совпало» (мутации из ревью Codex)
    п = прогон()
    del п["ответы"]["1"]["_доступность"]["банкротство"]
    в = вердикты(п)
    check(errors, в["номер_дела"] == "источник недоступен"
          and в["банкротство_есть_запись"] != "совпало", "без состояния: %r" % в)
    в = {s["поле"]: s["вердикт"] for s in b.сравнить(эталон, {"ответы": {}})}
    check(errors, "совпало" not in в.values(), "без ответа плеча: %r" % в)
    # неполная или битая выдача ЕФРСБ — эталон не собран, а не «записи нет»
    for raw in ({"pageData": [], "total": 1}, {"pageData": None, "total": 1},
                {"pageData": []}, {"total": 0}):
        check(errors, b.эталон_записи("1", None, raw)["банкротство"] is None,
              "неполная выдача ЕФРСБ принята: %r" % raw)
    for raw in ({"pageData": [{"ИНН": "1"}], "total": 1}, {"pageData": [None], "total": 1},
                {"pageData": [{"inn": "2"}, {"inn": "3"}], "total": 1}):
        check(errors, b.эталон_записи("1", None, raw)["банкротство"] is None,
              "повреждённая выдача ЕФРСБ принята как «записи нет»: %r" % raw)
    check(errors, b.эталон_записи("1", None, {"pageData": [], "total": 0})["банкротство"]
          == {"есть_запись": False, "номер_дела": None, "стадия_код": None},
          "полная пустая выдача — не «записи нет»")
    отчёт = b.отчёт_md({"собрано_utc": "x", "сеть": "y", "не_собрано": {"суды": "z"},
                        "записи": эталон["записи"]},
                       dict(прогон(), плечо="п", версия="1", дата_utc="d", сеть="s", тариф="t",
                            ручные_шаги="нет", секунд_всего=1), b.сравнить(эталон, прогон()))
    check(errors, "%" not in отчёт and "рейтинг" not in отчёт, "в отчёте доли/рейтинг")
    return errors


def case_привязка(b):
    """Отчёт не строится при чужом ключе, изменённой рубрике или эталоне."""
    import tempfile
    errors = []
    with tempfile.TemporaryDirectory() as td:
        эт = Path(td) / "etalon.json"
        эт.write_text(json.dumps({"ключ_id": b.ключ_id(), "записи": [], "не_собрано": {},
                                  "собрано_utc": "x", "сеть": "y"}), encoding="utf-8")
        база = {"эталон": str(эт), "эталон_sha256": b._sha(эт),
                "рубрика_sha256": b._sha(b.РУБРИКА), "ключ_id": b.ключ_id(),
                "плечо": "п", "версия": "1", "дата_utc": "d", "сеть": "s", "тариф": "t",
                "ручные_шаги": "нет", "секунд_всего": 1, "ответы": {}}
        for имя, правка in (("исходный", {}), ("чужой ключ", {"ключ_id": "0" * 16}),
                            ("рубрика", {"рубрика_sha256": "0" * 64}),
                            ("эталон", {"эталон_sha256": "0" * 64})):
            пр = Path(td) / "run.json"
            пр.write_text(json.dumps(dict(база, **правка)), encoding="utf-8")
            try:
                b.main(["bench.py", "отчёт", str(пр)])
                прошло = True
            except SystemExit:
                прошло = False
            check(errors, прошло == (имя == "исходный"), "%s: отчёт построен=%s" % (имя, прошло))
    return errors


def case_эталон(b):
    errors = []
    данные = {"rows": [{"i": "1", "g": "ДИРЕКТОР: Иванов"}]}
    b.замаскировать("егрюл", данные)
    check(errors, данные["rows"][0]["g"] == b.МАСКА, "g не замаскирован")
    д = {"pageData": [{"lastLegalCase": {"arbitrManagerFio": "Петров"}}]}
    b.замаскировать("ефрсб", д)
    check(errors, д["pageData"][0]["lastLegalCase"]["arbitrManagerFio"] == b.МАСКА,
          "управляющий не замаскирован")
    for путь in (ROOT / "benchmark" / "etalon").glob("*/etalon.json"):
        мета = json.loads(путь.read_text(encoding="utf-8"))
        for запись in мета["записи"]:
            свежая = b.эталон_из_сырых(запись["инн"], мета["сырые"], путь.parent)
            check(errors, свежая == запись, "%s: эталон не воспроизводится из сырых"
                  % запись["инн"])
            рук = (запись.get("егрюл") or {}).get("руководитель")
            check(errors, рук is None or str(рук).startswith("hmac:"),
                  "%s: руководитель не под HMAC" % запись["инн"])
        for имя, м in мета["сырые"].items():
            check(errors, not any(k.startswith("sha256_исх") for k in м),
                  "%s: опубликован хеш исходного тела" % имя)
    for f in (ROOT / "benchmark" / "etalon").glob("*/raw/*.json"):
        j = json.loads(f.read_text(encoding="utf-8"))
        for r in j.get("rows") or []:
            check(errors, r.get("g") in (None, "", b.МАСКА), "%s: ФИО в g" % f.name)
        for r in j.get("pageData") or []:
            check(errors, (r.get("lastLegalCase") or {}).get("arbitrManagerFio")
                  in (None, "", b.МАСКА), "%s: ФИО управляющего" % f.name)
    for f in (ROOT / "benchmark" / "runs").glob("*.json"):
        for inn, о in json.loads(f.read_text(encoding="utf-8"))["ответы"].items():
            рук = (о.get("егрюл") or {}).get("руководитель")
            check(errors, рук is None or str(рук).startswith("hmac:"),
                  "%s/%s: руководитель в прогоне не под HMAC" % (f.name, inn))
    return errors


def main():
    os.environ.setdefault("INN_CHECK_BENCH_KEY", "ключ-только-для-eval")
    b = load_module("bench", ROOT / "benchmark" / "bench.py")
    cases = {"категории дефектов": case_сравнение(b), "эталон и маскирование": case_эталон(b),
             "привязка отчёта к эталону, рубрике и ключу": case_привязка(b)}
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
