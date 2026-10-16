#!/usr/bin/env python3
"""Offline artifact/source contract; no claims about real agent-host installation."""
import ast
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'eval'))
from run_version_gate import _toml


class CapabilityParity(unittest.TestCase):
    def test_core_metadata_and_inventory(self):
        text = (ROOT / 'pyproject.toml').read_text(encoding='utf-8')
        project = _toml(ROOT / 'pyproject.toml')['project']
        caps = json.loads((ROOT / 'data/workflow_capabilities.json').read_text())
        self.assertEqual(caps['schema_version'], 1)
        self.assertEqual(caps['version'], project['version'])
        self.assertEqual(project['dependencies'], [])
        self.assertEqual(project['scripts'], caps['entry_points'])
        self.assertIn('"pypdf>=6.21,<7"', text)
        self.assertIn('"mcp>=1.30,<2"', text)
        declared = set(re.findall(r'"([a-z_0-9]+)"', text.split('py-modules = [')[1].split(']')[0]))
        excluded = {'build_release_zip', 'make_social_preview', 'build_sitemap'}
        actual = {p.stem for p in (ROOT / 'scripts').glob('*.py')} - excluded
        self.assertEqual(declared, actual, 'all runtime modules must ship in the wheel')
        for target in project['scripts'].values():
            module, function = target.split(':')
            location = ROOT / ('mcp/server.py' if module == 'inn_check_ru_mcp.server' else f'scripts/{module}.py')
            tree = ast.parse(location.read_text())
            self.assertIn(function, [n.name for n in tree.body if isinstance(n, ast.FunctionDef)])
        self.assertIn('inn_check_ru_data = ["*.json"', text)
        for name in ('cache_policy.json', 'evidence_policy.json', 'provider_policy.schema.json', 'workflow_capabilities.json'):
            self.assertTrue((ROOT / 'data' / name).is_file())

    def test_sdk_snapshot_and_security_boundary(self):
        caps = json.loads((ROOT / 'data/workflow_capabilities.json').read_text())
        snapshot = json.loads((ROOT / 'eval/fixtures/workflow-tools.json').read_text())
        self.assertEqual(len(snapshot), caps['mcp_tool_count'])
        names = [tool['name'] for tool in snapshot]
        self.assertEqual(len(names), len(set(names)))
        for tool in snapshot:
            self.assertFalse({'root', 'path', 'executable', 'command', 'store'} & set(tool['inputSchema'].get('properties', {})), tool['name'])
        for channel in caps['channels'].values():
            self.assertEqual(len(channel['settings']), 8)
            for config in channel['settings'].values():
                self.assertIn(config['status'], ('supported', 'unsupported'))
                self.assertTrue(config['route'] if config['status'] == 'supported' else config['limitation'])
        registered = {v['name'] for v in json.loads((ROOT / 'server.json').read_text())['packages'][0]['environmentVariables']}
        self.assertTrue({v['environment'] for v in caps['channels']['plain_mcp']['settings'].values()} <= registered)
        template = json.loads((ROOT / 'packaging/mcpb/manifest.template.json').read_text())
        self.assertIn('DADATA_API_KEY', template['server']['mcp_config']['env'])

    def test_mcp_pdf_claim_matches_public_document_inputs(self):
        caps = json.loads((ROOT / 'data/workflow_capabilities.json').read_text())
        snapshot = json.loads((ROOT / 'eval/fixtures/workflow-tools.json').read_text())
        extraction = next(t for t in snapshot if t['name'] == 'counterparty_document_extract')
        self.assertEqual(set(extraction['inputSchema']['properties']), {'text', 'run_id', 'artifact_id', 'ocr'})
        self.assertEqual(caps['channels']['plain_mcp']['pdf'], 'unsupported_public_input_use_host_cli')
        self.assertIn('text', caps['channels']['plain_mcp']['notes'])
        self.assertIn('managed', caps['channels']['plain_mcp']['notes'])
        self.assertEqual(caps['channels']['cli']['pdf'], 'optional_extra_and_host_binaries')
        # The managed-artifact registry is authoritative: no PDF registration is
        # accepted, so optional host binaries cannot imply an MCP PDF route.
        sys.path.insert(0, str(ROOT / 'scripts'))
        import run_store
        self.assertNotIn('pdf', run_store._ARTIFACT_FORMATS)

    def test_platform_matrix_is_declared_without_host_claim(self):
        caps = json.loads((ROOT / 'data/workflow_capabilities.json').read_text())
        self.assertEqual(caps['platform_matrix']['python_versions'], ['3.10', '3.12'])
        ci = (ROOT / '.github/workflows/ci.yml').read_text()
        self.assertIn('os: [ubuntu-latest, macos-latest, windows-latest]', ci)
        self.assertIn("python: ['3.10', '3.12']", ci)
        self.assertTrue(all(c['host_acceptance'] == 'not_run' for c in caps['channels'].values()))
        self.assertEqual(caps['empirical_validation'], 'awaiting_external_evidence')


if __name__ == '__main__':
    unittest.main(verbosity=2)
