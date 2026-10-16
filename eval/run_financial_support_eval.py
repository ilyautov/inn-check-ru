#!/usr/bin/env python3
"""Strict AND support for actual financial scorer branches; synthetic inputs."""
import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'eval')]
import evidence_contract as ec
from run_profiles_eval import fetch_clean, synthetic_verified


class FinancialSupportTests(unittest.TestCase):
    def fixture(self, revenue=500000, staff=0, year=2025):
        fetch = fetch_clean()
        for row, target in zip(fetch['финансы']['отчётность_по_годам'], (2025, 2024)):
            row['год'] = target
            row.pop('выручка', None)
            row['строки']['2110'] = revenue if target == 2025 else 100000
        fetch['риски']['численность_сотрудников'] = staff
        fetch['риски']['среднесписочная_за_год'] = year
        return fetch

    def evaluate(self, fetch):
        observed = synthetic_verified(fetch)
        now = max(c['retrieved_at'] for c in observed['_evidence']['claims'].values())
        return ec.evaluate(observed, profile='отсрочка', now=now)

    def transit(self, result):
        claims = result['fetch']['_evidence']['claims']
        claim = next(c for c in claims.values() if c['field'] == 'fin.транзитный_профиль')
        return claim, [claims[cid] for cid in claim['depends_on']]

    def assert_supported(self, result, value, expected_fields):
        claim, deps = self.transit(result)
        self.assertEqual((claim['knowledge'], claim['value']), ('known', value))
        self.assertEqual({d['field'] for d in deps}, expected_fields)
        self.assertTrue(all(d['knowledge'] == 'known' for d in deps), deps)
        # The gate applies to every known derived financial claim, without alias waivers.
        claims = result['fetch']['_evidence']['claims']
        for c in claims.values():
            if c['field'].startswith('fin.') and c['knowledge'] == 'known':
                self.assertTrue(c['depends_on'])
                self.assertTrue(all(claims[cid]['knowledge'] == 'known' for cid in c['depends_on']))

    @staticmethod
    def revenues():
        return {'финансы.отчётность_по_годам.2025.строки.2110',
                'финансы.отчётность_по_годам.2024.строки.2110'}

    def test_canonical_positive_uses_selected_year_not_absent_alias(self):
        result = self.evaluate(self.fixture())
        self.assert_supported(result, True, self.revenues() | {
            'риски.численность_сотрудников', 'риски.среднесписочная_за_год'})

    def test_high_growth_negative_still_needs_known_staff(self):
        self.assert_supported(self.evaluate(self.fixture(staff=45)), False, self.revenues() | {
            'риски.численность_сотрудников', 'риски.среднесписочная_за_год'})

    def test_timeout_low_growth_has_sufficient_revenue_proof(self):
        fetch = self.fixture(revenue=120000)
        fetch['риски'] = None
        fetch['_доступность']['риски']['состояние'] = 'не проверено'
        result = self.evaluate(fetch)
        self.assert_supported(result, False, self.revenues())
        self.assertFalse(result['metadata']['decision']['can_recommend'])

    def test_low_growth_does_not_require_mismatched_staff_period(self):
        self.assert_supported(self.evaluate(self.fixture(revenue=120000, year=2023)),
                              False, self.revenues())

    def test_high_growth_missing_staff_is_unknown_no_advice(self):
        fetch = self.fixture(staff=None)
        result = self.evaluate(fetch)
        claim, deps = self.transit(result)
        self.assertEqual(claim['knowledge'], 'unknown')
        self.assertTrue(any(d['knowledge'] == 'unknown' for d in deps))
        self.assertFalse(result['metadata']['decision']['can_recommend'])

    def test_high_growth_missing_year_is_unknown_no_advice(self):
        result = self.evaluate(self.fixture(year=None))
        claim, deps = self.transit(result)
        self.assertEqual(claim['knowledge'], 'unknown')
        self.assertTrue(any(d['knowledge'] == 'unknown' for d in deps))
        self.assertFalse(result['metadata']['decision']['can_recommend'])

    def test_high_growth_mismatched_year_is_unknown(self):
        claim, _ = self.transit(self.evaluate(self.fixture(year=2023)))
        self.assertEqual((claim['knowledge'], claim['reason_code']), ('unknown', 'period_mismatch'))

    def test_both_supplied_year_aliases_are_required(self):
        fetch = self.fixture()
        fetch['риски']['численность_год'] = 2025
        self.assert_supported(self.evaluate(fetch), True, self.revenues() | {
            'риски.численность_сотрудников', 'риски.среднесписочная_за_год', 'риски.численность_год'})

    def test_conflicting_year_aliases_cannot_be_ignored(self):
        fetch = self.fixture()
        fetch['риски']['численность_год'] = 2023
        result = self.evaluate(fetch)
        self.assertEqual(self.transit(result)[0]['knowledge'], 'unknown')
        self.assertFalse(result['metadata']['decision']['can_recommend'])

    def test_selected_fallback_source_excludes_unselected_source(self):
        fetch = self.fixture()
        fetch['риски']['численность_сотрудников'] = None
        fetch['дампы_фнс'] = {'численность_сотрудников': 0, 'численность_год': 2025}
        self.assert_supported(self.evaluate(fetch), True, self.revenues() | {
            'дампы_фнс.численность_сотрудников', 'дампы_фнс.численность_год'})

    def test_missing_revenue_never_proves_negative(self):
        fetch = self.fixture(revenue=None)
        result = self.evaluate(fetch)
        self.assertEqual(self.transit(result)[0]['knowledge'], 'unknown')
        self.assertFalse(result['metadata']['decision']['can_recommend'])

    def test_unmapped_staff_year_cannot_disappear_from_required_support(self):
        fetch = self.fixture()
        fetch['риски']['численность_сотрудников'] = None
        # The scorer accepts this alias, but the current evidence policy does
        # not map it for dumps. No checked claim may be invented for it.
        fetch['дампы_фнс'] = {'численность_сотрудников': 0, 'среднесписочная_за_год': 2025}
        result = self.evaluate(fetch)
        self.assertEqual(self.transit(result)[0]['knowledge'], 'unknown')
        self.assertFalse(result['metadata']['decision']['can_recommend'])

    def test_unmapped_top_level_staff_cannot_prove_transit(self):
        fetch = self.fixture(staff=None)
        fetch.update(численность_сотрудников=0, среднесписочная_за_год=2025)
        result = self.evaluate(fetch)
        self.assertEqual(self.transit(result)[0]['knowledge'], 'unknown')
        self.assertFalse(result['metadata']['decision']['can_recommend'])

    def test_unknown_required_nonfinancial_leaf_never_binds_known(self):
        result = self.evaluate(self.fixture())
        fetch = copy.deepcopy(result['fetch'])
        # Isolate a binding boundary: the exact economic input remains unchanged,
        # while a required year observation no longer has known evidence.
        for c in fetch['_evidence']['claims'].values():
            if c['field'] == 'риски.среднесписочная_за_год':
                c['knowledge'], c['reason_code'] = 'unknown', 'revision_unknown'
        bound = ec.bind_financial(fetch, result['fin'])
        self.assertEqual(self.transit({'fetch': bound})[0]['knowledge'], 'unknown')


if __name__ == '__main__':
    unittest.main(verbosity=2)
