#!/usr/bin/env python3
"""Offline policy, budget, export and integration acceptance checks."""
import concurrent.futures
import copy
import json
import pathlib
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
import provider_policy as pp


def policy(*, export=(), budget=4, retry=0):
    return {'schema_version': 1, 'total_cost_units': budget, 'providers': [
        {'id': 'checko', 'enabled': True, 'allowed_fields': ['name'], 'export_fields': list(export),
         'request_limit': 4, 'request_cost_units': 1, 'fallback_on': ['quota_exhausted', 'unavailable'],
         'transient_retries': retry},
        {'id': 'dadata', 'enabled': True, 'allowed_fields': ['name'], 'export_fields': list(export),
         'request_limit': 4, 'request_cost_units': 1, 'fallback_on': []}]}


class PolicyTests(unittest.TestCase):
    def test_malformed_capture_fails_before_export(self):
        import dossier
        import evidence_pack
        markers = [None, False, True, [], 'SECRET_SENTINEL', {},
                   {'schema_version': 1}, {'schema_version': True, 'policy': policy()},
                   {'schema_version': 1.0, 'policy': policy()},
                   {'schema_version': 2, 'policy': policy()},
                   {'schema_version': 1, 'policy': None},
                   {'schema_version': 1, 'policy': {'secret': 'SECRET_SENTINEL'}}]
        valid = {'schema_version': 1, 'policy': policy()}
        raw = [{'источник': 'агрегатор', 'url': 'https://api.checko.ru/v2/company',
                'метод': 'GET', 'статус': 200, 'данные': b'SECRET_SENTINEL'}]
        self.assertIsNone(pp.captured_policy({}))
        self.assertEqual(pp.captured_policy({'_provider_policy': valid}), policy())
        for marker in markers:
            source = {'_provider_policy': marker, 'инн': '7707083893',
                      'агрегатор': {'агрегатор': 'checko', 'name': 'SECRET_SENTINEL'}}
            with self.subTest(marker=marker):
                with self.assertRaisesRegex(ValueError, '^invalid_captured_provider_policy$'):
                    pp.captured_policy(source)
                with tempfile.TemporaryDirectory() as tmp:
                    out = pathlib.Path(tmp) / 'pack'
                    with self.assertRaisesRegex(ValueError, '^invalid_captured_provider_policy$'):
                        evidence_pack.записать_пакет(str(out), '7707083893', source, raw)
                    self.assertFalse(out.exists(), 'invalid capture must fail before creating any output')
                for fetch, graph in ((source, None), ({}, source),
                                     ({'_provider_policy': valid}, source)):
                    with self.assertRaisesRegex(ValueError, '^invalid_captured_provider_policy$'):
                        dossier.собрать(fetch, {}, граф=graph)
                with self.assertRaisesRegex(ValueError, '^invalid_captured_provider_policy$'):
                    pp.export_allowed(source, policy())

    def test_retry_reservation_denial_preserves_explicit_fallback(self):
        for mode in ('allowed', 'total_exhausted', 'disabled', 'fields_denied', 'fallback_denied'):
            with self.subTest(mode=mode):
                p = policy(retry=1, budget=1 if mode == 'total_exhausted' else 4)
                p['providers'][0]['request_limit'] = 1
                if mode == 'disabled': p['providers'][1]['enabled'] = False
                if mode == 'fields_denied': p['providers'][1]['allowed_fields'] = []
                if mode == 'fallback_denied': p['providers'][0]['fallback_on'] = []
                calls = []
                def request(provider, *args, calls=calls):
                    calls.append(provider)
                    return ({'status': 'error', 'reason_code': 'unavailable', 'requests_used': 1}
                            if provider == 'checko' else
                            {'status': 'ok', 'data': {'name': 'Company'}, 'requests_used': 1})
                result = pp.fetch_with_policy('7707083893', ['name'], p, request=request, ledger={})
                self.assertEqual(calls, ['checko', 'dadata'] if mode == 'allowed' else ['checko'])
                self.assertEqual(result['status'], 'ok' if mode == 'allowed' else 'partial')
                self.assertEqual(result['budget']['used_requests'], len(calls))
                self.assertEqual(result['budget']['used_cost_units'], len(calls))
                self.assertEqual(result['budget']['reserved_requests'], 0)
                self.assertEqual(result['attempts'][0]['reason_code'], 'unavailable')

    def fetch(self, p, reason='quota_exhausted'):
        calls = []
        def request(provider, inn, fields):
            calls.append(provider)
            return {'status': 'error', 'data': None, 'reason_code': reason, 'requests_used': 1}
        result = pp.fetch_with_policy('7707083893', ['name'], p, request=request, ledger={})
        return calls, result

    def test_quota_no_fallback_without_budget(self):
        calls, result = self.fetch(policy(budget=1))
        self.assertEqual(calls, ['checko'])
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['budget']['used_cost_units'], 1)
        self.assertIsNone(result['data'])

    def test_429_fallback_once(self):
        calls, result = self.fetch(policy(), 'http_429')
        self.assertEqual(calls, ['checko', 'dadata'])
        self.assertEqual(result['budget']['used_requests'], 2)

    def test_auth_never_retry_or_fallback(self):
        for reason in ('http_401', 'http_403'):
            calls, _ = self.fetch(policy(retry=1), reason)
            self.assertEqual(calls, ['checko'])

    def test_retry_bounded(self):
        calls, _ = self.fetch(policy(retry=1), 'unavailable')
        self.assertEqual(calls, ['checko', 'checko', 'dadata'])

    def test_timeout_conservative(self):
        p = policy(budget=1)
        result = pp.fetch_with_policy('7707083893', ['name'], p,
                request=lambda *a: (_ for _ in ()).throw(TimeoutError('SECRET_SENTINEL')), ledger={})
        self.assertEqual(result['budget']['used_cost_units'], 1)
        self.assertNotIn('SECRET_SENTINEL', json.dumps(result))

    def test_forbidden_fields_no_dispatch(self):
        calls = []
        result = pp.fetch_with_policy('7707083893', ['address'], policy(),
                                     request=lambda *a: calls.append(a), ledger={})
        self.assertEqual(calls, [])
        self.assertIsNone(result['data'])

    def test_schema_strict(self):
        for mutate in (lambda p: p.update(schema_version=True),
                       lambda p: p.update(total_cost_units=-1),
                       lambda p: p.update(secret='SECRET_SENTINEL'),
                       lambda p: p['providers'][0].update(id='evil'),
                       lambda p: p['providers'][0].update(allowed_fields=['secret']),
                       lambda p: p['providers'][0].update(transient_retries=2),
                       lambda p: p['providers'][0].update(request_limit=True),
                       lambda p: p['providers'][0].update(export_fields=['address'])):
            p = policy(); mutate(p)
            self.assertTrue(pp.validate_policy(p))
            self.assertNotIn('SECRET_SENTINEL', json.dumps(pp.validate_policy(p)))

    def test_request_limit_with_zero_cost(self):
        p = policy(budget=100)
        p['providers'][0].update(request_cost_units=0, request_limit=1, fallback_on=[])
        ledger = {}; calls = []
        def request(*args):
            calls.append(args)
            return {'status':'ok','data':{'name':'Company'},'requests_used':1}
        for _ in range(3):
            pp.fetch_with_policy('7707083893', ['name'], p, request=request, ledger=ledger)
        self.assertEqual(len(calls), 1)
        self.assertEqual(ledger['used_cost_units'], 0)

    def test_allowed_export_keeps_only_primitive_fields(self):
        p = policy(export=['name'])
        source = {'агрегатор':{'агрегатор':'checko','name':'Allowed','address':'FORBIDDEN'},
                  '_decision':{'text':'FORBIDDEN'}, 'copied':'FORBIDDEN'}
        result = pp.export_allowed(source, p)
        self.assertEqual(result['data'], {'name':'Allowed'})
        self.assertNotIn('FORBIDDEN', json.dumps(result))

    def test_concurrent_no_overspend(self):
        p = policy(budget=2); ledger = {}; calls = []
        def request(*a):
            calls.append(a); time.sleep(.01)
            return {'status': 'ok', 'data': {'name': 'Company'}, 'requests_used': 1}
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(lambda _: pp.fetch_with_policy('7707083893', ['name'], p,
                                                      request=request, ledger=ledger), range(24)))
        self.assertEqual(len(calls), 2)
        self.assertEqual(ledger['used_cost_units'], 2)
        self.assertEqual(ledger['reserved_requests'], 0)
        json.dumps(results)

    def test_projection_and_secret(self):
        result = pp.fetch_with_policy('7707083893', ['name'], policy(), request=lambda *a:
            {'status':'ok', 'data':{'name':'Company', 'raw':{'secret':'SECRET_SENTINEL'}},
             'requests_used':1, 'debug':'SECRET_SENTINEL'}, ledger={})
        self.assertEqual(result['data'], {'name':'Company'})
        self.assertNotIn('SECRET_SENTINEL', json.dumps(result))

    def test_export_derived_and_nested_closed(self):
        p = policy(); original = {'агрегатор': {'агрегатор':'checko', 'name':'SECRET_SENTINEL'},
            'nested':{'name':'SECRET_SENTINEL'}, '_decision':{'claim':'SECRET_SENTINEL'},
            '_provider_policy': {'schema_version':1, 'policy':p}}
        before = copy.deepcopy(original)
        exported = pp.export_allowed(original, p)
        self.assertNotIn('SECRET_SENTINEL', json.dumps(exported))
        self.assertEqual(original, before)
        self.assertTrue(exported['_export_policy']['omissions'])

    def test_raw_pack_and_dossier_closed(self):
        import dossier
        import evidence_pack
        p = policy()
        source = {'инн':'7707083893', 'агрегатор':{'агрегатор':'checko','name':'SECRET_SENTINEL'},
                  '_provider_policy':{'schema_version':1,'policy':p}}
        raw = [{'источник':'агрегатор','url':'https://api.checko.ru/v2/company', 'метод':'GET',
                'статус':200,'данные':b'{"name":"SECRET_SENTINEL"}'}]
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / 'pack'
            evidence_pack.записать_пакет(str(out), '7707083893', source, raw)
            for path in out.rglob('*'):
                if path.is_file(): self.assertNotIn(b'SECRET_SENTINEL', path.read_bytes())
            self.assertEqual(json.loads((out/'raw/index.json').read_text()), [])
            self.assertTrue(evidence_pack.проверить(str(out))['годен'])
        blocks = dossier.собрать(source, {'файл':'SECRET_SENTINEL'}, вердикт={'x':'SECRET_SENTINEL'})
        self.assertNotIn('SECRET_SENTINEL', json.dumps(blocks))

class IntegrationTests(unittest.TestCase):
    def test_legacy_no_fallback(self):
        import fetch_counterparty as fc
        with patch.dict('os.environ', {'CHECKO_API_KEY':'secret','DADATA_API_KEY':'secret','INN_CHECK_PROVIDER_POLICY':''}), \
             patch.object(fc, '_http_get', return_value=(429, '{}')) as get, \
             patch.object(fc, '_http_post') as post:
            fc.fetch_aggregator(object(), '7707083893')
        self.assertEqual(get.call_count, 1)
        post.assert_not_called()

    def test_real_parsers_and_capture_policy(self):
        import fetch_counterparty as fc
        p = policy(export=['name'])
        fixture = pathlib.Path(__file__).parent/'fixtures/aggregator/dadata_7705922751.json'
        raw = json.loads(fixture.read_text())
        raw["suggestions"][0]["data"]["name"] = {"short_with_opf":"Synthetic company"}
        text = json.dumps(raw)
        with tempfile.TemporaryDirectory() as tmp:
            cfg = pathlib.Path(tmp)/'policy.json'; cfg.write_text(json.dumps(p))
            with patch.dict('os.environ', {'INN_CHECK_PROVIDER_POLICY':str(cfg),
                    'CHECKO_API_KEY':'SECRET_SENTINEL', 'DADATA_API_KEY':'SECRET_SENTINEL'}), \
                 patch.object(fc, '_http_get', return_value=(429, '{}')) as get, \
                 patch.object(fc, '_http_post', return_value=text) as post:
                with fc.capture_session('7705922751') as capture:
                    data, av = fc.fetch_aggregator(object(), '7705922751')
                    marker = fc.provider_metadata(capture)
                self.assertEqual(data['агрегатор'], 'dadata')
                self.assertIn('name', data)
                self.assertEqual(av['состояние'], 'ok')
                self.assertEqual(marker['results'][0]['budget']['used_requests'], 2)
                self.assertNotIn('SECRET_SENTINEL', json.dumps(marker))
                self.assertEqual(get.call_count, 1); self.assertEqual(post.call_count, 1)

    def test_saved_render_does_not_load_current_policy_or_clock(self):
        import dossier
        source = {'_provider_policy':{'schema_version':1,'policy':policy()},
                  'агрегатор':{'агрегатор':'checko','name':'SECRET_SENTINEL'}}
        with patch.object(pp, 'configured_policy', side_effect=AssertionError('read current config')), \
             patch.object(dossier, '_сейчас', side_effect=AssertionError('clock')):
            self.assertEqual(dossier.собрать(source, {}, materialized=True),
                             dossier.собрать(source, {}, materialized=True))

    def test_graph_shared_budget_and_auth(self):
        import affiliates_graph as graph
        p = policy(budget=100)
        p['providers'][0].update(allowed_fields=['graph'], export_fields=[], request_limit=100,
                                  transient_retries=1)
        ledger = {}
        with patch.object(graph, '_fetch_company_transport', return_value=({'ИНН':'7707083893'}, 'ok')) as transport:
            for _ in range(30): graph.fetch_company('secret', inn='7707083893', policy=p, ledger=ledger)
        self.assertEqual(transport.call_count, 25)
        self.assertEqual(ledger['used_requests'], 25)
        with patch.object(graph, '_fetch_company_transport', return_value=(None, 'checko HTTP 401')) as transport:
            graph.fetch_company('secret', inn='7707083893', policy=p, ledger={})
        self.assertEqual(transport.call_count, 1)

    def test_disabled_fallback(self):
        p = policy(); p['providers'][1]['enabled'] = False
        calls, _ = PolicyTests().fetch(p)
        self.assertEqual(calls, ['checko'])

    def test_auth_copy_redaction(self):
        with patch.dict('os.environ', {'CHECKO_API_KEY':'SECRET_SENTINEL'}):
            result = pp.fetch_with_policy('7707083893', ['name'], policy(), request=lambda *a:
                {'status':'ok','data':{'name':'SECRET_SENTINEL'},'requests_used':1}, ledger={})
        self.assertNotIn('SECRET_SENTINEL', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
