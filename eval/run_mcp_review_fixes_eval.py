#!/usr/bin/env python3
"""Offline regressions for sanctions option values and concurrent SDK1 cancellation."""
import asyncio
import importlib.util
import inspect
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'eval'))
from mcp_handshake import StdioClient, decode_tool_json
from run_workflow_mcp_eval import process_alive


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ti = load('review_fix_tools', ROOT / 'mcp/tools_impl.py')


class LogicEval(unittest.TestCase):
    def test_sanctions_option_values_never_dispatch(self):
        sanctions = load('review_fix_sanctions', ROOT / 'scripts/sanctions_check.py')
        for field in ('inn', 'name'):
            for value in ('--refresh', ' --refresh', '--help', '--name', '-x'):
                with self.subTest(field=field, value=value), patch.object(ti, 'run_script') as runner:
                    with patch.object(sanctions, 'refresh') as refresh:
                        out = ti.sanctions_check(**{field: value})
                    self.assertEqual(out['статус'], 'не проверено')
                    runner.assert_not_called()
                    refresh.assert_not_called()
        expected = {'статус': 'проверено', 'совпадений': 0, 'source': 'fixture'}
        for args in ({'name': 'Иванов Иван'}, {'name': 'ООО «Тест --refresh»'},
                     {'inn': '7707083893'}, {'inn': '123456789012', 'name': 'Иванов-Иван'}):
            with patch.object(ti, 'run_script', return_value=expected) as runner:
                self.assertEqual(ti.sanctions_check(**args), expected)
                cli = []
                for field in ('inn', 'name'):
                    if args.get(field):
                        cli += ['--' + field, args[field]]
                runner.assert_called_once_with('sanctions_check.py', cli)

    def test_fixed_dispatch_rejects_untyped_and_non_api_inputs(self):
        for name, args in [('run_script', {'script': 'evil.py', 'args': []}),
                           ('sanctions_check', {'name': 'Иван', 'root': '/tmp'}),
                           ('sanctions_check', {'name': ['--refresh']}),
                           ('counterparty_fetch', {'inn': '7707083893', 'save': 'yes'}),
                           ('counterparty_batch', {'inns': [1]})]:
            with patch.object(ti, '_start_process') as start:
                self.assertEqual(asyncio.run(ti.legacy_call(name, args))['статус'], 'не проверено')
                start.assert_not_called()

    def test_all_transport_wrappers_async_and_legacy_logic_sync(self):
        import ast
        tree = ast.parse((ROOT / 'mcp/server.py').read_text())
        wrappers = {node.name: node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        self.assertEqual(len(ti._LEGACY_FUNCTIONS), 14)
        for name in ti._LEGACY_FUNCTIONS:
            self.assertIsInstance(wrappers[name], ast.AsyncFunctionDef)
            self.assertFalse(inspect.iscoroutinefunction(getattr(ti, name)))


class ProtocolEval(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.env = dict(os.environ, HOME=str(self.home), USERPROFILE=str(self.home),
                        INN_CHECK_RUNS_DIR=str(self.home / 'runs'), PYTHONDONTWRITEBYTECODE='1')

    def client(self, launcher):
        client = StdioClient([sys.executable, '-B', str(launcher)], env=self.env, cwd=str(self.home))
        self.addCleanup(client.close)
        client.initialize()
        return client

    def wait_file(self, path, client):
        deadline = time.monotonic() + 10
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(path.exists(), ''.join(client.errors))

    def send(self, client, ident, name, args):
        client.send({'jsonrpc': '2.0', 'id': ident, 'method': 'tools/call',
                     'params': {'name': name, 'arguments': args}})

    def test_stdio_sanctions_flags_cannot_launch_engine(self):
        marker = self.home / 'engine-attempt'
        worker = self.home / 'legacy.py'
        worker.write_text("""import sys
from pathlib import Path
sys.path.insert(0, %r)
import tools_impl
def forbidden(*a, **kw):
    Path(%r).write_text('engine attempted')
    raise AssertionError('engine forbidden')
tools_impl.run_script = forbidden
tools_impl._legacy_worker()
""" % (str(ROOT / 'mcp'), str(marker)))
        launcher = self.home / 'server.py'
        launcher.write_text("""import sys
sys.path.insert(0, %r)
import tools_impl
tools_impl._legacy_command = lambda: [sys.executable, '-B', %r]
import server
server.entry()
""" % (str(ROOT / 'mcp'), str(worker)))
        client = self.client(launcher)
        for ident, args in enumerate(({'inn': '--refresh'}, {'name': '--refresh'}), 10):
            self.send(client, ident, 'sanctions_check', args)
            result = decode_tool_json(client.receive(ident, timeout=5))
            self.assertEqual(result['статус'], 'не проверено')
        self.assertFalse(marker.exists())

    def test_all_legacy_json_matches_synchronous_public_logic(self):
        script_dir = self.home / 'scripts'
        script_dir.mkdir()
        generic = {'статус': 'ок', 'инн': '7707083893', 'результаты': [{'инн': '7707083893'}],
                   'профиль': 'нейтрально', 'быстрый_режим': 'fixture'}
        for script in ti._LEGACY_SCRIPTS:
            payload = '# fixture dossier' if script == 'dossier.py' else json.dumps(generic, ensure_ascii=False)
            (script_dir / script).write_text('print(%r)\n' % payload)
        launcher = self.home / 'server.py'
        launcher.write_text("import sys\nsys.path.insert(0, %r)\nimport tools_impl\ntools_impl.SCRIPTS = %r\nimport server\nserver.entry()\n"
                            % (str(ROOT / 'mcp'), str(script_dir)))
        client = self.client(launcher)
        for ident, name in enumerate(ti._LEGACY_FUNCTIONS, 10):
            fields = ti._LEGACY_FIELDS[name][1]
            args = {'inn': '7707083893'} if 'inn' in fields else {}
            if name == 'retro_verdict':
                args['date'] = '2026-10-16'
            if name == 'counterparty_batch':
                args['inns'] = ['7707083893']
            if name == 'extract_inns':
                args['text'] = 'ИНН 7707083893'
            with self.subTest(name=name):
                with patch.object(ti, 'run_script', side_effect=lambda script, *a, **kw:
                        '# fixture dossier\n' if script == 'dossier.py' else dict(generic)):
                    expected = getattr(ti, name)(**args)
                self.send(client, ident, name, args)
                self.assertEqual(decode_tool_json(client.receive(ident, timeout=5)), expected)

    def test_inner_exit_and_timeout_cleanup_before_next_stage(self):
        for index, mode in enumerate(('success', 'invalid_json', 'timeout')):
            with self.subTest(mode=mode):
                directory = self.home / mode
                directory.mkdir()
                ready, stage, marker = (directory / n for n in ('ready', 'stage', 'marker'))
                child_code = "import time; from pathlib import Path; time.sleep(1); Path(%r).write_text('orphan')" % str(marker)
                (directory / 'fetch_counterparty.py').write_text("""import subprocess, sys, time
from pathlib import Path
child = subprocess.Popen([sys.executable, '-c', %r], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
Path(%r).write_text(str(child.pid))
%s
""" % (child_code, str(ready), "time.sleep(30)" if mode == 'timeout' else
                    ("print('invalid')" if mode == 'invalid_json' else "print('{\"инн\": \"7707083893\"}')")))
                (directory / 'fin_scoring.py').write_text("""import json, time
from pathlib import Path
Path(%r).write_text('second stage')
time.sleep(.5)
print(json.dumps({'status': 'fixture'}))
""" % str(stage))
                launcher = directory / 'server.py'
                launcher.write_text("import sys\nsys.path.insert(0, %r)\nimport tools_impl\ntools_impl.SCRIPTS = %r\ntools_impl.TIMEOUT = %r\nimport server\nserver.entry()\n"
                                    % (str(ROOT / 'mcp'), str(directory), .2 if mode == 'timeout' else 3))
                client = self.client(launcher)
                self.send(client, 100 + index, 'counterparty_fin_scoring', {'inn': '7707083893'})
                self.wait_file(ready, client)
                pid = int(ready.read_text())
                if mode == 'success':
                    self.wait_file(stage, client)
                    self.assertFalse(process_alive(pid), 'first-stage descendant survived into next stage')
                result = decode_tool_json(client.receive(100 + index, timeout=8))
                self.assertFalse(process_alive(pid))
                if mode == 'success':
                    self.assertEqual(result, {'status': 'fixture'})
                else:
                    self.assertEqual(result['статус'], 'не проверено')
                    self.assertFalse(stage.exists())
                time.sleep(1.1)
                self.assertFalse(marker.exists())

    def concurrent_cancel(self, cancelled_tool):
        ready = self.home / 'ready.json'
        marker = self.home / 'descendant-marker'
        blocking = self.home / 'blocking'
        script_dir = self.home / 'scripts'
        script_dir.mkdir()
        # Actual run_script -> child -> descendant path. No subprocess mocking.
        (script_dir / 'sanctions_check.py').write_text("""import json, os, subprocess, sys, time
from pathlib import Path
if 'blocking' in sys.argv:
    Path(%r).write_text(json.dumps({'child': os.getpid()}))
    time.sleep(3)
    print(json.dumps({'status': 'fixture'}))
else:
    child = subprocess.Popen([sys.executable, '-c', %r])
    Path(%r).write_text(json.dumps({'child': os.getpid(), 'descendant': child.pid}))
    time.sleep(30)
""" % (str(blocking), "import time; from pathlib import Path; time.sleep(1); Path(%r).write_text('orphan')" % str(marker), str(ready)))
        legacy_worker = self.home / 'legacy.py'
        legacy_pids = self.home / 'legacy-pids'
        legacy_worker.write_text("""import sys, os
sys.path.insert(0, %r)
import tools_impl
tools_impl.SCRIPTS = %r
with open(%r, 'a') as output:
    output.write(str(os.getpid()) + '\\n')
tools_impl._legacy_worker()
""" % (str(ROOT / 'mcp'), str(script_dir), str(legacy_pids)))
        workflow_worker = self.home / 'workflow.py'
        workflow_worker.write_text("""import json, os, subprocess, sys, time
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
        "import time; from pathlib import Path; time.sleep(1); Path(%r).write_text('orphan')" % str(marker), str(ready)))
        launcher = self.home / 'server.py'
        launcher.write_text("""import sys
sys.path.insert(0, %r)
import tools_impl
tools_impl._legacy_command = lambda: [sys.executable, '-B', %r]
tools_impl._workflow_command = lambda: [sys.executable, '-B', %r]
tools_impl.SCRIPTS = %r
import server
server.entry()
""" % (str(ROOT / 'mcp'), str(legacy_worker), str(workflow_worker), str(script_dir)))
        client = self.client(launcher)
        args = {'inn': '7707083893'} if cancelled_tool == 'counterparty_run_start' else {'name': 'pending'}
        self.send(client, 50, cancelled_tool, args)
        self.wait_file(ready, client)
        pids = json.loads(ready.read_text())
        if cancelled_tool == 'sanctions_check':
            pids['wrapper'] = int(legacy_pids.read_text().splitlines()[0])
        self.send(client, 51, 'sanctions_check', {'name': 'blocking'})
        self.wait_file(blocking, client)
        blocker_pid = json.loads(blocking.read_text())['child']
        sent = time.monotonic()
        client.send({'jsonrpc': '2.0', 'method': 'notifications/cancelled',
                     'params': {'requestId': 50, 'reason': 'concurrent regression'}})
        deadline = sent + .8
        while any(process_alive(pid) for pid in pids.values()) and time.monotonic() < deadline:
            time.sleep(.01)
        for pid in pids.values():
            self.assertFalse(process_alive(pid), 'cancelled child/descendant remains alive')
        self.assertTrue(process_alive(blocker_pid), 'other operation should still be active')
        client.send({'jsonrpc': '2.0', 'id': 52, 'method': 'tools/list'})
        self.assertEqual({t['name'] for t in client.receive(52, timeout=1)['result']['tools']}, set(ti.EXPECTED_TOOLS))
        time.sleep(max(0, sent + 1.2 - time.monotonic()))
        self.assertFalse(marker.exists())
        self.assertEqual(decode_tool_json(client.receive(51, timeout=6)), {'status': 'fixture'})
        self.assertFalse(process_alive(blocker_pid))
        for pid in legacy_pids.read_text().splitlines():
            self.assertFalse(process_alive(int(pid)), 'legacy wrapper leaked')
        self.assertFalse(marker.exists())
        print(json.dumps({'cancelled_tool': cancelled_tool, 'worker_alive': False,
                          'descendant_marker_exists': False, 'other_call_completed': True}))

    def test_workflow_cancelled_while_legacy_active(self):
        self.concurrent_cancel('counterparty_run_start')

    def test_legacy_cancelled_while_another_legacy_active(self):
        self.concurrent_cancel('sanctions_check')


if __name__ == '__main__':
    unittest.main()
