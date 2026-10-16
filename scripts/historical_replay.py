"""Read-only replay of committed revisions; no acquisition or store mutations.

Original mode returns the complete saved materialized payload and manifest.
Current-rules mode creates an ephemeral, explicitly non-original artifact from
eligible saved leaves. Reporting periods never establish public availability.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timezone

import evidence_contract
import run_store
from redaction import redact

_TIMESTAMP = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])\Z')


class ReplayError(ValueError):
    """Safe, stable adapter boundary without caller-controlled diagnostics."""
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def _instant(value):
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def _select(run_id, cutoff, root):
    # Public reads verify the complete committed chain. Orphans never qualify.
    record = run_store.read(run_id, root=root)
    while _instant(record['created_at']) is None or _instant(record['created_at']) > cutoff:
        revision = record['parent_revision']
        if revision is None:
            raise ReplayError('no_revision_at_cutoff', 'До указанного момента нет сохранённой ревизии проверки.')
        record = run_store.read(run_id, revision=revision, root=root)
    return record


def _eligibility(payload, cutoff):
    fetch, metadata = payload['fetch'], payload['metadata']
    evidence = fetch.get('_evidence')
    if not isinstance(evidence, dict) or evidence_contract.validate_evidence(fetch):
        return {}, [{'claim_id': None, 'field': None, 'reason_code': 'unverified_evidence'}], 'unverified'
    semantics = metadata.get('publication_semantics', {})
    semantics = semantics if isinstance(semantics, dict) else {}
    eligible, exclusions = {}, []
    for cid, claim in evidence['claims'].items():
        # Derived observations inherit eligibility only by recomputation from leaves.
        if claim['field'].startswith('fin.') or claim['relation'] == 'derived':
            continue
        if claim['value'] is None:
            continue
        observed, published = _instant(claim['first_observed_at']), _instant(claim['published_at'])
        publication_available = evidence_contract._publication_available(claim, cutoff, semantics)
        if observed is not None and observed <= cutoff or publication_available:
            eligible[cid] = copy.deepcopy(claim)
        else:
            publication_confirmed = published is not None and evidence_contract._publication_available(claim, published, semantics)
            reason = 'observed_after_cutoff' if observed and observed > cutoff else 'publication_after_cutoff' if publication_confirmed and published > cutoff else 'edition_availability_unknown'
            exclusions.append({'claim_id': cid, 'field': claim['field'], 'reason_code': reason})
    status = 'verified' if eligible and not exclusions else 'partial' if eligible else 'unverified'
    return eligible, exclusions, status


def _put(fetch, field, value):
    pieces = field.split('.')
    if pieces[:2] == ['финансы', 'отчётность_по_годам'] and len(pieces) == 5:
        year = pieces[2]
        if not re.fullmatch(r'[0-9]{4}', year):
            return
        rows = fetch.setdefault('финансы', {}).setdefault('отчётность_по_годам', [])
        row = next((r for r in rows if str(r['год']) == year), None)
        if row is None:
            row = {'год': int(year), 'строки': {}}
            rows.append(row)
        row['строки'][pieces[4]] = copy.deepcopy(value)
        return
    current = fetch
    for key in pieces[:-1]:
        current = current.setdefault(key, {})
        if not isinstance(current, dict):
            raise ReplayError('invalid_replay_evidence', 'Пути сохранённых фактов несовместимы.')
    current[pieces[-1]] = copy.deepcopy(value)


def _filtered_fetch(payload, eligible, cutoff):
    original = payload['fetch']
    # Build only from traced paths. Untracked ancillary blocks must not become
    # fresh affirmative facts just because evaluate adapts a legacy JSON again.
    fetch = {key: copy.deepcopy(original[key]) for key in
             ('инн', 'тип', 'профиль', '_доступность', '_provider_policy') if key in original}
    previous = original.get('_evidence')
    if not isinstance(previous, dict) or evidence_contract.validate_evidence(original):
        return fetch
    by_field = {}
    for claim in previous['claims'].values():
        if not claim['field'].startswith('fin.') and claim['relation'] != 'derived':
            _put(fetch, claim['field'], None)
    # Preserve the candidates and their raw hashes; the authoritative resolver
    # retains conflicts and unknown states. No new raw observation is invented.
    retained = {cid: claim for cid, claim in eligible.items() if claim['knowledge'] in {'known', 'conflict'}}
    for claim in retained.values():
        by_field.setdefault(claim['field'], []).append(claim)
        claim['depends_on'] = [dep for dep in claim['depends_on'] if dep in retained]
    for field, claims in by_field.items():
        _put(fetch, field, claims[0]['value'])
    prior = copy.deepcopy(previous)
    prior['claims'] = retained
    prior['conflicts'] = []  # enrich resolves retained candidate sets anew.
    fetch['_evidence'] = prior
    # Browser-source coverage needs its saved operational envelope as well as
    # raw-backed leaves. The selected revision predates cutoff; additionally,
    # require actual observation of ALL original non-null source leaves, not
    # just the surviving subset. Late publication must not borrow this context.
    source_claims = {}
    traced_fields = {claim['field'] for claim in previous['claims'].values()}
    for claim in previous['claims'].values():
        if claim['value'] is not None and not claim['field'].startswith('fin.') and claim['relation'] != 'derived':
            source_claims.setdefault(claim['source_id'], []).append(claim)
    for source, claims in source_claims.items():
        block = original.get(source)
        if not isinstance(block, dict) or not all(
                claim['claim_id'] in eligible and claim['raw_ref']['availability'] == 'available'
                and _instant(claim['first_observed_at']) is not None
                and _instant(claim['first_observed_at']) <= cutoff for claim in claims):
            continue
        for key in ('статус', 'дата_проверки', 'url', 'ввод'):
            # A status used as a factual claim (e.g. EGRUL status) must remain
            # governed by its own retained claim, never copied as metadata.
            if key in block and f'{source}.{key}' not in traced_fields:
                fetch.setdefault(source, {})[key] = copy.deepcopy(block[key])
    return fetch


def replay(run_id: str, *, mode: str, cutoff: str, root=None) -> dict:
    """Replay local immutable data. `root` is trusted host configuration only.

    `data` is {fetch, fin, assessment, metadata}; original mode never re-scores.
    The artifact returned for current_rules is not persisted/exported implicitly.
    """
    if mode not in ('original', 'current_rules'):
        raise ReplayError('invalid_replay_mode', 'Режим replay: original или current_rules.')
    instant = _instant(cutoff)
    if instant is None:
        raise ReplayError('invalid_cutoff', 'Ожидается ISO 8601 с временем, секундами и часовым поясом.')
    cutoff = instant.isoformat().replace('+00:00', 'Z')
    record = _select(run_id, instant, root)
    payload = record['payload']
    eligible, exclusions, status = _eligibility(payload, instant)
    original_rules = record['manifest']['versions'].get('rules_version')
    out = {'schema_version': 1, 'run_id': run_id, 'mode': mode,
           'source_revision': record['revision'], 'original_rules_version': original_rules,
           'applied_rules_version': original_rules, 'cutoff': cutoff,
           'exclusions': exclusions, 'point_in_time_status': status,
           'eligible_claim_ids': sorted(eligible), 'manifest': copy.deepcopy(record['manifest']),
           'data': copy.deepcopy(payload),
           'real_vintage_validation': {'status': 'awaiting_external_evidence', 'dated_pairs': 0}}
    if mode == 'original':
        return out
    filtered = _filtered_fetch(payload, eligible, instant)
    evaluated = evidence_contract.evaluate(filtered, profile=record['profile'] or 'нейтрально', now=cutoff,
                                          publication_semantics=payload['metadata'].get('publication_semantics'))
    # Retain collection provenance / captured policy markers; evaluation versions
    # and decision metadata come from the fresh pass, never the saved assessment.
    metadata = copy.deepcopy(payload['metadata'])
    metadata.update(evaluated['metadata'])
    metadata.update(replay_mode='current_rules', original_historical_outcome=False, cutoff=cutoff)
    evaluated['metadata'] = metadata
    evaluated = redact(evaluated)
    out['data'] = evaluated
    out['applied_rules_version'] = evaluated['metadata']['rules_version']
    out['artifact_manifest'] = {
        'schema_version': 1, 'kind': 'historical_replay', 'mode': mode,
        'original_historical_outcome': False, 'source_run_id': run_id,
        'source_revision': record['revision'], 'source_manifest_sha256': _digest(record['manifest']),
        'source_payload_sha256': record['manifest']['input_sha256'],
        'eligible_input_sha256': _digest(filtered), 'result_sha256': _digest(evaluated),
        'original_rules_version': original_rules, 'applied_rules_version': out['applied_rules_version'],
        'cutoff': cutoff, 'exclusions': copy.deepcopy(exclusions),
        'point_in_time_status': status}
    return out
