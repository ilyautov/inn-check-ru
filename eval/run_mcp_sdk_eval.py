#!/usr/bin/env python3
"""
run_mcp_sdk_eval.py — ошибки MCP-сервера, когда FastMCP не импортируется: у
mcp 2.x (FastMCP переименован в MCPServer) и у сломанной установки 1.x сервер
называет версию и даёт свой совет, а не «не найден пакет». SDK не нужен:
подставной пакет mcp нужной версии кладётся первым в PYTHONPATH. Каждый
запуск — отдельный процесс (сервер при ошибке делает sys.exit). PASS/FAIL, CI.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "mcp" / "server.py"


def запуск(версия):
    """server.py при подставном mcp <версия> без FastMCP -> (код, stderr)."""
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "mcp").mkdir()
        (Path(tmp) / "mcp" / "__init__.py").write_text("", encoding="utf-8")
        dist = Path(tmp) / ("mcp-%s.dist-info" % версия)
        dist.mkdir()
        (dist / "METADATA").write_text("Metadata-Version: 2.1\nName: mcp\nVersion: %s\n" % версия,
                                       encoding="utf-8")
        env = dict(os.environ, PYTHONPATH=tmp)
        p = subprocess.run([sys.executable, str(SERVER)], cwd=tmp, env=env, capture_output=True,
                           text=True, timeout=60, check=False, stdin=subprocess.DEVNULL)
        return p.returncode, p.stderr


def main():
    cases = {}
    for версия, ждём, нельзя in (
            ("2.2.0", "mcp>=1.30,<2", "Не найден пакет"),
            ("1.30.0", "--force-reinstall", "Не найден пакет"),
            ("10.0.0", "mcp>=1.30,<2", "--force-reinstall")):
        код, err = запуск(версия)
        ошибки = []
        if код != 1:
            ошибки.append("код %r, ждали 1" % код)
        if версия not in err or ждём not in err or нельзя in err:
            ошибки.append("stderr: %r" % err[-300:])
        cases["mcp %s без FastMCP — версия и совет" % версия] = ошибки
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
