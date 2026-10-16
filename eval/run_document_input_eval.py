#!/usr/bin/env python3
"""Synthetic document extraction contract and containment checks; never network."""
import base64
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
try:
    import document_input as doc
except ImportError:
    doc = None
FIXTURES = ROOT / 'eval/fixtures/document-input'


def pdf(text=None, pages=1):
    """Original synthetic minimal PDF, standard Helvetica, no external assets."""
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>', b'']
    kids = []
    for _ in range(pages):
        page_id = len(objects) + 1
        kids.append('%d 0 R' % page_id)
        body = (b'BT /F1 12 Tf 50 700 Td (' + text.encode('ascii') + b') Tj ET') if text else b'0 0 1 1 re f'
        objects.append(('<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >> /Contents %d 0 R >>' % (page_id + 1)).encode())
        objects.append(b'<< /Length ' + str(len(body)).encode() + b' >>\nstream\n' + body + b'\nendstream')
    objects[1] = ('<< /Type /Pages /Count %d /Kids [%s] >>' % (pages, ' '.join(kids))).encode()
    result = b'%PDF-1.4\n'
    offsets = []
    for index, body in enumerate(objects, 1):
        offsets.append(len(result))
        result += ('%d 0 obj\n' % index).encode() + body + b'\nendobj\n'
    xref = len(result)
    result += ('xref\n0 %d\n0000000000 65535 f \n' % (len(objects) + 1)).encode()
    result += b''.join(('%010d 00000 n \n' % item).encode() for item in offsets)
    result += ('trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n' % (len(objects) + 1, xref)).encode()
    return result


class DocumentTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(doc, 'document_input implementation is required')

    def test_text_schema_offsets_roles_and_no_checksum_repair(self):
        text = 'Supplier INN 7707083893 KPP 773601001 OGRN 1027700132195 Address: Synthetic street\nBank INN 7702070139\nBad INN 7707083894'
        result = doc.extract(text.encode(), filename='untrusted.txt')
        self.assertEqual(result['schema_version'], 1)
        self.assertEqual([c['inn'] for c in result['candidates']], ['7707083893', '7702070139'])
        first = result['candidates'][0]
        self.assertEqual(first['kpp'], '773601001')
        self.assertEqual(first['ogrn'], '1027700132195')
        self.assertIn('supplier', first['role_hints'])
        self.assertEqual(text[first['offsets']['start']:first['offsets']['end']], first['inn'])
        self.assertTrue(any(x['reason_code'] == 'rejected_identifiers' for x in result['limitations']))
        self.assertNotIn('_texts', result)
        self.assertEqual(result, doc.extract(text.encode(), filename='../../evil;$(touch bad).txt'))

    def test_rejected_same_inn_cannot_steal_accepted_offsets_or_attributes(self):
        import extract_inn as legacy
        for invalid in ('SKU7707083893', '7707083893AB', 'SKU7707083893AB'):
            with self.subTest(invalid=invalid):
                text = ('Банк Bank ' + invalid + ' KPP 111111111 OGRN 1111111111111 Address: Wrong\n'
                        'Supplier INN 7707083893 KPP 773601001 OGRN 1027700132195 Address: Right')
                start = text.index('7707083893', text.index('Supplier'))
                accepted, _, _ = legacy.разобрать_текст(text, 'synthetic')
                self.assertEqual(len(accepted), 1)
                result = doc.extract(text.encode(), filename='synthetic.txt')
                self.assertEqual(result['status'], 'ok')
                self.assertEqual(len(result['candidates']), 1)
                candidate = result['candidates'][0]
                self.assertEqual(candidate['offsets'], {'start': start, 'end': start + 10})
                self.assertEqual(candidate['kpp'], '773601001')
                self.assertEqual(candidate['ogrn'], '1027700132195')
                self.assertEqual(candidate['address'], 'Right')
                self.assertEqual(candidate['role_hints'], ['supplier'])
                self.assertEqual(accepted[0]['offsets'], candidate['offsets'])

    def test_repeated_same_inn_preserves_distinct_line_occurrences(self):
        text = 'Supplier INN 7707083893 KPP 773601001\nBank INN 7707083893 KPP 111111111'
        candidates = doc.extract(text.encode(), filename='synthetic.txt')['candidates']
        self.assertEqual(len(candidates), 2)
        self.assertEqual(len({c['candidate_id'] for c in candidates}), 2)
        self.assertEqual([c['offsets']['start'] for c in candidates],
                         [text.index('7707083893'), text.rindex('7707083893')])
        self.assertEqual([c['kpp'] for c in candidates], ['773601001', '111111111'])
        self.assertEqual([c['role_hints'] for c in candidates], [['supplier'], ['bank']])
        self.assertIsNone(doc.select(candidates, role='supplier')['selected'])

    @unittest.skipUnless(importlib.util.find_spec('pypdf'), 'optional pypdf unavailable')
    def test_repeated_same_inn_preserves_distinct_pages(self):
        candidates = doc.extract(pdf('Supplier INN 7707083893', pages=2), filename='synthetic.pdf')['candidates']
        self.assertEqual(len(candidates), 2)
        self.assertEqual(len({c['candidate_id'] for c in candidates}), 2)
        self.assertEqual([c['page'] for c in candidates], [1, 2])
        self.assertEqual(candidates[0]['offsets'], candidates[1]['offsets'])
        self.assertIsNone(doc.select(candidates, role='supplier')['selected'])

    def test_approved_roles_require_explicit_selection(self):
        candidates = [{'candidate_id': 'subject', 'inn': '7707083893',
                       'role_hints': ['supplier', 'signatory', 'unknown']}]
        for role in ('supplier', 'buyer', 'own_company', 'bank', 'signatory', 'unknown', 'other'):
            with self.subTest(role=role):
                ambiguous = doc.select(candidates, role=role)
                self.assertEqual(ambiguous['status'], 'needs_input')
                self.assertEqual(ambiguous['reason_code'], 'explicit_selection_required')
                self.assertIsNone(ambiguous['selected'])
                selected = doc.select(candidates, role=role, candidate_id='subject')
                self.assertEqual(selected['status'], 'ok')
                self.assertEqual(selected['selected']['inn'], '7707083893')
        self.assertEqual(doc.select(candidates, role='unsupported', candidate_id='subject')['status'], 'error')

    def test_legacy_default_role_is_unknown(self):
        import extract_inn as legacy
        self.assertEqual(legacy._разобрать_аргументы(['extract_inn.py', 'a.txt'])[0]['role'], 'unknown')

    def test_approved_roles_cli_and_legacy_dispatch_boundary(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp, 'a.txt'); source.write_text('Supplier INN 7707083893')
            candidates = doc.extract(source.read_bytes(), filename=source.name)['candidates']
            candidate_id = candidates[0]['candidate_id']
            payload = Path(temp, 'candidates.json'); payload.write_text(json.dumps(candidates))
            run_root = Path(temp, 'runs')
            marker = Path(temp, 'dispatched')
            stub = Path(temp, 'batch.py')
            stub.write_text('import pathlib,json,sys\npathlib.Path(' + repr(str(marker)) + ').touch()\nprint(json.dumps({"argv":sys.argv[1:]}))')
            env = {**os.environ, 'INN_CHECK_EXTRACT_BATCH': str(stub), 'INN_CHECK_CACHE': temp}
            for role in ('signatory', 'unknown'):
                with self.subTest(role=role):
                    command = [sys.executable, str(ROOT/'scripts/workflow_cli.py'), 'document', 'select',
                               str(payload), '--role', role, '--store', str(run_root), '--json']
                    for extra, expected in (([], 'needs_input'), (['--candidate', candidate_id], 'ok')):
                        process = subprocess.run(command + extra, capture_output=True, text=True, env=env, check=False)
                        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
                        envelope = json.loads(process.stdout)
                        self.assertEqual(envelope['status'], expected)
                        self.assertIsNone(envelope['run_id'])
                        self.assertEqual(envelope['data']['selected'] is None, expected == 'needs_input')
                        self.assertFalse(run_root.exists())
                        self.assertFalse(marker.exists())
                    command = [sys.executable, str(ROOT/'scripts/extract_inn.py'), str(source),
                               '--проверить', '--подтверждаю', '--role', role, '--json']
                    process = subprocess.run(command, capture_output=True, text=True, env=env, check=False)
                    self.assertEqual(json.loads(process.stdout)['проверка']['reason_code'], 'explicit_selection_required')
                    self.assertFalse(marker.exists())
                    process = subprocess.run(command + ['--candidate', candidate_id], capture_output=True, text=True, env=env, check=False)
                    self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
                    self.assertEqual(json.loads(process.stdout)['проверка']['инны'], ['7707083893'])
                    self.assertTrue(marker.exists())
                    marker.unlink()

    def test_explicit_party_selection(self):
        candidates = [{'candidate_id': 'supplier-1', 'inn': '7707083893', 'role_hints': ['supplier']},
                      {'candidate_id': 'bank-1', 'inn': '7702070139', 'role_hints': ['bank']}]
        self.assertEqual(doc.select(candidates, role='supplier')['status'], 'needs_input')
        self.assertIsNone(doc.select(candidates, role='supplier')['selected'])
        self.assertEqual(doc.select(candidates, role='supplier', candidate_id='supplier-1')['selected']['inn'], '7707083893')
        self.assertEqual(doc.select(candidates[:1], role='supplier')['status'], 'needs_input')
        self.assertEqual(doc.select(candidates, role='supplier', candidate_id='missing')['reason_code'], 'candidate_not_found')
        self.assertEqual(doc.select(candidates * 2, role='supplier', candidate_id='supplier-1')['status'], 'error')
        self.assertEqual(doc.select([{'candidate_id':'a', 'inn':'7707083894'}], role='supplier', candidate_id='a')['status'], 'error')
        self.assertEqual(doc.select([], role='supplier')['status'], 'needs_input')

    def test_core_html_docx_without_pdf_import(self):
        html = b'<html><p>INN 7707083893</p></html>'
        self.assertEqual(doc.extract(html, filename='file.html')['candidates'][0]['inn'], '7707083893')
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>INN 7707083893</w:t></w:r></w:p></w:body></w:document>')
        self.assertEqual(doc.extract(buffer.getvalue(), filename='file.docx')['candidates'][0]['inn'], '7707083893')

    def test_docx_xml_limit_is_structured_without_partial_candidates(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('word/document.xml', '<w:p>Supplier INN 7707083893</w:p>')
            archive.writestr('word/header1.xml', '<w:p>Bank INN 7702070139</w:p>')
        with patch.object(doc, 'MAX_TEXT_BYTES', 50):
            result = doc._extract_inside(buffer.getvalue(), '.docx', False)
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['method'], 'docx')
        self.assertEqual(result['candidates'], [])
        self.assertEqual(result['limitations'][0]['reason_code'], 'output_too_large')
        self.assertNotIn('_texts', result)

    def test_normal_docx_header_footnote_offsets_and_roles_survive(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr('word/document.xml', '<w:p>Contract</w:p>')
            archive.writestr('word/header1.xml', '<w:p>Supplier INN 7707083893</w:p>')
            archive.writestr('word/footnotes.xml', '<w:p>Bank INN 7702070139</w:p>')
        result = doc.extract(buffer.getvalue(), filename='synthetic.docx')
        self.assertEqual(result['method'], 'docx')
        self.assertEqual({c['inn'] for c in result['candidates']}, {'7707083893', '7702070139'})
        texts = doc._extract_inside(buffer.getvalue(), '.docx', False)['_texts']
        for candidate in result['candidates']:
            start, end = candidate['offsets']['start'], candidate['offsets']['end']
            self.assertEqual(texts[0][start:end], candidate['inn'])
            self.assertEqual(candidate['role_hints'],
                             ['supplier'] if candidate['inn'] == '7707083893' else ['bank'])

    def test_limits_and_input(self):
        self.assertEqual(doc.extract(b'x' * (20 * 1024 * 1024 + 1), filename='a.pdf')['limitations'][0]['reason_code'], 'too_large')
        self.assertEqual(doc.extract('bad', filename='a.txt')['status'], 'error')
        self.assertEqual(doc.extract(b'\x00', filename='a.bin')['limitations'][0]['reason_code'], 'unsupported_format')

    def test_missing_pdf_extra_is_structured(self):
        with tempfile.TemporaryDirectory() as temp:
            Path(temp, 'pypdf.py').write_text('raise ImportError("synthetic missing extra")\n')
            with patch.dict(os.environ, {'PYTHONPATH': temp}):
                result = doc.extract(pdf('INN 7707083893'), filename='a.pdf')
        self.assertEqual(result['limitations'][0]['reason_code'], 'missing_pdf_extra')

    def test_whole_deadline_includes_parser(self):
        with tempfile.TemporaryDirectory() as temp:
            Path(temp, 'pypdf.py').write_text('import time\ntime.sleep(30)\n')
            with patch.dict(os.environ, {'PYTHONPATH': temp}), patch.object(doc, 'TIMEOUT_SECONDS', 0.3):
                started = time.monotonic()
                result = doc.extract(pdf(), filename='a.pdf')
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(result['limitations'][0]['reason_code'], 'timeout')

    def test_direct_timeout_removes_document_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary).resolve()
            ready = folder / 'workspace'
            (folder / 'pypdf.py').write_text('import os,time\nfrom pathlib import Path\nPath(%r).write_text(os.getcwd())\ntime.sleep(30)\n' % str(ready))
            with patch.dict(os.environ, {'PYTHONPATH': str(folder)}), patch.object(doc, 'TIMEOUT_SECONDS', .3):
                result = doc.extract(pdf(), filename='input.pdf')
            self.assertEqual(result['limitations'][0]['reason_code'], 'timeout')
            self.assertTrue(ready.exists(), 'parser must actually start')
            self.assertFalse(Path(ready.read_text()).exists(), 'document input.bin remains after timeout')

    @unittest.skipIf(os.name == 'nt', 'native POSIX SIGINT; Windows algorithm tests cover owned cleanup')
    def test_direct_keyboard_interrupt_removes_document_bytes(self):
        import signal
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary).resolve()
            ready = folder / 'workspace'
            (folder / 'pypdf.py').write_text('import os,time\nfrom pathlib import Path\nPath(%r).write_text(os.getcwd())\ntime.sleep(30)\n' % str(ready))
            command = [sys.executable, '-B', '-c',
                       'import sys; sys.path.insert(0, %r); import document_input; document_input.extract(%r, filename="input.pdf")' % (str(ROOT / 'scripts'), pdf())]
            process = subprocess.Popen(command, env=dict(os.environ, PYTHONPATH=str(folder)),
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(ready.exists(), 'parser must actually start')
                process.send_signal(signal.SIGINT)
                process.communicate(timeout=3)
                self.assertNotEqual(process.returncode, 0)
                self.assertFalse(Path(ready.read_text()).exists(), 'document input.bin remains after interrupt')
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=3)

    def test_fixed_stdin_worker_contract(self):
        command = [sys.executable, str(ROOT/'scripts/document_input.py'), '--stdin-worker']
        request = {'schema_version': 1, 'data_base64': base64.b64encode(b'INN 7707083893').decode(), 'filename': '../../hostile.txt', 'ocr': False}
        process = subprocess.run(command, input=json.dumps(request), text=True, capture_output=True, check=False)
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        self.assertEqual(result['candidates'][0]['inn'], '7707083893')
        self.assertNotIn('_texts', result)
        for invalid in ({**request, 'function': 'os.system'}, {**request, 'schema_version': 2}, {**request, 'ocr': 'true'}, {**request, 'data_base64': '!!!'}):
            process = subprocess.run(command, input=json.dumps(invalid), text=True, capture_output=True, check=False)
            self.assertEqual(json.loads(process.stdout)['status'], 'error')

    def test_synthetic_manifest_and_recall(self):
        manifest = json.loads((FIXTURES/'manifest.json').read_text())
        self.assertEqual(manifest['real_consented_documents'], 0)
        for item in manifest['files']:
            self.assertTrue(item['synthetic'])
            raw = (FIXTURES/item['file']).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), item['sha256'])
            result = doc.extract(raw, filename=item['file'])
            if item['file'].endswith('.pdf') and not importlib.util.find_spec('pypdf'):
                self.assertEqual(result['limitations'][0]['reason_code'], 'missing_pdf_extra')
            else:
                self.assertEqual([c['inn'] for c in result['candidates']], item['expected_inns_without_ocr'], item['file'])
                if item['expected_failure']:
                    self.assertEqual(result['status'], 'error')
            self.assertIsNone(doc.select(result['candidates'], role='supplier')['selected'])

    def test_no_silent_party_check_across_synthetic_corpus(self):
        manifest = json.loads((FIXTURES/'manifest.json').read_text())
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp, 'called')
            stub = Path(temp, 'batch.py')
            stub.write_text('from pathlib import Path\nPath(' + repr(str(marker)) + ').touch()\nprint("unexpected batch")')
            for item in manifest['files']:
                p = subprocess.run([sys.executable, str(ROOT/'scripts/extract_inn.py'), str(FIXTURES/item['file']), '--проверить', '--подтверждаю', '--json'], env={**os.environ, 'INN_CHECK_CACHE': temp, 'INN_CHECK_EXTRACT_BATCH': str(stub)}, capture_output=True, text=True, check=False)
                self.assertFalse(json.loads(p.stdout)['проверка']['запущен'], item['file'])
                self.assertFalse(marker.exists(), item['file'])

    @unittest.skipUnless(os.environ.get('INN_CHECK_DOCUMENT_LIVE_OCR') == '1', 'explicit local OCR smoke not requested')
    @unittest.skipUnless(importlib.util.find_spec('pypdf'), 'optional pypdf unavailable')
    def test_real_local_ocr_synthetic_scan(self):
        result = doc.extract((FIXTURES/'scan.pdf').read_bytes(), filename='scan.pdf', ocr=True)
        self.assertEqual(result['method'], 'pdf_ocr')
        self.assertEqual([item['inn'] for item in result['candidates']], ['7707083893'])
        self.assertEqual(result['status'], 'partial')

    @unittest.skipIf(os.name == 'nt', 'POSIX executable fixture')
    @unittest.skipUnless(importlib.util.find_spec('pypdf'), 'optional pypdf unavailable')
    def test_ocr_argv_and_stale_output_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            renderer = Path(temp, 'render')
            renderer.write_text('#!' + sys.executable + '\nimport pathlib,sys\nassert sys.argv[-2:]==["input.bin","page"]\npathlib.Path("page.png").write_bytes(b"synthetic")\n')
            renderer.chmod(0o700)
            ocr = Path(temp, 'ocr')
            ocr.write_text('#!' + sys.executable + '\nimport pathlib,sys\nassert sys.argv[1:]==["page.png","recognized","-l","rus+eng","--psm","3"]\nif not pathlib.Path("called").exists():\n pathlib.Path("recognized.txt").write_text("Supplier INN 7707083893\\nBad INN 7707083894")\n pathlib.Path("called").touch()\n')
            ocr.chmod(0o700)
            with patch.dict(os.environ, {'INN_CHECK_PDF_RENDERER': str(renderer), 'INN_CHECK_TESSERACT': str(ocr)}):
                result = doc.extract(pdf(), filename='../../hostile;$(touch bad).pdf', ocr=True)
                self.assertEqual(result['method'], 'pdf_ocr')
                self.assertEqual([x['inn'] for x in result['candidates']], ['7707083893'])
                result = doc.extract(pdf(pages=2), filename='a.pdf', ocr=True)
            self.assertEqual(result['status'], 'error', 'stale OCR output reused for second page')
            self.assertEqual(result['limitations'][0]['reason_code'], 'ocr_failed')

    def test_cli_extract_and_select(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp, 'a.txt'); source.write_text('Supplier INN 7707083893\nBank INN 7702070139')
            p = subprocess.run([sys.executable, str(ROOT/'scripts/workflow_cli.py'), 'document', 'extract', str(source), '--json'], capture_output=True, text=True, check=False)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            envelope = json.loads(p.stdout)
            self.assertEqual(envelope['status'], 'ok')
            self.assertIsNone(envelope['run_id'])
            source.write_text(p.stdout)
            command = [sys.executable, str(ROOT/'scripts/workflow_cli.py'), 'document', 'select', str(source), '--role', 'supplier', '--json']
            p = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertEqual(json.loads(p.stdout)['status'], 'needs_input')
            p = subprocess.run(command + ['--candidate', envelope['data']['candidates'][0]['candidate_id']], capture_output=True, text=True, check=False)
            self.assertEqual(json.loads(p.stdout)['data']['selected']['inn'], '7707083893')

    def test_legacy_multi_party_check_never_dispatches(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp, 'a.txt'); source.write_text('Supplier INN 7707083893\nBank INN 7702070139')
            sentinel = Path(temp, 'dispatch.py'); sentinel.write_text('raise SystemExit("NETWORK DISPATCH MUST NOT HAPPEN")')
            p = subprocess.run([sys.executable, str(ROOT/'scripts/extract_inn.py'), str(source), '--проверить', '--json'], env={**os.environ, 'INN_CHECK_EXTRACT_BATCH': str(sentinel), 'INN_CHECK_CACHE': temp}, capture_output=True, text=True, check=False)
            self.assertFalse(json.loads(p.stdout)['проверка']['запущен'])
            self.assertEqual(json.loads(p.stdout)['проверка']['reason_code'], 'explicit_selection_required')

    def test_legacy_explicit_candidate_checks_only_selected_inn(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp, 'a.txt'); source.write_text('Supplier INN 7707083893\nBank INN 7702070139')
            selected = doc.extract(source.read_bytes(), filename=source.name)['candidates'][0]['candidate_id']
            stub = Path(temp, 'batch.py'); stub.write_text('import json,sys\nprint(json.dumps({"argv":sys.argv[1:]}))')
            p = subprocess.run([sys.executable, str(ROOT/'scripts/extract_inn.py'), str(source), '--проверить', '--json', '--candidate', selected, '--role', 'supplier'], env={**os.environ, 'INN_CHECK_EXTRACT_BATCH': str(stub), 'INN_CHECK_CACHE': temp}, capture_output=True, text=True, check=False)
            self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
            checked = json.loads(p.stdout)['проверка']
            self.assertEqual(checked['инны'], ['7707083893'])
            self.assertNotIn('7702070139', checked['результат']['argv'])

    @unittest.skipUnless(importlib.util.find_spec('pypdf'), 'optional pypdf unavailable')
    def test_pdf_text_and_failures(self):
        result = doc.extract(pdf('Supplier INN 7707083893'), filename='a.pdf')
        self.assertEqual(result['method'], 'pdf_text')
        self.assertEqual(result['pages'], 1)
        self.assertEqual(result['candidates'][0]['page'], 1)
        self.assertEqual(result['candidates'][0]['inn'], '7707083893')
        self.assertEqual(doc.extract(pdf(), filename='a.pdf')['limitations'][0]['reason_code'], 'image_only')
        self.assertEqual(doc.extract(b'%PDF garbage', filename='a.pdf')['limitations'][0]['reason_code'], 'broken_pdf')
        self.assertEqual(doc.extract(pdf(pages=101), filename='a.pdf')['limitations'][0]['reason_code'], 'too_many_pages')
        from pypdf import PdfWriter
        writer = PdfWriter(); writer.add_blank_page(width=100, height=100); writer.encrypt('secret')
        stream = io.BytesIO(); writer.write(stream)
        self.assertEqual(doc.extract(stream.getvalue(), filename='a.pdf')['limitations'][0]['reason_code'], 'encrypted_pdf')

    @unittest.skipUnless(importlib.util.find_spec('pypdf'), 'optional pypdf unavailable')
    def test_missing_ocr_dependencies(self):
        with patch.dict(os.environ, {'INN_CHECK_PDF_RENDERER': '', 'INN_CHECK_TESSERACT': ''}):
            result = doc.extract(pdf(), filename='a.pdf', ocr=True)
        self.assertEqual(result['limitations'][0]['reason_code'], 'missing_renderer')
        with patch.dict(os.environ, {'INN_CHECK_PDF_RENDERER': sys.executable, 'INN_CHECK_TESSERACT': ''}):
            result = doc.extract(pdf(), filename='a.pdf', ocr=True)
        self.assertEqual(result['limitations'][0]['reason_code'], 'missing_tesseract')

    @unittest.skipIf(os.name == 'nt', 'POSIX executable fixture; Windows containment has separate process eval')
    @unittest.skipUnless(importlib.util.find_spec('pypdf'), 'optional pypdf unavailable')
    def test_deadline_kills_ocr_descendant(self):
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp, 'escaped')
            renderer = Path(temp, 'render')
            renderer.write_text('#!' + sys.executable + '\nimport subprocess,sys,time\nsubprocess.Popen([sys.executable,"-c",' + repr('import time,pathlib; time.sleep(1.5); pathlib.Path(' + repr(str(marker)) + ').write_text("bad")') + '])\ntime.sleep(30)\n')
            renderer.chmod(0o700)
            with patch.dict(os.environ, {'INN_CHECK_PDF_RENDERER': str(renderer), 'INN_CHECK_TESSERACT': sys.executable}), patch.object(doc, 'TIMEOUT_SECONDS', 0.5):
                started = time.monotonic()
                result = doc.extract(pdf(), filename='a.pdf', ocr=True)
            self.assertLess(time.monotonic() - started, 3)
            self.assertEqual(result['limitations'][0]['reason_code'], 'timeout')
            time.sleep(1.6)
            self.assertFalse(marker.exists(), 'OCR descendant survived timeout')


if __name__ == '__main__':
    unittest.main(verbosity=2)
