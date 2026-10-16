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
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
if not os.path.isdir(SCRIPTS):
    # установка колесом: движок лежит py-модулями рядом в site-packages
    import importlib.util as _ilu
    _spec = _ilu.find_spec("fetch_counterparty")
    if _spec and _spec.origin:
        SCRIPTS = os.path.dirname(_spec.origin)
# Use the canonical flat engine loader without changing sys.path/PYTHONPATH.
try:
    from sibling_imports import load_sibling as _load_sibling
except ModuleNotFoundError as _error:
    if _error.name != "sibling_imports":
        raise
    _spec = importlib.util.spec_from_file_location(
        "sibling_imports", os.path.join(SCRIPTS, "sibling_imports.py"))
    _helper = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_helper)
    _load_sibling = sys.modules.setdefault("sibling_imports", _helper).load_sibling

_process_runtime = _load_sibling("process_runtime")
signal = _process_runtime.signal  # compatibility for existing containment fault seams
CREATE_SUSPENDED, WindowsJob = _process_runtime.CREATE_SUSPENDED, _process_runtime.WindowsJob
_ProcessExchange = _process_runtime._ProcessExchange
_ProcessStartError = _process_runtime._ProcessStartError
_start_process = _process_runtime._start_process
_stop_process_tree = _process_runtime._stop_process_tree
_probe_binary_version = _process_runtime._probe_binary_version
_script_output = _process_runtime._script_output
_не_проверено = _process_runtime._не_проверено

# Ключи из настроек плагина (userConfig в .claude-plugin/plugin.json, хранятся в
# защищённом хранилище системы). .mcp.json кладёт их в свои переменные, а не прямо в
# CHECKO_API_KEY: пустое поле плагина затёрло бы ключ, уже заданный в окружении.
КЛЮЧИ_ПЛАГИНА = {"INN_CHECK_PLUGIN_CHECKO_KEY": "CHECKO_API_KEY",
                 "INN_CHECK_PLUGIN_DADATA_KEY": "DADATA_API_KEY"}


def ключи_из_плагина(environ):
    """Непустой ключ из настроек плагина -> переменная, которую читает движок.
    Пустое поле и неподставленный шаблон `${user_config.…}` ключом не считаются,
    ключ из окружения остаётся. Промежуточные переменные убираются всегда, чтобы
    не ехать в дочерние процессы. Возвращает имена заданных ключей, без значений."""
    заданы = []
    for откуда, куда in КЛЮЧИ_ПЛАГИНА.items():
        значение = (environ.pop(откуда, None) or "").strip()
        if значение and not значение.startswith("${"):
            environ[куда] = значение
            заданы.append(куда)
    return заданы


# полный сбор по сети может идти десятки секунд (egrul-поллинг)
TIMEOUT = float(os.environ.get("INN_CHECK_MCP_TIMEOUT", "150"))
CLEANUP_TIMEOUT = _process_runtime.CLEANUP_TIMEOUT
# Only set by the fixed legacy worker, never by environment or public input.
_LEGACY_WORKER = False

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


@functools.lru_cache(maxsize=1)
def _inn_validator():
    """Тот же контроль ФНС, что у CLI, без копии контрольных весов в MCP."""
    spec = importlib.util.spec_from_file_location(
        "inn_check_mcp_validation", os.path.join(SCRIPTS, "fetch_counterparty.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.validate_inn


def run_script(script, args, stdin_text=None, timeout=None, сырой=False, env=None):
    """Compatibility adapter; transport protocol remains MCP-owned."""
    if _LEGACY_WORKER:
        request = {'kind': 'script', 'script': script, 'args': args,
                   'stdin_text': stdin_text, 'timeout': timeout, 'raw': сырой, 'env': env}
        print(json.dumps(request, ensure_ascii=False, allow_nan=False), file=_LEGACY_OUTPUT, flush=True)
        return json.loads(sys.stdin.readline())
    return _process_runtime.run_script(script, args, stdin_text=stdin_text,
        timeout=timeout or TIMEOUT, сырой=сырой,
        env=dict(os.environ, **env) if env else None, scripts_dir=SCRIPTS,
        _start=_start_process, _exchange=_ProcessExchange, _stop=_stop_process_tree)


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
    # The CLI recognises option tokens before parsing values. Validate before
    # dispatch so no value can select refresh or another command mode.
    if inn and _valid_inn(inn) is None:
        return _не_проверено("некорректный ИНН (ожидается 10 или 12 цифр)")
    if name and (not isinstance(name, str) or name.lstrip().startswith('-')):
        return _не_проверено("name: ожидается имя, а не параметр команды")
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
    def reject(reason, code):
        out = _не_проверено(reason, "counterparty_batch")
        out["код"] = code
        return out

    if isinstance(inns, str):
        inns = [x for x in re.split(r"[\s,;]+", inns) if x]
    if not isinstance(inns, (list, tuple)):
        return reject("inns: ожидается список строк ИНН (или строка через запятую)",
                      "invalid_arguments")
    if len(inns) > BATCH_LIMIT:
        return reject("лимит %d ИНН за вызов, передано %d — разбейте батч"
                      % (BATCH_LIMIT, len(inns)), "batch_limit")
    if not inns:
        return reject("пустой список ИНН", "invalid_arguments")
    validated = []
    for index, value in enumerate(inns):
        # Пути, флаги, пустые элементы и нестроковые значения — ошибка API,
        # а не список аргументов batch_check. Не фильтруем вход до подсчёта.
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{10}|[0-9]{12}", value.strip()):
            return reject("inns[%d]: ожидается строка из 10 или 12 цифр" % index,
                          "invalid_arguments")
        inn = _inn_validator()(value.strip())
        if inn is None:
            return reject("inns[%d]: некорректный ИНН (неверное контрольное число ФНС)" % index,
                          "invalid_inn")
        validated.append(inn)
    return run_script("batch_check.py",
                      ["--json", "--тихо", "--профиль", str(profile or "нейтрально"), "--stdin"],
                      stdin_text="\n".join(validated) + "\n",
                      timeout=_batch_timeout(len(validated)))


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


# Workflow tools use a separate asynchronous subprocess owner. The core module
# stays stdlib-only; SDK types and JSON-schema declarations live in server.py.
WORKFLOW_TOOLS = (
    'counterparty_run_start', 'counterparty_run_read', 'counterparty_run_resume',
    'counterparty_run_manual', 'counterparty_run_refresh', 'counterparty_run_render',
    'counterparty_run_export', 'counterparty_run_snapshot', 'counterparty_doctor',
    'counterparty_probe', 'counterparty_cache_status', 'counterparty_cache_plan',
    'counterparty_events_list', 'counterparty_events_claim', 'counterparty_events_ack',
    'counterparty_document_extract', 'counterparty_document_select',
    'counterparty_compare', 'counterparty_run_replay',
)
EXPECTED_TOOLS += WORKFLOW_TOOLS
_WORKFLOW_FIELDS = {
    'counterparty_run_start': ({'inn'}, {'profile', 'mode'}),
    'counterparty_run_read': ({'run_id'}, {'revision'}),
    'counterparty_run_resume': ({'run_id'}, set()),
    'counterparty_run_manual': ({'run_id', 'blocks', 'expected_revision', 'operation_id'}, set()),
    'counterparty_run_refresh': ({'run_id', 'source_ids', 'expected_revision', 'operation_id'}, set()),
    'counterparty_run_render': ({'run_id'}, {'format', 'revision'}),
    'counterparty_run_export': ({'run_id'}, {'format', 'revision'}),
    'counterparty_run_snapshot': ({'run_id'}, {'revision'}),
    'counterparty_doctor': (set(), {'profile'}),
    'counterparty_probe': (set(), {'profile'}),
    'counterparty_cache_status': ({'profile', 'now'}, set()),
    'counterparty_cache_plan': ({'profile', 'now'}, set()),
    'counterparty_events_list': (set(), {'state', 'limit'}),
    'counterparty_events_claim': ({'consumer_id'}, {'limit', 'lease_seconds'}),
    'counterparty_events_ack': ({'event_ids', 'consumer_id'}, set()),
    'counterparty_document_extract': (set(), {'text', 'run_id', 'artifact_id', 'ocr'}),
    'counterparty_document_select': ({'candidates', 'role'}, {'candidate_id'}),
    'counterparty_compare': ({'run_ids', 'as_of'}, set()),
    'counterparty_run_replay': ({'run_id', 'mode', 'cutoff'}, set()),
}


def workflow_error(code='invalid_input'):
    """Fixed public messages; never echo exception text, argv, paths or stderr."""
    messages = {'invalid_input': 'Входные данные не соответствуют контракту.',
                'execution_error': 'Не удалось выполнить операцию проверки.',
                'timeout': 'Операция превысила допустимое время; проверьте сохранённую проверку.',
                'cleanup_unconfirmed': 'Завершение дерева процессов не подтверждено.'}
    return {'schema_version': 1, 'status': 'error', 'run_id': None,
            'revision': None, 'data': None,
            'error': {'code': code, 'message': messages[code],
                      'retryable': code in {'execution_error', 'timeout'}},
            'warnings': [], 'pending_actions': []}


def validate_workflow_arguments(name, arguments):
    """Validate raw JSON before FastMCP's permissive conversions or process launch."""
    import uuid
    if name not in _WORKFLOW_FIELDS or not isinstance(arguments, dict):
        raise ValueError('invalid_input')
    required, optional = _WORKFLOW_FIELDS[name]
    if not required <= arguments.keys() or arguments.keys() - required - optional:
        raise ValueError('invalid_input')
    for field in ('run_id', 'operation_id', 'artifact_id'):
        if field in arguments:
            value = arguments[field]
            if name == 'counterparty_document_extract' and value is None:
                continue
            if not isinstance(value, str) or str(uuid.UUID(value)) != value or uuid.UUID(value).version != 4:
                raise ValueError('invalid_input')
    for field in ('expected_revision', 'revision'):
        value = arguments.get(field)
        if (field in arguments and not (field == 'revision' and value is None) and
                (type(value) is not int or value < 1)):
            raise ValueError('invalid_input')
    for field in ('inn', 'profile', 'mode', 'now', 'format'):
        if field in arguments and not isinstance(arguments[field], str):
            raise ValueError('invalid_input')
    if 'blocks' in arguments:
        blocks = arguments['blocks']
        if not isinstance(blocks, dict) or any(not isinstance(k, str) or not isinstance(v, dict)
                                               for k, v in blocks.items()):
            raise ValueError('invalid_input')
    if 'source_ids' in arguments:
        values = arguments['source_ids']
        if not isinstance(values, list) or not values or any(not isinstance(v, str) for v in values):
            raise ValueError('invalid_input')
    if 'format' in arguments:
        allowed = {'markdown', 'json', 'docx'} if name.endswith('_export') else {'markdown', 'json'}
        if arguments['format'] not in allowed:
            raise ValueError('invalid_input')
    if 'consumer_id' in arguments:
        value = arguments['consumer_id']
        if not isinstance(value, str) or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', value) is None:
            raise ValueError('invalid_input')
    for field, maximum in (('limit', 50), ('lease_seconds', 3600)):
        if field in arguments and (type(arguments[field]) is not int or not 1 <= arguments[field] <= maximum):
            raise ValueError('invalid_input')
    if 'state' in arguments and arguments['state'] not in ('pending', 'delivered', 'acknowledged', 'dead_letter', 'all'):
        raise ValueError('invalid_input')
    if 'event_ids' in arguments:
        values = arguments['event_ids']
        if (not isinstance(values, list) or not 1 <= len(values) <= 50 or
                any(not isinstance(v, str) or re.fullmatch('[0-9a-f]{64}', v) is None for v in values)):
            raise ValueError('invalid_input')
    if name == 'counterparty_document_extract':
        text, run_id, artifact_id = (arguments.get(k) for k in ('text', 'run_id', 'artifact_id'))
        if text is not None:
            if not isinstance(text, str) or run_id is not None or artifact_id is not None:
                raise ValueError('invalid_input')
        elif run_id is None or artifact_id is None:
            raise ValueError('invalid_input')
        if 'ocr' in arguments and type(arguments['ocr']) is not bool:
            raise ValueError('invalid_input')
    if name == 'counterparty_document_select':
        candidates = arguments['candidates']
        if (not isinstance(candidates, list) or len(candidates) > 10000 or
                any(not isinstance(item, dict) for item in candidates) or
                arguments['role'] not in ('supplier', 'buyer', 'own_company', 'bank', 'signatory', 'unknown', 'other') or
                (arguments.get('candidate_id') is not None and not isinstance(arguments['candidate_id'], str))):
            raise ValueError('invalid_input')
    if name == 'counterparty_compare':
        values = arguments['run_ids']
        if not isinstance(values, list) or not 2 <= len(values) <= 5:
            raise ValueError('invalid_input')
        for value in values:
            if not isinstance(value, str) or str(uuid.UUID(value)) != value or uuid.UUID(value).version != 4:
                raise ValueError('invalid_input')
        if not isinstance(arguments['as_of'], str):
            raise ValueError('invalid_input')
    if (name == 'counterparty_run_replay' and
            (arguments['mode'] not in ('original', 'current_rules') or not isinstance(arguments['cutoff'], str))):
        raise ValueError('invalid_input')
    serialized = json.dumps(arguments, ensure_ascii=False, allow_nan=False)
    if len(serialized.encode('utf-8')) > 32 * 1024 * 1024:
        raise ValueError('invalid_input')


def _workflow_command():
    # This module is part of both the checkout and installed MCP package. Its
    # location and the engine route are derived locally, never from tool input.
    return [sys.executable, '-B', '-X', 'utf8', os.path.abspath(__file__), '--workflow-worker']


async def workflow_call(name, arguments):
    try:
        validate_workflow_arguments(name, arguments)
    except Exception:
        return workflow_error()
    if name == 'counterparty_document_extract':
        return await _document_extract(arguments)
    result = await _supervised_call(name, arguments, _workflow_command(), TIMEOUT, workflow_error,
                                    workflow=True)
    if name in {'counterparty_doctor', 'counterparty_probe'} and result.get('status') != 'error':
        result = await _complete_doctor_versions(result)
    return result


# Fixed API dispatch: no caller-controlled script, argv, root or executable.
_LEGACY_FUNCTIONS = {name: globals()[name] for name in EXPECTED_TOOLS if name not in WORKFLOW_TOOLS}
_LEGACY_FIELDS = {
    'counterparty_fetch': ({'inn'}, {'inn': str, 'save': bool}),
    'counterparty_fin_scoring': ({'inn'}, {'inn': str}),
    'sanctions_check': (set(), {'inn': str, 'name': str}),
    'affiliates_graph': ({'inn'}, {'inn': str}),
    'droblenie_check': ({'inn'}, {'inn': str}),
    'counterparty_diff': ({'inn'}, {'inn': str}),
    'retro_verdict': ({'inn', 'date'}, {'inn': str, 'date': str, 'profile': str}),
    'counterparty_verdict': ({'inn'}, {'inn': str, 'profile': str}),
    'counterparty_batch': ({'inns'}, {'inns': list, 'profile': str}),
    'access_check': (set(), {}),
    'industry_benchmarks': ({'inn'}, {'inn': str}),
    'paper_vat_signs': ({'inn'}, {'inn': str, 'subject': str, 'amount': str}),
    'due_diligence_dossier': ({'inn'}, {'inn': str, 'profile': str, 'subject': str,
                                       'amount': str, 'evidence_dir': str, 'timestamp': bool}),
    'extract_inns': ({'text'}, {'text': str, 'only_new': bool}),
}


def validate_legacy_arguments(name, arguments):
    if name not in _LEGACY_FUNCTIONS or not isinstance(arguments, dict):
        raise ValueError('invalid_input')
    required, fields = _LEGACY_FIELDS[name]
    if not required <= arguments.keys() or arguments.keys() - fields.keys():
        raise ValueError('invalid_input')
    if any(type(value) is not fields[key] for key, value in arguments.items()):
        raise ValueError('invalid_input')
    if 'inns' in arguments and any(not isinstance(value, str) for value in arguments['inns']):
        raise ValueError('invalid_input')
    if len(json.dumps(arguments, ensure_ascii=False, allow_nan=False).encode('utf-8')) > 32 * 1024 * 1024:
        raise ValueError('invalid_input')


def _legacy_error(code='invalid_input'):
    return _не_проверено(workflow_error(code)['error']['message'])


def _legacy_command():
    return [sys.executable, '-B', '-X', 'utf8', os.path.abspath(__file__), '--legacy-worker']


async def legacy_call(name, arguments):
    try:
        validate_legacy_arguments(name, arguments)
    except Exception:
        return _legacy_error()
    # Keep time for the two serial engine stages used by legacy wrappers, and
    # preserve the batch wave budget. Each run_script retains its own deadline.
    timeout = 2 * TIMEOUT + 2 * CLEANUP_TIMEOUT
    if name == 'counterparty_batch':
        timeout = max(timeout, _batch_timeout(min(len(arguments['inns']), BATCH_LIMIT)) + CLEANUP_TIMEOUT)
    return await _legacy_supervised_call(name, arguments, timeout)


async def _supervised_call(name, arguments, command, timeout, error, *, workflow=False):
    """Own the engine tree; only pipe IO runs in the exchange thread.

    Synchronous bounded finally cleanup has no cancellation checkpoints, so
    repeated cancellation cannot skip POSIX group / Windows Job termination.
    """
    import asyncio
    body = json.dumps({'name': name, 'arguments': arguments}, ensure_ascii=False, allow_nan=False)
    proc = exchange = None
    result = cleanup = None
    try:
        proc = _start_process(command, stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, encoding='utf-8',
                              env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
        exchange = _ProcessExchange(proc, body)
        exchange.thread.start()
        deadline = time.monotonic() + timeout
        while not exchange.done.is_set():
            if time.monotonic() >= deadline:
                result = error('timeout')
                break
            await asyncio.sleep(0.02)
        if result is None:
            if exchange.error is not None:
                raise exchange.error
            stdout, _stderr = exchange.output
            result = json.loads(stdout)
            if not isinstance(result, dict) or (workflow and
                    (result.get('schema_version') != 1 or 'status' not in result)):
                result = error('execution_error')
    except Exception:
        result = error('execution_error')
    finally:
        if proc is not None:
            cleanup = _stop_process_tree(proc, exchange=exchange)
            if cleanup:
                sys.stderr.write('mcp: process cleanup unconfirmed\n')
    return error('cleanup_unconfirmed') if cleanup else result


# The private pipe protocol accepts only scripts used by the fixed legacy API.
_LEGACY_SCRIPTS = frozenset({'fetch_counterparty.py', 'fin_scoring.py', 'sanctions_check.py',
    'affiliates_graph.py', 'droblenie_check.py', 'diff_counterparty.py', 'retro_verdict.py',
    'batch_check.py', 'check_access.py', 'benchmarks.py', 'paper_vat.py', 'dossier.py', 'extract_inn.py'})


class _LegacyChannel:
    """Pipe IO only: the async parent launches and owns every engine process."""
    def __init__(self, proc, body):
        import queue
        self.messages = queue.Queue()
        self.responses = queue.Queue()
        self.done = threading.Event()
        self.error = None
        # Drain stderr without retaining user data or permitting pipe deadlock.
        def drain_stderr():
            while proc.stderr.read(8192):
                pass
        self.stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
        proc.stderr_thread = self.stderr_thread

        def exchange():
            try:
                proc.stdin.write(body + '\n')
                proc.stdin.flush()
                for line in proc.stdout:
                    message = json.loads(line)
                    self.messages.put(message)
                    if message.get('kind') == 'result':
                        proc.wait()  # Reap normal exit before reporting pipe completion.
                        break
                    response = self.responses.get()
                    if response is None:
                        break
                    proc.stdin.write(json.dumps(response, ensure_ascii=False, allow_nan=False) + '\n')
                    proc.stdin.flush()
            except BaseException as exc:
                self.error = exc
            finally:
                self.done.set()
        self.thread = threading.Thread(target=exchange, daemon=True, name='inn-check-legacy-pipes')
        self.stderr_thread.start()
        self.thread.start()


async def _legacy_supervised_call(name, arguments, timeout):
    import asyncio
    import queue
    proc = channel = None
    result = cleanup = None
    try:
        proc = _start_process(_legacy_command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, encoding='utf-8',
                              env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
        channel = _LegacyChannel(proc, json.dumps({'name': name, 'arguments': arguments}, ensure_ascii=False))
        deadline = time.monotonic() + timeout
        while result is None:
            if time.monotonic() >= deadline:
                result = _legacy_error('timeout')
                break
            try:
                message = channel.messages.get_nowait()
            except queue.Empty:
                if channel.done.is_set():
                    raise RuntimeError('worker ended without result')
                await asyncio.sleep(.02)
                continue
            if message.get('kind') == 'result':
                result = message['result']
                if not isinstance(result, dict):
                    raise ValueError('invalid result')
                while not channel.done.is_set():
                    if time.monotonic() >= deadline:
                        raise TimeoutError('worker exit timeout')
                    await asyncio.sleep(.02)
            else:
                # Await owns the inner tree. Its finally finishes before outer
                # cleanup, on cancellation as well as normal completion.
                response = await _legacy_script_call(message, deadline)
                channel.responses.put(response)
    except Exception:
        result = _legacy_error('execution_error')
    finally:
        if channel is not None:
            channel.responses.put(None)
        if proc is not None:
            cleanup = _stop_process_tree(proc, exchange=channel)
            if cleanup:
                sys.stderr.write('mcp: process cleanup unconfirmed\n')
    return _legacy_error('cleanup_unconfirmed') if cleanup else result


async def _legacy_script_call(request, deadline):
    """Private fixed-script request, owned by the async transport coroutine."""
    import asyncio
    if (not isinstance(request, dict) or request.get('kind') != 'script' or
            set(request) != {'kind', 'script', 'args', 'stdin_text', 'timeout', 'raw', 'env'} or
            request['script'] not in _LEGACY_SCRIPTS or
            not isinstance(request['args'], list) or
            any(not isinstance(arg, str) for arg in request['args']) or
            (request['stdin_text'] is not None and not isinstance(request['stdin_text'], str)) or
            type(request['raw']) is not bool or
            request['env'] not in (None, {'INN_CHECK_TSA': 'да'})):
        raise ValueError('invalid internal request')
    requested_timeout = request['timeout']
    if requested_timeout is not None and (type(requested_timeout) not in (int, float) or
            not 0 < requested_timeout <= max(TIMEOUT, _batch_timeout(BATCH_LIMIT))):
        raise ValueError('invalid timeout')
    script = request['script']
    path = os.path.join(SCRIPTS, script)
    if not os.path.isfile(path):
        return _не_проверено('скрипт движка не найден: %s' % path, script)
    proc = exchange = None
    reason = cleanup = None
    stdout = stderr = None
    timeout = requested_timeout or TIMEOUT
    deadline = min(deadline, time.monotonic() + timeout)
    try:
        proc = _start_process([sys.executable, '-X', 'utf8', path, *request['args']],
                              stdin=subprocess.PIPE if request['stdin_text'] is not None else subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8',
                              env=dict(os.environ, **request['env']) if request['env'] else None)
        exchange = _ProcessExchange(proc, request['stdin_text'])
        exchange.thread.start()
        while not exchange.done.is_set():
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(proc.args, timeout)
            await asyncio.sleep(.02)
        if exchange.error is not None:
            raise exchange.error
        stdout, stderr = exchange.output
    except subprocess.TimeoutExpired:
        reason = 'движок не уложился в таймаут %.0f с (INN_CHECK_MCP_TIMEOUT)' % timeout
    except _ProcessStartError as exc:
        reason = 'сбой запуска движка (%s)' % exc
    except Exception as exc:
        reason = ('сбой обмена с движком' if proc is not None else 'сбой запуска движка')
        reason += ' (%s)' % type(exc).__name__
    finally:
        if proc is not None:
            cleanup = _stop_process_tree(proc, exchange=exchange)
    if cleanup:
        sys.stderr.write('mcp: process cleanup unconfirmed\n')
    return _script_output(script, proc, stdout, stderr, reason, cleanup, request['raw'])


def _legacy_worker():
    """One typed API call; each script stage is supervised by the parent."""
    import contextlib
    global _LEGACY_WORKER, _LEGACY_OUTPUT
    _LEGACY_WORKER = True
    _LEGACY_OUTPUT = sys.stdout
    sys.dont_write_bytecode = True
    try:
        request = json.loads(sys.stdin.buffer.readline(32 * 1024 * 1024 + 1024))
        name, arguments = request['name'], request['arguments']
        validate_legacy_arguments(name, arguments)
        with contextlib.redirect_stdout(sys.stderr):
            result = _LEGACY_FUNCTIONS[name](**arguments)
    except Exception:
        result = _legacy_error('execution_error')
    print(json.dumps({'kind': 'result', 'result': result}, ensure_ascii=False, allow_nan=False), flush=True)


async def counterparty_run_start(inn, profile='нейтрально', mode='полный'):
    return await workflow_call('counterparty_run_start', locals())


async def counterparty_run_read(run_id, revision=None):
    return await workflow_call('counterparty_run_read', locals())


async def counterparty_run_resume(run_id):
    return await workflow_call('counterparty_run_resume', locals())


async def counterparty_run_manual(run_id, blocks, expected_revision, operation_id):
    return await workflow_call('counterparty_run_manual', locals())


async def counterparty_run_refresh(run_id, source_ids, expected_revision, operation_id):
    return await workflow_call('counterparty_run_refresh', locals())


async def counterparty_run_render(run_id, format='markdown', revision=None):
    return await workflow_call('counterparty_run_render', locals())


async def counterparty_run_export(run_id, format='markdown', revision=None):
    return await workflow_call('counterparty_run_export', locals())


async def counterparty_run_snapshot(run_id, revision=None):
    return await workflow_call('counterparty_run_snapshot', locals())


async def counterparty_doctor(profile='нейтрально'):
    return await workflow_call('counterparty_doctor', locals())


async def counterparty_probe(profile='нейтрально'):
    return await workflow_call('counterparty_probe', locals())


async def counterparty_cache_status(profile, now):
    return await workflow_call('counterparty_cache_status', locals())


async def counterparty_cache_plan(profile, now):
    return await workflow_call('counterparty_cache_plan', locals())


async def counterparty_events_list(state='pending', limit=50):
    return await workflow_call('counterparty_events_list', locals())


async def counterparty_events_claim(consumer_id, limit=50, lease_seconds=300):
    return await workflow_call('counterparty_events_claim', locals())


async def counterparty_events_ack(event_ids, consumer_id):
    return await workflow_call('counterparty_events_ack', locals())


# These adapters remain separate from all legacy wrappers and their pipe protocol.
async def counterparty_document_extract(text=None, run_id=None, artifact_id=None, ocr=False):
    return await workflow_call('counterparty_document_extract', locals())


async def counterparty_document_select(candidates, role, candidate_id=None):
    return await workflow_call('counterparty_document_select', locals())


async def counterparty_compare(run_ids, as_of):
    return await workflow_call('counterparty_compare', locals())


async def counterparty_run_replay(run_id, mode, cutoff):
    return await workflow_call('counterparty_run_replay', locals())


async def _complete_doctor_versions(result):
    # Commands are rebuilt from trusted host configuration, never worker output
    # or public arguments. The async MCP owner also owns each binary process.
    _feature_modules()
    import workflow_doctor
    rows = workflow_doctor._binary_configurations()
    for row in rows.values():
        if row['present']:
            row.update(await _probe_binary_version(row['version_command']))
            row['ready'] = row['version_ready']
            if row['ready']:
                row.pop('reason_code', None)
    data = result['data']
    data['optional']['binaries'] = rows
    data['sections']['optional']['binaries'] = rows
    return _feature_public(result)


DOCUMENT_TIMEOUT = 60.0
DOCUMENT_MAX_BYTES = 20 * 1024 * 1024
DOCUMENT_MAX_RESULT = 24 * 1024 * 1024


def _feature_modules():
    # Only locally installed engine modules. No public module/path argument.
    if SCRIPTS not in sys.path:
        sys.path.insert(0, SCRIPTS)
    import redaction
    import run_workflow
    return run_workflow, redaction


def _feature_public(result):
    _, redaction = _feature_modules()
    secrets = tuple(value for key, value in os.environ.items() if value and
                    any(token in key.upper() for token in ('KEY', 'TOKEN', 'SECRET', 'PASSWORD')))
    return redaction.redact(result, secrets=secrets)


def _document_envelope(result):
    workflow, _ = _feature_modules()
    if result['status'] == 'error':
        code = result.get('reason_code') or result['limitations'][0]['reason_code']
        return workflow.envelope(status='error', data=result, error={
            'code': code, 'message': 'Документ или выбор стороны не обработан.', 'retryable': False})
    return workflow.envelope(status=result['status'], data=result)


def _document_source(arguments):
    workflow, _ = _feature_modules()
    if arguments.get('text') is not None:
        return arguments['text'].encode('utf-8'), 'input.txt'
    # Managed IDs only: registry membership and stored digest are checked by store.
    run_id, artifact_id = arguments['run_id'], arguments['artifact_id']
    data = workflow.store.read_artifact(run_id, artifact_id)
    artifact = next(row for row in workflow.store.list_artifacts(run_id)
                    if row['artifact_id'] == artifact_id)
    suffix = {'markdown': '.txt', 'json': '.json', 'snapshot': '.json', 'docx': '.docx'}[artifact['format']]
    return data, 'input' + suffix


def _document_command():
    return [sys.executable, '-B', '-X', 'utf8', os.path.join(SCRIPTS, 'document_input.py'), '--stdin-worker']


async def _document_extract(arguments):
    """Directly own parser + OCR descendants; never nest an extraction supervisor.

    Bounded temporary stdout keeps pipe buffering independent of document size.
    The fixed parser also enforces its own 24 MiB serialized-result bound.
    """
    import asyncio
    import base64
    import tempfile
    deadline = time.monotonic() + DOCUMENT_TIMEOUT
    proc = exchange = None
    cleanup = None
    workflow, _ = _feature_modules()
    try:
        data, filename = await asyncio.wait_for(asyncio.to_thread(_document_source, arguments),
                                                timeout=max(0, deadline - time.monotonic()))
        if len(data) > DOCUMENT_MAX_BYTES:
            return workflow.failure(workflow.WorkflowError('too_large', 'Документ превышает 20 МиБ.'))
        body = json.dumps({'schema_version': 1, 'data_base64': base64.b64encode(data).decode('ascii'),
                           'filename': filename, 'ocr': arguments.get('ocr', False)})
        # The parent owns the temporary root: killing a parser cannot strand
        # document bytes from the worker's interrupted TemporaryDirectory.
        with tempfile.TemporaryDirectory(prefix='inn-document-mcp-') as directory, tempfile.TemporaryFile() as output:
            try:
                if time.monotonic() >= deadline:
                    return workflow_error('timeout')
                proc = _start_process(_document_command(), stdin=subprocess.PIPE, stdout=output,
                                      stderr=subprocess.DEVNULL, text=True, encoding='utf-8',
                                      env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1',
                                               TMPDIR=directory, TEMP=directory, TMP=directory))
                exchange = _ProcessExchange(proc, body)
                exchange.thread.start()
                result = None
                while not exchange.done.is_set():
                    if time.monotonic() >= deadline:
                        result = workflow_error('timeout')
                        break
                    if os.fstat(output.fileno()).st_size > DOCUMENT_MAX_RESULT + 1:
                        result = workflow.failure(workflow.WorkflowError('output_too_large', 'Результат документа превышает лимит.'))
                        break
                    await asyncio.sleep(0.02)
                if result is None:
                    if exchange.error is not None:
                        raise exchange.error
                    if proc.returncode:
                        raise RuntimeError('document worker failed')
                    output.seek(0)
                    raw = output.read(DOCUMENT_MAX_RESULT + 2)
                    if len(raw) > DOCUMENT_MAX_RESULT + 1:
                        result = workflow.failure(workflow.WorkflowError('output_too_large', 'Результат документа превышает лимит.'))
                    else:
                        result = _document_envelope(json.loads(raw))
            finally:
                if proc is not None:
                    cleanup = _stop_process_tree(proc, exchange=exchange)
            return _feature_public(workflow_error('cleanup_unconfirmed') if cleanup else result)
    except asyncio.TimeoutError:
        return workflow_error('timeout')
    except Exception as error:
        return _feature_public(workflow.failure(error))


def _workflow_worker():
    """Fixed dispatch to the existing engine; one JSON request on stdin."""
    import contextlib
    sys.dont_write_bytecode = True
    sys.path.insert(0, SCRIPTS)
    import run_workflow as workflow
    try:
        raw = sys.stdin.buffer.read(32 * 1024 * 1024 + 1024)
        request = json.loads(raw)
        name, arguments = request['name'], request['arguments']
        validate_workflow_arguments(name, arguments)
        functions = {'counterparty_run_start': workflow.start,
                     'counterparty_run_read': lambda run_id, revision=None: (workflow.resume(run_id) if revision is None
                        else workflow._result(workflow._read(run_id, revision=revision))),
                     'counterparty_run_resume': workflow.resume,
                     'counterparty_run_manual': workflow.import_manual,
                     'counterparty_run_refresh': workflow.refresh,
                     'counterparty_run_render': workflow.render,
                     'counterparty_run_export': workflow.export,
                     'counterparty_run_snapshot': workflow.save_snapshot}
        with contextlib.redirect_stdout(sys.stderr):
            if name in functions:
                result = functions[name](**arguments)
            elif name == 'counterparty_compare':
                import compare_counterparties
                try:
                    result = workflow.envelope(data=compare_counterparties.compare(**arguments))
                except compare_counterparties.ComparisonError as error:
                    raise workflow.WorkflowError(error.code, error.message) from None
            elif name == 'counterparty_run_replay':
                import historical_replay
                try:
                    data = historical_replay.replay(**arguments)
                except historical_replay.ReplayError as error:
                    raise workflow.WorkflowError(error.code, error.message) from None
                result = workflow.envelope(status='ok' if data['point_in_time_status'] == 'verified' else 'partial',
                                           run_id=arguments['run_id'], revision=data['source_revision'], data=data)
            elif name == 'counterparty_document_select':
                import document_input
                result = _document_envelope(document_input.select(**arguments))
            elif name.startswith('counterparty_events_'):
                import monitor_events
                function = {'counterparty_events_list': monitor_events.list_events,
                            'counterparty_events_claim': monitor_events.claim_events,
                            'counterparty_events_ack': monitor_events.ack_events}[name]
                try:
                    result = workflow.envelope(data=function(**arguments))
                except monitor_events.EventError as error:
                    result = workflow.failure(workflow.WorkflowError(error.code, error.message))
            elif name in {'counterparty_doctor', 'counterparty_probe'}:
                import workflow_doctor
                workflow._profile(arguments.get('profile', 'нейтрально'))
                result = workflow.envelope(data=workflow_doctor.inspect(
                    arguments.get('profile', 'нейтрально'), probe=name == 'counterparty_probe', probe_versions=False))
            else:
                import cache_policy
                function = cache_policy.status if name == 'counterparty_cache_status' else cache_policy.plan
                result = workflow.envelope(data=function(**arguments))
    except Exception as error:
        result = workflow.failure(error)
    if locals().get('name') in {'counterparty_document_select', 'counterparty_compare', 'counterparty_run_replay'}:
        result = _feature_public(result)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == '__main__':
    if sys.argv[1:] == ['--workflow-worker']:
        _workflow_worker()
    elif sys.argv[1:] == ['--legacy-worker']:
        _legacy_worker()
    else:
        raise SystemExit(2)
