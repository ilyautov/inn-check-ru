#!/usr/bin/env python3
"""Offline regressions for captured export permissions and DOCX page bounds."""
import copy
import json
import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import dossier
import evidence_contract
import evidence_pack
import retro_verdict
import snapshot

W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'


def marker(exports=()):
    return {'schema_version': 1, 'policy': {
        'schema_version': 1, 'total_cost_units': 1, 'providers': [{
            'id': 'checko', 'enabled': True,
            'allowed_fields': ['недостоверность_сведений', 'дисквалификация_руководителя'],
            'export_fields': list(exports), 'request_limit': 1,
            'request_cost_units': 1, 'fallback_on': []}]}, 'results': []}


def fetch():
    return {'инн': '7707083893', 'агрегатор': {'агрегатор': 'checko',
            'недостоверность_сведений': True, 'дисквалификация_руководителя': False},
            '_доступность': {'агрегатор': {'состояние': 'ok'}},
            '_provider_policy': marker()}


def pack(value, path):
    evidence_pack.записать_пакет(path, '7707083893', value, [])
    return json.loads((path / 'fetch.json').read_text(encoding='utf-8'))


class CapturedPolicyIntegrity(unittest.TestCase):
    def test_materialized_snapshot_export_matches_direct(self):
        source = evidence_contract.evaluate(fetch(), profile='нейтрально',
            now='2026-10-16T09:00:00Z')['fetch']
        before = copy.deepcopy(source)
        fp = snapshot.fingerprint(source, дата='2026-10-16T09:00:00Z')
        restored, _ = retro_verdict.восстановить(fp)
        with tempfile.TemporaryDirectory() as td:
            direct = pack(source, Path(td) / 'direct')
            replayed = pack(restored, Path(td) / 'replayed')
        self.assertEqual(replayed, direct)
        self.assertEqual(replayed['data'], {})
        self.assertEqual(fp['_provider_policy'], source['_provider_policy'])
        self.assertEqual(fp['вердикт']['_provider_policy'], source['_provider_policy'])
        self.assertEqual(restored['_provider_policy'], source['_provider_policy'])
        self.assertEqual(source, before)
        self.assertIn('_evidence', fp)
        self.assertIn('_evidence', restored)

    def test_selective_export_survives_roundtrip_and_unrelated_environment(self):
        source = fetch()
        source['_provider_policy'] = marker(['недостоверность_сведений'])
        with tempfile.TemporaryDirectory() as td:
            current = Path(td) / 'current-policy.json'
            current.write_text(json.dumps(marker(['недостоверность_сведений',
                'дисквалификация_руководителя'])['policy']), encoding='utf-8')
            with patch.dict(os.environ, {'INN_CHECK_PROVIDER_POLICY': str(current)}):
                restored, _ = retro_verdict.восстановить(snapshot.fingerprint(source))
                exported = pack(restored, Path(td) / 'exported')
                direct = pack(source, Path(td) / 'direct')
        self.assertEqual(exported, direct)
        self.assertEqual(exported['data'], {'недостоверность_сведений': True})
        self.assertEqual(dossier.собрать(restored, {}), dossier.собрать(source, {}))

    def test_present_malformed_values_are_retained_and_exports_refuse(self):
        for malformed in (None, False, [], '', {}, {'schema_version': 1},
                          {'schema_version': True, 'policy': marker()['policy']}):
            with self.subTest(marker=malformed):
                source = fetch()
                source['_provider_policy'] = malformed
                fp = snapshot.fingerprint(source)
                self.assertIn('_provider_policy', fp)
                self.assertEqual(fp['_provider_policy'], malformed)
                self.assertIn('_provider_policy', fp['вердикт'])
                restored, _ = retro_verdict.восстановить(fp)
                self.assertIn('_provider_policy', restored)
                self.assertEqual(restored['_provider_policy'], malformed)
                with tempfile.TemporaryDirectory() as td:
                    path = Path(td) / 'must-not-exist'
                    with self.assertRaisesRegex(ValueError, '^invalid_captured_provider_policy$'):
                        pack(restored, path)
                    self.assertFalse(path.exists())
                with self.assertRaisesRegex(ValueError, '^invalid_captured_provider_policy$'):
                    dossier.собрать(restored, {})

    def test_passthrough_versions_promote_verdict_marker_including_null(self):
        for version in (1, 2):
            for saved in (marker(), None):
                with self.subTest(version=version, marker=saved):
                    fp = snapshot.fingerprint(fetch())
                    fp['версия_формата'] = version
                    fp.pop('_provider_policy', None)
                    fp['вердикт']['_provider_policy'] = saved
                    normalized = snapshot.fingerprint(fp)
                    self.assertIn('_provider_policy', normalized)
                    self.assertEqual(normalized['_provider_policy'], saved)
                    restored, _ = retro_verdict.восстановить(fp)
                    self.assertIn('_provider_policy', restored)
                    self.assertEqual(restored['_provider_policy'], saved)
                    with tempfile.TemporaryDirectory() as td:
                        if saved is None:
                            with self.assertRaises(ValueError):
                                pack(restored, Path(td) / 'export')
                        else:
                            self.assertEqual(pack(restored, Path(td) / 'export')['data'], {})

    def test_top_level_only_marker_reaches_restore(self):
        fp = snapshot.fingerprint(fetch())
        fp['вердикт'].pop('_provider_policy', None)
        fp['_provider_policy'] = None
        restored, _ = retro_verdict.восстановить(fp)
        self.assertIn('_provider_policy', restored)
        self.assertIsNone(restored['_provider_policy'])

    def test_conflicting_representations_fail_closed(self):
        alternatives = [None, marker(['недостоверность_сведений']),
                        dict(marker(), schema_version=True)]
        for saved in alternatives:
            fp = snapshot.fingerprint(fetch())
            fp['_provider_policy'] = marker()
            fp['вердикт']['_provider_policy'] = saved
            for adapter in (snapshot.fingerprint, retro_verdict.восстановить):
                with self.subTest(marker=saved, adapter=adapter.__name__), \
                        self.assertRaisesRegex(ValueError, '^invalid_captured_provider_policy$'):
                    adapter(fp)

    def test_old_markerless_snapshots_stay_markerless(self):
        source = fetch()
        source.pop('_provider_policy')
        for version in (1, 2):
            fp = snapshot.fingerprint(source)
            fp['версия_формата'] = version
            restored, _ = retro_verdict.восстановить(snapshot.fingerprint(fp))
            self.assertNotIn('_provider_policy', restored)
            with tempfile.TemporaryDirectory() as td:
                self.assertIn('агрегатор', pack(restored, Path(td) / 'export'))


class DocxLayoutIntegrity(unittest.TestCase):
    def test_heading_stays_with_following_content(self):
        xml = ET.fromstring(dossier.в_docx_xml([
            dossier.заголовок('Evidence records', 2), dossier.абзац('Первое значение')]))
        self.assertIsNotNone(xml.find('.//' + W + 'p/' + W + 'pPr/' + W + 'keepNext'))

    def test_status_colors_have_distinct_docx_only_text_labels(self):
        blocks = [dossier.абзац('Статусы: 🔴 🟡 🟢')]
        before = copy.deepcopy(blocks)
        xml = ET.fromstring(dossier.в_docx_xml(blocks))
        text = ''.join(n.text or '' for n in xml.iter(W + 't'))
        self.assertEqual(text, 'Статусы: 🔴 (красный) 🟡 (жёлтый) 🟢 (зелёный)')
        self.assertEqual(blocks, before)
        self.assertIn('Статусы: 🔴 🟡 🟢', dossier.в_markdown(blocks))

    def test_table_grid_and_cells_fit_printable_page(self):
        blocks = [dossier.таблица(['Источник', 'Tier', 'Deal-killer', 'Состояние',
                    'Дата', 'Первоисточник'], [['реестр', 'green', 'да',
                    'ok источник ответил, данные разобраны', '2026-10-16',
                    'https://example.invalid/' + 'x' * 160]])]
        xml = ET.fromstring(dossier.в_docx_xml(blocks))
        section = xml.find('.//' + W + 'sectPr')
        page, margins = section.find(W + 'pgSz'), section.find(W + 'pgMar')
        printable = int(page.get(W + 'w')) - int(margins.get(W + 'left')) - int(margins.get(W + 'right'))
        table = xml.find('.//' + W + 'tbl')
        props = table.find(W + 'tblPr')
        self.assertEqual(props.find(W + 'tblLayout').get(W + 'type'), 'fixed')
        table_width = props.find(W + 'tblW')
        self.assertEqual(table_width.get(W + 'type'), 'dxa')
        widths = [int(c.get(W + 'w')) for c in table.findall(W + 'tblGrid/' + W + 'gridCol')]
        self.assertEqual(len(widths), 6)
        self.assertEqual(sum(widths), int(table_width.get(W + 'w')))
        self.assertLessEqual(sum(widths), printable)
        for row in table.findall(W + 'tr'):
            cells = row.findall(W + 'tc')
            self.assertEqual([int(c.find(W + 'tcPr/' + W + 'tcW').get(W + 'w')) for c in cells], widths)
        self.assertIsNotNone(table.find(W + 'tr/' + W + 'trPr/' + W + 'tblHeader'))
        self.assertFalse(xml.findall('.//' + W + 'trHeight'))
        for paragraph in table.findall('.//' + W + 'p'):
            self.assertEqual(paragraph.find(W + 'pPr/' + W + 'wordWrap').get(W + 'val'), '0')

    def test_long_evidence_records_are_stacked_and_preserve_every_value_in_order(self):
        headers = ['Claim ID', 'Поле', 'Знание', 'Период / scope / raw / зависимости']
        rows = [['sha256:' + str(n) * 64, 'финансы.отчётность.2025.строки.1300',
                 'known:present', 'Период: 2025; область поиска: scope_%d; raw: %s; '
                 'зависимости: dependency_%d; полнота: complete; наблюдалось: 2026-10-16T09:00:00Z'
                 % (n, 'a' * 180, n)] for n in (1, 2)]
        xml = ET.fromstring(dossier.в_docx_xml([dossier.таблица(headers, rows)]))
        self.assertFalse(xml.findall('.//' + W + 'tbl'), 'wide evidence must flow as readable records')
        paragraphs = [''.join(p.itertext()) for p in xml.findall('.//' + W + 'p')]
        expected = ['%s: %s' % (label, value) for row in rows for label, value in zip(headers, row)]
        self.assertEqual([p for p in paragraphs if p], expected)
        sizes = [int(n.get(W + 'val')) for n in xml.findall('.//' + W + 'sz')]
        self.assertTrue(sizes)
        self.assertGreaterEqual(min(sizes), 20)
        self.assertFalse(xml.findall('.//' + W + 'trHeight'))


if __name__ == '__main__':
    unittest.main()
