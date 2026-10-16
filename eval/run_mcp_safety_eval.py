#!/usr/bin/env python3
"""Офлайн-регрессии границ MCP, отмены процесса и установки по README (stdlib)."""
import ast
import importlib.util
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import weakref
from pathlib import Path
from types import MethodType
from unittest.mock import ANY, Mock, patch

ROOT = Path(__file__).resolve().parent.parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.ti = load('safety_tools', ROOT / 'mcp/tools_impl.py')
        # POSIX fault injection also runs on Windows, which lacks SIGKILL.
        sigkill = patch.object(self.ti.signal, 'SIGKILL', 9, create=True)
        sigkill.start()
        self.addCleanup(sigkill.stop)

    def test_batch_flags_cannot_append_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'config.txt'
            target.write_text('keep\n', encoding='utf-8')
            out = self.ti.counterparty_batch(['--resume', str(target), 'bogus'])
            self.assertEqual(target.read_text(encoding='utf-8'), 'keep\n')
            self.assertEqual(out.get('код'), 'invalid_arguments')

    def test_batch_cannot_expand_file_beyond_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'input.csv'
            target.write_text('инн\n' + '\n'.join('invalid%02d' % n for n in range(51)), encoding='utf-8')
            with patch.object(self.ti, 'run_script') as run:
                out = self.ti.counterparty_batch([str(target)])
            run.assert_not_called()
            self.assertEqual(out.get('код'), 'invalid_arguments')

    def test_batch_rejects_actual_count_before_dedup_or_blanks(self):
        for values in (['7707083893'] * 51, [''] * 51):
            with self.subTest(values=values[:1]), patch.object(self.ti, 'run_script') as run:
                out = self.ti.counterparty_batch(values)
                run.assert_not_called()
                self.assertEqual(out.get('код'), 'batch_limit')

    def test_batch_checks_checksum_and_types_without_running_engine(self):
        for value, code in [('1234567890', 'invalid_inn'), (7707083893, 'invalid_arguments'),
                            (None, 'invalid_arguments'), ('', 'invalid_arguments'),
                            ('７７０７０８３８９３', 'invalid_arguments')]:
            with self.subTest(value=value), patch.object(self.ti, 'run_script') as run:
                out = self.ti.counterparty_batch([value])
                run.assert_not_called()
                self.assertEqual(out.get('код'), code)

    def test_valid_batch_sent_as_stdin_data(self):
        expected = {'статус': 'ок', 'результаты': [{'инн': '7707083893'}]}
        with patch.object(self.ti, 'run_script', return_value=expected) as run:
            out = self.ti.counterparty_batch('7707083893, 5036045205')
        self.assertEqual(out, expected)
        args, kwargs = run.call_args
        self.assertIn('--stdin', args[1])
        self.assertNotIn('7707083893', args[1])
        self.assertEqual(kwargs['stdin_text'], '7707083893\n5036045205\n')

    def test_timeout_stops_fetch_descendant(self):
        self._real_descendant(parent_exits=False)

    def test_timeout_stops_descendant_after_parent_exit(self):
        self._real_descendant(parent_exits=True)

    def test_success_also_stops_detached_descendant(self):
        self._real_descendant(parent_exits=True, detached=True)

    def _real_descendant(self, parent_exits, detached=False):
        with tempfile.TemporaryDirectory() as tmp:
            marker, started = Path(tmp) / 'finished', Path(tmp) / 'started'
            release = Path(tmp) / 'release'
            child = Path(tmp) / 'child.py'
            child.write_text('import pathlib,time,sys\n'
                             'pathlib.Path(sys.argv[1]).write_text("started")\n'
                             'while not pathlib.Path(sys.argv[3]).exists(): time.sleep(.01)\n'
                             'time.sleep(0.7)\npathlib.Path(sys.argv[2]).write_text("bad")\n')
            parent = Path(tmp) / 'parent.py'
            spawn = ('subprocess.Popen([sys.executable,*sys.argv[1:]], '
                     'stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n'
                     if detached else 'subprocess.Popen([sys.executable,*sys.argv[1:]])\n')
            parent.write_text('import subprocess,sys,time\n' + spawn +
                              ('print("{}")\n' if parent_exits else 'time.sleep(5)\n'))
            real_start = self.ti._start_process

            def launch_after_ready(*args, **kwargs):
                proc = real_start(*args, **kwargs)
                deadline = time.monotonic() + 10
                while not started.exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                if not started.exists():
                    self.ti._stop_process_tree(proc)
                    self.fail('descendant did not start within startup deadline')
                if parent_exits:
                    self.assertEqual(proc.wait(timeout=5), 0, 'parent must already have exited')
                release.write_text('go', encoding='utf-8')
                return proc

            # Allow interpreter startup before the measured engine timeout.
            with (patch.object(self.ti, 'SCRIPTS', tmp),
                  patch.object(self.ti, '_start_process', side_effect=launch_after_ready)):
                out = self.ti.run_script('parent.py',
                                        [str(child), str(started), str(marker), str(release)], timeout=.4)
            self.assertTrue(started.exists(), 'test child must have started')
            if detached:
                self.assertEqual(out, {})
            else:
                self.assertEqual(out['статус'], 'не проверено')
            time.sleep(.8)
            self.assertFalse(marker.exists(), 'fetch descendant survived MCP timeout')

    def test_large_stdin_deadline_native(self):
        for parent_exits in (False, True):
            with self.subTest(parent_exits=parent_exits):
                self._blocked_stdin(parent_exits)

    def test_large_stdin_deadline_windows_algorithm(self):
        if os.name == 'nt':
            self.skipTest('native test already uses Windows communicate')
        for parent_exits in (False, True):
            with self.subTest(parent_exits=parent_exits):
                self._blocked_stdin(parent_exits, windows_algorithm=True)

    def _blocked_stdin(self, parent_exits, windows_algorithm=False):
        # Exercise CPython's real Windows blocking-write algorithm with real
        # pipes on POSIX too; native Windows uses it without monkeypatches.
        methods = {}
        if windows_algorithm:
            tree = ast.parse(inspect.getsource(subprocess))
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and (
                    node.name == '_readerthread' or
                    (node.name == '_communicate' and '_stdin_write' in ast.unparse(node))
                ):
                    namespace = dict(vars(subprocess))
                    # Only the installed stdlib source, never engine/user code.
                    exec(compile(ast.Module(body=[node], type_ignores=[]),  # noqa: S102
                                 '<CPython Windows subprocess>', 'exec'), namespace)
                    methods[node.name] = namespace[node.name]
            self.assertEqual(set(methods), {'_readerthread', '_communicate'})
        with tempfile.TemporaryDirectory() as tmp:
            started, release, marker = (Path(tmp) / name for name in ('ready', 'release', 'leak'))
            (Path(tmp) / 'child.py').write_text(
                'import pathlib,time,sys\n'
                'pathlib.Path(sys.argv[1]).write_text("ready")\n'
                'while not pathlib.Path(sys.argv[2]).exists(): time.sleep(.005)\n'
                'time.sleep(.6)\npathlib.Path(sys.argv[3]).write_text("leaked")\n'
                'time.sleep(5)\n', encoding='utf-8')
            (Path(tmp) / 'engine.py').write_text(
                'import subprocess,sys,time\n'
                'subprocess.Popen([sys.executable,*sys.argv[1:]])\n' +
                ('' if parent_exits else 'time.sleep(10)\n'), encoding='utf-8')
            launched, start_times, watchdogs = [], [], []
            real_start = self.ti._start_process
            def start(*args, **kwargs):
                proc = real_start(*args, **kwargs)
                launched.append(proc)
                for name, fn in methods.items():
                    setattr(proc, name, MethodType(fn, proc))
                deadline = time.monotonic() + 10
                while not started.exists() and time.monotonic() < deadline:
                    time.sleep(.005)
                self.assertTrue(started.exists(), 'interpreter startup handshake failed')
                if parent_exits:
                    proc.wait(timeout=5)
                # Emergency termination makes the unfixed regression finite.
                def emergency():
                    if os.name == 'nt':
                        proc._inn_job.close()
                    else:
                        try:
                            os.killpg(proc.pid, 9)
                        except ProcessLookupError:
                            pass
                watchdog = threading.Timer(3, emergency)
                watchdog.daemon = True
                watchdog.start()
                watchdogs.append(watchdog)
                release.write_text('go', encoding='utf-8')
                start_times.append(time.monotonic())
                return proc
            try:
                with (patch.object(self.ti, 'SCRIPTS', tmp),
                      patch.object(self.ti, '_start_process', side_effect=start)):
                    out = self.ti.run_script('engine.py',
                        [str(Path(tmp) / 'child.py'), str(started), str(release), str(marker)],
                        stdin_text='x' * (4 * 1024 * 1024), timeout=.15)
                elapsed = time.monotonic() - start_times[0]
                self.assertLess(elapsed, .15 + self.ti.CLEANUP_TIMEOUT + .25)
                self.assertIn('таймаут', out['причина'])
                time.sleep(.7)
                self.assertFalse(marker.exists(), 'descendant survived blocked stdin deadline')
                proc = launched[0]
                self.assertIsNotNone(proc.poll(), 'direct child was not reaped')
                self.assertFalse(any(thread.name.startswith('inn-check-')
                                     for thread in threading.enumerate()))
                self.assertTrue(all(pipe.closed for pipe in (proc.stdin, proc.stdout, proc.stderr)))
                for name in ('stdout_thread', 'stderr_thread'):
                    reader = getattr(proc, name, None)
                    if reader is not None:
                        self.assertFalse(reader.is_alive(), name)
            finally:
                for proc in launched:
                    self.ti._stop_process_tree(proc)
                for watchdog in watchdogs:
                    watchdog.cancel()
                    watchdog.join(timeout=1)

    def test_large_stdin_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'engine.py').write_text(
                'import sys,json,hashlib\n'
                's=sys.stdin.read()\n'
                'print(json.dumps({"digest":hashlib.sha256(s.encode()).hexdigest()}))\n',
                encoding='utf-8')
            import hashlib
            payload = 'Большой stdin\n' * 100000
            with patch.object(self.ti, 'SCRIPTS', tmp):
                out = self.ti.run_script('engine.py', [], stdin_text=payload, timeout=10)
            self.assertEqual(out, {'digest': hashlib.sha256(payload.encode()).hexdigest()})

    def test_unicode_stdin_error_reaps_real_engine(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / 'leaked'
            (Path(tmp) / 'engine.py').write_text(
                'import time,pathlib,sys\ntime.sleep(.3)\n'
                'pathlib.Path(sys.argv[1]).write_text("bad")\ntime.sleep(2)\n')
            launched = []
            real_popen = subprocess.Popen
            def track(*args, **kwargs):
                proc = real_popen(*args, **kwargs)
                launched.append(proc)
                return proc
            try:
                with (patch.object(self.ti, 'SCRIPTS', tmp),
                      patch.object(self.ti.subprocess, 'Popen', side_effect=track)):
                    out = self.ti.run_script('engine.py', [str(marker)], stdin_text='\ud800')
                self.assertIn('UnicodeEncodeError', out['причина'])
                time.sleep(.5)
                self.assertFalse(marker.exists(), 'engine survived invalid Unicode stdin')
                self.assertIsNotNone(launched[0].poll(), 'engine was not reaped')
                self.assertTrue(all(pipe.closed for pipe in
                                    (launched[0].stdin, launched[0].stdout, launched[0].stderr)))
            finally:
                for proc in launched:
                    if proc.poll() is None:
                        proc.kill()
                    proc.wait(timeout=3)
                    for pipe in (proc.stdin, proc.stdout, proc.stderr):
                        if pipe:
                            pipe.close()

    def test_arbitrary_communication_error_still_reaps(self):
        for failure in (TypeError('saved input is str'), ValueError('closed pipe')):
            with self.subTest(failure=type(failure).__name__):
                proc = Mock(pid=321)
                proc.communicate.side_effect = failure
                stop = self.ti._stop_process_tree
                with (patch.object(self.ti, '_start_process', return_value=proc),
                      patch.object(self.ti.os, 'killpg', create=True),
                      patch.object(self.ti, '_stop_process_tree',
                          side_effect=lambda proc, exchange, stop=stop: stop(proc, windows=False, exchange=exchange))):
                    out = self.ti.run_script('batch_check.py', [])
                self.assertIn(type(failure).__name__, out['причина'])
                proc.communicate.assert_called_once_with(input=None)
                proc.wait.assert_called_once()
                self.assertLessEqual(proc.wait.call_args.kwargs['timeout'], self.ti.CLEANUP_TIMEOUT)
                for pipe in (proc.stdin, proc.stdout, proc.stderr):
                    pipe.close.assert_called_once_with()

    def test_cancellation_cleans_and_propagates(self):
        for failure in (KeyboardInterrupt(), SystemExit(7)):
            with self.subTest(failure=type(failure).__name__):
                proc = Mock(pid=321)
                proc.communicate.side_effect = failure
                with (patch.object(self.ti, '_start_process', return_value=proc),
                      patch.object(self.ti, '_stop_process_tree', return_value=None) as stop,
                      self.assertRaises(type(failure))):
                    self.ti.run_script('batch_check.py', [])
                stop.assert_called_once_with(proc, exchange=ANY)

    def test_windows_tree_termination_closes_job_and_reaps(self):
        proc = Mock(pid=123)
        job = Mock()
        proc._inn_job = job
        self.assertIsNone(self.ti._stop_process_tree(proc, windows=True))
        job.close.assert_called_once_with()
        proc.communicate.assert_not_called()

    def test_windows_spawn_is_suspended_until_assigned(self):
        from types import SimpleNamespace
        proc, job = Mock(pid=456), Mock()
        events = []
        proc.communicate.side_effect = [subprocess.TimeoutExpired('engine', .1), ('', '')]
        job.assign_and_resume.side_effect = lambda pid: events.append(('assign_resume', pid))
        def popen(*args, **kwargs):
            events.append(('popen', kwargs))
            return proc
        windows_os = SimpleNamespace(name='nt', path=os.path, environ=os.environ)
        with (patch.object(self.ti._process_runtime, 'os', windows_os),
              patch.object(self.ti._process_runtime, 'WindowsJob', return_value=job),
              patch.object(self.ti.subprocess, 'Popen', side_effect=popen)):
            out = self.ti.run_script('batch_check.py', [], timeout=.1)
        self.assertEqual(events[0][0], 'popen')
        self.assertEqual(events[0][1]['creationflags'] & 4, 4)
        self.assertNotIn('start_new_session', events[0][1])
        self.assertEqual(events[1], ('assign_resume', 456))
        job.close.assert_called_once_with()
        self.assertEqual(out['статус'], 'не проверено')

    def test_windows_assignment_failure_kills_suspended_child(self):
        from types import SimpleNamespace
        proc, job = Mock(pid=456), Mock()
        job.assign_and_resume.side_effect = OSError('assignment failed')
        windows_os = SimpleNamespace(name='nt', path=os.path, environ=os.environ)
        with (patch.object(self.ti._process_runtime, 'os', windows_os),
              patch.object(self.ti._process_runtime, 'WindowsJob', return_value=job),
              patch.object(self.ti.subprocess, 'Popen', return_value=proc)):
            out = self.ti.run_script('batch_check.py', [])
        proc.kill.assert_called_once_with()
        job.close.assert_called_once_with()
        proc.communicate.assert_not_called()
        self.assertIn('OSError', out['причина'])

    def test_windows_job_failure_fallback_is_bounded(self):
        for failure in (OSError('close failed'), ValueError('invalid handle')):
            with self.subTest(failure=repr(failure)):
                proc, job = Mock(pid=321), Mock()
                proc._inn_job = job
                job.close.side_effect = failure
                diagnostic = self.ti._stop_process_tree(proc, windows=True)
                proc.kill.assert_called_once_with()
                self.assertIn('завершение дерева не подтверждено', diagnostic)
                self.assertIn('job', diagnostic)
                proc.communicate.assert_not_called()

    def test_windows_api_assigns_before_resume_and_closes_handles(self):
        control = load('win_control_test', ROOT / 'scripts/process_windows.py')
        api = Mock()
        api.CreateJobObjectW.return_value = 101
        api.OpenProcess.return_value = 102
        api.CreateToolhelp32Snapshot.return_value = 103
        api.OpenThread.return_value = 104
        api.ResumeThread.return_value = 1
        def first(handle, pointer):
            pointer._obj.th32OwnerProcessID = 456
            pointer._obj.th32ThreadID = 789
            return True
        api.Thread32First.side_effect = first
        api.Thread32Next.return_value = False
        with (patch.object(control, '_kernel32', return_value=api),
              patch.object(control.ctypes, 'get_last_error', return_value=18, create=True)):
            job = control.WindowsJob()
            job.assign_and_resume(456)
            job.close()
            job.close()
        names = [call[0] for call in api.mock_calls]
        self.assertLess(names.index('AssignProcessToJobObject'), names.index('ResumeThread'))
        self.assertEqual([call.args[0] for call in api.CloseHandle.call_args_list],
                         [102, 103, 104, 101])
        api.OpenProcess.assert_called_once_with(0x101, False, 456)
        api.OpenThread.assert_called_once_with(2, False, 789)

    def test_windows_api_assignment_failure_never_resumes(self):
        control = load('win_control_test', ROOT / 'scripts/process_windows.py')
        api = Mock()
        api.CreateJobObjectW.return_value = 101
        api.OpenProcess.return_value = 102
        api.AssignProcessToJobObject.return_value = False
        with (patch.object(control, '_kernel32', return_value=api),
              patch.object(control, '_winerror', return_value=OSError('assign refused'))):
            job = control.WindowsJob()
            with self.assertRaises(OSError):
                job.assign_and_resume(456)
            job.close()
        api.ResumeThread.assert_not_called()
        api.CreateToolhelp32Snapshot.assert_not_called()
        self.assertEqual([call.args[0] for call in api.CloseHandle.call_args_list], [102, 101])

    def test_launch_failure_retains_cleanup_diagnostics(self):
        from types import SimpleNamespace
        proc, job = Mock(pid=456), Mock()
        job.assign_and_resume.side_effect = ValueError('assignment failed')
        job.close.side_effect = OSError('close failed')
        windows_os = SimpleNamespace(name='nt', path=os.path, environ=os.environ)
        with (patch.object(self.ti._process_runtime, 'os', windows_os),
              patch.object(self.ti._process_runtime, 'WindowsJob', return_value=job),
              patch.object(self.ti.subprocess, 'Popen', return_value=proc)):
            out = self.ti.run_script('batch_check.py', [])
        self.assertIn('ValueError', out['причина'])
        self.assertIn('job: OSError', out['причина'])

    def test_windows_popen_failure_closes_job(self):
        from types import SimpleNamespace
        windows_os = SimpleNamespace(name='nt', path=os.path, environ=os.environ)
        for failure in (OSError('launch failed'), KeyboardInterrupt(), SystemExit(7)):
            with self.subTest(failure=type(failure).__name__):
                job = Mock()
                with (patch.object(self.ti._process_runtime, 'os', windows_os),
                      patch.object(self.ti._process_runtime, 'WindowsJob', return_value=job),
                      patch.object(self.ti.subprocess, 'Popen', side_effect=failure)):
                    if isinstance(failure, Exception):
                        out = self.ti.run_script('batch_check.py', [])
                        self.assertIn('OSError', out['причина'])
                    else:
                        with self.assertRaises(type(failure)):
                            self.ti.run_script('batch_check.py', [])
                job.close.assert_called_once_with()
                job.assign_and_resume.assert_not_called()

    def test_windows_api_resume_failure_closes_auxiliary_handles(self):
        control = load('win_control_test', ROOT / 'scripts/process_windows.py')
        api = Mock()
        api.CreateJobObjectW.return_value = 101
        api.OpenProcess.return_value = 102
        api.CreateToolhelp32Snapshot.return_value = 103
        api.OpenThread.return_value = 104
        api.ResumeThread.return_value = 0xFFFFFFFF
        def first(handle, pointer):
            pointer._obj.th32OwnerProcessID = 456
            pointer._obj.th32ThreadID = 789
            return True
        api.Thread32First.side_effect = first
        api.Thread32Next.return_value = False
        with (patch.object(control, '_kernel32', return_value=api),
              patch.object(control.ctypes, 'get_last_error', return_value=18, create=True),
              patch.object(control, '_winerror', return_value=OSError('resume refused'))):
            job = control.WindowsJob()
            with self.assertRaises(OSError):
                job.assign_and_resume(456)
            job.close()
        self.assertEqual([call.args[0] for call in api.CloseHandle.call_args_list],
                         [102, 103, 104, 101])

    def test_package_import_outside_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copytree(ROOT / 'mcp', Path(tmp) / 'inn_check_ru_mcp')
            for module in ('sibling_imports', 'process_runtime', 'process_windows'):
                shutil.copyfile(ROOT / 'scripts' / (module + '.py'), Path(tmp) / (module + '.py'))
            engine = Path(tmp) / 'engine.py'
            engine.write_text('print("{}")', encoding='utf-8')
            code = ("import sys; sys.path.insert(0, %r); "
                    "from inn_check_ru_mcp import tools_impl as ti; ti.SCRIPTS=%r; "
                    "import process_runtime, process_windows; from inn_check_ru_mcp import process_control as compat; "
                    "assert ti._process_runtime is process_runtime; "
                    "assert compat.WindowsJob is process_windows.WindowsJob; "
                    "assert ti._ProcessStartError is process_runtime._ProcessStartError; "
                    "assert ti.run_script('engine.py', []) == {}") % (tmp, tmp)
            proc = subprocess.run([sys.executable, '-I', '-X', 'utf8', '-c', code],
                                  cwd=tmp, capture_output=True, encoding='utf-8',
                                  timeout=10, check=False)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_engine_pipe_error_still_cleans_process(self):
        proc = Mock(pid=321)
        proc.communicate.side_effect = [OSError("broken pipe"), ("", "")]
        with (patch.object(self.ti, "_start_process", return_value=proc),
              patch.object(self.ti, "_stop_process_tree", return_value=None) as stop):
            out = self.ti.run_script("batch_check.py", [])
        stop.assert_called_once_with(proc, exchange=ANY)
        self.assertEqual(out["статус"], "не проверено")
        self.assertIn("OSError", out["причина"])

    def test_failed_kill_and_held_pipes_have_bounded_cleanup(self):
        proc = Mock(pid=321)
        proc.communicate.side_effect = subprocess.TimeoutExpired("engine", .1)
        proc.kill.side_effect = PermissionError("blocked")
        proc.wait.side_effect = subprocess.TimeoutExpired("engine", .1)
        proc._inn_job = None
        with patch.object(self.ti.subprocess, "run", side_effect=AssertionError("no taskkill")):
            diagnostic = self.ti._stop_process_tree(proc, windows=True)
        self.assertIn("завершение дерева не подтверждено", diagnostic)
        self.assertIn("kill", diagnostic)
        self.assertIn("reap", diagnostic)
        proc.communicate.assert_not_called()
        proc.wait.assert_called_once()
        self.assertLessEqual(proc.wait.call_args.kwargs['timeout'], self.ti.CLEANUP_TIMEOUT)
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            pipe.close.assert_called_once_with()

    def test_posix_kill_error_has_bounded_fallback(self):
        proc = Mock(pid=321)
        with patch.object(self.ti.os, "killpg", side_effect=PermissionError("blocked"), create=True):
            diagnostic = self.ti._stop_process_tree(proc, windows=False)
        proc.kill.assert_called_once_with()
        proc.communicate.assert_not_called()
        self.assertIn("завершение дерева не подтверждено", diagnostic)
        self.assertIn("killpg", diagnostic)

    def test_pipe_read_error_still_kills_closes_and_reaps(self):
        proc = Mock(pid=321)
        proc.communicate.side_effect = OSError("pipe failed")
        stop = self.ti._stop_process_tree
        with (patch.object(self.ti, '_start_process', return_value=proc),
              patch.object(self.ti.os, 'killpg', create=True) as killpg,
              patch.object(self.ti, '_stop_process_tree',
                  side_effect=lambda proc, exchange, stop=stop: stop(proc, windows=False, exchange=exchange))):
            out = self.ti.run_script('batch_check.py', [])
        killpg.assert_called_once_with(321, 9)
        proc.communicate.assert_called_once_with(input=None)
        proc.wait.assert_called_once()
        self.assertIn('OSError', out['причина'])
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            pipe.close.assert_called_once_with()

    def test_supervisor_cancellation_joins_single_exchange(self):
        for failure in (KeyboardInterrupt(), SystemExit(7)):
            with self.subTest(failure=type(failure).__name__):
                release, entered = threading.Event(), threading.Event()
                proc = Mock(pid=321)
                def communicate(entered=entered, release=release, **kwargs):
                    entered.set()
                    release.wait(5)
                    return ('{}', '')
                proc.communicate.side_effect = communicate
                exchanges = []
                factory = self.ti._ProcessExchange
                stop = self.ti._stop_process_tree
                def exchange(*args, factory=factory, entered=entered, failure=failure, exchanges=exchanges):
                    owner = factory(*args)
                    def cancelled(timeout):
                        self.assertTrue(entered.wait(2))
                        raise failure
                    owner.done.wait = cancelled
                    exchanges.append(owner)
                    return owner
                try:
                    with (patch.object(self.ti, '_start_process', return_value=proc),
                          patch.object(self.ti, '_ProcessExchange', side_effect=exchange),
                          patch.object(self.ti.os, 'killpg', side_effect=lambda *args, release=release: release.set(), create=True),
                          patch.object(self.ti, '_stop_process_tree',
                              side_effect=lambda proc, exchange, stop=stop: stop(proc, windows=False, exchange=exchange)),
                          self.assertRaises(type(failure))):
                        self.ti.run_script('batch_check.py', [], stdin_text='x' * 1000000)
                    self.assertFalse(exchanges[0].thread.is_alive())
                    proc.communicate.assert_called_once()
                    proc.wait.assert_called_once()
                    for pipe in (proc.stdin, proc.stdout, proc.stderr):
                        pipe.close.assert_called_once_with()
                finally:
                    release.set()

    def test_failed_termination_never_reenters_exchange_or_closes_active_pipes(self):
        release, entered = threading.Event(), threading.Event()
        proc = Mock(pid=321)
        def communicate(**kwargs):
            entered.set()
            release.wait(5)
            return ('{}', '')
        proc.communicate.side_effect = communicate
        proc.kill.side_effect = PermissionError('blocked')
        exchange = self.ti._ProcessExchange(proc, 'x' * 1000000)
        try:
            with self.assertRaises(subprocess.TimeoutExpired):
                exchange.run(proc, .02)
            self.assertTrue(entered.is_set())
            with (patch.object(self.ti.os, 'killpg', side_effect=PermissionError('blocked'), create=True),
                  patch.object(self.ti._process_runtime, 'CLEANUP_TIMEOUT', .05)):
                start = time.monotonic()
                diagnostic = self.ti._stop_process_tree(proc, windows=False, exchange=exchange)
                self.assertLess(time.monotonic() - start, .3)
            self.assertIn('reap/close не подтверждены', diagnostic)
            proc.communicate.assert_called_once()
            proc.wait.assert_not_called()
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                pipe.close.assert_not_called()
        finally:
            release.set()
            exchange.thread.join(timeout=2)
        self.assertFalse(exchange.thread.is_alive())

    def test_blocked_pipe_close_cannot_block_timeout_response(self):
        import threading
        release, entered = threading.Event(), threading.Event()
        proc = Mock(pid=321)
        proc.communicate.side_effect = subprocess.TimeoutExpired("engine", .1)
        def blocked_close():
            entered.set()
            release.wait(5)
        proc.stdout.close.side_effect = blocked_close
        try:
            with (patch.object(self.ti.os, "killpg", create=True),
                  patch.object(self.ti._process_runtime, "CLEANUP_TIMEOUT", .05)):
                start = time.monotonic()
                diagnostic = self.ti._stop_process_tree(proc, windows=False)
                self.assertLess(time.monotonic() - start, .5)
            self.assertTrue(entered.is_set())
            self.assertIn("закрытие каналов", diagnostic)
        finally:
            release.set()

    def test_documented_manual_install_works_outside_checkout(self):
        for readme in ('README.md', 'README.en.md'):
            text = (ROOT / readme).read_text(encoding="utf-8")
            line = next((line for line in text.splitlines()
                         if 'SKILL.md' in line and 'scripts/' in line and 'references/' in line), '')
            paths = list(dict.fromkeys(re.findall(r'`(SKILL\.md|references/|scripts/|data/)`', line)))
            self.assertIn('data/', paths, readme)
            with tempfile.TemporaryDirectory() as tmp:
                for path in paths:
                    src, dest = ROOT / path, Path(tmp) / path
                    if src.is_dir():
                        shutil.copytree(src, dest)
                    else:
                        shutil.copy2(src, dest)
                env = dict(os.environ, HOME=tmp, INN_CHECK_OFFLINE='1')
                proc = subprocess.run([sys.executable, '-I', '-X', 'utf8', str(Path(tmp) / 'scripts/profiles.py'), '--list'],
                                      cwd=tmp, env=env, text=True, encoding='utf-8', capture_output=True, timeout=10, check=False)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertIn('отсрочка', proc.stdout)

    def test_fresh_cache_canary_is_explicitly_not_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = ("import sys,json; sys.path.insert(0, %r); "
                    "import fetch_counterparty as fc, check_access as ca; "
                    "report,rc=ca.run_canaries(fc, {'реестры_цб': fc.SOURCES['реестры_цб']}); "
                    "print(json.dumps(report,ensure_ascii=False)); sys.exit(rc)") % str(ROOT / 'scripts')
            proc = subprocess.run([sys.executable, '-I', '-X', 'utf8', '-c', code], cwd=tmp,
                                  env=dict(os.environ, HOME=tmp), capture_output=True,
                                  text=True, encoding='utf-8', timeout=10, check=False)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            row = json.loads(proc.stdout)['реестры_цб']
            self.assertEqual(row['результат'], 'не запускалась')
            self.assertTrue(row['причина'].startswith('кэш:'))

    def test_parallel_spy_retains_live_openers_across_waves(self):
        fc = load('fetch_safety', ROOT / 'scripts/fetch_counterparty.py')
        ev = load('fetch_eval_safety', ROOT / 'eval/run_fetch_eval.py')
        refs, expired = [], []

        class Opener:
            pass

        calls = 0

        def make():
            if calls <= 2 and any(ref() is None for ref in refs):
                expired.append(True)
            return Opener()

        collect, source = fc.collect, fc.run_source

        def collect_wave(*args, **kwargs):
            nonlocal calls
            calls += 1
            refs.clear()
            return collect(*args, **kwargs)

        def observe(sid, *args, **kwargs):
            if calls <= 2 and sid in fc.FETCHERS:
                refs.append(weakref.ref(kwargs['opener']))
            return source(sid, *args, **kwargs)

        with (patch.object(fc, '_make_opener', make), patch.object(fc, 'collect', collect_wave),
              patch.object(fc, 'run_source', observe)):
            ev.case_parallel(fc)
        self.assertFalse(expired, 'spy released opener identity during a collect')


if __name__ == '__main__':
    unittest.main(verbosity=2)
