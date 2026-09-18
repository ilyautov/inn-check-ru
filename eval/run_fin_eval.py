#!/usr/bin/env python3
"""
run_fin_eval.py — офлайн-прогон fin_scoring.py по синтетическим фикстурам.

Сверяет ожидаемые флаги (id + severity) с фактическими, печатает PASS/FAIL.
Чистый python3 stdlib, сети не требует — поэтому гоняется в CI.
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCORING = ROOT / "scripts" / "fin_scoring.py"
FIXTURES = ROOT / "eval" / "fixtures"

# Ожидания: файл -> (ожидаемые флаги {id: severity}, идентификаторы, которых
# быть НЕ должно, ожидаемый статус).
CASES = {
    "healthy.json": (
        {},  # здоровая компания — ни одного флага
        ["ча_отрицательные_2года", "транзитный_профиль", "падение_выручки",
         "убыток_2года", "коэффициент_автономии", "текущая_ликвидность",
         "ча_ниже_уставного_капитала"],
        "ок",
    ),
    "cha_negative.json": (
        {"ча_отрицательные_2года": "🔴",
         "ча_ниже_уставного_капитала": "🟡",
         "коэффициент_автономии": "🟡",
         "текущая_ликвидность": "🟡",
         "падение_выручки": "🟡",
         "убыток_2года": "🟡"},
        ["транзитный_профиль"],
        "ок",
    ),
    "transit.json": (
        {"транзитный_профиль": "🟡"},
        ["ча_отрицательные_2года", "убыток_2года"],
        "ок",
    ),
}


def run_case(name, expected_flags, forbidden_ids, expected_status):
    path = FIXTURES / name
    proc = subprocess.run(
        [sys.executable, str(SCORING), str(path)],
        capture_output=True, text=True, check=False,
    )
    errors = []
    if proc.returncode != 0:
        return ["exit code %s, stderr: %s" % (proc.returncode, proc.stderr.strip())]
    try:
        out = json.loads(proc.stdout)
    except ValueError:
        return ["вывод не JSON: %.200s" % proc.stdout]
    if out.get("статус") != expected_status:
        errors.append("статус %r != %r" % (out.get("статус"), expected_status))
    флаги = {f["id"]: f for f in out.get("флаги", [])}
    for fid, sev in expected_flags.items():
        got = флаги.get(fid)
        if got is None:
            errors.append("ожидался флаг %s (%s), его нет" % (fid, sev))
        elif got.get("severity") != sev:
            errors.append("флаг %s: severity %r != %r"
                          % (fid, got.get("severity"), sev))
    for fid in forbidden_ids:
        if fid in флаги:
            errors.append("флага %s быть не должно" % fid)
    return errors


def main():
    failed = 0
    for name, (expected, forbidden, status) in sorted(CASES.items()):
        errors = run_case(name, expected, forbidden, status)
        if errors:
            failed += 1
            print("FAIL %s" % name)
            for e in errors:
                print("  - %s" % e)
        else:
            print("PASS %s" % name)
    if failed:
        print("FAIL: %d/%d кейсов упало" % (failed, len(CASES)))
        return 1
    print("PASS: все %d кейсов зелёные" % len(CASES))
    return 0


if __name__ == "__main__":
    sys.exit(main())
