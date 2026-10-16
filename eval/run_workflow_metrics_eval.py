#!/usr/bin/env python3
"""Synthetic, offline tests for voluntary local metrics (no pilot observations)."""
from __future__ import annotations

import importlib.util
import json
import multiprocessing
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))


def event(**changes):
    value = {'session_id': 'pilot-01', 'stage': 'install', 'active_seconds': 60,
             'waiting_seconds': 30, 'manual_steps': 1, 'outcome': 'abandoned',
             'reason_code': 'missing_optional_dependency'}
    value.update(changes)
    return value


def append_events(root, count):
    import workflow_metrics as metrics
    for _ in range(count):
        result = metrics.record(event(), enabled=True, root=root)
        if result['status'] != 'recorded':
            raise AssertionError(result)


class MetricsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.find_spec('workflow_metrics')
        assert spec is not None, 'workflow_metrics module is not implemented'
        import workflow_metrics
        cls.metrics = workflow_metrics

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / 'metrics'

    def tearDown(self):
        self.tmp.cleanup()

    def record(self, **changes):
        return self.metrics.record(event(**changes), enabled=True, root=self.root)

    def test_default_disabled_never_touches_filesystem_or_network(self):
        with mock.patch('os.open', side_effect=AssertionError('filesystem')), \
                mock.patch('os.mkdir', side_effect=AssertionError('mkdir')), \
                mock.patch.object(Path, 'home', side_effect=AssertionError('home')), \
                mock.patch.object(socket, 'socket', side_effect=AssertionError('network')):
            result = self.metrics.record(event(), root=self.root)
        self.assertEqual(result, {'schema_version': 1, 'status': 'disabled'})
        self.assertFalse(self.root.exists())

    def test_explicit_disabled_example_leaves_existing_root_empty(self):
        self.root.mkdir()
        self.assertEqual(self.metrics.record(event(), enabled=False,
                                            root=self.root)['status'], 'disabled')
        self.assertEqual(list(self.root.iterdir()), [])

    def test_environment_does_not_enrol_and_import_has_no_io(self):
        with mock.patch.dict(os.environ, {'INN_CHECK_METRICS': '1',
                                         'INN_CHECK_METRICS_ENABLED': 'true'}), \
                mock.patch('os.open', side_effect=AssertionError('filesystem')), \
                mock.patch('os.mkdir', side_effect=AssertionError('mkdir')), \
                mock.patch.object(socket, 'socket', side_effect=AssertionError('network')):
            spec = importlib.util.spec_from_file_location('isolated_metrics',
                                                        SCRIPTS / 'workflow_metrics.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self.assertEqual(module.record(event())['status'], 'disabled')

    def test_enabled_local_record_preserves_only_exact_allowlist(self):
        with mock.patch.object(socket, 'socket', side_effect=AssertionError('network')):
            self.assertEqual(self.record(), {'schema_version': 1, 'status': 'recorded'})
            result = self.metrics.summarize(root=self.root)
        records = (self.root / 'events.jsonl').read_text().splitlines()
        self.assertEqual([json.loads(line) for line in records], [event()])
        self.assertEqual(result['event_count'], 1)
        self.assertEqual(result['active_seconds_total'], 60)
        self.assertEqual(result['waiting_seconds_total'], 30)
        if os.name != 'nt':
            self.assertEqual((self.root / 'events.jsonl').stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)

    def test_unexpected_sensitive_fields_rejected_before_mkdir_without_echo(self):
        for key in ('inn', 'document', 'api_key', 'proxy', 'environment', 'notes'):
            with self.subTest(key=key):
                result = self.record(**{key: 'SYNTHETIC_PRIVATE_SENTINEL'})
                self.assertEqual(result['status'], 'invalid_event')
                self.assertNotIn('SYNTHETIC_PRIVATE_SENTINEL', json.dumps(result))
                self.assertFalse(self.root.exists())

    def test_all_fields_required_and_event_must_be_plain_dict(self):
        for key in event():
            value = event()
            del value[key]
            self.assertEqual(self.metrics.record(value, enabled=True,
                                                root=self.root)['status'], 'invalid_event')
        for value in (None, [], 'secret'):
            self.assertEqual(self.metrics.record(value, enabled=True,
                                                root=self.root)['status'], 'invalid_event')
        self.assertFalse(self.root.exists())

    def test_strict_bounded_numbers_identifiers_and_enable_flag(self):
        invalid = {'active_seconds': (-1, True, float('nan'), float('inf'), '1', 10**20),
                   'waiting_seconds': (-1, False, float('-inf'), None, 10**20),
                   'manual_steps': (-1, True, 1.5, '1', 10**20),
                   'session_id': ('7707083893', '../pilot-01', 'api-secret', 'pilot-7707083893',
                                  '', 'pilot-01\nsecret', 1),
                   'stage': ('token=secret', '../install', '', 1, None),
                   'outcome': ('client said secret', '', 1, None),
                   'reason_code': ('Bearer private', '', 1, None)}
        for key, values in invalid.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    self.assertEqual(self.record(**{key: value})['status'], 'invalid_event')
        for value in (1, 'true', None):
            self.assertEqual(self.metrics.record(event(), enabled=value,
                                                root=self.root)['status'], 'invalid_event')
        self.assertFalse(self.root.exists())

    def test_zero_fractional_durations_and_uuid_pseudonym(self):
        self.assertEqual(self.record(session_id='123e4567-e89b-42d3-a456-426614174000',
                                     active_seconds=0, waiting_seconds=0.25,
                                     manual_steps=0)['status'], 'recorded')

    def test_summary_counts_failure_abandonment_and_unfinished_starts(self):
        self.record(session_id='pilot-01', outcome='failed', reason_code='installation_failed')
        self.record(session_id='pilot-02')
        self.record(session_id='pilot-03', outcome='started', reason_code='none')
        self.record(session_id='pilot-04', outcome='started', reason_code='none')
        self.record(session_id='pilot-04', stage='finish', outcome='completed', reason_code='none')
        result = self.metrics.summarize(root=self.root)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['event_count'], 5)
        self.assertEqual(result['session_count'], 4)
        self.assertEqual(result['denominators'], {'all_starts': 4, 'all_events': 5})
        self.assertEqual(result['outcome_counts'], {'failed': 1, 'abandoned': 1,
                                                  'started': 1, 'completed': 1})
        self.assertEqual(result['completion'], {'numerator': 1, 'denominator': 4, 'rate': 0.25})
        self.assertEqual(result['active_seconds_total'], 300)
        self.assertEqual(result['waiting_seconds_total'], 150)
        self.assertEqual(result['manual_steps_total'], 5)
        self.assertEqual(result['empirical_acceptance'], 'not_assessed')
        self.assertNotIn('pilot-', json.dumps(result))

    def test_empty_or_missing_summary_is_not_acceptance_and_does_not_create(self):
        for existing in (False, True):
            if existing:
                self.root.mkdir()
            result = self.metrics.summarize(root=self.root)
            self.assertEqual(result['status'], 'empty')
            self.assertEqual(result['observation_status'], 'no_observations')
            self.assertEqual(result['completion'], {'numerator': 0, 'denominator': 0, 'rate': None})
            self.assertEqual(result['empirical_acceptance'], 'not_assessed')
            self.assertEqual(list(self.root.iterdir()) if existing else [], [])
            self.assertEqual(self.root.exists(), existing)

    def test_summary_of_existing_log_never_writes_or_uses_network(self):
        self.record()
        before = (self.root / 'events.jsonl').read_bytes()
        with mock.patch('os.mkdir', side_effect=AssertionError('mkdir')), \
                mock.patch('os.write', side_effect=AssertionError('write')), \
                mock.patch('os.fsync', side_effect=AssertionError('fsync')), \
                mock.patch.object(socket, 'socket', side_effect=AssertionError('network')):
            self.assertEqual(self.metrics.summarize(root=self.root)['status'], 'ok')
        self.assertEqual((self.root / 'events.jsonl').read_bytes(), before)

    def test_latest_abandonment_replaces_intermediate_completion(self):
        self.record(outcome='completed', reason_code='none')
        self.record(stage='manual', outcome='abandoned', reason_code='user_stopped')
        result = self.metrics.summarize(root=self.root)
        self.assertEqual(result['completion'], {'numerator': 0, 'denominator': 1, 'rate': 0.0})
        self.assertEqual(result['outcome_counts'], {'abandoned': 1})

    def test_record_uses_validated_snapshot_if_caller_mutates_input(self):
        value = event()
        original = self.metrics._valid_event

        def validate(snapshot):
            accepted = original(snapshot)
            value['api_key'] = 'SYNTHETIC_PRIVATE_SENTINEL'
            return accepted

        with mock.patch.object(self.metrics, '_valid_event', side_effect=validate):
            result = self.metrics.record(value, enabled=True, root=self.root)
        self.assertEqual(result['status'], 'recorded')
        self.assertEqual(json.loads((self.root / 'events.jsonl').read_text()), event())

    def test_oversized_storage_and_duplicate_json_fields_are_unavailable(self):
        self.record()
        target = self.root / 'events.jsonl'
        duplicate = json.dumps(event())[:-1] + ',"outcome":"completed"}\n'
        for payload in (duplicate, json.dumps(event()).replace('60', 'NaN') + '\n'):
            target.write_text(payload)
            self.assertEqual(self.metrics.summarize(root=self.root)['status'], 'invalid_storage')
        target.write_text(json.dumps(event()) + '\n')
        with mock.patch.object(self.metrics, 'MAX_STORAGE_BYTES', 8):
            self.assertEqual(self.record()['status'], 'storage_error')
            self.assertEqual(self.metrics.summarize(root=self.root)['status'], 'invalid_storage')

    def assert_event_cap_preserves_storage(self, cap):
        self.root.mkdir()
        target = self.root / 'events.jsonl'
        failed = event(session_id='pilot-1', active_seconds=0, waiting_seconds=0,
                       manual_steps=0, outcome='failed', reason_code='installation_failed')
        abandoned = event(session_id='pilot-2', active_seconds=0, waiting_seconds=0,
                          manual_steps=0, outcome='abandoned', reason_code='user_stopped')
        compact = event(session_id='pilot-3', stage='diff', active_seconds=0,
                        waiting_seconds=0, manual_steps=0, outcome='started', reason_code='none')
        def line(value):
            return (json.dumps(value, separators=(',', ':')) + '\n').encode()
        target.write_bytes(line(failed) + line(abandoned) + line(compact) * (cap - 3))
        self.assertLess(target.stat().st_size + len(line(compact)) * 2,
                        self.metrics.MAX_STORAGE_BYTES)
        self.assertEqual(self.metrics.record(compact, enabled=True,
                                            root=self.root)['status'], 'recorded')
        before = target.read_bytes()
        summary = self.metrics.summarize(root=self.root)
        self.assertEqual(summary['status'], 'ok')
        self.assertEqual(summary['event_count'], cap)
        self.assertEqual(summary['outcome_counts'], {'failed': 1, 'abandoned': 1, 'started': 1})
        self.assertEqual(self.metrics.record(compact, enabled=True,
                                            root=self.root)['status'], 'storage_error')
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(self.metrics.summarize(root=self.root), summary)

    def test_event_cap_refuses_append_without_losing_failures_or_aggregates(self):
        with mock.patch.object(self.metrics, 'MAX_EVENTS', 4):
            self.assert_event_cap_preserves_storage(4)

    def test_actual_reviewed_100000_event_cap_remains_readable(self):
        self.assertEqual(self.metrics.MAX_EVENTS, 100_000)
        self.assert_event_cap_preserves_storage(self.metrics.MAX_EVENTS)

    def test_append_refuses_complete_corrupt_log_without_changing_bytes(self):
        self.root.mkdir()
        target = self.root / 'events.jsonl'
        before = b'{"api_key":"SYNTHETIC_PRIVATE_SENTINEL"}\n'
        target.write_bytes(before)
        result = self.record()
        self.assertEqual(result['status'], 'storage_error')
        self.assertEqual(target.read_bytes(), before)
        self.assertNotIn('SYNTHETIC_PRIVATE_SENTINEL', json.dumps(result))

    def test_short_write_failure_rolls_back_without_corrupting_previous_records(self):
        self.record()
        target = self.root / 'events.jsonl'
        before = target.read_bytes()
        original = os.write
        calls = 0

        def short_then_fail(descriptor, payload):
            nonlocal calls
            calls += 1
            if calls == 1:
                return original(descriptor, payload[:8])
            raise OSError('SYNTHETIC_PRIVATE_SENTINEL')

        with mock.patch('os.write', side_effect=short_then_fail):
            result = self.record()
        self.assertEqual(result['status'], 'storage_error')
        self.assertNotIn('SYNTHETIC_PRIVATE_SENTINEL', json.dumps(result))
        self.assertEqual(target.read_bytes(), before)

    def test_default_root_is_local_and_created_only_after_explicit_enable(self):
        home = self.root.parent / 'home'
        home.mkdir()
        expected = home / '.local' / 'share' / 'inn-check-ru' / 'metrics'
        with mock.patch.object(Path, 'home', return_value=home):
            self.assertEqual(self.metrics.summarize()['status'], 'empty')
            self.assertFalse(expected.exists())
            self.assertEqual(self.metrics.record(event(), enabled=True)['status'], 'recorded')
        self.assertTrue((expected / 'events.jsonl').is_file())

    def test_corrupt_private_storage_is_rejected_without_echo_or_partial_counts(self):
        self.record()
        target = self.root / 'events.jsonl'
        for payload in ('{"api_key":"SYNTHETIC_PRIVATE_SENTINEL"}\n',
                        '{broken SYNTHETIC_PRIVATE_SENTINEL',
                        json.dumps(event()) + '\n' + '{broken'):
            target.write_text(payload)
            result = self.metrics.summarize(root=self.root)
            self.assertEqual(result['status'], 'invalid_storage')
            self.assertIsNone(result['event_count'])
            self.assertIsNone(result['completion']['denominator'])
            self.assertNotIn('SYNTHETIC_PRIVATE_SENTINEL', json.dumps(result))

    def test_symlink_root_and_log_and_ancestor_are_refused(self):
        outside = self.root.parent / 'outside'
        outside.mkdir()
        self.root.symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.record()['status'], 'storage_error')
        self.assertEqual(self.metrics.summarize(root=self.root)['status'], 'storage_error')
        self.assertEqual(list(outside.iterdir()), [])
        self.root.unlink()
        self.root.mkdir()
        target = outside / 'private'
        target.write_text('SYNTHETIC_PRIVATE_SENTINEL')
        (self.root / 'events.jsonl').symlink_to(target)
        self.assertEqual(self.record()['status'], 'storage_error')
        self.assertEqual(self.metrics.summarize(root=self.root)['status'], 'storage_error')
        self.assertEqual(target.read_text(), 'SYNTHETIC_PRIVATE_SENTINEL')
        (self.root / 'events.jsonl').unlink()
        self.root.rmdir()
        self.root.symlink_to(outside, target_is_directory=True)
        nested = self.root / 'nested'
        self.assertEqual(self.metrics.record(event(), enabled=True,
                                            root=nested)['status'], 'storage_error')
        self.assertFalse((outside / 'nested').exists())

    @unittest.skipIf(os.name == 'nt', 'POSIX hardlink/FIFO checks')
    def test_nonregular_and_hardlinked_log_refused(self):
        self.root.mkdir()
        target = self.root / 'events.jsonl'
        os.mkfifo(target)
        self.assertEqual(self.record()['status'], 'storage_error')
        self.assertEqual(self.metrics.summarize(root=self.root)['status'], 'storage_error')
        target.unlink()
        private = self.root.parent / 'private'
        private.write_text('SYNTHETIC_PRIVATE_SENTINEL')
        os.link(private, target)
        self.assertEqual(self.record()['status'], 'storage_error')
        self.assertEqual(private.read_text(), 'SYNTHETIC_PRIVATE_SENTINEL')

    def test_multiple_processes_append_whole_events_with_os_lock(self):
        ctx = multiprocessing.get_context('spawn')
        processes = [ctx.Process(target=append_events, args=(str(self.root), 25))
                     for _ in range(4)]
        for process in processes:
            process.start()
        for process in processes:
            process.join(30)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(len((self.root / 'events.jsonl').read_text().splitlines()), 100)
        result = self.metrics.summarize(root=self.root)
        self.assertEqual(result['event_count'], 100)
        self.assertEqual(result['session_count'], 1)


if __name__ == '__main__':
    unittest.main()
