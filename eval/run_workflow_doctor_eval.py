"""Installed core diagnostics never require SDK, secrets or network."""
import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.doctor = importlib.import_module('workflow_doctor')

    def test_core_only_readonly_no_key_leak(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / 'not-created'
            with patch.dict(os.environ, {'HOME': str(home), 'CHECKO_API_KEY': 'sentinel-key-123',
                                         'INN_CHECK_PROXY': 'http://alice:sentinel-password@proxy.invalid'  # trufflehog:ignore
                                         }), \
                    patch('urllib.request.urlopen', side_effect=AssertionError('network')), \
                    patch.object(self.doctor.metadata, 'version', side_effect=self.doctor.metadata.PackageNotFoundError):
                report = self.doctor.inspect('нейтрально', now='2026-10-16T09:00:00Z')
            self.assertEqual(set(report['sections']), {'installation', 'core', 'sdk', 'cache',
                                                       'automatic', 'manual', 'optional', 'network'})
            self.assertTrue(report['core_ready'], report)
            self.assertFalse(report['sdk_ready'])
            self.assertEqual(report['sdk']['state'], 'not_installed')
            self.assertFalse(report['network']['probed'])
            self.assertIn('next_actions', report)
            self.assertFalse(home.exists())
            text = json.dumps(report)
            self.assertNotIn('sentinel-key', text)
            self.assertNotIn('sentinel-password', text)
            self.assertNotIn('alice', text)

    def test_optional_binary_version_commands_checked(self):
        with patch.dict(os.environ, {'INN_CHECK_PDF_RENDERER': sys.executable,
                                     'INN_CHECK_TESSERACT': sys.executable}), \
                patch.object(self.doctor, '_version', create=True, return_value={
                    'version_ready': True, 'version': 'fixture version 1.0'}) as run:
            result = self.doctor._optional()['binaries']
        self.assertEqual(run.call_count, 2)
        for row in result.values():
            self.assertTrue(row['version_ready'])
            self.assertTrue(row['configured'])
            self.assertEqual(row['version'], 'fixture version 1.0')
            self.assertEqual(row['version_command'][0], sys.executable)

    def test_path_presence_never_implies_ocr_configuration(self):
        with patch.dict(os.environ, {'INN_CHECK_PDF_RENDERER': '', 'INN_CHECK_TESSERACT': ''}), \
                patch.object(self.doctor.shutil, 'which', return_value='/usr/bin/fixture'):
            rows = self.doctor._optional()['binaries']
        for row in rows.values():
            self.assertFalse(row['ready'])
            self.assertFalse(row['configured'])
            self.assertEqual(row['reason_code'], 'not_configured')

    def test_pdf_metadata_range_without_parser_import(self):
        for version, ready in [('6.20.0', False), ('6.21.0', True), ('6.22.1', True),
                               ('7.0.0', False), ('6.21.0rc1', False)]:
            with patch.object(self.doctor.metadata, 'version', return_value=version), \
                    patch.dict(os.environ, {'INN_CHECK_PDF_RENDERER': '', 'INN_CHECK_TESSERACT': ''}), \
                    patch.object(self.doctor.importlib, 'import_module', side_effect=AssertionError('parser import')):
                pdf = self.doctor._optional()['pdf']
            self.assertEqual(pdf['ready'], ready, version)
            self.assertEqual(pdf['required'], '>=6.21,<7')

    def test_probe_alone_enables_network_cache_and_fingerprint(self):
        import check_access
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with patch.dict(os.environ, {'HOME': str(home), 'INN_CHECK_PROXY': ''}), \
                    patch.object(check_access, 'run_probes', return_value=[]), \
                    patch.object(check_access, 'detect_ip_class', return_value='неизвестно'):
                plain = self.doctor.inspect(probe=False)
                self.assertFalse((home / '.cache').exists())
                self.assertFalse(plain['network']['probed'])
                probed = self.doctor.inspect(probe=True)
                self.assertTrue(probed['network']['probed'])
                self.assertEqual(probed['network']['state'], 'completed')
                path = home / '.cache' / 'inn-check-ru' / 'access.json'
                self.assertTrue(path.is_file())
                self.assertEqual(json.loads(path.read_text())['сеть']['отпечаток'],
                                 probed['network']['network_fingerprint'])
                self.assertEqual(probed['network']['ttl_hours'], 24)

    def test_sdk_incompatible_and_broken_import_distinct(self):
        for version, state in (('0.9.0', 'incompatible'), ('1.30.0', 'broken_import')):
            with patch.object(self.doctor.metadata, 'version', return_value=version), \
                    patch.object(self.doctor.importlib, 'import_module', side_effect=ImportError('secret')):
                result = self.doctor.sdk_status()
            self.assertEqual(result['state'], state)
            self.assertNotIn('secret', json.dumps(result))

    def test_optional_key_descriptors_survive_redaction(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {'HOME': directory, 'CHECKO_API_KEY': 'sentinel-key',
                                        'DADATA_API_KEY': ''}), \
                patch.object(self.doctor.shutil, 'which', return_value=None):
            report = self.doctor.inspect(now='2026-10-16T09:00:00Z')
        records = report['optional']['keys']
        self.assertIsInstance(records, list)
        by_name = {row['name']: row for row in records}
        self.assertEqual(by_name['CHECKO_API_KEY'], {'name': 'CHECKO_API_KEY', 'present': True,
                                                   'source': 'environment'})
        self.assertFalse(by_name['DADATA_API_KEY']['present'])
        self.assertNotIn('sentinel-key', json.dumps(report))

    def test_named_configuration_secrets_redact_copied_proxy_credentials(self):
        environment = {'CHECKO_API_KEY': '  checko-sentinel  ', 'DADATA_API_KEY': 'dadata-sentinel',
                       'INN_CHECK_PLUGIN_CHECKO_KEY': 'plugin-checko-sentinel',
                       'INN_CHECK_PLUGIN_DADATA_KEY': 'plugin-dadata-sentinel',
                       'INN_CHECK_PROXY': 'http://proxy-user:proxy%2Dpassword@project.invalid:3128',  # decoded fixture: http://proxy-user:proxy-password@project.invalid:3128 trufflehog:ignore
                       'HTTPS_PROXY': 'http://upper-user:upper-password@upper.invalid:3128',  # trufflehog:ignore
                       'https_proxy': 'http://lower-user:lower-password@lower.invalid:3128',  # trufflehog:ignore
                       'HTTP_PROXY': 'http://http-user:http-password@http.invalid:3128',  # trufflehog:ignore
                       'http_proxy': 'http://small-user:small-password@http.invalid:3128',  # trufflehog:ignore
                       'ALL_PROXY': 'http://all-user:all-password@all.invalid:3128',  # trufflehog:ignore
                       'all_proxy': 'http://small-all-user:small-all-password@all.invalid:3128',  # trufflehog:ignore
                       'FTP_PROXY': 'http://ftp-user:ftp-password@ftp.invalid:3128',  # trufflehog:ignore
                       'ftp_proxy': 'http://small-ftp-user:small-ftp-password@ftp.invalid:3128',  # trufflehog:ignore
                       'UNRELATED_SECRET': 'unrelated-host-secret'}
        class NamedEnvironment(dict):
            def items(self):
                raise AssertionError('doctor must collect configuration through named lookups')
        # A configuration-specific reader needs only named lookups. A mapping
        # without enumeration catches accidental collection of host secrets.
        with patch.object(self.doctor.os, 'environ', NamedEnvironment(environment)):
            secrets = self.doctor._secrets()
        for key, value in environment.items():
            if key != 'UNRELATED_SECRET':
                self.assertIn(value, secrets, key)
        self.assertNotIn(environment['UNRELATED_SECRET'], secrets)
        copied = {'diagnostic': ' '.join([*environment.values(), 'checko-sentinel', 'proxy-password',
                                          'proxy-user', 'upper-password', 'small-ftp-user'])}
        cleaned = json.dumps(self.doctor.redact(copied, secrets=secrets))
        for value in ('checko-sentinel', 'dadata-sentinel', 'plugin-checko-sentinel',
                      'plugin-dadata-sentinel', 'proxy-password', 'proxy%2Dpassword',
                      'proxy-user', 'upper-password', 'small-ftp-user'):
            self.assertNotIn(value, cleaned)

    def test_public_binary_paths_retained_with_known_secret_parts_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / 'ocr-checko-path-sentinel'
            binary.write_text('fixture')
            binary.chmod(0o700)
            with patch.dict(os.environ, {'HOME': directory, 'CHECKO_API_KEY': 'checko-path-sentinel',
                                         'INN_CHECK_TESSERACT': str(binary),
                                         'INN_CHECK_PDF_RENDERER': sys.executable}), \
                    patch.object(self.doctor, '_version', return_value={
                        'version_ready': True, 'version': 'tesseract 5.5.1'}):
                report = self.doctor.inspect(now='2026-10-16T09:00:00Z')
            rows = report['optional']['binaries']
            self.assertEqual(rows['pdftoppm']['configured_path'], sys.executable)
            self.assertEqual(rows['pdftoppm']['version_command'], [sys.executable, '-v'])
            self.assertEqual(rows['tesseract']['configured_path'], str(binary).replace(
                'checko-path-sentinel', '[REDACTED]'))
            self.assertNotIn('checko-path-sentinel', json.dumps(report))


if __name__ == '__main__':
    unittest.main()
