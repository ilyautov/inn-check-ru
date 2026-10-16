#!/usr/bin/env python3
"""Frozen synthetic final-output corpus; no assertion of empirical accuracy."""
import copy
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'scripts'), str(ROOT / 'eval')]


class WorkflowScenarioEval(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert (ROOT / 'benchmark/decision_bench.py').exists(), 'Phase 6A evaluator missing'
        from benchmark import decision_bench
        cls.bench = decision_bench
        cls.cases = decision_bench.load_cases()
        cls.report = decision_bench.evaluate(cls.cases, now=decision_bench.CLOCK)

    def test_exact_report_contract_and_real_path(self):
        self.assertEqual(set(self.report), {'schema_version', 'engine_hash', 'rules_hash', 'corpus_hash',
            'seed', 'clock', 'counts', 'metrics', 'exclusions', 'cases'})
        self.assertEqual(self.report['counts']['evaluated_cases'], len(self.cases))
        for metric in self.report['metrics'].values():
            self.assertIn('numerator', metric)
            self.assertIn('denominator', metric)
        for case in self.report['cases']:
            self.assertEqual(case['channels'], ['workflow_cli_json', 'mcp_worker_json', 'dossier_model', 'dossier_markdown', 'snapshot'])
        self.assertGreater(self.report['metrics']['coverage']['denominator'], 0)
        self.assertGreater(self.report['metrics']['asserted']['denominator'], 0)
        eligible = [r for r in self.report['cases'] if r['oracle_status'] == 'synthetic_specification' and not r['error_code']]
        self.assertEqual(self.report['metrics']['unsupported_assertions']['denominator'],
            sum(r['asserted'] + len(r['recommendation_sentences']) + r['rendered_assertions'] for r in eligible))
        self.assertTrue(all(r['rendered_assertions'] == len(r['output_assertions']) > 0 for r in eligible))

    def test_frozen_corpus_has_six_segments_and_no_invented_reviews(self):
        self.assertEqual({c['segment'] for c in self.cases}, {'full', 'simplified', 'ip', 'bank', 'npo', 'restricted'})
        for case in self.cases:
            self.assertTrue(case['synthetic'])
            self.assertEqual(case['independent_reviews'], [])
            self.assertEqual(case['adjudication']['status'], 'awaiting_external_review')
            self.assertTrue(case['sources'])
            self.assertTrue(case['expected_atomic_claims'])
        self.bench.verify_frozen()

    def test_typed_boundary_contract_is_frozen_separately(self):
        from benchmark import decision_schema
        manifest = self.bench.verify_frozen()
        self.assertEqual(manifest['output_schema_revision'], decision_schema.SCHEMA_REVISION)
        self.assertEqual(manifest['output_schema_sha256'], self.bench._file_hash(ROOT / manifest['output_schema_path']))

    def test_final_json_text_and_oracle_gates(self):
        self.bench.assert_gate(self.report)
        for name in ('unsupported_assertions', 'contradicted_assertions', 'false_green_required_unknown'):
            self.assertEqual(self.report['metrics'][name]['numerator'], 0, self.report['cases'])
        self.assertGreater(self.report['metrics']['abstention']['numerator'], 0)
        self.assertGreater(self.report['counts']['oracle_excluded'], 0)
        self.assertLess(self.report['metrics']['oracle_agreement']['denominator'], len(self.cases))
        self.assertTrue(any(e['reason'] == 'missing_independent_oracle' for e in self.report['exclusions']))

    def test_hashes_reject_tampered_input(self):
        altered = copy.deepcopy(self.cases)
        altered[0]['fetch']['риски']['численность_сотрудников'] += 1
        with self.assertRaisesRegex(ValueError, 'source_hash_mismatch'):
            self.bench.evaluate(altered, now=self.bench.CLOCK)

    def test_existing_recorded_registry_parser_reaches_final_dossier(self):
        from unittest.mock import patch

        import run_run_workflow_eval as fixtures
        manifest = self.bench.verify_frozen()
        raw_path = ROOT / manifest['recorded_fixture_source']['path']
        self.assertEqual(self.bench._file_hash(raw_path), manifest['recorded_fixture_source']['sha256'])
        with tempfile.TemporaryDirectory() as directory:
            root = (Path(directory) / 'runs').resolve()
            with patch.object(fixtures.collector, 'collect', side_effect=fixtures.recorded_collect):
                saved = self.bench._cli(root, 'start', fixtures.INN, '--profile', 'отсрочка')
            self.assertNotEqual(saved['status'], 'error', saved)
            rendered = self.bench._cli(root, 'render', saved['run_id'], '--format', 'markdown')
            self.assertNotEqual(rendered['status'], 'error', rendered)
            claim = next(c for c in saved['data']['fetch']['_evidence']['claims'].values()
                         if c['field'] == 'егрюл.дата_регистрации')
            self.assertEqual(claim['value'], '16.08.2002')
            self.assertEqual(claim['raw_ref']['availability'], 'available')
            self.assertIn(claim['claim_id'], rendered['data']['content'])
            self.assertFalse(saved['data']['metadata']['decision']['can_recommend'])

    def test_real_sdk_stdio_reads_same_final_json_and_text(self):
        # Explicit dependency gate; run via uv --with mcp==1.30.0 when SDK absent.
        import importlib.metadata
        try:
            importlib.metadata.version('mcp')
        except importlib.metadata.PackageNotFoundError:
            self.skipTest('real SDK stdio requires optional mcp>=1.30,<2; worker JSON was exercised')
        from mcp_handshake import StdioClient, decode_tool_json
        with tempfile.TemporaryDirectory() as directory:
            saved = self.bench.run_case(self.cases[0], now=self.bench.CLOCK, root=Path(directory) / 'runs')
            import os
            client = StdioClient([sys.executable, '-B', str(ROOT / 'mcp/server.py')],
                env=dict(os.environ, INN_CHECK_RUNS_DIR=str((Path(directory) / 'runs').resolve())), cwd=directory)
            try:
                client.initialize()
                for number, (name, arguments, expected) in enumerate([
                    ('counterparty_run_read', {'run_id': saved['run_id']}, saved['payload']),
                    ('counterparty_run_render', {'run_id': saved['run_id'], 'format': 'markdown'}, saved['markdown'])], 5):
                    output = decode_tool_json(client.call({'jsonrpc': '2.0', 'id': number, 'method': 'tools/call',
                        'params': {'name': name, 'arguments': arguments}}))
                    self.assertNotEqual(output['status'], 'error', output)
                    self.assertEqual(output['data'] if name.endswith('read') else output['data']['content'], expected)
            finally:
                client.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
