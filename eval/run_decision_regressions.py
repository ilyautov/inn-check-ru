#!/usr/bin/env python3
"""Офлайн-регрессии H2/H3/H4: периоды, неизвестность и фактические рекомендации."""
import copy
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "eval")]
import fin_scoring as fs
import profiles as pf
import retro_verdict as rv
import snapshot
from run_profiles_eval import fetch_clean, fin_ok


class DecisionRegressions(unittest.TestCase):
    def clean(self):
        f = fetch_clean()
        healthy = json.loads((ROOT / "eval/fixtures/healthy.json").read_text())
        f["егрюл"]["уставный_капитал"] = "10000 руб."
        f["финансы"] = healthy["финансы"]
        f["финансы"]["форма_отчётности"] = "full"  # explicit synthetic passport retained
        return f

    def states(self, f):
        return {s["id"]: s["статус"] for s in pf.extract_signals(f, fs.score(f))}

    def test_nonconsecutive_years_are_unknown(self):
        f = self.clean()
        years = f["финансы"]["отчётность_по_годам"]
        years[0]["год"], years[1]["год"] = 2025, 2023
        for y in years:
            y["строки"]["1300"] = -100
            y["прибыль_убыток"] = -100
        flags = {s["id"] for s in fs.score(f)["флаги"]}
        for sid in ("ча_отрицательные_2года", "убыток_2года", "падение_выручки"):
            self.assertNotIn(sid, flags)
            self.assertEqual(self.states(f)[sid], "не проверен")

    def test_growth_requires_previous_calendar_year(self):
        f = self.clean()
        f["финансы"]["отчётность_по_годам"][0].update(год=2025, выручка=500001)
        f["риски"]["численность_сотрудников"] = 0
        self.assertEqual(self.states(f)["транзитный_профиль"], "не проверен")

    def test_identical_duplicate_year_is_deduplicated(self):
        f = self.clean()
        years = f["финансы"]["отчётность_по_годам"]
        years.insert(0, copy.deepcopy(years[0]))
        result = fs.score(f)
        self.assertEqual(result["финансовый_профиль"]["года"], [2024, 2023])
        self.assertEqual(self.states(f)["падение_выручки"], "отсутствует")

    def test_conflicting_duplicate_year_is_not_arbitrarily_selected(self):
        f = self.clean()
        years = f["финансы"]["отчётность_по_годам"]
        duplicate = copy.deepcopy(years[0])
        duplicate["строки"]["1300"] = -100
        years.insert(0, duplicate)
        self.assertEqual(self.states(f)["ча_ниже_уставного_капитала"], "не проверен")
        self.assertTrue(fs.score(f)["финансовый_профиль"]["не_проверено"])
        result = fs.score(f)
        years.reverse()
        self.assertEqual(fs.score(f), result)

    def test_duplicate_conflict_is_detected_before_display_rounding(self):
        f = self.clean()
        years = f["финансы"]["отчётность_по_годам"]
        duplicate = copy.deepcopy(years[0])
        duplicate["строки"]["1300"] += 0.001
        years.append(duplicate)
        self.assertEqual(self.states(f)["ча_ниже_уставного_капитала"], "не проверен")

    def test_partial_staff_source_is_not_used(self):
        f = self.clean()
        f["риски"].update(статус="проверено частично", численность_сотрудников=0)
        f["финансы"]["отчётность_по_годам"][0]["выручка"] = 500001
        self.assertEqual(self.states(f)["транзитный_профиль"], "не проверен")

    def test_invalid_year_does_not_become_a_period(self):
        for year in (None, True, 2024.9, "invalid"):
            with self.subTest(year=year):
                result = fs.score([{"год": year, "выручка": 5}])
                self.assertEqual(result["статус"], "не проверено")

    def test_missing_prior_equity_is_unknown(self):
        f = self.clean()
        years = f["финансы"]["отчётность_по_годам"]
        years[0]["строки"]["1300"] = -100
        years[1]["строки"].pop("1300")
        self.assertEqual(self.states(f)["ча_отрицательные_2года"], "не проверен")
        self.assertIsNone(pf.resolve(f, fs.score(f), "отсрочка")["светофор"])

    def test_missing_capital_is_unknown(self):
        f = self.clean()
        f["егрюл"].pop("уставный_капитал")
        self.assertEqual(self.states(f)["ча_ниже_уставного_капитала"], "не проверен")
        self.assertNotEqual(pf.resolve(f, fs.score(f), "отсрочка")["светофор"], "🟢")

    def test_missing_staff_with_growth_is_unknown(self):
        f = self.clean()
        f["риски"].pop("численность_сотрудников")
        f["финансы"]["отчётность_по_годам"][0]["выручка"] = 500001
        self.assertEqual(self.states(f)["транзитный_профиль"], "не проверен")
        self.assertNotEqual(pf.resolve(f, fs.score(f), "клиент_115фз")["светофор"], "🟢")

    def test_fns_zero_staff_and_year_are_preserved(self):
        f = self.clean()
        f["риски"].pop("численность_сотрудников")
        f["дампы_фнс"] = {"численность_сотрудников": 0, "численность_год": 2024}
        f["финансы"]["отчётность_по_годам"][0]["выручка"] = 500001
        result = fs.score(f)
        p = result["финансовый_профиль"]
        self.assertEqual(p["численность"], 0)
        self.assertEqual(p.get("численность_год"), 2024)
        self.assertEqual(p.get("численность_источник"), "дампы_фнс")
        self.assertEqual(self.states(f)["транзитный_профиль"], "найден")
        self.assertEqual(pf.resolve(f, result, "клиент_115фз")["светофор"], "🔴")

    def test_staff_other_year_cannot_prove_transit(self):
        f = self.clean()
        f["риски"].pop("численность_сотрудников")
        f["дампы_фнс"] = {"численность_сотрудников": 0, "численность_год": 2022}
        f["финансы"]["отчётность_по_годам"][0]["выручка"] = 500001
        self.assertEqual(self.states(f)["транзитный_профиль"], "не проверен")

    def test_actual_staff_year_prevents_stale_transit_before_and_after_snapshot(self):
        for year in (2022, 2024):
            with self.subTest(year=year):
                f = self.clean()
                f["риски"].update(численность_сотрудников=0, среднесписочная_за_год=year)
                f["финансы"]["отчётность_по_годам"][0]["выручка"] = 500001
                restored, _ = rv.восстановить(snapshot.fingerprint(f))
                self.assertEqual(restored["риски"]["среднесписочная_за_год"], year)
                for data in (f, restored):
                    result = fs.score(data)
                    self.assertEqual(result["финансовый_профиль"]["численность_год"], year)
                    expected = "найден" if year == 2024 else "не проверен"
                    self.assertEqual(self.states(data)["транзитный_профиль"], expected)
                    color = pf.resolve(data, result, "клиент_115фз")["светофор"]
                    if year == 2022:
                        self.assertNotEqual(color, "🔴")
                    else:
                        self.assertEqual(color, "🔴")

    def test_conflicting_staff_year_aliases_are_unknown(self):
        for canonical, legacy in ((2022, 2024), (2024, 2022)):
            with self.subTest(canonical=canonical, legacy=legacy):
                f = self.clean()
                f["риски"].update(численность_сотрудников=0,
                                  среднесписочная_за_год=canonical, численность_год=legacy)
                f["финансы"]["отчётность_по_годам"][0]["выручка"] = 500001
                restored, _ = rv.восстановить(snapshot.fingerprint(f))
                for data in (f, restored):
                    self.assertEqual(self.states(data)["транзитный_профиль"], "не проверен")
                    self.assertNotEqual(pf.resolve(data, fs.score(data), "клиент_115фз")["светофор"], "🔴")

    def test_matching_staff_year_aliases_are_accepted(self):
        f = self.clean()
        f["риски"].update(численность_сотрудников=0,
                          среднесписочная_за_год="2024", численность_год=2024)
        f["финансы"]["отчётность_по_годам"][0]["выручка"] = 500001
        self.assertEqual(self.states(f)["транзитный_профиль"], "найден")

    def test_invalid_explicit_staff_year_is_unknown(self):
        for key in ("среднесписочная_за_год", "численность_год"):
            for year in (True, "invalid", 2024.9):
                with self.subTest(key=key, year=year):
                    f = self.clean()
                    f["риски"].update(численность_сотрудников=0)
                    f["риски"][key] = year
                    f["финансы"]["отчётность_по_годам"][0]["выручка"] = 500001
                    self.assertEqual(self.states(f)["транзитный_профиль"], "не проверен")

    def test_unverified_staff_dump_is_not_used(self):
        f = self.clean()
        f["риски"].pop("численность_сотрудников")
        f["дампы_фнс"] = {"численность_сотрудников": 0, "численность_год": 2024}
        f["_доступность"]["дампы_фнс"]["состояние"] = "не проверено"
        f["финансы"]["отчётность_по_годам"][0]["выручка"] = 500001
        self.assertEqual(self.states(f)["транзитный_профиль"], "не проверен")

    def test_each_financial_rule_has_explicit_evaluation(self):
        result = fs.score(self.clean())
        evaluations = result.get("оценки_сигналов", {})
        self.assertEqual(len(evaluations), 7)
        self.assertTrue(all(r["статус"] == "отсутствует" for r in evaluations.values()))
        self.assertTrue(all(r.get("зависимости") for r in evaluations.values()))

    def test_automated_required_unknown_prevents_green(self):
        f = self.clean()
        f["риски"].pop("массовый_адрес")
        result = pf.resolve(f, fs.score(f), "отсрочка")
        self.assertIsNone(result["светофор"])
        self.assertIn("массовый_адрес", result.get("не_проверено_обязательные_сигналы", []))

    def test_unknown_red_override_prevents_yellow(self):
        f = self.clean()
        f["риски"].pop("массовый_руководитель")
        f["риски"]["массовый_адрес"] = True
        result = pf.resolve(f, fs.score(f), "клиент_115фз")
        self.assertIsNone(result["светофор"])
        self.assertFalse(result["проверка_состоялась"])
        self.assertNotIn("не массовый", result["рекомендация"])

    def test_found_red_survives_unknown_red_override(self):
        f = self.clean()
        f["риски"].pop("массовый_руководитель")
        f["санкции"]["совпадения"] = ["test"]
        from run_profiles_eval import synthetic_verified
        f = synthetic_verified(f)  # new synthetic observation, not a stale old absence
        result = pf.resolve(f, fs.score(f), "клиент_115фз")
        self.assertEqual(result["светофор"], "🔴")
        self.assertIn("массовый_руководитель", result["рекомендация"])

    def test_missing_optional_signal_does_not_block_profile(self):
        f = self.clean()
        f["_доступность"].pop("еркнм")
        self.assertEqual(pf.resolve(f, fs.score(f), "отсрочка")["светофор"], "🟢")

    def test_inapplicable_financial_fields_do_not_block_ip(self):
        f = self.clean()
        f["тип"] = "ип"
        f["инн"] = "504110181262"
        f["финансы"] = None
        f["риски"].pop("массовый_руководитель")
        from run_profiles_eval import synthetic_verified
        f = synthetic_verified(f)
        result = pf.resolve(f, None, "отсрочка")
        self.assertEqual(result["светофор"], "🟢")

    def test_green_recommendation_does_not_invent_staff(self):
        f = self.clean()
        f["риски"].pop("численность_сотрудников")
        result = pf.resolve(f, fs.score(f), "предоплата")
        self.assertNotIn("штат и активы видны", result["рекомендация"])
        self.assertNotIn("штат виден", result["рекомендация"])

    def test_yellow_recommendation_does_not_invent_negative_equity(self):
        f = self.clean()
        result = pf.resolve(f, fin_ok(f, "текущая_ликвидность"), "доля")
        self.assertNotIn("отрицательные чистые активы", result["рекомендация"])
        self.assertIn("аргумент для переговоров", result["рекомендация"])

    def test_cli_emits_unknown_without_absolute_recommendation(self):
        f = self.clean()
        f["риски"].pop("массовый_руководитель")
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fetch.json"
            path.write_text(json.dumps(f))
            p = subprocess.run([sys.executable, str(ROOT / "scripts/profiles.py"),
                                "--fetch", str(path), "--профиль", "клиент_115фз"],
                               text=True, capture_output=True, check=False)
        self.assertEqual(p.returncode, 0, p.stderr)
        result = json.loads(p.stdout)
        self.assertIsNone(result["светофор"])
        self.assertNotIn("не массовый", result["рекомендация"])


if __name__ == "__main__":
    unittest.main()
