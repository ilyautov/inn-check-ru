#!/usr/bin/env python3
"""Synthetic point-in-time mechanism tests; real vintage pairs remain zero."""
import concurrent.futures
import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import evidence_contract as evidence
import run_store as store

SPEC = importlib.util.find_spec('historical_replay')
if SPEC:
    import historical_replay as historical

INN = '7707083893'
OBSERVED = '2022-04-01T09:00:00Z'
CUTOFF = '2022-05-01T09:00:00Z'
LATER = '2023-04-01T09:00:00Z'
FIELD = 'финансы.отчётность_по_годам.2021.строки.2110'
NETWORK_FORBIDDEN = False


def audit(event, args):
    if NETWORK_FORBIDDEN and event in {'socket.connect', 'socket.connect_ex', 'socket.getaddrinfo', 'socket.bind'}:
        raise AssertionError('replay attempted network: ' + event)


sys.addaudithook(audit)


def fixture():
    fetch = {'инн': INN, 'тип': 'юрлицо', 'финансы': {
        'форма_отчётности': 'full', 'actualBfoDate': '2021-12-31',
        'отчётность_по_годам': [{'год': 2021, 'строки': {'2110': 100}}]}}
    body = json.dumps({'inn': INN, 'revenue': 100}).encode()
    refs = {'responses': {'v1': {'source_id': 'финансы', 'subject_inn': INN,
        'request': {'subject_inn': INN}, 'body': body,
        'sha256': hashlib.sha256(body).hexdigest(), 'retrieved_at': OBSERVED,
        'first_observed_at': OBSERVED}}, 'fields': {FIELD: [{
            'response_id': 'v1', 'locator': '/revenue', 'subject_locator': '/inn',
            'transformation': 'number_v1'}]}}
    fetch = evidence.enrich(fetch, now=OBSERVED, raw_refs=refs)
    payload = evidence.evaluate(fetch, profile='нейтрально', now=OBSERVED)
    payload['metadata'].update(profile='нейтрально', workflow_status='materialized')
    return payload


def revenue(payload):
    return next(c for c in payload['fetch']['_evidence']['claims'].values() if c['field'] == FIELD)


def disk_bytes(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in Path(root).rglob('*') if p.is_file()}


class ModuleTests(unittest.TestCase):
    def test_replay_module_exists(self):
        self.assertIsNotNone(SPEC, 'Phase 6B requires scripts/historical_replay.py')


@unittest.skipUnless(SPEC, 'replay module not implemented yet')
class ReplayTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / 'runs'
        self.payload = fixture()
        self.record = store.create(self.payload, root=self.root, now=OBSERVED)
        self.run_id = self.record['run_id']

    def replay(self, mode='original', cutoff=CUTOFF):
        global NETWORK_FORBIDDEN
        NETWORK_FORBIDDEN = True
        try:
            return historical.replay(self.run_id, mode=mode, cutoff=cutoff, root=self.root)
        finally:
            NETWORK_FORBIDDEN = False

    def test_original_returns_saved_payload_manifest_without_evaluation(self):
        before = disk_bytes(self.root)
        with patch.object(evidence, 'evaluate', side_effect=AssertionError('original rescored')):
            out = self.replay()
        self.assertEqual(out['data'], self.record['payload'])
        self.assertEqual(store._json(out['data']), store._json(self.record['payload']))
        self.assertEqual(out['manifest'], self.record['manifest'])
        self.assertEqual(out['original_rules_version'], self.record['manifest']['versions']['rules_version'])
        self.assertEqual(out['applied_rules_version'], out['original_rules_version'])
        self.assertEqual(disk_bytes(self.root), before)

    def test_later_correction_selects_committed_old_revision(self):
        original = self.replay()
        revised = copy.deepcopy(self.payload)
        revised['assessment']['correction'] = 'synthetic later correction'
        store.append(self.run_id, revised, expected_revision=1, operation_id=str(uuid.uuid4()),
                     root=self.root, now=LATER)
        replayed = self.replay()
        self.assertEqual(replayed['source_revision'], 1)
        self.assertEqual(replayed['data'], original['data'])
        self.assertEqual(replayed['manifest'], original['manifest'])
        self.assertEqual(self.replay(cutoff=LATER)['source_revision'], 2)

    def test_future_only_and_malformed_cutoffs_fail_safely(self):
        for cutoff in ['2022-01-01T00:00:00Z', '2022-05-01', '2022-05-01T09:00:00',
                       '2022-05-01T09:00:00Zgarbage', '20220501T090000Z', '2022-05-01T09:00:00+99:99', '2022-05-01T09:00:00+03:99', None]:
            with self.subTest(cutoff=cutoff), self.assertRaises(historical.ReplayError):
                self.replay(cutoff=cutoff)
        with self.assertRaises(historical.ReplayError):
            self.replay(mode='guess')
        self.assertEqual(self.replay(cutoff='2022-05-01T12:00:00+03:00')['cutoff'], CUTOFF)

    def _replace(self, payload):
        self.record = store.create(payload, root=self.root, now=OBSERVED)
        self.run_id = self.record['run_id']

    def test_2021_year_actual_bfo_date_and_late_observation_not_availability(self):
        revenue(self.payload)['first_observed_at'] = LATER
        self._replace(self.payload)
        original = self.replay()
        self.assertEqual(original['data'], self.record['payload'])
        self.assertNotEqual(original['point_in_time_status'], 'verified')
        current = self.replay('current_rules')
        c = revenue(current['data'])
        self.assertIsNone(c['value'])
        self.assertEqual(c['knowledge'], 'unknown')
        self.assertTrue(any(e.get('field') == FIELD and e['reason_code'] == 'observed_after_cutoff'
                            for e in current['exclusions']))

    def test_unknown_edition_excluded_even_when_reporting_period_old(self):
        revenue(self.payload)['first_observed_at'] = None
        revenue(self.payload)['published_at'] = '2021-01-01T00:00:00Z'
        self._replace(self.payload)
        self.assertIsNone(revenue(self.replay('current_rules')['data'])['value'])

    def test_confirmed_publication_semantics_accepts_old_edition(self):
        c = revenue(self.payload)
        c['first_observed_at'] = None
        c['published_at'] = '2021-12-31T00:00:00Z'
        self.payload['metadata']['publication_semantics'] = {c['claim_id']: {
            'confirmed': True, 'meaning': 'public_availability',
            'reference': 'synthetic-source-schema-v1'}}
        self._replace(self.payload)
        self.assertEqual(revenue(self.replay('current_rules')['data'])['value'], 100)

    def _published_payload(self, observed=LATER):
        payload = copy.deepcopy(self.payload)
        c = revenue(payload)
        c.update(first_observed_at=observed, retrieved_at=LATER, published_at='2021-12-31T00:00:00Z')
        payload['metadata']['publication_semantics'] = {c['claim_id']: {
            'confirmed': True, 'meaning': 'public_availability', 'reference': 'synthetic-source-schema-v1'}}
        return payload

    def test_late_retrieval_confirmed_publication_reaches_real_scorer_unchanged(self):
        for observed in (None, LATER):
            with self.subTest(first_observed_at=observed):
                payload = self._published_payload(observed)
                source = revenue(payload)
                self._replace(payload)
                before = disk_bytes(self.root)
                out = self.replay('current_rules')
                self.assertIn(source['claim_id'], out['eligible_claim_ids'])
                self.assertEqual(out['point_in_time_status'], 'verified')
                c = revenue(out['data'])
                self.assertEqual((c['value'], c['knowledge'], c['reason_code']), (100, 'known', None))
                self.assertEqual((c['first_observed_at'], c['retrieved_at']), (observed, LATER))
                normalized = evidence.normalized_financial_input(out['data']['fetch'])
                report = next(r for r in normalized['финансы']['отчётность_по_годам'] if r['год'] == 2021)
                self.assertEqual(report['строки']['2110'], 100)
                self.assertEqual(self.replay()['data'], self.record['payload'])
                self.assertEqual(out['manifest'], self.record['manifest'])
                self.assertFalse(out['artifact_manifest']['original_historical_outcome'])
                self.assertEqual(disk_bytes(self.root), before)

    def test_unconfirmed_or_future_publication_never_admits_late_leaf(self):
        for change in ('unconfirmed', 'missing_reference', 'wrong_meaning', 'future_date', 'missing_raw'):
            with self.subTest(change=change):
                payload = self._published_payload()
                c = revenue(payload)
                assertion = payload['metadata']['publication_semantics'][c['claim_id']]
                if change == 'unconfirmed': assertion['confirmed'] = False
                elif change == 'missing_reference': assertion.pop('reference')
                elif change == 'wrong_meaning': assertion['meaning'] = 'reporting_period'
                elif change == 'future_date': c['published_at'] = LATER
                else:
                    old_id = c['claim_id']
                    c['raw_ref'].update(availability='raw_unavailable', sha256=None, locator=None, response_id=None)
                    c['claim_id'] = evidence._claim_id(c)
                    claims = payload['fetch']['_evidence']['claims']
                    claims[c['claim_id']] = claims.pop(old_id)
                    for dependent in claims.values():
                        dependent['depends_on'] = [c['claim_id'] if d == old_id else d for d in dependent['depends_on']]
                    payload['metadata']['publication_semantics'] = {c['claim_id']: assertion}
                self._replace(payload)
                out = self.replay('current_rules')
                self.assertNotIn(c['claim_id'], out['eligible_claim_ids'])
                self.assertEqual(revenue(out['data'])['knowledge'], 'unknown')

    def test_confirmed_publication_supports_complete_production_decision(self):
        import run_profiles_eval
        cutoff = '2026-10-16T09:00:00Z'
        later = '2026-10-17T09:00:00Z'
        with patch.object(run_profiles_eval.datetime, 'datetime') as clock:
            clock.now.return_value = datetime.fromisoformat(cutoff.replace('Z', '+00:00'))
            fetch = run_profiles_eval.fetch_clean(список_цб={'формулировка': False})
        # Explicit synthetic observations at collection; no historical date is inferred.
        for claim in fetch['_evidence']['claims'].values():
            if claim['value'] is not None:
                claim['first_observed_at'] = cutoff
        payload = evidence.evaluate(fetch, profile='клиент_115фз', now=cutoff)
        self.assertTrue(payload['metadata']['decision']['can_recommend'])
        c = next(c for c in payload['fetch']['_evidence']['claims'].values()
                 if c['field'] == 'риски.массовый_руководитель')
        c.update(published_at=cutoff, first_observed_at=later, retrieved_at=later)
        payload['metadata'].update(profile='клиент_115фз', publication_semantics={c['claim_id']: {
            'confirmed': True, 'meaning': 'public_availability', 'reference': 'synthetic-source-schema-v1'}})
        self.record = store.create(payload, root=self.root, now=cutoff)
        self.run_id = self.record['run_id']
        before = disk_bytes(self.root)
        out = self.replay('current_rules', cutoff=cutoff)
        decision = out['data']['metadata']['decision']
        self.assertIn(c['claim_id'], out['eligible_claim_ids'])
        self.assertTrue(decision['can_recommend'], decision)
        self.assertEqual(decision['decision_status'], 'supported')
        self.assertEqual(disk_bytes(self.root), before)

    def test_observed_manual_source_context_survives_filter_without_borrowing_future(self):
        import run_profiles_eval
        cutoff = '2026-10-16T09:00:00Z'
        with patch.object(run_profiles_eval.datetime, 'datetime') as clock:
            clock.now.return_value = datetime.fromisoformat(cutoff.replace('Z', '+00:00'))
            fetch = run_profiles_eval.fetch_clean()
        for claim in fetch['_evidence']['claims'].values():
            if claim['value'] is not None:
                claim['first_observed_at'] = cutoff
        payload = evidence.evaluate(fetch, profile='отсрочка', now=cutoff)
        payload['metadata']['profile'] = 'отсрочка'
        self.assertTrue(payload['metadata']['decision']['can_recommend'])
        for change in ('none', 'future_leaf', 'published_late_observation', 'incomplete_search'):
            with self.subTest(change=change):
                candidate = copy.deepcopy(payload)
                claims = candidate['fetch']['_evidence']['claims']
                c = next(c for c in claims.values() if c['field'] == 'фссп.производств')
                if change == 'future_leaf': c['first_observed_at'] = '2026-10-17T09:00:00Z'
                if change == 'published_late_observation':
                    c.update(first_observed_at='2026-10-17T09:00:00Z', published_at=cutoff)
                    candidate['metadata']['publication_semantics'] = {c['claim_id']: {
                        'confirmed': True, 'meaning': 'public_availability', 'reference': 'synthetic-source-schema-v1'}}
                if change == 'incomplete_search':
                    c.update(knowledge='unknown', reason_code='coverage_incomplete', coverage_complete=False)
                    candidate['fetch']['фссп']['статус'] = 'проверено частично'
                self.record = store.create(candidate, root=self.root, now=cutoff)
                self.run_id = self.record['run_id']
                before = disk_bytes(self.root)
                out = self.replay('current_rules', cutoff=cutoff)
                self.assertEqual(out['data']['metadata']['decision']['can_recommend'], change == 'none')
                if change == 'none':
                    for source in ('фссп', 'суды'):
                        self.assertEqual(out['data']['fetch'][source]['статус'], payload['fetch'][source]['статус'])
                elif change == 'future_leaf':
                    self.assertNotIn('статус', out['data']['fetch']['фссп'])
                    self.assertEqual(out['point_in_time_status'], 'partial')
                elif change == 'published_late_observation':
                    self.assertIn(c['claim_id'], out['eligible_claim_ids'])
                    self.assertNotIn('статус', out['data']['fetch']['фссп'])
                    self.assertTrue(any('source_unavailable' in m['reason_codes']
                                        for m in out['data']['metadata']['decision']['missing_evidence']))
                self.assertEqual(disk_bytes(self.root), before)

    def test_publication_context_is_explicit_and_isolated_between_concurrent_calls(self):
        import fin_scoring
        payload = self._published_payload()
        semantics = payload['metadata']['publication_semantics']
        barrier = threading.Barrier(2)
        score = fin_scoring.score
        def overlap(fetch):
            barrier.wait(timeout=10)
            return score(fetch)
        def evaluate(trusted):
            kwargs = {'publication_semantics': semantics} if trusted else {}
            return evidence.evaluate(payload['fetch'], profile='нейтрально', now=CUTOFF, **kwargs)
        with patch.object(fin_scoring, 'score', side_effect=overlap), \
                concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            trusted, ordinary = list(pool.map(evaluate, [True, False]))
        self.assertEqual(revenue(trusted)['knowledge'], 'known')
        self.assertEqual(revenue(ordinary)['reason_code'], 'revision_unknown')
        self.assertEqual(set(trusted), {'fetch', 'fin', 'assessment', 'metadata'})

    def test_publication_context_resets_after_exception_and_nested_evaluation(self):
        import fin_scoring
        payload = self._published_payload()
        semantics = payload['metadata']['publication_semantics']
        def evaluate(**kwargs):
            return evidence.evaluate(payload['fetch'], profile='нейтрально', now=CUTOFF, **kwargs)
        with patch.object(fin_scoring, 'score', side_effect=ValueError('synthetic scorer failure')), \
                self.assertRaisesRegex(ValueError, 'synthetic scorer failure'):
            evaluate(publication_semantics=semantics)
        self.assertEqual(revenue(evaluate())['reason_code'], 'revision_unknown')
        score = fin_scoring.score
        nested = []
        def nesting(fetch):
            if not nested:
                nested.append(None)
                nested[0] = evaluate()
            return score(fetch)
        with patch.object(fin_scoring, 'score', side_effect=nesting):
            outer = evaluate(publication_semantics=semantics)
        self.assertEqual(revenue(nested[0])['reason_code'], 'revision_unknown')
        self.assertEqual(revenue(outer)['knowledge'], 'known')
        self.assertEqual(revenue(evaluate())['reason_code'], 'revision_unknown')

    def test_evaluator_publication_authority_is_claim_bound_and_keeps_expiry(self):
        for change in ('missing_reference', 'wrong_claim', 'future_date', 'invalid_date', 'stale'):
            with self.subTest(change=change):
                payload = self._published_payload()
                c = revenue(payload)
                semantics = payload['metadata']['publication_semantics']
                if change == 'missing_reference': semantics[c['claim_id']]['reference'] = ''
                elif change == 'wrong_claim': semantics = {'sha256:' + '0' * 64: next(iter(semantics.values()))}
                elif change == 'future_date': c['published_at'] = LATER
                elif change == 'invalid_date': c['published_at'] = '20211231T000000Z'
                else: c['valid_until'] = CUTOFF
                result = evidence.evaluate(payload['fetch'], profile='нейтрально', now=CUTOFF,
                                           publication_semantics=semantics)
                self.assertEqual(revenue(result)['knowledge'], 'unknown')
                self.assertEqual(revenue(result)['reason_code'], 'stale' if change == 'stale' else 'revision_unknown')

    def test_current_rules_new_manifest_temp_threshold_keeps_source_bytes(self):
        before = disk_bytes(self.root)
        out = self.replay('current_rules')
        canon = copy.deepcopy(evidence._resource('canon_ru.json'))
        threshold = next(v for v in canon['пороги'].values() if isinstance(v, dict) and isinstance(v.get('значение'), (int, float)))
        threshold['значение'] += 1
        resource = Path(self.tmp.name) / 'canon-test.json'
        resource.write_text(json.dumps(canon), encoding='utf-8')
        real_resource = evidence._resource
        def load(name):
            return json.loads(resource.read_text()) if name == 'canon_ru.json' else real_resource(name)
        with patch.object(evidence, '_resource', side_effect=load):
            changed = self.replay('current_rules')
        self.assertNotEqual(changed['applied_rules_version'], out['applied_rules_version'])
        self.assertEqual(changed['original_rules_version'], self.record['manifest']['versions']['rules_version'])
        self.assertNotEqual(changed['artifact_manifest'], out['artifact_manifest'])
        self.assertFalse(changed['artifact_manifest']['original_historical_outcome'])
        self.assertEqual(disk_bytes(self.root), before)

    def test_current_rules_preserves_captured_policy_no_current_config(self):
        marker = {'schema_version': 1, 'policy': {'schema_version': 1, 'total_cost_units': 0, 'providers': []}}
        self.payload['fetch']['_provider_policy'] = marker
        self.payload['metadata']['provider_policy_captured'] = True
        self._replace(self.payload)
        import provider_policy
        with patch.object(provider_policy, 'configured_policy', side_effect=AssertionError('current policy read')):
            out = self.replay('current_rules')
        self.assertEqual(out['data']['fetch']['_provider_policy'], marker)
        self.assertTrue(out['data']['metadata']['provider_policy_captured'])

    def test_legacy_without_claims_is_unverified_and_cannot_recreate_facts(self):
        legacy = {'fetch': {'инн': INN, 'риски': {'массовый_адрес': False}}, 'fin': {},
                  'assessment': {'stored': True}, 'metadata': {'schema_version': 1, 'profile': 'нейтрально'}}
        self._replace(legacy)
        self.assertEqual(self.replay()['point_in_time_status'], 'unverified')
        out = self.replay('current_rules')
        self.assertFalse(out['data']['assessment']['_decision']['can_recommend'])
        self.assertTrue(all(c['knowledge'] != 'known' for c in out['data']['fetch']['_evidence']['claims'].values()))

    def test_configured_root_and_missing_root_do_not_write(self):
        with patch.dict(os.environ, {'INN_CHECK_RUNS_DIR': str(self.root)}):
            self.assertEqual(historical.replay(self.run_id, mode='original', cutoff=CUTOFF)['data'], self.record['payload'])
        missing = Path(self.tmp.name).resolve() / 'missing'
        with self.assertRaises(store.RunStoreError):
            historical.replay(self.run_id, mode='original', cutoff=CUTOFF, root=missing)
        self.assertFalse(missing.exists())

    def test_cli_one_json_and_invalid_cutoff_exit_two(self):
        args = [sys.executable, str(ROOT / 'scripts/workflow_cli.py'), 'run', 'replay', self.run_id,
                '--mode', 'original', '--cutoff', CUTOFF, '--store', str(self.root)]
        result = subprocess.run(args, capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout)['data']['source_revision'], 1)
        args[args.index(CUTOFF)] = '2022-05-01'
        result = subprocess.run(args, capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)['error']['code'], 'invalid_cutoff')

    def test_retro_explicitly_labels_current_rules(self):
        import retro_verdict
        path = Path(self.tmp.name) / 'legacy.json'
        path.write_text(json.dumps({'инн': INN, 'дата': OBSERVED, 'тип': 'юрлицо'}))
        out = retro_verdict.retro(INN, '2022-05-01', снимок_путь=str(path))
        self.assertEqual(out['replay_mode'], 'current_rules')
        self.assertFalse(out['original_historical_outcome'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
