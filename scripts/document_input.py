"""Offline, bounded document extraction. Candidate hints never authorize a check.

PDF parsing and explicitly enabled local OCR run in one owned process tree.
The parent enforces the whole-operation deadline, including parser startup.
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

MAX_BYTES = 20 * 1024 * 1024
MAX_PAGES = 100
TIMEOUT_SECONDS = 60.0
MAX_TEXT_BYTES = 20 * 1024 * 1024
MAX_RESULT_BYTES = 24 * 1024 * 1024
MAX_CANDIDATES = 10000
# `other` remains a compatibility alias for the explicitly selected unknown role.
ROLES = ('supplier', 'buyer', 'own_company', 'bank', 'signatory', 'unknown', 'other')
MAX_REQUEST_BYTES = ((MAX_BYTES + 2) // 3) * 4 + 8192
_MESSAGES = {
    'invalid_input': 'Неверные данные документа.',
    'too_large': 'Документ превышает 20 МиБ.',
    'too_many_pages': 'В PDF больше 100 страниц.',
    'missing_pdf_extra': 'Для PDF установите inn-check-ru[pdf]; текстовые форматы работают без extra.',
    'encrypted_pdf': 'PDF зашифрован; передайте незашифрованную копию.',
    'broken_pdf': 'PDF повреждён или не может быть разобран.',
    'image_only': 'На странице нет извлекаемого текста; для скана явно включите локальный OCR.',
    'missing_renderer': 'Для OCR задайте INN_CHECK_PDF_RENDERER: полный путь к локальному pdftoppm.',
    'missing_tesseract': 'Для OCR задайте INN_CHECK_TESSERACT: полный путь к локальному Tesseract.',
    'ocr_failed': 'Локальное распознавание не завершилось; проверьте renderer, Tesseract и языковые данные.',
    'ocr_unverified': 'OCR может ошибаться; сверяйте выбранные реквизиты с оригиналом.',
    'timeout': 'Разбор документа превысил общий лимит времени 60 секунд.',
    'execution_error': 'Разбор документа не завершился.',
    'cleanup_unconfirmed': 'Завершение всего дерева процессов не подтверждено.',
    'unsupported_format': 'Формат не разобран; передайте текст, HTML, DOCX или PDF.',
    'output_too_large': 'Извлечённые данные превышают допустимый размер.',
    'rejected_identifiers': 'Некоторые числа отклонены существующей проверкой ИНН; исправления цифр не выполнялись.',
    'document_note': 'Исходный адаптер сообщил об ограничениях чтения.',
}


def _limitation(code, **details):
    return {'reason_code': code, 'message': _MESSAGES[code], **details}


def _result(status='ok', *, pages=0, method='none', candidates=None, limitations=None):
    return {'schema_version': 1, 'status': status, 'candidates': candidates or [],
            'pages': pages, 'method': method, 'limitations': limitations or []}


def _failure(code, *, pages=0, method='none'):
    return _result('error', pages=pages, method=method, limitations=[_limitation(code)])


def _process_helpers():
    # Canonical sibling module is shipped in both the wheel and standalone ZIP.
    # The stateless loader bootstrap also supports absolute-file imports.
    try:
        from sibling_imports import load_sibling
    except ModuleNotFoundError as error:
        if error.name != 'sibling_imports':
            raise
        path = Path(__file__).resolve().with_name('sibling_imports.py')
        spec = importlib.util.spec_from_file_location('sibling_imports', path)
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        load_sibling = sys.modules.setdefault('sibling_imports', helper).load_sibling
    return load_sibling('process_runtime')


def _run(data, *, filename, ocr=False):
    started = time.monotonic()
    if not isinstance(data, bytes) or not isinstance(filename, str) or type(ocr) is not bool:
        return _failure('invalid_input')
    if len(data) > MAX_BYTES:
        return _failure('too_large')
    # Filename is only a format hint. Never use it in paths or executable argv.
    suffix = _suffix(filename)
    proc = None
    cleanup = None
    try:
        helpers = _process_helpers()
        with tempfile.TemporaryDirectory(prefix='inn-document-') as directory:
            temp = Path(directory)
            (temp / 'input.bin').write_bytes(data)
            (temp / 'options.json').write_text(json.dumps({'suffix': suffix, 'ocr': ocr}), encoding='utf-8')
            proc = helpers._start_process([sys.executable, str(Path(__file__).resolve()), '--worker'],
                                          cwd=directory, stdin=subprocess.DEVNULL,
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                proc.wait(timeout=max(0, TIMEOUT_SECONDS - (time.monotonic() - started)))
                if proc.returncode:
                    result = _failure('execution_error')
                else:
                    with (temp / 'result.json').open('rb') as stream:
                        raw = stream.read(MAX_RESULT_BYTES + 1)
                    result = _failure('output_too_large') if len(raw) > MAX_RESULT_BYTES else json.loads(raw)
            except subprocess.TimeoutExpired:
                result = _failure('timeout')
            finally:
                # Also reap descendants left behind by an otherwise successful tool.
                cleanup = helpers._stop_process_tree(proc)
            if cleanup:
                result['status'] = 'error'
                result['limitations'].append(_limitation('cleanup_unconfirmed'))
            return result
    except KeyboardInterrupt:
        if proc is not None:
            helpers._stop_process_tree(proc)
        raise
    except Exception:
        return _failure('execution_error')


def extract(data: bytes, *, filename: str, ocr=False) -> dict:
    """Extract validated candidate occurrences; never performs network checks.

    Offsets are zero-based Unicode character offsets in normalized page text,
    with an exclusive end; they are not byte offsets into a PDF/DOCX file.
    """
    result = _run(data, filename=filename, ocr=ocr)
    result.pop('_texts', None)
    return result


def _suffix(filename):
    suffix = Path(filename).suffix.lower()
    return suffix if suffix in ('.pdf', '.docx', '.html', '.htm', '.xhtml', '.eml', '.mht', '.mhtml', '.msg', '.json', '.csv', '.tsv') else '.txt'


def _legacy_pdf(data, filename):
    """Internal compatibility bridge; raw text is not part of public API."""
    result = _run(data, filename=filename)
    texts = result.pop('_texts', [])
    return {'формат': 'pdf', 'текст': '\n'.join(texts) if texts else None,
            'причина': '; '.join(item['message'] for item in result['limitations']) or None,
            'document': result}


def _candidate_pages(texts):
    import extract_inn as legacy
    candidates, limitations = [], []
    total = 0
    for page, text in enumerate(texts, 1):
        total += len(text.encode('utf-8'))
        if total > MAX_TEXT_BYTES:
            raise ValueError('output_too_large')
        accepted, _, rejected = legacy.разобрать_текст(text, 'document')
        if rejected:
            limitations.append(_limitation('rejected_identifiers', page=page, count=sum(rejected.values())))
        # Use the validated occurrence itself: an earlier identical spelling
        # can have invalid boundaries and must never supply party metadata.
        for entry in accepted:
            start, end = entry['offsets']['start'], entry['offsets']['end']
            # Associate secondary attributes only within this line; multiple INNs
            # on one line make association uncertain and therefore leave nulls.
            line_start = text.rfind('\n', 0, start) + 1
            line_end = text.find('\n', end)
            line = text[line_start:line_end if line_end >= 0 else len(text)]
            multiple = sum(1 for item in accepted if item['строка'] == entry['строка']) > 1
            segment = '' if multiple else line
            kpp = re.search(r'\b(?:КПП|KPP)\s*[:№]?\s*([0-9]{9})(?![0-9])', segment, re.IGNORECASE)
            ogrn = re.search(r'\b(?:ОГРНИП|ОГРН|OGRNIP|OGRN)\s*[:№]?\s*([0-9]{15}|[0-9]{13})(?![0-9])', segment, re.IGNORECASE)
            address = re.search(r'\b(?:адрес|address)\s*:\s*([^\n;]{1,240})', segment, re.IGNORECASE)
            hints = []
            for role, pattern in (('supplier', r'поставщик|продавец|исполнитель|\bsupplier\b'),
                                  ('buyer', r'покупатель|заказчик|\bbuyer\b'),
                                  ('bank', r'\bбанк\b|\bbank\b'),
                                  ('own_company', r'наша компания|\bown company\b')):
                if re.search(pattern, segment, re.IGNORECASE):
                    hints.append(role)
            identifier = hashlib.sha256(('%s:%s:%s:%s' % (page, start, end, entry['инн'])).encode()).hexdigest()[:20]
            candidates.append({'candidate_id': 'candidate-' + identifier, 'inn': entry['инн'],
                               'kpp': kpp.group(1) if kpp else None, 'ogrn': ogrn.group(1) if ogrn else None,
                               'address': address.group(1).strip() if address else None,
                               'context': entry['контекст'], 'page': page,
                               'offsets': {'start': start, 'end': end}, 'role_hints': hints})
            if len(candidates) > MAX_CANDIDATES:
                raise ValueError('output_too_large')
    return candidates, limitations


def _executable(variable):
    value = os.environ.get(variable, '')
    return value if value and Path(value).is_absolute() and Path(value).is_file() and os.access(value, os.X_OK) else None


def _ocr_page(page):
    renderer = _executable('INN_CHECK_PDF_RENDERER')
    tesseract = _executable('INN_CHECK_TESSERACT')
    if not renderer:
        raise ValueError('missing_renderer')
    if not tesseract:
        raise ValueError('missing_tesseract')
    for name in ('page.png', 'recognized.txt'):
        Path(name).unlink(missing_ok=True)
    # Both are explicitly configured local binaries, invoked with fixed argv.
    # Inherit the worker process group/Windows job; never create another group.
    subprocess.run([renderer, '-f', str(page), '-l', str(page), '-singlefile', '-r', '150',
                    '-scale-to', '2500', '-png', 'input.bin', 'page'],
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    subprocess.run([tesseract, 'page.png', 'recognized', '-l', 'rus+eng', '--psm', '3'],
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    with Path('recognized.txt').open('rb') as stream:
        raw = stream.read(MAX_TEXT_BYTES + 1)
    if len(raw) > MAX_TEXT_BYTES:
        raise ValueError('output_too_large')
    return raw.decode('utf-8', errors='replace')


def _extract_inside(data, suffix, ocr):
    texts, limitations = [], []
    method = 'text'
    is_pdf = data.startswith(b'%PDF') or suffix == '.pdf'
    if is_pdf:
        method = 'pdf_text'
        try:
            from pypdf import PdfReader
        except ImportError:
            return _failure('missing_pdf_extra', method=method)
        try:
            reader = PdfReader(io.BytesIO(data), strict=False)
            if reader.is_encrypted:
                return _failure('encrypted_pdf', method=method)
            count = len(reader.pages)
            if count > MAX_PAGES:
                return _failure('too_many_pages', pages=count, method=method)
            for number, page in enumerate(reader.pages, 1):
                text = page.extract_text() or ''
                if not text.strip():
                    if ocr:
                        method = 'pdf_ocr'
                        try:
                            text = _ocr_page(number)
                        except ValueError as error:
                            return _failure(str(error), pages=count, method=method)
                        except (OSError, subprocess.SubprocessError):
                            return _failure('ocr_failed', pages=count, method=method)
                        limitations.append(_limitation('ocr_unverified', page=number))
                    if not text.strip():
                        limitations.append(_limitation('image_only', page=number))
                texts.append(text)
        except Exception:
            return _failure('broken_pdf', method=method)
    else:
        import extract_inn as legacy
        decoded = legacy.прочитать_документ('input' + suffix, data, docx_xml_limit=MAX_TEXT_BYTES)
        if decoded['текст'] is None:
            if decoded.get('reason_code') == 'output_too_large':
                return _failure('output_too_large', method='docx')
            return _failure('unsupported_format')
        texts = [decoded['текст']]
        method = {'текст': 'text'}.get(decoded['формат'], decoded['формат'])
        if decoded.get('причина'):
            # Do not echo parser diagnostics: they can contain filenames/content.
            limitations.append(_limitation('document_note'))
    try:
        candidates, rejected = _candidate_pages(texts)
    except ValueError as error:
        return _failure(str(error), pages=len(texts), method=method)
    limitations.extend(rejected)
    result = _result('partial' if any(x['reason_code'] in ('image_only', 'ocr_unverified', 'document_note') for x in limitations) else 'ok',
                     pages=len(texts), method=method, candidates=candidates, limitations=limitations)
    result['_texts'] = texts
    return result


def select(candidates: list[dict], *, role: str, candidate_id: str | None = None) -> dict:
    """Return a choice only after an explicit candidate id; hints are advisory."""
    def result(status, code, selected=None):
        return {'status': status, 'selected': selected, 'candidates': candidates if isinstance(candidates, list) else [], 'reason_code': code}
    if not isinstance(candidates, list) or len(candidates) > MAX_CANDIDATES or role not in ROLES or (candidate_id is not None and not isinstance(candidate_id, str)):
        return result('error', 'invalid_input')
    import extract_inn as legacy
    identifiers = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            return result('error', 'invalid_candidates')
        identifier, inn = candidate.get('candidate_id'), candidate.get('inn')
        if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', identifier) or identifier in identifiers:
            return result('error', 'invalid_candidates')
        if not isinstance(inn, str) or not re.fullmatch(r'[0-9]{10}|[0-9]{12}', inn) or legacy.валидный_инн(inn) is None or not legacy._код_региона_есть(inn):
            return result('error', 'invalid_candidates')
        identifiers.add(identifier)
    if not candidates:
        return result('needs_input', 'no_candidates')
    if candidate_id is None:
        return result('needs_input', 'explicit_selection_required')
    chosen = next((item for item in candidates if item['candidate_id'] == candidate_id), None)
    if chosen is None:
        return result('needs_input', 'candidate_not_found')
    return result('ok', 'explicit_selection', dict(chosen))


def _worker():
    try:
        options = json.loads(Path('options.json').read_text(encoding='utf-8'))
        with Path('input.bin').open('rb') as stream:
            data = stream.read(MAX_BYTES + 1)
        result = _failure('too_large') if len(data) > MAX_BYTES else _extract_inside(data, options['suffix'], options['ocr'])
        raw = json.dumps(result, ensure_ascii=False, allow_nan=False).encode('utf-8')
        if len(raw) > MAX_RESULT_BYTES:
            raw = json.dumps(_failure('output_too_large')).encode()
        Path('result.json').write_bytes(raw)
    except Exception:
        Path('result.json').write_text(json.dumps(_failure('execution_error')), encoding='utf-8')


def _stdin_worker():
    """Fixed wire worker, supervised directly by MCP's owning parent process.

    No nested process group: the caller MUST enforce 60s and cancel descendants.
    This entrypoint never resolves artifact ids or accepts filesystem paths.
    """
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            result = _failure('too_large')
        else:
            request = json.loads(raw)
            expected = {'schema_version', 'data_base64', 'filename', 'ocr'}
            if not isinstance(request, dict) or set(request) != expected or type(request['schema_version']) is not int or request['schema_version'] != 1 or not isinstance(request['data_base64'], str) or not isinstance(request['filename'], str) or type(request['ocr']) is not bool:
                result = _failure('invalid_input')
            else:
                data = base64.b64decode(request['data_base64'], validate=True)
                if len(data) > MAX_BYTES:
                    result = _failure('too_large')
                else:
                    with tempfile.TemporaryDirectory(prefix='inn-document-wire-') as directory:
                        previous = os.getcwd()
                        try:
                            os.chdir(directory)
                            Path('input.bin').write_bytes(data)
                            result = _extract_inside(data, _suffix(request['filename']), request['ocr'])
                        finally:
                            os.chdir(previous)
        result.pop('_texts', None)
    except (ValueError, TypeError, UnicodeError):
        result = _failure('invalid_input')
    except Exception:
        result = _failure('execution_error')
    output = json.dumps(result, ensure_ascii=False, allow_nan=False).encode('utf-8')
    if len(output) > MAX_RESULT_BYTES:
        output = json.dumps(_failure('output_too_large')).encode()
    sys.stdout.buffer.write(output + b'\n')


if __name__ == '__main__':
    if sys.argv[1:] == ['--stdin-worker']:
        _stdin_worker()
    elif sys.argv[1:] == ['--worker']:
        _worker()
    else:
        raise SystemExit(2)
