#!/usr/bin/env python3
"""Офлайн-контракт происхождения; синтетика проверяет механизм, не реестры."""
import copy
import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.find_spec("evidence_contract")
assert SPEC is not None, "missing assigned module scripts/evidence_contract.py"
assert Path(SPEC.origin).resolve() == ROOT / "scripts/evidence_contract.py"
from evidence_contract import enrich, validate_evidence

NOW = "2026-10-16T09:00:00Z"
INN = "7707083893"


def claims(out, field):
    return [c for c in out["_evidence"]["claims"].values() if c["field"] == field]


def raw_fixture(value=0, **mapping):
    body = json.dumps({"records": [{"inn": INN, "staff": value}]}).encode()
    ref = {"response_id": "r1", "locator": "/records/0/staff",
           "subject_locator": "/records/0/inn", "transformation": "integer_v1"}
    ref.update(mapping)
    return {"responses": {"r1": {"source_id": "дампы_фнс", "subject_inn": INN,
             "request": {"subject_inn": INN}, "body": body,
             "sha256": hashlib.sha256(body).hexdigest(), "retrieved_at": NOW}},
            "fields": {"дампы_фнс.численность_сотрудников": [ref]}}


class EvidenceTests(unittest.TestCase):
    def test_zero_is_fact_null_missing_input_immutable(self):
        fetch = {"инн": INN, "дампы_фнс": {"численность_сотрудников": 0},
                 "_доступность": {"дампы_фнс": {"состояние": "ok"}}}
        original = copy.deepcopy(fetch)
        out = enrich(fetch, now=NOW)
        c = claims(out, "дампы_фнс.численность_сотрудников")[0]
        self.assertEqual((c["value"], c["knowledge"]), (0, "known"))
        self.assertEqual(c["raw_ref"]["availability"], "raw_unavailable")
        self.assertIsNone(c["raw_ref"]["sha256"])
        self.assertEqual(fetch, original)
        self.assertEqual(validate_evidence(out), [])
        fetch["дампы_фнс"]["численность_сотрудников"] = None
        c = claims(enrich(fetch, now=NOW), "дампы_фнс.численность_сотрудников")[0]
        self.assertEqual(c["knowledge"], "unknown")

    def test_recorded_bytes_checked_locator_transformation_and_old_pack(self):
        from evidence_pack import записать_пакет, проверить
        fetch = {"инн": INN, "дампы_фнс": {"численность_сотрудников": 0}}
        refs = raw_fixture("0")
        original = copy.deepcopy(refs)
        out = enrich(fetch, now=NOW, raw_refs=refs)
        c = claims(out, "дампы_фнс.численность_сотрудников")[0]
        self.assertEqual(c["raw_ref"]["availability"], "available")
        self.assertEqual(c["raw_ref"]["sha256"], refs["responses"]["r1"]["sha256"])
        self.assertEqual(refs, original)
        with tempfile.TemporaryDirectory() as folder:
            записать_пакет(folder, INN, fetch, [{"источник": "дампы_фнс",
                "данные": refs["responses"]["r1"]["body"]}])
            self.assertTrue(проверить(folder)["годен"])
            index = json.loads((Path(folder) / "raw/index.json").read_text())
            saved = (Path(folder) / index[0]["файл"]).read_bytes()
            refs["responses"]["r1"].update(body=saved, sha256=index[0]["sha256"])
            recorded = enrich(fetch, now=NOW, raw_refs=refs)
            self.assertEqual(claims(recorded, "дампы_фнс.численность_сотрудников")[0]["value"], 0)
        self.assertEqual(validate_evidence(out), [])

    def test_corrupt_raw_mapping_rejected(self):
        fetch = {"инн": INN, "дампы_фнс": {"численность_сотрудников": 0}}
        for mutation, code in (
            (lambda r: r["responses"]["r1"].update(sha256="0" * 64), "raw_hash_mismatch"),
            (lambda r: r["responses"]["r1"].update(subject_inn="1234567890"), "foreign_subject"),
            (lambda r: r["fields"]["дампы_фнс.численность_сотрудников"][0].update(locator="/absent"), "unmapped_locator"),
            (lambda r: r["fields"]["дампы_фнс.численность_сотрудников"][0].update(subject_locator="/records/1/inn"), "foreign_subject"),
            (lambda r: r["responses"]["r1"].update(source_id="егрюл"), "source_mismatch"),
        ):
            with self.subTest(code=code):
                refs = raw_fixture()
                mutation(refs)
                with self.assertRaisesRegex(ValueError, code):
                    enrich(fetch, now=NOW, raw_refs=refs)
        with self.assertRaisesRegex(ValueError, "transformation_mismatch"):
            enrich(fetch, now=NOW, raw_refs=raw_fixture(8))

    def test_foreign_record_even_when_response_metadata_matches(self):
        refs = raw_fixture()
        body = json.dumps({"records": [{"inn": "1234567890", "staff": 0}]}).encode()
        refs["responses"]["r1"].update(body=body, sha256=hashlib.sha256(body).hexdigest())
        with self.assertRaisesRegex(ValueError, "foreign_subject"):
            enrich({"инн": INN, "дампы_фнс": {"численность_сотрудников": 0}}, now=NOW, raw_refs=refs)

    def test_profile_and_financial_leaf_matrix_no_silent_omissions(self):
        profiles = json.loads((ROOT / "data/profiles_ru.json").read_text())
        expected = {p for s in profiles["сигналы"].values() for p in s["откуда"]}
        out = enrich({"инн": INN}, now=NOW)
        seen = {c["field"] for c in out["_evidence"]["claims"].values()}
        self.assertLessEqual(expected, seen)
        self.assertTrue(all(c["knowledge"] == "unknown" for c in out["_evidence"]["claims"].values()))
        fetch = {"инн": INN, "риски": {"численность_сотрудников": 3,
                 "среднесписочная_за_год": 2025}, "финансы": {"отчётность_по_годам": [
                 {"год": 2025, "строки": {"2110": 400, "1300": 0}},
                 {"год": 2024, "выручка": 100}]}}
        out = enrich(fetch, now=NOW)
        self.assertEqual(claims(out, "финансы.отчётность_по_годам.2025.строки.2110")[0]["value"], 400)
        self.assertEqual(claims(out, "финансы.отчётность_по_годам.2024.строки.2110")[0]["value"], 100)
        self.assertEqual(claims(out, "риски.численность_сотрудников")[0]["period"]["end"], "2025-12-31")

    def test_unavailable_source_does_not_prove_absence(self):
        for value in (False, [], "", None):
            fetch = {"инн": INN, "банкротство": {"процедура": value},
                     "_доступность": {"банкротство": {"состояние": "не проверено"}}}
            self.assertEqual(claims(enrich(fetch, now=NOW), "банкротство.процедура")[0]["knowledge"], "unknown")
        out = enrich({"инн": INN, "банкротство": {"процедура": False}}, now=NOW)
        self.assertEqual(claims(out, "банкротство.процедура")[0]["reason_code"], "coverage_incomplete")

    def test_validation_dangling_cycles_subject_and_hash(self):
        out = enrich({"инн": INN}, now=NOW)
        ids = list(out["_evidence"]["claims"])
        a, b = [out["_evidence"]["claims"][i] for i in ids[:2]]
        a["depends_on"] = [ids[1], "missing"]
        b["depends_on"] = [ids[0]]
        b["subject_inn"] = "foreign"
        b["raw_ref"] = {"response_id": "r", "sha256": "bad", "locator": "/x", "availability": "available"}
        codes = {e["code"] for e in validate_evidence(out)}
        self.assertLessEqual({"dangling_reference", "dependency_cycle", "foreign_subject", "invalid_raw_ref"}, codes)

    def test_unknown_mappings_cannot_be_silently_dropped(self):
        refs = raw_fixture()
        refs["fields"]["nonexistent.path"] = refs["fields"]["дампы_фнс.численность_сотрудников"]
        with self.assertRaisesRegex(ValueError, "unsupported_field_mapping"):
            enrich({"инн": INN, "дампы_фнс": {"численность_сотрудников": 0}}, now=NOW, raw_refs=refs)

    def test_numeric_staff_type_and_conflicting_years(self):
        for value in (True, -1, "not a number"):
            out = enrich({"инн": INN, "риски": {"численность_сотрудников": value}}, now=NOW)
            self.assertEqual(claims(out, "риски.численность_сотрудников")[0]["knowledge"], "unknown")
        fetch = {"инн": INN, "риски": {"численность_сотрудников": 0,
            "среднесписочная_за_год": 2025, "численность_год": 2023},
            "финансы": {"отчётность_по_годам": [{"год": 2025, "выручка": 500}, {"год": 2024, "выручка": 100}]}}
        out = enrich(fetch, now=NOW)
        self.assertEqual(claims(out, "fin.транзитный_профиль")[0]["reason_code"], "period_mismatch")
        fetch["риски"]["численность_год"] = 2025
        fetch["риски"]["среднесписочная_за_год"] = 2023
        out = enrich(fetch, now=NOW)
        self.assertEqual(claims(out, "fin.транзитный_профиль")[0]["reason_code"], "period_mismatch")

    def test_same_canonical_field_from_explicit_second_provider(self):
        fetch = {"инн": INN, "дампы_фнс": {"численность_сотрудников": 0}}
        refs = raw_fixture()
        refs["responses"]["mirror"] = copy.deepcopy(refs["responses"]["r1"])
        refs["responses"]["mirror"]["source_id"] = "агрегатор"
        m = copy.deepcopy(refs["fields"]["дампы_фнс.численность_сотрудников"][0])
        m.update(response_id="mirror", source_id="агрегатор", relation="mirror")
        refs["fields"]["дампы_фнс.численность_сотрудников"].append(m)
        out = enrich(fetch, now=NOW, raw_refs=refs)
        cs = claims(out, "дампы_фнс.численность_сотрудников")
        self.assertEqual({c["source_id"] for c in cs}, {"дампы_фнс", "агрегатор"})
        self.assertEqual({c["origin_family"] for c in cs}, {"fns_staff"})
        self.assertEqual(out["_evidence"]["conflicts"], [])

    def test_all_profile_paths_accounted_known_or_authoritative_unknown(self):
        policy = json.loads((ROOT / "data/evidence_policy.json").read_text())
        fetch = {"инн": INN}
        for field, spec in policy["fields"].items():
            if spec["derived"] or field.startswith("санкции."):
                continue
            cur = fetch
            parts = field.split(".")
            for part in parts[:-1]:
                cur = cur.setdefault(part, {})
            cur[parts[-1]] = 2025 if field.endswith(("численность_год", "среднесписочная_за_год", ".год")) else 3 if field.endswith("численность_сотрудников") else "наблюдаемый факт"
        out = enrich(fetch, now=NOW)
        profiles = json.loads((ROOT / "data/profiles_ru.json").read_text())
        for field in {p for s in profiles["сигналы"].values() for p in s["откуда"]}:
            cs = claims(out, field)
            self.assertTrue(cs, field)
            expected = "unknown" if field.startswith(("fin.", "санкции.")) else "known"
            self.assertEqual(cs[0]["knowledge"], expected, field)

    def test_validate_malformed_graph_never_raises(self):
        out = enrich({"инн": INN}, now=NOW)
        c = next(iter(out["_evidence"]["claims"].values()))
        c["raw_ref"] = None
        c["depends_on"] = ["x"] * 33
        self.assertTrue(validate_evidence(out))


    def test_zero_count_and_string_denials_require_search_scope(self):
        for field, value in (("фссп.производств", 0), ("банкротство.процедура", "нет")):
            source, key = field.split(".")
            out = enrich({"инн": INN, source: {key: value}}, now=NOW)
            self.assertEqual(claims(out, field)[0]["knowledge"], "unknown")
            self.assertEqual(claims(out, field)[0]["reason_code"], "coverage_incomplete")


    def test_locator_to_foreign_sibling_cannot_borrow_subject(self):
        refs = raw_fixture()
        body = json.dumps({"records": [{"inn": INN, "staff": 0}, {"inn": "1234567890", "staff": 0}]}).encode()
        refs["responses"]["r1"].update(body=body, sha256=hashlib.sha256(body).hexdigest())
        refs["fields"]["дампы_фнс.численность_сотрудников"][0]["locator"] = "/records/1/staff"
        with self.assertRaisesRegex(ValueError, "foreign_subject"):
            enrich({"инн": INN, "дампы_фнс": {"численность_сотрудников": 0}}, now=NOW, raw_refs=refs)



class TimeTests(unittest.TestCase):
    def test_reload_keeps_period_dates_distinct(self):
        fetch = {"инн": INN, "дампы_фнс": {"численность_сотрудников": 3, "год": 2025,
                 "дата_выгрузки": "2027-02-01"}}
        first = claims(enrich(fetch, now=NOW), "дампы_фнс.численность_сотрудников")[0]
        second = claims(enrich(fetch, now="2026-11-16T09:00:00Z"), "дампы_фнс.численность_сотрудников")[0]
        self.assertEqual(first["period"], second["period"])
        self.assertNotEqual(first["retrieved_at"], second["retrieved_at"])
        self.assertIsNone(first["published_at"])
        self.assertIsNone(first["effective_at"])
        self.assertIsNone(first["valid_until"])
        self.assertEqual(enrich(fetch, now=NOW)["дампы_фнс"]["дата_выгрузки"], "2027-02-01")

    def test_utc_ttl_exact_boundary_and_missing_date(self):
        fetch = {"инн": INN, "санкции": {"совпадения": []}}
        body = json.dumps({"inn": INN, "hits": [], "asof": "2026-10-09T12:00:00+03:00"}).encode()
        refs = {"responses": {"r": {"source_id": "санкции", "subject_inn": INN,
            "request": {"subject_inn": INN}, "body": body, "sha256": hashlib.sha256(body).hexdigest()}},
            "fields": {"санкции.совпадения": [{"response_id": "r", "locator": "/hits",
                "subject_locator": "/inn", "date_locators": {"effective_at": "/asof"}, "effective_at": "2026-10-09T12:00:00+03:00",
                "search_scope": {"subject_inn": INN, "query": INN}, "coverage_complete": True}]}}
        before = claims(enrich(fetch, now="2026-10-16T08:59:59Z", raw_refs=refs), "санкции.совпадения")[0]
        at = claims(enrich(fetch, now=NOW, raw_refs=refs), "санкции.совпадения")[0]
        self.assertEqual(before["knowledge"], "known")
        self.assertEqual((at["knowledge"], at["reason_code"]), ("unknown", "stale"))
        self.assertEqual(at["valid_until"], NOW)
        del refs["fields"]["санкции.совпадения"][0]["effective_at"]
        missing = claims(enrich(fetch, now=NOW, raw_refs=refs), "санкции.совпадения")[0]
        self.assertEqual(missing["reason_code"], "revision_unknown")
        self.assertIsNone(missing["valid_until"])

    def test_conflicts_order_invariant_mirrors_and_history(self):
        fetch = {"инн": INN, "дампы_фнс": {"численность_сотрудников": 0}}
        refs = raw_fixture(0, period={"start": "2025-01-01", "end": "2025-12-31"})
        second = copy.deepcopy(refs["responses"]["r1"])
        body = json.dumps({"records": [{"inn": INN, "staff": 3}]}).encode()
        second.update(body=body, sha256=hashlib.sha256(body).hexdigest())
        refs["responses"]["r2"] = second
        mapping = copy.deepcopy(refs["fields"]["дампы_фнс.численность_сотрудников"][0])
        mapping.update(response_id="r2", candidate=True)
        refs["fields"]["дампы_фнс.численность_сотрудников"].append(mapping)
        first = enrich(fetch, now=NOW, raw_refs=refs)
        refs["fields"]["дампы_фнс.численность_сотрудников"].reverse()
        second = enrich(fetch, now=NOW, raw_refs=refs)
        self.assertEqual(first["_evidence"], second["_evidence"])
        self.assertEqual(len(first["_evidence"]["conflicts"]), 1)
        conflict = first["_evidence"]["conflicts"][0]
        self.assertIsNone(conflict["selected_claim_id"])
        self.assertEqual(len(conflict["candidate_claim_ids"]), 2)
        # Equal mirror values retain the same known origin, never extra independent families.
        refs["responses"]["r2"] = copy.deepcopy(refs["responses"]["r1"])
        mirrors = claims(enrich(fetch, now=NOW, raw_refs=refs), "дампы_фнс.численность_сотрудников")
        self.assertEqual({c["origin_family"] for c in mirrors}, {"fns_staff"})

    def test_historical_positive_survives_missing_current_source(self):
        fetch = {"инн": INN, "банкротство": {"процедура": True},
                 "_доступность": {"банкротство": {"состояние": "не проверено"}}}
        body = json.dumps({"inn": INN, "procedure": True, "asof": "2020-01-01T00:00:00Z"}).encode()
        refs = {"responses": {"old": {"source_id": "банкротство", "subject_inn": INN,
            "request": {"subject_inn": INN}, "body": body, "sha256": hashlib.sha256(body).hexdigest()}},
            "fields": {"банкротство.процедура": [{"response_id": "old", "locator": "/procedure",
                "subject_locator": "/inn", "date_locators": {"effective_at": "/asof"}, "effective_at": "2020-01-01T00:00:00Z"}]}}
        out = enrich(fetch, now=NOW, raw_refs=refs)
        self.assertEqual(claims(out, "банкротство.процедура")[0]["knowledge"], "known")
        self.assertEqual(out["_evidence"]["applicability"]["source_coverage"]["банкротство"]["state"], "unknown")

    def test_form_segments_have_explicit_rules_and_denominators(self):
        for form, subject in (("full", "legal_entity"), ("simplified", "legal_entity"),
                              ("ip", "ip"), ("bank", "bank"), ("npo", "npo"), ("restricted", "legal_entity")):
            with self.subTest(form=form):
                fetch = {"инн": "500100732259" if form == "ip" else INN, "_reporting": {"subject_type": subject, "reporting_form": form,
                    "form_version": "66н", "form_evidence_ids": []}}
                app = enrich(fetch, now=NOW)["_evidence"]["applicability"]
                self.assertEqual(app["reporting_form"], form)
                financial = [r for key, r in app["rules"].items() if key.startswith("fin.")]
                self.assertTrue(financial)
                if form == "ip":
                    self.assertTrue(all(r["status"] == "not_applicable" for r in financial))
                else:
                    self.assertTrue(all(r["status"] == "unknown" for r in financial))
                self.assertTrue(all(r["reason_code"] for r in financial))
        app = enrich({"инн": INN, "егрюл": {"оквэд": "64.19"}}, now=NOW)["_evidence"]["applicability"]
        self.assertIsNone(app["reporting_form"])

    def test_period_mismatch_has_complete_leaf_dependencies(self):
        fetch = {"инн": INN, "риски": {"численность_сотрудников": 0, "среднесписочная_за_год": 2023},
                 "финансы": {"отчётность_по_годам": [{"год": 2025, "выручка": 500}, {"год": 2024, "выручка": 100}]}}
        out = enrich(fetch, now=NOW)
        transit = claims(out, "fin.транзитный_профиль")[0]
        self.assertEqual(transit["reason_code"], "period_mismatch")
        deps = [out["_evidence"]["claims"][i] for i in transit["depends_on"]]
        self.assertLessEqual({"финансы.отчётность_по_годам.2025.строки.2110", "финансы.отчётность_по_годам.2024.строки.2110", "риски.численность_сотрудников", "риски.среднесписочная_за_год"}, {c["field"] for c in deps})

    def test_checked_form_passport_selects_rules_without_inventing_missing_rows(self):
        for form in ("full", "simplified"):
            fetch = {"инн": INN, "тип": "юрлицо", "финансы": {"форма_отчётности": form}}
            body = json.dumps({"inn": INN, "form": form}).encode()
            refs = {"responses": {"r": {"source_id": "финансы", "subject_inn": INN,
                "request": {"subject_inn": INN}, "body": body, "sha256": hashlib.sha256(body).hexdigest()}},
                "fields": {"финансы.форма_отчётности": [{"response_id": "r", "locator": "/form", "subject_locator": "/inn"}]}}
            out = enrich(fetch, now=NOW, raw_refs=refs)
            app = out["_evidence"]["applicability"]
            self.assertEqual(len(app["form_evidence_ids"]), 1)
            self.assertEqual(app["rules"]["fin.падение_выручки"]["status"], "applicable" if form == "full" else "unknown")
            self.assertEqual(app["rules"]["fin.текущая_ликвидность"]["status"], "applicable" if form == "full" else "unknown")
            self.assertEqual(claims(out, "fin.падение_выручки")[0]["knowledge"], "unknown")

    def test_dates_without_checked_locator_do_not_prove_publication(self):
        refs = raw_fixture(published_at="2000-01-01T00:00:00Z")
        out = enrich({"инн": INN, "дампы_фнс": {"численность_сотрудников": 0}}, now=NOW, raw_refs=refs)
        self.assertIsNone(claims(out, "дампы_фнс.численность_сотрудников")[0]["published_at"])


    def test_simplified_disclosed_rows_do_not_hide_red_capital(self):
        fetch = {"инн": INN, "финансы": {"форма_отчётности": "simplified",
            "отчётность_по_годам": [{"год": 2025, "капитал": -10}, {"год": 2024, "капитал": -20}]}}
        body = json.dumps({"inn": INN, "form": "simplified"}).encode()
        refs = {"responses": {"r": {"source_id": "финансы", "subject_inn": INN,
            "request": {"subject_inn": INN}, "body": body, "sha256": hashlib.sha256(body).hexdigest()}},
            "fields": {"финансы.форма_отчётности": [{"response_id": "r", "locator": "/form", "subject_locator": "/inn"}]}}
        out = enrich(fetch, now=NOW, raw_refs=refs)
        rules = out["_evidence"]["applicability"]["rules"]
        self.assertEqual(rules["fin.ча_отрицательные_2года"]["status"], "applicable")
        self.assertEqual(rules["fin.текущая_ликвидность"]["status"], "unknown")
        self.assertEqual(claims(out, "fin.ча_отрицательные_2года")[0]["knowledge"], "unknown")
        self.assertEqual(claims(out, "финансы.отчётность_по_годам.2025.строки.1300")[0]["value"], -10)

    def test_subject_metadata_cannot_make_company_ip(self):
        out = enrich({"инн": INN, "_reporting": {"subject_type": "ip", "reporting_form": "ip"}}, now=NOW)
        self.assertEqual(out["_evidence"]["applicability"]["subject_type"], "legal_entity")
        self.assertEqual(out["_evidence"]["applicability"]["rules"]["fin.убыток_2года"]["status"], "unknown")

    def test_manual_observation_never_inherits_government_lineage(self):
        refs = raw_fixture(relation="manual_observation")
        out = enrich({"инн": INN, "дампы_фнс": {"численность_сотрудников": 0}}, now=NOW, raw_refs=refs)
        c = claims(out, "дампы_фнс.численность_сотрудников")[0]
        self.assertIsNone(c["origin_family"])
        self.assertEqual(c["relation"], "manual_observation")


    def test_reenrich_retains_first_observed_at_and_period(self):
        first = enrich({"инн": INN, "дампы_фнс": {"численность_сотрудников": 0, "численность_год": 2025}}, now=NOW)
        second = enrich(first, now="2026-11-16T09:00:00Z")
        a = claims(first, "дампы_фнс.численность_сотрудников")[0]
        b = claims(second, "дампы_фнс.численность_сотрудников")[0]
        self.assertEqual(a["first_observed_at"], b["first_observed_at"])
        self.assertEqual(a["period"], b["period"])
        self.assertNotEqual(a["retrieved_at"], b["retrieved_at"])

    def test_distinct_annual_periods_do_not_conflict(self):
        refs = raw_fixture(0, period={"start": "2025-01-01", "end": "2025-12-31"})
        refs["responses"]["r2"] = copy.deepcopy(refs["responses"]["r1"])
        body = json.dumps({"records": [{"inn": INN, "staff": 3}]}).encode()
        refs["responses"]["r2"].update(body=body, sha256=hashlib.sha256(body).hexdigest())
        refs["fields"]["дампы_фнс.численность_сотрудников"].append({"response_id": "r2", "locator": "/records/0/staff", "subject_locator": "/records/0/inn", "period": {"start": "2024-01-01", "end": "2024-12-31"}, "candidate": True})
        out = enrich({"инн": INN, "дампы_фнс": {"численность_сотрудников": 0}}, now=NOW, raw_refs=refs)
        self.assertEqual(out["_evidence"]["conflicts"], [])
        self.assertEqual(len(claims(out, "дампы_фнс.численность_сотрудников")), 2)

    def test_restricted_publication_remains_not_disclosed(self):
        out = enrich({"инн": INN, "_reporting": {"reporting_form": "restricted"}}, now=NOW)
        self.assertEqual(out["_evidence"]["applicability"]["rules"]["fin.убыток_2года"]["reason_code"], "not_disclosed")



class ReviewRegressionTests(unittest.TestCase):
    def refs(self, source, field, body, **mapping):
        encoded = json.dumps(body).encode()
        return {"responses": {"r": {"source_id": source, "subject_inn": INN,
            "request": {"subject_inn": INN}, "body": encoded,
            "sha256": hashlib.sha256(encoded).hexdigest()}},
            "fields": {field: [{"response_id": "r", **mapping}]}}

    def test_foreign_sibling_date_cannot_freshen_old_negative(self):
        field = "санкции.совпадения"
        fetch = {"инн": INN, "санкции": {"совпадения": []}}
        refs = self.refs("санкции", field, {"records": [
            {"inn": INN, "hits": [], "asof": "2020-01-01T00:00:00Z"},
            {"inn": "1234567890", "asof": NOW}]},
            locator="/records/0/hits", subject_locator="/records/0/inn",
            date_locators={"effective_at": "/records/1/asof"}, effective_at=NOW,
            coverage_complete=True, search_scope={"subject_inn": INN, "query": INN})
        with self.assertRaisesRegex(ValueError, "foreign_subject"):
            enrich(fetch, now=NOW, raw_refs=refs)
        mapping = refs["fields"][field][0]
        mapping.update(date_locators={"effective_at": "/records/0/asof"},
                       effective_at="2020-01-01T00:00:00Z")
        c = claims(enrich(fetch, now=NOW, raw_refs=refs), field)[0]
        self.assertEqual((c["knowledge"], c["reason_code"]), ("unknown", "stale"))

    def test_identity_staff_string_is_replayable_and_not_typed_known(self):
        from evidence_contract import _pointer, _transform
        refs = raw_fixture("3", transformation="identity_v1")
        fetch = {"инн": INN, "дампы_фнс": {"численность_сотрудников": "3"}}
        c = claims(enrich(fetch, now=NOW, raw_refs=refs), "дампы_фнс.численность_сотрудников")[0]
        raw = json.loads(refs["responses"]["r1"]["body"])
        replayed = _transform(_pointer(raw, c["raw_ref"]["locator"]), c["transformation"])
        self.assertEqual((type(c["value"]), c["value"]), (type(replayed), replayed))
        self.assertEqual((c["knowledge"], c["reason_code"]), ("unknown", "schema_invalid"))
        refs["fields"][c["field"]][0]["transformation"] = "integer_v1"
        c = claims(enrich(fetch, now=NOW, raw_refs=refs), c["field"])[0]
        self.assertEqual((c["value"], c["knowledge"], c["transformation"]), (3, "known", "integer_v1"))

    def test_financial_candidates_cannot_bypass_field_type_or_simplified_disclosure(self):
        field = "финансы.отчётность_по_годам.2025.строки.1300"
        for invalid in (True, False, "not money", [], {}):
            with self.subTest(invalid=invalid):
                fetch = {"инн": INN, "финансы": {"форма_отчётности": "simplified",
                    "отчётность_по_годам": [{"год": 2025, "капитал": 100},
                                              {"год": 2024, "капитал": 100}]}}
                refs = self.refs("финансы", field, {"inn": INN, "capital": invalid,
                    "form": "simplified"}, locator="/capital", subject_locator="/inn", candidate=True)
                refs["fields"]["финансы.форма_отчётности"] = [
                    {"response_id": "r", "locator": "/form", "subject_locator": "/inn"}]
                out = enrich(fetch, now=NOW, raw_refs=refs)
                c = claims(out, field)[0]
                self.assertEqual((c["knowledge"], c["reason_code"]), ("unknown", "schema_invalid"))
                self.assertEqual(type(c["value"]), type(invalid))
                self.assertEqual(out["_evidence"]["applicability"]["rules"]["fin.ча_отрицательные_2года"]["status"], "unknown")

    def test_inconsistent_duplicate_coverage_is_rejected_in_both_orders(self):
        field = "санкции.совпадения"
        refs = self.refs("санкции", field, {"inn": INN, "hits": [], "asof": NOW},
            locator="/hits", subject_locator="/inn", effective_at=NOW,
            date_locators={"effective_at": "/asof"}, coverage_complete=True,
            search_scope={"subject_inn": INN, "query": INN})
        refs["fields"][field].append(dict(refs["fields"][field][0], coverage_complete=False))
        for timestamp in (NOW, "2026-10-23T09:00:00Z"):
            for _ in range(2):
                with self.assertRaisesRegex(ValueError, "inconsistent_duplicate_claim"):
                    enrich({"инн": INN, "санкции": {"совпадения": []}}, now=timestamp, raw_refs=refs)
                refs["fields"][field].reverse()

    def test_financial_inconsistent_duplicate_metadata_is_rejected(self):
        field = "финансы.отчётность_по_годам.2025.строки.1300"
        refs = self.refs("финансы", field, {"inn": INN, "capital": 100},
                         locator="/capital", subject_locator="/inn")
        refs["fields"][field].append(dict(refs["fields"][field][0], relation="manual_observation"))
        fetch = {"инн": INN, "финансы": {"отчётность_по_годам": [{"год": 2025, "капитал": 100}]}}
        for _ in range(2):
            with self.assertRaisesRegex(ValueError, "inconsistent_duplicate_claim"):
                enrich(fetch, now=NOW, raw_refs=refs)
            refs["fields"][field].reverse()

    def test_numeric_equivalent_mirrors_do_not_conflict_under_reversal(self):
        field = "егрюл.уставный_капитал"
        refs = self.refs("егрюл", field, {"inn": INN, "capital": 100},
                         locator="/capital", subject_locator="/inn")
        second = self.refs("егрюл", field, {"inn": INN, "capital": 100.0},
                           locator="/capital", subject_locator="/inn")
        refs["responses"]["r2"] = second["responses"]["r"]
        refs["fields"][field].append({"response_id": "r2", "locator": "/capital", "subject_locator": "/inn", "candidate": True})
        fetch = {"инн": INN, "егрюл": {"уставный_капитал": 100}}
        first = enrich(fetch, now=NOW, raw_refs=refs)
        refs["fields"][field].reverse()
        second = enrich(fetch, now=NOW, raw_refs=refs)
        self.assertEqual(first["_evidence"], second["_evidence"])
        self.assertEqual(first["_evidence"]["conflicts"], [])
        self.assertTrue(all(c["knowledge"] == "known" for c in claims(first, field)))

    def test_bool_is_distinct_from_number_in_values_and_raw_equality(self):
        from evidence_contract import _same_value
        self.assertTrue(_same_value({"amount": [100]}, {"amount": [100.0]}))
        self.assertFalse(_same_value({"amount": [True]}, {"amount": [1]}))
        field = "банкротство.процедура"
        fetch = {"инн": INN, "банкротство": {"процедура": True}}
        refs = self.refs("банкротство", field, {"inn": INN, "procedure": 1},
                         locator="/procedure", subject_locator="/inn")
        with self.assertRaisesRegex(ValueError, "transformation_mismatch"):
            enrich(fetch, now=NOW, raw_refs=refs)
        second = self.refs("банкротство", field, {"inn": INN, "procedure": True},
                           locator="/procedure", subject_locator="/inn")
        refs["responses"]["r2"] = second["responses"]["r"]
        refs["fields"][field][0]["candidate"] = True
        refs["fields"][field].append({"response_id": "r2", "locator": "/procedure", "subject_locator": "/inn"})
        first = enrich(fetch, now=NOW, raw_refs=refs)
        refs["fields"][field].reverse()
        self.assertEqual(first["_evidence"], enrich(fetch, now=NOW, raw_refs=refs)["_evidence"])
        self.assertEqual(len(first["_evidence"]["conflicts"]), 1)

    def test_identical_duplicates_collapse_without_changing_claim(self):
        refs = raw_fixture(3)
        fetch = {"инн": INN, "дампы_фнс": {"численность_сотрудников": 3}}
        first = enrich(fetch, now=NOW, raw_refs=refs)
        field = "дампы_фнс.численность_сотрудников"
        refs["fields"][field].append(copy.deepcopy(refs["fields"][field][0]))
        self.assertEqual(first["_evidence"], enrich(fetch, now=NOW, raw_refs=refs)["_evidence"])

    def test_all_date_kinds_reject_foreign_record_under_matching_root_header(self):
        field = "банкротство.процедура"
        for name in ("effective_at", "published_at", "first_observed_at"):
            with self.subTest(name=name):
                refs = self.refs("банкротство", field, {"inn": INN, "procedure": True,
                    "records": [{"inn": "1234567890", "asof": NOW}]},
                    locator="/procedure", subject_locator="/inn",
                    date_locators={name: "/records/0/asof"}, **{name: NOW})
                with self.assertRaisesRegex(ValueError, "foreign_subject"):
                    enrich({"инн": INN, "банкротство": {"процедура": True}}, now=NOW, raw_refs=refs)

    def test_same_record_future_revision_remains_unknown_until_its_actual_time(self):
        field = "санкции.совпадения"
        fetch = {"инн": INN, "санкции": {"совпадения": []}}
        future = "2026-10-17T09:00:00Z"
        refs = self.refs("санкции", field, {"records": [{"inn": INN, "hits": [], "asof": future}]},
            locator="/records/0/hits", subject_locator="/records/0/inn",
            date_locators={"effective_at": "/records/0/asof"}, effective_at=future,
            coverage_complete=True, search_scope={"subject_inn": INN, "query": INN})
        before = claims(enrich(fetch, now=NOW, raw_refs=refs), field)[0]
        self.assertEqual((before["knowledge"], before["reason_code"]), ("unknown", "revision_unknown"))
        self.assertIsNone(before["effective_at"])
        self.assertIsNone(before["valid_until"])
        current = claims(enrich(fetch, now=future, raw_refs=refs), field)[0]
        self.assertEqual(current["knowledge"], "known")
        expired = claims(enrich(fetch, now="2026-10-24T09:00:00Z", raw_refs=refs), field)[0]
        self.assertEqual((expired["knowledge"], expired["reason_code"]), ("unknown", "stale"))

    def test_monetary_candidate_transform_exactly_replays_stored_numeric_value(self):
        from evidence_contract import _pointer, _transform
        field = "финансы.отчётность_по_годам.2025.строки.1300"
        fetch = {"инн": INN, "финансы": {"отчётность_по_годам": [{"год": 2025, "капитал": 100}]}}
        for value, transformation in ((100, "identity_v1"), (100.0, "identity_v1"),
                                       ("-10,5", "number_v1"), ("3", "integer_v1")):
            with self.subTest(value=value, transformation=transformation):
                refs = self.refs("финансы", field, {"inn": INN, "capital": value},
                    locator="/capital", subject_locator="/inn", candidate=True, transformation=transformation)
                c = claims(enrich(fetch, now=NOW, raw_refs=refs), field)[0]
                replay = _transform(_pointer(json.loads(refs["responses"]["r"]["body"]), c["raw_ref"]["locator"]), c["transformation"])
                self.assertEqual((type(c["value"]), c["value"]), (type(replay), replay))
                self.assertEqual(c["knowledge"], "known")


if __name__ == "__main__":
    unittest.main(verbosity=2)
