"""Pure canonical redaction of JSON-like data, without environment or I/O access.

Credential fields, auth text and URL passwords discover copied secrets. URL
usernames are removed in context; ordinary environment values are not secrets.
Unmarked arbitrary credentials require the caller's explicit ``secrets``.
"""
from __future__ import annotations

import copy
import re
import urllib.parse

_REDACTED = '[REDACTED]'
_ENCODED_MARKER = urllib.parse.quote(_REDACTED, safe='')
_URL = re.compile(r'\b[a-z][a-z0-9+.-]*://[^\s<>"\']+', re.IGNORECASE)
_AUTH = re.compile(r'\b(?:Bearer|Basic)\s+([^\s,;"\'<>]+)', re.IGNORECASE)
_HEADER = re.compile(r'\b(?:Proxy-)?Authorization\s*:\s*([^\r\n]+)', re.IGNORECASE)
_ENV = {'env', 'environ', 'environment', 'processenvironment', 'osenviron'}
_CREDENTIAL = {'authorization', 'proxyauthorization', 'apikey', 'xapikey', 'token',
               'accesstoken', 'refreshtoken', 'idtoken', 'authtoken', 'auth',
               'password', 'passwd', 'pwd', 'secret', 'clientsecret', 'credential',
               'credentials', 'cookie', 'setcookie', 'proxycredentials', 'privatekey'}
_QUERY_EXTRA = {'key', 'signature', 'sig', 'accesskey'}


def _normalized(key):
    return re.sub(r'[^a-z0-9]', '', str(key).lower())


def _credential(key):
    normalized = _normalized(key)
    return (str(key).lower().startswith('_token') or normalized in _CREDENTIAL or
            normalized.endswith(('apikey', 'token', 'secret', 'password')))


def _query_credential(key):
    return _credential(key) or _normalized(key) in _QUERY_EXTRA


def redact(value, *, secrets=()):
    """Return an independent canonical copy; raise ValueError on secret keys.

    Keys retain their original meaning. A known secret colliding with an ordinary
    key cannot safely be persisted, so callers must reject that input rather than
    rename schema fields. Existing and emitted markers are atomic placeholders.
    """
    found = {s for s in secrets if isinstance(s, str) and s}

    def add(node):
        if isinstance(node, str) and node:
            found.add(node)
            for match in _AUTH.finditer(node):
                found.add(match.group(1))
        elif isinstance(node, dict):
            for child in node.values():
                add(child)
        elif isinstance(node, (list, tuple)):
            for child in node:
                add(child)

    def url_parts(url, *, discover=False):
        try:
            parts = urllib.parse.urlsplit(url)
            query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
            if discover:
                if parts.password:
                    found.update((parts.password, urllib.parse.unquote(parts.password)))
                for key, item in query:
                    if _query_credential(key) and item:
                        found.update((item, urllib.parse.quote(item, safe='')))
            return parts, query
        except ValueError:
            return None, None

    def authorization(header):
        found.add(header)
        # Skip the scheme, but discover copied token / quoted parameter values.
        pieces = re.findall(r'[^\s,;="\'<>]+', header)
        found.update(pieces[1:])

    def discover(node):
        if isinstance(node, dict):
            for key, child in node.items():
                if _credential(key):
                    add(child)
                    if _normalized(key) in {'authorization', 'proxyauthorization', 'auth'} and isinstance(child, str):
                        authorization(child)
                discover(child)
        elif isinstance(node, (list, tuple)):
            for child in node:
                discover(child)
        elif isinstance(node, str):
            for match in _AUTH.finditer(node):
                found.add(match.group(1))
            for match in _HEADER.finditer(node):
                authorization(match.group(1))
            for match in _URL.finditer(node):
                url_parts(match.group(), discover=True)

    discover(value)
    found.difference_update((_REDACTED, _ENCODED_MARKER))
    ordered = sorted(found, key=len, reverse=True)
    # Single-pass substitution never re-enters replacements or preexisting markers.
    alternatives = sorted([_REDACTED, _ENCODED_MARKER, *ordered], key=len, reverse=True)
    replacements = re.compile('|'.join(map(re.escape, alternatives)))

    def replace_secrets(item):
        return replacements.sub(lambda m: m.group() if m.group() in (
            _REDACTED, _ENCODED_MARKER) else _REDACTED, item)

    def clean_url(match):
        url = match.group()
        parts, query = url_parts(url)
        if parts is None:
            return _REDACTED if '@' in url else url
        if '@' not in parts.netloc and not any(_query_credential(key) for key, _ in query):
            return url
        netloc = parts.netloc.rsplit('@', 1)[-1]
        safe_query = [(key, _REDACTED if _query_credential(key) else item) for key, item in query]
        return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path,
                                       urllib.parse.urlencode(safe_query), parts.fragment))

    def text(item):
        item = _URL.sub(clean_url, item)
        item = _HEADER.sub(lambda m: m.group().split(':', 1)[0] + ': ' + _REDACTED, item)
        item = _AUTH.sub(lambda m: m.group().split()[0] + ' ' + _REDACTED, item)
        return replace_secrets(item)

    def clean(node):
        if isinstance(node, dict):
            result = {}
            for key, child in node.items():
                if _credential(key) or _normalized(key) in _ENV:
                    continue
                if isinstance(key, str) and text(key) != key:
                    raise ValueError('credential collides with a data key')
                result[key] = clean(child)
            return result
        if isinstance(node, list):
            return [clean(child) for child in node]
        if isinstance(node, tuple):
            return tuple(clean(child) for child in node)
        return text(node) if isinstance(node, str) else copy.deepcopy(node)

    return clean(value)
