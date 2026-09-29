#!/usr/bin/env python3
"""
run_mcp_sdk_eval.py — ошибки MCP-сервера, когда FastMCP не импортируется: у
mcp 2.x (FastMCP переименован в MCPServer) и у сломанной установки 1.x сервер
называет версию и даёт свой совет, а не «не найден пакет». SDK не нужен:
подставной пакет mcp нужной версии кладётся первым в PYTHONPATH. Каждый
запуск — отдельный процесс (сервер при ошибке делает sys.exit). PASS/FAIL, CI.
"""

import importlib.util
import json
import os
import re
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


def _версия(s):
    return tuple(int(ч) for ч in s.split("."))


def пины():
    """Каталог плагинов Anthropic блокирует лаунчер без точной версии и `uv run` без
    --frozen/--locked. Каждый --with в .mcp.json — `пакет==X.Y.Z`, пакеты те же,
    что в mcp/requirements.txt, и не ниже его нижних границ (там — почему)."""
    ошибки = []
    args = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))[
        "mcpServers"]["inn-check-ru"]["args"]
    if args[:2] != ["run", "--frozen"] or args[-1] != "${CLAUDE_PLUGIN_ROOT}/mcp/server.py":
        ошибки.append("запуск: %r" % args)
    пины = {}
    for i, a in enumerate(args):
        if a == "--with":
            m = re.fullmatch(r"([a-z0-9-]+)==(\d+(?:\.\d+)*)", args[i + 1])
            if not m:
                ошибки.append("не точная версия: %r" % args[i + 1])
                continue
            пины[m.group(1)] = m.group(2)
    границы = {}
    for строка in (ROOT / "mcp" / "requirements.txt").read_text(encoding="utf-8").splitlines():
        m = re.match(r"([a-z0-9-]+)>=([\d.]+)(?:,<(\d+))?\s*$", строка.strip())
        if m:
            границы[m.group(1)] = (m.group(2), m.group(3))
    if set(пины) != set(границы):
        ошибки.append("пакеты: %r против %r" % (sorted(пины), sorted(границы)))
    for пакет, (низ, верх) in границы.items():
        if пакет in пины and (_версия(пины[пакет]) < _версия(низ) or (
                верх and _версия(пины[пакет])[0] >= int(верх))):
            ошибки.append("%s==%s вне границ >=%s,<%s" % (пакет, пины[пакет], низ, верх))
    return ошибки


def ключи():
    """Ключи из настроек плагина: .mcp.json передаёт userConfig в INN_CHECK_PLUGIN_*,
    сервер переносит непустые в CHECKO_API_KEY / DADATA_API_KEY и не затирает ключ из
    окружения пустым полем или неподставленным шаблоном."""
    ошибки = []
    spec = importlib.util.spec_from_file_location("tools_impl", ROOT / "mcp" / "tools_impl.py")
    ti = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ti)
    env = {"INN_CHECK_PLUGIN_CHECKO_KEY": " ключ1 ", "INN_CHECK_PLUGIN_DADATA_KEY": "",
           "DADATA_API_KEY": "из-окружения", "PATH": "/bin"}
    if ti.ключи_из_плагина(env) != ["CHECKO_API_KEY"] or env != {
            "CHECKO_API_KEY": "ключ1", "DADATA_API_KEY": "из-окружения", "PATH": "/bin"}:
        ошибки.append("перенос: %r" % env)
    env = {"INN_CHECK_PLUGIN_CHECKO_KEY": "${user_config.checko_api_key}",
           "CHECKO_API_KEY": "из-окружения"}
    if ti.ключи_из_плагина(env) or env != {"CHECKO_API_KEY": "из-окружения"}:
        ошибки.append("шаблон принят за ключ: %r" % env)
    конф = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"][
        "inn-check-ru"].get("env", {})
    манифест = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(
        encoding="utf-8")).get("userConfig", {})
    if set(конф) != set(ti.КЛЮЧИ_ПЛАГИНА):
        ошибки.append("env .mcp.json: %r" % sorted(конф))
    for имя, значение in конф.items():
        m = re.fullmatch(r"\$\{user_config\.(\w+)\}", значение)
        опция = манифест.get(m.group(1)) if m else None
        if not (опция and опция.get("sensitive") is True and not опция.get("required")):
            ошибки.append("%s -> %r: нет необязательной скрытой опции userConfig" % (имя, значение))
    if "tools_impl.ключи_из_плагина(os.environ)" not in SERVER.read_text(encoding="utf-8"):
        ошибки.append("server.py не переносит ключи плагина")
    return ошибки


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
    cases[".mcp.json плагина: точные версии не ниже границ requirements.txt"] = пины()
    cases["ключи из настроек плагина (userConfig) не затирают окружение"] = ключи()
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
