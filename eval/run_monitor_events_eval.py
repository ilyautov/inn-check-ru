"""Durable queue acceptance: ten identities, crash/leases, concurrency, receipts."""
import contextlib
import copy
import datetime as dt
import hashlib
import io
import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import monitor_events as events

NOW = dt.datetime(2026, 10, 16, 9, tzinfo=dt.timezone.utc)


def report():
    rows = []
    for i in range(10):
        kind = ('company_changed', 'coverage_lost', 'coverage_restored', 'refresh_failed')[i % 4]
        change = {'поле': 'директор', 'было': 'A', 'стало': 'B'}
        if kind.startswith('coverage_'):
            change = {'поле': 'источник.егрюл', 'было': 'ok' if kind.endswith('lost') else 'не проверено',
                      'стало': 'не проверено' if kind.endswith('lost') else 'ok'}
        rows.append({'инн': str(7707083800 + i), 'профиль': 'нейтрально',
                     'статус': 'не проверено' if kind == 'refresh_failed' else 'изменения',
                     'причина': 'fetch failed' if kind == 'refresh_failed' else None,
                     'изменения': [] if kind == 'refresh_failed' else [change]})
    return {'дата': '2026-10-16T09:00:00Z', 'результаты': rows}


def claim_child(root, consumer, output):
    output.put(events.claim_events(consumer, root=root, limit=5))


def crash_child(root):
    events.claim_events('crashed', root=root, limit=50, lease_seconds=1)
    os._exit(0)


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = str(Path(self.tmp.name).resolve() / 'queue')
    def tearDown(self):
        self.tmp.cleanup()
    def test_ten_campaign(self):
        first = events.ingest(report(), root=self.root)
        self.assertEqual(len(first), 10)
        self.assertEqual({x['kind'] for x in first}, events.KINDS)
        self.assertEqual([x['event_id'] for x in first], [x['event_id'] for x in events.ingest(report(), root=self.root)])
        self.assertEqual(len(events.list_events(root=self.root)), 10)
        claimed = events.claim_events('host', root=self.root)
        self.assertEqual(len(claimed), 10)
        self.assertTrue(all(x['delivery']['state'] == 'delivered' and x['delivery']['acknowledged_at'] is None for x in claimed))
        ids = [x['event_id'] for x in claimed]
        events.ack_events(ids, consumer_id='host', root=self.root)
        events.ack_events(ids, consumer_id='host', root=self.root)
        self.assertEqual(events.list_events(root=self.root), [])
        self.assertEqual(len(events.list_events(state='acknowledged', root=self.root)), 10)
        events.ingest(report(), root=self.root)
        self.assertEqual(events.list_events(root=self.root), [])
    def test_pure_list(self):
        self.assertEqual(events.list_events(root=self.root), [])
        self.assertFalse(Path(self.root).exists())
        events.ingest(report(), root=self.root)
        before = {p.name: p.read_bytes() for p in Path(self.root).iterdir()}
        events.list_events(root=self.root)
        self.assertEqual(before, {p.name: p.read_bytes() for p in Path(self.root).iterdir()})
    def test_lease_retry_foreign_and_cap(self):
        with patch.object(events, '_now', return_value=NOW):
            events.ingest(report(), root=self.root)
            item = events.claim_events('a', limit=1, lease_seconds=1, root=self.root)[0]
            event_id = item['event_id']
            with self.assertRaises(events.EventError):
                events.ack_events([event_id], consumer_id='b', root=self.root)
            events.retry_events([event_id], consumer_id='a', root=self.root)
            self.assertNotIn(event_id, [x['event_id'] for x in events.claim_events('b', root=self.root)])
        for attempt in range(2, events.MAX_ATTEMPTS + 1):
            with patch.object(events, '_now', return_value=NOW + dt.timedelta(days=attempt)):
                claimed = events.claim_events('a', root=self.root)
                self.assertIn(event_id, [x['event_id'] for x in claimed])
                events.retry_events([event_id], consumer_id='a', root=self.root)
        with patch.object(events, '_now', return_value=NOW + dt.timedelta(days=20)):
            self.assertNotIn(event_id, [x['event_id'] for x in events.claim_events('a', root=self.root)])
            self.assertIn(event_id, [x['event_id'] for x in events.list_events(state='dead_letter', root=self.root)])
    def test_crash_recovery_same_ids(self):
        events.ingest(report(), root=self.root)
        ctx = multiprocessing.get_context('spawn')
        p = ctx.Process(target=crash_child, args=(self.root,)); p.start(); p.join(20)
        self.assertEqual(p.exitcode, 0)
        with patch.object(events, '_now', return_value=dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=2)):
            recovered = events.claim_events('recovered', root=self.root)
        self.assertEqual(len(recovered), 10)
        self.assertEqual({x['event_id'] for x in recovered}, {x['event_id'] for x in events.ingest(report(), root=self.root)})
    def test_concurrent_consumers(self):
        events.ingest(report(), root=self.root)
        ctx = multiprocessing.get_context('spawn'); output = ctx.Queue()
        children = [ctx.Process(target=claim_child, args=(self.root, name, output)) for name in ('a', 'b')]
        for p in children: p.start()
        values = [output.get(timeout=20) for _ in children]
        for p in children: p.join(20); self.assertEqual(p.exitcode, 0)
        self.assertEqual([len(x) for x in values], [5, 5])
        self.assertEqual(len({x['event_id'] for group in values for x in group}), 10)
    def test_validation_and_honest_legacy(self):
        items = events.ingest(report(), root=self.root)
        self.assertTrue(all(x['to_revision'] is None and x['origin']['kind'] == 'legacy_report' for x in items))
        for value in (0, 51, True):
            with self.assertRaises(events.EventError): events.claim_events('a', limit=value, root=self.root)
        with self.assertRaises(events.EventError): events.ack_events(['../secret'], consumer_id='a', root=self.root)
        self.assertEqual(events.ingest({'дата': '2026-10-16T09:00:00Z', 'результаты': [{'инн': '7707083893', 'статус': 'без изменений', 'изменения': []}]}, root=self.root), [])
    @unittest.skipUnless(os.name == 'posix', 'symlink available')
    def test_symlink_rejected(self):
        Path(self.root).symlink_to(self.tmp.name, target_is_directory=True)
        with self.assertRaises(events.store.RunStoreError): events.ingest(report(), root=self.root)


class QueueIntegrityTests(unittest.TestCase):
    def test_invalid_queue_refused_without_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / 'queue'
            small_report = report()
            small_report['результаты'] = small_report['результаты'][:1]
            events.ingest(small_report, root=root)
            event_id = events.claim_events('integrity-test', root=root)[0]['event_id']
            queue_file = root / 'events.json'
            valid_queue = json.loads(queue_file.read_bytes())
            event_path = ('events', event_id)
            cases = [
                ('schema', ('schema_version',), 2, False),
                ('schema_type', ('schema_version',), True, False),
                ('revision_zero', ('revision',), 0, False),
                ('revision_negative', ('revision',), -1, False),
                ('revision_bool', ('revision',), True, False),
                ('revision_string', ('revision',), '1', False),
                ('events_shape', ('events',), [], False),
                ('event_identity', (*event_path, 'event_id'), '0' * 64, False),
                ('identity_binding', (*event_path, 'profile'), 'другой', False),
                ('change_digest', (*event_path, 'data'), [], False),
                ('kind', (*event_path, 'kind'), 'other', True),
                ('delivery_state', (*event_path, 'delivery', 'state'), 'other', False),
                ('attempts_bool', (*event_path, 'delivery', 'attempts'), True, False),
                ('attempts_string', (*event_path, 'delivery', 'attempts'), '1', False),
                ('attempts_float', (*event_path, 'delivery', 'attempts'), 1.0, False),
                ('attempts_negative', (*event_path, 'delivery', 'attempts'), -1, False),
                ('attempts_over_limit', (*event_path, 'delivery', 'attempts'), events.MAX_ATTEMPTS + 1, False),
            ]
            for name, path, invalid, reidentify in cases:
                queue = copy.deepcopy(valid_queue)
                target = queue
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = invalid
                selected_id = event_id
                if reidentify:
                    event = queue['events'].pop(event_id)
                    selected_id = event['event_id'] = events._hash(events._identity(event))
                    queue['events'][selected_id] = event
                before = json.dumps(queue, ensure_ascii=False).encode('utf-8')
                operations = {
                    'list': lambda: events.list_events(state='all', root=root),
                    'ingest': lambda: events.ingest(report(), root=root),
                    'claim': lambda: events.claim_events('integrity-test', root=root),
                    'ack': lambda selected_id=selected_id: events.ack_events([selected_id], consumer_id='integrity-test', root=root),
                    'retry': lambda selected_id=selected_id: events.retry_events([selected_id], consumer_id='integrity-test', root=root),
                }
                for operation_name, operation in operations.items():
                    with self.subTest(case=name, operation=operation_name):
                        queue_file.write_bytes(before)
                        try:
                            with self.assertRaises(events.store.RunStoreError) as refused:
                                operation()
                            self.assertEqual(refused.exception.code, 'corrupt_store')
                        finally:
                            self.assertEqual(queue_file.read_bytes(), before)

    def test_invalid_queue_refused_under_optimized_python(self):
        result = subprocess.run([
            sys.executable, '-O', '-B', '-m', 'unittest',
            'eval.run_monitor_events_eval.QueueIntegrityTests.test_invalid_queue_refused_without_mutation',
        ], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
            timeout=30, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class MonitorIntegration(unittest.TestCase):
    def setUp(self):
        import run_store
        import watchlist
        self.watchlist, self.store = watchlist, run_store
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / 'monitor'
        self.root.mkdir()
        (self.root / 'watchlist.json').write_text(json.dumps({'контрагенты': [{'инн': '7707083893'}]}))
        self.fixture = Path(__file__).resolve().parent / 'fixtures/snapshots'
    def runner(self, fixture):
        def run(inn, profile, root):
            fetch = json.loads((self.fixture / fixture).read_text())
            payload = {'fetch': fetch, 'fin': {}, 'assessment': {},
                       'metadata': {'schema_version': 1, 'profile': profile, 'workflow_status': 'complete'}}
            record = self.store.create(payload, root=root)
            return {'status': 'ok', 'run_id': record['run_id'], 'revision': record['revision']}
        return run
    def test_saved_run_diff_origin_and_replay_after_publish_crash(self):
        with patch.object(self.watchlist, '_workflow_runner', side_effect=self.runner('states_a.json')):
            first = self.watchlist.monitor(root=self.root)
        self.assertEqual(first['проверено'], 1)
        original_ingest = events.ingest
        def crash_after_publication(report, **kwargs):
            original_ingest(report, **kwargs)
            raise RuntimeError('simulated process exit after queue commit')
        with patch.object(self.watchlist, '_workflow_runner', side_effect=self.runner('states_b.json')), \
             patch.object(events, 'ingest', side_effect=crash_after_publication), \
             self.assertRaises(RuntimeError):
            self.watchlist.monitor(root=self.root)
        queued = events.list_events(state='all', root=self.root / 'events')
        lost = [x for x in queued if x['kind'] == 'coverage_lost']
        self.assertTrue(lost, queued)
        self.assertTrue(all(x['origin']['kind'] == 'saved_run' and x['to_revision'] == 1 for x in queued))
        ids = {x['event_id'] for x in queued}
        # No new subject is requested; recovery must publish journal and advance baseline.
        (self.root / 'watchlist.json').write_text(json.dumps({'контрагенты': []}))
        with patch.object(self.watchlist, '_workflow_runner', side_effect=AssertionError('unexpected network')):
            self.watchlist.monitor(root=self.root)
        self.assertFalse((self.root / 'monitor-pending.json').exists())
        self.assertEqual(ids, {x['event_id'] for x in events.list_events(state='all', root=self.root / 'events')})
        state = json.loads((self.root / 'monitor-state.json').read_text())
        self.assertEqual(state['subjects']['нейтрально:7707083893']['run_id'], lost[0]['origin']['to_run_id'])
    def evidence_fetch(self, now, *, effective='2026-10-09T09:00:00Z', retrieved=None, complete=True):
        import evidence_contract as ec
        fetch = json.loads((self.fixture / 'states_a.json').read_text())
        fetch['санкции'] = {'совпадения': []}
        inn = fetch['инн']
        body = json.dumps({'inn': inn, 'hits': [], 'asof': effective}).encode()
        refs = {'responses': {'r': {'source_id': 'санкции', 'subject_inn': inn,
            'request': {'subject_inn': inn}, 'body': body,
            'sha256': hashlib.sha256(body).hexdigest(), 'retrieved_at': retrieved or effective}},
            'fields': {'санкции.совпадения': [{'response_id': 'r', 'locator': '/hits',
                'subject_locator': '/inn', 'date_locators': {'effective_at': '/asof'},
                'effective_at': effective, 'coverage_complete': complete,
                'search_scope': {'subject_inn': inn, 'query': inn}}]}}
        return ec.enrich(fetch, now=now, raw_refs=refs)

    def saved(self, fetch, decision=None):
        payload = {'fetch': fetch, 'fin': {}, 'assessment': {},
                   'metadata': {'schema_version': 1, 'profile': 'нейтрально', 'workflow_status': 'complete'}}
        if decision is not None:
            payload['metadata']['decision'] = decision
        return self.store.create(payload, root=self.root / 'runs')

    def run_saved(self, record):
        import workflow_cli
        output = io.StringIO()
        with patch.object(self.watchlist, '_workflow_runner', return_value={
                'run_id': record['run_id'], 'revision': record['revision']}), \
             patch('evidence_contract.enrich', side_effect=AssertionError('must read stored evidence')), \
             patch('evidence_contract.evaluate', side_effect=AssertionError('must not rescore')), \
             contextlib.redirect_stdout(output):
            code = workflow_cli.main(['watch', 'monitor', '--store', str(self.root)])
        self.assertEqual(code, 0)
        return json.loads(output.getvalue())

    def test_canonical_ttl_boundary_public_monitor_and_restore(self):
        times = ['2026-10-16T08:59:59Z', '2026-10-16T09:00:00Z', '2026-10-16T09:00:01Z']
        records = [self.saved(self.evidence_fetch(now)) for now in times]
        claim = lambda r: next(c for c in r['payload']['fetch']['_evidence']['claims'].values()
                               if c['field'] == 'санкции.совпадения')
        self.assertEqual([(claim(r)['knowledge'], claim(r)['reason_code']) for r in records],
                         [('known', None), ('unknown', 'stale'), ('unknown', 'stale')])
        self.run_saved(records[0])
        lost = self.run_saved(records[1])
        self.assertEqual(lost['status'], 'partial')
        row = lost['data']['результаты'][0]
        self.assertEqual(row['статус'], 'не проверено')
        self.assertIn('stale', json.dumps(row['canonical_evidence']))
        self.assertEqual(row['изменения'], [])
        self.assertEqual([e['kind'] for e in lost['data']['events']], ['coverage_lost'])
        event = lost['data']['events'][0]
        self.assertEqual(event['origin']['from_run_id'], records[0]['run_id'])
        self.assertEqual(event['origin']['to_run_id'], records[1]['run_id'])
        # The stored stale baseline advances only after publication. Repeated stale support is quiet.
        self.assertEqual(self.run_saved(records[2])['data']['events'], [])
        fresh = self.saved(self.evidence_fetch('2026-10-16T09:00:02Z', effective='2026-10-16T09:00:02Z'))
        restored = self.run_saved(fresh)
        self.assertEqual([e['kind'] for e in restored['data']['events']], ['coverage_restored'])
        claimed = events.claim_events('evidence-reader', root=self.root / 'events')
        events.ack_events([e['event_id'] for e in claimed], consumer_id='evidence-reader', root=self.root / 'events')
        events.ingest({'дата': times[1], 'результаты': [row]}, root=self.root / 'events')
        self.assertEqual(events.list_events(root=self.root / 'events'), [])

    def test_evidence_retrieval_and_rule_hash_are_not_changes(self):
        before = self.evidence_fetch('2026-10-16T08:59:58Z')
        after = self.evidence_fetch('2026-10-16T08:59:59Z', retrieved='2026-10-16T08:59:59Z')
        after['_evidence']['rules_version'] = 'a' * 64
        self.run_saved(self.saved(before))
        result = self.run_saved(self.saved(after))['data']
        self.assertEqual(result['events'], [])
        self.assertEqual(result['результаты'][0]['изменения'], [])

    def test_incomplete_conflict_missing_raw_and_missing_claim_coverage(self):
        known = self.evidence_fetch('2026-10-16T08:59:59Z')
        prior = self.saved(known)
        for condition in ('coverage_incomplete', 'conflict', 'raw_unavailable', 'missing_claim'):
            with self.subTest(condition=condition):
                changed = copy.deepcopy(known)
                claims = changed['_evidence']['claims']
                cid, c = next((cid, c) for cid, c in claims.items() if c['field'] == 'санкции.совпадения')
                if condition == 'coverage_incomplete':
                    changed = self.evidence_fetch('2026-10-16T08:59:59Z', complete=False)
                elif condition == 'missing_claim':
                    del claims[cid]
                elif condition == 'conflict':
                    c['knowledge'], c['reason_code'] = 'conflict', 'conflicting_sources'
                else:
                    c['raw_ref'] = {'availability': 'raw_unavailable', 'response_id': None, 'sha256': None, 'locator': None}
                    c['reason_code'] = 'raw_unavailable'
                row = self.watchlist._saved_row(self.saved(changed), prior)
                emitted = events.ingest({'дата': '2026-10-16T09:00:00Z', 'результаты': [row]}, root=self.root / 'events')
                self.assertEqual([e['kind'] for e in emitted], ['coverage_lost'])
                self.assertEqual(row['статус'], 'не проверено')
                self.assertEqual(row['изменения'], [])

    def test_stored_decision_support_and_business_diff_remain_separate(self):
        fetch = json.loads((self.fixture / 'states_a.json').read_text())
        supported = {'coverage': {'required_rules': ['r'], 'supported_rules': ['r']}, 'missing_evidence': []}
        missing = {'coverage': {'required_rules': ['r'], 'supported_rules': []},
                   'missing_evidence': [{'rule_id': 'r', 'reason_codes': ['stale']}]}
        before = self.saved(fetch, supported)
        after_fetch = copy.deepcopy(fetch)
        after_fetch['егрюл']['руководитель'] = 'Changed director'
        after = self.saved(after_fetch, missing)
        row = self.watchlist._saved_row(after, before)
        emitted = events.ingest({'дата': '2026-10-16T09:00:00Z', 'результаты': [row]}, root=self.root / 'events')
        self.assertEqual({e['kind'] for e in emitted}, {'company_changed', 'coverage_lost'})
        self.assertEqual(row['статус'], 'не проверено')

    def test_browser_only_unknown_is_disclosed_without_coverage_alarm(self):
        fetch = json.loads((self.fixture / 'states_a.json').read_text())
        unsupported = self.watchlist.snapshot.fingerprint(fetch)['не_покрыты']
        self.assertTrue(unsupported)
        source = unsupported[0]
        fetch['_evidence'] = {'claims': {'c': {'source_id': source, 'field': source + '.test',
            'knowledge': 'unknown', 'reason_code': 'source_unavailable', 'raw_ref': {'availability': 'raw_unavailable'}}}}
        before = self.saved(fetch)
        after = self.saved(copy.deepcopy(fetch))
        row = self.watchlist._saved_row(after, before)
        self.assertEqual(row['статус'], 'без изменений')
        self.assertIn(source, row['не_покрыты_deal_killer'])
        self.assertIn('source_unavailable', json.dumps(row['canonical_evidence']))
        self.assertEqual(events.ingest({'дата': '2026-10-16T09:00:00Z', 'результаты': [row]}, root=self.root / 'events'), [])

    def test_bounded_fixed_worker_and_cli(self):
        import types
        from unittest.mock import Mock
        runner = Mock(return_value={'status': 'partial'})
        fake = types.ModuleType('process_runtime'); fake.run_script = runner
        with patch.dict(sys.modules, {'process_runtime': fake}):
            self.watchlist._workflow_runner('7707083893', 'нейтрально', self.root / 'runs')
        runner.assert_called_once_with('workflow_cli.py', ['run', 'start', '7707083893', '--profile',
            'нейтрально', '--mode', 'всё', '--store', str(self.root / 'runs'), '--json'], timeout=120)
        # End-to-end CLI read, including error envelope, without fetching.
        import subprocess
        cli = Path(__file__).resolve().parents[1] / 'scripts/workflow_cli.py'
        result = subprocess.run([sys.executable, str(cli), 'events', 'list', '--store', str(self.root / 'events')], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['data'], [])
        result = subprocess.run([sys.executable, str(cli), 'events', 'claim', '--consumer-id', '../bad', '--store', str(self.root / 'events')], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)['error']['code'], 'invalid_arguments')
    def test_cli_claim_retry_ack(self):
        import subprocess
        cli = Path(__file__).resolve().parents[1] / 'scripts/workflow_cli.py'
        queue = self.root / 'events'
        events.ingest(report(), root=queue)
        def invoke(*args):
            proc = subprocess.run([sys.executable, str(cli), 'events', *args, '--store', str(queue)], capture_output=True, text=True, check=False)
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            return json.loads(proc.stdout)['data']
        claimed = invoke('claim', '--consumer-id', 'cli', '--limit', '2')
        self.assertEqual(len(claimed), 2)
        one, two = [item['event_id'] for item in claimed]
        self.assertEqual(invoke('retry', one, '--consumer-id', 'cli')[0]['delivery']['state'], 'pending')
        self.assertEqual(invoke('ack', two, '--consumer-id', 'cli')[0]['delivery']['state'], 'acknowledged')
        self.assertEqual(invoke('ack', two, '--consumer-id', 'cli')[0]['delivery']['state'], 'acknowledged')

    def test_timeout_never_no_changes_and_budget(self):
        import run_workflow

        with patch.object(self.watchlist, '_workflow_runner', return_value={'статус': 'не проверено'}):
            report = self.watchlist.monitor(root=self.root)
        self.assertEqual(report['не_проверено'], 1)
        self.assertEqual(report['без_изменений'], 0)
        self.assertEqual([x['kind'] for x in report['events']], ['refresh_failed'])
        (self.root / 'watchlist.json').write_text(json.dumps({'контрагенты': [{'инн': '7707083893'}] * 51}))
        with patch.object(self.watchlist, '_workflow_runner', side_effect=AssertionError('over budget fetch')), \
             self.assertRaises(run_workflow.WorkflowError):
            self.watchlist.monitor(root=self.root)


if __name__ == '__main__': unittest.main(verbosity=2)
