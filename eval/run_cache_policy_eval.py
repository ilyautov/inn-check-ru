"""Offline cache inventory, freshness and read-only contracts."""
import datetime as dt
import gzip
import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))


class CachePolicyTests(unittest.TestCase):
    def setUp(self):
        self.cp = importlib.import_module('cache_policy')

    def test_inventory_all_updaters_and_pure_reads(self):
        import refresh_indexes
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'not-created'
            with patch('urllib.request.urlopen', side_effect=AssertionError('network')):
                report = self.cp.status('нейтрально', now='2026-10-16T09:00:00Z', root=root)
                plan = self.cp.plan('нейтрально', now='2026-10-16T09:00:00Z', root=root)
            self.assertFalse(root.exists())
            self.assertEqual({x['updater'] for x in report['datasets']}, set(refresh_indexes.ИНДЕКСЫ))
            for item in report['datasets']:
                self.assertIn(item['state'], {'missing', 'fresh', 'stale', 'unknown', 'disabled'})
                for field in ('metadata_source', 'ttl_rationale', 'ttl_policy_version',
                              'estim_bytes', 'estim_bytes_unknown_reason', 'rf_network_required',
                              'applicable_profiles', 'argv', 'cache_path', 'next_due_at'):
                    self.assertIn(field, item)
            self.assertFalse(plan['executes_updates'])
            self.assertTrue(all(isinstance(x['argv'], list) for x in plan['actions']))
            self.assertFalse(any(x['dataset_id'] == 'rospatent_tz' for x in plan['minimal_actions']))

    def test_existing_metadata_basis_and_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'tz' / 'индекс.json.gz'
            path.parent.mkdir()
            with gzip.open(path, 'wt') as stream:
                json.dump({'дата_выгрузки': '2026-09-01', 'построен': '2026-10-16', 'по_инн': {}}, stream)
            def row(day):
                return next(x for x in self.cp.status('нейтрально', now=day, root=directory)['datasets']
                            if x['dataset_id'] == 'rospatent_tz')
            self.assertEqual(row('2026-10-16T00:00:00Z')['state'], 'fresh')
            self.assertEqual(row('2026-10-17T00:00:00Z')['state'], 'stale')
            self.assertEqual(row('2026-10-16T00:00:00Z')['published_at'], '2026-09-01')
            self.assertEqual(row('2026-10-16T00:00:00Z')['next_due_at'], '2026-10-15T00:00:00Z')

    def test_sanctions_alias_ttl_and_schedule_margin(self):
        item = next(x for x in self.cp.inventory() if x['dataset_id'] == 'sanctions')
        self.assertIn('sanctions_check', item['aliases'])
        self.assertLessEqual(item['ttl_days'], 7)
        self.assertEqual(item['refresh_margin_days'], 1)
        from schedule_templates import render
        schedule = render([sys.executable, '-m', 'workflow_cli', 'cache', 'status'], interval_days=6)
        self.assertLessEqual(schedule['maximum_interval_days'], 6)
        self.assertIn('windows_xml', schedule)
        self.assertNotIn('<key>Day</key>', schedule['plist'])
        self.assertFalse(schedule['installed'])

    def test_legacy_refresh_schedule_respects_sanctions_margin(self):
        import refresh_indexes
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'refresh.json'
            config.write_text(json.dumps({'индексы': {'sanctions_check': {}}}))
            result = refresh_indexes.расписание(str(config), день=5)
            self.assertNotIn('<key>Day</key>', result['plist'])
            self.assertIn('windows_xml', result)
            self.assertLessEqual(result['maximum_interval_days'], 6)

    def test_all_source_ttls_and_actual_metadata_bases(self):
        basis = {
            'cbr_registries': {'реестры': {'x': {'скачан': '2026-10-01'}}},
            'fts_registries': {'реестры': {'x': {'скачан': '2026-10-01'}}},
            'rkn_registries': {'реестры': {'x': {'скачан': '2026-10-01'}}},
            'cbr_warning': {'скачано_utc': '2026-10-01T00:00:00+00:00'},
            'disq_dump': {'дата_выгрузки': '2026-10-01'},
            'rzn_licenses': {'наборы': {'x': {'название': 'fixture', 'дата_выгрузки': '2026-10-01'}}},
            'rospatent_tz': {'дата_выгрузки': '2026-10-01'},
            'rar_licenses': {'выгрузка': '2026-10-01'},
            'fsa_registries': {'охват': {'x': {'по': '2026-10-01'}}},
            'minpromtorg': {'наборы': {'x': {'выгрузка': '2026-10-01'}}},
            'uc_mintsifry': {'скачан': '2026-10-01'},
            'erknm': {'охват': {'выгрузка': '2026-10-01'}},
        }
        for item in self.cp.inventory():
            name = item['dataset_id']
            if name not in basis:
                continue
            with self.subTest(name=name):
                module = importlib.import_module(item['updater'])
                constant = ('TTL_ДНЕЙ' if name == 'cbr_warning' else
                            'СВЕЖЕСТЬ_ВЫГРУЗКИ_ДНЕЙ' if name == 'rar_licenses' else 'СВЕЖЕСТЬ_ДНЕЙ')
                self.assertEqual(item['ttl_days'], getattr(module, constant))
                origin = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)
                state = self.cp._freshness(item, basis[name], origin + dt.timedelta(days=item['ttl_days']))
                self.assertEqual(state[0], 'fresh')
                state = self.cp._freshness(item, basis[name], origin + dt.timedelta(days=item['ttl_days'] + 1))
                self.assertEqual(state[0], 'stale')

    def test_sanctions_actual_mtime_cannot_be_refreshed_by_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc).timestamp()
            for name in ('terrorists-list.html', 'sdn.csv', 'eu-sanctions.xml'):
                body = root / name
                body.write_bytes(b'old body')
                os.utime(body, (old, old))
                Path(str(body) + '.meta.json').write_text(json.dumps({'скачан': '2026-10-16 00:00:00'}))
            row = next(x for x in self.cp.status('нейтрально', now='2026-10-16T09:00:00Z', root=root)['datasets']
                       if x['dataset_id'] == 'sanctions')
            self.assertEqual(row['state'], 'stale')

    def test_truncated_gzip_is_unknown_and_orphans_not_current(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'tz'
            path.mkdir()
            (path / 'индекс.json.gz').write_bytes(b'\x1f\x8b\x08')
            rows = self.cp.status('нейтрально', now='2026-10-16T09:00:00Z', root=directory)['datasets']
            self.assertEqual(next(x for x in rows if x['dataset_id'] == 'rospatent_tz')['state'], 'unknown')
            (path / 'индекс.json.gz').unlink()
            orphan = Path(directory) / '.workflow-generations' / 'rospatent_tz' / 'orphan'
            orphan.mkdir(parents=True)
            rows = self.cp.status('нейтрально', now='2026-10-16T09:00:00Z', root=directory)['datasets']
            self.assertEqual(next(x for x in rows if x['dataset_id'] == 'rospatent_tz')['state'], 'missing')

    def test_explicit_root_status_does_not_import_updater_user_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'tz'
            path.mkdir()
            with gzip.open(path / 'индекс.json.gz', 'wt') as stream:
                json.dump({'дата_выгрузки': '2026-10-01', 'по_инн': {}}, stream)
            original = importlib.import_module
            updaters = {x['updater'] for x in self.cp.inventory()}
            def guarded(name, *args, **kwargs):
                if name in updaters:
                    raise AssertionError('updater import')
                return original(name, *args, **kwargs)
            with patch.object(self.cp.importlib, 'import_module', side_effect=guarded):
                rows = self.cp.status('нейтрально', now='2026-10-16T09:00:00Z', root=directory)['datasets']
            self.assertEqual(next(x for x in rows if x['dataset_id'] == 'rospatent_tz')['state'], 'fresh')

    def test_explicit_validity_overrides_old_source_date(self):
        item = next(x for x in self.cp.inventory() if x['dataset_id'] == 'disq_dump')
        state = self.cp._freshness(item, {'дата_выгрузки': '2026-01-01',
                                         'действителен_до': '2026-10-20'},
                                    dt.datetime(2026, 10, 16, tzinfo=dt.timezone.utc))
        self.assertEqual(state[0], 'fresh')
        self.assertEqual(state[4], '2026-10-20T00:00:00Z')

    def test_unknown_policy_not_false_freshness(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'opendata'
            path.mkdir()
            policy = self.cp._item('opendata_refresh')
            for dataset in policy['required_archive_datasets']:
                folder = path / dataset
                folder.mkdir()
                archive = folder / 'data-20261001-structure-20200101.zip'
                archive.write_bytes(b'not parsed by status')
                Path(str(archive) + '.meta.json').write_text('{}')
            for dataset in policy['required_index_datasets']:
                index = path / dataset / ('index-%s.bin' % dataset)
                index.touch()
                Path(str(index) + '.meta.json').write_text('{}')
            item = next(x for x in self.cp.status('нейтрально', now=dt.datetime.now(dt.timezone.utc),
                                                root=directory)['datasets']
                        if x['dataset_id'] == 'opendata_refresh')
            self.assertNotEqual(item['state'], 'fresh')
            self.assertIsNone(item['ttl_days'])
            self.assertEqual(item['reason_code'], 'policy_unknown')

    def test_cbr_metadata_without_referenced_body_is_missing(self):
        import cbr_warning
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cbr_warning'
            path.mkdir()
            (path / 'meta.json').write_text(json.dumps({'скачано_utc': '2026-10-16T00:00:00Z',
                                                       'файл': 'black-list.missing.json'}))
            report = self.cp.plan('нейтрально', now='2026-10-16T09:00:00Z', root=directory)
            row = next(x for x in report['datasets'] if x['dataset_id'] == 'cbr_warning')
            self.assertEqual(row['state'], 'missing')
            self.assertIn('cbr_warning', [x['dataset_id'] for x in report['actions']])
            actual, _ = cbr_warning.lookup('7707083893', cache_dir=path)
            self.assertIsNone(actual)

    def test_schedule_resolves_from_outside_checkout(self):
        import subprocess

        import refresh_indexes
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'refresh 100%.json'
            config.write_text(json.dumps({'индексы': {'rospatent_tz': {}}}))
            schedule = refresh_indexes.расписание(str(config))
            # Keep generated launch prefix, replace update with harmless config example.
            prefix = schedule['argv'][:schedule['argv'].index('--прогон')]
            env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
            process = subprocess.run([*prefix, '--пример-конфига'], cwd=directory, env=env,
                                     capture_output=True, timeout=5, check=False)
            self.assertEqual(process.returncode, 0, process.stderr)

    def test_required_files_match_all_legacy_readers(self):
        import opendata_index
        import opendata_refresh
        policy = self.cp._item('opendata_refresh')
        self.assertEqual(set(policy['required_archive_datasets']), set(opendata_refresh.ОБЯЗАТЕЛЬНЫЕ))
        self.assertEqual(set(policy['required_index_datasets']),
                         set(opendata_index.ИНДЕКСИРУЕМЫЕ) & set(opendata_refresh.ОБЯЗАТЕЛЬНЫЕ))
        for item in self.cp.inventory():
            with self.subTest(dataset=item['dataset_id']), tempfile.TemporaryDirectory() as directory:
                path = Path(directory)
                metadata = {'папка': 'generation', 'файл': 'black-list.fixture.json'}
                files = [path / name for name in item['required_files']]
                if item['dataset_id'] == 'erknm':
                    files += [path / 'generation' / name for name in
                              ['словарь.json.gz'] + ['%02x.json.gz' % i for i in range(256)]]
                elif item['dataset_id'] == 'cbr_warning':
                    files.append(path / metadata['файл'])
                elif item['dataset_id'] == 'opendata_refresh':
                    for dataset in item['required_archive_datasets']:
                        archive = path / dataset / 'data-20261001-structure-20200101.zip'
                        files += [archive, Path(str(archive) + '.meta.json')]
                    for dataset in item['required_index_datasets']:
                        index = path / dataset / ('index-%s.bin' % dataset)
                        files += [index, Path(str(index) + '.meta.json')]
                for file in files:
                    file.parent.mkdir(parents=True, exist_ok=True)
                    file.touch()
                # Readiness inspects files only; semantic validation belongs to update.
                with patch.object(Path, 'read_bytes', side_effect=AssertionError('body parsed')):
                    self.assertEqual(self.cp._required_cache_files(item, path, metadata), len(files))
                    files[-1].unlink()
                    with self.assertRaises(FileNotFoundError):
                        self.cp._required_cache_files(item, path, metadata)


if __name__ == '__main__':
    unittest.main()
