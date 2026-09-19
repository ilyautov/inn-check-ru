#!/usr/bin/env python3
"""
run_workflows_gate.py — гейт рабочих процессов GitHub Actions.

Зачем: 19.09.2026 выяснилось, что ci.yml не разбирался как YAML — в двух
именах шагов стояло незакавыченное двоеточие («мониторинг: отпечатки…»).
Ошибка тихая вдвойне: локально ничего не падает, а на GitHub не падает
отдельный шаг — не запускается весь рабочий процесс, и отсутствие красного
читается как «всё зелено». Проверять это самим рабочим процессом нельзя:
сломанный YAML не запустит и проверку себя. Поэтому гейт локальный.

Что проверяется (чистый stdlib, парсера YAML в наборе нет — разбор строчный):
1. имя шага с «: » обязано быть в кавычках;
2. каждый `python3 eval/...py` и `python3 scripts/...py` из шагов указывает
   на существующий файл — шаг, ссылающийся на переименованный скрипт,
   красит CI уже после пуша;
3. у каждого шага есть либо `uses`, либо `run`.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"

RE_NAME = re.compile(r'^(\s*)- name:\s*(.*?)\s*$')
RE_STEP = re.compile(r'^(\s*)- (name|uses|run):')
RE_SCRIPT = re.compile(r'python3?\s+((?:eval|scripts|mcp)/[\w./-]+\.py)')


def проверить(path):
    errors = []
    text = path.read_text(encoding="utf-8")
    имя_файла = path.relative_to(ROOT)

    for n, line in enumerate(text.splitlines(), 1):
        m = RE_NAME.match(line)
        if not m:
            continue
        значение = m.group(2)
        если_в_кавычках = (значение.startswith('"') and значение.endswith('"')) or (
            значение.startswith("'") and значение.endswith("'"))
        if ": " in значение and not если_в_кавычках:
            errors.append(
                "%s:%d — имя шага с двоеточием не в кавычках: %s\n"
                "      YAML прочтёт это как вложенный ключ и не разберёт файл целиком"
                % (имя_файла, n, значение))

    # шаг без uses и без run — нечего выполнять, GitHub отклонит
    блоки, текущий = [], None
    for n, line in enumerate(text.splitlines(), 1):
        m = RE_STEP.match(line)
        if m and m.group(2) == "name" and line.lstrip().startswith("- "):
            if текущий:
                блоки.append(текущий)
            текущий = {"строка": n, "имя": line.split("- name:", 1)[1].strip(),
                       "ключи": set()}
        elif текущий is not None:
            k = re.match(r'^\s+(\w[\w-]*):', line)
            if k:
                текущий["ключи"].add(k.group(1))
            if line.lstrip().startswith("- ") and ": " in line and "name:" not in line:
                pass
    if текущий:
        блоки.append(текущий)
    for b in блоки:
        if not ({"uses", "run"} & b["ключи"]):
            errors.append("%s:%d — шаг %r без uses и без run"
                          % (имя_файла, b["строка"], b["имя"]))

    for n, line in enumerate(text.splitlines(), 1):
        for скрипт in RE_SCRIPT.findall(line):
            if not (ROOT / скрипт).exists():
                errors.append("%s:%d — шаг зовёт %s, а такого файла нет"
                              % (имя_файла, n, скрипт))
    return errors


def main():
    if not WORKFLOWS.is_dir():
        print("FAIL: нет каталога .github/workflows")
        return 1
    файлы = sorted(list(WORKFLOWS.glob("*.yml")) + list(WORKFLOWS.glob("*.yaml")))
    if not файлы:
        print("FAIL: в .github/workflows нет ни одного рабочего процесса")
        return 1
    всего = 0
    for path in файлы:
        errors = проверить(path)
        всего += len(errors)
        if errors:
            print("FAIL %s" % path.relative_to(ROOT))
            for e in errors:
                print("  - %s" % e)
        else:
            print("PASS %s" % path.relative_to(ROOT))
    if всего:
        print("FAIL: %d замечаний в рабочих процессах" % всего)
        return 1
    print("PASS гейт рабочих процессов: %d файлов" % len(файлы))
    return 0


if __name__ == "__main__":
    sys.exit(main())
