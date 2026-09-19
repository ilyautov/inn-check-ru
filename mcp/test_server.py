#!/usr/bin/env python3
"""
test_server.py — дымовой тест MCP-сервера без живого MCP-клиента.

1. tools_impl: каждый EXPECTED инструмент существует и вызывается;
   counterparty_fetch на фикстурном выводе (monkeypatch run_script) отдаёт
   JSON с обязательными полями; ошибочный путь -> «не проверено», не traceback.
   Волна 2: counterparty_verdict / counterparty_batch (лимит 50) / access_check
   гоняются тем же офлайн-путём — движок не запускается, сети нет.
2. SDK-транспорт (если пакет mcp установлен): сервер импортируется, список
   инструментов == EXPECTED_TOOLS.
3. Живой stdio-хендшейк отдельным процессом: initialize (имя и версия из
   SKILL.md, не версия SDK), tools/list по транспорту, tools/call с битым
   ИНН — движок должен быть достижим из процесса сервера. Импорт модуля
   этого не проверяет: команда запуска может не стартовать вовсе.
"""

import importlib.util
import json
import re
import subprocess
import sys
import threading
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


FIXTURE_BATCH = {
    "статус": "ок", "профиль": "отсрочка", "режим": "quick",
    "быстрый_режим": "доступен", "всего": 2, "проверено": 1,
    "не_проверено": 0, "некорректных_инн": 1, "дублей_схлопнуто": 0,
    "результаты": [
        {"инн": "7707083893", "название": "ПАО «Тест»", "светофор": "🔴",
         "статус": "проверено", "сигналы": ["недостоверность_сведений"],
         "проверка_состоялась": True, "не_проверено": [],
         "рекомендация": "не отгружать в долг", "вердикт": {"факты": []}},
        {"инн": "1234567890", "светофор": None, "статус": "некорректный ИНН",
         "сигналы": [], "причина": "неверное контрольное число"},
    ],
}

FIXTURE_ACCESS = {
    "дата": "2026-09-19T14:00:00", "ip_класс": "не-РФ",
    "источники": {"егрюл": {"состояние": "доступен", "http": 307, "мс": 350,
                            "причина": None}},
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
        if script == "batch_check.py":
            return dict(FIXTURE_BATCH)
        if script == "check_access.py":
            return dict(FIXTURE_ACCESS)
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

        # --- волна 2: вердикт / батч / доступность ---
        v = ti.counterparty_verdict("7707083893", profile="отсрочка")
        check(v.get("светофор") == "🔴", "verdict: светофор %s" % v.get("светофор"))
        check(v.get("сигналы") == ["недостоверность_сведений"],
              "verdict: сигналы %s" % v.get("сигналы"))
        check(v.get("профиль") == "отсрочка", "verdict: профиль %s" % v.get("профиль"))
        check(any(c[0] == "batch_check.py" and "--режим" in c[1] and "полный" in c[1]
                  for c in calls),
              "verdict должен звать батч полным режимом: %s" % [c[1] for c in calls])
        check(ti.counterparty_verdict("не-инн").get("статус") == "не проверено",
              "verdict: невалидный ИНН должен дать «не проверено»")

        b = ti.counterparty_batch(["7707083893", "1234567890"], profile="отсрочка")
        check(len(b.get("результаты") or []) == 2, "batch: результаты %s" % b)
        check((b["результаты"][0] or {}).get("светофор") == "🔴",
              "batch: сверху должен быть 🔴")
        b2 = ti.counterparty_batch("7707083893, 1234567890")
        check(len(b2.get("результаты") or []) == 2, "batch: строка через запятую %s" % b2)
        check(ti.counterparty_batch(["1"] * 51).get("статус") == "не проверено",
              "batch: лимит 50 не сработал")
        check(ti.counterparty_batch([]).get("статус") == "не проверено",
              "batch: пустой список должен дать «не проверено»")
        check(ti.counterparty_batch(42).get("статус") == "не проверено",
              "batch: не-список должен дать «не проверено», не traceback")

        a = ti.access_check()
        check(a.get("ip_класс") == "не-РФ", "access_check: %s" % a)

        # ошибочный путь: runner падает таймаутом -> «не проверено», не traceback
        def boom(script, args, stdin_text=None, timeout=None):
            raise subprocess.TimeoutExpired(cmd=script, timeout=1)
        ti.run_script = boom
        out7 = ti.counterparty_fetch("7707083893")
        check(out7.get("статус") == "не проверено",
              "таймаут должен дать «не проверено»: %s" % out7)
        for имя in ("counterparty_verdict", "counterparty_batch", "access_check"):
            fn = getattr(ti, имя)
            res = fn(["7707083893"]) if имя == "counterparty_batch" else (
                fn() if имя == "access_check" else fn("7707083893"))
            check(isinstance(res, dict) and res.get("статус") == "не проверено",
                  "%s при сбое движка должен дать «не проверено»: %s" % (имя, res))
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


def _версия_скилла():
    """Версия из frontmatter SKILL.md — источник истины релиза."""
    head = (MCP_DIR.parent / "SKILL.md").read_text(encoding="utf-8")[:2048]
    m = re.search(r'^\s*version:\s*"([^"]+)"', head, re.MULTILINE)
    return m.group(1) if m else None


def case_stdio():
    """Живой stdio-хендшейк: сервер поднимается ОТДЕЛЬНЫМ ПРОЦЕССОМ.

    Проверки в одном процессе (case_sdk) не ловят поломку запуска: импорт
    модуля работает и тогда, когда команда из .mcp.json не стартует вовсе.
    Именно так «Connection closed» жил незамеченным.
    """
    try:
        import mcp  # noqa: F401
    except ImportError:
        return ["SKIP: пакет mcp не установлен (проверяется в CI)"]

    запрос = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "smoke", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "counterparty_fetch", "arguments": {"inn": "123"}}},
    ]
    # stdin держим открытым, пока не получены все ответы: закрытие сразу
    # после записи гасит сервер посреди обработки вызова инструмента
    proc = subprocess.Popen(
        [sys.executable, str(MCP_DIR / "server.py")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1)
    ответы = {}
    таймер = threading.Timer(120, proc.kill)
    таймер.start()
    try:
        for m in запрос:
            proc.stdin.write(json.dumps(m) + "\n")
            proc.stdin.flush()
        for строка in proc.stdout:
            строка = строка.strip()
            if not строка:
                continue
            try:
                ответ = json.loads(строка)
            except ValueError:
                continue
            if ответ.get("id") is not None:
                ответы[ответ["id"]] = ответ
            if 3 in ответы:
                break
    except (BrokenPipeError, OSError):
        pass
    finally:
        таймер.cancel()
        try:
            proc.stdin.close()
        except OSError:
            pass
        proc.terminate()
        try:
            _, stderr = proc.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            _, stderr = proc.communicate()

    errors = []
    if 1 not in ответы:
        return ["сервер не прошёл initialize (код %s): %s"
                % (proc.returncode, (stderr or "")[-400:])]

    info = ответы[1].get("result", {}).get("serverInfo", {})
    if info.get("name") != "inn-check-ru":
        errors.append("serverInfo.name = %r" % info.get("name"))
    ожидаемая = _версия_скилла()
    # версия SDK вместо нашей — клиент увидит чужой номер релиза
    if ожидаемая and info.get("version") != ожидаемая:
        errors.append("serverInfo.version = %r, ожидалась %r"
                      % (info.get("version"), ожидаемая))

    if 2 not in ответы:
        errors.append("нет ответа на tools/list")
    else:
        ti = load("tools_impl", MCP_DIR / "tools_impl.py")
        names = sorted(t["name"] for t in ответы[2]["result"]["tools"])
        if names != sorted(ti.EXPECTED_TOOLS):
            errors.append("инструменты по транспорту %s != %s"
                          % (names, sorted(ti.EXPECTED_TOOLS)))

    if 3 not in ответы:
        errors.append("нет ответа на tools/call")
    else:
        res = ответы[3].get("result", {})
        текст = "".join(c.get("text", "") for c in res.get("content", []))
        # движок должен быть достижим из процесса сервера, а не только
        # из теста: битый ИНН — единственный ответ без сети
        if "не проверено" not in текст:
            errors.append("движок недостижим из сервера: %r" % текст[:200])
    return errors


def main():
    ti = load("tools_impl", MCP_DIR / "tools_impl.py")
    cases = {"tools_impl-логика": case_tools_impl(ti),
             "sdk-транспорт": case_sdk(),
             "живой stdio-хендшейк": case_stdio()}
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
