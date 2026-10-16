#!/usr/bin/env python3
"""Synthetic offline integration for explicit local metrics CLI commands."""
from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import workflow_cli
import workflow_metrics


def event(**changes):
    value = {'session_id': 'pilot-01', 'stage': 'install', 'active_seconds': 60,
             'waiting_seconds': 30, 'manual_steps': 1, 'outcome': 'abandoned',
             'reason_code': 'missing_optional_dependency'}
    value.update(changes)
    return value


class MetricsCLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / 'metrics'
        self.file = self.base / 'event.json'
        self.file.write_text(json.dumps(event()))

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args, expected=0):
        result = subprocess.run([sys.executable, str(SCRIPTS / 'workflow_cli.py'), *args],
                                check=False, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        self.assertEqual(result.stderr, '')
        self.assertEqual(len(result.stdout.splitlines()), 1)
        return json.loads(result.stdout)

    def test_disabled_record_with_missing_file_and_bad_root_performs_no_io(self):
        # The CLI must not even open EVENT.json unless the operator opts in.
        with mock.patch('os.open', side_effect=AssertionError('filesystem')), \
                mock.patch('os.mkdir', side_effect=AssertionError('filesystem')), \
                mock.patch.object(Path, 'open', side_effect=AssertionError('filesystem')), \
                mock.patch.object(Path, 'home', side_effect=AssertionError('filesystem')), \
                mock.patch.object(socket, 'socket', side_effect=AssertionError('network')):
            args = workflow_cli.parser().parse_args([
                '--store', '\x00/../bad', 'metrics', 'record', 'missing.json', '--json'])
            data = workflow_cli._dispatch(args)
        self.assertEqual(data['data'], {'schema_version': 1, 'status': 'disabled'})
        self.assertFalse(self.root.exists())

    def test_explicit_record_summary_and_export_only_aggregate_to_stdout(self):
        recorded = self.cli('--store', str(self.root), 'metrics', 'record',
                            str(self.file), '--enabled', '--json')
        self.assertEqual(recorded['data']['status'], 'recorded')
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        summary = self.cli('metrics', 'summarize', '--store', str(self.root), '--json')
        exported = self.cli('metrics', 'export', '--store', str(self.root), '--json')
        self.assertEqual(summary, exported)
        self.assertEqual(exported['data'], workflow_metrics.summarize(root=self.root))
        self.assertEqual(exported['data']['denominators'], {'all_starts': 1, 'all_events': 1})
        self.assertEqual(exported['data']['active_seconds_total'], 60)
        self.assertEqual(exported['data']['waiting_seconds_total'], 30)
        self.assertNotIn('pilot-01', json.dumps(exported))
        self.assertNotIn('session_id', json.dumps(exported))
        self.assertEqual({p.name: p.read_bytes() for p in self.root.iterdir()}, before)

    def test_missing_summary_does_not_create_root_or_claim_acceptance(self):
        result = self.cli('metrics', 'summarize', '--store', str(self.root), '--json')
        self.assertEqual(result['data']['status'], 'empty')
        self.assertIsNone(result['data']['completion']['rate'])
        self.assertEqual(result['data']['empirical_acceptance'], 'not_assessed')
        self.assertFalse(self.root.exists())

    def test_failed_abandoned_denominators(self):
        self.cli('metrics', 'record', str(self.file), '--store', str(self.root), '--enabled')
        self.file.write_text(json.dumps(event(session_id='pilot-02', outcome='failed',
                                             reason_code='installation_failed')))
        self.cli('metrics', 'record', str(self.file), '--store', str(self.root), '--enabled')
        result = self.cli('metrics', 'export', '--store', str(self.root), '--json')['data']
        self.assertEqual(result['denominators']['all_starts'], 2)
        self.assertEqual(result['outcome_counts'], {'abandoned': 1, 'failed': 1})
        self.assertEqual(result['completion']['denominator'], 2)

    def test_invalid_input_is_bounded_redacted_and_never_creates_storage(self):
        private = 'SYNTHETIC_PRIVATE_SENTINEL'
        values = ['{"api_key":"' + private + '"}',
                  '{broken ' + private,
                  json.dumps(event())[:-1] + ',"outcome":"failed"}',
                  json.dumps(event()).replace('60', 'NaN'),
                  json.dumps(event()) + ' ' * 4096,
                  '[' * 2000 + private]
        for raw in values:
            with self.subTest(raw_length=len(raw)):
                self.file.write_text(raw)
                result = self.cli('metrics', 'record', str(self.file), '--enabled',
                                  '--store', str(self.root), '--json', expected=2)
                self.assertEqual(result['status'], 'error')
                self.assertEqual(result['error']['code'], 'invalid_event')
                self.assertNotIn(private, json.dumps(result))
                self.assertNotIn(str(self.file), json.dumps(result))
                self.assertFalse(self.root.exists())

    @unittest.skipIf(os.name == 'nt', 'POSIX FIFO input')
    def test_fifo_input_is_refused_without_waiting_for_writer(self):
        self.file.unlink()
        os.mkfifo(self.file)
        result = self.cli('metrics', 'record', str(self.file), '--enabled',
                          '--store', str(self.root), expected=2)
        self.assertEqual(result['error']['code'], 'invalid_event')
        self.assertFalse(self.root.exists())

    def test_corrupt_export_has_unavailable_counts_and_no_raw_input(self):
        self.root.mkdir()
        (self.root / 'events.jsonl').write_text('{"api_key":"SYNTHETIC_PRIVATE_SENTINEL"}\n')
        result = self.cli('metrics', 'export', '--store', str(self.root), '--json', expected=2)
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['data']['status'], 'invalid_storage')
        self.assertIsNone(result['data']['event_count'])
        self.assertNotIn('SYNTHETIC_PRIVATE_SENTINEL', json.dumps(result))

    def test_enabled_input_and_summary_never_use_network(self):
        with mock.patch.object(socket, 'socket', side_effect=AssertionError('network')):
            for command in (['metrics', 'record', str(self.file), '--enabled'],
                            ['metrics', 'summarize'], ['metrics', 'export']):
                args = workflow_cli.parser().parse_args([*command, '--store', str(self.root)])
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertNotEqual(workflow_cli._dispatch(args)['status'], 'error')
                self.assertEqual(output.getvalue(), '')


if __name__ == '__main__':
    unittest.main()
