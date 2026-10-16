"""Isolated legacy updater runner with streaming transport budget.

Only invoked by cache_policy in a child with its own HOME. No updater or SDK is
imported before the child cache root and bounded urllib transport are installed.
"""
from __future__ import annotations

import contextlib
import http.client
import io
import json
import re
import runpy
import shutil
import sys
import urllib.request
from pathlib import Path


class BudgetExceeded(BaseException):
    """Cannot be swallowed by legacy updater network Exception handlers."""


class DownloadBudget:
    def __init__(self, maximum, ledger):
        self.maximum = maximum
        self.ledger = Path(ledger)
        self.downloaded = 0
        self.record()

    def record(self):
        # Parent polls only after child termination; replace avoids partial JSON.
        temporary = self.ledger.with_suffix('.tmp')
        temporary.write_text(json.dumps({'bytes_downloaded': self.downloaded}), encoding='utf-8')
        temporary.replace(self.ledger)

    def account(self, count):
        self.downloaded += count
        self.record()
        if self.downloaded > self.maximum:
            raise BudgetExceeded('download byte budget exceeded')


class BoundedResponse(io.BufferedIOBase):
    """Bound payload reads without changing full-read or EOF semantics.

    Unknown-length EOF needs a read returning zero; if quota is exhausted first,
    fail conservatively without reading an extra sentinel byte.
    """
    def __init__(self, response, budget):
        self.response = response
        self.budget = budget
        self.received = 0
        self.eof = False
        headers = getattr(response, 'headers', {})
        length = headers.get('Content-Length')
        chunked = getattr(response, 'chunked', False) or 'chunked' in headers.get('Transfer-Encoding', '').lower()
        if isinstance(response, http.client.HTTPResponse):
            # Native parsing applies HEAD/1xx/204/304 no-body semantics before
            # the redirect handler drains the response. Raw Content-Length may
            # describe the GET representation rather than bytes on this stream.
            self.expected = response.length
        else:
            self.expected = int(length) if not chunked and isinstance(length, str) and length.isdigit() else None

    def __getattr__(self, name):
        return getattr(self.response, name)

    def readable(self):
        return True

    def seekable(self):
        return False

    def _pull(self, size, *, once=False):
        if self.closed:
            raise ValueError('read of closed response')
        if size == 0 or self.eof or self.expected is not None and self.received == self.expected:
            return b''
        remaining = self.budget.maximum - self.budget.downloaded
        if remaining <= 0:
            raise BudgetExceeded('download byte budget exhausted before verified EOF')
        limit = min(size, remaining)
        if self.expected is not None:
            limit = min(limit, self.expected - self.received)
        # Native read(size) drops the last partial chunk from IncompleteRead.
        # read1 exposes every payload byte before EOF, so the ledger remains exact.
        reader = (getattr(self.response, 'read1', self.response.read)
                  if once or isinstance(self.response, http.client.HTTPResponse) else self.response.read)
        try:
            data = reader(limit)
        except http.client.IncompleteRead as error:
            # HTTPResponse may have consumed a partial chunk before detecting EOF.
            self.received += len(error.partial)
            self.budget.account(len(error.partial))
            raise BudgetExceeded('incomplete HTTP body') from None
        self.received += len(data)
        self.budget.account(len(data))
        if not data:
            self.eof = True
            if self.expected is not None and self.received != self.expected:
                raise BudgetExceeded('incomplete Content-Length body')
        return data

    def read(self, size=-1):
        unlimited = size is None or size < 0
        pieces = []
        count = 0
        while unlimited or count < size:
            chunk = self._pull(min(65536, size - count) if not unlimited else 65536)
            if not chunk:
                break
            pieces.append(chunk)
            count += len(chunk)
        return b''.join(pieces)

    def read1(self, size=-1):
        return self._pull(65536 if size is None or size < 0 else size, once=True)

    def readinto(self, target):
        data = self.read(len(target))
        target[:len(data)] = data
        return len(data)

    def readinto1(self, target):
        data = self.read1(len(target))
        target[:len(data)] = data
        return len(data)

    def readline(self, size=-1):
        out = bytearray()
        while size is None or size < 0 or len(out) < size:
            char = self.read(1)
            out.extend(char)
            if not char or char == b'\n':
                break
        return bytes(out)

    def close(self):
        self.response.close()
        super().close()


@contextlib.contextmanager
def bounded_transport(budget):
    """Wrap transport results BEFORE redirect/error handlers consume bodies.

    _open is the stdlib transport dispatch boundary. open additionally handles
    injected transports and nonstandard openers, without double accounting.
    """
    original_dispatch = urllib.request.OpenerDirector._open
    original_open = urllib.request.OpenerDirector.open

    def wrap(response):
        return response if isinstance(response, BoundedResponse) else BoundedResponse(response, budget)

    def dispatch(self, *args, **kwargs):
        return wrap(original_dispatch(self, *args, **kwargs))

    def open_response(self, *args, **kwargs):
        if budget.downloaded >= budget.maximum:
            raise BudgetExceeded('download byte budget exhausted')
        return wrap(original_open(self, *args, **kwargs))

    urllib.request.OpenerDirector._open = dispatch
    urllib.request.OpenerDirector.open = open_response
    try:
        yield
    finally:
        urllib.request.OpenerDirector._open = original_dispatch
        urllib.request.OpenerDirector.open = original_open


def result_success(text):
    """Legacy CLI exit 0 can still mean partially updated sanction lists."""
    try:
        result = json.loads(text)
    except ValueError:
        return False
    if not isinstance(result, dict):
        return False
    if 'записано' in result:
        return result['записано'] is True
    state = result.get('статус', '')
    if not isinstance(state, str) or not state.startswith(('обновлено', 'уже свежий')):
        return False
    match = re.search(r'(\d+)\s*/\s*(\d+)', state)
    return match is None or int(match[1]) == int(match[2])


def _seed_previous(item, previous, destination):
    """Copy only the selected dataset; preserves existing drop/count safeguards."""
    if not previous or not Path(previous).is_dir():
        return
    previous = Path(previous)
    if item['dataset_id'] == 'sanctions':
        destination.mkdir(parents=True, exist_ok=True)
        for filename in ('terrorists-list.html', 'sdn.csv', 'eu-sanctions.xml'):
            for suffix in ('', '.meta.json'):
                source = previous / (filename + suffix)
                if source.is_symlink():
                    raise ValueError('symlink in previous dataset')
                if source.is_file():
                    shutil.copy2(source, destination / source.name)
    else:
        if previous.is_symlink() or any(path.is_symlink() for path in previous.rglob('*')):
            raise ValueError('symlink in previous dataset')
        shutil.copytree(previous, destination, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns('*.lock', '*.tmp', '*.part'))


class _Output(io.StringIO):
    def write(self, value):
        if self.tell() + len(value) > 1 << 20:
            raise BudgetExceeded('updater output exceeds diagnostic limit')
        return super().write(value)


def _build_opendata_indexes():
    import opendata_index
    for dataset_id in opendata_index.ИНДЕКСИРУЕМЫЕ:
        if dataset_id not in opendata_index.od.ОБЯЗАТЕЛЬНЫЕ:
            continue
        _, reason = opendata_index.построить(dataset_id)
        if reason is not None:
            raise ValueError('open data index build failed')


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--updater', required=True)
    parser.add_argument('--max-bytes', type=int, required=True)
    parser.add_argument('--ledger', required=True)
    parser.add_argument('--previous', default='')
    args = parser.parse_args(argv)
    from cache_policy import _validate, cache_root, inventory
    item = next((x for x in inventory() if x['updater'] == args.updater), None)
    if item is None:
        parser.error('unknown updater')
    budget = DownloadBudget(args.max_bytes, args.ledger)
    destination = cache_root() / item['cache_subdir']
    _seed_previous(item, args.previous, destination)
    sys.argv = [args.updater, '--refresh']
    try:
        output = _Output()
        with bounded_transport(budget), contextlib.redirect_stdout(output):
            try:
                runpy.run_module(args.updater, run_name='__main__')
            except SystemExit as result:
                if result.code not in (0, None):
                    return result.code if isinstance(result.code, int) else 1
        if not result_success(output.getvalue()):
            return 1
        if item['dataset_id'] == 'opendata_refresh':
            _build_opendata_indexes()
        _validate(item, destination)
    except BudgetExceeded:
        return 75
    return 0


if __name__ == '__main__':
    sys.exit(main())
