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
import re
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
    "counterparty_verdict",
    "counterparty_batch",
    "access_check",
    "industry_benchmarks",
    "paper_vat_signs",
    "due_diligence_dossier",
    "extract_inns",
    "retro_verdict",
)

# Батч: 3 ИНН собираются одновременно (batch_check.py), волна ~15 с в quick-режиме.
BATCH_LIMIT = 50
BATCH_WAVE_SECONDS = 20.0


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


def run_script(script, args, stdin_text=None, timeout=None, сырой=False, env=None):
    """Запуск scripts/<script> и разбор его JSON-stdout.

    Возвращает dict скрипта как есть; любая деградация (таймаут, не-JSON,
    отсутствующий файл) -> {"статус": "не проверено", "причина": ...}.

    `сырой=True` — вернуть stdout строкой без разбора (досье отдаёт Markdown,
    а не JSON). Деградация и там остаётся словарём, чтобы вызывающий отличал
    документ от отказа по типу значения.
    """
    path = os.path.join(SCRIPTS, script)
    if not os.path.exists(path):
        return _не_проверено("скрипт движка не найден: %s" % path, script)
    try:
        proc = subprocess.run(
            [sys.executable, path, *args],
            input=stdin_text, capture_output=True, text=True,
            timeout=timeout or TIMEOUT, check=False,
            env=dict(os.environ, **env) if env else None,
        )
    except subprocess.TimeoutExpired:
        return _не_проверено(
            "движок не уложился в таймаут %.0f с (INN_CHECK_MCP_TIMEOUT)"
            % (timeout or TIMEOUT), script)
    except Exception as e:
        return _не_проверено("сбой запуска движка (%s)" % type(e).__name__, script)
    if сырой:
        if proc.returncode != 0 or not (proc.stdout or "").strip():
            return _не_проверено(
                "движок не отдал документ (exit %s): %s"
                % (proc.returncode, (proc.stderr or "")[:300]), script)
        return proc.stdout
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


@_guarded
def retro_verdict(inn, date, profile=""):
    """Вердикт на дату в прошлом по сохранённому тогда снимку."""
    inn = _valid_inn(inn)
    if inn is None:
        return _не_проверено("некорректный ИНН (ожидается 10 или 12 цифр)")
    args = [inn, "--дата", str(date or "")]
    if profile:
        args += ["--профиль", profile]
    return run_script("retro_verdict.py", args)


def _batch_timeout(n):
    """Батч идёт волнами по 3 ИНН (batch_check.py --потоков 3): даём волне
    BATCH_WAVE_SECONDS, но не меньше общего TIMEOUT."""
    return max(TIMEOUT, BATCH_WAVE_SECONDS * ((n + 2) // 3))


def _строки(out):
    """Список результатов батча или None, если пришла деградация обёртки."""
    рез = out.get("результаты") if isinstance(out, dict) else None
    return рез if isinstance(рез, list) else None


@_guarded
def counterparty_verdict(inn, profile="нейтрально"):
    """Главный инструмент: сбор + финансы + резолвер профиля одной командой.
    Возвращает светофор, поднявшие его сигналы, рекомендацию и что осталось
    «не проверено» — модели не нужно сводить вердикт из сырого JSON."""
    inn = _valid_inn(inn)
    if inn is None:
        return _не_проверено("некорректный ИНН (ожидается 10 или 12 цифр)")
    out = run_script("batch_check.py",
                     ["--json", "--тихо", "--профиль", str(profile or "нейтрально"),
                      "--режим", "полный", inn])
    строки = _строки(out)
    if строки is None:
        return out
    if not строки:
        return _не_проверено("батч вернул пустой результат", "counterparty_verdict")
    строка = dict(строки[0])
    строка["профиль"] = out.get("профиль")
    строка["быстрый_режим"] = out.get("быстрый_режим")
    return строка


@_guarded
def counterparty_batch(inns, profile="нейтрально"):
    """Батч по списку ИНН (лимит BATCH_LIMIT за вызов): по каждому — быстрая
    проверка и светофор профиля, сортировка 🔴 → не проверено → 🟡 → 🟢."""
    if isinstance(inns, str):
        inns = [x for x in re.split(r"[\s,;]+", inns) if x]
    if not isinstance(inns, (list, tuple)):
        return _не_проверено("inns: ожидается список ИНН (или строка через запятую)")
    inns = [str(i).strip() for i in inns if str(i).strip()]
    if not inns:
        return _не_проверено("пустой список ИНН")
    if len(inns) > BATCH_LIMIT:
        return _не_проверено("лимит %d ИНН за вызов, передано %d — разбейте батч"
                             % (BATCH_LIMIT, len(inns)))
    return run_script("batch_check.py",
                      ["--json", "--тихо", "--профиль", str(profile or "нейтрально"),
                       *inns],
                      timeout=_batch_timeout(len(inns)))


@_guarded
def access_check():
    """Таблица доступности источников с текущей сети (probe, кэш на сутки):
    что реально отвечает, где нужен РФ-IP, где корень УЦ Минцифры."""
    return run_script("check_access.py", ["--json"])


def _сбор(inn, режим="всё", пакет=None, штамп=False):
    """Общий шаг инструментов волны 3: полный сбор движком.

    Отдельной функцией, потому что все трое ниже начинают одинаково, и
    ошибочный JSON движка должен доходить до модели как есть, а не
    прятаться за собственным «не проверено» обёртки.
    """
    args = [inn, "--режим", str(режим)]
    if not пакет:
        return run_script("fetch_counterparty.py", args)
    args += ["--пакет", str(пакет)]
    if штамп:
        return run_script("fetch_counterparty.py", args, env={"INN_CHECK_TSA": "да"})
    return run_script("fetch_counterparty.py", args)


def _сбор_или_ошибка(inn, режим="всё", пакет=None, штамп=False):
    inn = _valid_inn(inn)
    if inn is None:
        return None, _не_проверено("некорректный ИНН (ожидается 10 или 12 цифр)")
    fetch = _сбор(inn, режим, пакет=пакет, штамп=штамп)
    if "инн" not in fetch:
        return None, fetch
    return fetch, None


@_guarded
def industry_benchmarks(inn):
    """Положение компании относительно отраслевой нормы из дампов ФНС.

    Норма — перцентили выручки, налоговой нагрузки, ССЧ и доли УСН по группе
    (ОКВЭД2, регион, размер). Группы меньше порога наблюдений не публикуются,
    у каждого показателя свой счётчик, а для «среднего» и «крупного» ответ
    несёт предупреждение о смещённой выборке.
    """
    fetch, ошибка = _сбор_или_ошибка(inn)
    if ошибка is not None:
        return ошибка
    return run_script("benchmarks.py", ["--stdin"],
                      stdin_text=json.dumps(fetch, ensure_ascii=False))


@_guarded
def paper_vat_signs(inn, subject=None, amount=None):
    """Признаки «технической» компании, из-за которой снимают вычет по НДС.

    Язык фактов, не налоговый вывод: что видно в открытых данных, чего не
    видно и что осталось непроверенным. Вердикта о вычете не выносит.
    `subject` — предмет сделки (сверяется с ОКВЭД), `amount` — сумма в рублях.
    """
    fetch, ошибка = _сбор_или_ошибка(inn)
    if ошибка is not None:
        return ошибка
    args = ["--stdin"]
    if subject:
        args += ["--предмет", str(subject)]
    if amount not in (None, ""):
        args += ["--сумма", str(amount)]
    return run_script("paper_vat.py", args,
                      stdin_text=json.dumps(fetch, ensure_ascii=False))


@_guarded
def due_diligence_dossier(inn, profile="нейтрально", subject=None, amount=None,
                          evidence_dir=None, timestamp=False):
    """Досье должной осмотрительности в Markdown — документ на дату проверки.

    Фиксирует, что было видно в открытых источниках, что осталось
    непроверенным и почему. Заключением не является и ценен только тогда,
    когда составлен ДО сделки — это написано в самом документе. DOCX здесь
    не отдаётся: инструмент возвращает текст, файл собирает dossier.py.

    `evidence_dir` — новый или пустой каталог на машине пользователя: туда
    ляжет пакет доказательств (сырые ответы источников + манифест SHA-256),
    а досье получит раздел о его целостности. `timestamp` — ещё и штамп
    времени RFC 3161 (на публичный TSA уходит только хеш манифеста).
    """
    if timestamp and not evidence_dir:
        return _не_проверено("штамп времени ставится на пакет доказательств: "
                             "укажите evidence_dir (новый или пустой каталог)")
    # Непригодный каталог движок отклоняет ДО сбора: {"ошибка": ...} без «инн»
    # доходит до модели как есть через _сбор_или_ошибка.
    fetch, ошибка = _сбор_или_ошибка(inn, пакет=evidence_dir or None,
                                     штамп=bool(timestamp))
    if ошибка is not None:
        return ошибка
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8",
                                     delete=False) as fh:
        json.dump(fetch, fh, ensure_ascii=False)
        путь = fh.name
    try:
        # С пакетом досье читает fetch.json пакета: тот же сбор, а байтовое
        # сравнение с пересериализованной копией дало бы ложное «разные проверки».
        args = (["--пакет", str(evidence_dir)] if evidence_dir else ["--fetch", путь])
        args += ["--профиль", str(profile or "нейтрально"), "--markdown"]
        if subject:
            args += ["--предмет", str(subject)]
        if amount not in (None, ""):
            args += ["--сумма", str(amount)]
        текст = run_script("dossier.py", args, сырой=True)
    finally:
        try:
            os.unlink(путь)
        except OSError:
            pass
    if isinstance(текст, dict):
        return текст
    ответ = {"статус": "ок", "инн": fetch.get("инн"), "формат": "markdown",
             "документ": текст}
    if evidence_dir:
        ответ["пакет"] = str(evidence_dir)
    return ответ


@_guarded
def extract_inns(text, only_new=False):
    """Вынуть ИНН из текста документа: счёта, договора, письма, выгрузки.

    Мусор режется контрольным числом ФНС: номера счетов, телефоны,
    расчётные счета и КПП отсеиваются с причиной. Каждый найденный ИНН —
    с контекстом и пометкой «новый» либо «уже проверялся» по кэшу снимков.
    Сеть не трогается: это разбор текста, а не проверка.
    """
    if not isinstance(text, str) or not text.strip():
        return _не_проверено("text: ожидается непустой текст документа")
    args = ["--stdin", "--json"] + (["--только-новые"] if only_new else [])
    return run_script("extract_inn.py", args, stdin_text=text)
