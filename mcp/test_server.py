#!/usr/bin/env python3
"""
test_server.py — дымовой тест MCP-сервера без живого MCP-клиента.

1. tools_impl: каждый EXPECTED инструмент существует и вызывается;
   counterparty_fetch на фикстурном выводе (monkeypatch run_script) отдаёт
   JSON с обязательными полями; ошибочный путь -> «не проверено», не traceback.
2. SDK-транспорт (если пакет mcp установлен): сервер импортируется, список
   инструментов == EXPECTED_TOOLS.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

MCP_DIR = Path(__file__).resolve().parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


FIXTURE_FETCH = {
    "инн": "7707083893",
    "тип": "юрлицо",
    "профиль": "нейтрально",
    "егрюл": {"статус": "Действующая организация", "вид": "ul"},
    "мсп": None,   # «пусто» -> блок данных null, факт в _доступность (§1.2)
    "_доступность": {
        # словарь фиксированной формы §1.1 (волна 1), не строка
        "егрюл": {"состояние": "ok", "причина": None, "канарейка": "не запускалась",
                  "tier": "🟢", "требует": "сеть", "дата": "2026-09-19"},
        "мсп": {"состояние": "пусто",
                "причина": "не найдена в Едином реестре МСП (крупная компания либо "
                           "исключена) — для МСБ-контрагента это стоп-сигнал",
                "канарейка": "ok", "tier": "🟢", "требует": "сеть",
                "дата": "2026-09-19"},
    },
    "_итог_проверки": {
        "источников": 12, "ok": 1, "пусто": 1, "не_проверено": 10,
        "deal_killer_источники": ["егрюл", "риски", "спецреестры", "санкции",
                                  "фссп", "суды", "банкротство"],
        "deal_killer_не_проверено": ["риски", "спецреестры", "санкции",
                                     "фссп", "суды", "банкротство"],
        "проверка_состоялась": False,
        "вывод": "проверка НЕ состоялась: 6 из 7 deal-killer-источников не проверено "
                 "— светофор 🟢 недопустим",
    },
}


def case_tools_impl(ti):
    errors = []

    def check(cond, msg):
        if not cond:
            errors.append(msg)

    for name in ti.EXPECTED_TOOLS:
        check(callable(getattr(ti, name, None)), "нет функции %s" % name)

    # офлайн-путь: подменяем run_script фикстурой
    orig = ti.run_script
    calls = []

    def fake(script, args, stdin_text=None, timeout=None):
        calls.append((script, args, stdin_text is not None))
        if script == "fetch_counterparty.py":
            return dict(FIXTURE_FETCH)
        if script == "fin_scoring.py":
            return {"статус": "не проверено", "причина": "нет отчётности"}
        if script == "sanctions_check.py":
            return {"статус": "проверено", "совпадений": 0}
        if script == "diff_counterparty.py":
            return {"статус": "изменений нет"}
        return {"статус": "не проверено", "причина": "фикстура"}

    ti.run_script = fake
    try:
        out = ti.counterparty_fetch("7707083893")
        check(out.get("инн") == "7707083893", "fetch: нет инн")
        check(out.get("тип") == "юрлицо", "fetch: нет тип")
        out2 = ti.counterparty_fetch("не-инн")
        check(out2.get("статус") == "не проверено",
              "невалидный ИНН должен дать «не проверено»")
        out3 = ti.counterparty_fin_scoring("7707083893")
        check(out3.get("статус") == "не проверено",
              "цепочка fin_scoring не дошла: %s" % out3)
        check(any(c[0] == "fin_scoring.py" and c[2] for c in calls),
              "fin_scoring не получил stdin")
        out4 = ti.sanctions_check(name="Иванов Иван")
        check(out4.get("статус") == "проверено", "sanctions: %s" % out4)
        out5 = ti.sanctions_check()
        check(out5.get("статус") == "не проверено",
              "sanctions без аргументов: %s" % out5)
        out6 = ti.counterparty_diff("7707083893")
        check(out6.get("статус") == "изменений нет", "diff: %s" % out6)

        # ошибочный путь: runner падает таймаутом -> «не проверено», не traceback
        def boom(script, args, stdin_text=None, timeout=None):
            raise subprocess.TimeoutExpired(cmd=script, timeout=1)
        ti.run_script = boom
        out7 = ti.counterparty_fetch("7707083893")
        check(out7.get("статус") == "не проверено",
              "таймаут должен дать «не проверено»: %s" % out7)
    finally:
        ti.run_script = orig
    return errors


def case_sdk():
    """Список инструментов сервера == EXPECTED_TOOLS (нужен пакет mcp)."""
    try:
        import mcp  # noqa: F401
    except ImportError:
        return ["SKIP: пакет mcp не установлен (проверяется в CI)"]
    import asyncio
    errors = []
    sys.path.insert(0, str(MCP_DIR))
    try:
        server = load("inn_check_mcp_server", MCP_DIR / "server.py")
        tools = asyncio.run(server.mcp_server.list_tools())
        names = sorted(t.name for t in tools)
        ti = load("tools_impl", MCP_DIR / "tools_impl.py")
        if names != sorted(ti.EXPECTED_TOOLS):
            errors.append("инструменты %s != %s" % (names, sorted(ti.EXPECTED_TOOLS)))
    except Exception as e:
        errors.append("сервер не поднялся: %s: %s" % (type(e).__name__, e))
    finally:
        sys.path.remove(str(MCP_DIR))
    return errors


def main():
    ti = load("tools_impl", MCP_DIR / "tools_impl.py")
    cases = {"tools_impl-логика": case_tools_impl(ti),
             "sdk-транспорт": case_sdk()}
    failed = 0
    for name, errors in cases.items():
        if errors and errors[0].startswith("SKIP"):
            print("SKIP %s — %s" % (name, errors[0][5:]))
        elif errors:
            failed += 1
            print("FAIL %s" % name)
            for e in errors:
                print("  - %s" % e)
        else:
            print("PASS %s" % name)
    if failed:
        print("FAIL: %d кейсов упало" % failed)
        return 1
    print("PASS: дымовой тест зелёный")
    return 0


if __name__ == "__main__":
    sys.exit(main())
