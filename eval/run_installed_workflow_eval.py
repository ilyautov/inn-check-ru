#!/usr/bin/env python3
"""Build/install outside checkout; fixed offline fixtures, actual executable and stdio.

Requires build tooling, optional --mcp installs the bounded SDK extra. No PYTHONPATH
or public fixture environment is used. Test index may be supplied by release owner
through ordinary GIT_INDEX_FILE; this script never stages files or modifies Git.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(command, *, cwd, env, timeout=180):
    proc = subprocess.run([str(v) for v in command], cwd=cwd, env=env, check=False,
                          text=True, encoding='utf-8', capture_output=True, timeout=timeout)
    if proc.returncode:
        raise AssertionError(f'{command[0]} exited {proc.returncode}:\n{proc.stdout}\n{proc.stderr}')
    return proc.stdout


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def check(condition, detail):
    if not condition:
        raise AssertionError(detail)



def standalone_zip_acceptance(archive, *, python, scratch, env):
    """Run the shipped skill alone: no checkout, SDK, site imports or PYTHONPATH.

    Only this acceptance harness injects the fixed recorded registry adapter.
    Production CLI/monitor arguments have no executable/fixture override.
    """
    unpacked = scratch / 'standalone-skill'
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(unpacked)
    check(not (unpacked / 'mcp').exists(), 'standalone ZIP unexpectedly contains MCP transport')
    home = scratch / 'zip-home'
    home.mkdir()
    clean = dict(env)
    clean.pop('PYTHONPATH', None)
    clean.pop('PYTHONHOME', None)
    clean.update(HOME=str(home), USERPROFILE=str(home), XDG_CACHE_HOME=str(home / 'cache'),
                 XDG_DATA_HOME=str(home / 'data'), PYTHONNOUSERSITE='1')
    cli = unpacked / 'scripts/workflow_cli.py'
    document = scratch / 'zip-document.txt'
    document.write_text('Поставщик ИНН 7707083893', encoding='utf-8')
    commands = []
    def invoke(*args):
        command = [python, '-S', cli, *args, '--json']
        commands.append([str(v) for v in command])
        return json.loads(run(command, cwd=scratch, env=clean, timeout=30))
    extraction = invoke('document', 'extract', str(document))
    check(extraction['status'] != 'error' and extraction['data']['candidates'], extraction)
    candidates = scratch / 'zip-candidates.json'
    candidates.write_text(json.dumps(extraction), encoding='utf-8')
    candidate = extraction['data']['candidates'][0]['candidate_id']
    selected = invoke('document', 'select', str(candidates), '--role', 'supplier', '--candidate', candidate)
    check(selected['data']['selected']['inn'] == '7707083893', selected)
    version_binary = scratch / ('zip-tesseract.cmd' if os.name == 'nt' else 'zip-tesseract')
    version_binary.write_text('@echo tesseract 5.5.1\r\n' if os.name == 'nt' else
                              '#!' + str(python) + '\nprint("tesseract 5.5.1")\n', encoding='utf-8')
    version_binary.chmod(0o700)
    clean['INN_CHECK_TESSERACT'] = str(version_binary)
    doctor = invoke('doctor')
    check(doctor['status'] != 'error', doctor)
    binary = doctor['data']['optional']['binaries']['tesseract']
    check(binary['version_ready'] and binary['version'] == 'tesseract 5.5.1', binary)
    # The outer harness runs with -S; the actual fixed monitor runner still
    # launches/owns the child. Substitute only its local fixture adapter command.
    recorded = scratch / 'zip-recorded-child.py'
    recorded.write_text(r"""import copy, importlib.util, json, runpy, sys
from pathlib import Path
from unittest.mock import patch
script = Path(sys.argv.pop(1)).resolve()
sys.path.insert(0, str(script.parent))
assert importlib.util.find_spec('inn_check_ru_mcp') is None
import fetch_counterparty as collector
raw = (Path(__file__).parent / 'egrul.raw.json').read_bytes()
def no_network(event, args):
    if event.startswith('socket.') and event in {'socket.connect', 'socket.connect_ex', 'socket.getaddrinfo', 'socket.bind'}:
        raise AssertionError('standalone monitor attempted network: ' + event)
sys.addaudithook(no_network)
def collect(inn, **kwargs):
    assert inn == '7707083893'
    collector._ИСТОЧНИК.id = 'егрюл'
    collector._ИСТОЧНИК.subject_inn = inn
    collector._записать('GET', 'https://egrul.nalog.ru/', 200, raw)
    block, av = collector.parse_egrul(json.loads(raw), inn)
    kwargs['on_source']('егрюл', copy.deepcopy(block), copy.deepcopy(av))
    (Path(__file__).parent / 'zip-source-calls.json').write_text(json.dumps(['егрюл']))
    return {'инн': inn, 'тип': 'ЮЛ', 'егрюл': block, '_доступность': {'егрюл': av}}
with patch.object(collector, 'collect', side_effect=collect):
    runpy.run_path(str(script), run_name='__main__')
""", encoding='utf-8')
    shutil.copyfile(ROOT / 'eval/fixtures/net/егрюл_7707083893.raw.json', scratch / 'egrul.raw.json')
    harness = scratch / 'zip-monitor.py'
    harness.write_text(r"""import importlib.util, json, sys
from pathlib import Path
from unittest.mock import patch
scripts, adapter, base = map(Path, sys.argv[1:])
sys.path.insert(0, str(scripts))
assert importlib.util.find_spec('inn_check_ru_mcp') is None
import document_input, process_runtime, run_store, watchlist
from sibling_imports import load_sibling
assert document_input._process_helpers() is process_runtime is load_sibling('process_runtime')
assert process_runtime._ProcessStartError is load_sibling('process_runtime')._ProcessStartError
def no_network(event, args):
    if event in {'socket.connect', 'socket.connect_ex', 'socket.getaddrinfo', 'socket.bind'}:
        raise AssertionError('standalone parent attempted network: ' + event)
sys.addaudithook(no_network)
base.mkdir()
(base / 'watchlist.json').write_text(json.dumps({'контрагенты': [{'инн': '7707083893', 'профиль': 'нейтрально'}]}))
start = process_runtime._start_process
launches = []
def recorded(command, **kwargs):
    assert Path(command[3]) == scripts / 'workflow_cli.py', command
    assert command[4:7] == ['run', 'start', '7707083893'], command
    launches.append(command)
    return start([sys.executable, '-S', str(adapter), *command[3:]], **kwargs)
with patch.object(process_runtime, '_start_process', side_effect=recorded):
    result = watchlist.monitor('нейтрально', root=base)
assert result['проверено'] == 1 and len(launches) == 1, result
state = json.loads((base / 'monitor-state.json').read_text())
saved = state['subjects']['нейтрально:7707083893']
record = run_store.read(saved['run_id'], revision=saved['revision'], root=base / 'runs')
assert record['payload']['metadata']['raw_refs']['responses']
assert record['payload']['fetch']['егрюл']['дата_регистрации']
print(json.dumps({'subjects': result['проверено'], 'owned_launches': len(launches), 'run_id': saved['run_id'], 'sdk_present': False}))
""", encoding='utf-8')
    command = [python, '-S', harness, unpacked / 'scripts', recorded, scratch / 'zip-monitor-store']
    commands.append([str(v) for v in command])
    monitoring = json.loads(run(command, cwd=scratch, env=clean, timeout=60))
    check(json.loads((scratch / 'zip-source-calls.json').read_text()) == ['егрюл'], 'monitor fixture call drift')
    return {'status': 'passed', 'execution': 'unpacked_zip_python_S_no_sdk_no_pythonpath_fresh_home',
            'document_extract_select': 'passed', 'configured_doctor_version': binary['version'],
            'monitor': monitoring, 'source_calls': ['егрюл'], 'live_network_calls': 0, 'commands': commands}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mcp', action='store_true')
    parser.add_argument('--zip-only', action='store_true', help='Focused standalone skill ZIP acceptance')
    parser.add_argument('--rollback-ref', default='3e06046995860b5360df5dd3a6110bd7108d36a3',
                        help='Committed pre-workflow package source; never downloaded from PyPI')
    parser.add_argument('--artifacts', type=Path, help='Keep wheel, ZIP and portable acceptance JSON here')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='inn-installed-') as name:
        scratch = Path(name).resolve()
        artifacts = args.artifacts.resolve() if args.artifacts else scratch / 'artifacts'
        artifacts.mkdir(parents=True, exist_ok=True)
        env = {k: v for k, v in os.environ.items() if k not in ('PYTHONPATH', 'PYTHONHOME')
               and not k.startswith(('CHECKO_', 'DADATA_', 'INN_CHECK_', 'COUNTERPARTY_'))}
        secret_sentinel = 'INSTALLED_ACCEPTANCE_SECRET_SENTINEL_9D32'
        env['CHECKO_API_KEY'] = secret_sentinel
        home = scratch / 'home'
        home.mkdir()
        env.update(HOME=str(home), USERPROFILE=str(home), XDG_CACHE_HOME=str(home / 'cache'),
                   XDG_DATA_HOME=str(home / 'data'), PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1')
        project_text = (ROOT / 'pyproject.toml').read_text()
        modules = re.findall(r'"([a-z_0-9]+)"', project_text.split('py-modules = [')[1].split(']')[0])
        data = sorted(p.name for p in (ROOT / 'data').glob('*.json'))
        version = re.search(r'^version = "([^"]+)"', project_text, re.MULTILINE)[1]
        wheel = artifacts / f'inn_check_ru-{version}-py3-none-any.whl'
        wheel_input_sha256 = {}
        if not args.zip_only:
            # Backend build/egg-info directories belong to this run. Copy only
            # the declared flat modules, package sources/data and build metadata.
            tracked = set(subprocess.check_output(['git', '-C', str(ROOT), 'ls-files', '-z'],
                                                 env=env, text=True).split('\0'))
            inputs = {'pyproject.toml', 'README.md', 'LICENSE', 'data/__init__.py'}
            inputs.update('scripts/' + module + '.py' for module in modules)
            inputs.update(str(path.relative_to(ROOT)) for path in (ROOT / 'mcp').glob('*.py'))
            inputs.update('data/' + filename for filename in data)
            inputs.update(str(path.relative_to(ROOT)) for path in (ROOT / 'data/ca').glob('*.pem'))
            build_source = scratch / 'wheel-source'
            build_source.mkdir()
            for filename in sorted(inputs):
                source = ROOT / filename
                check(filename in tracked and source.is_file() and not source.is_symlink(),
                      'wheel input must be a tracked regular source: ' + filename)
                target = build_source / filename
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
                wheel_input_sha256[filename] = digest(target)
            run([sys.executable, '-m', 'build', '--wheel', '--outdir', artifacts, build_source], cwd=scratch, env=env)
        archive = artifacts / 'inn-check-ru.zip'
        run([sys.executable, ROOT / 'scripts/build_release_zip.py', ROOT, '--output', archive], cwd=scratch, env=env)
        if not args.zip_only:
            with zipfile.ZipFile(wheel) as z:
                names = sorted(z.namelist())
                check(all(m + '.py' in names for m in modules), 'wheel module inventory incomplete')
                check(all('inn_check_ru_data/' + p in names for p in data), 'wheel data inventory incomplete')
        with zipfile.ZipFile(archive) as z:
            zip_names = sorted(z.namelist())
            missing = ['scripts/' + m + '.py' for m in modules if 'scripts/' + m + '.py' not in zip_names]
            missing += ['data/' + p for p in data if 'data/' + p not in zip_names]
            check(not missing, 'release ZIP missing tracked inputs; coordinate named temporary index: ' + repr(missing))
            check(z.testzip() is None, 'invalid ZIP CRC')
        standalone = standalone_zip_acceptance(archive, python=sys.executable, scratch=scratch, env=env)
        if args.zip_only:
            report = {'schema_version': 1, 'status': 'passed', 'python': sys.version.split()[0],
                      'zip_sha256': digest(archive), 'zip_inventory': zip_names, 'standalone_zip': standalone}
            (artifacts / 'standalone-zip-acceptance.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        venv = scratch / 'venv'
        run([sys.executable, '-m', 'venv', venv], cwd=scratch, env=env)
        bin_dir = venv / ('Scripts' if os.name == 'nt' else 'bin')
        python = bin_dir / ('python.exe' if os.name == 'nt' else 'python')
        executable = bin_dir / ('inn-check-workflow.exe' if os.name == 'nt' else 'inn-check-workflow')
        run([python, '-m', 'pip', 'install', '--disable-pip-version-check', str(wheel) + ('[mcp]' if args.mcp else '')], cwd=scratch, env=env)
        helper = scratch / 'child.py'
        shutil.copyfile(ROOT / 'eval/fixtures/installed/child.py', helper)
        shutil.copyfile(ROOT / 'eval/fixtures/net/егрюл_7707083893.raw.json', scratch / 'egrul.raw.json')

        def child(action, *arguments):
            return json.loads(run([python, '-I', helper, action, *arguments], cwd=scratch, env=env))

        origins = child('origins', *modules)
        check(origins['sdk_present'] == args.mcp, 'core install unexpectedly contains SDK or optional SDK missing')
        for entry in ('inn-check-ru', 'inn-check-ru-mcp', 'inn-check-workflow'):
            check((bin_dir / (entry + ('.exe' if os.name == 'nt' else ''))).exists(), 'entrypoint missing: ' + entry)
        # Invoke the installed console script, not only the module adapter.
        doctor = json.loads(run([executable, 'doctor', '--json'], cwd=scratch, env=env))
        check(doctor['schema_version'] == 1 and doctor['status'] != 'error', doctor)
        plan = json.loads(run([executable, 'cache', 'plan', '--json'], cwd=scratch, env=env))
        check(plan['status'] != 'error', plan)
        store = scratch / 'runs'

        def cli(*arguments, action='cli', expected_error=None, expected_calls=None, root=store):
            output = child(action, '--store', str(root), *arguments, '--json')
            result = output['result']
            if expected_error:
                check(result.get('error', {}).get('code') == expected_error, result)
            else:
                check(output['exit_code'] == 0 and result['status'] != 'error', result)
            calls = [] if action == 'cli' else ['егрюл'] if action == 'start' else expected_calls
            check(calls is not None and output['source_calls'] == calls, 'unexpected source calls: ' + repr(output['source_calls']))
            return result

        one = cli('run', 'start', '7707083893', action='start')
        rid = one['run_id']
        check(cli('run', 'resume', rid)['data'] == one['data'], 'restart revision drift')
        manual = scratch / 'manual.json'
        manual.write_text(json.dumps(child('manual', dt.datetime.now(dt.timezone.utc).date().isoformat())), encoding='utf-8')
        operation = str(uuid.uuid4())
        two = cli('run', 'manual', rid, '--block-json', str(manual), '--expected-revision', str(one['revision']), '--operation-id', operation)
        check(cli('run', 'manual', rid, '--block-json', str(manual), '--expected-revision', str(one['revision']), '--operation-id', operation) == two, 'manual retry drift')
        check(cli('run', 'resume', rid)['data'] == two['data'], 'manual restart drift')
        document = scratch / 'document.txt'
        document.write_text('Поставщик ИНН 7707083893', encoding='utf-8')
        extraction = cli('document', 'extract', str(document))
        extracted_file = scratch / 'candidates.json'
        extracted_file.write_text(json.dumps(extraction), encoding='utf-8')
        candidate = extraction['data']['candidates'][0]['candidate_id']
        selected = cli('document', 'select', str(extracted_file), '--role', 'supplier', '--candidate', candidate)
        check(selected['data']['selected']['inn'] == '7707083893', selected)
        markdown = cli('run', 'render', rid, '--format', 'markdown')
        check(markdown['data']['source_revision'] == two['revision'], 'render source revision drift')
        exported = cli('run', 'export', rid, '--format', 'docx')
        before_snapshot = cli('run', 'snapshot', rid)
        operation = str(uuid.uuid4())
        refresh_args = ('run', 'refresh', rid, '--source', 'риски', '--expected-revision', str(two['revision']), '--operation-id', operation)
        refreshed = cli(*refresh_args, action='refresh', expected_calls=['риски'])
        check(refreshed['revision'] > two['revision'], 'refresh did not commit a new revision')
        check(cli(*refresh_args, action='refresh', expected_calls=[]) == refreshed, 'refresh retry drift')
        after_snapshot = cli('run', 'snapshot', rid)
        changes = child('diff', rid, before_snapshot['data']['artifact_id'], after_snapshot['data']['artifact_id'], str(store))
        check(isinstance(changes['changes'], list), changes)
        queue = scratch / 'queue'
        seeded = child('events-seed', str(queue))
        check(len(seeded) == 2, seeded)
        claimed = cli('events', 'claim', '--consumer-id', 'acceptance', root=queue)['data']
        check(len(claimed) == 2, claimed)
        cli('events', 'ack', claimed[0]['event_id'], '--consumer-id', 'acceptance', root=queue)
        cli('events', 'retry', claimed[1]['event_id'], '--consumer-id', 'acceptance', root=queue)
        check(len(cli('events', 'list', '--state', 'acknowledged', root=queue)['data']) == 1, 'ack not durable')
        retried = next(event for event in cli('events', 'list', '--state', 'all', root=queue)['data'] if event['event_id'] == claimed[1]['event_id'])
        check(retried['delivery']['state'] == 'pending' and retried['delivery']['attempts'] == 1, 'retry not durable')
        cutoff = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1)).isoformat()
        original = cli('run', 'replay', rid, '--mode', 'original', '--cutoff', cutoff)
        check(original['data']['data'] == refreshed['data'], 'original replay changed saved data')
        cli('run', 'replay', rid, '--mode', 'current_rules', '--cutoff', cutoff)
        verified = child('verify-store', str(store), rid)
        restored = scratch / 'restored'
        shutil.copytree(store, restored)
        restored_verified = child('verify-store', str(restored), rid)
        check(verified == restored_verified, 'restored committed revisions/raw/artifacts drift')
        replay = cli('run', 'replay', rid, '--mode', 'original', '--cutoff', cutoff, root=restored)
        check(replay == original, 'restored replay changed')
        child('old-client', str(restored), rid)
        head_path = restored / rid / 'HEAD.json'
        head = json.loads(head_path.read_text()); head['schema_version'] = 999
        head_path.write_text(json.dumps(head), encoding='utf-8')
        before = {str(p.relative_to(restored)): digest(p) for p in restored.rglob('*') if p.is_file()}
        cli('run', 'resume', rid, root=restored, expected_error='unsupported_schema')
        check(before == {str(p.relative_to(restored)): digest(p) for p in restored.rglob('*') if p.is_file()}, 'unknown schema mutated store')
        other = cli('run', 'start', '7728168971', action='start')
        compare_at = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1)).isoformat()
        comparison = cli('compare', rid, other['run_id'], '--as-of', compare_at)
        stdio = 'not_run_no_optional_sdk'
        if args.mcp:
            sys.path.insert(0, str(ROOT / 'eval'))
            from mcp_handshake import StdioClient, decode_tool_json
            client = StdioClient([str(python), '-I', '-m', 'inn_check_ru_mcp.server'],
                env=dict(env, INN_CHECK_RUNS_DIR=str(store)), cwd=str(scratch))
            try:
                client.initialize()
                response = client.call({'jsonrpc':'2.0','id':2,'method':'tools/list','params':{}})
                check(len(response['result']['tools']) == 33, response)
                actual_tools = [{k: tool[k] for k in ('name', 'inputSchema', 'annotations')} for tool in response['result']['tools']]
                expected_tools = json.loads((ROOT / 'eval/fixtures/workflow-tools.json').read_text())
                check(sorted(actual_tools, key=lambda t: t['name']) == sorted(expected_tools, key=lambda t: t['name']), 'installed SDK snapshot mismatch')
                calls = [
                    ('counterparty_compare', {'run_ids':[rid,other['run_id']], 'as_of':compare_at}),
                    ('counterparty_run_read', {'run_id':rid}),
                    ('counterparty_run_replay', {'run_id':rid,'mode':'original','cutoff':cutoff}),
                    ('counterparty_document_extract', {'text':'Поставщик ИНН 7707083893'}),
                    ('counterparty_document_select', {'candidates':extraction['data']['candidates'],'role':'supplier','candidate_id':candidate})]
                for number, (tool, arguments) in enumerate(calls, 3):
                    output = decode_tool_json(client.call({'jsonrpc':'2.0','id':number,'method':'tools/call','params':{'name':tool,'arguments':arguments}}))
                    check(output['status'] != 'error', output)
                    if tool == 'counterparty_compare':
                        check(output['data'] == comparison['data'], 'installed stdio comparison mismatch')
                    if tool == 'counterparty_run_read':
                        check(output['data'] == refreshed['data'], 'installed stdio saved revision mismatch')
                    if tool == 'counterparty_run_replay':
                        check(output['data'] == original['data'], 'installed stdio replay mismatch')
                stdio = 'passed_actual_sdk_33_tools'
            finally:
                client.close()
        for path in store.rglob('*'):
            if path.is_file():
                check(secret_sentinel.encode() not in path.read_bytes(), 'secret sentinel persisted in run store')
        for path in (wheel, archive):
            with zipfile.ZipFile(path) as z:
                for member in z.namelist():
                    check(secret_sentinel.encode() not in z.read(member), 'secret sentinel shipped in artifact')
        # Actual downgrade to the previous package: it has no workflow API.
        # Do not fabricate an unsupported_schema result from an absent command.
        old_source = scratch / 'old-source'
        old_source.mkdir()
        old_archive = scratch / 'old-source.zip'
        run(['git', '-C', ROOT, 'archive', '--format=zip', '--output', old_archive, args.rollback_ref], cwd=scratch, env=env)
        with zipfile.ZipFile(old_archive) as z:
            for item in z.infolist():
                name = item.filename
                if item.is_dir() or not (name in {'pyproject.toml', 'README.md', 'LICENSE'} or name.startswith(('scripts/', 'mcp/', 'data/'))):
                    continue
                target = old_source / name
                check(target.resolve().is_relative_to(old_source), 'unsafe historical artifact path')
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(z.read(item))
        old_dist = artifacts / 'rollback'
        run([sys.executable, '-m', 'build', '--wheel', '--outdir', old_dist, old_source], cwd=scratch, env=env)
        old_wheel = next(old_dist.glob('*.whl'))
        old_version = re.search(r'^version = "([^"]+)"', (old_source / 'pyproject.toml').read_text(), re.MULTILINE)[1]
        before_downgrade = {str(p.relative_to(store)): digest(p) for p in store.rglob('*') if p.is_file()}
        run([python, '-m', 'pip', 'install', '--no-deps', '--force-reinstall', old_wheel], cwd=scratch, env=env)
        check(not executable.exists(), 'previous package unexpectedly retains workflow console script')
        probe = run([python, '-I', '-c', "import importlib.util,importlib.metadata; assert importlib.util.find_spec('run_workflow') is None; print(importlib.metadata.version('inn-check-ru'))"], cwd=scratch, env=env)
        check(probe.strip() == old_version, 'downgrade version mismatch')
        legacy = subprocess.run([str(bin_dir / ('inn-check-ru.exe' if os.name == 'nt' else 'inn-check-ru')), '123'],
                                cwd=scratch, env=env, check=False, text=True, encoding='utf-8', capture_output=True, timeout=30)
        check(legacy.returncode == 1 and 'Некорректный ИНН' in legacy.stdout + legacy.stderr, 'old one-shot validation failed')
        check(before_downgrade == {str(p.relative_to(store)): digest(p) for p in store.rglob('*') if p.is_file()}, 'package downgrade mutated store')
        rollback = {'source_ref': args.rollback_ref, 'version': old_version, 'wheel_sha256': digest(old_wheel),
                    'workflow': 'unavailable_in_previous_package', 'legacy_cli': 'passed_invalid_inn_offline', 'store_unchanged': True}
        manifests = ['pyproject.toml', 'server.json', '.mcp.json', '.claude-plugin/plugin.json',
                     'packaging/mcpb/manifest.template.json', 'Dockerfile', 'data/workflow_capabilities.json']
        report = {'schema_version':1,'status':'passed','version':version,'platform':sys.platform,
                  'python':sys.version.split()[0], 'revision':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
                  'working_tree_snapshot':True,'wheel_build_input_sha256':wheel_input_sha256,'standalone_zip':standalone,'wheel_sha256':digest(wheel),'zip_sha256':digest(archive),
                  'wheel_inventory':names,'zip_inventory':zip_names,'module_count':len(modules),
                  'manifest_sha256':{p:digest(ROOT/p) for p in manifests},
                  'runtime_source_sha256':{p:digest(ROOT/p) for p in [*[f'scripts/{m}.py' for m in modules], 'mcp/server.py', 'mcp/tools_impl.py']},'run_id':rid,
                  'final_revision':refreshed['revision'],'recovery':verified,'stdio':stdio,
                  'old_client':{'schema_reader':'simulated_schema_older_reader_refuses_without_mutation', 'actual_package_downgrade':rollback},
                  'live_paid_calls':0,'real_host_acceptance':'not_run','other_platforms':'not_run',
                  'dossier_artifact':exported['data']}
        (artifacts/'installed-acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        print(json.dumps({k:v for k,v in report.items() if k not in ('wheel_inventory','zip_inventory','recovery')},ensure_ascii=False,indent=2))
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
