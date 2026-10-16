#!/usr/bin/env python3
"""Regressions for independently reproduced Task 1C decision boundaries."""
import copy
import datetime
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'eval')]
import dossier
import evidence_contract as ec
import fin_scoring
import profiles
from run_profiles_eval import fetch_clean, synthetic_verified


class DecisionFixesTests(unittest.TestCase):
    def materialized(self):
        fetch = fetch_clean()
        now = max(c['retrieved_at'] for c in fetch['_evidence']['claims'].values())
        return ec.evaluate(fetch, profile='отсрочка', now=now)

    def test_stale_real_score_cannot_support_changed_fetch(self):
        clean = self.materialized()
        self.assertTrue(clean['assessment']['_decision']['can_recommend'])
        changed = copy.deepcopy(clean['fetch'])
        for row in changed['финансы']['отчётность_по_годам']:
            row['строки']['1300'] = -100
        changed = synthetic_verified(changed)
        self.assertEqual(fin_scoring.score(changed)['оценки_сигналов']['ча_отрицательные_2года']['статус'], 'найден')
        result = profiles.resolve(changed, clean['fin'], 'отсрочка')
        self.assertFalse(result['_decision']['can_recommend'])
        fact = next(f for f in result['факты'] if f['сигнал'] == 'ча_отрицательные_2года')
        self.assertEqual(fact['статус'], 'не проверен')
        fresh = profiles.resolve(changed, fin_scoring.score(changed), 'отсрочка')
        self.assertEqual(fresh['светофор'], '🔴')
        self.assertTrue(fresh['_decision']['can_recommend'])

    def test_cli_rejects_stale_fin_file(self):
        clean = self.materialized()
        changed = copy.deepcopy(clean['fetch'])
        for row in changed['финансы']['отчётность_по_годам']:
            row['строки']['1300'] = -100
        changed = synthetic_verified(changed)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'fetch.json').write_text(json.dumps(changed), encoding='utf-8')
            (path / 'fin.json').write_text(json.dumps(clean['fin']), encoding='utf-8')
            run = subprocess.run([sys.executable, str(ROOT / 'scripts/profiles.py'),
                                  '--fetch', str(path / 'fetch.json'), '--fin', str(path / 'fin.json'),
                                  '--профиль', 'отсрочка'], capture_output=True, text=True, check=True)
        self.assertFalse(json.loads(run.stdout)['_decision']['can_recommend'])

    def test_subcent_annual_change_rejects_stale_fin_at_real_threshold(self):
        original = fetch_clean()
        for row in original['финансы']['отчётность_по_годам']:
            row['строки'].update({'1300': 10.001, '1600': 100})
        original = synthetic_verified(original)
        old_fin = fin_scoring.score(original)
        changed = copy.deepcopy(original)
        for row in changed['финансы']['отчётность_по_годам']:
            row['строки']['1300'] = 9.999
        changed = synthetic_verified(changed)
        new_fin = fin_scoring.score(changed)
        signal = 'коэффициент_автономии'
        self.assertEqual(old_fin['оценки_сигналов'][signal]['статус'], 'отсутствует')
        self.assertEqual(new_fin['оценки_сигналов'][signal]['статус'], 'найден')
        self.assertEqual(old_fin['финансовый_профиль']['строки_баланса'],
                         new_fin['финансовый_профиль']['строки_баланса'])
        stale = profiles.resolve(changed, old_fin, 'отсрочка')
        self.assertFalse(stale['_decision']['can_recommend'])
        self.assertTrue(any('financial_input_mismatch' in item['reason_codes']
                            for item in stale['_decision']['missing_evidence']))
        self.assertNotEqual(old_fin['_input_identity'], new_fin['_input_identity'])
        fresh = profiles.resolve(changed, new_fin, 'отсрочка')
        self.assertEqual(fresh['светофор'], '🟡')
        self.assertTrue(fresh['_decision']['can_recommend'])
        same = profiles.resolve(copy.deepcopy(original), old_fin, 'отсрочка')
        self.assertEqual(same['светофор'], '🟢')
        self.assertTrue(same['_decision']['can_recommend'])

    def test_unbound_legacy_fin_is_unknown(self):
        clean = self.materialized()
        legacy = {k: v for k, v in clean['fin'].items() if k != '_input_identity'}
        result = profiles.resolve(clean['fetch'], legacy, 'отсрочка')
        self.assertFalse(result['_decision']['can_recommend'])
        self.assertTrue(any('financial_input_mismatch' in m['reason_codes']
                            for m in result['_decision']['missing_evidence']))

    def test_identity_preserves_every_normalized_numeric_input_and_presence(self):
        base = {'отчётность_по_годам': [{'год': 2025,
                 'строки': dict.fromkeys(fin_scoring.СТРОКИ, 10.001)}],
                'уставный_капитал': 10001, 'численность_сотрудников': 1.001}
        identity = fin_scoring.input_identity(base)
        for code in fin_scoring.СТРОКИ:
            changed = copy.deepcopy(base)
            changed['отчётность_по_годам'][0]['строки'][code] = 9.999
            with self.subTest(code=code):
                self.assertNotEqual(identity, fin_scoring.input_identity(changed))
        for key, value in [('уставный_капитал', 9999), ('численность_сотрудников', 0.999)]:
            changed = copy.deepcopy(base)
            changed[key] = value
            with self.subTest(key=key):
                self.assertNotEqual(identity, fin_scoring.input_identity(changed))
        missing = copy.deepcopy(base)
        del missing['отчётность_по_годам'][0]['строки']['1300']
        zero = copy.deepcopy(missing)
        zero['отчётность_по_годам'][0]['строки']['1300'] = 0
        self.assertNotEqual(fin_scoring.input_identity(missing), fin_scoring.input_identity(zero))
        equivalent = copy.deepcopy(base)
        equivalent['отчётность_по_годам'][0]['капитал'] = '10,001'
        del equivalent['отчётность_по_годам'][0]['строки']['1300']
        self.assertEqual(identity, fin_scoring.input_identity(equivalent))

    def test_identity_retains_periods_and_duplicate_precision_without_order_dependence(self):
        row = {'год': 2025, 'строки': {'1300': 10.001, '1600': 100}}
        base = [row, {'год': 2024, 'строки': {'1300': 10.001}}]
        identity = fin_scoring.input_identity(base)
        self.assertEqual(identity, fin_scoring.input_identity(list(reversed(base))))
        other_year = copy.deepcopy(base)
        other_year[0]['год'] = 2026
        self.assertNotEqual(identity, fin_scoring.input_identity(other_year))
        duplicate = base + [copy.deepcopy(row)]
        self.assertNotEqual(identity, fin_scoring.input_identity(duplicate))
        conflicting = copy.deepcopy(duplicate)
        conflicting[-1]['строки']['1300'] = 9.999
        self.assertNotEqual(fin_scoring.input_identity(duplicate), fin_scoring.input_identity(conflicting))
        self.assertEqual(fin_scoring.score(duplicate)['оценки_сигналов']['коэффициент_автономии']['статус'],
                         'отсутствует')
        self.assertEqual(fin_scoring.score(conflicting)['оценки_сигналов']['коэффициент_автономии']['статус'],
                         'не проверен')

    def test_old_scoring_rules_cannot_be_bound_as_current(self):
        clean = self.materialized()
        with mock.patch.object(fin_scoring, 'ПОРОГ_ЛИКВИДНОСТИ', 100):
            result = profiles.resolve(clean['fetch'], clean['fin'], 'отсрочка')
            self.assertFalse(result['_decision']['can_recommend'])
        old = copy.deepcopy(clean['fin'])
        old['_input_identity']['scorer_sha256'] = '0' * 64
        self.assertFalse(profiles.resolve(clean['fetch'], old, 'отсрочка')['_decision']['can_recommend'])

    def test_binding_covers_subject_staff_source_and_year(self):
        clean = self.materialized()
        variants = []
        for source, key, value in [('риски', 'численность_сотрудников', 0),
                                    ('риски', 'среднесписочная_за_год', 2023)]:
            changed = copy.deepcopy(clean['fetch'])
            changed[source][key] = value
            variants.append(synthetic_verified(changed))
        changed = copy.deepcopy(clean['fetch'])
        changed.setdefault('дампы_фнс', {})['численность_сотрудников'] = changed['риски'].pop('численность_сотрудников')
        changed['дампы_фнс']['численность_год'] = changed['риски']['среднесписочная_за_год']
        variants.append(synthetic_verified(changed))
        changed = copy.deepcopy(clean['fetch'])
        changed['инн'] = '7704217370'
        changed.pop('_evidence', None)
        variants.append(synthetic_verified(changed))
        for changed in variants:
            with self.subTest(subject=changed['инн'], staff=changed['риски']):
                result = profiles.resolve(changed, clean['fin'], 'отсрочка')
                self.assertFalse(result['_decision']['can_recommend'])

    def test_materialized_render_is_independent_of_clock_availability_policy(self):
        result = self.materialized()
        meta = {'версия': 'saved', 'сформирован': '2026-10-16T09:00:00Z', 'хеш': 'saved'}
        def render():
            blocks = dossier.собрать(result['fetch'], meta, fin=result['fin'],
                                    вердикт=result['assessment'], materialized=True)
            return dossier.в_markdown(blocks), dossier.в_docx_xml(blocks)
        before = render()
        actual_datetime = datetime.datetime
        class HistoricalClock(actual_datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2020, 1, 1, tzinfo=tz)
        resource = ec._resource
        def changed_policy(filename):
            out = copy.deepcopy(resource(filename))
            if filename == 'evidence_policy.json':
                out['max_dependencies'] = 1
            return out
        patches = [mock.patch.object(profiles.datetime, 'datetime', HistoricalClock),
                   mock.patch.object(profiles, '_состояние_блока', return_value=('не проверено', 'future policy', None)),
                   mock.patch.object(ec, '_resource', side_effect=changed_policy)]
        for patch in patches:
            with self.subTest(patch=patch), patch:
                self.assertEqual(render(), before)
        del result['fetch']['_evidence']['source_states']
        with self.assertRaisesRegex(ValueError, 'materialized_source_states_required'):
            render()

    def test_historical_reenrichment_preserves_actual_first_observation(self):
        now = '2026-10-16T09:00:00Z'
        body = json.dumps({'inn': '7707083893', 'staff': 3}).encode()
        refs = {'responses': {'r1': {'source_id': 'риски', 'subject_inn': '7707083893',
                 'request': {'subject_inn': '7707083893'}, 'body': body,
                 'sha256': hashlib.sha256(body).hexdigest(), 'retrieved_at': now,
                 'first_observed_at': now}},
                'fields': {'риски.численность_сотрудников': [{'response_id': 'r1',
                           'locator': '/staff', 'subject_locator': '/inn'}]}}
        fetch = ec.enrich({'инн': '7707083893', 'риски': {'численность_сотрудников': 3}},
                          now=now, raw_refs=refs)
        before = next(c for c in fetch['_evidence']['claims'].values()
                      if c['field'] == 'риски.численность_сотрудников')
        self.assertEqual(before['raw_ref']['availability'], 'available')
        cutoff = '2025-10-16T09:00:00Z'
        for out in (ec.enrich(fetch, now=cutoff), ec.evaluate(fetch, profile='нейтрально', now=cutoff)['fetch']):
            claim = out['_evidence']['claims'][before['claim_id']]
            self.assertEqual(claim['first_observed_at'], before['first_observed_at'])
            self.assertEqual(claim['retrieved_at'], before['retrieved_at'])
            self.assertEqual((claim['knowledge'], claim['reason_code']), ('unknown', 'revision_unknown'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
