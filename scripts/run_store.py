"""Immutable local materialized runs, OS-locked writers and atomic committed HEAD.

Only a local filesystem is supported; network filesystem locking is not promised.
Reads never create directories/files, take write locks, recollect or score data.
A revision not reachable from HEAD is uncommitted and never selected by reads.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from redaction import redact

MAX_JSON_BYTES = 32 * 1024 * 1024
SCHEMA_VERSION = 1
_PAYLOAD_KEYS = {'fetch', 'fin', 'assessment', 'metadata'}
_RECORD_KEYS = {'schema_version', 'run_id', 'revision', 'parent_revision', 'created_at',
                'subject_inn', 'profile', 'workflow_status', 'payload', 'operation_id',
                'manifest', 'artifacts'}
_DIGEST = re.compile(r'[0-9a-f]{64}\Z')
_INN = re.compile(r'(?:[0-9]{10}|[0-9]{12})\Z')
_POSIX = os.name == 'posix'


class RunStoreError(Exception):
    """Safe boundary error: code is stable, message never contains caller input."""
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(message)


def _fail(code):
    messages = {
        'invalid_run_id': 'Некорректный идентификатор проверки.',
        'invalid_operation_id': 'Некорректный идентификатор операции.',
        'invalid_revision': 'Ревизия должна быть положительным целым числом.',
        'invalid_payload': 'Некорректный материализованный результат проверки.',
        'invalid_time': 'Ожидается момент времени ISO 8601 с часовым поясом.',
        'unsupported_schema': 'Версия схемы не поддерживается.',
        'too_large': 'Размер JSON превышает 32 MiB.',
        'unsafe_path': 'Путь хранилища содержит недопустимую ссылку или тип файла.',
        'not_found': 'Проверка или зафиксированная ревизия не найдена.',
        'revision_conflict': 'Ревизия изменилась; сначала перечитайте проверку.',
        'operation_conflict': 'Идентификатор операции уже связан с другими данными.',
        'corrupt_store': 'Нарушена целостность хранилища проверки.',
        'lock_timeout': 'Не удалось дождаться блокировки проверки.',
        'storage_error': 'Не удалось выполнить операцию локального хранилища.',
    }
    raise RunStoreError(code, messages[code]) from None


def _id(value, *, operation=False):
    try:
        valid = isinstance(value, str) and str(uuid.UUID(value)) == value and uuid.UUID(value).version == 4
    except (ValueError, AttributeError):
        valid = False
    if not valid:
        _fail('invalid_operation_id' if operation else 'invalid_run_id')
    return value


def _revision(value):
    if type(value) is not int or value < 1:
        _fail('invalid_revision')
    return value


def _json(value, *, code='invalid_payload'):
    try:
        def check(node):
            if isinstance(node, dict):
                for key, child in node.items():
                    if not isinstance(key, str):
                        _fail(code)
                    check(child)
            elif isinstance(node, list):
                for child in node:
                    check(child)
            elif node is not None and type(node) not in (str, bool, int, float):
                _fail(code)
        check(value)
        data = json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, RecursionError, UnicodeError):
        _fail(code)
    if len(data) > MAX_JSON_BYTES:
        _fail('too_large')
    return data


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _validate_payload(value):
    if not isinstance(value, dict) or set(value) != _PAYLOAD_KEYS:
        _fail('invalid_payload')
    if not all(isinstance(value[key], dict) for key in _PAYLOAD_KEYS):
        _fail('invalid_payload')
    version = value['metadata'].get('schema_version')
    if type(version) is not int or version != SCHEMA_VERSION:
        _fail('unsupported_schema')
    metadata = value['metadata']
    if 'raw_refs' in metadata and not isinstance(metadata['raw_refs'], dict):
        _fail('invalid_payload')
    if (metadata.get('profile') is not None and not isinstance(metadata['profile'], str)) or (
            'workflow_status' in metadata and not isinstance(metadata['workflow_status'], str)):
        _fail('invalid_payload')
    inn = value['fetch'].get('инн')
    if not isinstance(inn, str) or not _INN.fullmatch(inn):
        _fail('invalid_payload')
    _json(value)  # Validate and bound original input too, before redaction/any writes.


def _payload(value):
    _validate_payload(value)
    try:
        safe = redact(value)
        _validate_payload(safe)
        # Persist only canonical output that the reader will accept unchanged.
        if redact(safe) != safe:
            _fail('invalid_payload')
        if safe['fetch']['инн'] != value['fetch']['инн']:
            _fail('invalid_payload')
        # Raw evidence references are immutable, including response IDs/locators,
        # not only hashes. A secret collision cannot silently rewrite provenance.
        if safe['metadata'].get('raw_refs') != value['metadata'].get('raw_refs'):
            _fail('invalid_payload')
        for key in ('profile', 'workflow_status', 'engine_version', 'rules_version',
                    'normalizer_version', 'source_catalog_version', 'cutoff', 'evaluated_at'):
            if safe['metadata'].get(key) != value['metadata'].get(key):
                _fail('invalid_payload')
        _preserve_identifiers(value, safe)
    except (TypeError, ValueError, KeyError, RecursionError, RunStoreError):
        _fail('invalid_payload')
    return safe, _sha(_json(safe))


def _preserve_identifiers(original, safe):
    """Reject collisions with ordinary INNs/digests rather than corrupt evidence."""
    if isinstance(original, dict):
        for key in original.keys() & safe.keys():
            if ((key in {'raw_ref', 'raw_refs', 'id', 'инн', 'inn', 'locator',
                         'subject_locator', 'transformation'} or key.endswith(('_id', '_inn')))
                    and original[key] != safe[key]):
                _fail('invalid_payload')
            _preserve_identifiers(original[key], safe[key])
    elif isinstance(original, list):
        for before, after in zip(original, safe):
            _preserve_identifiers(before, after)
    elif (isinstance(original, str) and (_DIGEST.fullmatch(original) or _INN.fullmatch(original))
          and original != safe):
        _fail('invalid_payload')


def _timestamp(now):
    if now is None:
        return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    try:
        if not isinstance(now, str):
            _fail('invalid_time')
        dt = datetime.fromisoformat(now.replace('Z', '+00:00'))
        if dt.tzinfo is None or 'T' not in now:
            _fail('invalid_time')
        return dt.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
    except ValueError:
        _fail('invalid_time')


def _unsafe(st):
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, 'st_file_attributes', 0) & 0x400)


class _Dir:
    """POSIX dir-fd anchoring; Windows rejects links/reparse points on every access."""
    def __init__(self, path, fd=None):
        self.path, self.fd = Path(path), fd

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _check_windows(self):
        current = Path(self.path.anchor)
        for piece in self.path.parts[1:]:
            current /= piece
            st = os.lstat(current)
            if _unsafe(st) or not stat.S_ISDIR(st.st_mode):
                _fail('unsafe_path')

    def info(self, name):
        try:
            if _POSIX:
                st = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
            else:
                self._check_windows()
                st = os.lstat(self.path / name)
            if _unsafe(st):
                _fail('unsafe_path')
            return st
        except FileNotFoundError:
            return None

    def directory(self, name, *, create=False, exclusive=False):
        if create:
            try:
                if _POSIX:
                    os.mkdir(name, mode=0o700, dir_fd=self.fd)
                else:
                    self._check_windows()
                    os.mkdir(self.path / name, mode=0o700)
                self.sync()
            except FileExistsError:
                if exclusive:
                    raise
        st = self.info(name)
        if st is None:
            _fail('not_found')
        if not stat.S_ISDIR(st.st_mode):
            _fail('unsafe_path')
        fd = None
        if _POSIX:
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.fd)
        return _Dir(self.path / name, fd)

    def open(self, name, flags, mode=0o600):
        st = self.info(name)
        if st is not None and (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1):
            _fail('unsafe_path')
        flags |= getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOINHERIT', 0)
        fd = (os.open(name, flags, mode, dir_fd=self.fd) if _POSIX else
              os.open(self.path / name, flags, mode))
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or _unsafe(st):
            os.close(fd)
            _fail('unsafe_path')
        return fd

    def read(self, name):
        try:
            fd = self.open(name, os.O_RDONLY)
        except FileNotFoundError:
            _fail('not_found')
        with os.fdopen(fd, 'rb') as file:
            if os.fstat(file.fileno()).st_size > MAX_JSON_BYTES:
                _fail('too_large')
            data = file.read(MAX_JSON_BYTES + 1)
        if len(data) > MAX_JSON_BYTES:
            _fail('too_large')
        return data

    def replace(self, src, dst, target=None):
        target = self if target is None else target
        for directory, name in [(self, src), (target, dst)]:
            st = directory.info(name)
            if st is not None and (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1):
                _fail('unsafe_path')
        if _POSIX:
            os.replace(src, dst, src_dir_fd=self.fd, dst_dir_fd=target.fd)
        else:
            os.replace(self.path / src, target.path / dst)
        self.sync()
        if target is not self:
            target.sync()

    def unlink(self, name):
        if _POSIX:
            os.unlink(name, dir_fd=self.fd)
        else:
            self._check_windows()
            os.unlink(self.path / name)

    def sync(self):
        if _POSIX:
            os.fsync(self.fd)


def _root(root, *, create=False):
    configured = root if root is not None else os.environ.get(
        'INN_CHECK_RUNS_DIR', '~/.local/share/inn-check-ru/runs')
    try:
        path = Path(configured).expanduser()
        if '..' in path.parts:
            _fail('unsafe_path')
        if not path.is_absolute():
            path = Path.cwd() / path
    except (TypeError, ValueError):
        _fail('unsafe_path')
    current = _Dir(path.anchor, os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY) if _POSIX else None)
    try:
        for component in path.parts[1:]:
            child = current.directory(component, create=create)
            current.close()
            current = child
        return current
    except BaseException:
        current.close()
        raise


@contextmanager
def _errors():
    try:
        yield
    except RunStoreError:
        raise
    except (ValueError, OverflowError):
        _fail('unsafe_path')
    except OSError as error:
        _fail('unsafe_path' if error.errno in (errno.ELOOP, errno.ENOTDIR) else 'storage_error')


@contextmanager
def _lock(run):
    fd = run.open('.lock', os.O_RDWR | os.O_CREAT)
    try:
        if _POSIX:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        else:
            import msvcrt
            if os.fstat(fd).st_size == 0:
                os.write(fd, b'\0')
            deadline = time.monotonic() + 30
            while True:
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        _fail('lock_timeout')
                    time.sleep(.05)
        yield
    finally:
        # Closing the descriptor releases the OS lock, including on process death.
        os.close(fd)


def _atomic_write(directory, name, data):
    temp = '.' + name + '.' + str(uuid.uuid4()) + '.tmp'
    fd = directory.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        with os.fdopen(fd, 'wb') as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        directory.replace(temp, name)
    finally:
        # A killed writer may leave its private temp; readers ignore all temps.
        if directory.info(temp) is not None:
            directory.unlink(temp)


def _parse(data):
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                _fail('corrupt_store')
            result[key] = value
        return result
    try:
        value = json.loads(data, object_pairs_hook=object_pairs,
                           parse_constant=lambda _: _fail('corrupt_store'))
    except (ValueError, RecursionError, UnicodeError):
        _fail('corrupt_store')
    if not isinstance(value, dict):
        _fail('corrupt_store')
    return value


def _record(payload, digest, run_id, revision, parent_digest, operation_id, now):
    metadata = payload['metadata']
    return {
        'schema_version': SCHEMA_VERSION, 'run_id': run_id, 'revision': revision,
        'parent_revision': revision - 1 if revision > 1 else None, 'created_at': now,
        'subject_inn': payload['fetch']['инн'],
        'profile': metadata.get('profile', payload['fetch'].get('профиль')),
        'workflow_status': metadata.get('workflow_status', 'materialized'),
        'payload': payload, 'operation_id': operation_id,
        'manifest': {'input_sha256': digest, 'previous_revision_sha256': parent_digest,
                     'raw_refs': metadata.get('raw_refs', {}),
                     'versions': {key: metadata[key] for key in (
                         'engine_version', 'rules_version', 'normalizer_version',
                         'source_catalog_version') if key in metadata},
                     'cutoff': metadata.get('cutoff', metadata.get('evaluated_at'))},
        'artifacts': [],
    }


def _history(run, run_id):
    head = _parse(run.read('HEAD.json'))
    if type(head.get('schema_version')) is not int or head.get('schema_version') != SCHEMA_VERSION:
        _fail('unsupported_schema' if 'schema_version' in head else 'corrupt_store')
    if (set(head) != {'schema_version', 'revision', 'digest'} or
            type(head['revision']) is not int or head['revision'] < 1 or
            not isinstance(head['digest'], str) or not _DIGEST.fullmatch(head['digest'])):
        _fail('corrupt_store')
    expected = head['digest']
    records = []
    with run.directory('revisions') as revisions:
        for number in range(head['revision'], 0, -1):
            try:
                data = revisions.read('%06d.json' % number)
            except RunStoreError as error:
                if error.code == 'not_found':
                    _fail('corrupt_store')
                raise
            if _sha(data) != expected:
                _fail('corrupt_store')
            record = _parse(data)
            if type(record.get('schema_version')) is not int or record.get('schema_version') != SCHEMA_VERSION:
                _fail('unsupported_schema')
            if (set(record) != _RECORD_KEYS or record['run_id'] != run_id or
                    type(record['revision']) is not int or record['revision'] != number or
                    (number > 1 and type(record['parent_revision']) is not int) or
                    record['parent_revision'] != (number - 1 if number > 1 else None)):
                _fail('corrupt_store')
            try:
                safe, digest = _payload(record['payload'])
                manifest = record['manifest']
                timestamp = _timestamp(record['created_at'])
                operation = record['operation_id']
                if operation is not None:
                    _id(operation, operation=True)
                if number > 1 and operation is None:
                    _fail('corrupt_store')
                rebuilt = _record(safe, digest, run_id, number,
                                  manifest['previous_revision_sha256'], operation, timestamp)
                if rebuilt != record or safe != record['payload']:
                    _fail('corrupt_store')
            except (KeyError, TypeError, RunStoreError):
                _fail('corrupt_store')
            expected = manifest['previous_revision_sha256']
            if ((number == 1 and expected is not None) or
                    (number > 1 and (not isinstance(expected, str) or not _DIGEST.fullmatch(expected)))):
                _fail('corrupt_store')
            records.append(record)
    records.reverse()
    operations = [record['operation_id'] for record in records if record['operation_id'] is not None]
    if len(operations) != len(set(operations)):
        _fail('corrupt_store')
    return records, head['digest']


def _commit(run, record):
    data = _json(record)
    number = record['revision']
    name = '%06d.json' % number
    with run.directory('revisions') as revisions:
        # A crash may have replaced N+1 but not HEAD. Preserve orphan bytes under
        # an immutable content-derived name before a fresh writer reuses N+1.
        if revisions.info(name) is not None:
            old = revisions.read(name)
            if old != data:
                with revisions.directory('orphans', create=True) as orphans:
                    orphan = '%06d-%s.json' % (number, _sha(old))
                    if orphans.info(orphan) is not None:
                        if orphans.read(orphan) != old:
                            _fail('corrupt_store')
                        revisions.unlink(name)
                    else:
                        revisions.replace(name, orphan, target=orphans)
            else:
                # An identical interrupted operation is safe to finish committing.
                data = old
        if revisions.info(name) is None:
            _atomic_write(revisions, name, data)
    head = {'schema_version': SCHEMA_VERSION, 'revision': number, 'digest': _sha(data)}
    _atomic_write(run, 'HEAD.json', _json(head))


def create(payload: dict, *, root=None, now=None) -> dict:
    """Create an immutable revision 1 for an already materialized result."""
    safe, digest = _payload(payload)
    timestamp = _timestamp(now)
    run_id = str(uuid.uuid4())
    record = _record(safe, digest, run_id, 1, None, None, timestamp)
    _json(record)  # Bound the full record before creating any paths.
    with (_errors(), _root(root, create=True) as base,
          base.directory(run_id, create=True, exclusive=True) as run, _lock(run)):
        with run.directory('revisions', create=True):
            pass
        _commit(run, record)
    return record


def read(run_id: str, *, revision: int | None = None, root=None) -> dict:
    """Read a committed revision, validating its full committed hash chain."""
    _id(run_id)
    if revision is not None:
        _revision(revision)
    with _errors(), _root(root) as base, base.directory(run_id) as run:
        records, _ = _history(run, run_id)
        number = revision if revision is not None else len(records)
        if number > len(records):
            _fail('not_found')
        return records[number - 1]


def append(run_id: str, payload: dict, *, expected_revision: int,
           operation_id: str, root=None, now=None) -> dict:
    """Append with CAS; identical committed operation retries precede CAS checks."""
    _id(run_id)
    _revision(expected_revision)
    _id(operation_id, operation=True)
    safe, digest = _payload(payload)
    timestamp = _timestamp(now)
    with _errors(), _root(root) as base, base.directory(run_id) as run, _lock(run):
        records, parent_digest = _history(run, run_id)
        for old in records:
            if old['operation_id'] == operation_id:
                if old['manifest']['input_sha256'] != digest:
                    _fail('operation_conflict')
                return old
        if expected_revision != len(records):
            _fail('revision_conflict')
        if safe['fetch']['инн'] != records[0]['subject_inn']:
            _fail('invalid_payload')
        record = _record(safe, digest, run_id, len(records) + 1, parent_digest,
                         operation_id, timestamp)
        _commit(run, record)
        return record


def find_operation(run_id: str, operation_id: str, *, root=None) -> dict | None:
    """Look up an idempotency result only in the committed, verified history."""
    _id(run_id)
    _id(operation_id, operation=True)
    with _errors(), _root(root) as base, base.directory(run_id) as run:
        records, _ = _history(run, run_id)
        return next((r for r in records if r['operation_id'] == operation_id), None)


@contextmanager
def workflow_lock(run_id: str, *, root=None):
    """Serialize workflow writers, including network work, independently of CAS."""
    _id(run_id)
    with (_errors(), _root(root) as base, base.directory(run_id) as run,
          run.directory('workflow', create=True) as workflow, _lock(workflow)):
        yield


def _blob(data):
    if not isinstance(data, bytes):
        _fail('invalid_payload')
    if len(data) > MAX_JSON_BYTES:
        _fail('too_large')
    return _sha(data)


def _digest_name(digest):
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        _fail('invalid_payload')
    return digest


def put_raw(run_id: str, data: bytes, *, root=None) -> str:
    """Persist already sanitized capture bytes under their real immutable digest.

    Capture sanitization belongs to evidence_pack: this helper does not rewrite
    bytes, guess formats, or invent a hash of the original unsanitized response.
    """
    _id(run_id)
    digest = _blob(data)
    with (_errors(), _root(root) as base, base.directory(run_id) as run, _lock(run),
          run.directory('raw', create=True) as raw):
        if raw.info(digest) is not None:
            if raw.read(digest) != data:
                _fail('corrupt_store')
        else:
            _atomic_write(raw, digest, data)
    return digest


def read_raw(run_id: str, digest: str, *, root=None) -> bytes:
    """Read and verify one content-addressed capture without filesystem writes."""
    _id(run_id)
    _digest_name(digest)
    with (_errors(), _root(root) as base, base.directory(run_id) as run,
          run.directory('raw') as raw):
        data = raw.read(digest)
        if _sha(data) != digest:
            _fail('corrupt_store')
        return data


_ARTIFACT_FORMATS = {'markdown', 'json', 'docx', 'snapshot'}
_ARTIFACT_KEYS = {'artifact_id', 'format', 'sha256', 'source_revision'}


def _artifacts(run, run_id):
    if run.info('artifacts.json') is None:
        return []
    registry = _parse(run.read('artifacts.json'))
    if set(registry) != {'schema_version', 'artifacts'} or registry['schema_version'] != 1:
        _fail('corrupt_store')
    records, _ = _history(run, run_id)
    items = registry['artifacts']
    if not isinstance(items, list):
        _fail('corrupt_store')
    seen = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != _ARTIFACT_KEYS:
            _fail('corrupt_store')
        try:
            _id(item['artifact_id'])
            _digest_name(item['sha256'])
            _revision(item['source_revision'])
            if (item['format'] not in _ARTIFACT_FORMATS or
                    item['source_revision'] > len(records) or item['artifact_id'] in seen):
                _fail('corrupt_store')
        except (RunStoreError, TypeError):
            _fail('corrupt_store')
        seen.add(item['artifact_id'])
    return items


def put_artifact(run_id: str, data: bytes, *, format: str,
                 source_revision: int, root=None) -> dict:
    """Atomic artifact promotion then registry append; orphans remain invisible."""
    _id(run_id)
    _revision(source_revision)
    if not isinstance(format, str) or format not in _ARTIFACT_FORMATS:
        _fail('invalid_payload')
    digest = _blob(data)
    artifact = {'artifact_id': str(uuid.uuid4()), 'format': format,
                'sha256': digest, 'source_revision': source_revision}
    with _errors(), _root(root) as base, base.directory(run_id) as run, _lock(run):
        records, _ = _history(run, run_id)
        if source_revision > len(records):
            _fail('not_found')
        previous = _artifacts(run, run_id)
        registry = _json({'schema_version': 1, 'artifacts': [*previous, artifact]})
        with run.directory('artifacts', create=True) as artifacts:
            _atomic_write(artifacts, artifact['artifact_id'], data)
        _atomic_write(run, 'artifacts.json', registry)
    return artifact


def list_artifacts(run_id: str, *, root=None) -> list[dict]:
    _id(run_id)
    with _errors(), _root(root) as base, base.directory(run_id) as run:
        return _artifacts(run, run_id)


def read_artifact(run_id: str, artifact_id: str, *, root=None) -> bytes:
    _id(run_id)
    _id(artifact_id)
    with _errors(), _root(root) as base, base.directory(run_id) as run:
        artifact = next((a for a in _artifacts(run, run_id) if a['artifact_id'] == artifact_id), None)
        if artifact is None:
            _fail('not_found')
        with run.directory('artifacts') as artifacts:
            data = artifacts.read(artifact_id)
        if _sha(data) != artifact['sha256']:
            _fail('corrupt_store')
        return data


_CAPTURE_KEYS = {'response_id', 'sha256', 'subject_inn', 'источник', 'метод',
                 'url', 'статус', 'время_utc', 'capture_format', 'original_response_preserved'}


def _capture_entries(run):
    if run.info('captures.json') is None:
        return []
    journal = _parse(run.read('captures.json'))
    if (set(journal) != {'schema_version', 'responses'} or journal['schema_version'] != 1
            or not isinstance(journal['responses'], list)):
        _fail('corrupt_store')
    seen = set()
    for response in journal['responses']:
        try:
            if not isinstance(response, dict) or set(response) != _CAPTURE_KEYS:
                _fail('corrupt_store')
            rid = response['response_id']
            if not isinstance(rid, str) or not rid.startswith('response-') or rid in seen:
                _fail('corrupt_store')
            _id(rid.removeprefix('response-'))
            _digest_name(response['sha256'])
            if redact(response) != response:
                _fail('corrupt_store')
            seen.add(rid)
        except (RunStoreError, ValueError, TypeError):
            _fail('corrupt_store')
    return journal['responses']


def put_capture(run_id: str, response: dict, *, root=None) -> dict:
    """Durable response identity before parsing; capture bytes are already safe.

    An interrupted source can leave no normalized block. Its response ID, source,
    actual saved hash and sanitization label still survive in this append journal.
    """
    _id(run_id)
    if not isinstance(response, dict) or not _CAPTURE_KEYS <= response.keys():
        _fail('invalid_payload')
    metadata = {key: response[key] for key in _CAPTURE_KEYS}
    rid = metadata['response_id']
    if not isinstance(rid, str) or not rid.startswith('response-'):
        _fail('invalid_payload')
    _id(rid.removeprefix('response-'))
    if redact(metadata) != metadata or _blob(response.get('данные')) != metadata['sha256']:
        _fail('invalid_payload')
    _json(metadata)
    # Bytes are immutable before their metadata is published. A kill between the
    # two writes leaves an unreferenced blob, never a dangling visible reference.
    put_raw(run_id, response['данные'], root=root)
    with _errors(), _root(root) as base, base.directory(run_id) as run, _lock(run):
        entries = _capture_entries(run)
        existing = next((r for r in entries if r['response_id'] == rid), None)
        if existing is not None:
            if existing != metadata:
                _fail('operation_conflict')
            return existing
        _atomic_write(run, 'captures.json', _json({'schema_version': 1, 'responses': [*entries, metadata]}))
    return metadata


def captured_responses(run_id: str, *, root=None) -> list[dict]:
    """Read durable capture metadata and validate its referenced raw bytes."""
    _id(run_id)
    with _errors(), _root(root) as base, base.directory(run_id) as run:
        entries = _capture_entries(run)
        if entries:
            with run.directory('raw') as raw:
                for response in entries:
                    if _sha(raw.read(response['sha256'])) != response['sha256']:
                        _fail('corrupt_store')
        return entries
