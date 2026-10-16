"""Explicit host-owned provider permissions, atomic budgets and export projection.

Cost units are operator-supplied accounting units, never inferred prices or rights.
No network, dynamic code or page-controlled configuration is implemented here.
"""
from __future__ import annotations

import copy
import datetime
import json
import os
import pathlib
import threading

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


_FIELDS = {'name', 'address', 'недостоверность_сведений', 'недостоверность',
           'дисквалификация_руководителя', 'дисквалификация', 'graph'}
SUPPORTED = {'checko': frozenset(_FIELDS), 'dadata': frozenset({'name', 'address', 'недостоверность_сведений'})}
_FALLBACK = {'quota_exhausted', 'unavailable', 'timeout'}
_LOCK = threading.RLock()  # protects every ledger, including first initialization


def validate_policy(policy: dict) -> list[dict]:
    errors = []
    def bad(path, code='invalid_policy'):
        errors.append({'path': path, 'code': code})
    def integer(value):
        return type(value) is int and 0 <= value <= 10**12
    if not isinstance(policy, dict):
        return [{'path': '$', 'code': 'invalid_policy'}]
    if set(policy) != {'schema_version', 'total_cost_units', 'providers'}:
        bad('$')
    if type(policy.get('schema_version')) is not int or policy.get('schema_version') != 1:
        bad('schema_version', 'unsupported_schema')
    if not integer(policy.get('total_cost_units')):
        bad('total_cost_units')
    providers = policy.get('providers')
    if not isinstance(providers, list) or not 1 <= len(providers) <= 2:
        bad('providers'); return errors
    seen = set()
    required = {'id', 'enabled', 'allowed_fields', 'export_fields', 'request_limit', 'request_cost_units', 'fallback_on'}
    for index, entry in enumerate(providers):
        path = 'providers[%d]' % index
        if not isinstance(entry, dict):
            bad(path); continue
        if not required <= set(entry) or set(entry) - required - {'transient_retries'}:
            bad(path)
        pid = entry.get('id')
        if not isinstance(pid, str) or pid not in SUPPORTED or pid in seen:
            bad(path + '.id'); continue
        seen.add(pid)
        if type(entry.get('enabled')) is not bool: bad(path + '.enabled')
        for field in ('request_limit', 'request_cost_units'):
            if not integer(entry.get(field)): bad(path + '.' + field)
        retry = entry.get('transient_retries', 0)
        if type(retry) is not int or retry not in (0, 1): bad(path + '.transient_retries')
        for key, choices in (('allowed_fields', SUPPORTED[pid]), ('export_fields', SUPPORTED[pid]), ('fallback_on', _FALLBACK)):
            values = entry.get(key)
            if (not isinstance(values, list) or any(not isinstance(v, str) or v not in choices for v in values)
                    or len(values) != len({v for v in values if isinstance(v, str)})):
                bad(path + '.' + key)
        allowed, exports = entry.get('allowed_fields'), entry.get('export_fields')
        if isinstance(allowed, list) and isinstance(exports, list) and any(v not in allowed for v in exports):
            bad(path + '.export_fields')
    return errors


def configured_policy():
    """Read only the host environment path, never accept a path from run/page data."""
    path = os.environ.get('INN_CHECK_PROVIDER_POLICY')
    if not path:
        return None
    try:
        p = pathlib.Path(path)
        if not p.is_absolute() or p.stat().st_size > 65536:
            raise ValueError()
        policy = json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise ValueError('invalid_provider_policy') from None
    if validate_policy(policy):
        raise ValueError('invalid_provider_policy')
    return policy


def _budget(ledger):
    return copy.deepcopy({key: ledger.get(key, {} if key == 'providers' else 0) for key in
        ('reserved_requests', 'used_requests', 'reserved_cost_units', 'used_cost_units', 'providers')})


def _reason(value):
    return {'http_429':'quota_exhausted', 'http_402':'quota_exhausted',
            'http_500':'unavailable', 'http_502':'unavailable', 'http_503':'unavailable',
            'http_504':'timeout'}.get(value, value if value in
            _FALLBACK | {'http_401','http_403','invalid_response','missing_key','not_found'} else 'invalid_response')


def fetch_with_policy(inn: str, fields: list[str], policy: dict, *, request, ledger: dict) -> dict:
    if validate_policy(policy):
        raise ValueError('invalid_provider_policy')
    policy = copy.deepcopy(policy)
    if (not isinstance(fields, list) or not fields or any(not isinstance(f, str) or f not in _FIELDS for f in fields)
            or len(fields) != len(set(fields))):
        raise ValueError('invalid_provider_fields')
    if not isinstance(ledger, dict):
        # Preserve the public validation error type for existing callers.
        raise ValueError('invalid_provider_ledger')  # noqa: TRY004
    attempts, transfers, limitations, data = [], [], [], None
    entries = {p['id']:p for p in policy['providers']}
    primary = 'checko' if entries.get('checko', {}).get('enabled') else 'dadata'
    def permitted(pid):
        p = entries.get(pid)
        return p and p['enabled'] and set(fields) <= set(p['allowed_fields']) and set(fields) <= SUPPORTED[pid]
    def dispatch(pid):
        p = entries[pid]; cost = p['request_cost_units']
        with _LOCK:
            fingerprint = json.dumps(policy, sort_keys=True, separators=(',', ':'))
            if ledger.get('_policy') not in (None, fingerprint): raise ValueError('ledger_policy_mismatch')
            ledger['_policy'] = fingerprint
            for key in ('reserved_requests','used_requests','reserved_cost_units','used_cost_units'):
                ledger.setdefault(key, 0)
            counts = ledger.setdefault('providers', {}).setdefault(pid, {'used_requests':0, 'reserved_requests':0})
            if (counts['used_requests'] + counts['reserved_requests'] >= p['request_limit'] or
                ledger['used_cost_units'] + ledger['reserved_cost_units'] + cost > policy['total_cost_units']):
                limitations.append('budget_exhausted'); return 'budget_exhausted', None, False
            counts['reserved_requests'] += 1
            ledger['reserved_requests'] += 1; ledger['reserved_cost_units'] += cost
        transfer = {'provider':pid, 'fields':list(fields), 'time':datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    'cost_units':cost}
        transfers.append(transfer)
        try:
            response = request(pid, inn, list(fields))
            response = _load_sibling('redaction').redact(response, secrets=tuple(
                os.environ.get(key, '') for key in ('CHECKO_API_KEY', 'DADATA_API_KEY')))
            if (not isinstance(response, dict) or type(response.get('requests_used')) is not int
                    or response.get('requests_used') != 1):
                reason, value = 'invalid_response', None
            elif response.get('status') == 'ok' and isinstance(response.get('data'), dict):
                # Schema projection also prevents adapter debug/auth/raw keys escaping.
                value = {f:copy.deepcopy(response['data'][f]) for f in fields if f in response['data']}
                reason = 'ok' if value else 'not_found'
                if not value: value = None
            else:
                reason, value = _reason(response.get('reason_code')), None
        except TimeoutError:
            reason, value = 'timeout', None
        except Exception as error:
            if type(error).__name__ == 'CaptureWriteError':
                raise
            reason, value = 'unavailable', None
        finally:
            with _LOCK:
                counts['reserved_requests'] -= 1; counts['used_requests'] += 1
                ledger['reserved_requests'] -= 1; ledger['used_requests'] += 1
                ledger['reserved_cost_units'] -= cost; ledger['used_cost_units'] += cost
        attempts.append({'provider':pid, 'reason_code':reason, 'requests_used':1, 'cost_units':cost})
        return reason, value, True
    if not permitted(primary):
        limitations.append('provider_or_fields_denied')
    else:
        reason, data, _ = dispatch(primary)
        p = entries[primary]
        if reason in {'unavailable','timeout'} and p.get('transient_retries', 0):
            retry_reason, retry_data, dispatched = dispatch(primary)
            # A reservation refusal is not a new provider response. Preserve
            # the last transport failure for an independently budgeted fallback.
            if dispatched:
                reason, data = retry_reason, retry_data
        if (data is None and primary == 'checko' and reason in p['fallback_on'] and permitted('dadata')):
            reason, data, _ = dispatch('dadata')
        if data is None and not limitations: limitations.append(reason)
    with _LOCK: budget = _budget(ledger)
    return {'status':'ok' if data is not None else 'partial', 'data':data, 'attempts':attempts,
            'budget':budget, 'transfers':transfers, 'limitations':limitations}


def captured_policy(payload):
    if not isinstance(payload, dict) or '_provider_policy' not in payload:
        return None
    marker = payload['_provider_policy']
    if (not isinstance(marker, dict) or type(marker.get('schema_version')) is not int
            or marker.get('schema_version') != 1 or validate_policy(marker.get('policy'))):
        raise ValueError('invalid_captured_provider_policy')
    return marker['policy']


def export_allowed(payload: dict, policy: dict) -> dict:
    """Closed export view: preserve local input; omit untracked derived copies.

    A provider's complete response exceeds its field allowlist even when every
    requested field is exportable. Raw bodies and untracked descendants cannot
    be safely licensed by field name. The conservative view does not re-score.
    """
    captured_policy(payload)  # Every public export entry point rejects malformed markers.
    if validate_policy(policy): raise ValueError('invalid_provider_policy')
    safe = {}
    block = payload.get('агрегатор') or payload.get('data') or {}
    pid = block.get('агрегатор') if isinstance(block, dict) else None
    if pid is None:
        attempts = payload.get('attempts') or []
        pid = attempts[-1].get('provider') if attempts and isinstance(attempts[-1], dict) else None
    p = next((p for p in policy['providers'] if p['id'] == pid), None)
    if p and isinstance(block, dict):
        for field in p['export_fields']:
            value = block.get(field)
            # Structured claims/raw/graph require field provenance throughout;
            # until available they are intentionally withheld, not guessed safe.
            if type(value) in (str, int, float, bool) or value is None and field in block:
                safe[field] = value
    return {'data':safe, '_export_policy':{'schema_version':1,
        'provider':pid if pid in SUPPORTED else None,
        'omissions':['raw_responses', 'untracked_nested_fields', 'derived_claims_and_decisions', 'run_metadata'],
        'reason_code':'provider_export_projection',
        'original_local_run_preserved':True,
        'limitation':'Only permitted primitive provider fields exported; no risk decision was recomputed.'}}
