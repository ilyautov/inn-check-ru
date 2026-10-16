"""Independent closed JSON boundary, schema revision 2 (stdlib only).

Declarations follow the published DTO/constructor contracts, never an observed
output, renderer snapshot, scorer result or golden model. Opaque source values
are permitted only by exact, recursively type-sensitive frozen INPUT equality.
The finite synthetic corpus is the supported input language; new forms require
an explicit revision rather than a permissive JSON fallback.
"""
from __future__ import annotations

import copy
import math
import re
from datetime import date, datetime

SCHEMA_REVISION = 2


def scalar(kind, **constraints):
    return {'kind': kind, **constraints}


def enum(*values):
    return scalar('enum', values=list(values))


def exact(value):
    return scalar('exact', value=copy.deepcopy(value))


def obj(required, optional=None, *, item_length_matches=None):
    return scalar('object', required=required, optional=optional or {},
                  item_length_matches=item_length_matches or {})


def array(item, maximum=10000, *, minimum=0):
    return scalar('array', item=item, minimum=minimum, maximum=maximum)


def mapping(key, value, *, minimum=0, maximum=10000):
    return scalar('map', key=key, value=value, minimum=minimum, maximum=maximum)


def union(*options):
    return scalar('union', options=list(options))


def nullable(value):
    return union(exact(None), value)


TEXT = scalar('string', maximum=2000000)
BOOL = scalar('boolean')
COUNT = scalar('integer', minimum=0, maximum=9007199254740991)
REVISION = scalar('integer', minimum=1, maximum=9007199254740991)
YEAR = scalar('integer', minimum=1900, maximum=9998)
NUMBER = scalar('number', minimum=-1e308, maximum=1e308)
NONNEGATIVE = scalar('number', minimum=0, maximum=1e308)
SHA = scalar('string', pattern=r'[a-f0-9]{64}')
UUID = scalar('string', pattern=r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}')
RESPONSE_ID = scalar('string', pattern=r'response-[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}')
CLAIM_ID = scalar('string', pattern=r'sha256:[a-f0-9]{64}')
INN = scalar('string', pattern=r'(?:[0-9]{10}|[0-9]{12})')
INSTANT = scalar('string', format='instant')
DAY = scalar('string', format='day')
POINTER = scalar('string', pattern=r'(?:/(?:[^~]|~[01])*)?')
URL = scalar('string', pattern=r'https://[^\s]+', maximum=2000)
PERIOD = obj({'start': DAY, 'end': DAY})
STATUS = enum('найден', 'отсутствует', 'не проверен', 'не применимо')
LIGHT = enum(None, '🟢', '🟡', '🔴')
FIN_IDS = ('ча_отрицательные_2года', 'ча_ниже_уставного_капитала',
           'коэффициент_автономии', 'текущая_ликвидность', 'падение_выручки',
           'транзитный_профиль', 'убыток_2года')
BALANCE_CODE = enum('1300', '1600', '1200', '1500', '1250', '1400', '2110', '2400')
BALANCES = mapping(scalar('string', pattern=r'[1-9][0-9]{3}'), mapping(BALANCE_CODE, NUMBER))
FIN_YEAR = union(YEAR, scalar('string', pattern=r'[1-9][0-9]{3}–[1-9][0-9]{3}'))
FLAG_VALUE = obj({'показатель': TEXT, 'значение': NUMBER, 'порог': TEXT, 'год': FIN_YEAR})

# dossier.заголовок/абзац/список/поля/таблица DTOs. Omitted paragraph
# presentation flags are equivalent to their declared False defaults.
MODEL_BLOCK = scalar('tagged', tag='тип', variants={
    'заголовок': obj({'тип': exact('заголовок'), 'уровень': scalar('integer', minimum=1, maximum=6), 'текст': TEXT}),
    'абзац': obj({'тип': exact('абзац'), 'текст': TEXT}, {'жирный': BOOL, 'курсив': BOOL}),
    'список': obj({'тип': exact('список'), 'пункты': array(TEXT)}),
    'поля': obj({'тип': exact('поля'), 'пары': array(scalar('tuple', items=[TEXT, TEXT]))}),
    'таблица': obj({'тип': exact('таблица'), 'шапка': array(TEXT, minimum=1),
                   'строки': array(array(TEXT, minimum=1))}, item_length_matches={'строки': 'шапка'}),
})
MODEL = array(MODEL_BLOCK)


def same(left, right):
    """Exact JSON types, including bool/int and numeric int/float distinctions."""
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        return left.keys() == right.keys() and all(same(left[k], right[k]) for k in left)
    if type(left) is list:
        return len(left) == len(right) and all(same(a, b) for a, b in zip(left, right))
    if type(left) is float and not math.isfinite(left):
        return False
    return left == right


def strings(value, path='$', include_keys=True):
    """Enumerate unexpected serialized strings recursively, including JSON keys."""
    stack = [(path, value)]
    while stack:
        path, value = stack.pop()
        if type(value) is str:
            yield path, value
        elif type(value) is dict:
            for key, child in reversed(list(value.items())):
                stack.append((path + '.' + str(key), child))
                if include_keys and type(key) is str:
                    stack.append((path + '.<key>', key))
        elif type(value) is list:
            stack.extend((path + '[' + str(i) + ']', child) for i, child in reversed(list(enumerate(value))))


def validate(value, schema, path='$'):
    """Return bounded diagnostics instead of coercing or throwing on bad JSON.

    Each issue owns a disjoint rejected subtree, so a malformed nested container
    cannot hide prose, and one rejected leaf is not counted once per union arm.
    No successful validation is an emitted assertion.
    """
    kind = schema['kind']
    def invalid(reason, rejected=value, location=path):
        return [{'path': location, 'reason': reason, 'strings': list(strings(rejected, location))}]
    if kind == 'exact':
        expected = schema['value']
        if type(value) is dict and type(expected) is dict:
            issues = [{'path': path + '.' + k, 'reason': 'required_frozen_input_key', 'strings': []}
                      for k in expected.keys() - value.keys()]
            for key, child in value.items():
                if key not in expected:
                    issues += invalid('unknown_frozen_input_key', {key: child}, path + '.' + str(key))
                else:
                    issues += validate(child, {'kind': 'exact', 'value': expected[key]}, path + '.' + str(key))
            return issues
        if type(value) is list and type(expected) is list:
            issues = []
            if len(value) < len(expected):
                issues.append({'path': path, 'reason': 'missing_frozen_input_items', 'strings': []})
            for i, child in enumerate(value):
                issues += (validate(child, {'kind': 'exact', 'value': expected[i]}, path + '[' + str(i) + ']')
                           if i < len(expected) else invalid('extra_frozen_input_item', child, path + '[' + str(i) + ']'))
            return issues
        return [] if same(value, expected) else invalid('frozen_input_value')
    if kind == 'enum':
        return [] if any(same(value, v) for v in schema['values']) else invalid('enum')
    if kind in ('boolean', 'integer', 'number', 'string'):
        correct = (type(value) is bool if kind == 'boolean' else
                   type(value) is int if kind == 'integer' else
                   type(value) in (int, float) if kind == 'number' else type(value) is str)
        if not correct:
            return invalid('type_' + kind)
        if (kind in ('integer', 'number') and
                (not schema.get('minimum', -math.inf) <= value <= schema.get('maximum', math.inf)
                 or type(value) is float and not math.isfinite(value))):
            return invalid('finite_range')
        if kind == 'string':
            if len(value) > schema.get('maximum', 2000000) or 'pattern' in schema and re.fullmatch(schema['pattern'], value) is None:
                return invalid('string_format')
            try:
                if schema.get('format') == 'instant':
                    if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z', value):
                        return invalid('utc_instant')
                    datetime.fromisoformat(value.replace('Z', '+00:00'))
                elif schema.get('format') == 'day':
                    if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
                        return invalid('calendar_day')
                    date.fromisoformat(value)
            except ValueError:
                return invalid('calendar_date')
        return []
    if kind == 'union':
        alternatives = [validate(value, option, path) for option in schema['options']]
        if any(not issues for issues in alternatives):
            return []
        # A nullable object with one wrong scalar owns only that rejected leaf,
        # never every legitimate string in the otherwise valid object. Branch
        # selection affects diagnostics only; every branch above already failed.
        def matches_type(option):
            kind = option['kind']
            if kind == 'exact': return type(value) is type(option['value'])
            if kind == 'enum': return any(type(value) is type(v) for v in option['values'])
            if kind == 'union': return any(matches_type(v) for v in option['options'])
            return (type(value) is dict if kind in ('object', 'map', 'tagged') else
                    type(value) is list if kind in ('array', 'tuple') else
                    type(value) is bool if kind == 'boolean' else
                    type(value) is int if kind == 'integer' else
                    type(value) in (int, float) if kind == 'number' else
                    type(value) is str if kind == 'string' else False)
        compatible = [issues for option, issues in zip(schema['options'], alternatives) if matches_type(option)]
        if compatible:
            return min(compatible, key=len)
        return invalid('union')
    if kind == 'tagged':
        if type(value) is not dict:
            return invalid('type_object')
        tag = value.get(schema['tag'])
        if type(tag) is not str or tag not in schema['variants']:
            return invalid('unknown_variant')
        return validate(value, schema['variants'][tag], path)
    if kind == 'object':
        if type(value) is not dict:
            return invalid('type_object')
        allowed = {**schema['required'], **schema['optional']}
        issues = []
        for key in schema['required'].keys() - value.keys():
            issues.append({'path': path + '.' + key, 'reason': 'required', 'strings': []})
        for key, child in value.items():
            if key not in allowed:
                issues += invalid('unknown_key', {key: child}, path + '.' + str(key))
            else:
                child_schema = allowed[key]
                reference = schema.get('item_length_matches', {}).get(key)
                if reference is not None and type(value.get(reference)) is list:
                    # This independent declaration binds only cardinality: the
                    # item type remains the explicitly declared nested schema.
                    # Validate the whole rejected row once, before leaf checks,
                    # so overlapping length/type failures cannot pad counts.
                    width = len(value[reference])
                    child_schema = dict(child_schema, item=dict(child_schema['item'], minimum=width, maximum=width))
                issues += validate(child, child_schema, path + '.' + str(key))
        return issues
    if kind in ('array', 'tuple'):
        if type(value) is not list:
            return invalid('type_array')
        if (kind == 'tuple' and len(value) != len(schema['items']) or
                not schema.get('minimum', 0) <= len(value) <= schema.get('maximum', 10000)):
            return invalid('array_length')
        issues = []
        for i, child in enumerate(value):
            issues += validate(child, schema['item'] if kind == 'array' else schema['items'][i], path + '[' + str(i) + ']')
        return issues
    if kind == 'map':
        if type(value) is not dict:
            return invalid('type_object')
        if not schema.get('minimum', 0) <= len(value) <= schema.get('maximum', 10000):
            return invalid('map_length')
        issues = []
        for key, child in value.items():
            if validate(key, schema['key']):
                issues += invalid('map_key', {key: child}, path + '.' + str(key))
            else:
                issues += validate(child, schema['value'], path + '.' + key)
        return issues
    raise ValueError('unhandled_contract_schema_kind:' + kind)


def _get(value, field):
    for key in field.split('.'):
        value = value.get(key) if type(value) is dict else None
    return value


def _pointer(value, locator):
    for key in locator.lstrip('/').split('/'):
        key = key.replace('~1', '/').replace('~0', '~')
        value = value[int(key)] if type(value) is list else value[key]
    return value


def _source_value(case, field):
    """Only quoted evidence values, bound to the exact frozen source field."""
    # Evidence DTO explicitly permits missing observations; semantic support
    # separately determines when None is actually justified (including decline).
    values = [None, _get(case['fetch'], field)]
    annual = re.fullmatch(r'финансы\.отчётность_по_годам\.([0-9]{4}|unknown)\.строки\.([0-9]{4})', field)
    if annual:
        for row in (case['fetch'].get('финансы') or {}).get('отчётность_по_годам', []):
            if str(row.get('год', 'unknown')) == annual[1]:
                values.append(row.get('строки', {}).get(annual[2]))
    for recording in case['recordings']:
        spec = recording['mappings'].get(field)
        if spec:
            value = _pointer(recording['body'], spec['locator'])
            if spec.get('transformation') in ('integer_v1', 'number_v1') and value is not None and type(value) is not bool:
                value = float(str(value).replace('\u00a0', '').replace(' ', '').replace(',', '.').replace('руб.', '').replace('₽', ''))
                if spec['transformation'] == 'integer_v1': value = int(value)
            values.append(value)
    # Numeric normalization is a DTO operation; preserve strict boolean types.
    if field.endswith('численность_сотрудников') or field.startswith('финансы.отчётность_по_годам.'):
        values += [float(v) for v in values if type(v) in (int, float)]
    return union(*(exact(v) for v in values))


def contracts(case, rubric, states, summary):
    """Build declarations using only trusted frozen input and rule vocabulary."""
    source = enum(*rubric['source_metadata'], 'fin', 'вход')
    signal = enum(*rubric['signal_catalogue'])
    reason = enum(*rubric['presentation_contract']['reason_codes'], 'unknown', 'conflict', 'not_applicable')
    field_names = list(rubric['evidence_field_contracts'])
    # The evidence DTO emits explicit missing-year/unknown-year leaves even
    # when no raw mapping exists. Derive that finite field set from source years
    # and the declared financial-row catalogue, not an inspected claim map.
    rows = (case['fetch'].get('финансы') or {}).get('отчётность_по_годам', [])
    years = {r.get('год') for r in rows if type(r) is dict and type(r.get('год')) is int and 1900 <= r['год'] <= 9998}
    if years and max(years) - 1 not in years: years.add(max(years) - 1)
    if not years: years.add('unknown')
    for year in sorted(years, key=str):
        field_names.extend('финансы.отчётность_по_годам.%s.строки.%s' % (year, code) for code in BALANCE_CODE['values'])
    for record in case['recordings']:
        field_names.extend(f for f in record['mappings'] if f not in field_names)
    field = enum(*field_names)
    ids = array(CLAIM_ID, 32)
    gap = obj({'rule_id': signal, 'reason_codes': array(reason), 'claim_ids': ids})
    coverage = obj({'numerator': COUNT, 'denominator': COUNT,
                    'supported_rules': array(signal), 'required_rules': array(signal)})
    decision = obj({'schema_version': exact(1), 'rules_version': SHA,
        'decision_status': enum('supported', 'facts_only', 'insufficient_evidence'), 'can_recommend': BOOL,
        'missing_evidence': array(gap), 'next_checks': array(obj({'rule_id': signal, 'reason_codes': array(reason)})),
        'claim_ids': array(CLAIM_ID), 'coverage': coverage})
    block_ref = obj({'run_id': UUID, 'revision': REVISION, 'source_id': source,
                     'locator': POINTER, 'block_sha256': SHA})
    scope = obj({'subject_inn': INN, 'query': nullable(TEXT)}, {'manual_observation': obj({
        'block_ref': block_ref, 'source_url': URL, 'observed_on': DAY,
        'date_precision': exact('day'), 'imported_at': INSTANT, 'expires_at': nullable(INSTANT),
        'freshness': enum('unknown', 'explicit_expiry')})})
    # Query wording is input-bound too, even when placed in a source-scope field.
    queries = {m['search_scope']['query'] for r in case['recordings'] for m in r['mappings'].values() if m.get('search_scope')}
    queries.update(b['параметры_поиска'] for b in (case.get('manual') or {}).values())
    scope['required']['query'] = enum(None, *sorted(queries))
    raw_ref = obj({'response_id': nullable(RESPONSE_ID), 'sha256': nullable(SHA), 'locator': nullable(POINTER),
                   'availability': enum('available', 'raw_unavailable', 'unmapped_locator')})
    origins = {v.get('origin_family') for v in rubric['evidence_field_contracts'].values()} | {rubric['annual_financial_contract']['origin_family']}
    upstream = {m.get('upstream_dataset') for r in case['recordings'] for m in r['mappings'].values()}
    claim_fields = {'claim_id': CLAIM_ID, 'subject_inn': INN, 'field': field, 'unit': enum(None, 'RUB', 'thousand_RUB', 'person'),
        'knowledge': enum('known', 'unknown', 'conflict', 'not_applicable'), 'reason_code': nullable(reason),
        'source_id': source, 'origin_family': enum(*sorted(origins - {None}), None), 'upstream_dataset': enum(*upstream),
        'relation': enum('derived', 'primary', 'mirror', 'legacy_untraced', 'manual_observation'),
        'raw_ref': raw_ref, 'retrieved_at': INSTANT, 'published_at': nullable(INSTANT),
        'first_observed_at': nullable(INSTANT), 'effective_at': nullable(INSTANT), 'period': nullable(PERIOD),
        'valid_until': nullable(INSTANT), 'freshness_policy': enum(*rubric['freshness_policies']),
        'transformation': enum('identity_v1', 'number_v1', 'integer_v1'), 'depends_on': ids,
        'search_scope': nullable(scope), 'coverage_complete': nullable(BOOL)}
    claim = scalar('tagged', tag='field', variants={f: obj(dict(claim_fields, value=nullable(BOOL) if f.startswith('fin.') else _source_value(case, f))) for f in field_names})
    evidence = obj({'schema_version': exact(1), 'claims': mapping(CLAIM_ID, claim),
        'conflicts': array(obj({'field': field, 'period': nullable(PERIOD), 'candidate_claim_ids': ids,
                              'selected_claim_id': nullable(CLAIM_ID), 'reason_code': exact('conflicting_values')})),
        'rules_version': SHA, 'applicability': obj({
            'subject_type': enum('legal_entity', 'ip', 'unknown'),
            'reporting_form': enum(None, 'full', 'simplified', 'ip', 'bank', 'npo', 'restricted'),
            'form_version': nullable(union(TEXT, YEAR)), 'form_evidence_ids': ids,
            'source_coverage': mapping(source, obj({'state': enum('known', 'unknown'), 'reason_code': enum(None, 'coverage_incomplete')})),
            'rules': mapping(field, obj({'status': enum('applicable', 'not_applicable', 'unknown'), 'reason_code': nullable(reason)}))}),
        'source_states': exact(states)})
    # Form version is quoted input, never an unbounded metadata prose channel.
    evidence['required']['applicability']['required']['form_version'] = enum(None, _get(case['fetch'], 'финансы.версия_формы'))
    trace = obj({'claim_id': CLAIM_ID, 'field': field, 'knowledge': enum('known', 'unknown', 'conflict', 'not_applicable'),
                 'raw_ref': raw_ref, 'period': nullable(PERIOD), 'reason_code': nullable(reason),
                 'search_scope': nullable(scope), 'coverage_complete': nullable(BOOL)})
    source_name = enum(None, 'источник неизвестен', *rubric['source_names'].values())
    dates = {None}
    for sid, state in states.items():
        dates.update((state.get('дата'), (case['fetch'].get(sid) or {}).get('дата_проверки')))
    source_date = enum(*sorted(dates - {None}), None)
    fact = obj({'сигнал': signal, 'статус': STATUS, 'текст': TEXT, 'источник': source_name,
                'дата': source_date, 'claim_ids': ids, 'evidence': array(trace)}, {'неполно': TEXT, 'оговорка': TEXT})
    input_values = [_source_value(case, f) for f in field_names if not f.startswith('fin.')]
    raised = obj({'сигнал': signal, 'severity': enum('⚪', '🟢', '🟡', '🔴'),
                  'описание': enum(*(v['описание'] for v in rubric['signal_catalogue'].values())),
                  'источник': source_name, 'дата': source_date, 'значение': union(FLAG_VALUE, *input_values)})
    assessment = obj({'_decision': decision, 'профиль': enum(*rubric['profile_catalogue']),
        'вопрос': nullable(TEXT), 'формулировка': enum('факты', 'вердикт'), 'светофор': LIGHT,
        'поднят_сигналами': array(raised), 'рекомендация': nullable(TEXT), 'факты': array(fact),
        'не_проверено_deal_killer': array(signal), 'не_проверено_обязательные_сигналы': array(signal),
        'проверка_состоялась': BOOL, 'источники_обязательные_не_проверены': array(source), 'вручную': array(TEXT)},
        {'итог_fetch': TEXT, **({'подсказка': exact(rubric['profile_catalogue'][case['profile']]['подсказка'])}
                              if 'подсказка' in rubric['profile_catalogue'][case['profile']] else {})})
    dependency = scalar('string', pattern=r'(?:егрюл\.уставный_капитал|численность_сотрудников|(?:1300|1600|1200|1500|2110|2400):[1-9][0-9]{3})')
    evaluation = obj({'статус': enum('найден', 'отсутствует', 'не проверен'), 'зависимости': array(dependency),
                      'причина': nullable(TEXT), 'достаточные_зависимости': array(dependency)})
    flag = obj({'id': enum(*FIN_IDS), 'severity': enum('🟡', '🔴'), 'год': FIN_YEAR, 'значение': NUMBER,
        'источник': exact('ГИР БО'), 'показатель': TEXT, 'порог': TEXT, 'пояснение': TEXT, 'строки_баланса': BALANCES})
    identity = obj({'schema_version': exact(1), 'sha256': SHA, 'scorer_sha256': SHA, 'rules_sha256': SHA})
    financial_profile = obj({'сводка': TEXT, 'оценено_лет': COUNT, 'года': array(YEAR),
        'уставный_капитал_тыс_руб': nullable(NONNEGATIVE), 'численность': nullable(NONNEGATIVE),
        'численность_год': nullable(YEAR), 'численность_источник': enum(None, 'риски', 'дампы_фнс', 'вход'),
        'не_проверено': array(TEXT), 'строки_баланса': BALANCES})
    fin = scalar('tagged', tag='статус', variants={
        'не проверено': obj({'_input_identity': identity, 'статус': exact('не проверено'), 'причина': TEXT, 'источник': exact('ГИР БО')}),
        'ок': obj({'_input_identity': identity, 'статус': exact('ок'), 'флаги': array(flag), 'версия_оценок': exact(1),
                   'оценки_сигналов': obj({k: evaluation for k in FIN_IDS}), 'финансовый_профиль': financial_profile}),
    })
    # Mapping metadata is quoted frozen adapter input, not a free metadata bag.
    mappings = {}
    for record in case['recordings']:
        for name, spec in record['mappings'].items():
            mappings.setdefault(name, []).append(obj(dict({'response_id': RESPONSE_ID}, **{k: exact(v) for k, v in spec.items()})))
    response = obj({'source_id': source, 'subject_inn': INN, 'request': obj({'subject_inn': INN}), 'sha256': SHA,
        'retrieved_at': INSTANT, 'first_observed_at': INSTANT, 'capture_format': enum('sanitized_json_v1', 'withheld_non_json_v1'), 'original_response_preserved': BOOL})
    # Each frozen fixture transport records every declared response. No-capture
    # input still permits an empty map; raw-loss injection requires a target.
    capture_minimum = max(len(case['recordings']), int(case.get('fault') == 'raw_loss'))
    raw_refs = obj({'responses': mapping(RESPONSE_ID, response, minimum=capture_minimum),
                    'fields': obj({}, {k: array(union(*v)) for k, v in mappings.items()})})
    warning = scalar('tagged', tag='code', variants={
        'interrupted': obj({'code': exact('interrupted'), 'message': exact('Сбор не завершён; сохранены доступные результаты. Для обновления нужна явная команда refresh.')}),
        'collection_failed': obj({'code': exact('collection_failed'), 'message': exact('Сбор остановлен из-за ошибки; сохранены доступные ответы.')}),
    })
    metadata = obj({'schema_version': exact(1), 'engine_version': scalar('string', pattern=r'[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?'),
        'rules_version': SHA, 'evaluated_at': exact(case.get('reload_at', rubric['frozen_at'])), 'decision': decision, 'profile': exact(case['profile']),
        'mode': exact('полный'), 'workflow_status': enum('collecting', 'complete', 'failed', 'cancelled'), 'raw_refs': raw_refs},
        {'manual_outcomes': mapping(source, enum('completed', 'declined', 'incomplete')), 'warnings': array(warning),
         'operation': obj({'kind': enum('manual', 'refresh'), 'request_sha256': SHA})})
    fetch_fields = {k: exact(v) for k, v in case['fetch'].items()}
    fetch_fields.update({'_evidence': evidence, '_decision': decision})
    if case.get('manual'):
        fetch_fields['_итог_проверки'] = exact(summary)
        availability = copy.deepcopy(case['fetch']['_доступность'])
        for sid, block in case['manual'].items():
            # The frozen manual contract only permits a declined observation.
            if block['outcome'] != 'declined':
                raise ValueError('manual_operation_outside_frozen_typed_contract')
            fetch_fields[sid] = exact({'инн': block['инн'], 'ввод': 'браузер', 'дата_проверки': block['дата_проверки'],
                'url': block['url'], 'проверил': 'пользователь', 'параметры_поиска': block['параметры_поиска'],
                'статус': 'не проверено: ' + block['reason'], 'outcome': 'declined', 'expires_at': None, 'scope_declared': True})
            availability[sid] = states[sid]
        fetch_fields['_доступность'] = exact(availability)
        manual_fields = {'суды': ('ответчик_крупные', 'ответчик_дел', 'ответчик_сумма'),
                         'фссп': ('производств', 'крупные', 'сумма'), 'залоги': ('уведомлений',)}
        manual_field = enum(*(sid + '.' + key for sid in case['manual'] for key in manual_fields[sid]))
        fetch_fields['_evidence_scope'] = mapping(manual_field, obj({'relation': exact('manual_observation'),
            'search_scope': nullable(scope), 'coverage_complete': BOOL, 'imported_at': INSTANT, 'observation_ref': block_ref}))
    fetch = obj(fetch_fields)
    payload = obj({'fetch': fetch, 'fin': fin, 'assessment': assessment, 'metadata': metadata})
    required_fields = enum(*(f.split('.', 1)[1] for f in rubric['evidence_field_contracts']))
    action_fields = {'run_id': UUID, 'revision': REVISION, 'kind': enum('manual', 'key', 'cache', 'network', 'refresh', 'resolve_conflict'),
        'source_id': source, 'reason_code': reason, 'query': obj({'инн': INN}),
        'required_fields': array(required_fields), 'expires_at': nullable(INSTANT),
        'action_id': SHA}
    # run_workflow._pending declares these dependent values together. A URL is
    # nullable only in the explicitly unavailable variant, never independently.
    action = union(obj(dict(action_fields, available=exact(True), url=URL, unavailable_reason=exact(None))),
                   obj(dict(action_fields, available=exact(False), url=exact(None), unavailable_reason=exact('official_url_unavailable'))))
    artifact = obj({'artifact_id': UUID, 'format': exact('snapshot'), 'sha256': SHA, 'source_revision': REVISION})
    envelope_base = {'schema_version': exact(1), 'status': enum('ok', 'partial', 'needs_input', 'cancelled', 'error'),
        'run_id': nullable(UUID), 'revision': nullable(REVISION), 'error': nullable(obj({'code': exact('corrupt_store'),
        'message': exact('Записанный исходный ответ отсутствует.'), 'retryable': exact(False)})),
        'warnings': array(warning), 'pending_actions': array(action)}
    envelopes = {}
    for stage, data in {'start': payload, 'manual': payload, 'read': payload, 'mcp_read': payload,
        'model': obj({'format': exact('json'), 'content': MODEL, 'source_revision': REVISION}),
        'mcp_model': obj({'format': exact('json'), 'content': MODEL, 'source_revision': REVISION}),
        'markdown': obj({'format': exact('markdown'), 'content': TEXT, 'source_revision': REVISION}),
        'mcp_text': obj({'format': exact('markdown'), 'content': TEXT, 'source_revision': REVISION}), 'snapshot': artifact}.items():
        envelopes[stage] = obj(dict(envelope_base, data=nullable(data)))
    snapshot = _snapshot_schema(case, fetch_fields, evidence, decision, source)
    output = obj({'error_code': exact(None), 'run_id': UUID, 'case_clock': INSTANT, 'elapsed_ms': NONNEGATIVE,
        'recorded_requests': COUNT, 'manual_steps': COUNT, 'payload': payload, 'model': MODEL,
        'markdown': TEXT, 'snapshot': snapshot, 'actions': array(action),
        'parity': obj({k: BOOL for k in ('read', 'model', 'markdown', 'snapshot')})},
        {'serialized': obj({k: envelopes[k] for k in ('read', 'model', 'markdown', 'snapshot', 'mcp_read', 'mcp_model', 'mcp_text')})})
    return {'output': output, 'envelopes': envelopes, 'model': MODEL, 'claim': claim,
            'assessment': assessment, 'decision': decision, 'fin': fin, 'snapshot': snapshot}


def _snapshot_schema(case, fetch_fields, evidence, decision, source):
    """Closed snapshot DTO; only quoted source projections retain opaque values."""
    fetch = case['fetch']
    alias = {'статус': 'егрюл.статус', 'дата_прекращения': 'егрюл.дата_прекращения',
        'руководитель': 'егрюл.руководитель', 'адрес': 'егрюл.адрес',
        'недостоверность_сведений': 'риски.недостоверность_сведений',
        'налоговая_задолженность': 'риски.налоговая_задолженность', 'мсп_статус': 'мсп.статус_мсп',
        'мсп_категория': 'мсп.категория', 'банкротство': 'банкротство.дело', 'список_цб': 'список_цб.в_списке'}
    fields = {k: exact(_get(fetch, v)) for k, v in alias.items()}
    fields.update({'финансы_годы': array(obj({'год': YEAR, 'чистые_активы': nullable(NUMBER), 'выручка': nullable(NUMBER)})),
        'спецреестры': exact({k: (v.get('статус') if type(v) is dict else None) for k, v in fetch.get('спецреестры', {}).items()} if type(fetch.get('спецреестры')) is dict else None),
        'ликвидация': BOOL, 'год_отчётности': nullable(YEAR), 'чистые_активы': nullable(NUMBER), 'выручка': nullable(NUMBER)})
    # Values are exact quoted input leaves; omission is left to snapshot semantics.
    verdict_fields = {'_evidence': evidence, '_decision': decision}
    for sid, block in fetch.items():
        if sid.startswith('_') or type(block) is not dict:
            continue
        if sid == 'финансы':
            verdict_fields[sid] = obj({}, {'форма_отчётности': exact(block.get('форма_отчётности')),
                'версия_формы': exact(block.get('версия_формы')), 'отчётность_по_годам': array(obj({'год': YEAR, 'строки': mapping(BALANCE_CODE, NUMBER)}))})
        else:
            verdict_fields[sid] = obj({}, {k: exact(v) for k, v in block.items()})
    for sid, block in (case.get('manual') or {}).items():
        verdict_fields[sid] = obj({}, {'ввод': exact('браузер'), 'дата_проверки': exact(block['дата_проверки']),
            'url': exact(block['url']), 'инн': exact(block['инн']), 'параметры_поиска': exact(block['инн']),
            'статус': exact('не проверено: ' + block['reason'])})
    return obj({'_evidence': evidence, '_decision': decision, 'версия_формата': exact(2), 'инн': INN, 'дата': INSTANT, 'поля': obj(fields),
        'вердикт': obj({'_evidence': evidence, '_decision': decision}, {k: v for k, v in verdict_fields.items() if not k.startswith('_')}),
        'состояния': mapping(source, enum('ok', 'пусто', 'не проверено')),
        'сбор': obj({'режим': exact(None), 'профиль': exact(None)}),
        'хеш_остального': scalar('string', pattern=r'sha256:[a-f0-9]{64}'),
        '_итог': obj({'проверка_состоялась': BOOL, 'не_проверено': COUNT}), 'не_покрыты': array(source)})


def audit(value, schema, path='$'):
    """Schema failures plus only the actual rejected string occurrences."""
    issues = validate(value, schema, path)
    failures = [{'code': 'output_schema_invalid', 'detail': {'path': i['path'], 'reason': i['reason']}} for i in issues]
    assertions = []
    for issue in issues:
        for location, rejected_text in issue['strings']:
            for unit in filter(None, re.split(r'(?<=[.!?])\s+', rejected_text)):
                assertions.append({'kind': 'unsupported_serialized_text', 'key': location, 'unit': unit, 'supported': False})
    return assertions, failures
