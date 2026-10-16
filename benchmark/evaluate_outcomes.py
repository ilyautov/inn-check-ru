"""Offline prospective-outcome validator. No collection, model fitting or tuning.

The JSON schemas define the accepted structure; semantic validation enforces temporal,
split and independent-reference contracts. Self-declared metadata is not an audit of
actual independence or authenticity. See outcome-study-protocol.md before use.
"""
import hashlib
import json
import math
import re
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

OUTCOMES = ('case_filed', 'procedure_opened', 'liquidation', 'client_loss')
BASE = Path(__file__).resolve().parent
SCHEMA = json.loads((BASE / 'outcome-cohort.schema.json').read_text())
REFERENCE_SCHEMA = json.loads((BASE / 'reference-review.schema.json').read_text())
_TIMESTAMP = re.compile(SCHEMA['$defs']['protocol']['properties']['frozen_at']['pattern'])


def _time(value):
    # fromisoformat accepts ISO basic forms and normalizes invalid offset minutes.
    # The schema's supported RFC3339 subset bounds every time component first;
    # datetime then checks calendar dates (including leap years).
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        raise ValueError('Expected supported RFC3339 timestamp')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Expected timezone')
    return result


def _schema_errors(value, schema, path=''):
    """Validate only the keywords used by the checked-in schemas (no network refs)."""
    if '$ref' in schema:
        ref = schema['$ref']
        target = REFERENCE_SCHEMA if ref == 'reference-review.schema.json' else SCHEMA['$defs'][ref.rsplit('/', 1)[-1]]
        return _schema_errors(value, target, path)
    if 'anyOf' in schema:
        if any(not _schema_errors(value, option, path) for option in schema['anyOf']):
            return []
        return [{'code': 'schema', 'path': path, 'reason': 'anyOf'}]
    errors = []
    def fail(reason):
        errors.append({'code': 'schema', 'path': path, 'reason': reason})
    checks = {'object': lambda v: isinstance(v, dict), 'array': lambda v: isinstance(v, list),
              'string': lambda v: isinstance(v, str), 'null': lambda v: v is None,
              'boolean': lambda v: type(v) is bool, 'integer': lambda v: type(v) is int,
              'number': lambda v: type(v) in (int, float) and math.isfinite(v)}
    types = schema.get('type', [])
    if isinstance(types, str): types = [types]
    if types and not any(checks[t](value) for t in types):
        fail('type'); return errors
    if 'const' in schema and (value != schema['const'] or type(value) is not type(schema['const'])):
        fail('const')
    if 'enum' in schema and value not in schema['enum']: fail('enum')
    if value is None: return errors
    if isinstance(value, dict):
        for key in schema.get('required', []):
            if key not in value: errors.append({'code': 'schema', 'path': f'{path}/{key}', 'reason': 'required'})
        props = schema.get('properties', {})
        for key, item in value.items():
            if key in props: errors.extend(_schema_errors(item, props[key], f'{path}/{key}'))
            elif schema.get('additionalProperties') is False: fail('additionalProperties')
    elif isinstance(value, list):
        if len(value) < schema.get('minItems', 0): fail('minItems')
        if len(value) > schema.get('maxItems', math.inf): fail('maxItems')
        if schema.get('uniqueItems') and len({json.dumps(v, sort_keys=True) for v in value}) != len(value): fail('uniqueItems')
        if 'items' in schema:
            for i, item in enumerate(value): errors.extend(_schema_errors(item, schema['items'], f'{path}/{i}'))
    elif isinstance(value, str):
        if len(value.strip()) < schema.get('minLength', 0): fail('minLength')
        if 'pattern' in schema and not re.search(schema['pattern'], value): fail('pattern')
        if schema.get('format') == 'date-time':
            try: _time(value)
            except ValueError: fail('date-time')
    elif type(value) in (float, int):
        if value < schema.get('minimum', -math.inf): fail('minimum')
        if value <= schema.get('exclusiveMinimum', -math.inf): fail('exclusiveMinimum')
        if value > schema.get('maximum', math.inf): fail('maximum')
    return errors


def validate_cohort(rows: list[dict], protocol: dict) -> list[dict]:
    """Return structural/semantic violations; missing oracle is an exclusion, not success."""
    errors = _schema_errors({'rows': rows, 'protocol': protocol}, SCHEMA)
    def fail(code, path='', **details):
        errors.append(dict(code=code, path=path, **details))

    # Diagnose overlap even for incomplete seed rows (public API example).
    subject_splits, group_splits = {}, {}
    if isinstance(protocol, dict) and isinstance(protocol.get('split'), dict):
        for split in ('train', 'test'):
            for kind, dest in [('subjects', subject_splits), ('groups', group_splits)]:
                values = protocol['split'].get(f'{split}_{kind}', [])
                if isinstance(values, list):
                    for value in values:
                        if isinstance(value, str): dest.setdefault(value, set()).add(split)
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict) or row.get('split') not in ('train', 'test'): continue
            for key, dest in [('inn', subject_splits), ('group_id', group_splits)]:
                if isinstance(row.get(key), str): dest.setdefault(row[key], set()).add(row['split'])
    for dest, code in [(subject_splits, 'subject_split_overlap'), (group_splits, 'group_split_overlap')]:
        for identity, splits in dest.items():
            if len(splits) > 1: fail(code, identity=identity)
    if any(e['code'] == 'schema' for e in errors): return errors

    frozen, analysis, read_at = (_time(protocol[k]) for k in ('frozen_at', 'analysis_at', 'outcomes_read_at'))
    if read_at < frozen: fail('outcomes_before_freeze')
    if analysis < read_at: fail('analysis_before_outcome_read')
    seen = set()
    for i, row in enumerate(rows):
        path = f'/rows/{i}'
        if row['inn'] in seen: fail('duplicate_subject', path)
        seen.add(row['inn'])
        split = row['split']
        if row['inn'] not in protocol['split'][f'{split}_subjects']: fail('unregistered_subject', path)
        if row['group_id'] not in protocol['split'][f'{split}_groups']: fail('unregistered_group', path)
        if (protocol['mode'] == 'real') == row['synthetic']:
            fail('synthetic_in_real_report' if row['synthetic'] else 'real_in_synthetic_report', path)
        for field in ('engine_hash', 'rules_hash'):
            if row[field] != protocol[field]: fail('freeze_hash_mismatch', path, field=field)
        decision, available = (_time(row[k]) for k in ('decision_at', 'data_available_at'))
        # Training records are provenance/split inventory only; not recomputed or fitted here.
        if split == 'train': continue
        if decision < frozen: fail('decision_before_freeze', path)
        if decision > read_at: fail('prediction_after_outcome_read', path)
        if available > decision: fail('leakage_cutoff', path)
        try: expected_horizon = decision + timedelta(days=protocol['horizon_days'])
        except OverflowError:
            fail('invalid_horizon', path); continue
        for kind, outcome in row['outcomes'].items():
            opath = f'{path}/outcomes/{kind}'
            horizon, observed = (_time(outcome[k]) for k in ('horizon_end', 'observed_until'))
            event = _time(outcome['event_at']) if outcome['event_at'] else None
            if horizon != expected_horizon: fail('horizon_mismatch', opath)
            if observed > analysis or observed < decision: fail('invalid_observation_time', opath)
            if outcome['status'] == 'event':
                if event is None or not decision < event <= min(horizon, observed): fail('invalid_event_time', opath)
            elif event is not None: fail('unexpected_event_time', opath)
            if outcome['status'] == 'no_event' and (observed < horizon or analysis < horizon): fail('censored_as_no_event', opath)
            ref = outcome['reference']
            if ref is None: continue
            if outcome['status'] not in ('event', 'no_event'):
                fail('unexpected_reference_label', opath); continue
            if ref['label'] != outcome['status']: fail('reference_label_mismatch', opath)
            if ref['subject_inn'] != row['inn'] or ref['outcome_type'] != kind: fail('reference_identity_mismatch', opath)
            if ref['rubric_hash'] != protocol['rubric_hash']: fail('rubric_hash_mismatch', opath)
            reviews = ref['reviews']
            ids = {r['reviewer_id'] for r in reviews}
            if len(ids) != 2: fail('independent_review_required', opath)
            evidence_time = max(_time(e['observed_at']) for e in ref['evidence'])
            label_time = event if outcome['status'] == 'event' else horizon
            if evidence_time > analysis or (label_time and evidence_time < label_time): fail('reference_time_invalid', opath)
            for review in reviews:
                if not max(evidence_time, read_at) <= _time(review['reviewed_at']) <= analysis: fail('review_time_invalid', opath)
            if len({r['label'] for r in reviews}) > 1:
                adj = ref['adjudication']
                if not adj or adj['reviewer_id'] in ids or adj['label'] != ref['label']:
                    fail('adjudication_required', opath)
                elif not max(_time(r['reviewed_at']) for r in reviews) <= _time(adj['reviewed_at']) <= analysis:
                    fail('adjudication_time_invalid', opath)
            elif reviews[0]['label'] != ref['label']:
                fail('reference_label_mismatch', opath)
            if outcome['status'] == 'no_event' and (not ref['negative_complete'] or _time(ref['scope_start']) > decision or _time(ref['scope_end']) < horizon):
                fail('negative_scope_incomplete', opath)
    return errors


def _wilson(k, n):
    z = 1.959963984540054
    p, z2 = k / n, z * z
    center = (p + z2 / (2*n)) / (1 + z2/n)
    half = z * math.sqrt(p*(1-p)/n + z2/(4*n*n)) / (1 + z2/n)
    return [max(0.0, center-half), min(1.0, center+half)]


def _metric(k, n, events, protocol, blockers, label):
    reasons = list(blockers)
    if not n: reasons.append('empty_denominator')
    if events < protocol['min_events']: reasons.append('insufficient_events')
    ci = _wilson(k, n) if n and not blockers else None
    if ci and ci[1] - ci[0] > protocol['target_ci_width']: reasons.append('target_ci_width_not_met')
    return {'label': label, 'numerator': k, 'denominator': n, 'event_count': events,
                'descriptive_rate': k/n if n else None,
                'estimate': k/n if n and not reasons else None,
                'ci95': ci if not reasons else None,
                'status': 'sufficient_under_registered_assumptions' if not reasons else 'insufficient_evidence',
                'reasons': reasons}


def evaluate_outcomes(rows: list[dict], protocol: dict) -> dict:
    """Report each outcome separately; invalid cohorts never produce risk estimates."""
    errors = validate_cohort(rows, protocol)
    if errors: return {'schema_version': 1, 'status': 'invalid_cohort', 'errors': errors, 'outcomes': {}}
    test = [r for r in rows if r['split'] == 'test']
    missing = set(protocol['split']['test_subjects']) - {r['inn'] for r in test}
    blockers = []
    if missing: blockers.append('missing_registered_subjects')
    if any(r['weight'] != 1 for r in test): blockers.append('nonunit_weights')
    if not protocol['sampling_frame']['independent_groups'] or len({r['group_id'] for r in test}) != len(test): blockers.append('dependent_groups')
    if protocol['sampling_frame']['method'] != 'simple_random': blockers.append('unsupported_sampling_method')
    attrition = Counter(r['followup_status'] for r in test if r['followup_status'] != 'retained')
    if attrition: blockers.append('attrition')
    result = {'schema_version': 1, 'study_id': protocol['study_id'], 'evidence_kind': protocol['mode'],
                  'status': 'insufficient_evidence', 'engine_hash': protocol['engine_hash'], 'rules_hash': protocol['rules_hash'],
                  'protocol_hash': hashlib.sha256(json.dumps(protocol,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest(),
                  'frozen_at': protocol['frozen_at'], 'analysis_at': protocol['analysis_at'], 'confidence_method': protocol['confidence_method'],
                  'counts': {'input_rows': len(rows), 'train_rows': len(rows)-len(test), 'test_rows': len(test),
                              'registered_test_subjects': len(protocol['split']['test_subjects']), 'missing_registered_test_subjects': len(missing),
                              'attrition': dict(attrition)},
                  'weight_summary': {'unit_count': sum(r['weight']==1 for r in test), 'nonunit_count': sum(r['weight']!=1 for r in test), 'method': 'unweighted_descriptive_only'},
                  'strata': {k:dict(Counter(str(r['strata'][k]) for r in test)) for k in protocol['strata']}, 'outcomes': {}}
    analysis = _time(protocol['analysis_at'])
    for kind in OUTCOMES:
        eligible, exclusions = [], Counter()
        for row in test:
            o = row['outcomes'][kind]
            # Exclusive reasons, in published precedence order. Early known positives
            # must not enter while their negative peers have an immature horizon.
            if row['followup_status'] != 'retained': reason = 'attrition'
            elif _time(o['horizon_end']) > analysis: reason = 'immature_horizon'
            elif o['status'] == 'censored': reason = 'censored'
            elif o['status'] == 'missing_oracle' or o['reference'] is None: reason = 'missing_oracle'
            else: reason = None
            if reason: exclusions[reason] += 1
            else: eligible.append(row)
        reasons = blockers + (['incomplete_outcome_ascertainment'] if exclusions else [])
        green = sum(r['green'] for r in eligible)
        events = sum(r['outcomes'][kind]['status'] == 'event' for r in eligible)
        both = sum(r['green'] and r['outcomes'][kind]['status'] == 'event' for r in eligible)
        metrics = {'p_outcome_given_green': _metric(both, green, both, protocol, reasons, f'P({kind}|green)'),
                       'p_green_given_outcome': _metric(both, events, events, protocol, reasons, f'P(green|{kind})')}
        result['outcomes'][kind] = {'eligible': len(eligible), 'exclusions': dict(exclusions), 'metrics': metrics}
    if all(m['estimate'] is not None for o in result['outcomes'].values() for m in o['metrics'].values()):
        result['status'] = 'sufficient_under_registered_assumptions'
    return result
