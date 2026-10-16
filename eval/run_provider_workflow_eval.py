#!/usr/bin/env python3
"""Offline persisted-policy checkpoints and actual export-byte regression tests."""
import copy
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import fetch_counterparty as collector
import run_store as store
import run_workflow as workflow

INN = '7707083893'
PRIVATE = ('PRIVATE_ADDRESS', 'PRIVATE_NESTED', 'PRIVATE_DERIVED', 'PRIVATE_RAW')


def policy(exports=('name',), budget=3):
    return {'schema_version': 1, 'total_cost_units': budget, 'providers': [
        {'id': 'checko', 'enabled': True, 'allowed_fields': ['name', 'address'],
         'export_fields': list(exports), 'request_limit': 3, 'request_cost_units': 1,
         'fallback_on': []}]}


def provider_source(sid, inn, **kwargs):
    if sid != 'агрегатор':
        return {'fixture': sid}, {'состояние': 'не проверено', 'причина': 'offline fixture'}
    collector._ИСТОЧНИК.id = sid
    collector._ИСТОЧНИК.subject_inn = inn
    collector._записать('GET', 'https://api.checko.ru/v2/company', 200,
                        b'{"name":"PUBLIC_NAME","private":"PRIVATE_RAW"}')
    capture = collector._CAPTURE.get()
    if capture.provider_policy is not None:
        with patch.object(collector, '_policy_provider_request', return_value={
                'status': 'ok', 'data': {'name': 'PUBLIC_NAME', 'address': 'PRIVATE_ADDRESS'},
                'requests_used': 1}):
            data, av = collector.fetch_aggregator(object(), inn)
    else:
        data, av = {'агрегатор': 'checko', 'name': 'PUBLIC_NAME', 'address': 'PRIVATE_ADDRESS'}, {'состояние': 'ok'}
    data['nested'] = {'value': 'PRIVATE_NESTED'}
    return data, av


def collected(inn, **kwargs):
    block, av = provider_source('агрегатор', inn)
    kwargs['on_source']('агрегатор', block, av)
    # Deliberately no marker: workflow must preserve policy when collect replaces fetch.
    return {'инн': inn, 'агрегатор': block, '_доступность': {'агрегатор': av}}


class ProviderWorkflowEval(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / 'runs'
        self.config = Path(self.tmp.name) / 'policy.json'
        self.configure(policy())
        env = patch.dict(os.environ, INN_CHECK_PROVIDER_POLICY=str(self.config))
        env.start()
        self.addCleanup(env.stop)
        network = patch('urllib.request.urlopen', side_effect=AssertionError('no live requests'))
        network.start()
        self.addCleanup(network.stop)

    def configure(self, value):
        self.config.write_text(json.dumps(value), encoding='utf-8')

    def start(self, collect=collected):
        with patch.object(collector, 'collect', side_effect=collect):
            result = workflow.start(INN, root=self.root)
        self.assertNotEqual(result['status'], 'error', result)
        return result

    def refresh(self, run, selected, source=provider_source, op=None):
        with patch.object(collector, 'run_source', side_effect=source):
            return workflow.refresh(run['run_id'], selected, expected_revision=run['revision'],
                                    operation_id=op or str(uuid.uuid4()), root=self.root)

    def marker(self, result):
        return result['data']['fetch'].get('_provider_policy')

    def test_malformed_marker_refuses_all_document_exports_before_artifact(self):
        run = self.start()
        record = store.read(run['run_id'], root=self.root)
        revision = record['revision']
        for marker in (None, False, [], 'PRIVATE_RAW', {}, {'schema_version': 1},
                       {'schema_version': True, 'policy': policy()}):
            with self.subTest(marker=marker):
                payload = copy.deepcopy(record['payload'])
                payload['fetch']['_provider_policy'] = marker
                store.append(run['run_id'], payload, expected_revision=revision,
                             operation_id=str(uuid.uuid4()), root=self.root)
                revision += 1
                before = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
                with patch.object(store, 'put_artifact') as write:
                    for fmt in ('markdown', 'json', 'docx'):
                        self.assertEqual(workflow.export(run['run_id'], format=fmt, root=self.root)['status'], 'error')
                    for fmt in ('markdown', 'json'):
                        self.assertEqual(workflow.render(run['run_id'], format=fmt, root=self.root)['status'], 'error')
                    self.assertEqual(workflow.save_snapshot(run['run_id'], root=self.root)['status'], 'error')
                    write.assert_not_called()
                self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_start_completed_and_partial_checkpoint_capture_policy_and_ledger(self):
        result = self.start()
        marker = self.marker(result)
        self.assertIsNotNone(marker)
        self.assertEqual(marker['policy'], policy())
        self.assertEqual(marker['results'][0]['budget']['used_requests'], 1)
        self.assertEqual(marker['results'][0]['transfers'][0]['provider'], 'checko')
        checkpoint = store.read(result['run_id'], revision=result['revision'] - 1, root=self.root)
        self.assertEqual(checkpoint['payload']['fetch']['_provider_policy'], marker)
        self.assertNotIn('PRIVATE_ADDRESS', json.dumps(marker))

    def test_start_cancel_failure_before_or_after_first_source_keep_policy(self):
        for error in (KeyboardInterrupt, RuntimeError):
            for completed in (False, True):
                with self.subTest(error=error, completed=completed):
                    def collect(inn, *, completed=completed, error=error, **kwargs):
                        if completed:
                            collected(inn, **kwargs)
                        raise error('fixture interruption')
                    result = self.start(collect)
                    self.assertIsNotNone(self.marker(result))
                    self.assertEqual(self.marker(result)['policy'], policy())
                    self.assertEqual(result['data']['metadata']['workflow_status'],
                                     'cancelled' if error is KeyboardInterrupt else 'failed')

    def test_start_hard_interruption_checkpoint_survives(self):
        def collect(inn, **kwargs):
            collected(inn, **kwargs)
            raise SystemExit(79)
        with patch.object(collector, 'collect', side_effect=collect), self.assertRaises(SystemExit):
            workflow.start(INN, root=self.root)
        rid = next(self.root.glob('*/HEAD.json')).parent.name
        resumed = workflow.resume(rid, root=self.root)
        self.assertIsNotNone(self.marker(resumed))
        self.assertEqual(self.marker(resumed)['policy'], policy())
        self.assertEqual(resumed['data']['metadata']['workflow_status'], 'collecting')

    def test_refresh_aggregator_changes_capture_nonaggregator_keeps_saved(self):
        run = self.start()
        old = self.marker(run)
        self.configure(policy(exports=(), budget=2))
        other = self.refresh(run, ['егрюл'])
        self.assertEqual(self.marker(other), old)
        updated = self.refresh(other, ['агрегатор'])
        self.assertIsNotNone(self.marker(updated))
        self.assertEqual(self.marker(updated)['policy'], policy(exports=(), budget=2))
        checkpoint = store.read(run['run_id'], revision=updated['revision'] - 1, root=self.root)
        self.assertEqual(checkpoint['payload']['fetch']['_provider_policy'], self.marker(updated))

    def test_refresh_removed_policy_preserves_restrictions(self):
        run = self.start()
        self.assertIsNotNone(self.marker(run))
        with patch.dict(os.environ, INN_CHECK_PROVIDER_POLICY=''):
            updated = self.refresh(run, ['агрегатор'])
        self.assertEqual(self.marker(updated), self.marker(run))

    def test_refresh_aggregator_not_completed_does_not_change_saved_policy(self):
        run = self.start()
        self.assertIsNotNone(self.marker(run))
        self.configure(policy(exports=(), budget=1))
        for error in (KeyboardInterrupt, RuntimeError):
            def incomplete(sid, inn, *, error=error, **kwargs):
                provider_source(sid, inn, **kwargs)
                raise error('not returned')
            updated = self.refresh(run, ['агрегатор'], incomplete)
            self.assertEqual(self.marker(updated), self.marker(run))
            run = updated

    def test_refresh_completed_then_cancel_or_fail_keeps_new_policy(self):
        for error in (KeyboardInterrupt, RuntimeError):
            run = self.start()
            self.configure(policy(exports=(), budget=2))
            def source(sid, inn, *, error=error, **kwargs):
                if sid == 'егрюл':
                    raise error('later source')
                return provider_source(sid, inn, **kwargs)
            updated = self.refresh(run, ['агрегатор', 'егрюл'], source)
            self.assertIsNotNone(self.marker(updated))
            self.assertEqual(self.marker(updated)['policy'], policy(exports=(), budget=2))

    def test_interrupted_refresh_continuation_retains_captured_policy_and_cas(self):
        run = self.start()
        op = str(uuid.uuid4())
        self.configure(policy(exports=(), budget=2))
        def interrupted(sid, inn, **kwargs):
            if sid == 'егрюл':
                raise SystemExit(79)
            return provider_source(sid, inn, **kwargs)
        with self.assertRaises(SystemExit):
            self.refresh(run, ['агрегатор', 'егрюл'], interrupted, op)
        resumed = workflow.resume(run['run_id'], root=self.root)
        self.assertIsNotNone(self.marker(resumed))
        self.assertEqual(self.marker(resumed)['policy'], policy(exports=(), budget=2))
        self.configure(policy(exports=('name', 'address'), budget=1))
        calls = []
        def remaining(sid, inn, **kwargs):
            calls.append(sid)
            return provider_source(sid, inn, **kwargs)
        finished = self.refresh(run, ['агрегатор', 'егрюл'], remaining, op)
        self.assertEqual(calls, ['егрюл'])
        self.assertEqual(self.marker(finished), self.marker(resumed))
        retry = self.refresh(run, ['агрегатор', 'егрюл'], remaining, op)
        self.assertEqual(retry, finished)
        self.assertEqual(calls, ['егрюл'])
        conflict = self.refresh(run, ['егрюл'], remaining)
        self.assertEqual(conflict['error']['code'], 'revision_conflict')

    def test_restricted_exports_filter_nested_derived_raw_and_hash_actual_bytes(self):
        run = self.start()
        self.assertIsNotNone(self.marker(run))
        original = store.read(run['run_id'], root=self.root)
        payload = copy.deepcopy(original['payload'])
        payload['fin']['sensitive'] = 'PRIVATE_DERIVED'
        payload['assessment']['sensitive'] = 'PRIVATE_DERIVED'
        record = store.append(run['run_id'], payload, expected_revision=run['revision'],
                              operation_id=str(uuid.uuid4()), root=self.root)
        immutable = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.configure(policy(exports=('name', 'address')))
        with patch.object(workflow.evidence_contract, 'evaluate', side_effect=AssertionError('no new score')), \
                patch.object(workflow, '_now', side_effect=AssertionError('no new time')), \
                patch.object(collector, 'capture_session', side_effect=AssertionError('no current policy')):
            resumed = workflow.resume(run['run_id'], root=self.root)
            self.assertEqual(resumed['data'], payload)
            for fmt in ('markdown', 'json', 'docx'):
                exported = workflow.export(run['run_id'], format=fmt, root=self.root)
                self.assertEqual(exported['status'], 'ok', exported)
                info = exported['data']
                blob = store.read_artifact(run['run_id'], info['artifact_id'], root=self.root)
                self.assertEqual(hashlib.sha256(blob).hexdigest(), info['sha256'])
                self.assertEqual(info['source_revision'], record['revision'])
                content = (b'\n'.join(zipfile.ZipFile(io.BytesIO(blob)).read(n)
                                     for n in zipfile.ZipFile(io.BytesIO(blob)).namelist())
                           if fmt == 'docx' else blob)
                self.assertIn(b'PUBLIC_NAME', content)
                for secret in PRIVATE:
                    self.assertNotIn(secret.encode(), content)
            for fmt in ('markdown', 'json'):
                rendered = workflow.render(run['run_id'], format=fmt, root=self.root)
                self.assertEqual(rendered['status'], 'ok', rendered)
                for secret in PRIVATE:
                    self.assertNotIn(secret, json.dumps(rendered['data']))
        self.assertEqual(store.read(run['run_id'], root=self.root), record)
        for path, blob in immutable.items():
            self.assertEqual(path.read_bytes(), blob)

    def test_snapshot_restricted_policy_refuses_without_artifact_or_fingerprint(self):
        run = self.start()
        with patch.object(workflow.snapshot, 'fingerprint', side_effect=AssertionError('must refuse first')):
            result = workflow.save_snapshot(run['run_id'], root=self.root)
        self.assertEqual(result['status'], 'error', result)
        self.assertEqual(result['error']['code'], 'provider_snapshot_export_restricted')
        self.assertEqual(result['run_id'], run['run_id'])
        self.assertEqual(result['revision'], run['revision'])
        self.assertEqual(store.list_artifacts(run['run_id'], root=self.root), [])

    def test_policy_configured_after_legacy_run_does_not_change_exports(self):
        with patch.dict(os.environ, INN_CHECK_PROVIDER_POLICY=''):
            run = self.start()
            before = workflow.render(run['run_id'], root=self.root)
        self.assertIsNone(self.marker(run))
        self.assertEqual(workflow.render(run['run_id'], root=self.root), before)
        snap = workflow.save_snapshot(run['run_id'], root=self.root)
        self.assertEqual(snap['status'], 'ok', snap)
        updated = self.refresh(run, ['егрюл'])
        self.assertIsNone(self.marker(updated))

    def test_snapshot_malformed_marker_also_fails_closed(self):
        run = self.start()
        for marker in (None, {}, {'schema_version': 999}):
            payload = copy.deepcopy(run['data'])
            payload['fetch']['_provider_policy'] = marker
            current = store.read(run['run_id'], root=self.root)
            record = store.append(run['run_id'], payload, expected_revision=current['revision'],
                                  operation_id=str(uuid.uuid4()), root=self.root)
            result = workflow.save_snapshot(record['run_id'], root=self.root)
            self.assertEqual(result['error']['code'], 'provider_snapshot_export_restricted')
            self.assertEqual(store.list_artifacts(record['run_id'], root=self.root), [])


if __name__ == '__main__':
    unittest.main()
