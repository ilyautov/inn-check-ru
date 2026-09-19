#!/usr/bin/env python3
"""
run_version_gate.py — гейт «одна версия везде» (паттерн marketplaces-mcp-ru).

Источник истины — version в frontmatter SKILL.md. Релиз тащит версию в
плагин-манифесты, CHANGELOG (последняя секция), бейдж README, pyproject.toml,
server.json и манифесты агентов; расхождение = в реестр/плагин уедет версия,
которой нет в коде. Чистый stdlib, гоняется в CI.
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def skill_version():
    text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    fm = text.split("---")[1]
    m = re.search(r'^\s*version:\s*"([^"]+)"', fm, re.MULTILINE)
    if not m:
        raise SystemExit("version не найден в frontmatter SKILL.md")
    return m.group(1)


def checks(v):
    out = []  # (имя, найденная версия)

    def json_version(path, *keys):
        try:
            j = json.loads((ROOT / path).read_text(encoding="utf-8"))
            for k in keys:
                j = j[k]
            out.append((path, j))
        except (OSError, ValueError, KeyError) as e:
            out.append((path, "ОШИБКА: %s" % e))

    json_version(".claude-plugin/plugin.json", "version")
    json_version(".claude-plugin/marketplace.json", "metadata", "version")
    json_version("server.json", "version")
    json_version(".codex-plugin/plugin.json", "version")
    json_version(".cursor-plugin/plugin.json", "version")
    json_version("gemini-extension.json", "version")

    ch = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    m = re.search(r"^## \[([0-9]+\.[0-9]+\.[0-9]+)\]", ch, re.MULTILINE)
    out.append(("CHANGELOG.md (последняя секция)", m.group(1) if m else "не найдена"))

    rd = (ROOT / "README.md").read_text(encoding="utf-8")
    m = re.search(r"версия-([0-9]+\.[0-9]+\.[0-9]+)-blueviolet", rd)
    out.append(("README.md (бейдж)", m.group(1) if m else "не найдена"))

    pp = ROOT / "pyproject.toml"
    if pp.exists():
        m = re.search(r'^version\s*=\s*"([^"]+)"',
                      pp.read_text(encoding="utf-8"), re.MULTILINE)
        out.append(("pyproject.toml", m.group(1) if m else "не найдена"))

    sj = ROOT / "server.json"
    if sj.exists():
        try:
            j = json.loads(sj.read_text(encoding="utf-8"))
            for pkg in j.get("packages", []):
                out.append(("server.json packages[%s]" % pkg.get("identifier"),
                            pkg.get("version", "нет")))
            # реестр отклоняет description > 100 символов (422 на публикации)
            if len(j.get("description", "")) > 100:
                out.append(("server.json description >100 chars", "ПРЕВЫШЕНО"))
        except ValueError as e:
            out.append(("server.json", "ОШИБКА: %s" % e))
    return out


def main():
    v = skill_version()
    errors = []
    for name, found in checks(v):
        if found != v:
            errors.append("%s: %r != %r" % (name, found, v))
    if errors:
        print("FAIL версионный гейт (истина: SKILL.md = %s)" % v)
        for e in errors:
            print("  - %s" % e)
        return 1
    print("PASS версионный гейт: везде %s" % v)
    return 0


if __name__ == "__main__":
    sys.exit(main())
