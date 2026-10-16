"""Explicit persisted workflow CLI; the original one-shot CLI stays compatible."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import stat
import sys
from pathlib import Path

import run_workflow as workflow

MAX_METRICS_EVENT_BYTES = 4096


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise workflow.WorkflowError('invalid_arguments', 'Неверные аргументы команды; используйте --help.')


def _common(parser):
    parser.add_argument('--json', action='store_true', default=argparse.SUPPRESS,
                        help='Вывести один JSON envelope (формат по умолчанию).')
    parser.add_argument('--store', default=argparse.SUPPRESS,
                        help='Локальный каталог проверок; только настройка CLI.')


def parser():
    root = Parser(prog='inn-check-workflow')
    _common(root)
    commands = root.add_subparsers(dest='command', required=True)
    metrics = commands.add_parser('metrics')
    _common(metrics)
    metric_commands = metrics.add_subparsers(dest='metrics_command', required=True)
    for name in ('record', 'summarize', 'export'):
        sub = metric_commands.add_parser(name)
        _common(sub)
        if name == 'record':
            sub.add_argument('event_file')
            sub.add_argument('--enabled', action='store_true',
                             help='Разрешить запись только этого локального события.')
    document = commands.add_parser('document')
    _common(document)
    document_commands = document.add_subparsers(dest='document_command', required=True)
    for name in ('extract', 'select'):
        sub = document_commands.add_parser(name)
        _common(sub)
        sub.add_argument('file')
        if name == 'extract':
            sub.add_argument('--ocr', action='store_true')
        else:
            sub.add_argument('--role', required=True,
                             choices=('supplier', 'buyer', 'own_company', 'bank', 'signatory', 'unknown', 'other'))
            sub.add_argument('--candidate')
    comparison = commands.add_parser('compare')
    _common(comparison)
    comparison.add_argument('run_ids', nargs='+')
    comparison.add_argument('--as-of', required=True)
    doctor = commands.add_parser('doctor')
    _common(doctor)
    doctor.add_argument('--profile', default='нейтрально')
    doctor.add_argument('--probe', action='store_true')
    cache = commands.add_parser('cache')
    _common(cache)
    cache_commands = cache.add_subparsers(dest='cache_command', required=True)
    for name in ('status', 'plan', 'update'):
        sub = cache_commands.add_parser(name)
        _common(sub)
        sub.add_argument('--profile', default='нейтрально')
        if name == 'update':
            sub.add_argument('--dataset', action='append', required=True)
            sub.add_argument('--max-bytes', type=int, required=True)
            sub.add_argument('--timeout-seconds', type=float, required=True)
    watch = commands.add_parser('watch')
    _common(watch)
    watch_commands = watch.add_subparsers(dest='watch_command', required=True)
    for name in ('plan', 'monitor'):
        sub = watch_commands.add_parser(name)
        _common(sub)
        sub.add_argument('--profile', default='нейтрально')
        if name == 'plan':
            sub.add_argument('--now')
            sub.add_argument('--platform', choices=('linux', 'macos', 'windows'),
                             default='windows' if sys.platform == 'win32' else 'macos' if sys.platform == 'darwin' else 'linux')
        else:
            sub.add_argument('--max-subjects', type=int, default=50)
    events = commands.add_parser('events')
    _common(events)
    event_commands = events.add_subparsers(dest='events_command', required=True)
    for name in ('list', 'claim', 'ack', 'retry'):
        sub = event_commands.add_parser(name)
        _common(sub)
        if name == 'list':
            sub.add_argument('--state', default='pending', choices=('pending', 'delivered', 'acknowledged', 'dead_letter', 'all'))
        else:
            sub.add_argument('--consumer-id', required=True)
        if name in ('list', 'claim'):
            sub.add_argument('--limit', type=int, default=50)
        if name == 'claim':
            sub.add_argument('--lease-seconds', type=int, default=300)
        if name in ('ack', 'retry'):
            sub.add_argument('event_ids', nargs='+')
    run = commands.add_parser('run')
    _common(run)
    run_commands = run.add_subparsers(dest='run_command', required=True)
    for name in ('start', 'show', 'resume', 'manual', 'refresh', 'render', 'export', 'snapshot', 'replay'):
        sub = run_commands.add_parser(name)
        _common(sub)
        sub.add_argument('inn' if name == 'start' else 'run_id')
        if name == 'replay':
            sub.add_argument('--mode', required=True, choices=('original', 'current_rules'))
            sub.add_argument('--cutoff', required=True)
        if name == 'start':
            sub.add_argument('--profile', default='нейтрально')
            sub.add_argument('--mode', default='полный')
        if name in {'manual', 'refresh'}:
            sub.add_argument('--expected-revision', type=int, required=True)
            sub.add_argument('--operation-id', required=True)
        if name == 'manual':
            sub.add_argument('--block-json', required=True)
        if name == 'refresh':
            sub.add_argument('--source', action='append', required=True)
        if name in {'render', 'export', 'snapshot'}:
            sub.add_argument('--revision', type=int)
        if name in {'render', 'export'}:
            sub.add_argument('--format', default='markdown')
    return root


def _metrics_dispatch(args, root):
    import workflow_metrics as metrics

    if args.metrics_command == 'record':
        if not args.enabled:
            # Disabled means no input-file open, root resolution or storage I/O.
            report = metrics.record(None, root=root)
        else:
            try:
                flags = os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0)
                descriptor = os.open(args.event_file, flags)
                try:
                    info = os.fstat(descriptor)
                    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_METRICS_EVENT_BYTES:
                        raise ValueError('invalid_event_file')
                    raw = os.read(descriptor, MAX_METRICS_EVENT_BYTES + 1)
                finally:
                    os.close(descriptor)
                if len(raw) > MAX_METRICS_EVENT_BYTES:
                    raise ValueError('invalid_event_file')
                event = json.loads(raw, object_pairs_hook=metrics._unique_fields,
                                   parse_constant=metrics._reject_constant)
            except (OSError, ValueError, TypeError, RecursionError):
                report = {'schema_version': metrics.SCHEMA_VERSION, 'status': 'invalid_event',
                          'error_code': 'invalid_event'}
            else:
                report = metrics.record(event, enabled=True, root=root)
    else:
        # Export is the same aggregate on stdout. No raw events or automatic file/send.
        report = metrics.summarize(root=root)
    if report['status'] in {'invalid_event', 'invalid_storage', 'storage_error'}:
        code = 'storage_error' if report['status'] == 'storage_error' else report['error_code']
        return workflow.envelope(status='error', data=report,
                                 error={'code': code, 'message': 'Локальная метрика не обработана.',
                                        'retryable': code == 'storage_error'})
    return workflow.envelope(data=report)


def _dispatch(args):
    root = getattr(args, 'store', None)
    if args.command == 'metrics':
        return _metrics_dispatch(args, root)
    if args.command == 'compare':
        import compare_counterparties
        try:
            data = compare_counterparties.compare(args.run_ids, as_of=args.as_of, root=root)
        except compare_counterparties.ComparisonError as error:
            raise workflow.WorkflowError(error.code, error.message) from None
        return workflow.envelope(data=data)
    if args.command == 'document':
        import document_input
        with Path(args.file).open('rb') as stream:
            raw = stream.read(document_input.MAX_BYTES + 1)
        if len(raw) > document_input.MAX_BYTES:
            raise workflow.WorkflowError('too_large', 'Документ превышает 20 МиБ.')
        if args.document_command == 'extract':
            report = document_input.extract(raw, filename=Path(args.file).name, ocr=args.ocr)
        else:
            payload = json.loads(raw)
            if isinstance(payload, dict) and isinstance(payload.get('data'), dict):
                payload = payload['data']
            candidates = payload.get('candidates') if isinstance(payload, dict) else payload
            report = document_input.select(candidates, role=args.role, candidate_id=args.candidate)
        if report['status'] == 'error':
            code = report.get('reason_code') or report['limitations'][0]['reason_code']
            return workflow.envelope(status='error', data=report,
                                     error={'code': code, 'message': 'Документ или выбор стороны не обработан.', 'retryable': False})
        return workflow.envelope(status=report['status'], data=report)
    if args.command == 'watch':
        import monitor_events
        if args.watch_command == 'plan':
            return workflow.envelope(data=monitor_events.host_plan(args.profile, now=args.now or workflow._now(),
                                                                   platform=args.platform, root=root))
        import watchlist
        report = watchlist.monitor(args.profile, root=root, max_subjects=args.max_subjects)
        return workflow.envelope(status='partial' if report['не_проверено'] else 'ok', data=report)
    if args.command == 'events':
        import monitor_events
        if args.events_command == 'list':
            data = monitor_events.list_events(state=args.state, limit=args.limit, root=root)
        elif args.events_command == 'claim':
            data = monitor_events.claim_events(args.consumer_id, limit=args.limit,
                                              lease_seconds=args.lease_seconds, root=root)
        else:
            function = monitor_events.ack_events if args.events_command == 'ack' else monitor_events.retry_events
            data = function(args.event_ids, consumer_id=args.consumer_id, root=root)
        return workflow.envelope(data=data)
    if args.command == 'doctor':
        import workflow_doctor
        workflow._profile(args.profile)
        return workflow.envelope(data=workflow_doctor.inspect(args.profile, probe=args.probe))
    if args.command == 'cache':
        import cache_policy
        workflow._profile(args.profile)
        if args.cache_command == 'update':
            report = cache_policy.execute(args.dataset, profile=args.profile, max_bytes=args.max_bytes,
                                           timeout_seconds=args.timeout_seconds)
            return workflow.envelope(status='partial' if report.get('errors') else 'ok', data=report)
        function = cache_policy.status if args.cache_command == 'status' else cache_policy.plan
        return workflow.envelope(data=function(args.profile, now=workflow._now()))
    command = args.run_command
    if command == 'start':
        return workflow.start(args.inn, profile=args.profile, mode=args.mode, root=root)
    if command in {'show', 'resume'}:
        return workflow.resume(args.run_id, root=root)
    if command == 'manual':
        # File selection is explicit local CLI input, never a field from page data.
        with Path(args.block_json).open('rb') as stream:
            raw = stream.read(workflow.store.MAX_JSON_BYTES + 1)
        if len(raw) > workflow.store.MAX_JSON_BYTES:
            raise workflow.WorkflowError('too_large', 'Ручной блок превышает допустимый размер.')
        blocks = json.loads(raw)
        return workflow.import_manual(args.run_id, blocks, expected_revision=args.expected_revision,
                                      operation_id=args.operation_id, root=root)
    if command == 'refresh':
        return workflow.refresh(args.run_id, args.source, expected_revision=args.expected_revision,
                                operation_id=args.operation_id, root=root)
    if command == 'replay':
        import historical_replay
        try:
            data = historical_replay.replay(args.run_id, mode=args.mode, cutoff=args.cutoff, root=root)
        except historical_replay.ReplayError as error:
            raise workflow.WorkflowError(error.code, error.message) from None
        return workflow.envelope(status='ok' if data['point_in_time_status'] == 'verified' else 'partial',
                                 run_id=args.run_id, revision=data['source_revision'], data=data)
    if command == 'snapshot':
        return workflow.save_snapshot(args.run_id, revision=args.revision, root=root)
    function = workflow.render if command == 'render' else workflow.export
    return function(args.run_id, format=args.format, revision=args.revision, root=root)


def main(argv=None):
    # Redirected Windows stdout otherwise defaults to cp1252, corrupting Russian JSON.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='backslashreplace')
    try:
        args = parser().parse_args(argv)
        with contextlib.redirect_stdout(sys.stderr):
            result = _dispatch(args)
    except KeyboardInterrupt:
        result = workflow.envelope(status='cancelled')
    except Exception as error:
        from monitor_events import EventError
        if isinstance(error, EventError):
            error = workflow.WorkflowError(error.code, error.message)
        result = workflow.failure(error)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    if result['status'] == 'cancelled':
        return 130
    if result['status'] != 'error':
        return 0
    return 1 if result['error']['code'] in {'execution_error', 'storage_error', 'corrupt_store', 'lock_timeout'} else 2


def cli():
    """Installed console entry point; retain main(argv) for embedding/tests."""
    return main()


if __name__ == '__main__':
    raise SystemExit(main())
