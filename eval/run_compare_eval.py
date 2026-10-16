#!/usr/bin/env python3
"""Saved-revision comparison regression tests; synthetic evidence, no network."""
import copy
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import evidence_contract as evidence
import run_store as store

try:
    import compare_counterparties as comparison
except ImportError:
    comparison = None

NOW = '2026-10-16T09:00:00Z'
BEFORE = '2026-10-15T09:00:00Z'
AFTER = '2026-10-17T09:00:00Z'


class CompareEval(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / 'runs'
        self.assertIsNotNone(comparison, 'comparison module required')

    def save(self, inn, *, profile='предоплата', complete=True, now=BEFORE):
        fetch = {'инн': inn, 'профиль': profile, 'дата': now,
                 'дампы_фнс': {'статус': 'ок', 'численность_сотрудников': 12,
                              'численность_год': 2025}} if complete else {'инн': inn, 'профиль': profile}
        fetch = evidence.enrich(fetch, now=now)
        payload = {'fetch': fetch, 'fin': {}, 'assessment': {},
                   'metadata': {'schema_version': 1, 'profile': profile}}
        return store.create(payload, root=self.root, now=now)

    def compare(self, rows, **kwargs):
        return comparison.compare([r['run_id'] for r in rows], as_of=kwargs.get('as_of', NOW), root=self.root)

    def test_coverage_unknown_never_favourable_and_real_periods(self):
        a, b = self.save('7707083893'), self.save('7702070139', complete=False)
        out = self.compare([a, b])
        self.assertEqual(out['schema_version'], 1)
        self.assertEqual(out['profile'], 'предоплата')
        self.assertEqual(len(out['subjects']), 2)
        self.assertTrue(out['coverage_differences'])
        self.assertNotIn('ranking', out)
        row = next(r for r in out['criteria'] if r['field'] == 'дампы_фнс.численность_сотрудников')
        self.assertEqual(row['subjects'][a['subject_inn']]['state'], 'known')
        self.assertEqual(row['subjects'][b['subject_inn']]['state'], 'unknown')
        self.assertEqual(row['subjects'][a['subject_inn']]['facts'][0]['period'],
                         {'start': '2025-01-01', 'end': '2025-12-31'})
        for criterion in out['criteria']:
            self.assertIn('evidence_ids', criterion)
            self.assertNotIn('unknown', criterion.get('favourable_states', []))
        self.assertEqual(row['subjects'][a['subject_inn']]['facts'][0]['raw_ref']['availability'], 'raw_unavailable')

    def test_as_of_selects_saved_revision_not_latest(self):
        a, b = self.save('7707083893'), self.save('7702070139')
        updated = copy.deepcopy(a['payload'])
        updated['metadata']['profile'] = 'нейтрально'
        store.append(a['run_id'], updated, expected_revision=1, operation_id=str(uuid.uuid4()), root=self.root, now=AFTER)
        out = self.compare([a, b])
        self.assertEqual(out['subjects'][0]['revision'], 1)
        self.assertEqual(out['profile'], 'предоплата')

    def test_profile_and_count_and_duplicate_refused(self):
        a, b = self.save('7707083893'), self.save('7702070139', profile='нейтрально')
        for rows, code in [([a], 'invalid_subject_count'), ([a] * 6, 'invalid_subject_count'),
                           ([a, a], 'duplicate_subject'), ([a, b], 'profile_mismatch'),
                           ([a, self.save('7707083893')], 'duplicate_subject')]:
            with self.subTest(code=code), self.assertRaises(comparison.ComparisonError) as ctx:
                self.compare(rows)
            self.assertEqual(ctx.exception.code, code)

    def test_five_subjects_and_invalid_cutoff(self):
        rows = [self.save(str(7707083800 + i)) for i in range(5)]
        self.assertEqual(len(self.compare(rows)['subjects']), 5)
        for value in ('2026-10-16', '', None, 'garbage'):
            with self.subTest(value=value), self.assertRaises(comparison.ComparisonError):
                self.compare(rows, as_of=value)

    def test_future_only_refused(self):
        a, b = self.save('7707083893', now=AFTER), self.save('7702070139')
        with self.assertRaises(comparison.ComparisonError) as ctx:
            self.compare([a, b])
        self.assertEqual(ctx.exception.code, 'revision_unavailable')

    def test_strict_read_only_no_network_no_rescore(self):
        a, b = self.save('7707083893'), self.save('7702070139')
        before = {str(p): (p.stat().st_mtime_ns, p.read_bytes()) for p in self.root.rglob('*') if p.is_file()}
        with patch('urllib.request.urlopen', side_effect=AssertionError('network')), \
             patch.object(evidence, 'evaluate', side_effect=AssertionError('rescore')), \
             patch.object(store, 'create', side_effect=AssertionError('write')), \
             patch.object(store, 'append', side_effect=AssertionError('write')):
            self.compare([a, b])
        after = {str(p): (p.stat().st_mtime_ns, p.read_bytes()) for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_missing_evidence_is_explicit(self):
        a, b = self.save('7707083893'), self.save('7702070139')
        payload = copy.deepcopy(b['payload'])
        payload['fetch'].pop('_evidence')
        store.append(b['run_id'], payload, expected_revision=1, operation_id=str(uuid.uuid4()), root=self.root, now=NOW)
        out = self.compare([a, b])
        self.assertTrue(any('legacy_untraced' in s for s in out['limitations']))
        self.assertTrue(all(r['subjects'][b['subject_inn']]['state'] == 'unknown' for r in out['criteria']))

    def test_foreign_or_mutated_claim_refused(self):
        a, b = self.save('7707083893'), self.save('7702070139')
        payload = copy.deepcopy(a['payload'])
        claim = next(iter(payload['fetch']['_evidence']['claims'].values()))
        claim['subject_inn'] = '7702070139'
        store.append(a['run_id'], payload, expected_revision=1, operation_id=str(uuid.uuid4()), root=self.root, now=NOW)
        with self.assertRaises(comparison.ComparisonError) as ctx:
            self.compare([a, b])
        self.assertEqual(ctx.exception.code, 'invalid_evidence')

    def test_future_claim_and_expired_absence_stay_unknown(self):
        a, b = self.save('7707083893'), self.save('7702070139')
        payload = copy.deepcopy(a['payload'])
        claims = payload['fetch']['_evidence']['claims']
        cid, claim = next((k, c) for k, c in claims.items() if c['field'] == 'дампы_фнс.численность_сотрудников')
        claim['first_observed_at'] = AFTER
        # Time is not part of the content identity; preserve real canonical id.
        store.append(a['run_id'], payload, expected_revision=1, operation_id=str(uuid.uuid4()), root=self.root, now=NOW)
        out = self.compare([a, b])
        row = next(r for r in out['criteria'] if r['field'] == claim['field'])
        self.assertEqual(row['subjects'][a['subject_inn']]['state'], 'unknown')
        self.assertIsNone(row['subjects'][a['subject_inn']]['facts'][0]['value'])

    def test_saved_policy_restriction_and_current_environment_ignored(self):
        a, b = self.save('7707083893'), self.save('7702070139')
        with patch.dict(os.environ, {'INN_CHECK_PROVIDER_POLICY': '/missing/current-policy.json'}):
            self.assertEqual(len(self.compare([a, b])['subjects']), 2)
        payload = copy.deepcopy(a['payload'])
        payload['fetch']['_provider_policy'] = {'schema_version': 1, 'policy': {
            'schema_version': 1, 'total_cost_units': 0,
            'providers': [{'id': 'checko', 'enabled': True, 'allowed_fields': ['name'],
                           'export_fields': [], 'request_limit': 1, 'request_cost_units': 0,
                           'fallback_on': []}]}, 'transfers': []}
        store.append(a['run_id'], payload, expected_revision=1, operation_id=str(uuid.uuid4()), root=self.root, now=NOW)
        with self.assertRaises(comparison.ComparisonError) as ctx:
            self.compare([a, b])
        self.assertEqual(ctx.exception.code, 'export_restricted')

    def test_malformed_policy_refused_without_saved_run_mutation(self):
        a, b = self.save('7707083893'), self.save('7702070139')
        revision = 1
        for marker in (None, False, [], 'PRIVATE_SENTINEL', {}, {'schema_version': 1},
                       {'schema_version': True, 'policy': {}}, {'schema_version': 2, 'policy': {}}):
            with self.subTest(marker=marker):
                payload = copy.deepcopy(a['payload'])
                payload['fetch']['_provider_policy'] = marker
                store.append(a['run_id'], payload, expected_revision=revision,
                             operation_id=str(uuid.uuid4()), root=self.root, now=NOW)
                revision += 1
                before = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
                with self.assertRaises(comparison.ComparisonError) as ctx:
                    self.compare([a, b])
                self.assertEqual(ctx.exception.code, 'invalid_captured_provider_policy')
                self.assertNotIn('PRIVATE_SENTINEL', str(ctx.exception))
                self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_expired_fact_keeps_original_period_but_cannot_support_decision(self):
        a, b = self.save('7707083893'), self.save('7702070139')
        payload = copy.deepcopy(a['payload'])
        claim = next(c for c in payload['fetch']['_evidence']['claims'].values()
                     if c['field'] == 'дампы_фнс.численность_сотрудников')
        claim['valid_until'] = NOW
        store.append(a['run_id'], payload, expected_revision=1, operation_id=str(uuid.uuid4()), root=self.root, now=NOW)
        row = next(r for r in self.compare([a, b])['criteria'] if r['field'] == claim['field'])
        fact = row['subjects'][a['subject_inn']]['facts'][0]
        self.assertEqual(fact['knowledge'], 'unknown')
        self.assertEqual(fact['reason_code'], 'stale')
        self.assertEqual(fact['period'], claim['period'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
