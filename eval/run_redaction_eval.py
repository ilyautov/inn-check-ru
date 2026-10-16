#!/usr/bin/env python3
"""Canonical export redaction: pure, recursive and conservative about data."""
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
try:
    from redaction import redact
except ImportError:
    redact = None


class RedactionEval(unittest.TestCase):
    def test_ordinary_fields_unchanged_and_deep_copied(self):
        self.assertIsNotNone(redact, 'canonical redact helper must exist')
        value = {'name': 'Ромашка', 'rows': [{'a': 0, 'b': False, 'c': None}],
                 'url': 'https://example.test/a?q=company', 'key': 'ordinary business key'}
        result = redact(value)
        self.assertEqual(result, value)
        self.assertIsNot(result['rows'], value['rows'])

    def test_known_secret_copies_query_userinfo_headers_and_explicit_secrets(self):
        self.assertIsNotNone(redact, 'canonical redact helper must exist')
        sentinel = 'SENTINEL_PRIVATE_123'
        value = {'nested': [{'_token_pb': sentinel}], 'authorization': 'Bearer ' + sentinel,
                 'apiKey': sentinel, 'HTTP_PROXY': 'http://alice:' + sentinel + '@proxy.test:8080/p',
                 'url': 'https://example.test/api?token=' + sentinel + '&inn=7707083893',
                 'diagnostic': 'request failed with ' + sentinel,
                 'socks': 'socks5h://socksuser:' + sentinel + '@proxy.test:1080',
                 'other': 'text ExplicitSecret42 suffix', 'environment': {'x': sentinel}}
        original = copy.deepcopy(value)
        result = redact(value, secrets=('ExplicitSecret42',))
        self.assertEqual(value, original)
        self.assertNotIn(sentinel, repr(result))
        self.assertNotIn('ExplicitSecret42', repr(result))
        self.assertNotIn('alice', repr(result))
        self.assertNotIn('socksuser', repr(result))
        self.assertNotIn('environment', result)
        self.assertIn('proxy.test:8080/p', result['HTTP_PROXY'])
        self.assertIn('inn=7707083893', result['url'])
        self.assertEqual(redact(result), result, 'redaction must be idempotent for persisted data')

    def test_normalized_query_credentials_and_token_header_without_secret_fields(self):
        for text in [
            'https://example.test/?api_token=PRIVATEQUERY',
            'https://example.test/?AUTH=PRIVATEQUERY',
            'https://example.test/?client-secret=PRIVATEQUERY',
            'Authorization: Token PRIVATEQUERY',
            'Proxy-Authorization: Custom PRIVATEQUERY',
        ]:
            with self.subTest(text=text):
                value = {'diagnostic': text, 'copy': 'failed with PRIVATEQUERY'}
                result = redact(value)
                self.assertNotIn('PRIVATEQUERY', repr(result))
                self.assertEqual(redact(result), result)

    def test_environment_and_short_url_usernames_do_not_corrupt_ordinary_values(self):
        value = {'fetch': {'инн': '7707083893'}, 'metadata': {
            'environment': {'DEBUG': '7'},
            'proxy': 'http://a:password@proxy.example:8080',  # trufflehog:ignore -- reserved-domain synthetic redaction fixture
            'hash': 'b' * 64,
            'nested': [{'url': 'socks5h://b:password@proxy.test'}],
        }}
        result = redact(value)
        self.assertEqual(result['fetch'], value['fetch'])
        self.assertEqual(result['metadata']['hash'], 'b' * 64)
        self.assertEqual(result['metadata']['proxy'], 'http://proxy.example:8080')
        self.assertEqual(result['metadata']['nested'][0]['url'], 'socks5h://proxy.test')
        self.assertEqual(redact(result), result)

    def test_markers_and_short_credentials_are_idempotent(self):
        for secret in ['D', 'REDACTED', 'RE', '%5B']:
            with self.subTest(secret=secret):
                value = {'password': secret,
                         'diagnostic': '[REDACTED] Bearer ' + secret,
                         'url': 'https://example.test/?api_token=' + secret}
                result = redact(value)
                self.assertEqual(redact(result), result)
                self.assertNotIn('[RE[REDACTED]', repr(result))
                self.assertIn('[REDACTED]', result['diagnostic'])

    def test_configured_environment_secrets_scrub_copies_only_for_credential_names(self):
        value = {'environment': {'DEBUG': '7', 'SERVICE_API_TOKEN': 'CONFIGURED_PRIVATE'},
                 'diagnostic': 'CONFIGURED_PRIVATE', 'инн': '7707083893'}
        self.assertEqual(redact(value), {'diagnostic': '[REDACTED]', 'инн': '7707083893'})

    def test_explicit_secret_containing_marker_is_fully_scrubbed(self):
        secret = '[REDACTED]private-suffix'
        value = {'diagnostic': 'copied ' + secret}
        result = redact(value, secrets=(secret,))
        self.assertEqual(result, {'diagnostic': 'copied [REDACTED]'})
        self.assertEqual(redact(result, secrets=(secret,)), result)

    def test_structured_authorization_scheme_scrubs_copied_token(self):
        for name in ['Authorization', 'proxy-authorization', 'auth']:
            with self.subTest(name=name):
                result = redact({name: 'Token PRIVATECOPY', 'diagnostic': 'PRIVATECOPY'})
                self.assertEqual(result, {'diagnostic': '[REDACTED]'})

    def test_free_text_embedded_credentials(self):
        self.assertIsNotNone(redact, 'canonical redact helper must exist')
        text = 'error https://bob:PRIVATE@proxy.test/path?api_key=QUERYVALUE Authorization: Bearer HEADERSECRET'  # trufflehog:ignore -- reserved-domain synthetic redaction fixture
        result = redact({'diagnostic': text})['diagnostic']
        for secret in ['bob', 'PRIVATE', 'QUERYVALUE', 'HEADERSECRET']:
            self.assertNotIn(secret, result)


if __name__ == '__main__':
    unittest.main(verbosity=2)
