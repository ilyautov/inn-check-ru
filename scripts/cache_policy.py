"""Read-only cache inventory and explicit, bounded transactional updates (stdlib).

Managed generations use one atomic manifest pointer. All legacy readers are
routed through dataset_root; old flat caches remain supported and are never
removed by an update. No status/plan call creates directories or probes a URL.
"""
from __future__ import annotations

import contextlib
import copy
import datetime as dt
import gzip
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from importlib import resources
from pathlib import Path

# Preserve flat installed imports and legacy absolute-file entry points.
try:
    from sibling_imports import load_sibling as _load_sibling
except ModuleNotFoundError as _error:
    if _error.name != "sibling_imports":
        raise
    import importlib.util as _import_util
    import sys as _import_sys
    from pathlib import Path as _ImportPath
    _spec = _import_util.spec_from_file_location(
        "sibling_imports", _ImportPath(__file__).resolve().with_name("sibling_imports.py"))
    _helper = _import_util.module_from_spec(_spec)
    _spec.loader.exec_module(_helper)
    # Publish only the complete, stateless bootstrap. Concurrent bootstrap calls
    # all select the same helper; dependency loads use Python's module locks.
    _load_sibling = _import_sys.modules.setdefault("sibling_imports", _helper).load_sibling

redact = _load_sibling("redaction").redact
_process_runtime = _load_sibling("process_runtime")
_start_process = _process_runtime._start_process
_ProcessExchange = _process_runtime._ProcessExchange
_ProcessStartError = _process_runtime._ProcessStartError
_stop_process_tree = _process_runtime._stop_process_tree

_MANIFEST = '.workflow-cache.json'


def inventory():
    try:
        text = (resources.files('inn_check_ru_data') / 'cache_policy.json').read_text(encoding='utf-8')
    except (ModuleNotFoundError, FileNotFoundError):
        text = (Path(__file__).resolve().parent.parent / 'data' / 'cache_policy.json').read_text(encoding='utf-8')
    data = json.loads(text)
    if data.get('schema_version') != 1:
        raise ValueError('unsupported cache policy version')
    return copy.deepcopy(data['datasets'])


def _item(dataset_id):
    for item in inventory():
        if dataset_id == item['dataset_id'] or dataset_id in item['aliases']:
            return item
    raise ValueError('unknown dataset id')


def cache_root(root=None):
    return Path(root) if root is not None else Path.home() / '.cache' / 'inn-check-ru'


def _manifest(root):
    try:
        result = json.loads((root / _MANIFEST).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {'schema_version': 1, 'datasets': {}}
    if result.get('schema_version') != 1 or not isinstance(result.get('datasets'), dict):
        raise ValueError('invalid cache generation manifest')
    return result


def dataset_root(dataset_id, *, root=None):
    """Effective directory for this dataset; pure read with legacy fallback.

    A malformed managed pointer is an error, never an excuse to silently use
    another cache. Resolved paths must remain inside the explicitly chosen root.
    """
    item = _item(dataset_id)
    base = cache_root(root)
    pointer = _manifest(base)['datasets'].get(item['dataset_id'])
    if pointer is None:
        return base / item['cache_subdir'] if item['cache_subdir'] else base
    if not isinstance(pointer, str):
        raise TypeError('invalid cache generation pointer')
    result = base / pointer
    if not result.resolve().is_relative_to(base.resolve() / '.workflow-generations'):
        raise ValueError('cache generation pointer escaped root')
    return result


def configure_readers(*, root=None):
    """Refresh loaded legacy modules before a long-lived collector run.

    Called once at collection entry. Explicit module CACHE_DIR overrides remain
    untouched when there is no managed pointer and no explicit root argument.
    """
    base = cache_root(root)
    managed = _manifest(base)['datasets']
    for item in inventory():
        if root is None and item['dataset_id'] not in managed:
            continue
        module = sys.modules.get(item['updater'])
        if module is None:
            continue
        path = dataset_root(item['dataset_id'], root=base)
        if item['dataset_id'] == 'opendata_refresh':
            module.OPENDATA_DIR = str(path)
            for name, descriptor in module.ДАТАСЕТЫ.items():
                descriptor['каталог'] = str(path / name)
        else:
            module.CACHE_DIR = path if item['dataset_id'] == 'cbr_warning' else str(path)
        if item['dataset_id'] == 'sanctions':
            for descriptor in module.SOURCES.values():
                descriptor['file'] = str(path / Path(descriptor['file']).name)


def _moment(value):
    if value is None:
        return dt.datetime.now(dt.timezone.utc)
    result = value if isinstance(value, dt.datetime) else dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return result.replace(tzinfo=dt.timezone.utc) if result.tzinfo is None else result.astimezone(dt.timezone.utc)


def _iso(value):
    return value.astimezone(dt.timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def _dates(value, names):
    if not isinstance(value, dict):
        return []
    found = []
    for key, child in value.items():
        if key in names and isinstance(child, str):
            found.append(child)
        elif isinstance(child, dict):
            found.extend(_dates(child, names))
    return found


def _read_metadata(item, path):
    name = item['dataset_id']
    if name == 'opendata_refresh':
        return [json.loads(x.read_text(encoding='utf-8')) for x in path.glob('**/*.zip.meta.json')]
    if name == 'sanctions':
        result = []
        for filename in ('terrorists-list.html', 'sdn.csv', 'eu-sanctions.xml'):
            body = path / filename
            if not body.is_file():
                raise FileNotFoundError('sanctions body missing')
            metadata = json.loads(Path(str(body) + '.meta.json').read_text(encoding='utf-8'))
            metadata['effective_mtime'] = _iso(dt.datetime.fromtimestamp(body.stat().st_mtime, dt.timezone.utc))
            result.append(metadata)
        return result
    file = path / item['metadata_source']
    if file.name.endswith('.gz'):
        with gzip.open(file, 'rt', encoding='utf-8') as stream:
            return json.load(stream)
    return json.loads(file.read_text(encoding='utf-8'))


def _required_cache_files(item, path, metadata):
    """Check reader prerequisites by stat only; never hash/parse dump bodies."""
    files = [path / name for name in item['required_files']]
    name = item['dataset_id']
    if name == 'cbr_warning':
        body = str(metadata.get('файл') or 'black-list.json')
        if '/' in body or '\\' in body or not body.startswith('black-list'):
            raise ValueError('invalid CBR body path')
        files.append(path / body)
    elif name == 'erknm':
        folder = path / metadata['папка']
        if folder.parent.resolve() != path.resolve():
            raise ValueError('invalid ERKNM folder')
        files.extend(folder / filename for filename in
                     ['словарь.json.gz'] + ['%02x.json.gz' % i for i in range(256)])
    elif name == 'opendata_refresh':
        for dataset in item['required_archive_datasets']:
            # Match the legacy archive reader's filename grammar, not arbitrary zip files.
            import re
            archives = [f for f in (path / dataset).glob('*.zip')
                        if re.fullmatch(r'data-\d{8}-structure-\d{8}\.zip', f.name)]
            if not archives:
                raise FileNotFoundError('required FNS archive missing')
            archive = max(archives, key=lambda f: f.name)
            files.extend([archive, Path(str(archive) + '.meta.json')])
        for dataset in item['required_index_datasets']:
            index = path / dataset / ('index-%s.bin' % dataset)
            files.extend([index, Path(str(index) + '.meta.json')])
    for file in files:
        if not file.is_file():
            raise FileNotFoundError('required reader file missing')
    return len(files)


def _freshness(item, metadata, now):
    name = item['dataset_id']
    published = _dates(metadata, {'дата_выгрузки', 'выгрузка', 'последняя_выгрузка'})
    downloaded = _dates(metadata, {'скачан', 'скачано_utc', 'построен'})
    if name == 'opendata_refresh':
        return 'unknown', 'policy_unknown', None, None, None
    if name == 'sanctions':
        downloaded = [x['скачан'] for x in metadata]
        bases = [x['effective_mtime'] for x in metadata]
    elif name == 'cbr_warning':
        bases = [metadata['скачано_utc']]
        # Existing loader performs body hash/schema validation without network.
    elif name == 'fsa_registries':
        bases = [x['по'] for x in metadata['охват'].values()]
    elif name == 'erknm':
        bases = [metadata['охват']['выгрузка']]
    elif name == 'rzn_licenses':
        bases = [x.get('дата_выгрузки') or x.get('действителен_до') for x in metadata['наборы'].values()]
    elif name in ('cbr_registries', 'fts_registries', 'rkn_registries'):
        bases = [x.get('скачан') for x in metadata['реестры'].values()]
    elif name == 'uc_mintsifry':
        bases = [metadata['скачан']]
    elif name == 'minpromtorg':
        bases = [x['выгрузка'] for x in metadata['наборы'].values()]
    else:
        bases = [metadata.get('дата_выгрузки') or metadata.get('выгрузка') or metadata.get('действителен_до')]
    if not bases or any(not x for x in bases):
        raise ValueError('metadata missing source date')
    moments = [_moment(x) for x in bases]
    if any(_moment(x) > now for x in (published or bases)) and name not in ('disq_dump', 'rzn_licenses'):
        return 'unknown', 'future_metadata', min(published, default=None), min(downloaded, default=None), None
    earliest = min(moments)
    expiry = earliest + dt.timedelta(days=item['ttl_days'])
    stale = now > expiry if name in ('sanctions', 'cbr_warning') else now.date() > expiry.date()
    if name in ('disq_dump', 'rzn_licenses'):
        nodes = metadata['наборы'].values() if name == 'rzn_licenses' else [metadata]
        expirations = [(_moment(node['действителен_до']) + dt.timedelta(days=1)
                        if node.get('действителен_до') else
                        _moment(node['дата_выгрузки']) + dt.timedelta(days=item['ttl_days']))
                       for node in nodes]
        expiry = min(expirations)
        nodes = metadata['наборы'].values() if name == 'rzn_licenses' else [metadata]
        stale = any((now.date() > _moment(node['действителен_до']).date()
                     if node.get('действителен_до') else
                     (now.date() - _moment(node['дата_выгрузки']).date()).days > item['ttl_days'])
                    for node in nodes)
    due = expiry - dt.timedelta(days=item['refresh_margin_days'])
    return ('stale' if stale else 'fresh', 'ttl_expired' if stale else 'within_ttl',
            min(published, default=None), min(downloaded, default=None), _iso(due))


def _profile(profile):
    profiles = _load_sibling("profiles")
    data = profiles.load()['профили']
    if profile not in data:
        raise ValueError('unknown profile')
    return data[profile]


def status(profile, *, now, root=None):
    selected = _profile(profile)
    moment = _moment(now)
    datasets = []
    for item in inventory():
        row = dict(item)
        name = item['dataset_id']
        row.update({'argv': [sys.executable, '-m', 'workflow_cli', 'cache', 'update', '--dataset', name,
                             '--profile', profile, '--max-bytes', '50000000', '--timeout-seconds', '120', '--json'],
                    'updater_argv': [sys.executable, '-m', item['updater'], '--refresh'],
                    'published_at': None, 'downloaded_at': None, 'next_due_at': None,
                    'state': 'missing', 'reason_code': 'cache_missing',
                    'readiness': 'missing', 'readiness_basis': 'required_files_exist',
                    'required_files_checked': 0,
                    'cache_path': str(cache_root(root) / item['cache_subdir']),
                    'minimal_required': item['source_id'] in selected.get('источники_обязательные', [])
                                        and not item['large_optional']})
        applicable = item['applicable_profiles'] == '*' or profile in item['applicable_profiles']
        row['applicable'] = applicable
        try:
            path = dataset_root(name, root=root)
            row['cache_path'] = str(path)
            if not item['enabled'] or not applicable:
                row.update(state='disabled', readiness='disabled', reason_code='disabled_by_policy' if not item['enabled'] else 'not_applicable')
            else:
                metadata = _read_metadata(item, path)
                row['required_files_checked'] = _required_cache_files(item, path, metadata)
                row['readiness'] = 'present'
                if name == 'opendata_refresh' and not metadata:
                    if path.exists():
                        row.update(state='unknown', reason_code='policy_unknown')
                else:
                    state, reason, published, downloaded, due = _freshness(item, metadata, moment)
                    row.update(state=state, reason_code=reason, published_at=published,
                               downloaded_at=downloaded, next_due_at=due)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, KeyError, AttributeError, EOFError):
            row.update(state='unknown', readiness='unknown', reason_code='invalid_metadata')
        datasets.append(row)
    return {'schema_version': 1, 'profile': profile, 'checked_at': _iso(moment),
            'cache_root': str(cache_root(root)), 'executes_updates': False, 'datasets': datasets}


def plan(profile, *, now, root=None):
    report = status(profile, now=now, root=root)
    moment = _moment(now)
    actions = [x for x in report['datasets'] if x['state'] in ('missing', 'stale', 'unknown') or
               x['state'] == 'fresh' and x['next_due_at'] and _moment(x['next_due_at']) <= moment]
    for row in actions:
        row['requires_explicit_selection'] = True
    return {**report, 'actions': actions, 'minimal_actions': [x for x in actions if x['minimal_required']],
            'optional_actions': [x for x in actions if not x['minimal_required']], 'executes_updates': False}


@contextlib.contextmanager
def _writer_lock(root):
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / '.workflow-update.lock'
    if lock_path.is_symlink():
        raise OSError('symlink writer lock rejected')
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    lock = os.fdopen(descriptor, 'r+b')
    try:
        if os.name == 'nt':
            import msvcrt
            lock.seek(0)
            if not lock.read(1):
                lock.write(b'0')
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        lock.close()  # OS lock released even on process death; file is intentionally retained.


def _worker_argv(item, ledger, maximum):
    return [sys.executable, '-m', 'cache_update_worker', '--updater', item['updater'],
            '--max-bytes', str(maximum), '--ledger', str(ledger),
            '--previous', item.get('_previous_root', '')]


def _stop(process, exchange=None):
    # The process group/job may still own descendants after its leader exits.
    # A cleanup gap must never turn a successful exit into a promoted cache.
    if _stop_process_tree(process, exchange=exchange):
        raise OSError('cache worker cleanup unconfirmed')


def _run_worker(item, home, ledger, budget, deadline):
    # Public registry updaters do not need provider keys or unrelated host
    # credentials. Retain interpreter/loader and locale settings, project TLS
    # and proxy.py configuration, plus urllib's legacy proxy channels. In CGI,
    # REQUEST_METHOD keeps urllib's protection against untrusted HTTP_PROXY.
    inherited = (
        'PATH', 'PATHEXT', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'SYSTEMDRIVE',
        'PYTHONHOME', 'PYTHONPATH', 'PYTHONUTF8', 'PYTHONIOENCODING',
        'PYTHONCOERCECLOCALE', 'PYTHONNOUSERSITE', 'PYTHONUSERBASE', 'PYTHONPLATLIBDIR',
        'LD_LIBRARY_PATH', 'DYLD_LIBRARY_PATH', 'DYLD_FALLBACK_LIBRARY_PATH',
        'LANG', 'LANGUAGE', 'LC_ALL', 'LC_CTYPE', 'LC_MESSAGES', 'LC_TIME',
        'LC_NUMERIC', 'LC_MONETARY', 'LC_COLLATE', 'LC_ADDRESS', 'LC_IDENTIFICATION',
        'LC_MEASUREMENT', 'LC_NAME', 'LC_PAPER', 'LC_TELEPHONE', 'TZ',
        'SSL_CERT_FILE', 'SSL_CERT_DIR', 'COUNTERPARTY_CA_BUNDLE', 'COUNTERPARTY_INSECURE',
        'INN_CHECK_PROXY', 'HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy',
        'ALL_PROXY', 'all_proxy', 'FTP_PROXY', 'ftp_proxy', 'NO_PROXY', 'no_proxy',
        'REQUEST_METHOD',
    )
    env = {name: value for name in inherited
           if (value := os.environ.get(name)) is not None}
    env.update(HOME=str(home), USERPROFILE=str(home), PYTHONDONTWRITEBYTECODE='1',
               XDG_CACHE_HOME=str(home / '.cache'), XDG_CONFIG_HOME=str(home / '.config'),
               TMPDIR=str(home / 'tmp'), TEMP=str(home / 'tmp'), TMP=str(home / 'tmp'))
    (home / 'tmp').mkdir()
    # Preserve importing the installed flat modules or repository scripts, even
    # when the parent was launched by a script path outside cwd.
    paths = [str(Path(__file__).resolve().parent)]
    if env.get('PYTHONPATH'):
        paths.append(env['PYTHONPATH'])
    env['PYTHONPATH'] = os.pathsep.join(paths)
    # Raw updater output may contain credentials: discard it entirely. The
    # canonical owner establishes the group/suspended job before code executes.
    process = exchange = None
    try:
        if time.monotonic() >= deadline:
            raise TimeoutError('update deadline exceeded')
        try:
            process = _start_process(_worker_argv(item, ledger, budget), env=env, cwd=home,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        except _ProcessStartError as error:
            raise OSError('cache worker start cleanup unconfirmed') from error
        exchange = _ProcessExchange(process, None)
        exchange.run(process, max(0, deadline - time.monotonic()))
        return {'returncode': process.returncode, 'validated': process.returncode == 0}
    finally:
        if process is not None:
            _stop(process, exchange=exchange)


def _validate(item, directory):
    metadata = _read_metadata(item, directory)
    _required_cache_files(item, directory, metadata)
    name = item['dataset_id']
    if name == 'opendata_refresh':
        import zipfile
        files = list(directory.glob('**/*.zip'))
        if not files or not metadata:
            raise ValueError('missing open data archives')
        for file in files:
            with zipfile.ZipFile(file) as archive:
                if not archive.namelist() or archive.testzip() is not None:
                    raise ValueError('invalid open data archive')
        return
    if name == 'sanctions':
        module = importlib.import_module('sanctions_check')
        for source, descriptor in module.SOURCES.items():
            parser = {'рфм': module._entries_rfm, 'ofac': module._entries_ofac, 'eu': module._entries_eu}[source]
            _, state, _ = parser(str(directory / Path(descriptor['file']).name))
            if state != module.ПРОВЕРЕНО:
                raise ValueError('invalid sanctions schema')
        return
    if name == 'cbr_warning':
        import hashlib
        module = importlib.import_module('cbr_warning')
        body = directory / metadata['файл']
        if body.parent.resolve() != directory.resolve():
            raise ValueError('invalid body path')
        data = body.read_bytes()
        if hashlib.sha256(data).hexdigest() != metadata['sha256']:
            raise ValueError('body digest mismatch')
        module.разобрать(data)
        return
    required = {'cbr_registries': {'реестры', 'по_инн'}, 'disq_dump': {'записи'},
                'rzn_licenses': {'наборы', 'по_инн'}, 'rospatent_tz': {'по_инн', 'дата_выгрузки'},
                'fts_registries': {'реестры', 'по_инн'}, 'rkn_registries': {'реестры', 'по_инн'},
                'rar_licenses': {'по_инн'}, 'fsa_registries': {'охват', 'по_инн'},
                'minpromtorg': {'наборы'}, 'uc_mintsifry': {'перечни'},
                'erknm': {'папка', 'охват', 'скачан'}}[name]
    if not isinstance(metadata, dict) or not required <= metadata.keys():
        raise ValueError('invalid index schema')
    _freshness(item, metadata, dt.datetime.now(dt.timezone.utc))
    if name == 'erknm':
        folder = directory / metadata['папка']
        if folder.parent.resolve() != directory.resolve() or not (folder / 'словарь.json.gz').is_file():
            raise ValueError('missing ERKNM generation')
        for filename in ['словарь.json.gz'] + ['%02x.json.gz' % i for i in range(256)]:
            with gzip.open(folder / filename, 'rt', encoding='utf-8') as stream:
                if not isinstance(json.load(stream), dict):
                    raise TypeError('invalid ERKNM shard')


def _atomic_manifest(base, manifest):
    fd, temporary = tempfile.mkstemp(dir=base, prefix='.workflow-manifest-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(manifest, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, base / _MANIFEST)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def execute(dataset_ids, *, profile, max_bytes, timeout_seconds, root=None):
    _profile(profile)
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
        raise ValueError('max_bytes must be a positive integer')
    if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or not 0 < timeout_seconds < float('inf'):
        raise ValueError('timeout_seconds must be positive and finite')
    if isinstance(dataset_ids, str) or not dataset_ids:
        raise ValueError('explicit dataset ids required')
    items = [_item(name) for name in dataset_ids]
    if len({x['dataset_id'] for x in items}) != len(items):
        raise ValueError('duplicate dataset ids')
    for item in items:
        if not item['enabled'] or item['applicable_profiles'] != '*' and profile not in item['applicable_profiles']:
            raise ValueError('dataset disabled or not applicable')
    base = cache_root(root).resolve()
    started = time.monotonic()
    deadline = started + timeout_seconds
    report = {'schema_version': 1, 'profile': profile, 'datasets': [], 'bytes_downloaded': 0,
              'elapsed_seconds': 0, 'preserved_previous': True, 'errors': []}
    try:
        with _writer_lock(base):
            for item in items:
                stage = None
                promoted = False
                ledger = None
                used = 0
                try:
                    if time.monotonic() >= deadline or report['bytes_downloaded'] >= max_bytes:
                        raise TimeoutError('budget exhausted')
                    generations = base / '.workflow-generations' / item['dataset_id']
                    if not generations.resolve().is_relative_to(base):
                        raise ValueError('generation directory escaped cache root')
                    generations.mkdir(parents=True, exist_ok=True)
                    stage = generations / uuid.uuid4().hex
                    home = stage / 'home'
                    home.mkdir(parents=True)
                    ledger = stage / 'bytes.json'
                    worker_item = {**item, '_previous_root': str(dataset_root(item['dataset_id'], root=base))}
                    worker = _run_worker(worker_item, home, ledger, max_bytes - report['bytes_downloaded'], deadline)
                    if ledger.exists():
                        used = json.loads(ledger.read_text(encoding='utf-8'))['bytes_downloaded']
                    report['bytes_downloaded'] += used
                    if worker['returncode'] != 0:
                        raise ValueError('updater failed; staging discarded')
                    if report['bytes_downloaded'] > max_bytes:
                        raise ValueError('download byte budget exceeded')
                    if time.monotonic() >= deadline:
                        raise TimeoutError('update deadline exceeded')
                    payload = home / '.cache' / 'inn-check-ru'
                    destination = payload / item['cache_subdir'] if item['cache_subdir'] else payload
                    if not worker.get('validated'):
                        _validate(item, destination)
                    if time.monotonic() >= deadline:
                        raise TimeoutError('validation deadline exceeded')
                    manifest = _manifest(base)
                    manifest['datasets'][item['dataset_id']] = str(destination.relative_to(base))
                    _atomic_manifest(base, manifest)
                    promoted = True
                    report['datasets'].append({'dataset_id': item['dataset_id'], 'state': 'updated',
                                               'bytes_downloaded': used})
                except (OSError, ValueError, TypeError, KeyError, EOFError, TimeoutError, subprocess.SubprocessError) as error:
                    # Count bytes already downloaded even if the child timed out.
                    if used == 0 and ledger is not None and ledger.exists():
                        try:
                            used = json.loads(ledger.read_text(encoding='utf-8'))['bytes_downloaded']
                            report['bytes_downloaded'] += used
                        except (OSError, ValueError, KeyError):
                            pass
                    failure = {'dataset_id': item['dataset_id'], 'state': 'failed',
                               'reason_code': type(error).__name__, 'preserved_previous': True}
                    report['datasets'].append(failure)
                    report['errors'].append(failure.copy())
                finally:
                    if stage is not None and not promoted:
                        shutil.rmtree(stage, ignore_errors=True)
    except OSError as error:
        report['errors'].append({'reason_code': 'writer_busy' if isinstance(error, BlockingIOError) else type(error).__name__})
    report['elapsed_seconds'] = round(time.monotonic() - started, 6)
    if any(row['state'] == 'updated' for row in report['datasets']):
        configure_readers(root=base)
    return redact(report)
