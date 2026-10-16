#!/usr/bin/env python3
"""Offline integration contract: authoritative decisions and recorded provenance."""
import concurrent.futures
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'eval')]
import dossier
import evidence_contract as ec
import fin_scoring
import profiles
import snapshot
from run_profiles_eval import fetch_clean

NOW = '2026-10-16T09:00:00Z'
INN = '7707083893'


class DecisionContractTests(unittest.TestCase):
    def test_public_api_is_from_checkout(self):
        self.assertEqual(Path(ec.__file__).resolve(), ROOT / 'scripts/evidence_contract.py')
        self.assertTrue(callable(getattr(ec, 'evaluate', None)))

    def test_neutral_and_snapshot_roundtrip(self):
        result = ec.evaluate({'инн': INN}, profile='нейтрально', now=NOW)
        self.assertEqual(set(result), {'fetch', 'fin', 'assessment', 'metadata'})
        decision = result['metadata']['decision']
        self.assertEqual(decision['decision_status'], 'facts_only')
        self.assertFalse(decision['can_recommend'])
        self.assertEqual(result['fetch']['_decision'], decision)
        self.assertEqual(result['assessment']['_decision'], decision)
        snap = snapshot.fingerprint(result['fetch'], дата=NOW)
        self.assertEqual(json.loads(json.dumps(snap))['_decision'], decision)
        self.assertEqual(snap['_evidence'], result['fetch']['_evidence'])

    def test_missing_mass_head_survives_final_outputs(self):
        f = fetch_clean()
        f['риски'].pop('массовый_руководитель')
        result = ec.evaluate(f, profile='предоплата', now=NOW)
        decision = result['metadata']['decision']
        self.assertFalse(decision['can_recommend'])
        self.assertIn('массовый_руководитель', json.dumps(decision, ensure_ascii=False))
        self.assertNotIn('не массовый', result['assessment']['рекомендация'])
        meta = {'версия': 'test', 'сформирован': NOW, 'вход': 'fixture', 'хеш': 'x'}
        blocks = dossier.собрать(result['fetch'], meta, fin=result['fin'], вердикт=result['assessment'])
        for rendered in (dossier.в_markdown(blocks), dossier.в_docx_xml(blocks)):
            self.assertIn('missing_required_field', rendered)
            self.assertIn('массовый_руководитель', rendered)
            self.assertIn('sha256:', rendered)

    def test_authoritative_financial_dependencies_and_periods(self):
        f = {'инн': INN, 'риски': {'численность_сотрудников': 0, 'среднесписочная_за_год': 2025},
             'финансы': {'отчётность_по_годам': [
                 {'год': 2025, 'выручка': 5000}, {'год': 2024, 'выручка': 100}]}}
        out = ec.evaluate(f, profile='нейтрально', now=NOW)
        claims = out['fetch']['_evidence']['claims']
        transit = next(c for c in claims.values() if c['field'] == 'fin.транзитный_профиль')
        fields = [claims[c]['field'] for c in transit['depends_on']]
        for field in ('риски.численность_сотрудников', 'риски.среднесписочная_за_год',
                      'финансы.отчётность_по_годам.2025.строки.2110',
                      'финансы.отчётность_по_годам.2024.строки.2110'):
            self.assertIn(field, fields)
        self.assertEqual(out['fin']['версия_оценок'], 1)
        self.assertEqual(out['fin']['финансовый_профиль']['численность_год'], 2025)

    def test_reevaluate_preserves_verified_raw_and_dates(self):
        body = json.dumps({'inn': INN, 'staff': 3}).encode()
        refs = {'responses': {'r1': {'source_id': 'риски', 'subject_inn': INN,
                 'request': {'subject_inn': INN}, 'body': body,
                 'sha256': hashlib.sha256(body).hexdigest(), 'retrieved_at': NOW}},
                'fields': {'риски.численность_сотрудников': [{'response_id': 'r1',
                           'locator': '/staff', 'subject_locator': '/inn'}]}}
        f = ec.enrich({'инн': INN, 'риски': {'численность_сотрудников': 3}}, now=NOW, raw_refs=refs)
        out = ec.evaluate(f, profile='нейтрально', now='2026-10-17T09:00:00Z')
        before = next(c for c in f['_evidence']['claims'].values() if c['field'] == 'риски.численность_сотрудников')
        after = out['fetch']['_evidence']['claims'][before['claim_id']]
        self.assertEqual(after['raw_ref'], before['raw_ref'])
        self.assertEqual(after['retrieved_at'], before['retrieved_at'])

    def test_evidence_unknown_masks_legacy_value_and_fin(self):
        f = ec.enrich({'инн': INN, 'риски': {'массовый_руководитель': False}}, now=NOW)
        signals = {s['id']: s for s in profiles.extract_signals(f)}
        self.assertEqual(signals['массовый_руководитель']['статус'], 'не проверен')
        self.assertTrue(signals['массовый_руководитель']['причина'].startswith('coverage_incomplete'))

    def test_capture_records_actual_bytes_and_locator(self):
        import fetch_counterparty as fc
        with fc.capture_session(INN) as capture:
            fc._ИСТОЧНИК.id = 'риски'
            fc._записать('GET', 'https://pb.nalog.ru/search?queryUl=' + INN, 200,
                        json.dumps({'ul': {'data': [{'inn': INN, 'sulst_name_ex': 'действующее',
                                                    'token': 'secret-token'}]}}).encode())
        refs = capture.raw_refs({'инн': INN, 'риски': {'статус': 'действующее'}})
        self.assertEqual(len(refs['responses']), 1)
        response = next(iter(refs['responses'].values()))
        self.assertNotIn(b'secret-token', response['body'])
        self.assertEqual(response['sha256'], hashlib.sha256(response['body']).hexdigest())
        f = ec.enrich({'инн': INN, 'риски': {'статус': 'действующее'}}, now=response['retrieved_at'], raw_refs=refs)
        claim = next(c for c in f['_evidence']['claims'].values() if c['field'] == 'риски.статус')
        self.assertEqual(claim['raw_ref']['locator'], '/ul/data/0/sulst_name_ex')
        self.assertEqual(claim['raw_ref']['availability'], 'available')
        self.assertEqual(claim['first_observed_at'], ec._iso(ec._instant(response['retrieved_at'])))
        reread = ec.enrich(f, now=response['retrieved_at'])
        self.assertEqual(reread['_evidence']['claims'][claim['claim_id']]['first_observed_at'], claim['first_observed_at'])


    def test_one_authoritative_score_and_actual_versions(self):
        with mock.patch.object(fin_scoring, 'score', wraps=fin_scoring.score) as score:
            out = ec.evaluate({'инн': INN}, profile='нейтрально', now=NOW)
        self.assertEqual(score.call_count, 1)
        expected = ec._digest({'profiles': ec._resource('profiles_ru.json'),
                               'policy': ec._resource('evidence_policy.json'),
                               'canon': ec._resource('canon_ru.json')})
        self.assertEqual(out['metadata']['rules_version'], expected)
        from evidence_pack import _версия
        self.assertEqual(out['metadata']['engine_version'], _версия())
        self.assertEqual(ec.validate_evidence(out['fetch']), [])

    def test_strict_direct_legacy_resolver(self):
        f = {'инн': INN, 'риски': {'массовый_руководитель': False},
             '_доступность': {'риски': {'состояние': 'ok'}}}
        result = profiles.resolve(f, None, 'предоплата')
        fact = next(c for c in result['факты'] if c['сигнал'] == 'массовый_руководитель')
        self.assertEqual(fact['статус'], 'не проверен')
        self.assertFalse(result['_decision']['can_recommend'])

    def test_missing_required_coverage_and_override(self):
        f = fetch_clean()
        now = max(c['retrieved_at'] for c in f['_evidence']['claims'].values())
        complete = ec.evaluate(f, profile='отсрочка', now=now)
        self.assertTrue(complete['metadata']['decision']['can_recommend'])
        f['риски'].pop('массовый_руководитель')
        partial = ec.evaluate(f, profile='отсрочка', now=now)
        coverage = partial['metadata']['decision']['coverage']
        self.assertEqual(coverage['numerator'], coverage['denominator'] - 1)
        self.assertFalse(partial['metadata']['decision']['can_recommend'])
        f['финансы']['отчётность_по_годам'] = []
        override = ec.evaluate(f, profile='клиент_115фз', now=now)
        self.assertIn('транзитный_профиль', override['metadata']['decision']['coverage']['required_rules'])

    def test_verified_form_gate_and_red_capital(self):
        from run_profiles_eval import synthetic_verified
        for form in ('full', 'simplified', 'bank', 'npo', 'restricted', None):
            with self.subTest(form=form):
                f = fetch_clean()
                f['финансы']['форма_отчётности'] = form
                for row in f['финансы']['отчётность_по_годам']:
                    row['строки']['1300'] = -100
                f = synthetic_verified(f)
                now = max(c['retrieved_at'] for c in f['_evidence']['claims'].values())
                result = ec.evaluate(f, profile='отсрочка', now=now)
                math = result['fin']['оценки_сигналов']['ча_отрицательные_2года']['статус']
                self.assertEqual(math, 'найден')
                fact = next(c for c in result['assessment']['факты'] if c['сигнал'] == 'ча_отрицательные_2года')
                self.assertEqual(fact['статус'], 'найден' if form in ('full', 'simplified') else 'не проверен')
                if form in ('full', 'simplified'):
                    self.assertEqual(result['assessment']['светофор'], '🔴')

    def test_header_only_secret_cannot_persist_or_fake_raw(self):
        import evidence_pack as ep
        import fetch_counterparty as fc
        secret = 'HEADER_ONLY_CREDENTIAL_SENTINEL'
        with mock.patch.dict(fc.os.environ, {'DADATA_API_KEY': secret}), fc.capture_session(INN) as cap:
            fc._ИСТОЧНИК.id = 'риски'
            fc._ИСТОЧНИК.subject_inn = INN
            fc._записать('GET', 'https://example.test', 403,
                        json.dumps({'error': 'provider echoed ' + secret}).encode())
            # A detached detail response carries no INN and gets no locator.
            fc._записать('GET', 'https://example.test/detail', 200,
                        json.dumps({'staff': 3, '_token_pb': 'pb-secret'}).encode())
        refs = cap.raw_refs({'инн': INN, 'риски': {'численность_сотрудников': 3}})
        self.assertFalse(refs['fields'])
        f = ec.enrich({'инн': INN, 'риски': {'численность_сотрудников': 3}},
                      now=next(iter(refs['responses'].values()))['retrieved_at'], raw_refs=refs)
        c = next(c for c in f['_evidence']['claims'].values() if c['field'] == 'риски.численность_сотрудников')
        self.assertEqual(c['raw_ref']['availability'], 'unmapped_locator')
        with tempfile.TemporaryDirectory() as directory:
            ep.записать_пакет(directory, INN, dict(f, error=secret), cap.responses, secrets=cap.secrets)
            self.assertTrue(ep.проверить(directory)['годен'])
            for file in Path(directory).rglob('*'):
                if file.is_file():
                    self.assertNotIn(secret.encode(), file.read_bytes())
                    self.assertNotIn(b'pb-secret', file.read_bytes())
            index = json.loads((Path(directory) / 'raw/index.json').read_text())
            for record in index:
                saved = (Path(directory) / record['файл']).read_bytes()
                self.assertEqual(record['sha256'], hashlib.sha256(saved).hexdigest())
                self.assertIn(record['capture_format'], ('sanitized_json_v1', 'withheld_non_json_v1'))

    def test_four_worker_capture_isolation_and_context_deadlines(self):
        import fetch_counterparty as fc
        barrier = threading.Barrier(4)
        def worker(number):
            inn = INN if number % 2 else '504110181262'
            with fc.capture_session(inn) as cap:
                fc._ИСТОЧНИК.id = 'риски'
                fc._ИСТОЧНИК.subject_inn = inn
                started = fc.time.monotonic() - (number + 1)
                fc._RUN_START.set(started)
                barrier.wait(timeout=5)
                if number == 0:
                    fc.reset_deadline()
                barrier.wait(timeout=5)
                elapsed = fc.DEADLINE - fc._time_left()
                fc._записать('GET', 'https://example.test?inn=' + inn, 200,
                            json.dumps({'inn': inn, 'staff': number}).encode())
                return cap, elapsed
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(worker, range(4)))
        ids = set()
        for number, (cap, elapsed) in enumerate(results):
            self.assertEqual(len(cap.responses), 1)
            record = cap.responses[0]
            self.assertEqual(json.loads(record['данные'])['inn'], cap.subject_inn)
            self.assertNotIn(record['response_id'], ids)
            ids.add(record['response_id'])
            self.assertGreaterEqual(elapsed, number + 1 if number else 0)
            self.assertLess(elapsed, number + 2 if number else 1)

    def test_worker_pool_propagates_the_capture_context(self):
        import fetch_counterparty as fc
        sources = ['егрюл', 'риски', 'финансы', 'мсп']
        def run_source(sid, inn, **kwargs):
            fc._ИСТОЧНИК.id, fc._ИСТОЧНИК.subject_inn = sid, inn
            fc._записать('GET', 'https://example.test?inn=' + inn, 200,
                        json.dumps({'inn': inn, 'source': sid}).encode())
            return {'source': sid}, {'состояние': 'ok'}
        with fc.capture_session(INN) as cap, mock.patch.object(fc, 'run_source', side_effect=run_source), mock.patch.object(fc, '_sequential', return_value=False), mock.patch.object(fc, '_thread_opener', return_value=None):
            out = fc._run_wave(sources, INN, None, {}, None)
        self.assertEqual(len(out), 4)
        self.assertEqual({r['источник'] for r in cap.responses}, set(sources))
        self.assertTrue(all(r['subject_inn'] == INN for r in cap.responses))


    def test_response_callback_copy_and_failure_are_visible(self):
        import fetch_counterparty as fc
        saved = []
        def writer(record):
            saved.append(record)
            record['данные'] = b'changed-by-callback'
        with fc.capture_session(INN, secrets=('sentinel',), on_response=writer) as cap:
            fc._ИСТОЧНИК.id, fc._ИСТОЧНИК.subject_inn = 'риски', INN
            fc._записать('GET', 'https://example.test', 200, b'{"error":"sentinel"}')
        self.assertEqual(len(saved), 1)
        self.assertNotIn(b'sentinel', cap.responses[0]['данные'])
        self.assertNotEqual(saved[0]['данные'], cap.responses[0]['данные'])
        def fail(record):
            raise OSError('disk full sentinel')
        with self.assertRaisesRegex(fc.CaptureWriteError, '^capture_callback_failed$'), fc.capture_session(INN, on_response=fail):
            fc._записать('GET', 'https://example.test', 200, b'{}')

    def test_source_progress_uses_completion_order_and_failure_propagates(self):
        import fetch_counterparty as fc
        completed = []
        release = threading.Event()
        def run_source(sid, inn, **kwargs):
            if sid == 'егрюл':
                self.assertTrue(release.wait(timeout=3))
            return {'source': sid, '_token_pb': 'secret'}, {'состояние': 'ok'}
        def callback(sid, data, availability):
            completed.append(sid)
            self.assertNotIn('_token_pb', data)
            if sid == 'риски':
                release.set()
            data['source'] = 'mutated-copy'
        with fc.capture_session(INN):
            token = fc._ON_SOURCE.set(callback)
            try:
                with mock.patch.object(fc, 'run_source', side_effect=run_source), mock.patch.object(fc, '_sequential', return_value=False), mock.patch.object(fc, '_thread_opener', return_value=None):
                    out = fc._run_wave(['егрюл', 'риски'], INN, None, {}, None)
            finally:
                fc._ON_SOURCE.reset(token)
        self.assertEqual(completed, ['риски', 'егрюл'])
        self.assertEqual(out[0][1][0]['source'], 'егрюл')
        def failure(sid, data, availability):
            raise OSError('disk full')
        with fc.capture_session(INN):
            token = fc._ON_SOURCE.set(failure)
            try:
                with mock.patch.object(fc, 'run_source', return_value=({}, {})), mock.patch.object(fc, '_make_opener', return_value=None), self.assertRaisesRegex(OSError, 'disk full'):
                    fc._run_wave(['егрюл'], INN, None, {}, None)
            finally:
                fc._ON_SOURCE.reset(token)

    def test_materialized_renderer_uses_stored_revision_without_recompute(self):
        f = fetch_clean()
        now = max(c['retrieved_at'] for c in f['_evidence']['claims'].values())
        result = ec.evaluate(f, profile='отсрочка', now=now)
        meta = {'версия': 'saved-version', 'сформирован': now, 'хеш': 'saved-input-hash'}
        kwargs = {'fin': result['fin'], 'вердикт': result['assessment'], 'materialized': True,
                  'профиль_вердиктный': True}
        before = dossier.в_markdown(dossier.собрать(result['fetch'], meta, **kwargs))
        resources_before = ec._resource
        def changed_rules(filename):
            resource = json.loads(json.dumps(resources_before(filename)))
            resource['test_future_rule_revision'] = '2050-01-01'
            return resource
        with mock.patch.object(ec, 'prepare', side_effect=AssertionError('must not prepare')), mock.patch.object(fin_scoring, 'score', side_effect=AssertionError('must not score')), mock.patch.object(profiles, 'resolve', side_effect=AssertionError('must not resolve')), mock.patch.object(ec, '_EVALUATION_NOW', new=mock.Mock()), mock.patch.object(ec, '_resource', side_effect=changed_rules):
            after = dossier.в_markdown(dossier.собрать(result['fetch'], meta, **kwargs))
        self.assertEqual(after, before)
        bad = json.loads(json.dumps(result['fetch']))
        bad['_evidence']['schema_version'] = 2
        with self.assertRaisesRegex(ValueError, 'unsupported_schema'):
            dossier.собрать(bad, meta, **kwargs)
        with self.assertRaisesRegex(ValueError, 'decision_mismatch'):
            dossier.собрать(result['fetch'], meta, fin=result['fin'], materialized=True)


    def test_cli_json_is_utf8_under_non_utf8_default(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fetch.json'
            path.write_text(json.dumps({'инн': INN}, ensure_ascii=False), encoding='utf-8')
            env = dict(os.environ, PYTHONIOENCODING='cp1252', PYTHONUTF8='0')
            for script, args in (('profiles.py', ['--профиль', 'нейтрально', '--fetch', str(path)]),
                                 ('fin_scoring.py', [str(path)])):
                with self.subTest(script=script):
                    run = subprocess.run([sys.executable, str(ROOT / 'scripts' / script), *args],
                                         env=env, capture_output=True, timeout=20, check=False)
                    self.assertEqual(run.returncode, 0, run.stderr.decode('utf-8', 'replace'))
                    payload = json.loads(run.stdout.decode('utf-8'))
                    self.assertIsInstance(payload, dict)
                    self.assertNotIn(b'UnicodeEncodeError', run.stderr)


    def test_captured_records_remain_immutable_when_later_secrets_are_discovered(self):
        import evidence_pack as ep
        import fetch_counterparty as fc
        saved = []
        with fc.capture_session(INN, on_response=lambda record: saved.append(record)) as cap:
            fc._ИСТОЧНИК.id, fc._ИСТОЧНИК.subject_inn = 'егрюл', INN
            fc._записать('POST', 'https://example.test', 200, b'{"message":"future-unmarked-string"}')
            fc._записать('POST', 'https://example.test', 200, b'{"t":"future-unmarked-string"}')
            fc._записать('GET', 'https://example.test/search-result/future-unmarked-string', 200, b'{}')
        before = cap.responses[0]['данные']
        refs = cap.raw_refs({'инн': INN})
        self.assertEqual(cap.responses[0]['данные'], before)
        for record, durable in zip(cap.responses, saved):
            self.assertEqual(record['данные'], durable['данные'])
            self.assertEqual(refs['responses'][record['response_id']]['sha256'], record['sha256'])
        self.assertNotIn('future-unmarked-string', saved[2]['url'])
        # A later supplied explicit secret cannot silently mutate the first saved response.
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(ValueError, 'captured_bytes_redaction_mismatch'):
            ep.записать_пакет(directory, INN, {'инн': INN}, cap.responses, secrets=cap.secrets)


if __name__ == '__main__':
    unittest.main()
