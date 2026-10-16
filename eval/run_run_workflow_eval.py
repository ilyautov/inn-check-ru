#!/usr/bin/env python3
"""Persisted workflow checks using recorded source bytes and temporary stores."""
import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import fetch_counterparty as collector
import manual_block
import run_store

workflow = __import__('run_workflow') if importlib.util.find_spec('run_workflow') else None
INN = '7707083893'
NOW = '2026-10-16T09:00:00Z'
RAW = (ROOT / 'eval/fixtures/net/егрюл_7707083893.raw.json').read_bytes()


def recorded_collect(inn, **kwargs):
    collector._ИСТОЧНИК.id = 'егрюл'
    collector._ИСТОЧНИК.subject_inn = inn
    collector._записать('GET', 'https://egrul.nalog.ru/', 200, RAW)
    block, availability = collector.parse_egrul(json.loads(RAW), inn)
    result = {'инн': inn, 'тип': 'ЮЛ', 'профиль': 'нейтрально', 'егрюл': block,
              '_доступность': {'егрюл': availability}}
    if kwargs.get('on_source'):
        kwargs['on_source']('егрюл', copy.deepcopy(block), copy.deepcopy(availability))
    return result


class WorkflowEval(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(workflow, 'persisted workflow module is required')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / 'runs'

    def start(self):
        with patch.object(collector, 'collect', side_effect=recorded_collect):
            result = workflow.start(INN, root=self.root)
        self.assertNotEqual(result['status'], 'error', result)
        return result

    def block(self, **changes):
        block = manual_block.собрать('суды', INN, 'https://kad.arbitr.ru/',
                                    NOW[:10], 'проверено', {'ответчик_крупные': False})
        block.update(changes)
        return {'суды': block}

    def cli(self, *args):
        env = dict(os.environ, PYTHONPATH=str(ROOT / 'scripts'), PYTHONDONTWRITEBYTECODE='1')
        proc = subprocess.run([sys.executable, '-m', 'workflow_cli', '--store', str(self.root),
                               *args, '--json'], env=env, text=True, capture_output=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        return json.loads(proc.stdout)

    def test_start_persists_before_network_raw_hash_and_readonly_restart(self):
        def check_collect(inn, **kwargs):
            runs = list(self.root.glob('*/HEAD.json'))
            self.assertEqual(len(runs), 1)
            record = run_store.read(runs[0].parent.name, root=self.root)
            self.assertEqual(record['workflow_status'], 'collecting')
            return recorded_collect(inn, **kwargs)
        with patch.object(collector, 'collect', side_effect=check_collect):
            started = workflow.start(INN, root=self.root)
        self.assertNotEqual(started['status'], 'error', started)
        rid = started['run_id']
        self.assertGreaterEqual(started['revision'], 2)
        refs = started['data']['metadata']['raw_refs']['responses']
        self.assertTrue(refs)
        for value in refs.values():
            raw = run_store.read_raw(rid, value['sha256'], root=self.root)
            self.assertEqual(hashlib.sha256(raw).hexdigest(), value['sha256'])
            self.assertNotIn('body', value)
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with patch.object(collector, 'collect', side_effect=AssertionError('network')), \
                patch('urllib.request.urlopen', side_effect=AssertionError('network')):
            self.assertEqual(workflow.resume(rid, root=self.root)['data'], started['data'])
            rendered = workflow.render(rid, root=self.root)
        self.assertEqual(rendered['data']['source_revision'], started['revision'])
        self.assertIn(INN, rendered['data']['content'])
        self.assertEqual(before, {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        self.assertEqual(self.cli('run', 'resume', rid)['data'], started['data'])

    def test_partial_operations_preserve_unreplaced_acquisition(self):
        from datetime import datetime, timedelta

        def fixture(inn, **kwargs):
            result = recorded_collect(inn, **kwargs)
            result['мсп'] = {'статус_мсп': 'микропредприятие'}
            result['_доступность']['мсп'] = {'состояние': 'ok'}
            kwargs['on_source']('мсп', result['мсп'], result['_доступность']['мсп'])
            return result

        def claim(result, field):
            return next(c for c in result['data']['fetch']['_evidence']['claims'].values()
                        if c['field'] == field)

        for operation in ('manual', 'unrelated_refresh', 'same_source_refresh'):
            with self.subTest(operation=operation):
                with patch.object(collector, 'collect', side_effect=fixture):
                    before = workflow.start(INN, root=self.root)
                self.assertNotEqual(before['status'], 'error', before)
                original = claim(before, 'мсп.статус_мсп')
                old_record = run_store.read(before['run_id'], root=self.root)
                later = (datetime.fromisoformat(original['retrieved_at'].replace('Z', '+00:00'))
                         + timedelta(hours=1)).isoformat().replace('+00:00', 'Z')
                selected = 'мсп' if operation == 'same_source_refresh' else 'риски'
                def source(sid, inn, selected=selected, **kwargs):
                    self.assertEqual(sid, selected)
                    return ({'статус_мсп': 'микропредприятие'}, {'состояние': 'ok'}) if sid == 'мсп' else (None, {'состояние': 'не проверено'})
                with patch.object(workflow, '_now', return_value=later), \
                     patch.object(collector, 'collect', side_effect=AssertionError('unexpected collect')), \
                     patch.object(collector, 'run_source', side_effect=source) as calls:
                    options = {'expected_revision': before['revision'], 'operation_id': str(uuid.uuid4()), 'root': self.root}
                    after = (workflow.import_manual(before['run_id'], self.block(), **options)
                             if operation == 'manual' else workflow.refresh(before['run_id'], [selected], **options))
                self.assertNotEqual(after['status'], 'error', after)
                updated = claim(after, 'мсп.статус_мсп')
                self.assertEqual(original['claim_id'], updated['claim_id'])
                self.assertEqual(calls.call_count, 0 if operation == 'manual' else 1)
                for name in ('retrieved_at', 'first_observed_at'):
                    self.assertEqual(updated[name], later if operation == 'same_source_refresh' else original[name])
                self.assertEqual(updated['raw_ref'], original['raw_ref'])
                self.assertNotEqual(updated['raw_ref']['availability'], 'available')
                self.assertEqual(updated['knowledge'], original['knowledge'])
                self.assertEqual(updated['coverage_complete'], original['coverage_complete'])
                self.assertEqual(after['data']['fetch'].get('_provider_policy'), before['data']['fetch'].get('_provider_policy'))
                self.assertEqual(run_store.read(before['run_id'], revision=before['revision'], root=self.root), old_record)

    def test_retained_acquisition_does_not_restore_expired_knowledge_or_derived_state(self):
        from datetime import datetime, timedelta, timezone
        one = self.start()
        moment = datetime.now(timezone.utc)
        imported_at = moment.isoformat().replace('+00:00', 'Z')
        expiry = (moment + timedelta(minutes=30)).isoformat().replace('+00:00', 'Z')
        later = (moment + timedelta(hours=1)).isoformat().replace('+00:00', 'Z')
        blocks = self.block(ответчик_крупные=True, pagination_complete=True,
                            параметры_поиска='ИНН ' + INN, expires_at=expiry)
        with patch.object(workflow, '_now', return_value=imported_at):
            before = workflow.import_manual(one['run_id'], blocks, expected_revision=one['revision'],
                                             operation_id=str(uuid.uuid4()), root=self.root)
        self.assertNotEqual(before['status'], 'error', before)
        def source(sid, inn, **kwargs):
            self.assertEqual(sid, 'риски')
            return None, {'состояние': 'не проверено'}
        with patch.object(workflow, '_now', return_value=later), \
             patch.object(collector, 'run_source', side_effect=source):
            after = workflow.refresh(before['run_id'], ['риски'], expected_revision=before['revision'],
                                     operation_id=str(uuid.uuid4()), root=self.root)
        self.assertNotEqual(after['status'], 'error', after)
        def court(result):
            return next(c for c in result['data']['fetch']['_evidence']['claims'].values()
                        if c['field'] == 'суды.ответчик_крупные')
        self.assertEqual(court(before)['knowledge'], 'known')
        self.assertEqual(court(after)['knowledge'], 'unknown')
        self.assertEqual(court(after)['reason_code'], 'stale')
        self.assertEqual(court(after)['retrieved_at'], court(before)['retrieved_at'])
        self.assertEqual(court(after)['valid_until'], expiry)
        expected = workflow.evidence_contract.evaluate(after['data']['fetch'], profile='нейтрально', now=later)
        self.assertEqual(after['data']['fin'], expected['fin'])
        self.assertEqual(after['data']['assessment'], expected['assessment'])
        current_claims = after['data']['fetch']['_evidence']['claims']
        for cid, claim in current_claims.items():
            if claim['relation'] == 'derived':
                self.assertEqual(claim, expected['fetch']['_evidence']['claims'][cid])

    def test_retention_requires_full_candidate_and_source_binding(self):
        before = self.start()['data']['fetch']
        claim = next(c for c in before['_evidence']['claims'].values()
                     if c['field'] == 'егрюл.дата_регистрации')
        # These properties are deliberately absent from the claim ID. A shared
        # ID cannot authorize reusing acquisition metadata after a scope change.
        for key, replacement in (('valid_until', '2099-01-01T00:00:00Z'),
                                 ('coverage_complete', True), ('origin_family', 'changed'),
                                 ('search_scope', {'query': 'changed'})):
            payload = {'fetch': copy.deepcopy(before)}
            candidate = payload['fetch']['_evidence']['claims'][claim['claim_id']]
            candidate[key] = replacement
            candidate['retrieved_at'] = NOW
            workflow._retain_acquisition(payload, before, set())
            self.assertEqual(candidate['retrieved_at'], NOW, key)
        for key in ('егрюл', '_доступность', '_evidence_scope'):
            payload = {'fetch': copy.deepcopy(before)}
            if key == 'егрюл':
                payload['fetch'][key]['changed'] = True
            elif key == '_доступность':
                payload['fetch'][key]['егрюл'] = {'состояние': 'не проверено'}
            else:
                payload['fetch'].setdefault(key, {})[claim['field']] = {'coverage_complete': True}
            candidate = payload['fetch']['_evidence']['claims'][claim['claim_id']]
            candidate['retrieved_at'] = NOW
            workflow._retain_acquisition(payload, before, set())
            self.assertEqual(candidate['retrieved_at'], NOW, key)

    def test_manual_retry_cas_subject_and_incomplete(self):
        one = self.start()
        rid, revision = one['run_id'], one['revision']
        op = str(uuid.uuid4())
        blocks = self.block(pagination_complete=True)
        with patch.object(collector, 'collect', side_effect=AssertionError('network')), \
                patch('urllib.request.urlopen', side_effect=AssertionError('network')):
            two = workflow.import_manual(rid, blocks, expected_revision=revision, operation_id=op, root=self.root)
            retry = workflow.import_manual(rid, blocks, expected_revision=revision, operation_id=op, root=self.root)
        self.assertEqual(two, retry)
        self.assertEqual(two['revision'], revision + 1, two)
        self.assertFalse(any(a['kind'] == 'manual' and a['source_id'] == 'суды' for a in two['pending_actions']))
        self.assertEqual(two['data']['metadata']['raw_refs'], one['data']['metadata']['raw_refs'])
        conflict = workflow.import_manual(rid, self.block(найдено='changed'), expected_revision=revision,
                                          operation_id=op, root=self.root)
        self.assertEqual(conflict['error']['code'], 'operation_conflict')
        wrong = workflow.import_manual(rid, self.block(инн='7728168971'), expected_revision=two['revision'],
                                       operation_id=str(uuid.uuid4()), root=self.root)
        self.assertEqual(wrong['status'], 'error')
        stale = workflow.import_manual(rid, blocks, expected_revision=revision,
                                       operation_id=str(uuid.uuid4()), root=self.root)
        self.assertEqual(stale['error']['code'], 'revision_conflict')
        for outcome in ('declined', 'incomplete'):
            blocks = self.block(outcome=outcome, reason='Проверка не завершена')
            two = workflow.import_manual(rid, blocks, expected_revision=two['revision'],
                                         operation_id=str(uuid.uuid4()), root=self.root)
            self.assertNotEqual(two['status'], 'error', two)
            self.assertNotEqual(two['data']['fetch']['_доступность']['суды']['состояние'], 'ok')
            self.assertTrue(any(a['source_id'] == 'суды' for a in two['pending_actions']))

    def test_manual_rejects_url_expired_and_execution_fields(self):
        one = self.start()
        for changes in ({'url': 'https://kad.arbitr.ru.evil.example/'}, {'url': 'https://x@kad.arbitr.ru/'},
                        {'expires_at': '2020-01-01T00:00:00Z'}, {'дата_проверки': '2099-01-01'}, {'root': '/tmp/evil'},
                        {'command': 'touch /tmp/evil'}, {'параметры_поиска': 'ИНН 7728168971'}):
            result = workflow.import_manual(one['run_id'], self.block(**changes),
                expected_revision=one['revision'], operation_id=str(uuid.uuid4()), root=self.root)
            self.assertEqual(result['status'], 'error', result)

    def test_export_atomic_registry_snapshot_and_original_immutable(self):
        one = self.start()
        rid = one['run_id']
        original = run_store.read(rid, root=self.root)
        for fmt in ('markdown', 'json', 'docx'):
            exported = workflow.export(rid, format=fmt, root=self.root)
            self.assertEqual(exported['status'], 'ok', exported)
            info = exported['data']
            blob = run_store.read_artifact(rid, info['artifact_id'], root=self.root)
            self.assertEqual(hashlib.sha256(blob).hexdigest(), info['sha256'])
            self.assertEqual(info['source_revision'], one['revision'])
            if fmt == 'docx':
                self.assertTrue(blob.startswith(b'PK'))
        snap = workflow.save_snapshot(rid, root=self.root)
        self.assertEqual(snap['status'], 'ok', snap)
        content = json.loads(run_store.read_artifact(rid, snap['data']['artifact_id'], root=self.root))
        self.assertEqual(content['инн'], INN)
        self.assertEqual(run_store.read(rid, root=self.root), original)
        self.assertEqual(len(run_store.list_artifacts(rid, root=self.root)), 4)
        if os.name == 'posix':
            self.assertTrue(all(p.stat().st_mode & 0o077 == 0 for p in self.root.rglob('*') if p.is_file()))

    def test_cancel_and_interrupted_never_recollect(self):
        with patch.object(collector, 'collect', side_effect=KeyboardInterrupt):
            cancelled = workflow.start(INN, root=self.root)
        self.assertEqual(cancelled['status'], 'cancelled', cancelled)
        self.assertEqual(workflow.resume(cancelled['run_id'], root=self.root)['status'], 'cancelled')
        payload = copy.deepcopy(cancelled['data'])
        payload['metadata']['workflow_status'] = 'collecting'
        interrupted = run_store.create(payload, root=self.root)
        with patch.object(collector, 'collect', side_effect=AssertionError('network')):
            result = workflow.resume(interrupted['run_id'], root=self.root)
        self.assertEqual(result['status'], 'partial')
        self.assertIn('interrupted', [w['code'] for w in result['warnings']])

    def test_selected_refresh_dependency_closure_retry_and_preserves_unselected_raw(self):
        one = self.start()
        called = []
        def source(sid, inn, **kwargs):
            called.append(sid)
            return {'fixture': sid}, {'состояние': 'не проверено', 'причина': 'fixture missing'}
        op = str(uuid.uuid4())
        with patch.object(collector, 'collect', side_effect=AssertionError('full collect forbidden')), \
                patch.object(collector, 'run_source', side_effect=source):
            refreshed = workflow.refresh(one['run_id'], ['спецреестры'], expected_revision=one['revision'],
                                          operation_id=op, root=self.root)
            self.assertNotEqual(refreshed['status'], 'error', refreshed)
            first_calls = list(called)
            retry = workflow.refresh(one['run_id'], ['спецреестры'], expected_revision=one['revision'],
                                      operation_id=op, root=self.root)
        self.assertEqual(refreshed, retry)
        self.assertEqual(called, first_calls)
        self.assertEqual(called, workflow._selected(['спецреестры']))
        self.assertEqual(refreshed['data']['metadata']['refreshed_sources'], called)
        # A second refresh of an unrelated source must retain original ЕГРЮЛ evidence.
        original = one['data']['metadata']['raw_refs']['responses']
        self.assertTrue(set(original) <= set(refreshed['data']['metadata']['raw_refs']['responses']))
        with patch.object(collector, 'run_source', side_effect=AssertionError('no call on conflict')):
            conflict = workflow.refresh(one['run_id'], ['риски'], expected_revision=one['revision'],
                                         operation_id=op, root=self.root)
        self.assertEqual(conflict['error']['code'], 'operation_conflict')

    def test_cancel_after_completed_source_retains_partial_fetch_and_capture(self):
        def cancel(inn, **kwargs):
            recorded_collect(inn, **kwargs)
            raise KeyboardInterrupt
        with patch.object(collector, 'collect', side_effect=cancel):
            result = workflow.start(INN, root=self.root)
        self.assertEqual(result['status'], 'cancelled', result)
        self.assertTrue(result['data']['fetch']['егрюл'])
        self.assertTrue(result['data']['metadata']['raw_refs']['responses'])
        self.assertGreaterEqual(result['revision'], 3)

    def test_separate_processes_start_manual_resume_render_snapshot(self):
        env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT / 'scripts'), str(ROOT / 'eval')]),
                   PYTHONDONTWRITEBYTECODE='1')
        code = """import json,sys; from pathlib import Path
from unittest.mock import patch
from run_run_workflow_eval import collector, recorded_collect, INN
import run_workflow
with patch.object(collector, 'collect', side_effect=recorded_collect):
 print(json.dumps(run_workflow.start(INN, root=Path(sys.argv[1])), ensure_ascii=False))
"""
        process = subprocess.run([sys.executable, '-c', code, str(self.root)], env=env,
                                  text=True, capture_output=True, check=False)
        self.assertEqual(process.returncode, 0, process.stderr)
        one = json.loads(process.stdout)
        path = Path(self.temp.name) / 'manual.json'
        path.write_text(json.dumps(self.block(), ensure_ascii=False), encoding='utf-8')
        op = str(uuid.uuid4())
        two = self.cli('run', 'manual', one['run_id'], '--block-json', str(path),
                       '--expected-revision', str(one['revision']), '--operation-id', op)
        self.assertEqual(two['run_id'], one['run_id'])
        self.assertEqual(self.cli('run', 'resume', one['run_id'])['data'], two['data'])
        self.assertEqual(self.cli('run', 'render', one['run_id'])['data']['source_revision'], two['revision'])
        self.assertEqual(self.cli('run', 'snapshot', one['run_id'])['data']['source_revision'], two['revision'])
        self.assertEqual(self.cli('run', 'manual', one['run_id'], '--block-json', str(path),
                                  '--expected-revision', str(one['revision']), '--operation-id', op), two)

    def test_render_uses_materialized_payload_not_current_evaluator(self):
        one = self.start()
        with patch('evidence_contract.evaluate', side_effect=AssertionError('rescore')), \
                patch('evidence_contract.prepare', side_effect=AssertionError('reenrich')), \
                patch('urllib.request.urlopen', side_effect=AssertionError('network')), \
                patch('socket.socket.connect', side_effect=AssertionError('network')):
            result = workflow.render(one['run_id'], root=self.root)
        self.assertEqual(result['status'], 'ok', result)

    def test_stale_claim_offers_refresh_without_re_evaluation(self):
        one = self.start()
        payload = copy.deepcopy(one['data'])
        claim = next(c for c in payload['fetch']['_evidence']['claims'].values() if c['source_id'] == 'егрюл')
        claim['valid_until'] = '2000-01-01T00:00:00Z'
        changed = run_store.append(one['run_id'], payload, expected_revision=one['revision'],
                                   operation_id=str(uuid.uuid4()), root=self.root)
        with patch.object(collector, 'collect', side_effect=AssertionError('network')):
            result = workflow.resume(one['run_id'], root=self.root)
        self.assertEqual(result['revision'], changed['revision'])
        self.assertTrue(any(a['kind'] == 'refresh' and a['source_id'] == 'егрюл' for a in result['pending_actions']))

    def test_read_detects_corrupt_raw_capture(self):
        one = self.start()
        refs = one['data']['metadata']['raw_refs']['responses']
        response = next(iter(refs.values()))
        raw_path = self.root / one['run_id'] / 'raw' / response['sha256']
        raw_path.write_bytes(b'corrupted')
        result = workflow.resume(one['run_id'], root=self.root)
        self.assertEqual(result['status'], 'error', result)
        self.assertEqual(result['error']['code'], 'corrupt_store')

    def test_killed_process_retains_completed_sources_and_never_recollects(self):
        env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT / 'scripts'), str(ROOT / 'eval')]))
        code = """import os,sys; from pathlib import Path
from unittest.mock import patch
from run_run_workflow_eval import collector, recorded_collect, INN
import run_workflow
def killed(inn, **kwargs):
 recorded_collect(inn, **kwargs)
 os._exit(77)
with patch.object(collector, 'collect', side_effect=killed):
 run_workflow.start(INN, root=Path(sys.argv[1]))
"""
        process = subprocess.run([sys.executable, '-c', code, str(self.root)], env=env, capture_output=True, check=False)
        self.assertEqual(process.returncode, 77, process.stderr)
        run_id = next(self.root.glob('*/HEAD.json')).parent.name
        with patch.object(collector, 'collect', side_effect=AssertionError('recollect')):
            result = workflow.resume(run_id, root=self.root)
        self.assertEqual(result['status'], 'partial', result)
        self.assertTrue(result['data']['fetch']['егрюл'])
        self.assertTrue(result['data']['metadata']['raw_refs']['responses'])
        self.assertIn('interrupted', [w['code'] for w in result['warnings']])

    def test_hard_kill_during_source_preserves_response_identity_in_capture_journal(self):
        env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT / 'scripts'), str(ROOT / 'eval')]))
        code = """import os,sys; from pathlib import Path
from unittest.mock import patch
from run_run_workflow_eval import collector, RAW, INN
import run_workflow
def killed(inn, **kwargs):
 collector._ИСТОЧНИК.id = 'егрюл'
 collector._ИСТОЧНИК.subject_inn = inn
 collector._записать('GET', 'https://egrul.nalog.ru/', 200, RAW)
 os._exit(78)
with patch.object(collector, 'collect', side_effect=killed):
 run_workflow.start(INN, root=Path(sys.argv[1]))
"""
        process = subprocess.run([sys.executable, '-c', code, str(self.root)], env=env, capture_output=True, check=False)
        self.assertEqual(process.returncode, 78, process.stderr)
        run_id = next(self.root.glob('*/HEAD.json')).parent.name
        self.assertTrue(hasattr(run_store, 'captured_responses'), 'durable response metadata journal required')
        responses = run_store.captured_responses(run_id, root=self.root)
        self.assertEqual(len(responses), 1)
        self.assertTrue(responses[0]['response_id'].startswith('response-'))
        self.assertEqual(responses[0]['subject_inn'], INN)
        body = run_store.read_raw(run_id, responses[0]['sha256'], root=self.root)
        self.assertEqual(hashlib.sha256(body).hexdigest(), responses[0]['sha256'])
        resumed = workflow.resume(run_id, root=self.root)
        self.assertIn('unmaterialized_captures', [warning['code'] for warning in resumed['warnings']])

    def test_cli_utf8_redirected_output_errors_and_plan_parser(self):
        env = dict(os.environ, PYTHONPATH=str(ROOT / 'scripts'), PYTHONIOENCODING='cp1252')
        process = subprocess.run([sys.executable, '-m', 'workflow_cli', 'run', 'start', 'bad', '--json',
                                  '--store', str(self.root)], env=env, capture_output=True, check=False)
        self.assertEqual(process.returncode, 2)
        result = json.loads(process.stdout.decode('utf-8'))
        self.assertEqual(result['error']['code'], 'invalid_inn')
        self.assertFalse(self.root.exists())
        import cache_policy
        import workflow_cli
        report = cache_policy.plan('нейтрально', now=NOW, root=Path(self.temp.name) / 'cache')
        for action in report['actions']:
            args = workflow_cli.parser().parse_args(action['argv'][3:])
            self.assertEqual(args.command, 'cache')
            self.assertEqual(args.cache_command, 'update')
        with patch('cache_policy.execute', return_value={'errors': [], 'datasets': []}) as execute:
            args = workflow_cli.parser().parse_args(['cache', 'update', '--dataset', 'sanctions',
                        '--max-bytes', '1024', '--timeout-seconds', '1', '--profile', 'нейтрально'])
            result = workflow_cli._dispatch(args)
        self.assertEqual(result['status'], 'ok')
        execute.assert_called_once_with(['sanctions'], profile='нейтрально', max_bytes=1024, timeout_seconds=1.0)

    def test_concurrent_duplicate_refresh_makes_one_source_call(self):
        one = self.start()
        calls = Path(self.temp.name) / 'calls'
        env = dict(os.environ, PYTHONPATH=str(ROOT / 'scripts'))
        code = """import json,sys,time
from pathlib import Path
from unittest.mock import patch
import fetch_counterparty,run_workflow
def source(sid,inn,**kwargs):
 with open(sys.argv[5],'a',encoding='utf-8') as log: log.write(sid+'\\n')
 time.sleep(.2)
 return None,{'состояние':'не проверено','причина':'fixture unavailable'}
with patch.object(fetch_counterparty,'run_source',side_effect=source):
 result=run_workflow.refresh(sys.argv[2],['риски'],expected_revision=int(sys.argv[3]),operation_id=sys.argv[4],root=Path(sys.argv[1]))
 print(json.dumps(result,ensure_ascii=False))
"""
        argv = [sys.executable, '-c', code, str(self.root), one['run_id'], str(one['revision']),
                str(uuid.uuid4()), str(calls)]
        processes = [subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
        outputs = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, stderr)
            outputs.append(json.loads(stdout))
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[0]['revision'], one['revision'] + 3)
        self.assertEqual(calls.read_text(encoding='utf-8').splitlines(), ['риски'])

    def test_refresh_redacts_actual_configured_key_from_normalized_source_text(self):
        one = self.start()
        secret = 'workflow-refresh-secret-813'
        with patch.dict(os.environ, {'CHECKO_API_KEY': '  ' + secret + '\n'}), \
                patch.object(collector, 'run_source', return_value=({'note': secret}, {'состояние': 'ok'})):
            result = workflow.refresh(one['run_id'], ['риски'], expected_revision=one['revision'],
                                       operation_id=str(uuid.uuid4()), root=self.root)
        self.assertNotEqual(result['status'], 'error', result.get('error'))
        self.assertTrue(secret not in json.dumps(result), 'configured credential leaked into envelope')
        for path in self.root.rglob('*'):
            if path.is_file():
                self.assertNotIn(secret.encode(), path.read_bytes(), str(path))

    def test_invalid_start_does_not_create_store(self):
        for kwargs in ({'profile': 'invalid'}, {'mode': 'invalid'}, {}):
            result = workflow.start('bad' if not kwargs else INN, root=self.root, **kwargs)
            self.assertEqual(result['status'], 'error')
        self.assertFalse(self.root.exists())

    def test_refresh_kill_checkpoints_completed_source_and_resumes_same_operation(self):
        self._killed_refresh(after_completed=True)

    def test_refresh_kill_before_parse_surfaces_unmaterialized_capture(self):
        self._killed_refresh(after_completed=False)

    def _killed_refresh(self, *, after_completed):
        one = self.start()
        op = str(uuid.uuid4())
        env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT / 'scripts'), str(ROOT / 'eval')]))
        code = """import os,sys
from pathlib import Path
from unittest.mock import patch
from run_run_workflow_eval import collector, RAW
import run_workflow
def source(sid,inn,**kwargs):
 collector._ИСТОЧНИК.id = sid
 collector._ИСТОЧНИК.subject_inn = inn
 collector._записать('GET','https://egrul.nalog.ru/',200,RAW)
 if sid == 'риски': os._exit(79)
 if sys.argv[5] == 'before_parse': os._exit(79)
 data,av = collector.parse_egrul(__import__('json').loads(RAW),inn)
 data['workflow_review_marker']='completed-before-kill'
 return data,av
with patch.object(collector,'run_source',side_effect=source):
 run_workflow.refresh(sys.argv[2],['егрюл','риски'],expected_revision=int(sys.argv[3]),operation_id=sys.argv[4],root=Path(sys.argv[1]))
"""
        process = subprocess.run([sys.executable, '-c', code, str(self.root), one['run_id'],
            str(one['revision']), op, 'after_completed' if after_completed else 'before_parse'],
            env=env, capture_output=True, check=False)
        self.assertEqual(process.returncode, 79, process.stderr)
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with patch.object(collector, 'run_source', side_effect=AssertionError('resume must not dispatch')):
            resumed = workflow.resume(one['run_id'], root=self.root)
        self.assertIn('interrupted', [w['code'] for w in resumed['warnings']])
        self.assertEqual(before, {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        if after_completed:
            self.assertEqual(resumed['data']['fetch']['егрюл']['workflow_review_marker'], 'completed-before-kill')
            self.assertEqual(len(resumed['data']['metadata']['raw_refs']['responses']), 2)
            self.assertIn('unmaterialized_captures', [w['code'] for w in resumed['warnings']])
        else:
            self.assertIn('unmaterialized_captures', [w['code'] for w in resumed['warnings']])
        called = []
        def finish(sid, inn, **kwargs):
            called.append(sid)
            return None, {'состояние': 'не проверено'}
        with patch.object(collector, 'run_source', side_effect=finish):
            done = workflow.refresh(one['run_id'], ['егрюл', 'риски'], expected_revision=one['revision'],
                                    operation_id=op, root=self.root)
            again = workflow.refresh(one['run_id'], ['егрюл', 'риски'], expected_revision=one['revision'],
                                     operation_id=op, root=self.root)
        self.assertNotEqual(done['status'], 'error', done)
        self.assertEqual(done, again)
        self.assertEqual(called, ['риски'] if after_completed else ['егрюл', 'риски'])
        self.assertIn('unmaterialized_captures', [w['code'] for w in workflow.resume(one['run_id'], root=self.root)['warnings']])

    def test_complete_head_does_not_hide_unmaterialized_captures(self):
        one = self.start()
        with collector.capture_session(INN, on_response=lambda r: run_store.put_capture(one['run_id'], r, root=self.root)):
            recorded_collect(INN)
        resumed = workflow.resume(one['run_id'], root=self.root)
        self.assertIn('unmaterialized_captures', [w['code'] for w in resumed['warnings']])

    def test_manual_full_negative_retains_declared_provenance_and_completeness(self):
        one = self.start()
        blocks = self.block(pagination_complete=True, параметры_поиска='ИНН ' + INN,
                            дата_проверки='2026-10-01')
        result = workflow.import_manual(one['run_id'], blocks, expected_revision=one['revision'],
                                        operation_id=str(uuid.uuid4()), root=self.root)
        self.assertNotEqual(result['status'], 'error', result)
        claims = result['data']['fetch']['_evidence']['claims'].values()
        claim = next(c for c in claims if c['field'] == 'суды.ответчик_крупные')
        self.assertEqual(claim['knowledge'], 'known', claim)
        self.assertEqual(claim['relation'], 'manual_observation')
        self.assertIsNone(claim['origin_family'])
        self.assertEqual(claim['raw_ref']['availability'], 'raw_unavailable')
        self.assertIsNone(claim['raw_ref']['sha256'])
        self.assertEqual(claim['search_scope']['subject_inn'], INN)
        self.assertEqual(claim['search_scope']['query'], 'ИНН ' + INN)
        self.assertIs(claim['coverage_complete'], True)
        self.assertIsNone(claim['first_observed_at'])
        self.assertIsNone(claim['published_at'])
        self.assertIsNone(claim['valid_until'])
        observation = claim['search_scope']['manual_observation']
        self.assertEqual(observation['observed_on'], '2026-10-01')
        self.assertEqual(observation['date_precision'], 'day')
        self.assertEqual(observation['source_url'], 'https://kad.arbitr.ru/')
        self.assertEqual(observation['block_ref']['revision'], result['revision'])
        self.assertEqual(observation['imported_at'], claim['retrieved_at'])
        self.assertEqual(observation['freshness'], 'unknown')
        self.assertFalse(any(a['source_id'] == 'суды' for a in result['pending_actions']))

    def test_manual_absent_or_incomplete_coverage_remains_actionable(self):
        one = self.start()
        for changes in ({}, {'pagination_complete': False}, {'полно': False},
                        {'outcome': 'declined', 'pagination_complete': False}):
            result = workflow.import_manual(one['run_id'], self.block(**changes),
                expected_revision=one['revision'], operation_id=str(uuid.uuid4()), root=self.root)
            self.assertNotEqual(result['status'], 'error', result)
            claim = next(c for c in result['data']['fetch']['_evidence']['claims'].values()
                         if c['field'] == 'суды.ответчик_крупные')
            self.assertEqual(claim['knowledge'], 'unknown')
            self.assertTrue(any(a['source_id'] == 'суды' for a in result['pending_actions']))
            if changes.get('outcome') == 'declined':
                self.assertEqual(result['data']['metadata']['manual_outcomes']['суды'], 'declined')
            one = result
        for changes in ({'pagination_complete': 'true'}, {'полно': 1}, {'полнота': {}}):
            result = workflow.import_manual(one['run_id'], self.block(**changes),
                expected_revision=one['revision'], operation_id=str(uuid.uuid4()), root=self.root)
            self.assertEqual(result['status'], 'error', result)

    def test_manual_expiry_binding_and_unrelated_refresh_preserve_observation(self):
        import evidence_contract
        one = self.start()
        result = workflow.import_manual(one['run_id'], self.block(pagination_complete=True,
            expires_at='2099-01-01T00:00:00Z'), expected_revision=one['revision'],
            operation_id=str(uuid.uuid4()), root=self.root)
        self.assertNotEqual(result['status'], 'error', result)
        claim = next(c for c in result['data']['fetch']['_evidence']['claims'].values()
                     if c['field'] == 'суды.ответчик_крупные')
        self.assertEqual(claim['valid_until'], '2099-01-01T00:00:00Z')
        self.assertEqual(claim['reason_code'], 'raw_unavailable')
        for key, value in (('инн', '7728168971'), ('дата_проверки', '2026-09-01'),
                           ('url', 'https://evil.example/'), ('pagination_complete', False)):
            tampered = copy.deepcopy(result['data']['fetch'])
            tampered['суды'][key] = value
            with self.assertRaisesRegex(ValueError, 'invalid_manual_observation'):
                evidence_contract.enrich(tampered, now='2026-10-17T09:00:00Z')
        with patch.object(collector, 'run_source', return_value=(None, {'состояние': 'не проверено'})):
            refreshed = workflow.refresh(result['run_id'], ['риски'], expected_revision=result['revision'],
                operation_id=str(uuid.uuid4()), root=self.root)
        self.assertNotEqual(refreshed['status'], 'error', refreshed)
        after = next(c for c in refreshed['data']['fetch']['_evidence']['claims'].values()
                     if c['field'] == 'суды.ответчик_крупные')
        self.assertEqual(after, claim)
        with patch.object(collector, 'run_source', return_value=(None, {'состояние': 'не проверено'})):
            replaced = workflow.refresh(result['run_id'], ['суды'], expected_revision=refreshed['revision'],
                operation_id=str(uuid.uuid4()), root=self.root)
        self.assertNotEqual(replaced['status'], 'error', replaced)
        self.assertTrue(any(a['source_id'] == 'суды' for a in replaced['pending_actions']))

    def test_refresh_exception_after_capture_remains_unmaterialized(self):
        one = self.start()
        def fail(sid, inn, **kwargs):
            recorded_collect(inn)
            raise RuntimeError('interrupted parser')
        with patch.object(collector, 'run_source', side_effect=fail):
            result = workflow.refresh(one['run_id'], ['егрюл'], expected_revision=one['revision'],
                operation_id=str(uuid.uuid4()), root=self.root)
        self.assertNotEqual(result['status'], 'error', result)
        self.assertEqual(result['data']['metadata']['workflow_status'], 'failed')
        resumed = workflow.resume(one['run_id'], root=self.root)
        self.assertIn('unmaterialized_captures', [w['code'] for w in resumed['warnings']])


if __name__ == '__main__':
    unittest.main()
