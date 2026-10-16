"""Voluntary local workflow measurements; no telemetry or automatic enrolment.

A session_id identifies one task/installation attempt. Every distinct observed
attempt contributes to all_starts, including failed installations and dropoffs.
Use a fresh pseudonym for a repeated attempt. Outcomes are the latest recorded
event per attempt, not an earlier successful intermediate operation.

No event is written unless enabled is exactly True. summarize reads existing
local storage and returns aggregates only; neither API sends or exports data.
Schema version 1 is independent of the event allowlist (no extra stored fields).
"""
from __future__ import annotations

import json
import math
import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 1
EVENT_FIELDS = frozenset({'session_id', 'stage', 'active_seconds', 'waiting_seconds',
                          'manual_steps', 'outcome', 'reason_code'})
STAGES = frozenset({'install', 'doctor', 'cache_plan', 'run_start', 'manual', 'resume',
                    'document', 'snapshot', 'refresh', 'diff', 'events', 'replay',
                    'render', 'compare', 'finish'})
OUTCOMES = frozenset({'started', 'in_progress', 'completed', 'failed', 'abandoned',
                      'refused'})
REASON_CODES = frozenset({'none', 'user_stopped', 'missing_optional_dependency',
                          'network_unavailable', 'source_unavailable', 'permission_denied',
                          'invalid_input', 'storage_failure', 'timeout',
                          'unsupported_platform', 'installation_failed',
                          'extraction_failed', 'manual_selection_required', 'declined',
                          'unknown'})
MAX_DURATION_SECONDS = 31 * 24 * 60 * 60
MAX_MANUAL_STEPS = 100_000
MAX_STORAGE_BYTES = 16 * 1024 * 1024
MAX_EVENTS = 100_000
_SESSION = re.compile(r'(?:pilot-[0-9]{1,6}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-'
                      r'[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\Z')
_LOG_NAME = 'events.jsonl'


def _valid_event(event):
    if type(event) is not dict or set(event) != EVENT_FIELDS:
        return False
    session_id = event['session_id']
    if type(session_id) is not str or _SESSION.fullmatch(session_id) is None:
        return False
    for key, choices in (('stage', STAGES), ('outcome', OUTCOMES),
                         ('reason_code', REASON_CODES)):
        if type(event[key]) is not str or event[key] not in choices:
            return False
    for key in ('active_seconds', 'waiting_seconds'):
        value = event[key]
        # Exact builtin types exclude bool, custom objects, NaN and infinity.
        if type(value) not in (int, float) or not 0 <= value <= MAX_DURATION_SECONDS:
            return False
        if not math.isfinite(value):
            return False
    return type(event['manual_steps']) is int and 0 <= event['manual_steps'] <= MAX_MANUAL_STEPS


def _root_path(root):
    path = Path.home() / '.local' / 'share' / 'inn-check-ru' / 'metrics' if root is None else Path(root)
    if '..' in path.parts:
        raise OSError('unsafe_root')
    # Do not resolve: resolving would hide a symlink which must be refused.
    return Path(os.path.abspath(path))


@contextmanager
def _directory(path, *, create):
    """POSIX walks use anchored descriptors and O_NOFOLLOW at every component."""
    if os.name == 'nt':
        # Windows has no dir_fd API. Refuse symlinks AND junction/reparse points.
        current = Path(path.anchor)
        for part in path.parts[1:]:
            current /= part
            if create:
                try:
                    current.mkdir(mode=0o700)
                except FileExistsError:
                    pass
            info = current.lstat()
            if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                    or getattr(info, 'st_file_attributes', 0) & 0x400):
                raise OSError('unsafe_root')
        yield None
        return
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _locked_log(path, *, create):
    """The log itself is the lock; read never creates a lockfile or directory."""
    with _directory(path, create=create) as directory:
        flags = os.O_RDWR if create else os.O_RDONLY
        flags |= getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
        flags |= getattr(os, 'O_BINARY', 0)
        target = path / _LOG_NAME
        if os.name == 'nt':
            try:
                info = target.lstat()
            except FileNotFoundError:
                pass
            else:
                if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                    raise OSError('unsafe_log')
        name = target if directory is None else _LOG_NAME
        kwargs = {} if directory is None else {'dir_fd': directory}
        if create:
            try:
                descriptor = os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o600, **kwargs)
            except FileExistsError:
                descriptor = os.open(name, flags, **kwargs)
        else:
            descriptor = os.open(name, flags, **kwargs)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise OSError('unsafe_log')
            if os.name == 'nt':
                import msvcrt
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_LOCK if create else msvcrt.LK_RLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_EX if create else fcntl.LOCK_SH)
            try:
                yield descriptor
            finally:
                if os.name == 'nt':
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def record(event: dict, *, enabled: bool = False, root=None) -> dict:
    """Append one strictly validated event with explicit, per-call opt-in.

    Returns schema_version/status and an optional controlled error_code. Never
    echoes input, credentials, paths or exception messages, including on failure.
    Invalid input and disabled calls perform zero filesystem/network operations.
    """
    if enabled is False:
        return {'schema_version': SCHEMA_VERSION, 'status': 'disabled'}
    # Snapshot a builtin dict once: validation and serialization must observe
    # the same values if another caller thread subsequently mutates its dict.
    snapshot = event.copy() if type(event) is dict else None
    if enabled is not True or not _valid_event(snapshot):
        return {'schema_version': SCHEMA_VERSION, 'status': 'invalid_event',
                'error_code': 'invalid_event'}
    payload = (json.dumps(snapshot, sort_keys=True, separators=(',', ':'), allow_nan=False)
               + '\n').encode('utf-8')
    try:
        with _locked_log(_root_path(root), create=True) as descriptor:
            initial_size = os.fstat(descriptor).st_size
            if initial_size + len(payload) > MAX_STORAGE_BYTES:
                raise OSError('storage_limit')
            # Validate the same bounded log the reader accepts while holding
            # its exclusive lock. Never append an event that hides prior starts
            # behind an unreadable event count or pre-existing corruption.
            if len(_read_events(descriptor)) >= MAX_EVENTS:
                raise OSError('event_limit')
            os.lseek(descriptor, 0, os.SEEK_END)
            try:
                remaining = memoryview(payload)
                while remaining:
                    written = os.write(descriptor, remaining)
                    if written <= 0:
                        raise OSError('short_write')
                    remaining = remaining[written:]
                os.fsync(descriptor)
            except OSError:
                os.ftruncate(descriptor, initial_size)
                raise
    except (OSError, TypeError, ValueError, RecursionError):
        return {'schema_version': SCHEMA_VERSION, 'status': 'storage_error',
                'error_code': 'local_storage_unavailable'}
    return {'schema_version': SCHEMA_VERSION, 'status': 'recorded'}


def _summary(events=None, *, unavailable=False):
    if unavailable:
        return {'schema_version': SCHEMA_VERSION, 'status': 'storage_error',
                'observation_status': 'unavailable', 'event_count': None,
                'session_count': None, 'denominators': {'all_starts': None, 'all_events': None},
                'outcome_counts': None, 'reason_counts': None, 'stage_counts': None,
                'active_seconds_total': None, 'waiting_seconds_total': None,
                'manual_steps_total': None,
                'completion': {'numerator': None, 'denominator': None, 'rate': None},
                'empirical_acceptance': 'not_assessed'}
    events = events or []
    latest = {}
    stages = {}
    active = waiting = manual = 0
    for event in events:
        latest[event['session_id']] = event
        stage = event['stage']
        stages[stage] = stages.get(stage, 0) + 1
        active += event['active_seconds']
        waiting += event['waiting_seconds']
        manual += event['manual_steps']
    outcomes, reasons = {}, {}
    for event in latest.values():
        outcome, reason = event['outcome'], event['reason_code']
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        reasons[reason] = reasons.get(reason, 0) + 1
    completed = outcomes.get('completed', 0)
    return {'schema_version': SCHEMA_VERSION, 'status': 'ok' if events else 'empty',
            'observation_status': 'recorded_local_events' if events else 'no_observations',
            'event_count': len(events), 'session_count': len(latest),
            'denominators': {'all_starts': len(latest), 'all_events': len(events)},
            'outcome_counts': outcomes, 'reason_counts': reasons, 'stage_counts': stages,
            'active_seconds_total': active, 'waiting_seconds_total': waiting,
            'manual_steps_total': manual,
            'completion': {'numerator': completed, 'denominator': len(latest),
                           'rate': completed / len(latest) if latest else None},
            'empirical_acceptance': 'not_assessed'}


def _reject_constant(_value):
    raise ValueError('invalid_json_number')


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_field')
        result[key] = value
    return result


def _read_events(descriptor):
    """Validate a complete bounded log under the caller's held OS lock."""
    os.lseek(descriptor, 0, os.SEEK_SET)
    if os.fstat(descriptor).st_size > MAX_STORAGE_BYTES:
        raise ValueError('storage_limit')
    chunks, size = [], 0
    while True:
        chunk = os.read(descriptor, 64 * 1024)
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if size > MAX_STORAGE_BYTES:
            raise ValueError('storage_limit')
    raw = b''.join(chunks)
    if raw and not raw.endswith(b'\n'):
        raise ValueError('incomplete_log')
    lines = raw.decode('utf-8').splitlines()
    if len(lines) > MAX_EVENTS:
        raise ValueError('event_limit')
    events = []
    for line in lines:
        event = json.loads(line, parse_constant=_reject_constant,
                           object_pairs_hook=_unique_fields)
        if not _valid_event(event):
            raise ValueError('invalid_event')
        events.append(event)
    return events


def summarize(*, root=None) -> dict:
    """Read a local aggregate without creating storage or exposing session IDs.

    Durations/steps sum incremental stage measurements, never cumulative clocks.
    A missing/empty log has no observations and a null completion rate. Corrupt,
    oversized or unreadable storage has unavailable (null) counts, never partial
    totals which could silently exclude failed installations or abandonment.
    Observed completion does not attest human understanding or empirical validity.
    """
    try:
        with _locked_log(_root_path(root), create=False) as descriptor:
            events = _read_events(descriptor)
        return _summary(events)
    except FileNotFoundError:
        return _summary([])
    except (ValueError, UnicodeError, RecursionError):
        result = _summary(unavailable=True)
        result.update(status='invalid_storage', error_code='invalid_local_storage')
        return result
    except (OSError, TypeError):
        result = _summary(unavailable=True)
        result['error_code'] = 'local_storage_unavailable'
        return result
