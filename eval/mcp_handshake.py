#!/usr/bin/env python3
"""
mcp_handshake.py — поднимается ли MCP-сервер ТОЙ командой, которую запустит клиент.

    python3 eval/mcp_handshake.py <команда> [аргументы...]
    python3 eval/mcp_handshake.py uvx inn-check-ru-mcp

Шлёт по stdio `initialize`, `notifications/initialized` и `tools/list`, ждёт
ответы и сверяет имя сервера и число инструментов. Код выхода 0 — сервер
ответил как надо, 1 — нет, с причиной в stderr.

Зачем отдельно от mcp/test_server.py. Тот проверяет сервер, запущенный из
репозитория. Этот проверяет КОМАНДУ ЗАПУСКА: до 1.11.1 сервер был исправен,
а команда из server.json запускала CLI проверки ИНН. Клиент видел процесс,
который печатает справку и выходит, и ни одной ошибки. Поймать такое можно
только запуском той самой команды.
"""

import json
import os
import queue
import subprocess
import sys
import threading
import time

ИМЯ_СЕРВЕРА = "inn-check-ru"
# Additive contract imported without SDK or repository-package shadowing.
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location('handshake_tools', Path(__file__).resolve().parents[1] / 'mcp/tools_impl.py')
_tools = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_tools)
EXPECTED_TOOLS = _tools.EXPECTED_TOOLS
ИНСТРУМЕНТОВ = len(EXPECTED_TOOLS)
ТАЙМАУТ = float(os.environ.get("MCP_HANDSHAKE_TIMEOUT", "120"))


def _читатель(поток, очередь):
    for строка in iter(поток.readline, b""):
        очередь.put(строка)
    очередь.put(None)


def _ждать(очередь, ид, до):
    """Ответ JSON-RPC с данным id; строки-уведомления и мусор пропускаются."""
    while True:
        осталось = до - time.monotonic()
        if осталось <= 0:
            return None, "таймаут %.0f с без ответа на id=%s" % (ТАЙМАУТ, ид)
        try:
            строка = очередь.get(timeout=осталось)
        except queue.Empty:
            continue
        if строка is None:
            return None, "процесс закрыл stdout, не ответив на id=%s" % ид
        try:
            сообщение = json.loads(строка)
        except ValueError:
            # Не JSON в stdout у stdio-сервера — уже признак беды (справка CLI,
            # отладочный print). Показываем, но ждём дальше: вдруг это баннер.
            sys.stderr.write("  не JSON в stdout: %r\n" % строка[:160])
            continue
        if сообщение.get("id") == ид:
            return сообщение, None


class StdioClient:
    """Small real JSON-lines protocol client shared by workflow integration evals."""
    def __init__(self, command, *, env=None, cwd=None):
        self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, env=env, cwd=cwd)
        self.queue = queue.Queue()
        self.pending = {}
        self.errors = []
        self.reader = threading.Thread(target=_читатель, args=(self.proc.stdout, self.queue), daemon=True)
        self.reader.start()
        def stderr_reader():
            for line in iter(self.proc.stderr.readline, b''):
                self.errors.append(line.decode('utf-8', 'replace'))
        self.stderr_reader = threading.Thread(target=stderr_reader, daemon=True)
        self.stderr_reader.start()

    def send(self, message):
        self.proc.stdin.write((json.dumps(message, ensure_ascii=False) + '\n').encode('utf-8'))
        self.proc.stdin.flush()

    def receive(self, request_id, timeout=30):
        if request_id in self.pending:
            return self.pending.pop(request_id)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                line = self.queue.get(timeout=max(0.001, deadline - time.monotonic()))
            except queue.Empty:
                break
            if line is None:
                raise AssertionError('MCP closed stdout: ' + ''.join(self.errors)[-1000:])
            message = json.loads(line)
            if message.get('id') == request_id:
                return message
            if 'id' in message:
                self.pending[message['id']] = message
        raise AssertionError('MCP response timed out: %s' % request_id)

    def call(self, request, timeout=30):
        self.send(request)
        return self.receive(request['id'], timeout)

    def initialize(self):
        response = self.call({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
            'protocolVersion': '2025-06-18', 'capabilities': {},
            'clientInfo': {'name': 'workflow-mcp-eval', 'version': '1'}}})
        if response.get('result', {}).get('serverInfo', {}).get('name') != ИМЯ_СЕРВЕРА:
            raise AssertionError(response)
        self.send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        return response

    def close(self):
        if self.proc.stdin:
            self.proc.stdin.close()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        self.reader.join(timeout=2)
        self.stderr_reader.join(timeout=2)
        self.proc.stdout.close()
        self.proc.stderr.close()


def decode_tool_json(response):
    result = response.get('result', {})
    if 'structuredContent' in result:
        return result['structuredContent']
    return json.loads(''.join(block.get('text', '') for block in result.get('content', [])
                              if block.get('type') == 'text'))


def main(argv):
    if len(argv) < 2:
        sys.stderr.write(__doc__)
        return 2
    команда = argv[1:]
    try:
        proc = subprocess.Popen(команда, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
    except OSError as e:
        sys.stderr.write("FAIL команда не запускается: %s\n" % e)
        return 1
    очередь = queue.Queue()
    threading.Thread(target=_читатель, args=(proc.stdout, очередь), daemon=True).start()
    до = time.monotonic() + ТАЙМАУТ

    def послать(сообщение):
        proc.stdin.write((json.dumps(сообщение) + "\n").encode("utf-8"))
        proc.stdin.flush()

    ошибка = None
    try:
        послать({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "mcp_handshake", "version": "0"}}})
        ответ, ошибка = _ждать(очередь, 1, до)
        if ответ is not None:
            имя = ((ответ.get("result") or {}).get("serverInfo") or {}).get("name")
            if имя != ИМЯ_СЕРВЕРА:
                ошибка = "serverInfo.name %r, ожидали %r: ответ %s" % (
                    имя, ИМЯ_СЕРВЕРА, json.dumps(ответ, ensure_ascii=False)[:300])
            else:
                версия = ответ["result"]["serverInfo"].get("version")
                print("initialize ok: %s %s" % (имя, версия))
                послать({"jsonrpc": "2.0", "method": "notifications/initialized"})
                послать({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
                ответ, ошибка = _ждать(очередь, 2, до)
                if ответ is not None:
                    инструменты = (ответ.get("result") or {}).get("tools") or []
                    if sorted(t.get("name") for t in инструменты) != sorted(EXPECTED_TOOLS):
                        ошибка = "инструментов %d, ожидали %d: %s" % (
                            len(инструменты), ИНСТРУМЕНТОВ,
                            ", ".join(sorted(t.get("name", "?") for t in инструменты)))
                    else:
                        print("tools/list ok: %d инструментов" % len(инструменты))
    except (BrokenPipeError, OSError) as e:
        ошибка = "процесс закрыл stdin: %s" % e
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    if ошибка:
        хвост = proc.stderr.read().decode("utf-8", "replace")[-800:] if proc.stderr else ""
        sys.stderr.write("FAIL %s\n  команда: %s\n  stderr процесса: %s\n"
                         % (ошибка, " ".join(команда), хвост.strip() or "(пусто)"))
        return 1
    print("PASS %s" % " ".join(команда))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
