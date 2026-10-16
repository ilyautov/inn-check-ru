#!/usr/bin/env python3
"""Saved-run handoff through real native frames. No installed Chrome acceptance."""
import copy
import hashlib
import io
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'eval')]
import fetch_counterparty as collector
import native_host
import run_store
import run_workflow as workflow
from run_run_workflow_eval import INN, recorded_collect


class HandoffEval(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / 'runs'
        with patch.object(collector, 'collect', side_effect=recorded_collect):
            self.run = workflow.start(INN, root=self.root)
        self.assertNotEqual(self.run['status'], 'error', self.run)

    def frame(self, message):
        body = json.dumps(message, ensure_ascii=False).encode()
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/native_host.py')],
            input=struct.pack('=I', len(body)) + body, capture_output=True, timeout=30, check=False,
            env=dict(os.environ, INN_CHECK_RUNS_DIR=str(self.root)))
        self.assertEqual(result.returncode, 0, result.stderr)
        size, = struct.unpack('=I', result.stdout[:4])
        self.assertEqual(size, len(result.stdout) - 4)
        self.assertLessEqual(size, 1024 * 1024)
        self.last_frame_size = size
        return json.loads(result.stdout[4:])

    def enlarge(self, *, multibyte=False):
        current = run_store.read(self.run['run_id'], root=self.root)
        payload = copy.deepcopy(current['payload'])
        payload['fetch']['риски'] = {'статус': 'ok', 'records': [
            {'description': 'x' * 1024, 'id': i} for i in range(1100)]}
        if multibyte:
            payload['fetch'].setdefault('егрюл', {})['название'] = 'Я😀\\"' * 100000
            payload['metadata']['warnings'] = [{'code': 'oversize', 'message': 'Я😀' * 200000}]
            claim = next(iter(payload['fetch']['_evidence']['claims'].values()))
            claim.update(knowledge='unknown', reason_code='!Я😀' * 200000)
        record = run_store.append(self.run['run_id'], payload, root=self.root,
            expected_revision=current['revision'], operation_id=str(uuid.uuid4()))
        self.run['revision'] = record['revision']
        self.assertGreater(len(run_store._json(record['payload'])), 1024 * 1024)
        return record

    def test_large_framed_read_manual_retry_keep_receipt_and_original_bytes(self):
        original = self.enlarge()
        original_hash = hashlib.sha256(run_store._json(original['payload'])).hexdigest()
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()
                  and p.name not in {'HEAD.json', '.lock'}}
        read = self.frame({'команда': 'run_read', 'run_id': self.run['run_id']})
        self.assertEqual(run_store.read(self.run['run_id'], root=self.root), original)
        msg = self.message()
        first = self.frame(msg)
        committed = run_store.read(self.run['run_id'], root=self.root)
        again = self.frame(msg)
        self.assertEqual(committed['revision'], original['revision'] + 1)
        self.assertEqual(run_store.read(self.run['run_id'], root=self.root), committed)
        self.assertEqual(first.get('revision'), committed['revision'],
                         {'read': read, 'manual': first, 'retry': again,
                          'committed_revision': committed['revision']})
        self.assertEqual(read.get('run_id'), self.run['run_id'], read)
        self.assertEqual(read['revision'], original['revision'])
        self.assertEqual(read['data']['fetch']['инн'], INN)
        self.assertEqual(read['native_projection']['kind'], 'binding')
        self.assertFalse(read['native_projection']['complete'])
        self.assertIn('fetch_source_details', read['native_projection']['omitted'])
        self.assertNotIn('риски', read['data']['fetch'])
        self.assertNotEqual(first['status'], 'error', first)
        self.assertEqual(first['revision'], original['revision'] + 1)
        self.assertEqual(first['data']['metadata']['manual_outcomes']['суды'], 'completed')
        self.assertEqual(first['native_projection']['kind'], 'binding')
        self.assertEqual(first, again)
        self.assertEqual(run_store.read(self.run['run_id'], root=self.root), committed)
        self.assertEqual(committed['payload']['metadata']['raw_refs'], original['payload']['metadata']['raw_refs'])
        old = run_store.read(self.run['run_id'], revision=original['revision'], root=self.root)
        self.assertEqual(hashlib.sha256(run_store._json(old['payload'])).hexdigest(), original_hash)
        for path, contents in before.items():
            self.assertEqual(path.read_bytes(), contents, path)

    def test_large_multibyte_names_metadata_pending_are_bounded_deterministic(self):
        original = self.enlarge(multibyte=True)
        msg = {'команда': 'run_read', 'run_id': self.run['run_id']}
        first = self.frame(msg)
        self.assertEqual(first.get('run_id'), self.run['run_id'], first)
        self.assertEqual(first['native_projection']['kind'], 'binding')
        self.assertTrue(first['native_projection']['truncated'])
        self.assertIn('pending_action.reason_code', first['native_projection']['truncated'])
        self.assertLess(self.last_frame_size, 256 * 1024)
        self.assertEqual(first, self.frame(msg))
        self.assertEqual(first['data']['fetch']['инн'], INN)
        self.assertIsInstance(first['data']['fetch']['егрюл']['название'], str)
        self.assertEqual(run_store.read(self.run['run_id'], root=self.root), original)

    def test_large_invalid_input_keeps_workflow_error_envelope(self):
        msg = {'команда': 'run_read', 'run_id': self.run['run_id'], 'Я😀' * 300000: True}
        result = self.frame(msg)
        self.assertEqual(result.get('status'), 'error', result)
        self.assertEqual(result['error']['code'], 'invalid_input')
        self.assertEqual(result['native_projection']['kind'], 'binding')
        self.assertTrue(result['native_projection']['truncated'])

    def test_generic_large_metadata_does_not_break_binding_projection(self):
        current = self.enlarge()
        payload = copy.deepcopy(current['payload'])
        payload['metadata']['warnings'] = {'unstructured': 'Я😀' * 200000}
        record = run_store.append(self.run['run_id'], payload, root=self.root,
            expected_revision=current['revision'], operation_id=str(uuid.uuid4()))
        result = self.frame({'команда': 'run_read', 'run_id': self.run['run_id']})
        self.assertEqual(result['revision'], record['revision'], result)
        self.assertNotEqual(result['status'], 'error', result)
        self.assertIn('warnings', result['native_projection']['truncated'])
        self.assertEqual(result['warnings'], [])

    def test_projection_caps_each_collection_and_escaped_field(self):
        # Exercise the adapter's hard bounds beyond today's source-catalog size.
        huge = '\x00Я😀"\\' * 10000
        response = copy.deepcopy(self.run)
        response['data']['metadata']['profile'] = huge
        response['data']['metadata']['manual_outcomes'] = {sid: huge for sid in native_host.ХОСТЫ}
        response['data']['fetch']['_decision'] = {'decision_status': huge, 'can_recommend': False}
        response['pending_actions'] = [{'source_id': huge, 'kind': huge, 'reason_code': huge}] * 1000
        response['warnings'] = [{'code': huge, 'message': huge}] * 1000
        original = copy.deepcopy(response)
        projected = native_host._native_workflow(response)
        output = io.BytesIO()
        native_host.писать_кадр(output, projected)
        frame = output.getvalue()
        self.assertEqual(struct.unpack('=I', frame[:4])[0], len(frame) - 4)
        self.assertLess(len(frame) - 4, native_host.МАКС_ОТВЕТ)
        self.assertEqual(json.loads(frame[4:]), projected)
        self.assertEqual(projected['native_projection']['kind'], 'binding')
        self.assertEqual(projected['native_projection']['pending_actions_total'], 1000)
        self.assertEqual(projected['native_projection']['warnings_total'], 1000)
        self.assertEqual(len(projected['pending_actions']), 128)
        self.assertEqual(len(projected['warnings']), 16)
        self.assertFalse(projected['data']['fetch']['_decision']['can_recommend'])
        for field in ('pending_actions', 'warnings', 'profile', 'decision_status'):
            self.assertIn(field, projected['native_projection']['truncated'])
        self.assertEqual(response, original)
        self.assertEqual(native_host._native_workflow(response), projected)

    def message(self, **changes):
        block = {'инн': INN, 'url': 'https://kad.arbitr.ru/',
            'дата_проверки': datetime.now(timezone.utc).date().isoformat(),
            'статус': 'проверено', 'outcome': 'completed', 'ответчик_крупные': False,
            'параметры_поиска': 'ИНН ' + INN, 'pagination_complete': True}
        block.update(changes)
        return {'команда': 'run_manual', 'run_id': self.run['run_id'],
            'expected_revision': self.run['revision'], 'operation_id': str(uuid.uuid4()),
            'blocks': {'суды': block}}

    def test_read_unknown_and_exact_subject(self):
        result = self.frame({'команда': 'run_read', 'run_id': self.run['run_id']})
        self.assertEqual(result['native_projection']['kind'], 'full')
        self.assertTrue(result['native_projection']['complete'])
        self.assertEqual(result['data']['fetch']['инн'], INN)
        self.assertEqual(result['revision'], self.run['revision'])
        self.assertNotIn('root', result)
        unknown = self.frame({'команда': 'run_read', 'run_id': str(uuid.uuid4())})
        self.assertEqual(unknown['error']['code'], 'not_found')

    def test_reject_subject_url_future_expiry(self):
        for change in [{'инн': '7736050003'}, {'url': 'https://evil.example/'},
            {'дата_проверки': (datetime.now(timezone.utc) + timedelta(days=5)).date().isoformat()},
            {'expires_at': '2020-01-01T00:00:00Z'}]:
            with self.subTest(change=change):
                result = self.frame(self.message(**change))
                self.assertEqual(result['status'], 'error', result)
        self.assertEqual(run_store.read(self.run['run_id'], root=self.root)['revision'], self.run['revision'])

    def test_old_observation_without_explicit_expiry_is_preserved(self):
        result = self.frame(self.message(дата_проверки='2020-01-01'))
        self.assertNotEqual(result['status'], 'error', result.get('error'))
        self.assertEqual(result['data']['fetch']['суды']['дата_проверки'], '2020-01-01')
        self.assertFalse(result['data']['fetch']['_decision']['can_recommend'])

    def test_unknown_fields_and_types(self):
        for key, value in [('root', '/tmp/evil'), ('path', '/tmp/evil'),
                           ('executable', 'touch /tmp/evil'), ('expected_revision', True),
                           ('operation_id', []), ('blocks', [])]:
            msg = self.message()
            msg[key] = value
            result = self.frame(msg)
            self.assertEqual(result['status'], 'error', result)
        msg = self.message()
        msg['blocks']['суды']['command'] = '<script>touch /tmp/evil</script>'
        self.assertEqual(self.frame(msg)['status'], 'error')

    def test_incomplete_and_refusal_survive_restart(self):
        for outcome in ['incomplete', 'declined']:
            msg = self.message(outcome=outcome, reason='Пользователь не завершил поиск', pagination_complete=False)
            msg['expected_revision'] = run_store.read(self.run['run_id'], root=self.root)['revision']
            result = self.frame(msg)
            self.assertNotEqual(result['status'], 'error', result)
            read = self.frame({'команда': 'run_read', 'run_id': self.run['run_id']})
            self.assertEqual(read['data']['metadata']['manual_outcomes']['суды'], outcome)
            self.assertTrue(any(a['source_id'] == 'суды' for a in read['pending_actions']))

    def test_completed_requires_explicit_pagination_flag(self):
        msg = self.message()
        del msg['blocks']['суды']['pagination_complete']
        self.assertEqual(self.frame(msg)['status'], 'error')
        for invalid in ['true', 1, None]:
            msg['blocks']['суды']['pagination_complete'] = invalid
            self.assertEqual(self.frame(msg)['error']['code'], 'invalid_input')
        msg['blocks']['суды']['pagination_complete'] = False
        result = self.frame(msg)
        self.assertNotEqual(result['status'], 'error', result.get('error'))
        self.assertEqual(result['data']['metadata']['manual_outcomes']['суды'], 'incomplete')

    def test_existing_wrong_run_cannot_receive_other_subject(self):
        with patch.object(collector, 'collect', side_effect=recorded_collect):
            other = workflow.start('7736050003', root=self.root)
        self.assertNotEqual(other['status'], 'error')
        msg = self.message()
        msg.update(run_id=other['run_id'], expected_revision=other['revision'])
        result = self.frame(msg)
        self.assertEqual(result['error']['code'], 'subject_mismatch')
        self.assertEqual(run_store.read(other['run_id'], root=self.root)['revision'], other['revision'])

    def test_five_scripted_handoffs_survive_reopen(self):
        cases = [('суды', 'completed', True, ''), ('суды', 'incomplete', False, 'Капча'),
                 ('фссп', 'completed', True, ''), ('фссп', 'incomplete', False, 'Не все страницы'),
                 ('фссп', 'declined', False, 'Отказ пользователя')]
        refs = copy.deepcopy(self.run['data']['metadata']['raw_refs'])
        for source, outcome, complete, reason in cases:
            with self.subTest(source=source, outcome=outcome):
                current = self.frame({'команда': 'run_read', 'run_id': self.run['run_id']})
                msg = self.message(outcome=outcome, pagination_complete=complete, reason=reason)
                msg['expected_revision'] = current['revision']
                if source == 'фссп':
                    block = msg['blocks'].pop('суды')
                    block.pop('ответчик_крупные')
                    block.update(url='https://fssp.gov.ru/', производств=0, крупные=False)
                    msg['blocks'][source] = block
                result = self.frame(msg)
                self.assertNotEqual(result['status'], 'error', result.get('error'))
                reopened = self.frame({'команда': 'run_read', 'run_id': self.run['run_id']})
                self.assertEqual(reopened['revision'], result['revision'])
                self.assertEqual(reopened['data']['metadata']['manual_outcomes'][source], outcome)
                self.assertEqual(reopened['data']['metadata']['raw_refs'], refs)
                if outcome != 'completed':
                    self.assertTrue(any(a['source_id'] == source for a in reopened['pending_actions']))

    def test_retry_conflict_and_original_bytes(self):
        before = copy.deepcopy(self.run['data']['metadata']['raw_refs'])
        msg = self.message(найдено='<script>alert(1)</script> $(touch /tmp/never)')
        first = self.frame(msg)
        self.assertNotEqual(first['status'], 'error', first)
        again = self.frame(msg)
        self.assertEqual(first['revision'], again['revision'])
        self.assertEqual(first['data'], again['data'])
        self.assertEqual(first['data']['metadata']['raw_refs'], before)
        self.assertEqual(first['data']['fetch']['суды']['найдено'], msg['blocks']['суды']['найдено'])
        conflict = self.frame(self.message())
        self.assertEqual(conflict['error']['code'], 'revision_conflict')
        changed = copy.deepcopy(msg)
        changed['blocks']['суды']['ответчик_крупные'] = True
        self.assertEqual(self.frame(changed)['error']['code'], 'operation_conflict')


if __name__ == '__main__':
    unittest.main()
