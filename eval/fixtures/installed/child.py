"""FIXED acceptance harness copied outside checkout; never shipped to users.

No configurable executable/transport hook exists in the production interface.
All collection here uses one recorded registry response or a fixed missing source.
"""
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import fetch_counterparty as collector
import run_store
import workflow_cli

CALLS = []


def no_network(event, args):
    if event in {'socket.connect', 'socket.connect_ex', 'socket.getaddrinfo', 'socket.bind'}:
        raise AssertionError('installed acceptance attempted network: ' + event)


sys.addaudithook(no_network)


def collect(inn, **kwargs):
    CALLS.append('егрюл')
    if inn == '7728168971':
        return {'инн': inn, 'тип': 'ЮЛ', 'профиль': 'нейтрально', '_доступность': {
            'егрюл': {'состояние': 'не проверено', 'причина': 'fixed fixture unavailable'}}}
    assert inn == '7707083893'
    raw = (Path(__file__).parent / 'egrul.raw.json').read_bytes()
    collector._ИСТОЧНИК.id = 'егрюл'
    collector._ИСТОЧНИК.subject_inn = inn
    collector._записать('GET', 'https://egrul.nalog.ru/', 200, raw)
    block, availability = collector.parse_egrul(json.loads(raw), inn)
    if kwargs.get('on_source'):
        kwargs['on_source']('егрюл', copy.deepcopy(block), copy.deepcopy(availability))
    return {'инн': inn, 'тип': 'ЮЛ', 'профиль': 'нейтрально', 'егрюл': block,
            '_доступность': {'егрюл': availability}}


def source(sid, inn, **kwargs):
    CALLS.append(sid)
    assert sid == 'риски', sid
    return {}, {'состояние': 'не проверено', 'причина': 'recorded fixture has no risk response'}


def forbidden(*args, **kwargs):
    raise AssertionError('offline operation attempted collection')


def bytes_map(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in Path(root).rglob('*') if p.is_file()}


def main():
    action, *args = sys.argv[1:]
    if action == 'origins':
        assert 'PYTHONPATH' not in os.environ
        origins = {}
        for name in args:
            spec = importlib.util.find_spec(name)
            assert spec and spec.origin and 'site-packages' in spec.origin, (name, spec)
            assert Path(spec.origin).resolve().is_relative_to(Path(sys.prefix).resolve())
            origins[name] = spec.origin
        from importlib.resources import files
        caps = json.loads(files('inn_check_ru_data').joinpath('workflow_capabilities.json').read_text())
        print(json.dumps({'origins': origins, 'capabilities': caps,
                          'sdk_present': importlib.util.find_spec('mcp') is not None}))
        return
    if action == 'manual':
        import manual_block
        print(json.dumps({'суды': manual_block.собрать('суды', '7707083893',
              'https://kad.arbitr.ru/', args[0], 'проверено', {'ответчик_крупные': False})}))
        return
    if action == 'events-seed':
        import monitor_events
        report = {'дата': '2026-10-16T09:00:00Z', 'результаты': [
            {'инн': '7707083893', 'профиль': 'нейтрально', 'статус': 'изменения',
             'изменения': [{'поле': 'директор', 'было': 'Recorded A', 'стало': 'Recorded B'}]},
            {'инн': '7728168971', 'профиль': 'нейтрально', 'статус': 'не проверено',
             'причина': 'recorded missing response', 'изменения': []}]}
        print(json.dumps(monitor_events.ingest(report, root=args[0])))
        return
    if action == 'verify-store':
        root, rid = Path(args[0]), args[1]
        before = bytes_map(root)
        head = run_store.read(rid, root=root)
        revisions = []
        raw_count = 0
        for number in range(1, head['revision'] + 1):
            record = run_store.read(rid, revision=number, root=root)
            assert record['revision'] == number
            revisions.append(hashlib.sha256(run_store._json(record)).hexdigest())
            for item in record['payload']['metadata'].get('raw_refs', {}).get('responses', {}).values():
                blob = run_store.read_raw(rid, item['sha256'], root=root)
                assert hashlib.sha256(blob).hexdigest() == item['sha256']
                raw_count += 1
        artifacts = run_store.list_artifacts(rid, root=root)
        for item in artifacts:
            blob = run_store.read_artifact(rid, item['artifact_id'], root=root)
            assert hashlib.sha256(blob).hexdigest() == item['sha256']
        assert bytes_map(root) == before
        print(json.dumps({'revisions': revisions, 'raw_checks': raw_count, 'artifacts': artifacts, 'files': before}))
        return
    if action == 'old-client':
        root, rid = Path(args[0]), args[1]
        before = bytes_map(root)
        # Simulates a schema-older reader, not a claim to have installed an old release.
        with patch.object(run_store, 'SCHEMA_VERSION', 0):
            try:
                run_store.read(rid, root=root)
            except run_store.RunStoreError as error:
                assert error.code == 'unsupported_schema', error.code
            else:
                raise AssertionError('older-schema reader accepted newer store')
        assert before == bytes_map(root)
        print(json.dumps({'code': 'unsupported_schema', 'unchanged': True}))
        return
    if action == 'diff':
        import diff_counterparty
        a, b = (json.loads(run_store.read_artifact(args[0], ident, root=args[3])) for ident in args[1:3])
        changes = diff_counterparty.diff(a, b, '2026-10-16', '2026-10-16')
        print(json.dumps({'changes': changes}))
        return
    assert action in {'cli', 'start', 'refresh'}
    output = io.StringIO()
    with patch.object(collector, 'collect', side_effect=collect if action == 'start' else forbidden), \
         patch.object(collector, 'run_source', side_effect=source if action == 'refresh' else forbidden), \
         contextlib.redirect_stdout(output):
        code = workflow_cli.main(args)
    print(json.dumps({'exit_code': code, 'result': json.loads(output.getvalue()), 'source_calls': CALLS}))


if __name__ == '__main__':
    main()
