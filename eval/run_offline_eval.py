#!/usr/bin/env python3
"""
run_offline_eval.py — офлайн-режим движка (`--офлайн` / INN_CHECK_OFFLINE=1):
сетевые источники — «не проверено» с причиной «режим:», источники-кэши
собираются, и ни одного сетевого вызова из процесса. Сетевые вызовы ловит
audit-хук (sys.addaudithook — его нельзя снять изнутри процесса), поэтому тест
не зависит от того, как движок сам запрещает сеть. Каждый прогон — отдельный
процесс: запрет сети необратим. PASS/FAIL, чистый stdlib, гоняется в CI.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ИНН = "7707083893"

# Дочерний процесс: audit-хук пишет каждое сетевое событие, затем main движка.
# «поломка» заставляет источник-кэш полезть в сеть — запрет обязан его остановить.
РЕБЁНОК = r"""
import json, sys
события = []
def хук(имя, арг):
    if имя in ("socket.connect", "socket.getaddrinfo", "socket.gethostbyname"):
        события.append(имя)
sys.addaudithook(хук)
sys.path.insert(0, sys.argv[1])
import fetch_counterparty as fc
if sys.argv[2] == "поломка":
    import socket
    def лезет_в_сеть(*_a, **_k):
        try:
            socket.gethostbyname("www.cbr.ru")
        except OSError:
            pass
        socket.create_connection(("www.cbr.ru", 443), timeout=5)
    fc.FETCHERS["список_цб"] = лезет_в_сеть
код = fc.main(["fetch_counterparty.py"] + json.loads(sys.argv[3]))
sys.stderr.write("\nСОБЫТИЯ=" + json.dumps(события) + "\n")
sys.exit(код)
"""


def прогон(аргументы, env_офлайн=False, поломка=False, tsa=False):
    env = dict(os.environ)
    env.pop("INN_CHECK_OFFLINE", None)
    env.pop("INN_CHECK_TSA", None)
    if env_офлайн:
        env["INN_CHECK_OFFLINE"] = "1"
    if tsa:
        env["INN_CHECK_TSA"] = "да"
    p = subprocess.run(
        [sys.executable, "-c", РЕБЁНОК, str(ROOT / "scripts"),
         "поломка" if поломка else "-", json.dumps(аргументы)],
        capture_output=True, text=True, env=env, timeout=300, check=False)
    строки = [s for s in p.stderr.splitlines() if s.startswith("СОБЫТИЯ=")]
    события = json.loads(строки[-1].split("=", 1)[1]) if строки else None
    try:
        вывод = json.loads(p.stdout)
    except ValueError:
        вывод = None
    return p.returncode, вывод, события, p.stderr


def case_офлайн(аргументы, env_офлайн=False):
    sys.path.insert(0, str(ROOT / "scripts"))
    import sources
    errors = []
    код, рез, события, err = прогон(аргументы, env_офлайн=env_офлайн)
    if код != 0 or not isinstance(рез, dict):
        return ["движок упал (%s): %s" % (код, err[-500:])]
    if события != []:
        errors.append("сетевые вызовы в офлайне: %r" % события)
    if рез.get("_сбор", {}).get("офлайн") is not True:
        errors.append("_сбор.офлайн не True: %r" % рез.get("_сбор"))
    av = рез.get("_доступность") or {}
    for sid, d in sources.SOURCES.items():
        a = av.get(sid) or {}
        причина = str(a.get("причина") or "")
        if d.get("требует") == "сеть":
            if a.get("состояние") != "не проверено" or not причина.startswith("режим: офлайн"):
                errors.append("сетевой %s в офлайне: %r" % (sid, a))
        elif d.get("требует") == "кэш" and причина.startswith(("режим:", "сеть:")):
            errors.append("кэш %s не собран офлайн: %r" % (sid, a))
    return errors


def case_поломка():
    """Источник-кэш, полезший в сеть, упирается в запрет: «не проверено», 0 вызовов."""
    errors = []
    код, рез, события, err = прогон([ИНН, "--офлайн", "--режим", "всё"], поломка=True)
    if код != 0 or not isinstance(рез, dict):
        return ["движок упал (%s): %s" % (код, err[-500:])]
    if события != []:
        errors.append("запрет не остановил соединение: %r" % события)
    a = (рез.get("_доступность") or {}).get("список_цб") or {}
    if a.get("состояние") != "не проверено" or not str(a.get("причина")).startswith(
            "режим: офлайн"):
        errors.append("полезший в сеть источник: %r" % a)
    return errors


def case_несовместимое():
    errors = []
    for имя, арг, tsa in (("канарейки", ["--канарейки", "--офлайн"], False),
                          ("штамп времени", [ИНН, "--офлайн"], True)):
        код, рез, события, _ = прогон(арг, tsa=tsa)
        if код != 2 or "офлайн" not in str((рез or {}).get("ошибка")):
            errors.append("%s в офлайне не отклонены: %s %r" % (имя, код, рез))
        if события:
            errors.append("%s: сетевые вызовы %r" % (имя, события))
    return errors


def main():
    cases = {
        "--офлайн: сеть не запрошена, кэши собраны": case_офлайн(
            [ИНН, "--офлайн", "--режим", "всё"]),
        "INN_CHECK_OFFLINE=1 — то же без флага": case_офлайн(
            [ИНН, "--режим", "всё"], env_офлайн=True),
        "источник-кэш, полезший в сеть, упирается в запрет": case_поломка(),
        "канарейки и штамп времени в офлайне — отказ": case_несовместимое(),
    }
    failed = 0
    for name, errors in cases.items():
        if errors:
            failed += 1
            print("FAIL %s" % name)
            for e in errors:
                print("  - %s" % e)
        else:
            print("PASS %s" % name)
    if failed:
        print("FAIL: %d/%d кейсов упало" % (failed, len(cases)))
        return 1
    print("PASS: все %d кейсов зелёные" % len(cases))
    return 0


if __name__ == "__main__":
    sys.exit(main())
