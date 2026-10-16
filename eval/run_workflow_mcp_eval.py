#!/usr/bin/env python3
"""Workflow MCP contract and real SDK1 stdio checks (offline recorded fixtures)."""
import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'eval'))
import run_run_workflow_eval as fixtures
from mcp_handshake import StdioClient, decode_tool_json

spec = importlib.util.spec_from_file_location('workflow_mcp_tools', ROOT / 'mcp/tools_impl.py')
ti = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ti)
NAMES = ('counterparty_run_start', 'counterparty_run_read', 'counterparty_run_resume',
         'counterparty_run_manual', 'counterparty_run_refresh', 'counterparty_run_render',
         'counterparty_run_export', 'counterparty_run_snapshot', 'counterparty_doctor',
         'counterparty_probe', 'counterparty_cache_status', 'counterparty_cache_plan',
         'counterparty_events_list', 'counterparty_events_claim', 'counterparty_events_ack')


class ContractEval(unittest.TestCase):
    def test_all_tools_are_registered_and_cancellable(self):
        for name in NAMES:
            self.assertIn(name, ti.EXPECTED_TOOLS)
            self.assertTrue(asyncio.iscoroutinefunction(getattr(ti, name, None)), name)

    def test_invalid_input_is_safe_envelope(self):
        for name, args in [('counterparty_run_read', {'run_id': '../escape'}),
                           ('counterparty_run_start', {'inn': 7707083893}),
                           ('counterparty_run_render', {'run_id': 'bad', 'format': 'html'}),
                           ('counterparty_run_manual', {'run_id': 'bad', 'blocks': 'path'})]:
            with self.subTest(name=name):
                out = asyncio.run(ti.workflow_call(name, args))
                self.assertEqual(out['schema_version'], 1)
                self.assertEqual(out['status'], 'error')
                self.assertNotIn('escape', json.dumps(out))


def file_state(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def process_alive(pid):
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not api.GetExitCodeProcess(handle, ctypes.byref(code)):
                raise ctypes.WinError(ctypes.get_last_error())
            return code.value == 259
        finally:
            api.CloseHandle(handle)
    result = subprocess.run(['ps', '-p', str(pid), '-o', 'stat='], capture_output=True, text=True, check=False)
    return bool(result.stdout.strip()) and not result.stdout.strip().startswith('Z')


class ProtocolEval(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.runs = self.home / 'runs'
        with patch.object(fixtures.collector, 'collect', side_effect=fixtures.recorded_collect):
            self.saved = fixtures.workflow.start(fixtures.INN, root=self.runs)
        self.assertNotEqual(self.saved['status'], 'error', self.saved)
        self.env = dict(os.environ, INN_CHECK_RUNS_DIR=str(self.runs),
                        INN_CHECK_EVENTS_DIR=str(self.home / 'events'), HOME=str(self.home),
                        USERPROFILE=str(self.home), PYTHONDONTWRITEBYTECODE='1')
        self.sequence = 2
        self.client = self.open_client()

    def open_client(self, launcher=None):
        client = StdioClient([sys.executable, '-B', str(launcher or ROOT / 'mcp/server.py')],
                             env=self.env, cwd=str(self.home))
        self.addCleanup(client.close)
        client.initialize()
        return client

    def call(self, name, args=None, client=None):
        self.sequence += 1
        response = (client or self.client).call({'jsonrpc': '2.0', 'id': self.sequence,
            'method': 'tools/call', 'params': {'name': name, 'arguments': args or {}}})
        result = decode_tool_json(response)
        self.assertEqual(result['schema_version'], 1, response)
        return result

    def test_real_tools_list_snapshot_names_schemas_annotations(self):
        response = self.client.call({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
        tools = response['result']['tools']
        snapshot = [{key: tool[key] for key in ('name', 'inputSchema', 'annotations')}
                    for tool in sorted(tools, key=lambda t: t['name'])]
        fixture = ROOT / 'eval/fixtures/workflow-tools.json'
        self.assertEqual(snapshot, json.loads(fixture.read_text(encoding='utf-8')))
        self.assertEqual({t['name'] for t in tools}, set(ti.EXPECTED_TOOLS))
        for tool in tools:
            if tool['name'] in NAMES:
                self.assertNotIn('root', tool['inputSchema'].get('properties', {}))
                self.assertFalse(tool['inputSchema']['additionalProperties'])
        by_name = {t['name']: t for t in tools}
        for name in ('counterparty_fetch', 'due_diligence_dossier', 'access_check',
                     'counterparty_run_export', 'counterparty_probe'):
            self.assertFalse(by_name[name]['annotations']['readOnlyHint'])
        self.assertTrue(by_name['counterparty_probe']['annotations']['openWorldHint'])
        self.assertFalse(by_name['counterparty_doctor']['annotations']['openWorldHint'])

    def test_stdio_saved_read_historical_read_render_resume(self):
        rid = self.saved['run_id']
        before = file_state(self.home)
        out = self.call('counterparty_run_read', {'run_id': rid})
        self.assertEqual(out['data']['metadata']['decision'], self.saved['data']['metadata']['decision'])
        self.assertEqual(out['data'], self.saved['data'])
        self.assertEqual(self.call('counterparty_run_resume', {'run_id': rid})['data'], out['data'])
        historic = self.call('counterparty_run_read', {'run_id': rid, 'revision': 1})
        self.assertEqual(historic['revision'], 1)
        for format in ('markdown', 'json'):
            render = self.call('counterparty_run_render', {'run_id': rid, 'format': format})
            self.assertEqual(render['data']['source_revision'], out['revision'])
            self.assertIn(fixtures.INN, json.dumps(render['data'], ensure_ascii=False))
        self.assertEqual(before, file_state(self.home))

    def test_bad_raw_arguments_all_return_json_envelopes(self):
        rid = self.saved['run_id']
        cases = [('counterparty_run_read', {'run_id': '../secret'}),
                 ('counterparty_run_read', {'run_id': rid, 'revision': True}),
                 ('counterparty_run_read', {'run_id': rid, 'revision': '1'}),
                 ('counterparty_run_read', {'run_id': rid, 'revision': 0}),
                 ('counterparty_run_read', {'run_id': rid, 'root': '/tmp/escape'}),
                 ('counterparty_run_read', {}),
                 ('counterparty_run_manual', {'run_id': rid, 'blocks': '{"суды": {}}',
                     'expected_revision': 1, 'operation_id': str(uuid.uuid4())}),
                 ('counterparty_run_manual', {'run_id': rid, 'blocks': {'суды': []},
                     'expected_revision': 1, 'operation_id': str(uuid.uuid4())}),
                 ('counterparty_run_refresh', {'run_id': rid, 'source_ids': [1],
                     'expected_revision': 1, 'operation_id': str(uuid.uuid4())}),
                 ('counterparty_run_render', {'run_id': rid, 'format': 'docx'}),
                 ('counterparty_run_start', {'inn': 7707083893}),
                 ('counterparty_doctor', {'probe': True})]
        before = file_state(self.home)
        for name, args in cases:
            with self.subTest(name=name, args=args):
                out = self.call(name, args)
                self.assertEqual(out['status'], 'error')
                self.assertEqual(out['error']['code'], 'invalid_input')
                self.assertNotIn('secret', json.dumps(out))
        self.assertEqual(before, file_state(self.home))

    def test_mutations_manual_export_snapshot_and_idempotency(self):
        rid = self.saved['run_id']
        block = fixtures.manual_block.собрать('суды', fixtures.INN, 'https://kad.arbitr.ru/',
            fixtures.NOW[:10], 'проверено', {'ответчик_крупные': False})
        args = {'run_id': rid, 'blocks': {'суды': block},
                'expected_revision': self.saved['revision'], 'operation_id': str(uuid.uuid4())}
        manual = self.call('counterparty_run_manual', args)
        self.assertNotEqual(manual['status'], 'error', manual)
        self.assertEqual(manual['revision'], self.saved['revision'] + 1)
        self.assertEqual(self.call('counterparty_run_manual', args), manual)
        stale = self.call('counterparty_run_manual', dict(args, operation_id=str(uuid.uuid4())))
        self.assertEqual(stale['error']['code'], 'revision_conflict')
        for name in ('counterparty_run_export', 'counterparty_run_snapshot'):
            out = self.call(name, {'run_id': rid, 'revision': self.saved['revision']})
            self.assertEqual(out['status'], 'ok', out)
            self.assertEqual(out['data']['source_revision'], self.saved['revision'])
            self.assertEqual(len(out['data']['sha256']), 64)
        self.assertEqual(self.call('counterparty_run_read', {'run_id': rid})['revision'], manual['revision'])

    def test_doctor_cache_tools_are_offline_and_create_no_files(self):
        before = file_state(self.home)
        doctor = self.call('counterparty_doctor')
        self.assertEqual(doctor['status'], 'ok', doctor)
        self.assertFalse(doctor['data']['network']['probed'])
        for name in ('counterparty_cache_status', 'counterparty_cache_plan'):
            out = self.call(name, {'profile': 'нейтрально', 'now': fixtures.NOW})
            self.assertEqual(out['status'], 'ok', out)
        self.assertEqual(before, file_state(self.home))

    def test_event_list_claim_ack_stdio_and_safe_conflicts(self):
        import run_monitor_events_eval as queue_fixtures
        empty = self.call('counterparty_events_list')
        self.assertEqual(empty['data'], [])
        self.assertFalse((self.home / 'events').exists())
        queue_fixtures.events.ingest(queue_fixtures.report(), root=self.home / 'events')
        before = file_state(self.home)
        pending = self.call('counterparty_events_list')
        self.assertEqual(len(pending['data']), 10)
        self.assertEqual(before, file_state(self.home))
        claimed = self.call('counterparty_events_claim', {'consumer_id': 'mcp-fixture', 'limit': 2})
        self.assertEqual(len(claimed['data']), 2)
        ids = [event['event_id'] for event in claimed['data']]
        conflict = self.call('counterparty_events_ack', {'event_ids': ids, 'consumer_id': 'other'})
        self.assertEqual(conflict['error']['code'], 'lease_conflict', conflict)
        acknowledged = self.call('counterparty_events_ack', {'event_ids': ids, 'consumer_id': 'mcp-fixture'})
        self.assertEqual(acknowledged, self.call('counterparty_events_ack', {
            'event_ids': ids, 'consumer_id': 'mcp-fixture'}))
        self.assertEqual(len(self.call('counterparty_events_list', {'state': 'acknowledged'})['data']), 2)
        for name, args in [('counterparty_events_claim', {'consumer_id': '../escape'}),
                           ('counterparty_events_claim', {'consumer_id': 'ok', 'limit': True}),
                           ('counterparty_events_claim', {'consumer_id': 'ok', 'lease_seconds': 3601}),
                           ('counterparty_events_ack', {'consumer_id': 'ok', 'event_ids': ['/tmp/file']}),
                           ('counterparty_events_list', {'state': ['pending']})]:
            self.assertEqual(self.call(name, args)['error']['code'], 'invalid_input')

    def test_pure_tools_with_network_forbidden_in_actual_worker(self):
        worker = self.home / 'offline_worker.py'
        worker.write_text("""import sys, socket, urllib.request
from pathlib import Path
sys.path.insert(0, %r)
import tools_impl
def forbidden(*args, **kwargs):
    Path(%r).write_text('network attempted')
    raise AssertionError('network forbidden')
socket.socket.connect = forbidden
socket.socket.connect_ex = forbidden
urllib.request.urlopen = forbidden
tools_impl._workflow_worker()
""" % (str(ROOT / 'mcp'), str(self.home / 'network-attempted')), encoding='utf-8')
        launcher = self.home / 'offline_server.py'
        launcher.write_text("""import sys
sys.path.insert(0, %r)
import tools_impl
tools_impl._workflow_command = lambda: [sys.executable, '-B', %r]
import server
server.entry()
""" % (str(ROOT / 'mcp'), str(worker)), encoding='utf-8')
        client = self.open_client(launcher)
        before = file_state(self.home)
        for name, args in [('counterparty_run_read', {'run_id': self.saved['run_id']}),
                           ('counterparty_run_resume', {'run_id': self.saved['run_id']}),
                           ('counterparty_run_render', {'run_id': self.saved['run_id']}),
                           ('counterparty_doctor', {}),
                           ('counterparty_cache_status', {'profile': 'нейтрально', 'now': fixtures.NOW}),
                           ('counterparty_cache_plan', {'profile': 'нейтрально', 'now': fixtures.NOW}),
                           ('counterparty_events_list', {})]:
            with self.subTest(name=name):
                result = self.call(name, args, client=client)
                self.assertNotEqual(result['status'], 'error', result)
                self.assertNotIn('network forbidden', json.dumps(result))
        self.assertEqual(before, file_state(self.home))

    def test_real_cancellation_terminates_child_and_descendant_and_recovers_run(self):
        worker = self.home / 'test_worker.py'
        ready = self.home / 'ready.json'
        marker = self.home / 'delayed-marker'
        worker.write_text("""import json, os, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, %r)
sys.path.insert(0, %r)
import tools_impl, fetch_counterparty

def pending(inn, **kwargs):
    child = subprocess.Popen([sys.executable, '-c', %r])
    Path(%r).write_text(json.dumps({'child': os.getpid(), 'descendant': child.pid}))
    time.sleep(30)
    return {}
fetch_counterparty.collect = pending
tools_impl._workflow_worker()
""" % (str(ROOT / 'mcp'), str(ROOT / 'scripts'),
        "import time; from pathlib import Path; time.sleep(3); Path(%r).write_text('orphan')" % str(marker),
        str(ready)), encoding='utf-8')
        launcher = self.home / 'test_server.py'
        launcher.write_text("""import sys
sys.path.insert(0, %r)
import tools_impl
tools_impl._workflow_command = lambda: [sys.executable, '-B', %r]
import server
server.entry()
""" % (str(ROOT / 'mcp'), str(worker)), encoding='utf-8')
        client = self.open_client(launcher)
        prior = {p.name for p in self.runs.iterdir()}
        client.send({'jsonrpc': '2.0', 'id': 50, 'method': 'tools/call', 'params': {
            'name': 'counterparty_run_start', 'arguments': {'inn': fixtures.INN}}})
        deadline = time.monotonic() + 15
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(ready.exists(), ''.join(client.errors))
        pids = json.loads(ready.read_text())
        self.assertTrue(process_alive(pids['child']))
        self.assertTrue(process_alive(pids['descendant']))
        client.send({'jsonrpc': '2.0', 'method': 'notifications/cancelled', 'params': {
            'requestId': 50, 'reason': 'User cancelled fixture collection'}})
        deadline = time.monotonic() + 5
        while any(process_alive(pid) for pid in pids.values()) and time.monotonic() < deadline:
            time.sleep(.02)
        for pid in pids.values():
            self.assertFalse(process_alive(pid), 'orphan process %d' % pid)
        # A later marker must not appear even after its scheduled delay.
        time.sleep(3.2)
        self.assertFalse(marker.exists())
        created = [p.name for p in self.runs.iterdir() if p.name not in prior]
        self.assertEqual(len(created), 1)
        recovered = self.call('counterparty_run_resume', {'run_id': created[0]}, client=client)
        self.assertIn(recovered['status'], ('partial', 'cancelled'))
        self.assertTrue(recovered['status'] == 'cancelled' or
                        any(w['code'] == 'interrupted' for w in recovered['warnings']))
        self.assertFalse(marker.exists())


if __name__ == '__main__':
    unittest.main()
