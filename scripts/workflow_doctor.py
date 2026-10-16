"""Installed-package diagnostics, independent of optional MCP/PDF SDKs."""
from __future__ import annotations

import importlib
import json
import os
import platform
import re
import shutil
import sys
from importlib import metadata, resources
from pathlib import Path

import cache_policy
from redaction import redact


def sdk_status():
    try:
        version = metadata.version('mcp')
    except metadata.PackageNotFoundError:
        return {'ready': False, 'state': 'not_installed', 'version': None,
                'required': '>=1.30,<2'}
    match = re.fullmatch(r'(\d+)\.(\d+)(?:\.(\d+))?(.*)', version)
    if not match or (int(match[1]), int(match[2])) < (1, 30) or int(match[1]) >= 2 or match[4]:
        return {'ready': False, 'state': 'incompatible', 'version': version,
                'required': '>=1.30,<2'}
    try:
        module = importlib.import_module('mcp.server.fastmcp')
        if not hasattr(module, 'FastMCP'):
            raise ImportError('FastMCP missing')
    except Exception:
        return {'ready': False, 'state': 'broken_import', 'version': version,
                'required': '>=1.30,<2'}
    return {'ready': True, 'state': 'ready', 'version': version, 'required': '>=1.30,<2'}


def _installation():
    resource_names = ('canon_ru.json', 'profiles_ru.json', 'cache_policy.json')
    found = {}
    mode = 'installed'
    for name in resource_names:
        try:
            text = (resources.files('inn_check_ru_data') / name).read_text(encoding='utf-8')
        except (ModuleNotFoundError, FileNotFoundError):
            mode = 'checkout'
            try:
                text = (Path(__file__).resolve().parent.parent / 'data' / name).read_text(encoding='utf-8')
            except OSError:
                text = ''
        try:
            found[name] = isinstance(json.loads(text), dict)
        except ValueError:
            found[name] = False
    try:
        version = metadata.version('inn-check-ru')
    except metadata.PackageNotFoundError:
        version = None
    return {'mode': mode, 'version': version, 'resources': found, 'ready': all(found.values())}


def _pdf_status():
    # Distribution metadata only: doctor must not import/parse untrusted PDFs.
    try:
        version = metadata.version('pypdf')
    except metadata.PackageNotFoundError:
        return {'ready': False, 'state': 'not_installed', 'version': None, 'required': '>=6.21,<7'}
    match = re.fullmatch(r'(\d+)\.(\d+)(?:\.(\d+))?', version)
    ready = bool(match and (6, 21) <= (int(match[1]), int(match[2])) < (7, 0))
    return {'ready': ready, 'state': 'ready' if ready else 'incompatible',
            'version': version, 'required': '>=6.21,<7'}


def _binary_configurations():
    binaries = {}
    for name, variable, flag in (('tesseract', 'INN_CHECK_TESSERACT', '--version'),
                                  ('pdftoppm', 'INN_CHECK_PDF_RENDERER', '-v')):
        configured = os.environ.get(variable, '')
        executable = bool(configured and Path(configured).is_absolute() and
                          Path(configured).is_file() and os.access(configured, os.X_OK))
        binaries[name] = {'configured': bool(configured), 'configuration_variable': variable,
                          'configured_path': configured or None, 'present': executable,
                          'path_discovered': shutil.which(name),
                          'ready': False, 'version_ready': False, 'version': None,
                          'version_command': [configured, flag] if executable else None,
                          'readiness_scope': 'configured_executable_and_version',
                          'reason_code': 'not_probed' if executable else
                              'invalid_configuration' if configured else 'not_configured'}
    return binaries


def _version(command):
    import asyncio

    # Canonical shipped stdlib process owner. MCP invokes the async
    # owner in its parent instead, so cancellation cannot strand a nested group.
    from document_input import _process_helpers
    return asyncio.run(_process_helpers()._probe_binary_version(command))


def _optional(*, probe_versions=True):
    keys = [{'name': name, 'present': bool(os.environ.get(name)),
                   'source': 'environment' if os.environ.get(name) else None}
            for name in ('CHECKO_API_KEY', 'DADATA_API_KEY', 'COUNTERPARTY_CA_BUNDLE')]
    modules = {name: importlib.util.find_spec(name) is not None for name in ('pypdf',)}
    binaries = _binary_configurations()
    if probe_versions:
        for row in binaries.values():
            if row['present']:
                row.update(_version(row['version_command']))
                row['ready'] = row['version_ready']
                if row['ready']:
                    row.pop('reason_code', None)
    return {'keys': keys, 'modules': modules, 'pdf': _pdf_status(), 'binaries': binaries}


def _probe(now):
    import check_access
    import proxy
    configuration = proxy.настройка()
    if configuration['ошибка']:
        return {'probed': True, 'state': 'configuration_error', 'ttl_hours': 24}
    requester = check_access.make_requester(прокси_url=configuration['url'])
    results = check_access.run_probes(check_access.probe_targets(), requester)
    report = check_access.build_report(results, check_access.detect_ip_class(requester),
                                       now=cache_policy._moment(now), конф=configuration)
    report = redact(report, secrets=_secrets())
    check_access.write_cache(report, str(cache_policy.cache_root() / 'access.json'))
    return {'probed': True, 'state': 'completed', 'ttl_hours': 24,
            'network_fingerprint': configuration['отпечаток'], 'report': report}


def _secrets():
    # Collect only the configuration this diagnostic can use. Canonical
    # redaction also needs decoded userinfo when an error copies a proxy
    # credential without its URL. Host-owned executable/CA paths remain useful
    # diagnostics; they are not credentials by themselves.
    import urllib.parse

    found = []
    for name in ('CHECKO_API_KEY', 'DADATA_API_KEY',
                 'INN_CHECK_PLUGIN_CHECKO_KEY', 'INN_CHECK_PLUGIN_DADATA_KEY'):
        value = os.environ.get(name)
        if value:
            found.extend((value, value.strip()))
    for name in ('INN_CHECK_PROXY', 'HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy',
                 'ALL_PROXY', 'all_proxy', 'FTP_PROXY', 'ftp_proxy'):
        value = os.environ.get(name)
        if not value:
            continue
        found.extend((value, value.strip()))
        try:
            parts = urllib.parse.urlsplit(value.strip())
            for credential in (parts.username, parts.password):
                if credential:
                    found.extend((credential, urllib.parse.unquote(credential)))
        except ValueError:
            pass  # Malformed configuration is still masked as a whole.
    return tuple(found)


def inspect(profile='нейтрально', *, probe=False, now=None, probe_versions=True):
    moment = cache_policy._moment(now)
    installation = _installation()
    core_modules = ('fetch_counterparty', 'fin_scoring', 'sanctions_check', 'profiles', 'sources')
    modules = {name: importlib.util.find_spec(name) is not None for name in core_modules}
    core = {'ready': sys.version_info >= (3, 10) and all(modules.values()) and installation['ready'],
            'python': platform.python_version(), 'requires_python': '>=3.10', 'modules': modules}
    sdk = sdk_status()
    cache = cache_policy.status(profile, now=moment)
    preparation = cache_policy.plan(profile, now=moment)
    import sources
    applicable = {sid: row for sid, row in sources.SOURCES.items()
                  if row['профили'] == '*' or profile in row['профили']}
    automatic = {'sources': [sid for sid, row in applicable.items() if row['требует'] in ('сеть', 'кэш')],
                 'ready': core['ready']}
    manual = {'sources': [sid for sid, row in applicable.items() if row['требует'] == 'браузер']}
    optional = _optional(probe_versions=probe_versions)
    network = {'probed': False, 'state': 'not_probed', 'ttl_hours': 24,
               'network_fingerprint': None, 'cache_writes': False}
    if probe:
        try:
            network = {**_probe(moment), 'cache_writes': True}
        except Exception as error:
            network = {'probed': True, 'state': 'failed', 'reason_code': type(error).__name__,
                       'ttl_hours': 24, 'cache_writes': True}
    sections = {'installation': installation, 'core': core, 'sdk': sdk, 'cache': cache,
                'automatic': automatic, 'manual': manual, 'optional': optional, 'network': network}
    actions = [{'kind': 'cache', 'dataset_id': row['dataset_id'], 'argv': row['argv'],
                'optional': not row['minimal_required']} for row in preparation['actions']]
    if not sdk['ready']:
        actions.append({'kind': 'optional_sdk', 'argv': [sys.executable, '-m', 'pip', 'install',
                                                        'inn-check-ru[mcp]']})
    for binary, row in optional['binaries'].items():
        if not row['present']:
            actions.append({'kind': 'optional_binary', 'binary': binary, 'platform': sys.platform,
                            'reason_code': 'local_ocr_binary_missing'})
    result = {'schema_version': 1, 'profile': profile, 'checked_at': cache_policy._iso(moment),
              'core_ready': core['ready'], 'sdk_ready': sdk['ready'], 'sections': sections,
              'next_actions': actions, **sections}
    return redact(result, secrets=_secrets())


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', default='нейтрально')
    parser.add_argument('--probe', action='store_true')
    args = parser.parse_args(argv)
    report = inspect(args.profile, probe=args.probe)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['core_ready'] else 1


if __name__ == '__main__':
    sys.exit(main())
