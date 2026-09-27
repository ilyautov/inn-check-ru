#!/usr/bin/env python3
"""
run_commands_eval.py — слэш-команды плагина (commands/*.md) не расходятся с кодом:
фронтматтер с description и argument-hint, каждый упомянутый скрипт есть в
scripts/, каждый его флаг — в исходнике этого скрипта, каждый профиль — в
data/profiles_ru.json, каждый файл references/ существует, есть $ARGUMENTS.
Набор команд — ровно тот, что в спеке волны 6. PASS/FAIL, CI.
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
КОМАНДЫ = ROOT / "commands"
НАБОР = {"inn-check", "inn-dossier", "inn-batch", "inn-watch", "inn-retro", "inn-links",
         "inn-vat", "inn-self"}


def фронтматтер(текст):
    """Простые пары «ключ: "значение"» между «---»; None — если блока нет или
    значение не в кавычках (двоеточие внутри ломает YAML)."""
    m = re.match(r"---\n(.*?)\n---\n", текст, re.S)
    if not m:
        return None
    поля = {}
    for строка in m.group(1).splitlines():
        ключ, _, значение = строка.partition(": ")
        if not re.fullmatch(r'"[^"\n]*"', значение):
            return None
        поля[ключ] = значение[1:-1]
    return поля


def ошибки_команды(путь, профили):
    текст = путь.read_text(encoding="utf-8")
    ош = []
    фм = фронтматтер(текст)
    if фм is None or not фм.get("description") or "argument-hint" not in фм:
        ош.append("фронтматтер: нужны description и argument-hint в кавычках")
    if "$ARGUMENTS" not in текст:
        ош.append("нет $ARGUMENTS — аргументы команды потеряются")
    # «скрипт.py … --флаг» в одних обратных кавычках: флаг обязан быть в исходнике
    for кусок in re.findall(r"`([^`]+)`", текст):
        скрипты = re.findall(r"([\w-]+\.py)", кусок)
        for имя in скрипты:
            if not (ROOT / "scripts" / имя).exists():
                ош.append("нет scripts/%s" % имя)
        if len(скрипты) == 1 and (ROOT / "scripts" / скрипты[0]).exists():
            исходник = (ROOT / "scripts" / скрипты[0]).read_text(encoding="utf-8")
            for флаг in re.findall(r"(--[\w-]+)", кусок):
                if флаг not in исходник:
                    ош.append("%s: флага %s в скрипте нет" % (скрипты[0], флаг))
        if re.fullmatch(r"[\w.-]+/[\w./-]+\.(md|json)", кусок) and not (ROOT / кусок).exists():
            ош.append("нет файла %s" % кусок)
        for п in re.findall(r"--профиль (\w+)", кусок):
            if п not in профили:
                ош.append("профиля %s нет" % п)
        # одиночное слово кириллицей в кавычках — имя профиля
        if re.fullmatch(r"[а-я_0-9]+", кусок) and кусок not in профили:
            ош.append("профиля %s нет" % кусок)
    return ош


def main():
    профили = set(json.loads((ROOT / "data" / "profiles_ru.json").read_text(
        encoding="utf-8"))["профили"])
    cases = {}
    файлы = sorted(КОМАНДЫ.glob("*.md"))
    cases["набор команд — как в спеке"] = (
        [] if {f.stem for f in файлы} == НАБОР
        else ["есть %s, ждали %s" % (sorted(f.stem for f in файлы), sorted(НАБОР))])
    for f in файлы:
        cases["/%s сходится с кодом" % f.stem] = ошибки_команды(f, профили)
    # сама проверка ловит расхождения, а не молчит
    проба = ROOT / "commands" / ".проба.md"
    try:
        проба.write_text('---\ndescription: без: кавычек\nargument-hint: "x"\n---\n'
                         "`fetch_counterparty.py --нет-такого` `нет_скрипта.py` "
                         "`--профиль выдуманный` `references/нет.md`\n", encoding="utf-8")
        ош = ошибки_команды(проба, профили)
        ждём = ("фронтматтер", "$ARGUMENTS", "--нет-такого", "нет_скрипта.py",
                "выдуманный", "references/нет.md")
        cases["проверка ловит расхождения"] = [
            "не поймано: %s" % ж for ж in ждём if not any(ж in e for e in ош)]
    finally:
        проба.unlink(missing_ok=True)
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
