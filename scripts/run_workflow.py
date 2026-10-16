"""Local persisted check workflow. Only start/explicit refresh collect sources.

All public operations return schema-1 envelopes; storage roots are local host
configuration, never values taken from a manual block or a saved response.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import uuid
from datetime import date, datetime, timezone
from urllib.parse import urlsplit

import dossier
import evidence_contract
import fetch_counterparty as collector
import manual_block
import profiles
import run_store as store
import snapshot
import sources
from redaction import redact


class WorkflowError(Exception):
    def __init__(self, code, message='Некорректные параметры операции.'):
        self.code, self.message = code, message
        super().__init__(message)


def _now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _canonical(value):
    return store._json(value)


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def envelope(*, status='ok', run_id=None, revision=None, data=None, error=None,
             warnings=None, pending_actions=None):
    return {'schema_version': 1, 'status': status, 'run_id': run_id,
            'revision': revision, 'data': data, 'error': error,
            'warnings': warnings or [], 'pending_actions': pending_actions or []}


def failure(error, *, run_id=None, revision=None):
    if isinstance(error, (store.RunStoreError, WorkflowError)):
        code, message = error.code, error.message
    elif isinstance(error, (ValueError, TypeError, KeyError)):
        code, message = 'invalid_input', 'Входные данные не соответствуют контракту.'
    else:
        code, message = 'execution_error', 'Не удалось выполнить операцию проверки.'
    return envelope(status='error', run_id=run_id, revision=revision,
                    error={'code': code, 'message': message,
                           'retryable': code in {'storage_error', 'lock_timeout', 'execution_error'}})


def _profile(profile):
    if not isinstance(profile, str) or profile not in profiles.load()['профили']:
        raise WorkflowError('invalid_profile', 'Неизвестный профиль проверки.')
    return profile


def _url(source_id, value):
    if not isinstance(value, str) or len(value) > 2000 or any(ord(c) < 33 for c in value):
        raise WorkflowError('invalid_url', 'Нужна официальная ссылка источника.')
    try:
        parsed = urlsplit(value)
        allowed = {urlsplit(sources.SOURCES[source_id].get('документация', '')).hostname,
                   urlsplit(sources.SOURCES[source_id].get('probe', {}).get('url', '')).hostname}
        allowed = {host.removeprefix('www.') for host in allowed if host}
        host = (parsed.hostname or '').removeprefix('www.')
        if (parsed.scheme != 'https' or host not in allowed or parsed.username or parsed.password or
                parsed.port not in (None, 443) or parsed.fragment):
            raise ValueError
    except (ValueError, KeyError):
        raise WorkflowError('invalid_url', 'Ссылка не входит в официальный список источника.') from None
    return value


def _pending(record):
    payload = record['payload']
    fetch, metadata = payload['fetch'], payload['metadata']
    actions = []
    claims = fetch.get('_evidence', {}).get('claims', {})
    reasons = {}
    moment = datetime.now(timezone.utc)
    for claim in claims.values():
        expiry = claim.get('valid_until')
        if expiry and datetime.fromisoformat(expiry.replace('Z', '+00:00')) <= moment:
            reasons.setdefault(claim.get('source_id'), set()).add('stale')
        if claim.get('knowledge') == 'conflict':
            reasons.setdefault(claim.get('source_id'), set()).add('conflict')
        if claim.get('knowledge') in {'unknown', 'conflict'} or claim.get('freshness') == 'stale':
            reasons.setdefault(claim.get('source_id'), set()).add(claim.get('reason_code') or 'missing_required_field')
    required = set(profiles.sources_for(metadata.get('profile', 'нейтрально')))
    required.update(fetch.get('_доступность', {}))
    required.update(s for s in manual_block.браузерные() if s in sources.sources_for_profile(metadata.get('profile', 'нейтрально')))
    for sid in sources.SOURCES:
        if sid not in required and sid not in reasons:
            continue
        state, _, _ = profiles._состояние_блока(fetch, sid)
        codes = reasons.get(sid, set())
        if metadata.get('manual_outcomes', {}).get(sid) == 'completed' and state == 'ok' and not codes:
            expiry = (fetch.get(sid) or {}).get('expires_at')
            if not expiry or datetime.fromisoformat(expiry.replace('Z', '+00:00')) > moment:
                continue  # The manual task is done; evidence limitations remain in assessment.
            codes.add('stale')
        if state in {'ok', 'пусто'} and not codes:
            continue
        requirement = sources.SOURCES[sid].get('требует')
        kind = {'браузер': 'manual', 'ключ': 'key', 'кэш': 'cache'}.get(requirement, 'network')
        reason = min(codes) if codes else 'source_unavailable'
        if 'stale' in codes:
            kind, reason = 'refresh', 'stale'
        if 'conflict' in codes:
            kind = 'resolve_conflict'
        url = sources.SOURCES[sid].get('документация')
        try:
            url = _url(sid, url)
        except WorkflowError:
            url = None
        action = {'run_id': record['run_id'], 'revision': record['revision'], 'kind': kind,
                  'source_id': sid, 'reason_code': reason, 'url': url,
                  'query': {'инн': record['subject_inn']},
                  'required_fields': sorted(profiles._поля_сигналов(sid)) if kind == 'manual' else [],
                  'expires_at': None, 'available': url is not None,
                  'unavailable_reason': None if url else 'official_url_unavailable'}
        action['action_id'] = _digest(action)
        actions.append(action)
    return actions


def _result(record, *, data=None, status=None):
    metadata = record['payload']['metadata']
    actions = _pending(record)
    warnings = copy.deepcopy(metadata.get('warnings', []))
    workflow_status = metadata.get('workflow_status')
    if status is None:
        if workflow_status == 'cancelled':
            status = 'cancelled'
        elif workflow_status == 'collecting':
            status = 'partial'
            warnings.append({'code': 'interrupted', 'message': 'Сбор не завершён; сохранены доступные результаты. Для обновления нужна явная команда refresh.'})
        elif any(a['kind'] == 'manual' for a in actions):
            status = 'needs_input'
        elif actions or workflow_status == 'failed':
            status = 'partial'
        else:
            status = 'ok'
    return envelope(status=status, run_id=record['run_id'], revision=record['revision'],
                    data=record['payload'] if data is None else data, warnings=warnings,
                    pending_actions=actions)


def _materialize(fetch, previous=None, *, profile, mode, status, now=None):
    payload = evidence_contract.evaluate(fetch, profile=profile, now=now or _now())
    old = previous or {}
    payload['metadata'].update({k: copy.deepcopy(old[k]) for k in
        ('raw_refs', 'manual_outcomes', 'warnings') if k in old})
    payload['metadata'].update({'profile': profile, 'mode': mode, 'workflow_status': status})
    payload['metadata'].setdefault('raw_refs', {'responses': {}, 'fields': {}})
    return payload


def _retain_acquisition(payload, previous_fetch, replaced_sources):
    """Re-evaluation is not a new observation of an unchanged source candidate.

    Retain only acquisition instants after authoritative current evaluation.
    Knowledge, expiry, applicability, derived claims and support remain owned by
    the evaluator. ID alone is insufficient: it omits scope and policy metadata.
    """
    current_fetch = payload['fetch']
    previous = previous_fetch.get('_evidence', {}).get('claims', {})
    binding = ('subject_inn', 'field', 'value', 'unit', 'source_id', 'origin_family',
               'upstream_dataset', 'relation', 'raw_ref', 'period', 'freshness_policy',
               'transformation', 'search_scope', 'coverage_complete', 'published_at',
               'effective_at', 'valid_until')
    for cid, claim in current_fetch['_evidence']['claims'].items():
        sid = claim['source_id']
        prior = previous.get(cid)
        if (sid in replaced_sources or claim['relation'] == 'derived' or not prior
                or any(claim.get(key) != prior.get(key) for key in binding)
                or current_fetch.get(sid) != previous_fetch.get(sid)
                or current_fetch.get('_доступность', {}).get(sid) != previous_fetch.get('_доступность', {}).get(sid)
                or current_fetch.get('_evidence_scope', {}).get(claim['field']) !=
                   previous_fetch.get('_evidence_scope', {}).get(claim['field'])):
            continue
        for name in ('first_observed_at', 'retrieved_at'):
            claim[name] = prior[name]
    return payload


def _capture_refs(run_id, capture, fetch, previous, root):
    refs = capture.raw_refs(fetch)
    merged = copy.deepcopy(previous or {'responses': {}, 'fields': {}})
    for rid, response in refs['responses'].items():
        item = dict(response)
        body = item.pop('body')
        if store.put_raw(run_id, body, root=root) != item['sha256']:
            raise WorkflowError('capture_hash_mismatch', 'Нарушена целостность записанного ответа.')
        record = next(r for r in capture.responses if r['response_id'] == rid)
        item['original_response_preserved'] = record.get('original_response_preserved', False)
        merged['responses'][rid] = item
    for field, mappings in refs['fields'].items():
        old = merged['fields'].setdefault(field, [])
        for mapping in mappings:
            if mapping not in old:
                old.append(mapping)
    return merged


def _capture_provider_policy(fetch, capture):
    """Bind export permissions to collected data, retaining historical restrictions."""
    marker = collector.provider_metadata(capture)
    if marker is not None:
        fetch['_provider_policy'] = marker
    # An absent current host policy cannot release restrictions on saved values.


def start(inn: str, *, profile='нейтрально', mode='полный', root=None) -> dict:
    record = None
    try:
        if not isinstance(inn, str) or collector.validate_inn(inn) != inn:
            raise WorkflowError('invalid_inn', 'Нужен ИНН с верной контрольной суммой.')
        _profile(profile)
        if mode not in collector.РЕЖИМЫ:
            raise WorkflowError('invalid_mode', 'Неизвестный режим сбора.')
        fetch = {'инн': inn, 'профиль': profile, '_доступность': {}}
        initial = _materialize(fetch, profile=profile, mode=mode, status='collecting')
        record = store.create(initial, root=root)
        run_id = record['run_id']
        state = {'fetch': fetch, 'record': record}
        def raw_ready(response):
            store.put_capture(run_id, response, root=root)
        with collector.capture_session(inn, on_response=raw_ready) as capture:
            def progress(source_id, data, availability):
                state['fetch'][source_id] = data
                state['fetch']['_доступность'][source_id] = availability
                _capture_provider_policy(state['fetch'], capture)
                enriched = evidence_contract.enrich(state['fetch'], now=_now(), raw_refs=capture.raw_refs(state['fetch']))
                payload = _materialize(enriched, state['record']['payload']['metadata'],
                                       profile=profile, mode=mode, status='collecting')
                payload['metadata']['raw_refs'] = _capture_refs(run_id, capture, state['fetch'],
                    state['record']['payload']['metadata']['raw_refs'], root)
                state['record'] = store.append(run_id, payload, expected_revision=state['record']['revision'],
                                               operation_id=str(uuid.uuid4()), root=root)
            terminal = 'complete'
            with store.workflow_lock(run_id, root=root):
                try:
                    state['fetch'] = collector.collect(inn, профиль=profile, режим=mode, on_source=progress)
                except KeyboardInterrupt:
                    terminal = 'cancelled'
                except Exception:
                    terminal = 'failed'
                _capture_provider_policy(state['fetch'], capture)
                enriched = evidence_contract.enrich(state['fetch'], now=_now(), raw_refs=capture.raw_refs(state['fetch']))
                payload = _materialize(enriched, state['record']['payload']['metadata'],
                                       profile=profile, mode=mode, status=terminal)
                payload['metadata']['raw_refs'] = _capture_refs(run_id, capture, state['fetch'],
                    state['record']['payload']['metadata']['raw_refs'], root)
                if terminal == 'failed':
                    payload['metadata'].setdefault('warnings', []).append(
                        {'code': 'collection_failed', 'message': 'Сбор остановлен из-за ошибки; сохранены доступные ответы.'})
                record = store.append(run_id, payload, expected_revision=state['record']['revision'],
                                      operation_id=str(uuid.uuid4()), root=root)
        return _result(record)
    except Exception as error:
        return failure(error, run_id=record['run_id'] if record else None,
                       revision=record['revision'] if record else None)


def _read(run_id, *, revision=None, root=None):
    record = store.read(run_id, revision=revision, root=root)
    for response in record['payload']['metadata'].get('raw_refs', {}).get('responses', {}).values():
        try:
            store.read_raw(run_id, response['sha256'], root=root)
        except store.RunStoreError as error:
            if error.code == 'not_found':
                raise store.RunStoreError('corrupt_store', 'Записанный исходный ответ отсутствует.') from None
            raise
    return record


def resume(run_id: str, *, root=None) -> dict:
    try:
        record = _read(run_id, root=root)
        result = _result(record)
        recorded = store.captured_responses(run_id, root=root)
        committed = record['payload']['metadata'].get('raw_refs', {}).get('responses', {})
        unmaterialized = [r for r in recorded if r['response_id'] not in committed
                          and r['subject_inn'] == record['subject_inn']]
        if unmaterialized:
            result['warnings'].append({'code': 'unmaterialized_captures',
                'message': 'Ответы сохранены до завершения разбора; они не включены в оценку.',
                'responses': unmaterialized})
        return result
    except Exception as error:
        return failure(error)


def _operation(run_id, operation_id, expected_revision, request, root):
    store._id(operation_id, operation=True)
    store._revision(expected_revision)
    digest = _digest(request)
    previous = store.find_operation(run_id, operation_id, root=root)
    if previous:
        if previous['payload']['metadata'].get('operation', {}).get('request_sha256') != digest:
            raise WorkflowError('operation_conflict', 'Идентификатор операции уже связан с другим запросом.')
        return previous, digest, True
    current = _read(run_id, root=root)
    active = current['payload']['metadata'].get('active_operation', {})
    if active.get('operation_id') == operation_id:
        if active.get('request_sha256') != digest:
            raise WorkflowError('operation_conflict', 'Идентификатор операции уже связан с другим запросом.')
        if expected_revision in {active.get('base_revision'), current['revision']}:
            return current, digest, False
    if current['revision'] != expected_revision:
        raise WorkflowError('revision_conflict', 'Проверка изменилась; перечитайте текущую ревизию.')
    return current, digest, False


_MANUAL_COMMON = {'инн', 'ввод', 'дата_проверки', 'url', 'проверил', 'параметры_поиска',
                  'найдено', 'статус', 'причина', 'outcome', 'reason', 'expires_at',
                  'полно', 'полнота', 'pagination_complete', 'примечание',
                  'уведомления', 'залогодержатели', 'последнее'}


def _manual(blocks, record):
    if not isinstance(blocks, dict) or not blocks:
        raise WorkflowError('invalid_manual')
    safe, outcomes = {}, {}
    today = datetime.now(timezone.utc).date()
    for sid, block in blocks.items():
        if sid not in manual_block.ПОЛЯ or not isinstance(block, dict):
            raise WorkflowError('invalid_manual')
        if set(block) - _MANUAL_COMMON - set(manual_block.ПОЛЯ[sid]):
            raise WorkflowError('invalid_manual', 'Ручной блок содержит неподдерживаемые поля.')
        if block.get('инн') != record['subject_inn']:
            raise WorkflowError('subject_mismatch', 'ИНН ручного блока не совпадает с проверкой.')
        _url(sid, block.get('url'))
        observed = date.fromisoformat(block.get('дата_проверки', ''))
        if observed > today:
            raise WorkflowError('expired_manual', 'Дата ручной проверки находится в будущем.')
        expires = block.get('expires_at')
        if expires is not None and store._timestamp(expires) <= _now():
            raise WorkflowError('expired_manual', 'Срок ручной проверки истёк.')
        outcome = block.get('outcome')
        if outcome is None:
            outcome = 'completed' if block.get('статус') == 'проверено' else 'incomplete'
        if outcome not in {'completed', 'declined', 'incomplete'}:
            raise WorkflowError('invalid_manual')
        for flag in ('полно', 'pagination_complete'):
            if flag in block and type(block[flag]) is not bool:
                raise WorkflowError('invalid_manual', 'Полнота проверки должна быть логическим значением.')
        if 'полнота' in block and block['полнота'] not in ('полно', 'полная', 'complete', 'частично', 'incomplete'):
            raise WorkflowError('invalid_manual', 'Неизвестное значение полноты проверки.')
        if outcome == 'completed' and (block.get('полно') is False or block.get('pagination_complete') is False
                                       or block.get('полнота') in {'частично', 'incomplete'}):
            outcome = 'incomplete'
        fields = {k: block[k] for k in manual_block.ПОЛЯ[sid] if k in block}
        if outcome == 'completed' and any(k not in fields for k in profiles._поля_сигналов(sid)):
            outcome = 'incomplete'
        reason = block.get('reason') or block.get('причина') or block.get('примечание') or 'Проверка не завершена'
        canonical = manual_block.собрать(sid, record['subject_inn'], block['url'], observed.isoformat(),
            'проверено' if outcome == 'completed' else 'не проверено', fields,
            найдено=block.get('найдено'), параметры=block.get('параметры_поиска'),
            причина=reason, сегодня=today)
        canonical['outcome'] = outcome
        canonical['expires_at'] = expires
        for key in ('полно', 'полнота', 'pagination_complete'):
            if key in block:
                canonical[key] = block[key]
        # Only explicitly supplied search text is evidence of a declared scope.
        canonical['scope_declared'] = bool(isinstance(block.get('параметры_поиска'), str)
                                           and block['параметры_поиска'].strip())
        safe[sid], outcomes[sid] = canonical, outcome
    return safe, outcomes


def import_manual(run_id: str, blocks: dict, *, expected_revision: int,
                  operation_id: str, root=None) -> dict:
    try:
        request = {'kind': 'manual', 'blocks': blocks}
        with store.workflow_lock(run_id, root=root):
            current, digest, retry = _operation(run_id, operation_id, expected_revision, request, root)
            if retry:
                return _result(current)
            safe, outcomes = _manual(blocks, current)
            safe = redact(safe)
            fetch, _ = manual_block.вклеить(current['payload']['fetch'], safe)
            scope = fetch.setdefault('_evidence_scope', {})
            imported_at = _now()
            for sid, block in safe.items():
                complete = (block.get('pagination_complete', block.get('полно'))
                            if 'pagination_complete' in block or 'полно' in block
                            else True if block.get('полнота') in {'полно', 'полная', 'complete'} else None)
                if outcomes[sid] != 'completed':
                    complete = False
                for field in manual_block.ПОЛЯ[sid]:
                    scope[sid + '.' + field] = {
                        'relation': 'manual_observation',
                        'search_scope': {'subject_inn': current['subject_inn'], 'query': block['параметры_поиска']}
                            if block['scope_declared'] else None,
                        'coverage_complete': complete,
                        'imported_at': imported_at,
                        'observation_ref': {'run_id': run_id, 'revision': current['revision'] + 1,
                                            'source_id': sid, 'locator': '/fetch/' + sid,
                                            'block_sha256': _digest(block)},
                    }
            metadata = current['payload']['metadata']
            refs = copy.deepcopy(metadata['raw_refs'])
            refs['fields'] = {field: mappings for field, mappings in refs['fields'].items()
                              if field.split('.', 1)[0] not in safe}
            hydrated = copy.deepcopy(refs)
            for response in hydrated['responses'].values():
                response['body'] = store.read_raw(run_id, response['sha256'], root=root)
            fetch.pop('_evidence', None)
            fetch.pop('_decision', None)
            fetch = evidence_contract.enrich(fetch, now=_now(), raw_refs=hydrated)
            payload = _materialize(fetch, metadata, profile=metadata['profile'], mode=metadata['mode'], status='complete')
            _retain_acquisition(payload, current['payload']['fetch'], set(safe))
            payload['metadata']['raw_refs'] = refs
            payload['metadata'].setdefault('manual_outcomes', {}).update(outcomes)
            payload['metadata']['operation'] = {'kind': 'manual', 'request_sha256': digest}
            return _result(store.append(run_id, payload, expected_revision=expected_revision,
                                        operation_id=operation_id, root=root))
    except Exception as error:
        return failure(error)


def _selected(source_ids):
    if (not isinstance(source_ids, list) or not source_ids or
            any(not isinstance(s, str) or s not in sources.SOURCES for s in source_ids)):
        raise WorkflowError('invalid_source', 'Нужны идентификаторы источников из реестра.')
    result, visiting = [], set()
    def add(sid):
        if sid in result:
            return
        if sid in visiting:
            raise WorkflowError('invalid_source', 'Циклические зависимости источников.')
        visiting.add(sid)
        for prerequisite in sources.depends_on(sid):
            add(prerequisite)
        visiting.remove(sid)
        result.append(sid)
    for sid in source_ids:
        add(sid)
    return result


def refresh(run_id: str, source_ids: list[str], *, expected_revision: int,
            operation_id: str, root=None) -> dict:
    try:
        selected = _selected(source_ids)
        request = {'kind': 'refresh', 'source_ids': sorted(set(source_ids))}
        with store.workflow_lock(run_id, root=root):
            current, digest, retry = _operation(run_id, operation_id, expected_revision, request, root)
            if retry:
                return _result(current)
            metadata = current['payload']['metadata']
            fetch = copy.deepcopy(current['payload']['fetch'])
            active = metadata.get('active_operation')
            if not active or active.get('operation_id') != operation_id:
                active = {'kind': 'refresh', 'operation_id': operation_id,
                          'request_sha256': digest, 'base_revision': current['revision'],
                          'completed_sources': []}
                initial = copy.deepcopy(current['payload'])
                initial['metadata']['workflow_status'] = 'collecting'
                initial['metadata']['active_operation'] = active
                initial['metadata']['requested_sources'] = source_ids
                initial['metadata']['refreshed_sources'] = []
                current = store.append(run_id, initial, expected_revision=current['revision'],
                                       operation_id=str(uuid.uuid4()), root=root)
                metadata = current['payload']['metadata']
            from cache_policy import configure_readers
            configure_readers()
            completed = list(active['completed_sources'])
            terminal = 'complete'
            with collector.capture_session(current['subject_inn'],
                    on_response=lambda r: store.put_capture(run_id, r, root=root)) as capture:
                def checkpoint(status, *, final=False, replaced_sources=()):
                    nonlocal current, metadata
                    # Only completed provider values belong to this capture.
                    # A continued refresh may have completed the aggregator in
                    # an earlier capture; its saved policy must remain intact.
                    if 'агрегатор' in newly_completed:
                        _capture_provider_policy(fetch, capture)
                    fetch['_итог_проверки'] = collector.build_summary(fetch.get('_доступность', {}))
                    # Retain archived bytes but replace mappings only for refreshed sources.
                    previous_refs = copy.deepcopy(metadata['raw_refs'])
                    previous_refs['fields'] = {field: mappings for field, mappings in previous_refs['fields'].items()
                                               if field.split('.', 1)[0] not in newly_completed}
                    refs = _capture_refs(run_id, capture, fetch, previous_refs, root)
                    # A captured response is archived immediately, but belongs to
                    # this materialization only after its source returned a result.
                    pending_ids = {r['response_id'] for r in capture.responses
                                   if r['источник'] not in newly_completed}
                    for response_id in pending_ids:
                        refs['responses'].pop(response_id, None)
                    refs['fields'] = {field: [m for m in mappings if m['response_id'] not in pending_ids]
                                      for field, mappings in refs['fields'].items()}
                    hydrated = copy.deepcopy(refs)
                    for response in hydrated['responses'].values():
                        response['body'] = store.read_raw(run_id, response['sha256'], root=root)
                    fetch.pop('_evidence', None)
                    fetch.pop('_decision', None)
                    enriched = evidence_contract.enrich(fetch, now=_now(), raw_refs=hydrated)
                    payload = _materialize(enriched, metadata, profile=metadata['profile'], mode=metadata['mode'], status=status)
                    _retain_acquisition(payload, current['payload']['fetch'], set(replaced_sources))
                    payload['metadata']['raw_refs'] = refs
                    payload['metadata']['refreshed_sources'] = list(completed)
                    payload['metadata']['requested_sources'] = source_ids
                    if final:
                        payload['metadata']['operation'] = {'kind': 'refresh', 'request_sha256': digest}
                    else:
                        payload['metadata']['active_operation'] = {**active, 'completed_sources': list(completed)}
                    current = store.append(run_id, payload, expected_revision=current['revision'],
                                           operation_id=operation_id if final else str(uuid.uuid4()), root=root)
                    metadata = current['payload']['metadata']
                newly_completed = []
                try:
                    for sid in selected:
                        if sid in completed:
                            continue
                        data, av = collector.run_source(sid, current['subject_inn'], контекст=fetch)
                        safe_pair = redact({'data': data, 'availability': av}, secrets=capture.secrets)
                        data, av = safe_pair['data'], safe_pair['availability']
                        fetch[sid] = data
                        fetch.setdefault('_доступность', {})[sid] = av
                        fetch['_evidence_scope'] = {field: scope for field, scope in fetch.get('_evidence_scope', {}).items()
                                                     if field.split('.', 1)[0] != sid}
                        metadata.get('manual_outcomes', {}).pop(sid, None)
                        completed.append(sid)
                        newly_completed.append(sid)
                        checkpoint('collecting', replaced_sources=(sid,))
                except KeyboardInterrupt:
                    terminal = 'cancelled'
                except Exception:
                    terminal = 'failed'
                checkpoint(terminal, final=True)
                return _result(current)
    except Exception as error:
        return failure(error)


def _document(record):
    payload = record['payload']
    metadata = payload['metadata']
    meta = {'сформирован': metadata.get('evaluated_at', record['created_at']),
            'версия': metadata.get('engine_version', dossier.версия_инструмента()),
            'профиль': metadata.get('profile'), 'режим': metadata.get('mode'),
            'файл': 'run:%s/revision:%s' % (record['run_id'], record['revision']),
            'хеш': record['manifest']['input_sha256'], 'размер': len(_canonical(payload))}
    blocks = dossier.собрать(payload['fetch'], meta, fin=payload['fin'], вердикт=payload['assessment'],
                             профиль_вердиктный=metadata.get('profile') != 'нейтрально',
                             sources=sources.SOURCES, materialized=True)
    # Dossier model uses tuple pairs; the public JSON model uses JSON arrays.
    return json.loads(json.dumps(blocks, ensure_ascii=False, allow_nan=False))


def render(run_id: str, *, format='markdown', revision=None, root=None) -> dict:
    try:
        if format not in {'markdown', 'json'}:
            raise WorkflowError('invalid_format', 'Для просмотра доступны markdown и json; DOCX создаёт export.')
        record = _read(run_id, revision=revision, root=root)
        blocks = _document(record)
        content = dossier.в_markdown(blocks) if format == 'markdown' else blocks
        return _result(record, data={'format': format, 'content': content,
                                    'source_revision': record['revision']}, status='ok')
    except Exception as error:
        return failure(error)


def export(run_id: str, *, format='markdown', revision=None, root=None) -> dict:
    try:
        if format not in {'markdown', 'json', 'docx'}:
            raise WorkflowError('invalid_format')
        record = _read(run_id, revision=revision, root=root)
        blocks = _document(record)
        if format == 'docx':
            buffer = io.BytesIO()
            dossier.записать_docx(blocks, buffer)
            data = buffer.getvalue()
        elif format == 'markdown':
            data = dossier.в_markdown(blocks).encode('utf-8')
        else:
            data = _canonical(blocks)
        info = store.put_artifact(run_id, data, format=format, source_revision=record['revision'], root=root)
        return _result(record, data=info, status='ok')
    except Exception as error:
        return failure(error)


def save_snapshot(run_id: str, *, revision=None, root=None) -> dict:
    record = None
    try:
        record = _read(run_id, revision=revision, root=root)
        # A closed provider projection lacks full snapshot identity. Never pass
        # it through fingerprint/prepare as if it were the complete source run.
        # Presence alone fails closed even for a malformed captured marker.
        if '_provider_policy' in record['payload']['fetch']:
            raise WorkflowError('provider_snapshot_export_restricted',
                                'Снимок с ограничениями провайдера не поддерживается; '
                                'используйте экспорт markdown, json или docx с исключениями.')
        fingerprint = snapshot.fingerprint(record['payload']['fetch'], дата=record['created_at'])
        info = store.put_artifact(run_id, _canonical(redact(fingerprint)), format='snapshot',
                                  source_revision=record['revision'], root=root)
        return _result(record, data=info, status='ok')
    except Exception as error:
        return failure(error, run_id=record['run_id'] if record else None,
                       revision=record['revision'] if record else None)
