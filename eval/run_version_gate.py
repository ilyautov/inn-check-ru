#!/usr/bin/env python3
"""
run_version_gate.py — гейт «одна версия везде» (паттерн marketplaces-mcp-ru).

Источник истины — version в frontmatter SKILL.md. Релиз тащит версию в
плагин-манифесты, CHANGELOG (последняя секция), бейдж README, pyproject.toml,
server.json и манифесты агентов; расхождение = в реестр/плагин уедет версия,
которой нет в коде. Чистый stdlib, гоняется в CI.

Плюс две проверки реестра MCP (registry_checks), обе — по живым отказам:
  * пакет из server.json обязан иметь исполняемый файл с ТЕМ ЖЕ именем.
    Клиент собирает `uvx <identifier> <packageArguments>`, и аргументы уходят
    этому файлу. До 1.11.1 identifier был inn-check-ru — клиент запускал CLI
    проверки ИНН, получал справку и мёртвый процесс;
  * README этого пакета (он же описание на PyPI) несёт `mcp-name: <name>`.
    Без него реестр не признаёт пакет твоим и отказывает в публикации.
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


# Точка входа MCP-сервера. Исполняемый файл пакета из server.json обязан вести
# сюда: «файл с таким именем есть» мало — у inn-check-ru он есть и запускает CLI.
ТОЧКА_ВХОДА_MCP = "inn_check_ru_mcp.server:entry"

# Где лежит pyproject.toml пакета с данным именем на PyPI.
ПАКЕТЫ = {
    "inn-check-ru": ROOT,
    "inn-check-ru-mcp": ROOT / "packaging" / "inn-check-ru-mcp",
}


def _toml(path):
    """pyproject как словарь; tomllib есть с 3.11, на 3.10 — регэксп по нужным полям."""
    text = path.read_text(encoding="utf-8")
    try:
        import tomllib
        return tomllib.loads(text)
    except ImportError:
        pass
    proj = {}
    m = re.search(r'^name\s*=\s*"([^"]+)"', text, re.MULTILINE)
    proj["name"] = m.group(1) if m else None
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    proj["version"] = m.group(1) if m else None
    m = re.search(r'^readme\s*=\s*"([^"]+)"', text, re.MULTILINE)
    proj["readme"] = m.group(1) if m else None
    # Закрывающая скобка списка — последняя в строке: внутри элементов бывают
    # свои (`inn-check-ru[mcp]==…`), и ленивый `.*?\]` резал бы по ним.
    m = re.search(r"^dependencies\s*=\s*\[(.*?)\]\s*$", text, re.MULTILINE | re.DOTALL)
    proj["dependencies"] = re.findall(r'"([^"]+)"', m.group(1)) if m else []
    блок = re.search(r"^\[project\.scripts\]\n(.*?)(?:^\[|\Z)", text,
                     re.MULTILINE | re.DOTALL)
    proj["scripts"] = dict(re.findall(r'^([\w.-]+)\s*=\s*"([^"]+)"', блок.group(1),
                                      re.MULTILINE)) if блок else {}
    return {"project": proj}


# `mcp-name:` + имя + граница: перевод строки, пробел, тег или `-->`.
# Точка вплотную («…/inn-check-ru.») границей НЕ считается — так же, как в реестре.
def _mcp_name_есть(text, name):
    return re.search(r"mcp-name:\s*" + re.escape(name) + r"(?=\s|<|-->|$)", text) is not None


def registry_checks(v):
    """Ошибки, из-за которых реестр MCP отказал бы или выдал нерабочую команду."""
    errors = []
    try:
        sj = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return ["server.json не читается: %s" % e]
    имя = sj.get("name")
    for pkg in sj.get("packages", []):
        ident = pkg.get("identifier")
        if pkg.get("registryType") != "pypi":
            continue
        где = ПАКЕТЫ.get(ident)
        if где is None or not (где / "pyproject.toml").is_file():
            errors.append("server.json: пакет %r не собирается из этого репозитория "
                          "(нет в ПАКЕТЫ или нет pyproject.toml)" % ident)
            continue
        proj = _toml(где / "pyproject.toml")["project"]
        if proj.get("name") != ident:
            errors.append("%s/pyproject.toml: name %r != identifier %r"
                          % (где.relative_to(ROOT) if где != ROOT else ".",
                             proj.get("name"), ident))
        скрипты = proj.get("scripts") or {}
        if ident not in скрипты:
            errors.append(
                "server.json: у пакета %r нет исполняемого файла %r — клиент реестра "
                "запустит `uvx %s` и получит не MCP-сервер (есть: %s)"
                % (ident, ident, ident, ", ".join(sorted(скрипты)) or "ничего"))
        elif скрипты[ident] != ТОЧКА_ВХОДА_MCP:
            errors.append(
                "server.json: `uvx %s` запустит %r, а не MCP-сервер (%s) — клиент "
                "реестра получит чужую программу" % (ident, скрипты[ident], ТОЧКА_ВХОДА_MCP))
        # Аргументы уходят исполняемому файлу; `--from` там — признак попытки
        # подменить исполняемый файл аргументами, что по спеке невозможно.
        for arg in pkg.get("packageArguments") or []:
            if arg.get("name") == "--from" or arg.get("value") == "--from":
                errors.append("server.json: `--from` в packageArguments уйдёт "
                              "исполняемому файлу %r, а не uvx" % ident)
        readme = где / (proj.get("readme") or "README.md")
        try:
            текст = readme.read_text(encoding="utf-8")
        except OSError:
            текст = ""
        if not _mcp_name_есть(текст, имя):
            errors.append("%s: нет `mcp-name: %s` с границей после имени — реестр "
                          "не признает пакет %r своим" % (readme.relative_to(ROOT), имя, ident))
        if где != ROOT:
            if proj.get("version") != v:
                errors.append("%s: version %r != %r" % (где.relative_to(ROOT),
                                                        proj.get("version"), v))
            пин = "inn-check-ru[mcp]==%s" % v
            if пин not in (proj.get("dependencies") or []):
                errors.append("%s: зависимость должна быть ровно %r, а не %r — "
                              "обёртка поверх другой версии движка отвечает не тем кодом"
                              % (где.relative_to(ROOT), пин, proj.get("dependencies")))
    return errors


def main():
    v = skill_version()
    errors = []
    for name, found in checks(v):
        if found != v:
            errors.append("%s: %r != %r" % (name, found, v))
    errors += registry_checks(v)
    if errors:
        print("FAIL версионный гейт (истина: SKILL.md = %s)" % v)
        for e in errors:
            print("  - %s" % e)
        return 1
    print("PASS версионный гейт: везде %s" % v)
    return 0


if __name__ == "__main__":
    sys.exit(main())
