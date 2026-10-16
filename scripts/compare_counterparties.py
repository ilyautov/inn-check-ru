"""Offline evidence comparison of 2–5 distinct subjects at a saved-time cutoff.

This is a projection of immutable evidence, never a scorer or a ranking. Read
operations use run_store's validated committed history and never write a run.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone

import evidence_contract
import run_store


class ComparisonError(ValueError):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def _instant(value):
    try:
        if not isinstance(value, str) or 'T' not in value:
            raise ValueError
        moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if moment.tzinfo is None:
            raise ValueError
        return moment.astimezone(timezone.utc)
    except (ValueError, TypeError):
        raise ComparisonError('invalid_as_of', 'Нужен момент ISO 8601 с часовым поясом.') from None


def _revision_at(run_id, cutoff, root):
    latest = run_store.read(run_id, root=root)
    # Revision order is authoritative; clock rollback must not skip an eligible
    # earlier revision. Every read verifies the committed chain, including HEAD.
    for number in range(latest['revision'], 0, -1):
        record = latest if number == latest['revision'] else run_store.read(run_id, revision=number, root=root)
        if _instant(record['created_at']) <= cutoff:
            return record
    raise ComparisonError('revision_unavailable', 'До указанной даты нет сохранённой ревизии.')


def _fact(claim, cutoff):
    fact = copy.deepcopy(claim)
    fact['evidence_id'] = fact.pop('claim_id')
    # Availability and fact time are separate. A future observation/publication
    # cannot be used just because it was manually placed in an earlier run.
    unavailable = any(claim.get(key) and _instant(claim[key]) > cutoff
                      for key in ('retrieved_at', 'first_observed_at', 'published_at', 'effective_at'))
    reason = 'future_evidence' if unavailable else None
    if not claim.get('first_observed_at') and not claim.get('retrieved_at'):
        reason = 'availability_unknown'
    if claim.get('valid_until') and _instant(claim['valid_until']) <= cutoff:
        reason = reason or 'stale'
    if reason:
        fact.update(knowledge='unknown', value=None, reason_code=reason)
    return fact


def compare(run_ids: list[str], *, as_of: str, root=None) -> dict:
    """Return an additive schema-1 evidence matrix or raise ComparisonError.

    Unknown, conflicts, applicability, original periods and source limitations
    remain visible. No financial model or resolver is invoked, even for stale
    evidence. Callers map ComparisonError/RunStoreError to their own envelope.
    """
    if not isinstance(run_ids, list) or not 2 <= len(run_ids) <= 5:
        raise ComparisonError('invalid_subject_count', 'Для сравнения нужны 2–5 компаний.')
    cutoff = _instant(as_of)
    if any(not isinstance(rid, str) for rid in run_ids):
        raise ComparisonError('invalid_run_id', 'Нужны идентификаторы сохранённых проверок.')
    if len(set(run_ids)) != len(run_ids):
        raise ComparisonError('duplicate_subject', 'Повторная компания в сравнении.')
    records = [_revision_at(rid, cutoff, root) for rid in run_ids]
    if len({r['subject_inn'] for r in records}) != len(records):
        raise ComparisonError('duplicate_subject', 'Повторная компания в сравнении.')
    profiles = {r['profile'] for r in records}
    if len(profiles) != 1 or not next(iter(profiles)):
        raise ComparisonError('profile_mismatch', 'Нужны проверки с одной явно заданной целью.')
    subjects, by_subject, limitations = [], {}, []
    fields = set()
    for record in records:
        inn = record['subject_inn']
        fetch = record['payload']['fetch']
        # Comparison is an output surface: captured policies currently exclude
        # derived claims. Refuse rather than leak them through a new adapter.
        import provider_policy
        try:
            captured = provider_policy.captured_policy(fetch)
        except ValueError:
            raise ComparisonError('invalid_captured_provider_policy',
                                  'Сохранённая политика провайдера не прошла проверку.') from None
        if captured is not None:
            raise ComparisonError('export_restricted', 'Политика провайдера запрещает экспорт производных доказательств для сравнения.')
        evidence = fetch.get('_evidence')
        grouped = {}
        if evidence is None:
            limitations.append('%s: legacy_untraced — ссылки на факты отсутствуют.' % inn)
        else:
            if evidence_contract.validate_evidence(fetch):
                raise ComparisonError('invalid_evidence', 'Сохранённые ссылки на доказательства не прошли проверку.')
            visible = {cid: _fact(claim, cutoff) for cid, claim in evidence['claims'].items()}
            # A derived fact cannot bypass a cutoff through future/stale inputs.
            for _ in range(len(visible)):
                changed = False
                for fact in visible.values():
                    if fact['knowledge'] == 'known' and any(visible[cid]['knowledge'] in {'unknown', 'conflict'} for cid in fact['depends_on']):
                        fact.update(knowledge='unknown', value=None, reason_code='dependency_unavailable')
                        changed = True
                if not changed:
                    break
            for fact in visible.values():
                grouped.setdefault(fact['field'], []).append(fact)
                fields.add(fact['field'])
        by_subject[inn] = grouped
        subjects.append({'subject_inn': inn, 'run_id': record['run_id'],
                         'revision': record['revision'], 'saved_at': record['created_at'],
                         'evaluated_at': record['payload']['metadata'].get('evaluated_at'),
                         'workflow_status': record['workflow_status']})
    criteria, coverage = [], []
    for field in sorted(fields):
        cells, evidence_ids, periods = {}, {}, []
        for subject in subjects:
            inn = subject['subject_inn']
            facts = sorted(by_subject[inn].get(field, []), key=lambda c: c['evidence_id'])
            states = {c['knowledge'] for c in facts}
            state = ('conflict' if 'conflict' in states else 'unknown' if not facts or 'unknown' in states
                     else 'not_applicable' if states == {'not_applicable'} else 'known')
            ids = [f['evidence_id'] for f in facts]
            cells[inn] = {'state': state, 'facts': facts, 'evidence_ids': ids,
                          'reason_code': 'missing_evidence' if not facts else None}
            evidence_ids[inn] = ids
            periods.append({str(f.get('period')) for f in facts if f['knowledge'] == 'known'})
        same_periods = bool(periods) and all(p and 'None' not in p and p == periods[0] for p in periods)
        criteria.append({'field': field, 'subjects': cells, 'evidence_ids': evidence_ids,
                         'favourable_states': [], 'periods_comparable': same_periods})
        state_map = {inn: cell['state'] for inn, cell in cells.items()}
        if len(set(state_map.values())) > 1 or not same_periods:
            coverage.append({'field': field, 'states': state_map, 'periods_comparable': same_periods})
    limitations += [
        'Сравнение сохранённых фактов на один момент; даты и периоды самих фактов могут различаться.',
        'Неизвестное и неполное покрытие не являются благоприятными признаками. Общий рейтинг не рассчитывается.',
        'Сохранённые решения не пересчитываются; актуальные сведения требуют отдельного явного обновления.',
    ]
    if any(f.get('raw_ref', {}).get('availability') != 'available'
           for grouped in by_subject.values() for facts in grouped.values() for f in facts):
        limitations.append('Часть фактов не имеет доступного сырого ответа; доказательства не дополнены вымышленными ссылками.')
    return {'schema_version': 1, 'profile': next(iter(profiles)),
            'as_of': cutoff.isoformat().replace('+00:00', 'Z'), 'subjects': subjects,
            'criteria': criteria, 'coverage_differences': coverage, 'limitations': limitations}
