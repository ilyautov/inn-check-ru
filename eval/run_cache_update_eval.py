"""Offline transactional updates and bounded transport."""
import gzip
import hashlib
import importlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))


def write_index(root, *, valid=True):
    path = Path(root) / 'tz' / 'индекс.json.gz'
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, 'wt') as stream:
        json.dump({'дата_выгрузки': '2026-10-16', 'построен': '2026-10-16', 'по_инн': {}} if valid else {}, stream)


def descendant_command(ready, marker):
    child = ('import pathlib,time;pathlib.Path(%r).touch();time.sleep(.5);'
             'pathlib.Path(%r).touch()' % (str(ready), str(marker)))
    leader = ('import pathlib,subprocess,sys,time\n'
              'subprocess.Popen([sys.executable,"-c",%r])\n'
              'while not pathlib.Path(%r).exists(): time.sleep(.005)\n' % (child, str(ready)))
    return [sys.executable, '-c', leader]


class CacheUpdateTests(unittest.TestCase):
    def setUp(self):
        self.cp = importlib.import_module('cache_policy')

    def test_worker_success_stops_descendant_after_parent_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            ready, marker = home / 'ready', home / 'late'
            with patch.object(self.cp, '_worker_argv', return_value=descendant_command(ready, marker)):
                result = self.cp._run_worker({}, home, home / 'bytes.json', 10,
                                             time.monotonic() + 2)
            self.assertTrue(ready.exists(), 'the descendant must have started')
            self.assertTrue(result['validated'], result)
            time.sleep(.6)
            self.assertFalse(marker.exists(), 'successful parent exit left a live descendant')

    def test_worker_failure_after_parent_exit_stops_descendant(self):
        real_launch = subprocess.Popen
        for failure in (subprocess.TimeoutExpired('fixture', .2), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                ready, marker = home / 'ready', home / 'late'
                processes = []

                def launch(*args, failure=failure, processes=processes, **kwargs):
                    process = real_launch(*args, **kwargs)
                    processes.append(process)
                    original_wait = process.wait
                    raised = False

                    def wait_then_fail(*args, **kwargs):
                        nonlocal raised
                        result = original_wait(*args, **kwargs)
                        if not raised:
                            raised = True
                            raise failure
                        return result

                    process.wait = wait_then_fail
                    return process

                with patch.object(self.cp, '_worker_argv', return_value=descendant_command(ready, marker)), \
                        patch.object(subprocess, 'Popen', side_effect=launch), \
                        self.assertRaises(type(failure)):
                    self.cp._run_worker({}, home, home / 'bytes.json', 10,
                                        time.monotonic() + 2)
                self.assertTrue(ready.exists(), 'the descendant must have started')
                self.assertEqual(processes[0].returncode, 0, 'leader exit precedes cleanup')
                time.sleep(.6)
                self.assertFalse(marker.exists(), 'failure after parent exit left a live descendant')

    def test_unconfirmed_cleanup_preserves_previous_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'cache'
            write_index(root)
            previous = root / 'tz' / 'индекс.json.gz'
            digest = hashlib.sha256(previous.read_bytes()).hexdigest()
            code = ('import gzip,json,pathlib\n'
                    'path=pathlib.Path.home()/".cache"/"inn-check-ru"/"tz"/"индекс.json.gz"\n'
                    'path.parent.mkdir(parents=True)\n'
                    'with gzip.open(path,"wt") as stream:\n'
                    ' json.dump({"дата_выгрузки":"2026-10-16","построен":"2026-10-16","по_инн":{}},stream)\n')
            cleanup = getattr(self.cp, '_stop_process_tree', None)

            def unconfirmed(*args, **kwargs):
                if cleanup is not None:
                    cleanup(*args, **kwargs)
                return 'fixture: cleanup could not be confirmed'

            with patch.object(self.cp, '_worker_argv', return_value=[sys.executable, '-c', code]), \
                    patch.object(self.cp, '_stop_process_tree', side_effect=unconfirmed, create=True):
                result = self.cp.execute(['rospatent_tz'], profile='нейтрально', max_bytes=10,
                                         timeout_seconds=2, root=root)
            self.assertTrue(result['errors'], result)
            self.assertEqual(result['datasets'][0]['state'], 'failed')
            self.assertEqual(hashlib.sha256(previous.read_bytes()).hexdigest(), digest)
            self.assertFalse((root / '.workflow-cache.json').exists())
            self.assertFalse(any((root / '.workflow-generations' / 'rospatent_tz').iterdir()))

    def test_worker_passes_only_named_runtime_network_environment(self):
        inherited = {
            'PATH': '/fixture/bin', 'PATHEXT': '.COM;.EXE', 'SYSTEMROOT': 'C:\\Windows',
            'WINDIR': 'C:\\Windows', 'COMSPEC': 'C:\\Windows\\cmd.exe',
            'SYSTEMDRIVE': 'C:', 'PYTHONHOME': '/fixture/python',
            'PYTHONPATH': '/fixture/modules', 'PYTHONUTF8': '1',
            'PYTHONIOENCODING': 'utf-8', 'PYTHONCOERCECLOCALE': '0',
            'PYTHONNOUSERSITE': '1', 'PYTHONUSERBASE': '/fixture/user-python',
            'PYTHONPLATLIBDIR': 'lib', 'LD_LIBRARY_PATH': '/fixture/lib',
            'DYLD_LIBRARY_PATH': '/fixture/dylib', 'DYLD_FALLBACK_LIBRARY_PATH': '/fixture/fallback',
            'LANG': 'C.UTF-8', 'LANGUAGE': 'ru', 'LC_ALL': 'C', 'LC_CTYPE': 'C.UTF-8',
            'LC_MESSAGES': 'C', 'LC_TIME': 'C', 'LC_NUMERIC': 'C', 'LC_MONETARY': 'C',
            'LC_COLLATE': 'C', 'LC_ADDRESS': 'C', 'LC_IDENTIFICATION': 'C',
            'LC_MEASUREMENT': 'C', 'LC_NAME': 'C', 'LC_PAPER': 'C', 'LC_TELEPHONE': 'C',
            'TZ': 'UTC', 'SSL_CERT_FILE': '/fixture/ca.pem', 'SSL_CERT_DIR': '/fixture/certs',
            'COUNTERPARTY_CA_BUNDLE': '/fixture/project-ca.pem', 'COUNTERPARTY_INSECURE': '1',
            'INN_CHECK_PROXY': 'socks5://proxy.invalid:1080',
            'HTTPS_PROXY': 'http://upper.invalid:3128', 'https_proxy': '',
            'HTTP_PROXY': 'http://upper.invalid:3128', 'http_proxy': 'http://lower.invalid:3128',
            'ALL_PROXY': 'http://upper.invalid:3128', 'all_proxy': 'http://lower.invalid:3128',
            'FTP_PROXY': 'http://upper.invalid:3128', 'ftp_proxy': 'http://lower.invalid:3128',
            'NO_PROXY': 'localhost', 'no_proxy': '.invalid', 'REQUEST_METHOD': 'GET',
        }
        unrelated = {'CHECKO_API_KEY': 'host-key-sentinel', 'DADATA_API_KEY': 'host-dadata-sentinel',
                     'UNRELATED_SECRET': 'host-secret-sentinel', 'AWS_SESSION_TOKEN': 'aws-sentinel',
                     'UNRELATED_SETTING': 'ordinary-setting', 'INN_CHECK_PROVIDER_POLICY': '/host/policy',
                     'INN_CHECK_TESSERACT': '/host/ocr', 'HOME': '/host/home',
                     'TMPDIR': '/host/tmp', 'XDG_CONFIG_HOME': '/host/config'}
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with patch.dict(os.environ, {**inherited, **unrelated}, clear=True), \
                    patch.object(self.cp, '_start_process') as launch, \
                    patch.object(self.cp, '_ProcessExchange'), \
                    patch.object(self.cp, '_stop_process_tree', return_value=None) as cleanup:
                launch.return_value.returncode = 0
                before = dict(os.environ)
                result = self.cp._run_worker({'updater': 'rospatent_tz'}, home,
                                             home / 'bytes.json', 10, time.monotonic() + 2)
                self.assertEqual(dict(os.environ), before)
            child = launch.call_args.kwargs['env']
            expected = {**inherited, 'HOME': str(home), 'USERPROFILE': str(home),
                        'PYTHONDONTWRITEBYTECODE': '1', 'XDG_CACHE_HOME': str(home / '.cache'),
                        'XDG_CONFIG_HOME': str(home / '.config'), 'TMPDIR': str(home / 'tmp'),
                        'TEMP': str(home / 'tmp'), 'TMP': str(home / 'tmp')}
            expected['PYTHONPATH'] = str(Path(self.cp.__file__).resolve().parent) + os.pathsep + inherited['PYTHONPATH']
            self.assertEqual(child, expected)
            for stream in ('stdin', 'stdout', 'stderr'):
                self.assertEqual(launch.call_args.kwargs[stream], subprocess.DEVNULL)
            cleanup.assert_called_once()
            self.assertTrue(result['validated'])
            self.assertTrue((home / 'tmp').is_dir())

    def test_real_child_import_proxy_ca_and_secret_isolation(self):
        import ssl

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / 'home'
            modules = Path(directory) / 'modules'
            home.mkdir()
            modules.mkdir()
            (modules / 'env_fixture.py').write_text('VALUE = "external import works"\n')
            ledger = home / 'result.json'
            ca_file = ssl.get_default_verify_paths().cafile
            self.assertTrue(ca_file, 'test runtime must have a default CA file')
            code = '''import json, os, ssl, tempfile, urllib.request
from pathlib import Path
import env_fixture, proxy
from fetch_counterparty import _build_ssl_context
context = _build_ssl_context()
Path(%r).write_text(json.dumps({
    'external': env_fixture.VALUE, 'proxy': proxy.настройка()['url'],
    'urllib_proxies': urllib.request.getproxies(),
    'verified': context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname,
    'ca_count': context.cert_store_stats()['x509_ca'],
    'home': str(Path.home()), 'tmp': tempfile.gettempdir(),
    'secret': os.environ.get('UNRELATED_SECRET'), 'api': os.environ.get('CHECKO_API_KEY')}))
''' % str(ledger)
            environment = {'HOME': '/unrelated-home', 'PYTHONPATH': str(modules),
                           'COUNTERPARTY_CA_BUNDLE': ca_file, 'SSL_CERT_FILE': ca_file,
                           'INN_CHECK_PROXY': 'socks5://project.invalid:1080',
                           'HTTPS_PROXY': 'http://upper.invalid:3128',
                           'https_proxy': 'http://lower.invalid:3128', 'no_proxy': 'localhost',
                           'UNRELATED_SECRET': 'host-secret-sentinel', 'CHECKO_API_KEY': 'host-key-sentinel'}
            # Keep this host's executable/loader runtime; synthetic network config
            # is inspected in the child without making any network requests.
            with patch.dict(os.environ, environment), \
                    patch.object(self.cp, '_worker_argv', return_value=[sys.executable, '-c', code]):
                result = self.cp._run_worker({}, home, ledger, 10, time.monotonic() + 5)
            self.assertTrue(result['validated'], result)
            report = json.loads(ledger.read_text())
            self.assertEqual(report['external'], 'external import works')
            self.assertEqual(report['proxy'], environment['INN_CHECK_PROXY'])
            self.assertEqual(report['urllib_proxies']['https'], environment['https_proxy'])
            self.assertEqual(report['urllib_proxies']['no'], environment['no_proxy'])
            self.assertTrue(report['verified'])
            self.assertGreater(report['ca_count'], 0)
            self.assertEqual(Path(report['home']).resolve(), home.resolve())
            self.assertEqual(Path(report['tmp']).resolve(), (home / 'tmp').resolve())
            self.assertIsNone(report['secret'])
            self.assertIsNone(report['api'])

    def test_partial_legacy_success_is_rejected(self):
        from cache_update_worker import result_success
        self.assertFalse(result_success('{"статус":"обновлено 1/3 списков"}'))
        self.assertFalse(result_success('{"статус":"индекс не обновлён: сеть"}'))
        self.assertTrue(result_success('{"статус":"обновлено 3/3 списков"}'))
        self.assertTrue(result_success('{"записано":true}'))

    def test_stream_cap_unknown_length_and_chunk_boundary(self):
        from cache_update_worker import BoundedResponse, BudgetExceeded, DownloadBudget
        with tempfile.TemporaryDirectory() as directory:
            ledger = DownloadBudget(4, Path(directory) / 'bytes.json')
            underlying = io.BytesIO(b'abcdefgh')
            stream = BoundedResponse(underlying, ledger)
            self.assertEqual(stream.read(3), b'abc')
            with self.assertRaises(BudgetExceeded):
                stream.read()
            self.assertEqual(ledger.downloaded, 4)
            self.assertEqual(underlying.tell(), 4)

    def test_success_uses_only_selected_dataset_and_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'cache'
            outside = Path(directory) / 'sentinel'
            outside.write_text('unchanged')
            calls = []
            def worker(item, home, ledger, budget, deadline):
                calls.append(item['dataset_id'])
                write_index(home / '.cache' / 'inn-check-ru')
                ledger.write_text('{"bytes_downloaded": 7}')
                return {'returncode': 0}
            with patch.object(self.cp, '_run_worker', side_effect=worker):
                result = self.cp.execute(['rospatent_tz'], profile='нейтрально', max_bytes=100,
                                         timeout_seconds=3, root=root)
            self.assertFalse(result['errors'], result)
            self.assertEqual(calls, ['rospatent_tz'])
            self.assertEqual(result['bytes_downloaded'], 7)
            self.assertTrue((self.cp.dataset_root('rospatent_tz', root=root) / 'индекс.json.gz').is_file())
            import rospatent_tz
            self.cp.configure_readers(root=root)
            actual, note = rospatent_tz.lookup('7707083893', сегодня='2026-10-16')
            self.assertEqual(note, 'ok')
            self.assertEqual(actual['дата_выгрузки'], '2026-10-16')
            self.assertFalse(actual['в_реестре'])
            self.assertEqual(outside.read_text(), 'unchanged')
            self.assertFalse((root / 'cbr_reg').exists())

    def test_schema_disk_budget_timeout_preserve_previous(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_index(root)
            before = (root / 'tz' / 'индекс.json.gz').read_bytes()
            for fault in ('schema', 'disk', 'budget', 'timeout'):
                def worker(item, home, ledger, budget, deadline, fault=fault):
                    if fault == 'disk':
                        raise OSError(28, 'disk full')
                    if fault == 'timeout':
                        raise TimeoutError('deadline')
                    write_index(home / '.cache' / 'inn-check-ru', valid=fault != 'schema')
                    ledger.write_text(json.dumps({'bytes_downloaded': 101 if fault == 'budget' else 5}))
                    return {'returncode': 0}
                with patch.object(self.cp, '_run_worker', side_effect=worker):
                    result = self.cp.execute(['rospatent_tz'], profile='нейтрально', max_bytes=100,
                                             timeout_seconds=1, root=root)
                self.assertTrue(result['errors'], (fault, result))
                self.assertTrue(result['preserved_previous'])
                self.assertEqual((root / 'tz' / 'индекс.json.gz').read_bytes(), before)
                self.assertEqual(self.cp.dataset_root('rospatent_tz', root=root), root / 'tz')

    def test_real_legacy_updater_in_bounded_child_and_reader(self):
        import os

        import rospatent_tz
        import run_rospatent_tz_eval as fixtures
        meta = fixtures.meta(дата='20261015', url=fixtures.URL.replace('20260902', '20261015'))
        body = fixtures.csv_байты()
        bootstrap = (
            'import io,runpy,urllib.request\n'
            'class Response(io.BytesIO):\n'
            '    headers={}\n'
            'def fixture(self,request,*a,**k):\n'
            '    url=request.full_url if hasattr(request,"full_url") else request\n'
            '    if url.endswith("/meta.csv"): return Response(%r)\n'
            '    if url.endswith("data-20261015-structure-20180828.csv"): return Response(%r)\n'
            '    raise AssertionError("unexpected URL")\n'
            'urllib.request.OpenerDirector.open=fixture\n'
            'runpy.run_module("cache_update_worker",run_name="__main__")\n' % (meta, body))
        transport = {'bootstrap': bootstrap}
        original = self.cp._worker_argv
        def command(item, ledger, maximum):
            return [sys.executable, '-c', transport['bootstrap'], *original(item, ledger, maximum)[3:]]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'cache'
            sentinel = Path(directory) / 'outside'
            sentinel.mkdir()
            environment_before = dict(os.environ)
            with patch.object(self.cp, '_worker_argv', side_effect=command):
                result = self.cp.execute(['rospatent_tz'], profile='нейтрально', max_bytes=5000,
                                         timeout_seconds=5, root=root)
                self.assertFalse(result['errors'], result)
                self.assertEqual(result['bytes_downloaded'], len(meta) + len(body))
                row, _ = rospatent_tz.lookup('7707083893', сегодня='2026-10-16')
                self.assertEqual(row['знаков'], 3)
                pointer_before = (root / '.workflow-cache.json').read_bytes()
                transport['bootstrap'] = bootstrap.replace(repr(body), repr(fixtures.csv_байты([fixtures.СТРОКИ[1]]))).replace('20261015', '20261016')
                shrunk = self.cp.execute(['rospatent_tz'], profile='нейтрально', max_bytes=5000,
                                         timeout_seconds=5, root=root)
                self.assertTrue(shrunk['errors'], shrunk)
                self.assertEqual((root / '.workflow-cache.json').read_bytes(), pointer_before)
                transport['bootstrap'] = bootstrap
                failed = self.cp.execute(['rospatent_tz'], profile='нейтрально', max_bytes=5,
                                         timeout_seconds=5, root=root)
                self.assertTrue(failed['errors'])
                self.assertEqual((root / '.workflow-cache.json').read_bytes(), pointer_before)
                row, _ = rospatent_tz.lookup('7707083893', сегодня='2026-10-16')
                self.assertEqual(row['знаков'], 3)
            self.assertEqual(dict(os.environ), environment_before)
            self.assertEqual(list(sentinel.iterdir()), [])

    @unittest.skipIf(sys.platform == 'win32', 'symlink creation requires Windows developer mode')
    def test_preexisting_generation_symlink_cannot_escape_explicit_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'cache'
            outside = Path(directory) / 'outside'
            root.mkdir()
            outside.mkdir()
            (root / '.workflow-generations').symlink_to(outside, target_is_directory=True)
            with patch.object(self.cp, '_run_worker') as worker:
                result = self.cp.execute(['rospatent_tz'], profile='нейтрально', max_bytes=5,
                                         timeout_seconds=1, root=root)
            self.assertTrue(result['errors'])
            worker.assert_not_called()
            self.assertEqual(list(outside.iterdir()), [])

    def test_timeout_stops_actual_child_and_second_lock_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.cp._writer_lock(root):
                report = self.cp.execute(['rospatent_tz'], profile='нейтрально', max_bytes=1,
                                         timeout_seconds=1, root=root)
                self.assertTrue(report['errors'])
            marker = root / 'late'
            with patch.object(self.cp, '_worker_argv', return_value=[sys.executable, '-c',
                              'import time,pathlib;time.sleep(1);pathlib.Path(%r).touch()' % str(marker)]):
                result = self.cp.execute(['rospatent_tz'], profile='нейтрально', max_bytes=5,
                                         timeout_seconds=.1, root=root)
            self.assertTrue(result['errors'])
            time.sleep(1.1)
            self.assertFalse(marker.exists())

    def test_redirect_body_budget_actual_stdlib_pipeline(self):
        bootstrap = """
import io, runpy, urllib.request, urllib.response
from email.message import Message
class Handler(urllib.request.HTTPHandler):
    def http_open(self, req):
        headers = Message()
        redirected = not req.full_url.endswith('/destination')
        body = b'x' * 100000 if redirected else b'ok'
        headers['Content-Length'] = str(len(body))
        if redirected: headers['Location'] = 'http://fixture.invalid/destination'
        response = urllib.response.addinfourl(io.BytesIO(body), headers, req.full_url, 302 if redirected else 200)
        response.msg = 'fixture'
        return response
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({}), Handler()))
def run(*a, **k):
    urllib.request.urlopen('http://fixture.invalid/start').read()
    print('{"статус":"обновлено"}')
runpy.run_module = run
import cache_update_worker
cache_update_worker._seed_previous = lambda *a: None
import cache_policy
cache_policy._validate = lambda *a: None
raise SystemExit(cache_update_worker.main())
"""
        import subprocess
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / 'bytes.json'
            process = subprocess.run([sys.executable, '-c', bootstrap, '--updater', 'sanctions_check',
                                      '--max-bytes', '10', '--ledger', str(ledger)],
                                     env={'PYTHONPATH': str(Path(self.cp.__file__).parent), 'HOME': directory},
                                     capture_output=True, timeout=5, check=False)
            self.assertNotEqual(process.returncode, 0, process.stdout)
            self.assertLessEqual(json.loads(ledger.read_text())['bytes_downloaded'], 10)

    def test_sanctions_actual_child_rejects_truncated_content_length(self):
        import sanctions_check
        fixtures = Path(__file__).parent / 'fixtures' / 'sanctions'
        bodies = {row['url']: (fixtures / filename).read_bytes()
                  for row, filename in zip(sanctions_check.SOURCES.values(),
                                          ('rfm_sample.html', 'sdn_sample.csv', 'eu_sample.xml'))}
        bootstrap = r"""
import io, runpy, urllib.request, http.client
bodies = %r
truncated = %r
class Socket:
    def __init__(self, data): self.data = data
    def makefile(self, *a): return io.BytesIO(self.data)
def fixture(self, request, *a, **k):
    url = request.full_url if hasattr(request, 'full_url') else request
    body = bodies[url]
    length = len(body) + (10000 if truncated and url.endswith('sdn.csv') else 0)
    response = http.client.HTTPResponse(Socket(b'HTTP/1.1 200 OK\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: ' + str(length).encode() + b'\r\n\r\n' + body))
    response.begin()
    return response
urllib.request.OpenerDirector.open = fixture
runpy.run_module('cache_update_worker', run_name='__main__')
"""
        original = self.cp._worker_argv
        mode = {'truncated': False}
        def command(item, ledger, maximum):
            return [sys.executable, '-c', bootstrap % (bodies, mode['truncated']),
                    *original(item, ledger, maximum)[3:]]
        with tempfile.TemporaryDirectory() as directory, patch.object(self.cp, '_worker_argv', side_effect=command):
            root = Path(directory)
            first = self.cp.execute(['sanctions'], profile='нейтрально', max_bytes=100000,
                                    timeout_seconds=5, root=root)
            self.assertFalse(first['errors'], first)
            before = (root / '.workflow-cache.json').read_bytes()
            old_lookup = sanctions_check._load('ofac')
            mode['truncated'] = True
            second = self.cp.execute(['sanctions'], profile='нейтрально', max_bytes=100000,
                                     timeout_seconds=5, root=root)
            self.assertTrue(second['errors'], second)
            self.assertEqual((root / '.workflow-cache.json').read_bytes(), before)
            self.assertEqual(sanctions_check._load('ofac'), old_lookup)

    def test_fns_head_probe_follows_bodyless_redirect_actual_stdlib_pipeline(self):
        import http.client
        import urllib.request

        import opendata_refresh
        from cache_update_worker import DownloadBudget, bounded_transport

        seen = []
        class Socket:
            def __init__(self, data):
                self.data = data
            def makefile(self, *args):
                return io.BytesIO(self.data)
        class Handler(urllib.request.HTTPHandler):
            def http_open(self, request):
                redirected = request.full_url.endswith('/start')
                headers = (
                    b'HTTP/1.1 302 Found\r\nContent-Length: 100\r\n'
                    b'Location: http://fixture.invalid/destination\r\n\r\n'
                    if redirected else
                    b'HTTP/1.1 200 OK\r\nContent-Length: 5000\r\n\r\n')
                response = http.client.HTTPResponse(Socket(headers), method=request.get_method())
                response.begin()
                response.url, response.msg = request.full_url, 'fixture'
                seen.append((request.get_method(), response.status, response.length))
                return response
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), Handler())
        with patch.object(urllib.request, '_opener', opener), tempfile.TemporaryDirectory() as directory:
            url = 'http://fixture.invalid/start'
            self.assertEqual(opendata_refresh._размер_на_сервере(url, None), 5000)
            # Python 3.12's redirect handler rebuilds the final request as GET,
            # including when the original request used HEAD.
            self.assertEqual(seen, [('HEAD', 302, 0), ('GET', 200, 5000)])
            seen.clear()
            ledger = DownloadBudget(1000, Path(directory) / 'bytes.json')
            with bounded_transport(ledger):
                self.assertEqual(opendata_refresh._размер_на_сервере(url, None), 5000)
            self.assertEqual(seen, [('HEAD', 302, 0), ('GET', 200, 5000)])
            self.assertEqual(ledger.downloaded, 0)

    def test_http_response_bodyless_methods_and_statuses_ignore_representation_length(self):
        import http.client

        from cache_update_worker import BoundedResponse, DownloadBudget
        class Socket:
            def __init__(self, data):
                self.data = data
            def makefile(self, *args):
                return io.BytesIO(self.data)
        with tempfile.TemporaryDirectory() as directory:
            for method, status in (('HEAD', 200), ('HEAD', 302), ('GET', 101),
                                   ('GET', 204), ('GET', 304)):
                for chunked in (False, True):
                    with self.subTest(method=method, status=status, chunked=chunked):
                        headers = b'Content-Length: 5000\r\n'
                        if chunked:
                            headers += b'Transfer-Encoding: chunked\r\n'
                        response = http.client.HTTPResponse(Socket(
                            b'HTTP/1.1 ' + str(status).encode() + b' Fixture\r\n' + headers + b'\r\n'),
                            method=method)
                        response.begin()
                        self.assertEqual(response.length, 0)
                        ledger = DownloadBudget(10, Path(directory) / 'bytes.json')
                        with BoundedResponse(response, ledger) as stream:
                            self.assertEqual(stream.read(), b'')
                            self.assertEqual(stream.read1(), b'')
                            self.assertEqual(stream.readline(), b'')
                            self.assertEqual(stream.readinto(bytearray(2)), 0)
                            self.assertEqual(stream.readinto1(bytearray(2)), 0)
                        self.assertEqual(ledger.downloaded, 0)

    def test_http_response_methods_chunking_gzip_and_incomplete_reads(self):
        import http.client

        from cache_update_worker import BoundedResponse, BudgetExceeded, DownloadBudget
        class Socket:
            def __init__(self, data):
                self.data = data
            def makefile(self, *args):
                return io.BytesIO(self.data)
        def response(data, headers):
            stream = http.client.HTTPResponse(Socket(b'HTTP/1.1 200 OK\r\n' + headers + b'\r\n\r\n' + data))
            stream.begin()
            return stream
        with tempfile.TemporaryDirectory() as directory:
            for method in ('read', 'read1', 'readinto', 'readinto1', 'readline'):
                for chunked in (False, True):
                    with self.subTest(method=method, chunked=chunked):
                        data = b'ab\ncd'
                        payload = b'3\r\nab\n\r\n2\r\ncd\r\n0\r\n\r\n' if chunked else data
                        headers = b'Transfer-Encoding: chunked\r\nContent-Length: 999' if chunked else b'Content-Length: 5'
                        ledger = DownloadBudget(20, Path(directory) / 'bytes.json')
                        stream = BoundedResponse(response(payload, headers), ledger)
                        out = bytearray()
                        while True:
                            if 'into' in method:
                                target = bytearray(2)
                                count = getattr(stream, method)(target)
                                part = target[:count]
                            else:
                                part = getattr(stream, method)(2)
                            if not part:
                                break
                            out.extend(part)
                        self.assertEqual(out, data)
                        self.assertEqual(ledger.downloaded, len(data))
            for headers, payload in ((b'Content-Length: 20', b'abc'),
                                     (b'Transfer-Encoding: chunked', b'5\r\nabc')):
                ledger = DownloadBudget(10, Path(directory) / 'bytes.json')
                stream = BoundedResponse(response(payload, headers), ledger)
                with self.assertRaises(BudgetExceeded):
                    stream.read()
                self.assertEqual(ledger.downloaded, 3)
            zipped = gzip.compress(b'hello' * 100)
            ledger = DownloadBudget(len(zipped), Path(directory) / 'bytes.json')
            stream = BoundedResponse(response(zipped, b'Content-Length: ' + str(len(zipped)).encode()), ledger)
            with gzip.GzipFile(fileobj=stream) as decoded:
                self.assertEqual(decoded.read(), b'hello' * 100)
            self.assertEqual(ledger.downloaded, len(zipped))

    def test_redirect_chain_counts_every_body_once_and_caps_actual_reads(self):
        import urllib.request
        import urllib.response
        from email.message import Message

        from cache_update_worker import (
            BudgetExceeded,
            DownloadBudget,
            bounded_transport,
        )
        consumed = []
        class Tracked(io.BytesIO):
            def read(self, size=-1):
                data = super().read(size)
                consumed.append(len(data))
                return data
        class Handler(urllib.request.HTTPHandler):
            def http_open(self, request):
                step = int(request.full_url.rsplit('/', 1)[1])
                headers = Message()
                body = b'abc' if step < 2 else b'ok'
                headers['Content-Length'] = str(len(body))
                if step < 2:
                    headers['Location'] = 'http://fixture.invalid/%d' % (step + 1)
                result = urllib.response.addinfourl(Tracked(body), headers, request.full_url,
                                                    302 if step < 2 else 200)
                result.msg = 'fixture'
                return result
        with tempfile.TemporaryDirectory() as directory:
            for maximum in (8, 5):
                consumed.clear()
                ledger = DownloadBudget(maximum, Path(directory) / 'bytes.json')
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), Handler())
                with bounded_transport(ledger):
                    if maximum == 8:
                        with opener.open('http://fixture.invalid/0') as response:
                            self.assertEqual(response.read(), b'ok')
                    else:
                        with self.assertRaises(BudgetExceeded):
                            opener.open('http://fixture.invalid/0').read()
                self.assertEqual(ledger.downloaded, maximum)
                self.assertEqual(sum(consumed), maximum)

    def test_child_deadline_covers_seed_and_validation(self):
        original = self.cp._worker_argv
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'cache'
            write_index(root)
            before = (root / 'tz' / 'индекс.json.gz').read_bytes()
            for phase in ('seed', 'validation'):
                marker = Path(directory) / ('late-' + phase)
                bootstrap = (
                    'import time,pathlib,runpy,json\n'
                    'import cache_policy,cache_update_worker\n'
                    'def slow(*a):\n'
                    '    time.sleep(1)\n'
                    '    pathlib.Path(%r).touch()\n'
                    'if %r == "seed": cache_update_worker._seed_previous = slow\n'
                    'else: cache_policy._validate = slow\n'
                    'runpy.run_module = lambda *a,**k: print(json.dumps({"статус":"обновлено"}))\n'
                    'raise SystemExit(cache_update_worker.main())\n' % (str(marker), phase))
                def command(item, ledger, maximum, bootstrap=bootstrap):
                    return [sys.executable, '-c', bootstrap, *original(item, ledger, maximum)[3:]]
                with patch.object(self.cp, '_worker_argv', side_effect=command):
                    report = self.cp.execute(['rospatent_tz'], profile='нейтрально', max_bytes=5,
                                             timeout_seconds=.2, root=root)
                self.assertTrue(report['errors'], (phase, report))
                self.assertLess(report['elapsed_seconds'], 1)
                self.assertEqual((root / 'tz' / 'индекс.json.gz').read_bytes(), before)
                self.assertFalse((root / '.workflow-cache.json').exists())
                time.sleep(1)
                self.assertFalse(marker.exists(), phase)


if __name__ == '__main__':
    unittest.main()
