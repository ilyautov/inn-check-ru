#!/usr/bin/env python3
"""Frozen, synthetic decision-mechanism benchmark through production boundaries.

Only the recorded collector transport/locator adapter and clock are test doubles.
Scoring, resolving, persistence, CLI JSON, MCP worker JSON, snapshots and dossiers
are production code. No external I/O, alternate scorer, or public injection hook.
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ''):
    # Direct-file execution starts on benchmark/, so expose the repository
    # package while retaining the canonical benchmark.decision_schema module.
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))
import evidence_contract as ec
import fetch_counterparty as collector
import profiles
import run_store
import run_workflow
import workflow_cli

from benchmark import decision_schema

CLOCK = '2026-10-16T09:00:00Z'
SEED = 601016
MANIFEST = ROOT / 'benchmark/decision-corpus/manifest.json'


def _bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()


def _hash(value):
    return hashlib.sha256(_bytes(value)).hexdigest()


def _file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_frozen():
    manifest = json.loads(MANIFEST.read_text())
    for kind in ('rubric', 'corpus', 'output_schema'):
        if _file_hash(ROOT / manifest[kind + '_path']) != manifest[kind + '_sha256']:
            raise ValueError(kind + '_hash_mismatch')
    if manifest['output_schema_revision'] != decision_schema.SCHEMA_REVISION:
        raise ValueError('output_schema_revision_mismatch')
    recorded = manifest.get('recorded_fixture_source')
    if recorded and _file_hash(ROOT / recorded['path']) != recorded['sha256']:
        raise ValueError('recorded_fixture_hash_mismatch')
    return manifest


def load_cases():
    manifest = verify_frozen()
    cases = json.loads((ROOT / manifest['corpus_path']).read_text())['cases']
    if _hash(cases) != manifest['cases_sha256']:
        raise ValueError('cases_hash_mismatch')
    return cases


def _validate_case(case):
    if not isinstance(case, dict) or not case.get('synthetic') or case.get('corpus_class') != 'synthetic_target':
        raise ValueError('only_explicit_synthetic_fixtures_supported')
    if _hash(case['fetch']) != case['input_sha256']:
        raise ValueError('source_hash_mismatch')
    sources = {s['recording_id']: s for s in case['sources']}
    for recording in case['recordings']:
        source = sources[recording['id']]
        if _hash(recording['body']) != source['sha256'] or not source['license'] or not source['synthetic']:
            raise ValueError('source_hash_mismatch')


def _engine_hash():
    files = sorted([*ROOT.glob('scripts/*.py'), ROOT / 'mcp/tools_impl.py', ROOT / 'mcp/server.py'])
    return _hash({str(p.relative_to(ROOT)): _file_hash(p) for p in files})


def _rules_hash():
    return ec._digest({'profiles': ec._resource('profiles_ru.json'), 'policy': ec._resource('evidence_policy.json'),
                       'canon': ec._resource('canon_ru.json')})


@contextlib.contextmanager
def _fixture_transport(case, now):
    """Private eval-only injection, never read from a production argument/env/path."""
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime.fromisoformat(now.replace('Z', '+00:00'))
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)
    recordings = case['recordings']
    # Local oracle context comes from real capture IDs, before any inspected
    # CLI/MCP DTO exists. It is never serialized or accepted as runtime input.
    trusted_refs = {'responses': {}, 'fields': {}}
    # Use real CaptureSession.record/sanitize/store callbacks; retain exact captured bytes.
    def collect(inn, **kwargs):
        for recording in recordings:
            collector._ИСТОЧНИК.id = recording['source_id']
            collector._ИСТОЧНИК.subject_inn = inn
            collector._записать('GET', 'https://fixture.invalid/recorded/' + recording['id'],
                              200, _bytes(recording['body']))
        return copy.deepcopy(case['fetch'])
    def refs(capture, fetch):
        out = {'responses': {}, 'fields': {}}
        if not capture.responses:
            return out
        if len(capture.responses) != len(recordings):
            raise AssertionError('fixture_capture_count')
        for recorded, definition in zip(capture.responses, recordings):
            rid = recorded['response_id']
            if (recorded['sha256'] != _hash(definition['body']) or
                    recorded['источник'] != definition['source_id'] or
                    recorded['subject_inn'] != case['fetch']['инн']):
                raise AssertionError('fixture_capture_changed')
            out['responses'][rid] = {'source_id': recorded['источник'], 'subject_inn': capture.subject_inn,
                'request': {'subject_inn': capture.subject_inn}, 'body': recorded['данные'],
                'sha256': recorded['sha256'], 'retrieved_at': now, 'first_observed_at': CLOCK,
                'capture_format': recorded['capture_format']}
            for field, mapping in definition['mappings'].items():
                out['fields'].setdefault(field, []).append(dict(copy.deepcopy(mapping), response_id=rid))
            trusted_refs['responses'][rid] = {k: copy.deepcopy(v) for k, v in out['responses'][rid].items() if k != 'body'}
            trusted_refs['responses'][rid]['original_response_preserved'] = recorded['original_response_preserved']
        trusted_refs['fields'] = copy.deepcopy(out['fields'])
        return out
    # Bind all application wall-clock decisions, including pending/manual expiry.
    with patch.object(collector, 'collect', side_effect=collect), \
         patch.object(collector.CaptureSession, 'raw_refs', refs), \
         patch.object(run_workflow, '_now', return_value=now), \
         patch.object(run_workflow, 'datetime', FixedDatetime), \
         patch.object(run_store, 'datetime', FixedDatetime), \
         patch.object(profiles.datetime, 'datetime', FixedDatetime), \
         patch('urllib.request.urlopen', side_effect=AssertionError('network_forbidden')), \
         patch('socket.socket.connect', side_effect=AssertionError('network_forbidden')):
        yield trusted_refs


def _cli(root, *arguments):
    stream, errors = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(errors):
        status = workflow_cli.main(['--store', str(root), 'run', *arguments, '--json'])
    result = json.loads(stream.getvalue())
    if status not in (0, 1, 2):
        raise AssertionError('unexpected_cli_status')
    return result


def _mcp(root, name, arguments):
    # This is the actual fixed-dispatch worker used by the SDK server, not a fake tool.
    # The separately runnable E2E test also exercises the real optional SDK stdio server.
    env = dict(os.environ, INN_CHECK_RUNS_DIR=str(root), PYTHONDONTWRITEBYTECODE='1')
    process = subprocess.run([sys.executable, '-B', str(ROOT / 'mcp/tools_impl.py'), '--workflow-worker'],
        input=json.dumps({'name': name, 'arguments': arguments}, ensure_ascii=False),
        text=True, capture_output=True, env=env, timeout=45, check=False)
    if process.returncode:
        raise RuntimeError('mcp_worker_transport_error')
    return json.loads(process.stdout)


def run_case(case: dict, *, now: str, root: Path) -> dict:
    """Execute public serialized operations in an isolated local store."""
    _validate_case(case)
    root = root.resolve()
    started = time.perf_counter()
    clock = case.get('reload_at', now)
    with _fixture_transport(case, clock) as trusted_refs:
        start = _cli(root, 'start', case['fetch']['инн'], '--profile', case['profile'])
        assertions, violations = _envelope_audit(case, {'start': start}, trusted_refs=trusted_refs)
        if violations:
            return {'error_code': None, 'elapsed_ms': (time.perf_counter()-started)*1000,
                    'contract_assertions': assertions, 'contract_failures': violations}
        if start['status'] == 'error':
            return {'error_code': start['error']['code'], 'failed_stage': 'start', 'elapsed_ms': (time.perf_counter()-started)*1000}
        rid = start['run_id']
        expected_refs = trusted_refs
        if case.get('manual'):
            expected_refs = _post_manual_capture_context(case, trusted_refs)
            manual_path = root.parent / 'manual.json'
            manual_path.write_bytes(_bytes(case['manual']))
            start = _cli(root, 'manual', rid, '--block-json', str(manual_path),
                '--expected-revision', str(start['revision']), '--operation-id', str(uuid.uuid4()))
            assertions, violations = _envelope_audit(case, {'manual': start}, trusted_refs=expected_refs)
            if violations:
                return {'error_code': None, 'elapsed_ms': (time.perf_counter()-started)*1000,
                        'contract_assertions': assertions, 'contract_failures': violations}
            if start['status'] == 'error':
                return {'error_code': start['error']['code'], 'failed_stage': 'manual', 'elapsed_ms': (time.perf_counter()-started)*1000}
        if case.get('fault') == 'raw_loss':
            digest = next(iter(start['data']['metadata']['raw_refs']['responses'].values()))['sha256']
            (root / rid / 'raw' / digest).unlink()
        read = _cli(root, 'resume', rid)
        model = _cli(root, 'render', rid, '--format', 'json')
        markdown = _cli(root, 'render', rid, '--format', 'markdown')
        snapshot = _cli(root, 'snapshot', rid)
        mcp_read = _mcp(root, 'counterparty_run_read', {'run_id': rid})
        mcp_model = _mcp(root, 'counterparty_run_render', {'run_id': rid, 'format': 'json'})
        mcp_text = _mcp(root, 'counterparty_run_render', {'run_id': rid, 'format': 'markdown'})
    stages = {'read': read, 'model': model, 'markdown': markdown, 'snapshot': snapshot,
              'mcp_read': mcp_read, 'mcp_model': mcp_model, 'mcp_text': mcp_text}
    common = {'run_id': rid, 'case_clock': clock, 'elapsed_ms': (time.perf_counter()-started)*1000,
              'recorded_requests': len(case['recordings']), 'manual_steps': int(bool(case.get('manual')))}
    assertions, violations = _envelope_audit(case, stages, trusted_refs=expected_refs)
    if violations:
        parity = {'read': read == mcp_read, 'model': model == mcp_model, 'markdown': markdown == mcp_text}
        violations.extend({'code': 'serialized_channel_disagreement', 'detail': k} for k, same in parity.items() if not same)
        return dict(common, error_code=None, contract_assertions=assertions, contract_failures=violations, contract_parity=parity)
    errors = {stage: out['error']['code'] for stage, out in stages.items() if out['status'] == 'error'}
    if errors:
        return dict(common, error_code=next(iter(errors.values())), channel_errors=errors)
    snap = json.loads(run_store.read_artifact(rid, snapshot['data']['artifact_id'], root=root))
    return dict(common, error_code=None, payload=read['data'], model=model['data']['content'],
        markdown=markdown['data']['content'], snapshot=snap, actions=read['pending_actions'],
        parity={'read': read['data'] == mcp_read['data'], 'model': model['data']['content'] == mcp_model['data']['content'],
                'markdown': markdown['data']['content'] == mcp_text['data']['content'],
                'snapshot': snap['_evidence'] == read['data']['fetch']['_evidence'] and snap['_decision'] == read['data']['metadata']['decision']},
        serialized=stages)


def _post_manual_capture_context(case, trusted_refs):
    """Apply the frozen manual source transition to private capture expectations."""
    expected = copy.deepcopy(trusted_refs)
    manual_sources = set(case.get('manual') or {})
    expected['fields'] = {field: mappings for field, mappings in expected['fields'].items()
                          if field.split('.', 1)[0] not in manual_sources}
    return expected


def _typed_contract(case, *, before_manual=False):
    if before_manual and case.get('manual'):
        case = dict(case)
        case.pop('manual')
    rubric = json.loads((ROOT / 'benchmark/decision-rubric.json').read_text())
    states = _source_text_oracle(case, rubric)
    return decision_schema.contracts(case, rubric, states, _summary_text_oracle(case, states, rubric))


def _envelope_audit(case, stages, *, trusted_refs=None):
    contracts = _typed_contract(case)['envelopes']
    assertions, failures = [], []
    for stage, value in stages.items():
        schema = _typed_contract(case, before_manual=True)['envelopes'][stage] if stage == 'start' else contracts[stage]
        audited, violations = decision_schema.audit(value, schema, '$.' + stage)
        assertions.extend(audited)
        failures.extend(violations)
        if not violations:
            valid = ((value['data'] is None and value['error'] is not None) if value['status'] == 'error' else
                     value['data'] is not None and value['error'] is None and value['run_id'] is not None and value['revision'] is not None)
            if not valid:
                failures.append({'code': 'output_schema_invalid', 'detail': {'path': '$.' + stage, 'reason': 'envelope_status_binding'}})
            if (valid and trusted_refs is not None and value['status'] != 'error' and
                    stage in ('start', 'manual', 'read', 'mcp_read') and
                    value['data']['metadata']['raw_refs'] != trusted_refs):
                failures.append({'code': 'capture_identity_binding_mismatch', 'detail': stage})
    return assertions, failures


def _output_shape_audit(case, output):
    """Validate every shape, inventory each explicitly bound CLI view once."""
    contract = _typed_contract(case)['output']
    assertions, violations = decision_schema.audit(output, contract)
    if type(output) is not dict or type(output.get('serialized')) is not dict:
        return assertions, violations, []
    # These are the only canonical projections made by run_case. Their public
    # origins remain separate from every other CLI/MCP envelope and snapshot.
    origins = {'payload': ('read', 'data'), 'model': ('model', 'data', 'content'),
               'markdown': ('markdown', 'data', 'content'), 'actions': ('read', 'pending_actions')}
    bound, binding_failures = set(), []
    for projection, path in origins.items():
        if projection not in output:
            continue
        origin = output['serialized']
        for key in path:
            if type(origin) is not dict or key not in origin:
                binding_failures.append({'code': 'canonical_projection_binding_mismatch', 'detail': projection})
                break
            origin = origin[key]
        else:
            if decision_schema.same(output[projection], origin):
                bound.add(projection)
            else:
                binding_failures.append({'code': 'canonical_projection_binding_mismatch', 'detail': projection})
    if bound:
        # Re-enumerate only inventory after full validation above. Remove exact
        # top-level projection keys structurally: unescaped diagnostic paths can
        # otherwise confuse an unrelated unknown key such as "model.some.key".
        inventory = {key: value for key, value in output.items() if key not in bound}
        inventory_contract = dict(contract, required={key: spec for key, spec in contract['required'].items()
                                                    if key not in bound})
        assertions, _ = decision_schema.audit(inventory, inventory_contract)
    return assertions, violations, binding_failures


def _frozen_leaf_binding(case, refs, field, raw):
    """Resolve an observation through its response, never through claim prose.

    Body bytes alone do not identify a provider. The actual envelope path first
    checks these response IDs against the private capture context; saved-output
    checks additionally require a unique frozen source/hash/field mapping.
    """
    rid = raw.get('response_id')
    response = refs['responses'].get(rid, {})
    subject = case['fetch']['инн']
    if (not re.fullmatch(r'response-[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}', str(rid)) or
            response.get('sha256') != raw.get('sha256') or
            response.get('subject_inn') != subject or
            response.get('request', {}).get('subject_inn') != subject):
        return None, {}
    candidates = [(r, r['mappings'][field]) for r in case['recordings'] if field in r['mappings'] and
                  r['source_id'] == response.get('source_id') and _hash(r['body']) == raw.get('sha256')]
    if len(candidates) != 1:
        return None, {}
    recording, mapping = candidates[0]
    if (raw.get('locator') != mapping['locator'] or
            mapping.get('source_id', recording['source_id']) != response['source_id'] or
            refs['fields'].get(field, []).count(dict(mapping, response_id=rid)) != 1):
        return None, {}
    try:
        observed_subject = recording['body']
        for part in mapping['subject_locator'].lstrip('/').split('/'):
            part = part.replace('~1', '/').replace('~0', '~')
            observed_subject = observed_subject[int(part)] if isinstance(observed_subject, list) else observed_subject[part]
        if observed_subject != subject:
            return None, {}
    except (KeyError, IndexError, TypeError, ValueError):
        return None, {}
    return recording, mapping


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


def _source_text_oracle(case, rubric):
    """Finite input grammar for source diagnostics; no inspected output is read.

    The corpus accepts normalized source observations, not arbitrary collector
    strings. Unsupported new input forms require a frozen contract revision.
    """
    fetch = case['fetch']
    states = {}
    for sid, entry in fetch.get('_доступность', {}).items():
        state = copy.deepcopy(entry)
        if not isinstance(state, dict):
            raise TypeError('source_state_outside_frozen_input_grammar')
        block = fetch.get(sid) or {}
        reason = state.get('причина')
        own = str(block.get('статус') or '').strip().lower()
        if own.startswith(('не проверено', 'не собрано')):
            state['состояние'] = 'не проверено'
            reason = block.get('причина') or block.get('примечание') or reason or own
        if state.get('состояние') not in ('ok', 'пусто', 'не проверено'):
            state['состояние'], reason = 'не проверено', reason or 'состояние не распознано'
        if rubric['source_metadata'].get(sid, {}).get('требует') == 'браузер' and state['состояние'] in ('ok', 'пусто'):
            if not (block.get('дата_проверки') and re.fullmatch(r'https?://\S+', str(block.get('url') or ''))):
                raise ValueError('manual_source_outside_frozen_input_grammar')
            reason = reason or 'по статусу блока (ручной ввод / браузер)'
        state['причина'] = reason
        state['дата'] = state.get('дата') or block.get('дата') or block.get('дата_проверки') or next(
            (fetch[k] for k in ('дата', 'дата_проверки', '_дата') if fetch.get(k)), None)
        states[sid] = state
    # The only frozen manual operation is an explicit declined observation. It
    # adds a source-bound diagnostic, never a positive or negative claim.
    for sid, block in (case.get('manual') or {}).items():
        if block.get('outcome') != 'declined':
            raise ValueError('manual_operation_outside_frozen_input_grammar')
        states[sid] = {'состояние': 'не проверено', 'причина': 'не покрыто: браузерный источник — ручной блок',
            'дата': block['дата_проверки'], 'ввод': 'браузер', 'url': block['url']}
    return dict(sorted(states.items()))


def _summary_text_oracle(case, states, rubric):
    if not case.get('manual'):
        return case['fetch'].get('_итог_проверки', {})
    killers = rubric['deal_killer_sources']
    unknown = [s for s in killers if states.get(s, {}).get('состояние') not in ('ok', 'пусто')]
    occurred = len(unknown) * 2 <= len(killers)
    explanation = ('проверка состоялась: %d из %d deal-killer-источников не проверено%s' % (
        len(unknown), len(killers), ' (%s)' % ', '.join(unknown) if unknown else '') if occurred else
        'проверка НЕ состоялась: %d из %d deal-killer-источников не проверено — светофор 🟢 недопустим' % (len(unknown), len(killers)))
    return {'источников': len(states), 'ok': sum(s['состояние'] == 'ok' for s in states.values()),
        'пусто': sum(s['состояние'] == 'пусто' for s in states.values()),
        'не_проверено': sum(s['состояние'] == 'не проверено' for s in states.values()),
        'deal_killer_источники': killers, 'deal_killer_не_проверено': unknown,
        'проверка_состоялась': occurred, 'вывод': explanation}


def _financial_text_oracle(case, source_states):
    """Independent arithmetic/diagnostic grammar for text-bearing fin fields.

    Operands come from frozen normalized observations. This does not invoke the
    scorer, resolver, renderer, persisted assessment, or their returned prose.
    """
    fetch = case['fetch']
    records = (fetch.get('финансы') or {}).get('отчётность_по_годам') or []
    if not records:
        return {'status': 'не проверено', 'reason': 'во входных данных нет блока «финансы»/«отчётность_по_годам» (ГИР БО не отдал отчётность или скрипт не запускался)',
                'reasons': {}, 'unverified': [], 'flags': {}}
    years = sorted({int(r['год']): r for r in records}.items(), reverse=True)
    year, cur = years[0]
    prev = dict(years).get(year - 1, {})
    def number(value):
        if value is None or isinstance(value, bool):
            return None
        return float(str(value).replace(' ', '').replace('\u00a0', '').replace(',', '.'))
    def row(rec, code):
        return number(rec.get('строки', {}).get(code))
    capital = number((fetch.get('егрюл') or {}).get('уставный_капитал'))
    capital = capital / 1000 if capital is not None else None
    a, pa = row(cur, '1300'), row(prev, '1300')
    assets, current_assets, debt = row(cur, '1600'), row(cur, '1200'), row(cur, '1500')
    rev, prev_rev = row(cur, '2110'), row(prev, '2110')
    profit, prev_profit = row(cur, '2400'), row(prev, '2400')
    revenue_missing = not prev or rev is None or prev_rev is None or prev_rev <= 0
    reasons = {
        'ча_отрицательные_2года': f'чистые активы два года подряд — нужны строки 1300 за {year} и {year - 1}' if a is None or pa is None else None,
        'ча_ниже_уставного_капитала': 'чистые активы vs уставный капитал — нет строки 1300 или уставного капитала' if a is None or capital is None else None,
        'коэффициент_автономии': 'коэффициент автономии — нет строк 1300/1600 или активы равны нулю' if a is None or not assets else None,
        'текущая_ликвидность': 'текущая ликвидность — нет строк 1200/1500 или обязательства равны нулю' if current_assets is None or not debt else None,
        'падение_выручки': f'динамика выручки — нужны строки 2110 за {year} и {year - 1} и положительная база' if revenue_missing else None,
        'транзитный_профиль': f'динамика выручки — нужны строки 2110 за {year} и {year - 1} и положительная база' if revenue_missing else None,
        'убыток_2года': f'убыток 2+ года — нужны строки 2400 за {year} и {year - 1}' if not prev or profit is None or prev_profit is None else None,
    }
    unverified = [reasons[k] for k in ('ча_отрицательные_2года', 'ча_ниже_уставного_капитала') if reasons[k]]
    if a is None: unverified.append('чистые активы (стр. 1300) — нет строки за последний год')
    for key, extra in [('коэффициент_автономии', 'коэффициент автономии — нет строк 1300/1600'),
                       ('текущая_ликвидность', 'текущая ликвидность — нет строк 1200/1500')]:
        if reasons[key]: unverified.extend([reasons[key], extra])
    for key in ('падение_выручки', 'транзитный_профиль'):
        if reasons[key]: unverified.append(reasons[key])
    flags = {}
    def flag(sid, indicator, value, threshold, period):
        flags[sid] = (indicator, round(value, 2), threshold, period)
    if a is not None and pa is not None and a < 0 and pa < 0:
        flag('ча_отрицательные_2года', 'чистые активы (стр. 1300)', a, '< 0 два года подряд', f'{year - 1}–{year}')
    if a is not None and capital is not None and a < capital:
        flag('ча_ниже_уставного_капитала', 'чистые активы (стр. 1300)', a, '< уставного капитала %.0f тыс. руб.' % capital, year)
    if a is not None and assets and a / assets < .1:
        flag('коэффициент_автономии', 'автономия (1300/1600)', a / assets, '< 0.1', year)
    if current_assets is not None and debt and current_assets / debt < 1:
        flag('текущая_ликвидность', 'ликвидность (1200/1500)', current_assets / debt, '< 1', year)
    if not revenue_missing:
        fall, growth = (prev_rev - rev) / prev_rev, (rev - prev_rev) / prev_rev
        if fall > .5:
            flag('падение_выручки', 'выручка (стр. 2110)', fall * 100, 'падение > 50% г/г', year)
        staff, staff_year, staff_source = None, None, None
        for source in ('риски', 'дампы_фнс'):
            block = fetch.get(source) or {}
            if source_states.get(source, {}).get('состояние') != 'ok': continue
            staff = number(block.get('численность_сотрудников'))
            if staff is not None:
                staff_source = source
                staff_year = block.get('среднесписочная_за_год', block.get('численность_год'))
                break
        if growth > 3:
            if staff is None or staff_year not in (None, year) or staff_source == 'дампы_фнс' and staff_year is None:
                reasons['транзитный_профиль'] = f'транзитный профиль — численность не проверена за {year} (источник: {staff_source or "нет"}, год: {staff_year or "не указан"})'
                unverified.append(reasons['транзитный_профиль'])
            elif staff <= 1:
                flag('транзитный_профиль', 'рост выручки (стр. 2110)', growth * 100, '> 300% при численности ~0', year)
    else:
        unverified.append('динамика выручки — нужна строка 2110 за 2 года')
    if reasons['убыток_2года']:
        unverified.extend([reasons['убыток_2года'], 'убыток 2+ года — нужна строка 2400 за 2 года'])
    elif profit < 0 and prev_profit < 0:
        flag('убыток_2года', 'чистая прибыль (стр. 2400)', profit, '< 0 два года подряд', f'{year - 1}–{year}')
    return {'status': 'ок', 'reason': None, 'reasons': reasons, 'unverified': unverified, 'flags': flags}


def _presentation_audit(case, output):
    """Independent finite-language oracle, never calls dossier/profiles rendering.

    Claim IDs alone are not evidence. Each fact has a typed field binding; each
    transitive leaf is checked against frozen recording bytes/mappings; each
    matching presentation row must contain that exact trace. All emitted model
    atoms are classified, and Markdown is independently tokenized against the
    validated structured atoms. Unknown prose fails closed.
    """
    from collections import Counter
    rubric = json.loads((ROOT / 'benchmark/decision-rubric.json').read_text())
    contract = rubric['presentation_contract']
    payload, model = output['payload'], output['model']
    claims = payload['fetch']['_evidence']['claims']
    decision, assessment = payload['metadata']['decision'], payload['assessment']
    failures, assertions = [], []
    invalid_support = set()
    expected_claim_states = {}

    def check(kind, key, valid, detail=None, *, location=None):
        # Only emitted assertion occurrences enter the output denominator.
        # Schema, input-binding and trace-edge checks are gate failures, not
        # extra successful assertions that could dilute an error rate.
        if kind in ('unsupported_rendered_paragraph', 'unsupported_rendered_list_assertion',
                    'unsupported_rendered_field', 'unsupported_rendered_table_row',
                    'unvalidated_markdown_atom') or (not valid and kind in (
                        'unsupported_rendered_heading', 'unsupported_serialized_text')):
            units = [key]
            if not valid and isinstance(detail, str):
                units = [part for part in re.split(r'(?<=[.!?])\s+', detail) if part]
            for unit in units:
                assertions.append({'kind': kind, 'key': key, 'unit': unit, 'supported': bool(valid),
                                   'location': location or '$.payload.' + key})
        if not valid:
            invalid_support.add(key)
        if not valid:
            failures.append({'code': kind, 'detail': detail if detail is not None else key})

    def display(value):
        if value is None or value == '' or value == []:
            return '—'
        if isinstance(value, bool):
            return 'да' if value else 'нет'
        if isinstance(value, (list, tuple)):
            return ', '.join(display(x) for x in value)
        return str(value)

    def pointer(body, locator):
        for part in locator.lstrip('/').split('/'):
            part = part.replace('~1', '/').replace('~0', '~')
            body = body[int(part)] if isinstance(body, list) else body[part]
        return body

    source_states = _source_text_oracle(case, rubric)
    source_summary = _summary_text_oracle(case, source_states, rubric)
    fin_oracle = _financial_text_oracle(case, source_states)
    profile = rubric['profile_catalogue'][case['profile']]
    reason_codes = set(contract['reason_codes'])
    refs = payload['metadata']['raw_refs']
    expected_captures = Counter((r['source_id'], _hash(r['body']), case['fetch']['инн'], case['fetch']['инн'])
                                for r in case['recordings'])
    actual_captures = Counter((r['source_id'], r['sha256'], r['subject_inn'], r['request']['subject_inn'])
                              for r in refs['responses'].values())
    check('frozen_capture_inventory_mismatch', 'raw_refs', actual_captures == expected_captures)
    def text_binding(key, actual, expected):
        valid = actual == expected
        if not valid:
            strings = list(_strings(actual))
            for value in strings or [str(actual)]:
                check('unsupported_serialized_text', key, False, value)
        return valid
    def closed_keys(key, value, allowed):
        for extra in set(value) - set(allowed):
            text_binding(key + '.' + str(extra), value[extra], object())

    version = re.search(r'^version\s*=\s*"([^"]+)"', (ROOT / 'pyproject.toml').read_text(), re.MULTILINE)[1]
    text_binding('engine_version', payload['metadata']['engine_version'], version)
    text_binding('mode', payload['metadata']['mode'], 'полный')
    text_binding('case_clock', output['case_clock'], case.get('reload_at', CLOCK))

    # Exhaustive schemas cover every dynamic string previously promoted from
    # inspected output into a permitted rendering. Successful input/schema
    # checks add no assertion units; emitted unexpected prose always does.
    closed_keys('assessment', assessment, ('_decision', 'вопрос', 'вручную',
        'источники_обязательные_не_проверены', 'не_проверено_deal_killer',
        'не_проверено_обязательные_сигналы', 'поднят_сигналами', 'проверка_состоялась',
        'профиль', 'рекомендация', 'светофор', 'факты', 'формулировка', 'итог_fetch'))
    for key, expected in [('профиль', case['profile']), ('вопрос', profile.get('вопрос')),
                          ('формулировка', profile.get('формулировка')),
                          ('вручную', profile.get('вручную') or [])]:
        text_binding(key, assessment.get(key), expected)
    if 'итог_fetch' in assessment:
        text_binding('итог_fetch', assessment['итог_fetch'], source_summary.get('вывод'))
    check('invalid_assessment_light', 'светофор', assessment.get('светофор') in (None, '🟢', '🟡', '🔴'))
    actual_states = payload['fetch']['_evidence']['source_states']
    text_binding('source_states', actual_states, source_states)
    fin = payload['fin']
    closed_keys('fin', fin, ('_input_identity', 'статус', 'причина', 'источник', 'версия_оценок',
                            'оценки_сигналов', 'финансовый_профиль', 'флаги'))
    text_binding('fin.статус', fin.get('статус'), fin_oracle['status'])
    text_binding('fin.причина', fin.get('причина'), fin_oracle['reason'])
    if 'источник' in fin: text_binding('fin.источник', fin['источник'], 'ГИР БО')
    identity = fin.get('_input_identity', {})
    closed_keys('fin.identity', identity, ('rules_sha256', 'schema_version', 'scorer_sha256', 'sha256'))
    for key in ('rules_sha256', 'scorer_sha256', 'sha256'):
        check('invalid_financial_input_hash', key, bool(re.fullmatch(r'[a-f0-9]{64}', str(identity.get(key, '')))))
    evaluations = fin.get('оценки_сигналов') or {}
    text_binding('fin.evaluation_rules', sorted(evaluations), sorted(fin_oracle['reasons']))
    for sid, evaluation in evaluations.items():
        closed_keys('fin.' + sid, evaluation, ('причина', 'статус', 'зависимости', 'достаточные_зависимости'))
        text_binding('fin.reason.' + sid, evaluation.get('причина'), fin_oracle['reasons'].get(sid))
        expected_status = ('найден' if sid in fin_oracle['flags'] else
                           'не проверен' if fin_oracle['reasons'].get(sid) else 'отсутствует')
        text_binding('fin.status.' + sid, evaluation.get('статус'), expected_status)
        for key in ('зависимости', 'достаточные_зависимости'):
            for p in evaluation.get(key, []):
                valid = p == 'егрюл.уставный_капитал' or p == 'численность_сотрудников' or bool(re.fullmatch(r'(?:1300|1600|1200|1500|2110|2400):\d{4}', str(p)))
                if not valid: text_binding('fin.dependency.' + sid, p, None)
    frozen_balances = {str(r['год']): {code: round(float(value), 2) for code, value in r.get('строки', {}).items()
        if value is not None and type(value) in (int, float)}
        for r in (case['fetch'].get('финансы') or {}).get('отчётность_по_годам', [])}
    fp = fin.get('финансовый_профиль') or {}
    if fp:
        closed_keys('fin.profile', fp, ('года', 'не_проверено', 'оценено_лет', 'сводка',
            'строки_баланса', 'уставный_капитал_тыс_руб', 'численность', 'численность_год', 'численность_источник'))
        text_binding('fin.unverified', fp.get('не_проверено'), fin_oracle['unverified'])
        text_binding('fin.balances', fp.get('строки_баланса'), frozen_balances)
        n, red = len(fin_oracle['flags']), int('ча_отрицательные_2года' in fin_oracle['flags'])
        years = {r['год'] for r in (case['fetch'].get('финансы') or {}).get('отчётность_по_годам', [])}
        text_binding('fin.summary', fp.get('сводка'), 'флагов: %d (🔴 %d, 🟡 %d); оценено лет: %d' % (n, red, n-red, len(years)))
        if fp.get('численность_источник') is not None:
            check('invalid_staff_source', 'численность_источник', fp['численность_источник'] in ('риски', 'дампы_фнс', 'вход'))
        for key in ('оценено_лет', 'уставный_капитал_тыс_руб', 'численность', 'численность_год'):
            if fp.get(key) is not None and type(fp[key]) not in (int, float):
                text_binding('fin.numeric.' + key, fp[key], None)
        if any(type(y) is not int for y in fp.get('года', [])):
            text_binding('fin.years', fp.get('года'), sorted(years, reverse=True))
    flags = fin.get('флаги') or []
    text_binding('fin.flag_ids', sorted(f.get('id', '') for f in flags), sorted(fin_oracle['flags']))
    for flag in flags:
        sid = flag.get('id')
        closed_keys('fin.flag.' + str(sid), flag, ('id', 'severity', 'год', 'значение', 'источник', 'показатель', 'порог', 'пояснение', 'строки_баланса'))
        text_binding('fin.flag_tuple.' + str(sid), tuple(flag.get(k) for k in ('показатель', 'значение', 'порог', 'год')), fin_oracle['flags'].get(sid))
        text_binding('fin.flag_source.' + str(sid), flag.get('источник'), 'ГИР БО')
        text_binding('fin.flag_balances.' + str(sid), flag.get('строки_баланса'), frozen_balances)
        text_binding('fin.flag_severity.' + str(sid), flag.get('severity'), '🔴' if sid == 'ча_отрицательные_2года' else '🟡')
        explanation = contract['financial_explanations'].get(sid)
        if explanation and '{percent}' in explanation:
            explanation = explanation.format(percent='%.0f' % fin_oracle['flags'][sid][1])
        if explanation and '{staff}' in explanation:
            explanation = explanation.format(staff=int((case['fetch'].get('риски') or {}).get('численность_сотрудников', 0)))
        text_binding('fin.flag_explanation.' + str(sid), flag.get('пояснение'), explanation)

    closed_keys('decision', decision, ('schema_version', 'rules_version', 'decision_status', 'can_recommend',
        'missing_evidence', 'next_checks', 'claim_ids', 'coverage'))
    check('invalid_decision_status', 'decision_status', decision.get('decision_status') in ('supported', 'facts_only', 'insufficient_evidence'))
    check('decision_missing_evidence_conflict', 'decision_status', not decision['can_recommend'] or not decision['missing_evidence'])
    for gap in decision['missing_evidence']:
        closed_keys('gap', gap, ('rule_id', 'reason_codes', 'claim_ids'))
        sid = gap.get('rule_id')
        related = [claims[cid] for cid in gap.get('claim_ids', []) if cid in claims]
        expected_reasons = sorted({c['reason_code'] or c['knowledge'] for c in related}) or ['missing_required_field']
        valid_gap = sid in rubric['signal_catalogue'] and all(
            c['field'] in rubric['signal_catalogue'].get(sid, {}).get('откуда', []) for c in related)
        check('invalid_gap_rule_binding', str(sid), valid_gap)
        text_binding('gap.reason_codes', gap.get('reason_codes'), expected_reasons)
        for code in gap.get('reason_codes', []):
            if code not in reason_codes | {'unknown', 'conflict', 'not_applicable'}:
                text_binding('gap.reason_code', code, None)
    text_binding('decision.next_checks', decision.get('next_checks'), [
        {'rule_id': g['rule_id'], 'reason_codes': g['reason_codes']} for g in decision['missing_evidence']])

    # Compare captured leaf semantics with independently frozen input, not with
    # another scorer call or another renderer run. Explicit unavailable leaves
    # remain unavailable; they are not forced to invent a valid raw pointer.
    for cid, claim in claims.items():
        raw = claim['raw_ref']
        spec = rubric['evidence_field_contracts'].get(claim['field'], {})
        if re.fullmatch(r'финансы\.отчётность_по_годам\.(?:\d{4}|unknown)\.строки\.\d+', claim['field']):
            spec = rubric['annual_financial_contract']
        if not spec:
            check('claim_field_outside_frozen_catalogue', cid, False)
        recording, frozen_mapping = _frozen_leaf_binding(case, refs, claim['field'], raw)
        expected_source = frozen_mapping.get('source_id', spec.get('source_id'))
        check('invalid_claim_reason_code', cid, claim.get('reason_code') is None or claim['reason_code'] in reason_codes)
        if claim.get('reason_code') is not None and claim['reason_code'] not in reason_codes:
            text_binding('claim.reason_code', claim['reason_code'], None)
        check('invalid_claim_knowledge', cid, claim.get('knowledge') in ('known', 'unknown', 'conflict', 'not_applicable'))
        check('invalid_claim_dependency_binding', cid, all(d in claims for d in claim['depends_on']))
        manual = (case.get('manual') or {}).get(spec.get('source_id'))
        check('invalid_claim_metadata', cid, claim['source_id'] == expected_source and
              claim['origin_family'] == (None if manual else spec.get('origin_family')) and
              claim['freshness_policy'] == ('unknown_v1' if manual else spec.get('freshness_policy')) and
              claim['retrieved_at'] == output['case_clock'])
        if raw['availability'] == 'available':
            valid = recording is not None
            if recording is not None:
                mapping = frozen_mapping
                valid = valid and raw['locator'] == mapping['locator']
                valid = valid and claim['search_scope'] == mapping.get('search_scope')
                valid = valid and claim['coverage_complete'] == mapping.get('coverage_complete')
                # Annual periods are claim semantics even when not supplied as a
                # mapping date: source field/record year is independently fixed.
                period = mapping.get('period')
                annual = re.fullmatch(r'финансы\.отчётность_по_годам\.(\d{4})\.строки\.\d+', claim['field'])
                spec = rubric['evidence_field_contracts'].get(claim['field'], {})
                year = int(annual[1]) if annual else None
                if annual:
                    spec = rubric['annual_financial_contract']
                elif claim['field'].endswith('.численность_сотрудников'):
                    holder = case['fetch'].get(claim['field'].split('.')[0]) or {}
                    years = [holder[k] for k in ('среднесписочная_за_год', 'численность_год') if holder.get(k) is not None]
                    if not years and claim['field'].startswith('дампы_фнс.') and holder.get('год'):
                        years = [holder['год']]
                    if years and len({str(y) for y in years}) == 1 and re.fullmatch(r'\d{4}', str(years[0])):
                        year = int(years[0])
                if period is None and year is not None:
                    period = {'start': '%04d-01-01' % year, 'end': '%04d-12-31' % year}
                valid = valid and claim['period'] == period
                valid = valid and claim['source_id'] == mapping.get('source_id', spec.get('source_id'))
                valid = valid and claim['origin_family'] == (None if mapping.get('relation') == 'manual_observation' else spec.get('origin_family'))
                valid = valid and claim['freshness_policy'] == spec.get('freshness_policy')
                valid = valid and claim['retrieved_at'] == output['case_clock']
                for date_field in ('effective_at', 'published_at', 'first_observed_at'):
                    expected_date = CLOCK if date_field == 'first_observed_at' else None
                    if mapping.get('date_locators', {}).get(date_field):
                        expected_date = mapping.get(date_field)
                        valid = valid and pointer(recording['body'], mapping['date_locators'][date_field]) == expected_date
                    valid = valid and claim[date_field] == expected_date
                policy = rubric['freshness_policies'][spec['freshness_policy']]
                basis = mapping.get(policy['basis']) if policy['basis'] else None
                expiry = None
                if policy['ttl_seconds'] and basis and mapping.get('date_locators', {}).get(policy['basis']):
                    expiry = (datetime.fromisoformat(basis.replace('Z', '+00:00')) +
                              timedelta(seconds=policy['ttl_seconds'])).isoformat().replace('+00:00', 'Z')
                valid = valid and claim['valid_until'] == expiry
                valid = valid and claim['transformation'] == mapping.get('transformation', 'identity_v1')
                valid = valid and claim['relation'] == mapping.get('relation', 'primary')
                valid = valid and claim['upstream_dataset'] == mapping.get('upstream_dataset')
                try:
                    value = pointer(recording['body'], mapping['locator'])
                    if claim['knowledge'] == 'known' and claim['transformation'] == 'identity_v1':
                        valid = valid and type(value) is type(claim['value']) and value == claim['value']
                except (KeyError, IndexError, TypeError, ValueError):
                    valid = False
            check('frozen_leaf_binding_mismatch', cid, valid)
        else:
            check('invalid_unavailable_raw_trace', cid,
                  raw['availability'] in ('raw_unavailable', 'unmapped_locator') and
                  all(raw[k] is None for k in ('response_id', 'sha256', 'locator')))
            # No locator means no source evidence for publication/effective
            # date/expiry. Adaptation time cannot be relabelled historical time.
            expected_observed = None if manual or claim['relation'] == 'derived' or claim['value'] in (None, '') else output['case_clock']
            check('unavailable_claim_date_binding_mismatch', cid,
                  claim['first_observed_at'] == expected_observed and
                  all(claim[k] is None for k in ('effective_at', 'published_at', 'valid_until')))
            expected_scope, expected_completeness = None, None
            if manual:
                canonical_block = {'инн': case['fetch']['инн'], 'ввод': 'браузер',
                    'дата_проверки': manual['дата_проверки'], 'url': manual['url'], 'проверил': 'пользователь',
                    'параметры_поиска': manual['параметры_поиска'], 'статус': 'не проверено: ' + manual['reason'],
                    'outcome': 'declined', 'expires_at': None, 'scope_declared': True}
                block_sha = hashlib.sha256(json.dumps(canonical_block, ensure_ascii=False, sort_keys=True,
                    separators=(',', ':')).encode()).hexdigest()
                scope = claim.get('search_scope') or {}
                ref = scope.get('manual_observation', {}).get('block_ref', {})
                check('invalid_manual_revision_binding', cid, type(ref.get('revision')) is int and ref['revision'] >= 2)
                expected_scope = {'subject_inn': case['fetch']['инн'], 'query': manual['параметры_поиска'],
                    'manual_observation': {'block_ref': {'run_id': output['run_id'], 'revision': ref.get('revision'),
                        'source_id': claim['source_id'], 'locator': '/fetch/' + claim['source_id'], 'block_sha256': block_sha},
                        'source_url': manual['url'], 'observed_on': manual['дата_проверки'], 'date_precision': 'day',
                        'imported_at': output['case_clock'], 'expires_at': None, 'freshness': 'unknown'}}
                expected_completeness = False
            check('unavailable_claim_metadata_binding_mismatch', cid,
                  claim['search_scope'] == expected_scope and claim['coverage_complete'] == expected_completeness and
                  claim['transformation'] in ('identity_v1', 'number_v1', 'integer_v1') and
                  claim['relation'] == ('manual_observation' if manual else 'derived' if claim['field'].startswith('fin.') else 'legacy_untraced') and
                  claim['upstream_dataset'] is None)

        if not claim['field'].startswith('fin.'):
            definitions = [(recording, frozen_mapping)] if recording is not None else []
            mapping = frozen_mapping
            if manual:
                definitions = []
                value = manual.get(claim['field'].split('.', 1)[1])
            elif definitions:
                value = pointer(definitions[0][0]['body'], mapping['locator'])
            else:
                value = case['fetch']
                for part in claim['field'].split('.'):
                    if isinstance(value, list):
                        value = next((r for r in value if isinstance(r, dict) and str(r.get('год')) == part), None)
                    else:
                        value = value.get(part) if isinstance(value, dict) else None
            transform = mapping.get('transformation', 'identity_v1')
            schema_invalid = False
            numeric = claim['field'].endswith('численность_сотрудников') or claim['field'].startswith('финансы.отчётность_по_годам.')
            if transform in ('number_v1', 'integer_v1') or numeric and value is not None:
                try:
                    if isinstance(value, bool): raise TypeError('boolean_number')
                    value = float(str(value).replace(' ', '').replace('\u00a0', '').replace(',', '.').replace('руб.', '').replace('₽', ''))
                    if not math.isfinite(value): raise ValueError('non_finite')
                    if transform == 'integer_v1': value = int(value)
                    if claim['field'].endswith('численность_сотрудников') and (value < 0 or not float(value).is_integer()):
                        schema_invalid = True
                except (ValueError, TypeError):
                    schema_invalid = True
            reason, knowledge = ('schema_invalid', 'unknown') if schema_invalid else (None, 'known')
            if value is None or value == '': reason, knowledge = 'missing_required_field', 'unknown'
            negative = (value is False or isinstance(value, (dict, list)) and not value or
                        isinstance(value, str) and value.strip().lower() in contract['negative_value_literals'])
            holder = case['fetch'].get(expected_source) or {}
            # A verified mapped provider need not also have a legacy availability
            # declaration. Explicit unavailable states still veto negatives, and
            # their complete subject-bound search scope is checked below.
            source_available = (source_states.get(expected_source, {}).get('состояние') in ('ok', 'пусто')
                if expected_source in case['fetch'].get('_доступность', {}) else
                not str(holder.get('статус') or '').lower().startswith(('не проверено', 'проверено частично')))
            if (knowledge == 'known' and not source_available
                    and not (definitions and mapping.get('effective_at') and not negative)):
                reason, knowledge = 'source_unavailable', 'unknown'
            if knowledge == 'known' and (negative or spec.get('requires_scope_for_zero') and value == 0):
                scope = mapping.get('search_scope')
                if mapping.get('coverage_complete') is not True or not isinstance(scope, dict) or scope.get('subject_inn') != case['fetch']['инн'] or not scope.get('query'):
                    reason, knowledge = 'coverage_incomplete', 'unknown'
            policy = rubric['freshness_policies'].get(spec.get('freshness_policy'), {})
            if knowledge == 'known' and policy.get('ttl_seconds'):
                basis = mapping.get(policy['basis']) if mapping.get('date_locators', {}).get(policy['basis']) else None
                if not basis:
                    reason, knowledge = 'revision_unknown', 'unknown'
                elif datetime.fromisoformat(output['case_clock'].replace('Z', '+00:00')) >= datetime.fromisoformat(basis.replace('Z', '+00:00')) + timedelta(seconds=policy['ttl_seconds']):
                    reason, knowledge = 'stale', 'unknown'
            if knowledge == 'known' and not definitions: reason = 'raw_unavailable'
            # Conflicts require two frozen candidate observations with differing
            # values for this field. The conflict wrapper never permits prose.
            frozen_values = [pointer(r['body'], r['mappings'][claim['field']]['locator']) for r in case['recordings'] if claim['field'] in r['mappings']]
            if knowledge == 'known' and frozen_values and any(v != frozen_values[0] for v in frozen_values[1:]):
                knowledge, reason = 'conflict', None
            expected_claim_states[cid] = (knowledge, reason)
            check('frozen_leaf_knowledge_mismatch', cid, claim['knowledge'] == knowledge and claim['reason_code'] == reason)
            if not schema_invalid:
                check('frozen_leaf_value_mismatch', cid, claim['value'] == value and (type(claim['value']) is bool) == (type(value) is bool))

    # Derived financial diagnostics are constrained by frozen form policy,
    # independent arithmetic text operands and the independently checked leaves.
    for cid, claim in claims.items():
        if not claim['field'].startswith('fin.'): continue
        sid = claim['field'].split('.', 1)[1]
        records = (case['fetch'].get('финансы') or {}).get('отчётность_по_годам') or []
        year = max((int(r['год']) for r in records), default=None)
        fields = set()
        for operand in rubric['financial_rule_inputs'][sid]:
            if ':' in operand:
                code, relative = operand.split(':')
                operand_year = (year if relative == 'current' else year - 1) if year else 'unknown'
                fields.add('финансы.отчётность_по_годам.%s.строки.%s' % (operand_year, code))
            elif operand == 'численность_сотрудников':
                fields.update(s + '.' + k for s, keys in (
                    ('риски', ('численность_сотрудников', 'среднесписочная_за_год', 'численность_год')),
                    ('дампы_фнс', ('численность_сотрудников', 'численность_год', 'год'))) for k in keys)
            else:
                fields.add(operand)
        if sid == 'транзитный_профиль' and year:
            current = next(r for r in records if int(r['год']) == year)
            previous = next((r for r in records if int(r['год']) == year-1), {})
            rev, prev_rev = current.get('строки', {}).get('2110'), previous.get('строки', {}).get('2110')
            revenue_proves_false = type(rev) in (int, float) and type(prev_rev) in (int, float) and prev_rev > 0 and (rev-prev_rev)/prev_rev <= 3
            staff_source = next((s for s in ('риски', 'дампы_фнс') if
                source_states.get(s, {}).get('состояние') == 'ok' and
                type((case['fetch'].get(s) or {}).get('численность_сотрудников')) in (int, float)), None)
            if revenue_proves_false:
                fields = {f for f in fields if f.startswith('финансы.')}
            elif staff_source:
                holder = case['fetch'][staff_source]
                years = [staff_source + '.' + k for k in ('среднесписочная_за_год', 'численность_год') if holder.get(k) is not None]
                staff_fields = {staff_source + '.численность_сотрудников', *(years or [staff_source + '.численность_год'])}
                fields = {f for f in fields if f.startswith('финансы.')} | staff_fields
        expected_deps = sorted(c['claim_id'] for c in claims.values() if c['field'] in fields)
        check('frozen_derived_dependency_mismatch', cid, sorted(claim['depends_on']) == expected_deps)
        form = (case['fetch'].get('_reporting') or {}).get('reporting_form') or (case['fetch'].get('финансы') or {}).get('форма_отчётности')
        deps = [claims[d] for d in claim['depends_on'] if d in claims]
        knowledge, reason = 'known', None
        if form == 'ip' and len(case['fetch']['инн']) == 12:
            knowledge, reason = 'not_applicable', 'not_measurable_for_form'
        elif form == 'restricted': knowledge, reason = 'unknown', 'not_disclosed'
        elif form not in ('full', 'simplified'): knowledge, reason = 'unknown', 'unsupported_form'
        elif any(expected_claim_states[d['claim_id']][0] == 'conflict' for d in deps): knowledge, reason = 'conflict', 'conflicting_values'
        elif sid == 'транзитный_профиль' and any(d['field'].endswith('численность_сотрудников') and d['period'] != claim['period'] for d in deps):
            knowledge, reason = 'unknown', 'period_mismatch'
        elif not deps or any(expected_claim_states[d['claim_id']][0] != 'known' for d in deps) or fin_oracle['reasons'].get(sid):
            knowledge, reason = 'unknown', 'missing_required_field'
        expected_claim_states[cid] = (knowledge, reason)
        check('frozen_derived_knowledge_mismatch', cid, (claim['knowledge'], claim['reason_code']) == (knowledge, reason))
        if knowledge == 'known':
            check('frozen_derived_value_mismatch', cid, claim['value'] is (sid in fin_oracle['flags']))

    # Independent signal vocabulary is frozen before evaluation. A changed
    # assessment fact string does not become a new text oracle.
    def reason_allowed(reason, rule):
        atoms = {expected_reason or expected_knowledge for cid, (expected_knowledge, expected_reason)
                 in expected_claim_states.items() if claims[cid]['field'] in rule['откуда'] and
                 (expected_reason in reason_codes or expected_knowledge in ('unknown', 'conflict', 'not_applicable')) and
                 cid not in invalid_support}
        atoms |= {'не применимо', 'not_measurable_for_form', 'причина не указана',
                  'к ИП не применимо (см. references/scenarii.md «Проверка ИП»)'}
        for path in rule['откуда']:
            source, field = path.split('.', 1)
            atoms.update({source + ': блок «' + source + '» отсутствует в fetch-JSON',
                          source + ': поле ' + field + ' не отдано (null внутри ok-блока)',
                          source + ': не проверено'})
            state = source_states.get(source, {})
            if isinstance(state, dict) and state.get('причина'):
                atoms.add(source + ': ' + state['причина'])
        atoms.update(r for sid, r in fin_oracle['reasons'].items() if r and 'fin.' + sid in rule['откуда'])
        if fin_oracle['reason']:
            atoms.add(fin_oracle['reason'])
        # These diagnostics are non-affirmative, but additions are still rejected:
        # no wildcard language inside an 'unknown' wrapper is allowed.
        return reason in atoms or all(part in atoms for part in reason.removeprefix('частично: ').split('; '))

    fact_texts = set()
    fact_groups = {'найден': [], 'отсутствует': [], 'не проверен': [], 'не применимо': []}
    def truth(value):
        if value is None: return None
        if isinstance(value, str): return value.strip().lower() not in set(contract['negative_value_literals']) | {''}
        if isinstance(value, dict):
            if 'статус' in value:
                return None if str(value['статус']).lower().startswith('не') else truth(value.get('данные'))
            for key in ('rows', 'записи', 'items', 'data'):
                if isinstance(value.get(key), list): return bool(value[key])
            for key in ('total', 'count', 'всего', 'записей'):
                if type(value.get(key)) in (int, float): return value[key] > 0
        return bool(value)
    def signal_truth(sid, claim):
        value = claim['value']
        if sid == 'ликвидация':
            if claim['field'].endswith('дата_прекращения'): return bool(value)
            if isinstance(value, str): return any(marker in value.lower() for marker in contract['liquidation_markers'])
        if sid == 'не_в_мсп': return 'в реестре' not in value.lower() if isinstance(value, str) else None
        if sid == 'молодая_компания':
            try:
                date = datetime.strptime(value[:10], '%d.%m.%Y' if '.' in value[:10] else '%Y-%m-%d').date()
                reference = datetime.fromisoformat((claim['effective_at'] or claim['retrieved_at']).replace('Z', '+00:00')).date()
                return (reference - date).days < 365
            except (ValueError, TypeError): return None
        return truth(value)
    for fact in assessment['факты']:
        sid, status = fact['сигнал'], fact['статус']
        closed_keys('fact.' + sid, fact, ('сигнал', 'статус', 'текст', 'источник', 'дата', 'claim_ids', 'evidence', 'неполно', 'оговорка'))
        rule = rubric['signal_catalogue'].get(sid)
        bound = [claims[cid] for cid in fact.get('claim_ids', []) if cid in claims]
        if not rule:
            check('fact_outside_frozen_catalogue', sid, False)
            continue
        if status not in fact_groups:
            text_binding('fact.status.' + sid, status, None)
            continue
        desc = rule['описание']
        valid_binding = all(c['field'] in rule['откуда'] for c in bound)
        check('fact_field_binding_mismatch', sid, valid_binding)
        candidates = [c for c in claims.values() if c['field'] in rule['откуда']]
        known = [c for c in candidates if c['knowledge'] == 'known']
        na = (len(case['fetch']['инн']) == 12 and rule.get('применимо_к') == ['юрлицо'] or
              bool(candidates) and all(c['knowledge'] == 'not_applicable' for c in candidates))
        expected_status = 'не применимо' if na else 'не проверен'
        if not na and not any(c['knowledge'] == 'conflict' for c in candidates):
            outcomes = [signal_truth(sid, c) for c in known]
            if any(result is True for result in outcomes): expected_status = 'найден'
            elif any(result is False for result in outcomes) and not (sid == 'банкротство' and len(known) != len(candidates)):
                expected_status = 'отсутствует'
        check('fact_status_outside_frozen_support', sid, status == expected_status)
        evidence_keys = ('claim_id', 'field', 'knowledge', 'reason_code', 'raw_ref', 'period', 'search_scope', 'coverage_complete')
        expected_evidence = [{k: c[k] for k in evidence_keys} for c in candidates]
        if status == 'не применимо' and len(case['fetch']['инн']) == 12 and rule.get('применимо_к') == ['юрлицо']:
            expected_evidence = []
        text_binding('fact.evidence.' + sid, fact.get('evidence'), expected_evidence)
        date = fact.get('дата') or 'дата неизвестна'
        source = fact.get('источник') or 'источник неизвестен'
        if status in ('найден', 'отсутствует'):
            source_ids = {p.split('.')[0].replace('fin', 'финансы') for p in rule['откуда']}
            allowed_sources = {rubric['source_names'].get(s, s) for s in source_ids}
            check('fact_source_binding_mismatch', sid, source in allowed_sources)
            dates = {None}
            for sid_source in source_ids:
                dates.add((case['fetch'].get(sid_source) or {}).get('дата_проверки'))
                state = case['fetch'].get('_доступность', {}).get(sid_source, {})
                if isinstance(state, dict):
                    dates.add(state.get('дата'))
            check('fact_date_binding_mismatch', sid, fact.get('дата') in dates)
        if status == 'отсутствует':
            allowed = {f'{desc}: не выявлено — {source}, {date}'}
        elif status == 'найден':
            values = [c['value'] for c in bound if c['knowledge'] == 'known']
            suffixes = set()
            for value in values:
                suffixes.add('' if value is None or value is True else
                             ' (' + (json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else str(value)) + ')')
            # Financial numeric explanatory tuples are taken from the structured
            # scorer diagnostic; their truth is gated by the claim and frozen
            # atomic labels, not by accepting arbitrary free text in that tuple.
            if sid in fin_oracle['flags']:
                suffixes.add(' (%s = %s, порог %s, %s)' % fin_oracle['flags'][sid])
            allowed = {f'{desc}{suffix} — {source}, {date}' for suffix in suffixes}
        else:
            prefix = desc + (': не проверено (' if status == 'не проверен' else ': ')
            candidate = fact.get('текст', '')
            reason = candidate[len(prefix):-1] if status == 'не проверен' else candidate[len(prefix):]
            valid = candidate.startswith(prefix) and reason_allowed(reason, rule)
            valid = valid and (status != 'не проверен' or candidate.endswith(')'))
            allowed = {candidate} if valid else set()
        text = fact.get('текст', '')
        check('fact_wording_outside_frozen_oracle', sid, text in allowed)
        if text not in allowed:
            text_binding('fact.text.' + sid, text, None)
            continue
        if fact.get('неполно'):
            check('unvalidated_partial_reason', sid, reason_allowed(str(fact['неполно']), rule))
            text += ' (доказано не всеми источниками — ' + str(fact['неполно']).replace('частично: ', '', 1) + ')'
        if fact.get('оговорка'):
            source_id = next((source for source, name in rubric['source_names'].items() if name == fact.get('источник')), None)
            caveat = rubric['source_metadata'].get(source_id, {}).get('пусто_с_оговоркой')
            valid_caveat = status == 'отсутствует' and source_states.get(source_id, {}).get('состояние') == 'пусто' and fact['оговорка'] == caveat
            check('fact_caveat_outside_frozen_source', sid, valid_caveat)
            if not valid_caveat:
                text_binding('fact.caveat.' + sid, fact['оговорка'], caveat)
            else:
                text += ' (оговорка источника: ' + caveat + ')'
        fact_texts.add(text)
        fact_groups[status].append(text)

    # Raised signal order and wording have one independent production grammar:
    # highest applicable severity among found facts, then catalogue IDs. Never
    # use returned descriptions or returned traffic light as text permissions.
    def severity(sid):
        rule = rubric['signal_catalogue'][sid]
        return '🔴' if sid in profile['deal_killers'] else profile['severity_override'].get(sid, rule['severity_по_умолчанию'])
    found = [f for f in assessment['факты'] if f['статус'] == 'найден' and f['сигнал'] in rubric['signal_catalogue']]
    red = sorted((f for f in found if severity(f['сигнал']) == '🔴'), key=lambda f: f['сигнал'])
    yellow = sorted((f for f in found if severity(f['сигнал']) == '🟡'), key=lambda f: f['сигнал'])
    raised_facts = red or yellow
    text_binding('raised_signal_ids', [s.get('сигнал') for s in assessment.get('поднят_сигналами', [])],
                 [f['сигнал'] for f in raised_facts])
    if red:
        check('light_contradicts_supported_facts', 'Вердикт', assessment.get('светофор') == '🔴')
    elif yellow and assessment.get('проверка_состоялась'):
        check('light_contradicts_supported_facts', 'Вердикт', assessment.get('светофор') == '🟡')
    if decision['can_recommend']:
        template = rubric['recommendation_templates'][case['profile']]
        factual = (template['found_prefix'] + '; '.join(rubric['signal_catalogue'][f['сигнал']]['описание'] for f in raised_facts) + '.'
                   if raised_facts else template['no_found_facts'])
        recommendation = factual + ' ' + template['actions'].get(assessment.get('светофор'), '')
    else:
        recommendation = contract['abstention_template'].format(gaps=', '.join(g['rule_id'] for g in decision['missing_evidence']) or 'полноту обязательных источников')
        if not assessment.get('проверка_состоялась'):
            recommendation = 'Проверка не состоялась. ' + recommendation
    text_binding('assessment.recommendation', assessment.get('рекомендация'), recommendation)

    unknown_facts = [f for f in assessment['факты'] if f['статус'] == 'не проверен']
    required_unknown = [f['сигнал'] for f in unknown_facts if severity(f['сигнал']) != '⚪' and (
        f['сигнал'] in profile['deal_killers'] or profile['severity_override'].get(f['сигнал']) == '🔴' or
        any(p.split('.')[0].replace('fin', 'финансы') in profile['источники_обязательные'] for p in rubric['signal_catalogue'][f['сигнал']]['откуда']))]
    # Lists in assessments follow catalogue extraction order, independently of
    # the sorted final fact presentation.
    catalogue_order = list(rubric['signal_catalogue'])
    required_unknown.sort(key=catalogue_order.index)
    dk_unknown = [sid for sid in catalogue_order if any(f['сигнал'] == sid for f in unknown_facts) and (
        sid in profile['deal_killers'] or profile['severity_override'].get(sid) == '🔴')]
    occurred = bool(source_summary.get('проверка_состоялась')) and not dk_unknown
    text_binding('assessment.check_occurred', assessment['проверка_состоялась'], occurred)
    missing_sources = [s for s in profile['источники_обязательные'] if source_states.get(s, {}).get('состояние') == 'не проверено']
    for sid in required_unknown:
        for path in rubric['signal_catalogue'][sid]['откуда']:
            source = path.split('.')[0].replace('fin', 'финансы')
            if source in profile['источники_обязательные'] and source not in missing_sources:
                missing_sources.append(source)
    for key, expected in [('источники_обязательные_не_проверены', missing_sources),
                          ('не_проверено_deal_killer', dk_unknown),
                          ('не_проверено_обязательные_сигналы', required_unknown)]:
        text_binding(key, assessment.get(key), expected)
    required = []
    for fact in assessment['факты']:
        sid = fact['сигнал']
        source_ids = {p.split('.')[0].replace('fin', 'финансы') for p in rubric['signal_catalogue'][sid]['откуда']}
        needed_rule = sid in profile['deal_killers'] or profile['severity_override'].get(sid) == '🔴' or (
            bool(source_ids & set(profile['источники_обязательные'])) and severity(sid) != '⚪')
        if needed_rule and fact['статус'] != 'не применимо': required.append(sid)
    supported_rules = sorted(f['сигнал'] for f in assessment['факты'] if f['сигнал'] in required and f['статус'] in ('найден', 'отсутствует'))
    text_binding('decision.coverage', decision['coverage'], {'numerator': len(supported_rules), 'denominator': len(required),
        'required_rules': sorted(required), 'supported_rules': supported_rules})
    can_recommend = (profile['формулировка'] != 'факты' and not decision['missing_evidence'] and bool(required)
                     and assessment['светофор'] is not None)
    text_binding('decision.can_recommend', decision['can_recommend'], can_recommend)
    text_binding('decision.status_binding', decision['decision_status'],
                 'facts_only' if profile['формулировка'] == 'факты' else 'supported' if can_recommend else 'insufficient_evidence')
    expected_ids = sorted({cid for f in assessment['факты'] if f['статус'] in ('найден', 'отсутствует') for cid in f.get('claim_ids', [])})
    text_binding('decision.claim_ids', decision['claim_ids'], expected_ids)

    needed = set(decision['claim_ids'])
    for gap in decision['missing_evidence']:
        needed.update(gap['claim_ids'])
    for fact in assessment['факты']:
        if fact['статус'] in ('найден', 'отсутствует'):
            needed.update(fact.get('claim_ids', []))
    pending = list(needed)
    while pending:
        for cid in claims.get(pending.pop(), {}).get('depends_on', []):
            if cid not in needed:
                needed.add(cid)
                pending.append(cid)
    evidence_tables = [b for b in model if b.get('шапка', [])[:1] == ['Claim ID']]
    rows = evidence_tables[0].get('строки', []) if len(evidence_tables) == 1 else []
    indexed = {}
    for row in rows:
        if isinstance(row, list) and len(row) == 4:
            indexed.setdefault(row[0], []).append(row)
    for cid in needed:
        claim = claims.get(cid)
        valid = claim is not None and len(indexed.get(cid, [])) == 1
        if valid:
            row, raw = indexed[cid][0], claim['raw_ref']
            expected = 'Период: %s; область поиска: %s; raw: %s %s %s %s; зависимости: %s' % (
                json.dumps(claim['period'], ensure_ascii=False), json.dumps(claim['search_scope'], ensure_ascii=False),
                raw['availability'], raw['response_id'], raw['sha256'], raw['locator'], ', '.join(claim['depends_on']))
            expected += '; полнота: %s; преобразование: %s; источник: %s; семейство: %s; получено: %s; наблюдалось: %s; действует: %s; опубликовано: %s; актуально до: %s; политика: %s' % tuple(
                claim[k] for k in ('coverage_complete', 'transformation', 'source_id', 'origin_family', 'retrieved_at',
                                  'first_observed_at', 'effective_at', 'published_at', 'valid_until', 'freshness_policy'))
            valid = row == [cid, claim['field'], claim['knowledge'] + ':' + str(claim['reason_code'] or ''), expected]
        check('assertion_evidence_missing_from_dossier', cid, valid)
    check('unexpected_evidence_row', 'evidence-table', set(indexed) == needed and len(rows) == len(needed))

    static = set(contract['static_explanations'])
    # Conditional assertions are not unconditional boilerplate permissions.
    conditional = {
        'Непроверенных сигналов нет.': not fact_groups['не проверен'],
        'Нет признаков, по которым источники дали явный отрицательный ответ.': not fact_groups['отсутствует'],
        'Сигналов из каталога в отвечавших источниках не найдено. Это утверждение действует только в пределах раздела 2 — с учётом раздела 3.': not fact_groups['найден'],
        'Блока «_итог_проверки» во входном файле нет: подтвердить, что проверка состоялась, нечем. Отсутствие итога — это «не проверено», а не «всё в порядке».': not source_summary,
        'Во входном файле нет блока «_доступность»: состояния источников неизвестны. Ни одна строка таблицы выше не означает, что источник был опрошен и что-то ответил.': not source_states,
        'Все источники реестра ответили: состояние каждого приведено в разделе 2.': bool(source_states) and all(s['состояние'] in ('ok', 'пусто') for s in source_states.values()),
        'Перечень источников не построен: реестр источников недоступен, а блока «_доступность» во входном файле нет. Что именно проверялось, по этому документу установить нельзя.': not source_states,
        'Перечень источников не построен: реестр источников недоступен, а состояний во входном файле нет. Это НЕ значит, что источники ответили, — о них просто нет сведений.': not source_states,
        'Исходный локальный запуск сохранён. Сырые ответы, вложенные копии, производные выводы и метаданные исключены из экспорта. Решение не пересчитывалось.': False,
        'Ниже перечислены неразвёрнутые связи. Отсутствие найденной связи здесь не означает, что её нет.': False,
        'Общий адрес, директор или учредитель — виды связи. Они сами по себе не доказывают владение или контроль.': False,
        'Основание: сохранённая политика провайдера; разрешения поставщика отдельно не проверены.': False,
    }
    static.difference_update(k for k, v in conditional.items() if not v)
    paragraphs = set(static)
    if source_summary.get('вывод'):
        paragraphs.add('Вывод о полноте проверки: ' + source_summary['вывод'])
    paragraphs.add('Статус решения: %s; покрытие %s/%s; рекомендации: %s' % (
        decision['decision_status'], decision['coverage']['numerator'], decision['coverage']['denominator'], decision['can_recommend']))
    paragraphs.add('Рекомендация связана с утверждениями: ' + ', '.join(decision['claim_ids']))
    for gap in decision['missing_evidence']:
        paragraphs.add('Пробел %s: %s; %s' % (gap['rule_id'], ', '.join(gap['reason_codes']), ', '.join(gap['claim_ids'])))
    expected_conflicts = []
    groups = {}
    for cid, claim in claims.items():
        if claim['value'] is not None and claim['knowledge'] in ('known', 'conflict'):
            groups.setdefault(_hash([claim['field'], claim['period']]), []).append(claim)
    for group in groups.values():
        if any(c['value'] != group[0]['value'] for c in group[1:]):
            expected_conflicts.append({'field': group[0]['field'], 'period': group[0]['period'],
                'candidate_claim_ids': sorted(c['claim_id'] for c in group),
                'selected_claim_id': None, 'reason_code': 'conflicting_values'})
    canonical = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    expected_conflicts.sort(key=canonical)
    text_binding('evidence.conflicts', payload['fetch']['_evidence']['conflicts'], expected_conflicts)
    for conflict in expected_conflicts:
        paragraphs.add('Конфликт: ' + json.dumps(dict(sorted(conflict.items())), ensure_ascii=False))
    paragraphs.add('Перечень профиля «%s»: чего в открытых данных не видно и что запрашивается у самого контрагента. Инструмент этого не проверял.' % case['profile'])
    paragraphs.add('Документ сформирован автоматически: inn-check-ru %s (https://github.com/ilyautov/inn-check-ru), %s. Данные — открытые источники, перечисленные в разделе 2; sha256 исходного JSON приведён в разделе 1. Ответственность за решение по сделке несёт лицо, принимающее решение.' % (payload['metadata']['engine_version'], output['case_clock']))

    known_lists = fact_texts | static | set(profile.get('вручную') or [])
    for raised in assessment.get('поднят_сигналами', []):
        closed_keys('raised', raised, ('сигнал', 'severity', 'описание', 'источник', 'дата', 'значение'))
        sid = raised.get('сигнал')
        rule = rubric['signal_catalogue'].get(sid, {})
        description = rule.get('описание')
        severity = '🔴' if sid in profile['deal_killers'] else profile['severity_override'].get(sid, rule.get('severity_по_умолчанию'))
        matching = next((f for f in assessment['факты'] if f['сигнал'] == sid and f['статус'] == 'найден'), None)
        valid_raised = matching is not None and sid not in invalid_support
        valid_raised = valid_raised and all(raised.get(k) == matching.get(k) for k in ('источник', 'дата'))
        valid_raised = valid_raised and raised.get('описание') == description and raised.get('severity') == severity
        if matching:
            bound_values = [claims[cid]['value'] for cid in matching.get('claim_ids', []) if cid in claims]
            valid_value = raised.get('значение') in bound_values
            if sid in fin_oracle['flags']:
                valid_value = raised.get('значение') == dict(zip(('показатель', 'значение', 'порог', 'год'), fin_oracle['flags'][sid]))
            valid_raised = valid_raised and valid_value
        check('raised_signal_outside_frozen_binding', str(sid), valid_raised)
        if not valid_raised:
            text_binding('raised.value.' + str(sid), raised.get('значение'), None)
        if valid_raised:
            known_lists.add('%s %s — %s, %s' % (severity, description, display(matching.get('источник')), display(matching.get('дата'))))
    if fin_oracle['status'] == 'не проверено':
        known_lists.add('финансовый разбор: не проверено — ' + display(fin_oracle['reason']))
    for item in fin_oracle['unverified']:
        known_lists.add('финансовый разбор: ' + item)
    if assessment.get('источники_обязательные_не_проверены'):
        required_sources = assessment['источники_обязательные_не_проверены']
        valid_sources = all(s in profile['источники_обязательные'] for s in required_sources)
        check('invalid_required_source_ids', 'required_sources', valid_sources)
        if valid_sources:
            known_lists.add('обязательные источники профиля без данных: ' + display(required_sources))

    if not decision['can_recommend']:
        abstention = contract['abstention_template'].format(gaps=', '.join(g['rule_id'] for g in decision['missing_evidence']) or 'полноту обязательных источников')
        if not assessment.get('проверка_состоялась'):
            abstention = 'Проверка не состоялась. ' + abstention
        check('abstention_outside_frozen_text_oracle', 'Рекомендация', assessment.get('рекомендация') == abstention)

    state_text = {'ok': 'источник ответил, данные разобраны', 'пусто': 'источник ответил, записей по контрагенту нет', 'не проверено': 'данных нет — см. раздел 3'}
    tables = {}
    tables['Источник'] = [[sid, '—', 'неизвестно', state['состояние'] + ' (' + state_text[state['состояние']] + ')', display(state.get('дата')), '—'] for sid, state in source_states.items()]
    tables['Почему не проверено'] = [[sid, '—', 'неизвестно', state['состояние'], display(state.get('требует')), display(state.get('причина')) if state.get('причина') else 'причина не указана источником'] for sid, state in source_states.items() if state['состояние'] == 'не проверено']
    def rubles(v):
        if type(v) not in (int, float):
            return display(v)
        return (f'{v * 1000:,.0f}'.replace(',', '\u00a0') + ' руб.')
    tables['Год'] = [[str(year['год']), *[rubles(year.get(k)) for k in ('выручка', 'прибыль_убыток', 'активы')], rubles(year.get('строки', {}).get('1300')), display(year.get('дата_отчётности'))] for year in (case['fetch'].get('финансы') or {}).get('отчётность_по_годам', [])]
    tables['Что закрыто'] = contract['legal_explanations']
    tables['Критерий'] = []
    for label, ids, manual in contract['due_diligence_criteria']:
        states = [source_states.get(s, {}).get('состояние', 'сведений во входе нет') for s in ids]
        n = sum(s in ('ok', 'пусто') for s in states)
        tables['Критерий'].append([label, 'проверено %s из %s источников' % (n, len(ids)), '; '.join(s + ' — ' + state for s, state in zip(ids, states)), '; '.join(manual)])

    fetch = case['fetch']
    registry = fetch.get('егрюл', {})
    identity = {'Наименование полное': registry.get('наименование_полное') or (fetch.get('риски') or {}).get('наименование') or registry.get('наименование'),
        'Наименование краткое': registry.get('наименование_краткое') or registry.get('наименование_полное') or registry.get('наименование'), 'ИНН': fetch['инн'], 'ОГРН': registry.get('огрн'), 'КПП': registry.get('кпп'), 'Тип': fetch.get('тип'),
        'Статус по данным источников': registry.get('статус'), 'Основной ОКВЭД': (fetch.get('риски') or {}).get('оквэд'), 'Дата регистрации': registry.get('дата_регистрации'), 'Регион': registry.get('регион'), 'Адрес': registry.get('адрес'), 'Руководитель / управляющая организация': registry.get('руководитель')}
    pairs = {k: display(v) for k, v in identity.items()}
    pairs.update({'Документ сформирован': output['case_clock'], 'Дата ответов источников': '—', 'Инструмент': 'inn-check-ru %s, https://github.com/ilyautov/inn-check-ru' % payload['metadata']['engine_version'], 'Профиль проверки': case['profile'], 'Режим сбора': payload['metadata']['mode'],
        'Профиль': case['profile'], 'Вопрос профиля': profile.get('вопрос'), 'Вердикт': assessment.get('светофор') or 'не выдаётся — проверка не состоялась (см. раздел 3)', 'Рекомендация': recommendation or '—',
        'Итог финансового разбора': 'не проверено — ' + display(fin_oracle['reason']) if fin_oracle['status'] == 'не проверено' else fin_oracle['status'],
        'Флагов найдено': str(len(fin_oracle['flags'])), 'Флаги финансового разбора': 'не подсчитывались: разбор не состоялся, см. раздел 3'})
    total = source_summary
    for label, key in [('Источников опрошено', 'источников'), ('Ответили', 'ok'), ('Ответили пустым результатом', 'пусто'), ('Не проверено', 'не_проверено')]:
        pairs[label] = display(total.get(key))
    pairs['Проверка состоялась'] = 'да' if total.get('проверка_состоялась') else 'нет'
    headings = {'1. Кто проверен и когда', 'Когда и чем проверено', '2. Что проверено', 'Бухгалтерская отчётность (ГИР БО) и финансовый разбор', 'Финансовый разбор', '3. Что НЕ проверено и почему', '3.1. Источники, не давшие данных', '3.2. Сигналы, оставшиеся непроверенными', '4. Что найдено в источниках', '4.1. Найденные сигналы', '4.2. Признаки, которых источники не показали', '4.3. Неприменимые признаки', '4.3. Вердикт профиля', '4.4. Вердикт профиля', 'Доказательства решения и пробелы', '5. Границы этого документа', '5.1. Что закрыто законом и не видно ни в одном открытом источнике', '5.2. Подпись'}
    headings.add('Досье должной осмотрительности — %s (ИНН %s)' % (pairs['Наименование полное'], fetch['инн']))
    for n in range(3, 6):
        headings.update({'3.%s. %s' % (n, title) for title in ('Прочие пробелы', 'Критерии должной осмотрительности (письма ФНС)', 'Что инструмент не проверяет — запросить и проверить вручную')})

    # Parse complete Markdown surface into line atoms independently. The model
    # is accepted only through the semantic checks above/below, not trusted as
    # an oracle merely because both transports agree.
    expected_lines = []
    emitted_facts = Counter()
    seen_paragraphs = Counter()
    def clean(s, cell=False):
        s = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', str(s))
        s = re.sub(r'[\r\n\x85\u2028\u2029]+', ' ', s).strip()
        return s.replace('|', '\\|') if cell else s
    for i, block in enumerate(model):
        typ = block.get('тип')
        allowed_keys = {'абзац': {'тип', 'текст', 'жирный', 'курсив'}, 'список': {'тип', 'пункты'}, 'заголовок': {'тип', 'текст', 'уровень'}, 'поля': {'тип', 'пары'}, 'таблица': {'тип', 'шапка', 'строки'}}
        for key in set(block) - allowed_keys[typ] if typ in allowed_keys else ():
            for value in _strings(block[key]):
                check('unsupported_serialized_text', '%s:%s' % (i, key), False, value,
                      location='$.model[%s].%s' % (i, key))
            check('unknown_model_key', str(i), False, key)
        if typ == 'абзац':
            value = block.get('текст', '')
            seen_paragraphs[value] += 1
            valid = value in paragraphs and seen_paragraphs[value] == 1
            if value not in static or not valid:
                check('unsupported_rendered_paragraph', str(i), valid, value, location='$.model[%s].текст' % i)
            value = clean(value)
            if block.get('жирный'): value = '**' + value + '**'
            if block.get('курсив'): value = '_' + value + '_'
            expected_lines.append(value)
        elif typ == 'список':
            for j, value in enumerate(block.get('пункты', [])):
                if value in fact_texts:
                    emitted_facts[value] += 1
                check('unsupported_rendered_list_assertion', '%s:%s' % (i, j), value in known_lists and not any(f['сигнал'] in invalid_support or any(cid in invalid_support for cid in f.get('claim_ids', [])) for f in assessment['факты'] if value.startswith(rubric['signal_catalogue'][f['сигнал']]['описание'])), value,
                      location='$.model[%s].пункты[%s]' % (i, j))
                expected_lines.append('- ' + clean(value))
        elif typ == 'заголовок':
            check('unsupported_rendered_heading', str(i), block.get('текст') in headings, block.get('текст'),
                  location='$.model[%s].текст' % i)
            expected_lines.append('#' * block.get('уровень', 2) + ' ' + clean(block.get('текст', '')))
        elif typ == 'поля':
            for j, (label, value) in enumerate(block.get('пары', [])):
                valid = label in pairs and value == pairs[label] and label not in invalid_support
                if label == 'Исходный файл': valid = bool(re.fullmatch('run:' + re.escape(output['run_id']) + r'/revision:\d+', str(value)))
                if label == 'sha256 исходного файла': valid = bool(re.fullmatch(r'[a-f0-9]{64}', str(value)))
                if label == 'Размер исходного файла': valid = bool(re.fullmatch(r'\d+ байт', str(value)))
                check('unsupported_rendered_field', label, valid, str(value) if label in pairs else str(label) + ': ' + str(value),
                      location='$.model[%s].пары[%s]' % (i, j))
                expected_lines.append('- **' + clean(label) + ':** ' + clean(value))
        elif typ == 'таблица':
            header, actual_rows = block.get('шапка', []), block.get('строки', [])
            if header not in contract['table_headers']:
                check('unrecognized_table_header', str(i), False)
                known_cells = {cell for cells in contract['table_headers'] for cell in cells}
                for j, cell in enumerate(header):
                    if cell not in known_cells:
                        check('unsupported_serialized_text', str(i), False, str(cell), location='$.model[%s].шапка[%s]' % (i, j))
            if header[:1] != ['Claim ID']:
                key = 'Почему не проверено' if header[-1:] == ['Почему не проверено'] else (header[0] if header else '')
                check('unsupported_rendered_table', str(i), key in tables and actual_rows == tables[key], key)
                for j, actual in enumerate(actual_rows):
                    check('unsupported_rendered_table_row', '%s:%s' % (i, j), key in tables and j < len(tables[key]) and actual == tables[key][j], ' | '.join(str(v) for v in actual),
                          location='$.model[%s].строки[%s]' % (i, j))
            else:
                for j, actual in enumerate(actual_rows):
                    check('unsupported_rendered_table_row', actual[0], actual[0] in needed and actual[0] not in invalid_support, ' | '.join(str(v) for v in actual),
                          location='$.model[%s].строки[%s]' % (i, j))
            expected_lines.extend(['| ' + ' | '.join(clean(x, True) for x in header) + ' |', '|' + '|'.join('---' for _ in header) + '|'])
            expected_lines.extend('| ' + ' | '.join(clean(x, True) for x in row) + ' |' for row in actual_rows)
        else:
            check('unknown_rendered_block', str(i), False, typ)
            for value in _strings(block):
                check('unsupported_serialized_text', str(i), False, value, location='$.model[%s]' % i)
    # Sequence, not substring membership: removed/extra/reordered atoms cannot
    # be masked by another appendix occurrence of the same claim ID.
    for text in fact_texts:
        check('rendered_fact_multiplicity_mismatch', text, emitted_facts[text] == 1)
    lines = [line for line in output['markdown'].splitlines() if line.strip()]
    if lines != expected_lines:
        delta = Counter(lines) - Counter(expected_lines)
        missing_lines = Counter(expected_lines) - Counter(lines)
        if not delta:
            failures.append({'code': 'markdown_order_or_missing_atoms'})
        for i, line in enumerate(lines):
            if delta[line]:
                check('unvalidated_markdown_atom', line, False, line, location='$.markdown[%s]' % i)
                delta[line] -= 1
        if missing_lines:
            failures.append({'code': 'missing_markdown_atoms', 'detail': sum(missing_lines.values())})
    return assertions, failures


def _projection_semantic_audit(case, output, primary_assertions):
    """Audit declared real origins through the same independent prose oracle."""
    from collections import Counter
    serialized = output.get('serialized', {})
    origins = {'payload': (('read', 'data'), ('mcp_read', 'data')),
        'model': (('model', 'data', 'content'), ('mcp_model', 'data', 'content')),
        'markdown': (('markdown', 'data', 'content'), ('mcp_text', 'data', 'content')),
        'actions': (('read', 'pending_actions'), ('mcp_read', 'pending_actions'))}
    assertions, failures = [], []
    contract = _typed_contract(case)['output']
    for channel_index in (0, 1):
        shadow, available = dict(output), {}
        for projection, paths in origins.items():
            path = paths[channel_index]
            origin = serialized
            for key in path:
                if type(origin) is not dict or key not in origin:
                    break
                origin = origin[key]
            else:
                if decision_schema.validate(origin, contract['required'][projection]):
                    continue  # A nullable error envelope is not a canonical view.
                shadow[projection] = origin
                available[projection] = decision_schema.same(origin, output[projection])
        if not available or (all(available.values()) and (channel_index == 0 or all(a['supported'] for a in primary_assertions))):
            continue
        # Retain each channel's payload/model/Markdown/action context together;
        # its returned text still receives only the independent input/rule oracle.
        audited = primary_assertions if all(available.values()) else _presentation_audit(case, shadow)[0]
        for projection, same in available.items():
            if channel_index == 0 and same:
                continue
            path, prefix = origins[projection][channel_index], '$.' + projection
            def on_surface(atom, prefix=prefix):
                return atom['location'] == prefix or atom['location'].startswith((prefix + '.', prefix + '['))
            primary = [a for a in primary_assertions if not a['supported'] and on_surface(a)]
            candidates = [a for a in audited if not a['supported'] and on_surface(a)]
            # Only common occurrences in the same canonical/CLI origin are
            # subtracted. Distinct locations and real MCP emissions remain.
            common = Counter((a['location'], a['kind'], a['unit']) for a in primary) if channel_index == 0 else Counter()
            emitted = []
            for atom in candidates:
                identity = (atom['location'], atom['kind'], atom['unit'])
                if common[identity]:
                    common[identity] -= 1
                else:
                    emitted.append(dict(atom, location='$.serialized.' + '.'.join(path) + atom['location'][len(prefix):]))
            assertions.extend(emitted)
            if emitted:
                failures.append({'code': 'unsupported_serialized_projection', 'detail': '.'.join(path)})
    return assertions, failures


def _summarize(case, output):
    inspected = output
    if type(output) is not dict:
        output = {}
    def metric_value(key):
        value = output.get(key, 0)
        return value if type(value) in (int, float) and 0 <= value <= 1e308 and math.isfinite(value) else 0
    row = {'id': case['id'], 'segment': case['segment'], 'synthetic': True, 'oracle_status': case['oracle_status'],
        'channels': ['workflow_cli_json', 'mcp_worker_json', 'dossier_model', 'dossier_markdown', 'snapshot'],
        'error_code': output.get('error_code'), 'failures': [], 'can_recommend': False,
        'case_clock': output.get('case_clock', case.get('reload_at', CLOCK)),
        'coverage': {'numerator': 0, 'denominator': 0}, 'legacy_coverage': {'numerator': 0, 'denominator': 0},
        'abstention_reasons': [], 'asserted': 0, 'facts': 0, 'unsupported': 0, 'contradicted': 0,
        'rendered_assertions': 0, 'output_assertions': [], 'recommendation_sentences': [], 'false_green': 0, 'next_checks': 0, 'executable_next_checks': 0,
        'elapsed_ms': metric_value('elapsed_ms'), 'recorded_requests': metric_value('recorded_requests'),
        'manual_steps': metric_value('manual_steps'), 'semantic_decision': None, 'origin_families': {}, 'annual_periods': {}}
    if output.get('contract_failures'):
        row['failures'] = output['contract_failures']
        row['output_assertions'] = output['contract_assertions']
        row['rendered_assertions'] = row['unsupported'] = len(row['output_assertions'])
        row['serialized_parity'] = output.get('contract_parity', {})
        return row
    if output.get('error_code'):
        if output['error_code'] != case.get('expected_error'):
            row['failures'].append({'code': 'unexpected_pipeline_error', 'detail': output['error_code']})
        elif len(output.get('channel_errors', {})) != 7:
            row['failures'].append({'code': 'raw_loss_not_closed_on_every_channel'})
        row['channel_errors'] = output.get('channel_errors', {})
        return row
    if case.get('expected_error'):
        row['failures'].append({'code': 'expected_error_not_emitted'})
    audited, violations, binding_failures = _output_shape_audit(case, inspected)
    row['failures'].extend(binding_failures)
    if violations:
        row['failures'].extend(violations)
        row['output_assertions'] = audited
        row['rendered_assertions'] = row['unsupported'] = len(audited)
        return row
    payload = output['payload']
    assessment = payload['assessment']
    decision = payload['metadata']['decision']
    claims = payload['fetch']['_evidence']['claims']
    model_text = '\n'.join(_strings(output['model']))
    text = output['markdown']
    row['can_recommend'] = decision['can_recommend']
    row['coverage'] = {k: decision['coverage'][k] for k in ('numerator', 'denominator')}
    row['abstention_reasons'] = sorted({r for m in decision['missing_evidence'] for r in m['reason_codes']})
    row['semantic_decision'] = {'can_recommend': decision['can_recommend'], 'status': decision['decision_status'],
        'light': assessment['светофор'], 'coverage': row['coverage'],
        'facts': {f['сигнал']: f['статус'] for f in assessment['факты']}}
    for c in claims.values():
        if c['knowledge'] == 'known' and c['origin_family']:
            row['origin_families'].setdefault(c['field'], set()).add(c['origin_family'])
        if c.get('period'):
            row['annual_periods'][c['field']] = c['period']
    row['origin_families'] = {k: sorted(v) for k, v in sorted(row['origin_families'].items())}
    def fail(code, detail=None):
        row['failures'].append({'code': code, **({'detail': detail} if detail is not None else {})})
    if ec.validate_evidence(payload['fetch']):
        fail('invalid_final_evidence_graph')
    for channel, same in output['parity'].items():
        if not same:
            fail('serialized_channel_disagreement', channel)
    # Recompute retained channel parity; saved carrier flags cannot attest to
    # values that have since changed. Prose inventory uses independent semantic
    # audits below, so JSON tags and structural keys are never invented units.
    serialized = output.get('serialized', {})
    for left, right in (('read', 'mcp_read'), ('model', 'mcp_model'), ('markdown', 'mcp_text')):
        if left in serialized and right in serialized and not decision_schema.same(serialized[left], serialized[right]):
            fail('serialized_channel_disagreement', left)
    if decision != assessment['_decision'] or decision != payload['fetch']['_decision']:
        fail('decision_binding_lost')
    def supported(cid, seen=None):
        seen = set() if seen is None else seen
        if cid in seen or cid not in claims:
            return False
        c = claims[cid]
        if c['knowledge'] != 'known':
            return False
        return all(supported(d, seen | {cid}) for d in c['depends_on'])
    for expected in case['expected_atomic_claims']:
        actual = [c for c in claims.values() if c['field'] == expected['field']]
        if not actual or not any(all(c.get(k) == v for k, v in expected.items()) for c in actual):
            fail('atomic_oracle_disagreement', expected)
            row['contradicted'] += 1
    if 'expected_can_recommend' in case and decision['can_recommend'] != case['expected_can_recommend']:
        fail('recommendation_oracle_disagreement')
    for fact in assessment['факты']:
        row['facts'] += 1
        if fact['статус'] not in ('найден', 'отсутствует'):
            continue
        row['asserted'] += 1
        ids = fact.get('claim_ids', [])
        unsupported = not ids or not all(supported(cid) for cid in ids)
        if unsupported:
            fail('unsupported_final_assertion', fact['сигнал'])
        # Count statements, not failed edges: one assertion has at most one error.
        for cid in ids:
            if cid not in model_text or cid not in text:
                unsupported = True
                fail('assertion_evidence_missing_from_dossier', cid)
        row['unsupported'] += int(unsupported)
        boolean_values = [claims[cid]['value'] for cid in ids if cid in claims and type(claims[cid]['value']) is bool]
        if boolean_values and len(boolean_values) == len(ids):
            contradiction = (fact['статус'] == 'найден' and not any(boolean_values) or
                             fact['статус'] == 'отсутствует' and any(boolean_values))
            if contradiction:
                row['contradicted'] += 1
                fail('assertion_contradicts_bound_value', fact['сигнал'])
    missing = decision['missing_evidence']
    required_unknown = any(f['сигнал'] in decision['coverage']['required_rules'] and
        (f['статус'] == 'не проверен' or f['статус'] in ('найден', 'отсутствует') and
         (not f.get('claim_ids') or not all(supported(cid) for cid in f['claim_ids'])))
        for f in assessment['факты'])
    if assessment['светофор'] == '🟢' and (missing or required_unknown):
        row['false_green'] = 1
        fail('false_green_required_unknown')
    recommendation = assessment.get('рекомендация') or ''
    if not decision['can_recommend']:
        if not missing or not row['abstention_reasons'] or 'Недостаточно подтверждённых данных' not in recommendation:
            fail('unexplained_abstention')
        for gap in missing:
            if gap['rule_id'] not in text or not any(r in text for r in gap['reason_codes']):
                fail('abstention_missing_from_text', gap['rule_id'])
    else:
        sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', recommendation) if s.strip()]
        rubric = json.loads((ROOT / 'benchmark/decision-rubric.json').read_text())
        template = rubric['recommendation_templates'][case['profile']]
        raised = assessment.get('поднят_сигналами') or []
        factual = (template['found_prefix'] + '; '.join(s['описание'] for s in raised) + '.'
                   if raised else template['no_found_facts'])
        allowed = factual + ' ' + template['actions'].get(assessment['светофор'], '')
        wording_supported = recommendation == allowed
        if not wording_supported:
            fail('recommendation_outside_frozen_text_oracle')
        # The dossier binds the entire recommendation explicitly to actual IDs.
        binding = 'Рекомендация связана с утверждениями: ' + ', '.join(decision['claim_ids'])
        for sentence in sentences:
            ids = decision['claim_ids']
            valid = bool(ids) and all(supported(cid) for cid in ids) and binding in model_text and binding in text
            valid = valid and sentence in model_text and sentence in text and wording_supported
            row['recommendation_sentences'].append({'text': sentence, 'claim_ids': ids, 'supported': valid})
            if not valid:
                row['unsupported'] += 1
                fail('unsupported_recommendation_sentence', sentence)
    for gap in missing:
        row['next_checks'] += 1
        wanted = {p.split('.')[0].replace('fin', 'финансы') for p in profiles.load()['сигналы'][gap['rule_id']]['откуда']}
        actions = [a for a in output['actions'] if a['source_id'] in wanted]
        executable = any(a.get('available') and a.get('url', '').startswith('https://') and
            a.get('query', {}).get('инн') == case['fetch']['инн'] and a.get('run_id') == output['run_id'] and
            a.get('action_id') and a.get('reason_code') for a in actions)
        if executable:
            row['executable_next_checks'] += 1
        elif not any(a.get('available') is False and a.get('unavailable_reason') for a in actions):
            fail('next_check_not_actionable_or_explained', gap['rule_id'])
    audited, output_failures = _presentation_audit(case, output)
    channel_atoms, channel_failures = _projection_semantic_audit(case, output, audited)
    row['output_assertions'] = audited + channel_atoms
    row['rendered_assertions'] = len(row['output_assertions'])
    row['unsupported'] += sum(not a['supported'] for a in row['output_assertions'])
    row['failures'].extend(output_failures)
    row['failures'].extend(channel_failures)
    # Matched-input comparison with public legacy resolver on identical evidence/fin/rules.
    legacy = profiles.resolve(copy.deepcopy(payload['fetch']), copy.deepcopy(payload['fin']), case['profile'])
    row['legacy_coverage'] = {k: legacy['_decision']['coverage'][k] for k in ('numerator', 'denominator')}
    row['legacy_abstention'] = not legacy['_decision']['can_recommend']
    row['capital_thousand_rub'] = payload['fin'].get('финансовый_профиль', {}).get('уставный_капитал_тыс_руб')
    return row


def evaluate(cases: list[dict], *, now: str) -> dict:
    """Run frozen labelled inputs; error/oracle exclusions never become successes."""
    if not isinstance(cases, list) or not ec._instant(now):
        raise ValueError('invalid_evaluation_input')
    manifest = verify_frozen()  # before any production execution
    frozen = {c['id']: _hash(c) for c in load_cases()}
    for case in cases:
        _validate_case(case)
        if frozen.get(case['id']) != _hash(case):
            raise ValueError('case_not_in_frozen_corpus')
    if len({c['id'] for c in cases}) != len(cases):
        raise ValueError('duplicate_case_id')
    engine_hash, rules_hash = _engine_hash(), _rules_hash()
    rows, exclusions = [], []
    for case in cases:
        with tempfile.TemporaryDirectory(prefix='inn-decision-eval-') as temp:
            try:
                output = run_case(case, now=now, root=Path(temp) / 'runs')
                row = _summarize(case, output)
            except (subprocess.SubprocessError, RuntimeError, ValueError, KeyError) as error:
                row = _summarize(case, {'error_code': 'evaluation_transport_error', 'elapsed_ms': 0})
                row['failures'].append({'code': 'evaluation_exception', 'exception_type': type(error).__name__})
        if row['failures']:
            # The artifact contains only frozen synthetic input and bounded diagnostics.
            row['minimal_failure'] = {'seed': SEED, 'clock': now, 'case_id': case['id'],
                'case_sha256': _hash(case), 'expected_atomic_claims': case['expected_atomic_claims'],
                'failures': row['failures'], 'reproduce': 'evaluate([case from frozen corpus], now=clock)'}
        if case['oracle_status'] != 'synthetic_specification':
            exclusions.append({'case_id': case['id'], 'reason': 'missing_independent_oracle', 'scope': 'quality_metrics'})
        if row['error_code']:
            exclusions.append({'case_id': case['id'], 'reason': row['error_code'], 'scope': 'quality_metrics',
                               'expected': row['error_code'] == case.get('expected_error')})
        rows.append(row)
    excluded = {e['case_id'] for e in exclusions}
    eligible = [r for r in rows if r['id'] not in excluded]
    def metric(n, d, unit='fraction'):
        return {'numerator': n, 'denominator': d, 'value': n/d if d else None, 'unit': unit}
    assertions = sum(r['asserted']+len(r['recommendation_sentences'])+r['rendered_assertions'] for r in eligible)
    metrics = {
        'unsupported_assertions': metric(sum(r['unsupported'] for r in eligible), assertions),
        'contradicted_assertions': metric(sum(r['contradicted'] for r in eligible), sum(len(c['expected_atomic_claims']) for c in cases if c['id'] not in excluded) + sum(r['asserted'] for r in eligible)),
        'asserted': metric(sum(r['asserted'] for r in eligible), sum(r['facts'] for r in eligible)),
        'false_green_required_unknown': metric(sum(r['false_green'] for r in eligible), len(eligible)),
        'coverage': metric(sum(r['coverage']['numerator'] for r in eligible), sum(r['coverage']['denominator'] for r in eligible)),
        'abstention': metric(sum(not r['can_recommend'] for r in eligible), len(eligible)),
        'abstention_explained': metric(sum(bool(r['abstention_reasons']) for r in eligible if not r['can_recommend']), sum(not r['can_recommend'] for r in eligible)),
        'next_check_executability': metric(sum(r['executable_next_checks'] for r in eligible), sum(r['next_checks'] for r in eligible)),
        'oracle_agreement': metric(sum(not r['failures'] for r in eligible), len(eligible)),
        'legacy_matched_input_coverage': metric(sum(r['legacy_coverage']['numerator'] for r in eligible), sum(r['legacy_coverage']['denominator'] for r in eligible)),
        'legacy_matched_input_abstention': metric(sum(r.get('legacy_abstention', True) for r in eligible), len(eligible)),
        'latency_ms': metric(sum(r['elapsed_ms'] for r in rows), len(rows), 'milliseconds_per_pipeline_attempt'),
        'recorded_requests': metric(sum(r['recorded_requests'] for r in rows), len(rows), 'recorded_responses_per_pipeline_attempt'),
        'manual_steps': metric(sum(r['manual_steps'] for r in rows), len(rows), 'manual_imports_per_pipeline_attempt'),
        'representative_frequency': metric(0, 0),
        'arbitrary_accounting_unit_conversion': metric(0, 0),
    }
    exclusions.extend([{'case_id': None, 'reason': 'no_representative_sample', 'scope': 'representative_frequency'},
                       {'case_id': None, 'reason': 'unsupported_feature', 'scope': 'arbitrary_accounting_unit_conversion'}])
    if _engine_hash() != engine_hash or _rules_hash() != rules_hash:
        raise RuntimeError('engine_changed_during_evaluation')
    return {'schema_version': 1, 'engine_hash': engine_hash, 'rules_hash': rules_hash,
        'corpus_hash': _hash({'manifest': manifest, 'selected_cases': [_hash(c) for c in cases]}),
        'seed': SEED, 'clock': now,
        'counts': {'input_cases': len(cases), 'evaluated_cases': len(rows), 'quality_eligible_cases': len(eligible),
            'oracle_excluded': sum(c['oracle_status'] != 'synthetic_specification' for c in cases),
            'pipeline_errors': sum(bool(r['error_code']) for r in rows), 'failed_cases': sum(bool(r['failures']) for r in rows),
            'synthetic_cases': len(cases), 'representative_cases': 0},
        'metrics': metrics, 'exclusions': exclusions, 'cases': rows}


def assert_gate(report):
    failures = [{'id': c['id'], 'failures': c['failures']} for c in report['cases'] if c['failures']]
    assert not failures, json.dumps(failures, ensure_ascii=False)
    for name in ('unsupported_assertions', 'contradicted_assertions', 'false_green_required_unknown'):
        assert report['metrics'][name]['numerator'] == 0, name


if __name__ == '__main__':
    report = evaluate(load_cases(), now=CLOCK)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    assert_gate(report)
