#!/usr/bin/env python3
"""
run_diff_eval.py — офлайн-прогон diff_counterparty.py по парам снимков-фикстур.

Сверяет ожидаемые изменения (поле + severity) с фактическими, PASS/FAIL.
Чистый python3 stdlib, сети не требует — гоняется в CI.
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIFF = ROOT / "scripts" / "diff_counterparty.py"
FIXTURES = ROOT / "eval" / "fixtures" / "snapshots"

# Пара снимков -> (ожидаемые изменения {поле: severity}, запрещённые поля,
# ожидаемый статус).
CASES = {
    ("director_a.json", "director_b.json"): (
        {"руководитель": "🟡"},
        ["статус", "адрес", "недостоверность_сведений"],
        "сравнено",
    ),
    ("status_a.json", "status_b.json"): (
        {"статус": "🔴", "дата_прекращения": "🔴",
         "чистые_активы": "🔴", "выручка": "🟡"},
        ["руководитель"],
        "сравнено",
    ),
    ("same_a.json", "same_b.json"): (
        {},
        ["статус", "руководитель", "адрес", "чистые_активы"],
        "изменений нет",
    ),
}


def run_case(pair, expected, forbidden, expected_status):
    a, b = (str(FIXTURES / name) for name in pair)
    proc = subprocess.run(
        [sys.executable, str(DIFF), "--a", a, "--b", b],
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
    изменения = {c["поле"]: c for c in out.get("изменения", [])}
    for поле, sev in expected.items():
        got = изменения.get(поле)
        if got is None:
            errors.append("ожидалось изменение %s (%s), его нет" % (поле, sev))
        elif got.get("severity") != sev:
            errors.append("%s: severity %r != %r"
                          % (поле, got.get("severity"), sev))
        else:
            for ключ in ("было", "стало", "дата_было", "дата_стало"):
                if ключ not in got:
                    errors.append("%s: нет поля %r" % (поле, ключ))
    for поле in forbidden:
        if поле in изменения:
            errors.append("изменения %s быть не должно" % поле)
    return errors


def main():
    failed = 0
    for pair, (expected, forbidden, status) in sorted(CASES.items()):
        errors = run_case(pair, expected, forbidden, status)
        label = "%s -> %s" % pair
        if errors:
            failed += 1
            print("FAIL %s" % label)
            for e in errors:
                print("  - %s" % e)
        else:
            print("PASS %s" % label)
    if failed:
        print("FAIL: %d/%d кейсов упало" % (failed, len(CASES)))
        return 1
    print("PASS: все %d кейсов зелёные" % len(CASES))
    return 0


if __name__ == "__main__":
    sys.exit(main())
