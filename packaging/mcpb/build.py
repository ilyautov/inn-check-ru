#!/usr/bin/env python3
"""Сборка расширения Claude Desktop: dist/inn-check-ru.mcpb.

Тип сервера uv (MCPB manifest 0.4): в архиве только манифест, pyproject.toml и
обёртка src/server.py, а движок inn-check-ru[mcp] той же версии хост ставит сам
через uv. Python у пользователя не нужен, архив весит килобайты.

    python3 packaging/mcpb/build.py            # dist/inn-check-ru.mcpb
    python3 packaging/mcpb/build.py --каталог  # ещё и распакованный dist/mcpb/

Версия — из корневого pyproject.toml, отдельной версии у расширения нет.
"""

import argparse
import json
import re
import shutil
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]

PYPROJECT = """[project]
name = "inn-check-ru-mcpb"
version = "{version}"
description = "Claude Desktop extension: inn-check-ru MCP server"
requires-python = ">=3.10"
dependencies = ["inn-check-ru[mcp]=={version}"]
"""


def версия():
    текст = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', текст, re.MULTILINE)
    if not m:
        raise SystemExit("в pyproject.toml нет version")
    return m.group(1)


def файлы(v):
    """Имя в архиве -> содержимое (bytes)."""
    манифест = json.loads((HERE / "manifest.template.json").read_text(encoding="utf-8"))
    манифест["version"] = v
    return {
        "manifest.json": (json.dumps(манифест, ensure_ascii=False, indent=2) + "\n").encode(),
        "pyproject.toml": PYPROJECT.format(version=v).encode(),
        "src/server.py": (HERE / "src" / "server.py").read_bytes(),
        "LICENSE": (ROOT / "LICENSE").read_bytes(),
    }


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--каталог", action="store_true",
                    help="распаковать рядом dist/mcpb/ (для проверки uv run без хоста)")
    a = ap.parse_args(argv)
    v = версия()
    состав = файлы(v)
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    архив = dist / "inn-check-ru.mcpb"
    with zipfile.ZipFile(архив, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for имя, данные in состав.items():
            z.writestr(имя, данные)
    if a.каталог:
        каталог = dist / "mcpb"
        shutil.rmtree(каталог, ignore_errors=True)
        for имя, данные in состав.items():
            путь = каталог / имя
            путь.parent.mkdir(parents=True, exist_ok=True)
            путь.write_bytes(данные)
    print("%s: %d файлов, %d байт, версия %s" % (архив.relative_to(ROOT), len(состав),
                                                архив.stat().st_size, v))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
