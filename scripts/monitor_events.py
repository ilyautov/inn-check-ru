"""Local durable monitoring outbox; at-least-once leases, explicit receiver ack.

The local filesystem and run_store's anchored I/O/OS locks are the trust boundary.
Listing never writes. Claim records delivery to a consumer, never its side effect.
"""
from __future__ import annotations

import copy
import datetime as dt
import os
import re
import sys
from pathlib import Path

import run_store as store
from redaction import redact

KINDS = {'company_changed', 'coverage_lost', 'coverage_restored', 'refresh_failed'}
STATES = {'pending', 'delivered', 'acknowledged', 'dead_letter'}
MAX_ATTEMPTS = 5
BACKOFF_SECONDS = 30
MAX_BACKOFF_SECONDS = 3600
MAX_SUBJECTS = 50
MAX_GRAPH_NODES = 25
FETCH_TIMEOUT_SECONDS = 120
_FILE = 'events.json'


class EventError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def _invalid():
    raise EventError('invalid_arguments', 'Некорректные аргументы очереди событий.')


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _iso(value):
    return value.astimezone(dt.timezone.utc).isoformat().replace('+00:00', 'Z')


def _time(value):
    return dt.datetime.fromisoformat(store._timestamp(value).replace('Z', '+00:00'))


def _base(root):
    return Path(root).expanduser() if root is not None else Path(os.environ.get(
        'INN_CHECK_EVENTS_DIR', str(Path.home() / '.local/share/inn-check-ru/events'))).expanduser()


def _hash(value):
    return store._sha(store._json(value))


def _consumer(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', value):
        _invalid()
    return value


def _limit(value):
    if type(value) is not int or not 1 <= value <= MAX_SUBJECTS:
        _invalid()


def _ids(values):
    if not isinstance(values, list) or not 1 <= len(values) <= MAX_SUBJECTS:
        _invalid()
    if any(not isinstance(x, str) or not re.fullmatch(r'[0-9a-f]{64}', x) for x in values):
        _invalid()
    return list(dict.fromkeys(values))


def _effective(delivery, now):
    if delivery['state'] == 'delivered' and _time(delivery['lease_expires_at']) <= now:
        return 'dead_letter' if delivery['attempts'] >= MAX_ATTEMPTS else 'pending'
    return delivery['state']


def _identity(event):
    return {key: event[key] for key in ('schema_version', 'subject_inn', 'profile',
            'from_revision', 'to_revision', 'kind', 'change_digest', 'origin')}


def _load(directory):
    if directory.info(_FILE) is None:
        return {'schema_version': 1, 'revision': 0, 'events': {}}
    value = store._parse(directory.read(_FILE))
    try:
        if (type(value['schema_version']) is not int or value['schema_version'] != 1
                or type(value['revision']) is not int or value['revision'] <= 0):
            store._fail('corrupt_store')
        if not isinstance(value['events'], dict):
            store._fail('corrupt_store')
        for event_id, event in value['events'].items():
            if event['event_id'] != event_id or event_id != _hash(_identity(event)):
                store._fail('corrupt_store')
            if event['change_digest'] != _hash(event['data']) or event['kind'] not in KINDS:
                store._fail('corrupt_store')
            if event['delivery']['state'] not in STATES:
                store._fail('corrupt_store')
            if (type(event['delivery']['attempts']) is not int
                    or not 0 <= event['delivery']['attempts'] <= MAX_ATTEMPTS):
                store._fail('corrupt_store')
            _time(event['observed_at'])
            delivery = event['delivery']
            if delivery['state'] == 'delivered':
                _consumer(delivery['consumer_id']); _time(delivery['lease_expires_at'])
            if delivery['next_retry_at'] is not None: _time(delivery['next_retry_at'])
    except (KeyError, TypeError, ValueError, EventError):
        store._fail('corrupt_store')
    return value


def _write(directory, queue):
    queue['revision'] += 1
    store._atomic_write(directory, _FILE, store._json(queue))


def _view(event, now):
    event = copy.deepcopy(event)
    event['delivery']['state'] = _effective(event['delivery'], now)
    return event


def _origin(row, observed):
    origin = row.get('origin')
    if origin is None:
        return {'kind': 'legacy_report', 'from_run_id': None, 'to_run_id': None,
                'from_sha256': None, 'to_sha256': None, 'report_observed_at': observed}
    if not isinstance(origin, dict) or origin.get('kind') != 'saved_run':
        _invalid()
    origin = copy.deepcopy(origin)
    for prefix in ('from', 'to'):
        run_id, digest = origin.get(prefix + '_run_id'), origin.get(prefix + '_sha256')
        if run_id is not None: store._id(run_id)
        if digest is not None and (not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)): _invalid()
        if (run_id is None) != (digest is None): _invalid()
    if origin.get('to_run_id') is None: _invalid()
    return origin


def _build(report):
    store._json(report)
    report = redact(report)
    if not isinstance(report, dict) or not isinstance(report.get('результаты'), list): _invalid()
    # Legacy reports used naive local timestamps. Preserve their stated time with
    # the host local zone; never invent a run/revision for these reports.
    observed = report.get('observed_at', report.get('дата'))
    try:
        moment = dt.datetime.fromisoformat(observed.replace('Z', '+00:00'))
        if moment.tzinfo is None: moment = moment.astimezone()
        observed = _iso(moment)
    except (TypeError, ValueError, AttributeError): _invalid()
    result = []
    for row in report['результаты']:
        if not isinstance(row, dict): _invalid()
        inn = row.get('инн')
        if not isinstance(inn, str) or not re.fullmatch(r'(?:[0-9]{10}|[0-9]{12})', inn): _invalid()
        profile = row.get('профиль') or report.get('профиль') or 'нейтрально'
        if not isinstance(profile, str): _invalid()
        origin = _origin(row, observed)
        before, after = row.get('from_revision'), row.get('to_revision')
        for revision in (before, after):
            if revision is not None: store._revision(revision)
        if origin['kind'] == 'legacy_report': before = after = None
        elif after is None: _invalid()
        changes = row.get('изменения', [])
        if not isinstance(changes, list) or any(not isinstance(x, dict) for x in changes): _invalid()
        grouped = {}
        for change in changes:
            field = change.get('поле', '')
            kind = 'company_changed'
            if isinstance(field, str) and field.startswith('источник.'):
                if change.get('стало') == 'не проверено': kind = 'coverage_lost'
                elif change.get('было') == 'не проверено' and change.get('стало') in ('ok', 'пусто'): kind = 'coverage_restored'
            grouped.setdefault(kind, []).append(change)
        coverage_changes = row.get('coverage_changes', [])
        if not isinstance(coverage_changes, list): _invalid()
        for change in coverage_changes:
            if not isinstance(change, dict) or change.get('kind') not in {'coverage_lost', 'coverage_restored'}:
                _invalid()
            grouped.setdefault(change['kind'], []).append(change)
        if row.get('refresh_failed', row.get('статус') == 'не проверено'):
            grouped['refresh_failed'] = {'reason': row.get('причина'),
                'unavailable_sources': row.get('не_ответили_deal_killer', [])}
        for kind, data in sorted(grouped.items()):
            event = {'schema_version': 1, 'subject_inn': inn, 'profile': profile,
                     'from_revision': before, 'to_revision': after, 'kind': kind,
                     'change_digest': _hash(data), 'observed_at': observed, 'data': data,
                     'origin': origin}
            event['event_id'] = _hash(_identity(event))
            event['delivery'] = {'state': 'pending', 'consumer_id': None, 'attempts': 0,
                'delivered_at': None, 'acknowledged_at': None, 'lease_expires_at': None,
                'next_retry_at': None, 'max_attempts': MAX_ATTEMPTS,
                'backoff_seconds': BACKOFF_SECONDS, 'max_backoff_seconds': MAX_BACKOFF_SECONDS}
            result.append(event)
    return result


def ingest(report: dict, *, root=None) -> list[dict]:
    candidates = list({event['event_id']: event for event in _build(report)}.values())
    if not candidates: return []
    with store._errors(), store._root(_base(root), create=True) as directory, store._lock(directory):
        queue = _load(directory)
        changed = False
        for event in candidates:
            if event['event_id'] not in queue['events']:
                queue['events'][event['event_id']] = event
                changed = True
        if changed: _write(directory, queue)
        return [copy.deepcopy(queue['events'][event['event_id']]) for event in candidates]


def list_events(*, state='pending', limit=50, root=None) -> list[dict]:
    _limit(limit)
    if state not in STATES | {'all'}: _invalid()
    now = _now()
    try:
        with store._errors(), store._root(_base(root)) as directory:
            queue = _load(directory)
    except store.RunStoreError as error:
        if error.code == 'not_found': return []
        raise
    return [_view(event, now) for event in queue['events'].values()
            if state == 'all' or _effective(event['delivery'], now) == state][:limit]


def claim_events(consumer_id, *, limit=50, lease_seconds=300, root=None) -> list[dict]:
    _consumer(consumer_id); _limit(limit)
    if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600: _invalid()
    claimed = []
    with store._errors(), store._root(_base(root), create=True) as directory, store._lock(directory):
        now = _now()
        queue = _load(directory)
        for event in queue['events'].values():
            delivery = event['delivery']
            if _effective(delivery, now) != 'pending': continue
            if delivery['next_retry_at'] and _time(delivery['next_retry_at']) > now: continue
            delivery.update(state='delivered', consumer_id=consumer_id, attempts=delivery['attempts'] + 1,
                            delivered_at=_iso(now), lease_expires_at=_iso(now + dt.timedelta(seconds=lease_seconds)),
                            next_retry_at=None)
            claimed.append(copy.deepcopy(event))
            if len(claimed) == limit: break
        if claimed: _write(directory, queue)
    return claimed


def _receipt(event_ids, consumer_id, root, *, retry):
    ids = _ids(event_ids); _consumer(consumer_id)
    with store._errors(), store._root(_base(root)) as directory, store._lock(directory):
        now = _now()
        queue = _load(directory)
        selected = []
        for event_id in ids:
            event = queue['events'].get(event_id)
            if event is None: raise EventError('not_found', 'Событие не найдено.')
            delivery = event['delivery']
            if delivery['consumer_id'] != consumer_id:
                raise EventError('lease_conflict', 'Событие принадлежит другому потребителю.')
            if not retry and delivery['state'] == 'acknowledged':
                selected.append(event); continue
            if _effective(delivery, now) != 'delivered':
                raise EventError('lease_conflict', 'Аренда события истекла или не активна.')
            selected.append(event)
        changed = False
        for event in selected:
            delivery = event['delivery']
            if not retry and delivery['state'] == 'acknowledged': continue
            changed = True
            if retry:
                terminal = delivery['attempts'] >= MAX_ATTEMPTS
                delay = min(MAX_BACKOFF_SECONDS, BACKOFF_SECONDS * 2 ** (delivery['attempts'] - 1))
                delivery.update(state='dead_letter' if terminal else 'pending', lease_expires_at=None,
                                next_retry_at=None if terminal else _iso(now + dt.timedelta(seconds=delay)))
            else:
                delivery.update(state='acknowledged', acknowledged_at=_iso(now), lease_expires_at=None)
        if changed: _write(directory, queue)
        return copy.deepcopy(selected)


def ack_events(event_ids, *, consumer_id, root=None) -> list[dict]:
    return _receipt(event_ids, consumer_id, root, retry=False)


def retry_events(event_ids, *, consumer_id, root=None) -> list[dict]:
    return _receipt(event_ids, consumer_id, root, retry=True)


def host_plan(profile: str, *, now: str, platform: str, root=None) -> dict:
    import cache_policy
    import schedule_templates
    moment = _time(now)
    if platform not in {'linux', 'macos', 'windows'}: _invalid()
    # root is the host workspace. Fixed subpaths cannot originate from page data.
    base = Path(root).expanduser().absolute() if root is not None else Path.home() / '.local/share/inn-check-ru'
    if '..' in base.parts: _invalid()
    inventory = cache_policy.plan(profile, now=_iso(moment), root=None)
    dates = [_time(x['next_due_at']) for x in inventory['datasets'] if x.get('applicable') and x.get('next_due_at')]
    next_run = moment if inventory['minimal_actions'] else min(dates, default=moment + dt.timedelta(days=1))
    next_run = max(moment, min(next_run, moment + dt.timedelta(days=1)))
    # Calendar schedulers have minute precision. Round up rather than schedule
    # an instant already passed in the current minute.
    if next_run.second or next_run.microsecond:
        next_run = next_run.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
    argv = [sys.executable, str(Path(__file__).resolve().with_name('workflow_cli.py')), 'watch', 'monitor',
            '--profile', profile, '--store', str(base), '--max-subjects', str(MAX_SUBJECTS), '--json']
    timeout = MAX_SUBJECTS * FETCH_TIMEOUT_SECONDS + 60
    templates = schedule_templates.render(argv, interval_days=1, hour=next_run.hour, minute=next_run.minute,
                 label='tech.aifrontier.inn-check-ru.monitor', start_at=_iso(next_run), timeout_seconds=timeout)
    return {**templates, 'platform': platform, 'profile': profile, 'next_run_at': _iso(next_run),
            'timeout_seconds': timeout, 'max_subjects': MAX_SUBJECTS,
            'limits': {'fetch_timeout_seconds': FETCH_TIMEOUT_SECONDS, 'max_subjects': MAX_SUBJECTS,
                       'max_graph_nodes': MAX_GRAPH_NODES, 'max_attempts': MAX_ATTEMPTS},
            'event_output': str(base / 'events' / _FILE), 'watchlist_file': str(base / 'watchlist.json'),
            'cache_plan': inventory, 'task_xml': templates['windows_xml'],
            'schedule_timezone': 'UTC; cron/launchd hour converted to local host timezone',
            'installed': False}
