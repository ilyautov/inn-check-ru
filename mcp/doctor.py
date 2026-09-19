#!/usr/bin/env python3
"""
doctor.py — отчёт «взлетит ли эта установка» в стилистике семьи
schema-mcp-core: python, MCP SDK, скрипты движка, ключи, кэш.
"""

import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.expanduser(os.path.join("~", ".cache", "inn-check-ru"))

SCRIPTS = ("fetch_counterparty.py", "fin_scoring.py", "sanctions_check.py",
           "affiliates_graph.py", "droblenie_check.py", "diff_counterparty.py",
           "registries_refresh.py")


def main():
    проверки = []

    def add(имя, ок, деталь):
        проверки.append({"проверка": имя, "ok": bool(ок), "деталь": деталь})

    vi = sys.version_info
    add("python >= 3.10", vi >= (3, 10),
        "%d.%d.%d" % (vi.major, vi.minor, vi.micro))
    try:
        import mcp  # noqa: F401
        add("MCP SDK (pip install mcp)", True, "установлен")
    except ImportError:
        add("MCP SDK (pip install mcp)", False, "нет — pip install mcp")
    отсутствуют = [s for s in SCRIPTS
                   if not os.path.exists(os.path.join(REPO_ROOT, "scripts", s))]
    add("скрипты движка scripts/", not отсутствуют,
        "на месте" if not отсутствуют else "нет: %s" % ", ".join(отсутствуют))
    key = os.environ.get("CHECKO_API_KEY")
    add("CHECKO_API_KEY (граф связей)", bool(key),
        "задан" if key else "не задан — affiliates_graph/droblenie_check "
                            "ответят «не проверено» (не ошибка)")
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        writable = os.access(CACHE_DIR, os.W_OK)
    except OSError:
        writable = False
    add("кэш ~/.cache/inn-check-ru доступен на запись", writable, CACHE_DIR)

    критично = all(p["ok"] for p in проверки[:3])
    out = {"статус": "взлетит" if критично else "не взлетит",
           "проверки": проверки}
    sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
    return 0 if критично else 1


if __name__ == "__main__":
    sys.exit(main())
