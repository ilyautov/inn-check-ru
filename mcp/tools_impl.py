#!/usr/bin/env python3
"""
tools_impl.py — логика MCP-инструментов inn-check-ru поверх скриптов движка
(scripts/). Только stdlib: вызов через subprocess, разбор JSON-вывода.
Дубликата логики нет — скрипты возвращают свои tier/оговорки/предупреждения,
мы отдаём их как есть.

Отделено от MCP-транспорта (server.py на FastMCP): дымовой тест гоняет этот
модуль без установленного SDK. Каждая функция НИКОГДА не падает traceback'ом —
возвращает JSON {"статус": "не проверено", "причина": ...}.
"""

import functools
import json
import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
if not os.path.isdir(SCRIPTS):
    # установка колесом: движок лежит py-модулями рядом в site-packages
    import importlib.util as _ilu
    _spec = _ilu.find_spec("fetch_counterparty")
    if _spec and _spec.origin:
        SCRIPTS = os.path.dirname(_spec.origin)
# полный сбор по сети может идти десятки секунд (egrul-поллинг)
TIMEOUT = float(os.environ.get("INN_CHECK_MCP_TIMEOUT", "150"))

EXPECTED_TOOLS = (
    "counterparty_fetch",
    "counterparty_fin_scoring",
    "sanctions_check",
    "affiliates_graph",
    "droblenie_check",
    "counterparty_diff",
)


def _не_проверено(причина, инструмент=None):
    out = {"статус": "не проверено", "причина": причина}
    if инструмент:
        out["инструмент"] = инструмент
    return out


def _guarded(fn):
    """Инструмент НИКОГДА не падает traceback'ом в MCP-ответ: любой сбой
    превращается в JSON «не проверено» + причина."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            return _не_проверено(
                "внутренний сбой обёртки (%s)" % type(e).__name__, fn.__name__)
    return wrapper


def run_script(script, args, stdin_text=None, timeout=None):
    """Запуск scripts/<script> и разбор его JSON-stdout.

    Возвращает dict скрипта как есть; любая деградация (таймаут, не-JSON,
    отсутствующий файл) -> {"статус": "не проверено", "причина": ...}.
    """
    path = os.path.join(SCRIPTS, script)
    if not os.path.exists(path):
        return _не_проверено("скрипт движка не найден: %s" % path, script)
    try:
        proc = subprocess.run(
            [sys.executable, path, *args],
            input=stdin_text, capture_output=True, text=True,
            timeout=timeout or TIMEOUT, check=False,
        )
    except subprocess.TimeoutExpired:
        return _не_проверено(
            "движок не уложился в таймаут %.0f с (INN_CHECK_MCP_TIMEOUT)"
            % (timeout or TIMEOUT), script)
    except Exception as e:
        return _не_проверено("сбой запуска движка (%s)" % type(e).__name__, script)
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return _не_проверено(
            "движок вернул не-JSON (exit %s): %s"
            % (proc.returncode, (proc.stderr or proc.stdout)[:300]), script)


def _valid_inn(inn):
    s = str(inn or "").strip()
    return s if s.isdigit() and len(s) in (10, 12) else None


@_guarded
def counterparty_fetch(inn, save=False):
    """Полный сбор fetch_counterparty.py: зелёная зона ФНС + МСП + НПД +
    ЕРКНМ/РНП по кэшу. Без браузерных слоёв (суды/банкротства/ФССП)."""
    inn = _valid_inn(inn)
    if inn is None:
        return _не_проверено("некорректный ИНН (ожидается 10 или 12 цифр)")
    args = [inn] + (["--save"] if save else [])
    return run_script("fetch_counterparty.py", args)


@_guarded
def counterparty_fin_scoring(inn):
    """fetch + финансовый профиль кодом по цепочке (для ИП честно
    «не проверено» — публичной отчётности у ИП нет)."""
    inn = _valid_inn(inn)
    if inn is None:
        return _не_проверено("некорректный ИНН (ожидается 10 или 12 цифр)")
    fetch = run_script("fetch_counterparty.py", [inn])
    if "инн" not in fetch:
        return fetch  # это уже ошибочный JSON движка/обёртки
    return run_script("fin_scoring.py", ["--stdin"],
                      stdin_text=json.dumps(fetch, ensure_ascii=False))


@_guarded
def sanctions_check(inn=None, name=None):
    """Офлайн-сверка по перечням РФМ + OFAC SDN + EU (кэши пользователя)."""
    args = []
    if inn:
        args += ["--inn", str(inn)]
    if name:
        args += ["--name", str(name)]
    if not args:
        return _не_проверено("укажите inn и/или name")
    return run_script("sanctions_check.py", args)


@_guarded
def affiliates_graph(inn):
    """Граф связей через checko API (CHECKO_API_KEY в env сервера); без
    ключа движок честно отвечает «не проверено»."""
    inn = _valid_inn(inn)
    if inn is None:
        return _не_проверено("некорректный ИНН (ожидается 10 или 12 цифр)")
    return run_script("affiliates_graph.py", [inn])


@_guarded
def droblenie_check(inn):
    """Граф + признаки дробления кодом (с предупреждением об одном источнике
    и формулировочной дисциплиной из движка)."""
    inn = _valid_inn(inn)
    if inn is None:
        return _не_проверено("некорректный ИНН (ожидается 10 или 12 цифр)")
    graph = run_script("affiliates_graph.py", [inn])
    if graph.get("статус") != "ок":
        return graph
    return run_script("droblenie_check.py", ["--stdin"],
                      stdin_text=json.dumps(graph, ensure_ascii=False))


@_guarded
def counterparty_diff(inn):
    """Мониторинг: diff двух последних снимков (снимки — counterparty_fetch
    с save=True)."""
    inn = _valid_inn(inn)
    if inn is None:
        return _не_проверено("некорректный ИНН (ожидается 10 или 12 цифр)")
    return run_script("diff_counterparty.py", [inn])
