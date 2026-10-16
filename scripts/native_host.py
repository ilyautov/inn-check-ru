#!/usr/bin/env python3
"""
native_host.py — хост native messaging для браузерного расширения inn-check-ru
(extension/). Chrome запускает его сам (манифест хоста ставит
install_native_host.py) и говорит с ним кадрами: 4 байта длины в порядке байтов
машины + JSON в UTF-8. Спека — docs/superpowers/specs/2026-09-28-browser-extension-design.md.

Команды (поле «команда»), и больше ничего:
    run_read       {run_id} -> workflow envelope сохранённой проверки
    run_manual     {run_id, expected_revision, operation_id, blocks}
                     -> новая ревизия с ручным исходом; тот же envelope
    проверить       {инн, профиль?}  -> светофор быстрой проверки (batch_check.py)
    ручной_блок     {операция, инн, блок, url, дата, итог, поля?, найдено?,
                     параметры?, причина?} -> <каталог>/<инн>/ручные/<блок>.json
    доказательство  {операция, инн, блок, url, дата, png, найдено?}
                     -> снимок в пакет <каталог>/<инн>/пакет (evidence_pack.добавить)

Граница доверия: данные страницы недоверенные. Путей в сообщении нет — каталог
задан в ~/.config/inn-check-ru/host.json («каталог») или по умолчанию
~/inn-check-ru; имена файлов создаёт хост. ИНН — с контрольной суммой, блок —
браузерный источник, URL — только сайт этого источника, операция — id для
повторов без дублей. Движок вызывается функциями и фиксированным argv без shell.
stdout — только протокол, диагностика — stderr.
"""

import base64
import json
import os
import re
import struct
import sys
import tempfile
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import manual_block
import profiles

МАКС_ВХОД = 16 * 1024 * 1024        # свой лимит на кадр от Chrome (у Chrome — 64 МиБ)
МАКС_ОТВЕТ = 1024 * 1024            # лимит Chrome на кадр от хоста
МАКС_PNG = 10 * 1024 * 1024
PNG_ПОДПИСЬ = b"\x89PNG\r\n\x1a\n"
ОПЕРАЦИЯ = re.compile(r"^[A-Za-z0-9-]{8,64}$")
# Сайты браузерных источников: URL ручного блока и снимка — только отсюда
# (поддомены — тоже). Расширение видит вкладку, поэтому здесь, а не в движке,
# сайт сверяется строго.
ХОСТЫ = {"фссп": ("fssp.gov.ru",), "суды": ("kad.arbitr.ru",),
         "залоги": ("reestr-zalogov.ru",)}
ПОЛЯ_КОМАНД = {
    "run_read": {"команда", "run_id"},
    "run_manual": {"команда", "run_id", "expected_revision", "operation_id", "blocks"},
    "проверить": {"команда", "инн", "профиль"},
    "ручной_блок": {"команда", "операция", "инн", "блок", "url", "дата", "итог", "поля",
                    "найдено", "параметры", "причина", "текст"},
    "доказательство": {"команда", "операция", "инн", "блок", "url", "дата", "png",
                       "найдено"},
}


class Отказ(Exception):
    """Сообщение не принято: причина уходит в ответ, хост продолжает работу."""


# --- протокол ---------------------------------------------------------------

def читать_кадр(поток):
    """-> dict | None (EOF). Отказ — длина сверх лимита, обрыв, не JSON-объект."""
    голова = поток.read(4)
    if not голова:
        return None
    if len(голова) < 4:
        raise Отказ("кадр оборван на длине")
    (длина,) = struct.unpack("=I", голова)
    if длина > МАКС_ВХОД:
        # тело не читаем: канал рассинхронизирован, дальше только выход
        raise Отказ("кадр %d байт больше лимита %d" % (длина, МАКС_ВХОД))
    тело = b""
    while len(тело) < длина:
        кусок = поток.read(длина - len(тело))
        if not кусок:
            raise Отказ("кадр оборван: %d из %d байт" % (len(тело), длина))
        тело += кусок
    try:
        сообщение = json.loads(тело.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise Отказ("кадр не JSON в UTF-8: %s" % type(e).__name__)
    if not isinstance(сообщение, dict):
        raise Отказ("кадр — не JSON-объект")
    return сообщение


def писать_кадр(поток, ответ):
    тело = json.dumps(ответ, ensure_ascii=False).encode("utf-8")
    if len(тело) > МАКС_ОТВЕТ:
        тело = json.dumps({"ok": False, "ошибка": "ответ хоста больше 1 МБ — лимит Chrome"},
                          ensure_ascii=False).encode("utf-8")
    поток.write(struct.pack("=I", len(тело)) + тело)
    поток.flush()


def _native_workflow(ответ):
    """Native transport view only; never mutate the materialized run/receipt.

    Small envelopes keep their existing full data. Large ones expose only the
    popup binding fields, with explicit omissions and bounded UTF-8-safe text.
    The same boundary applies to failures (including huge unknown input keys).
    """
    полный = dict(ответ, native_projection={
        "version": 1, "kind": "full", "complete": True, "omitted": [], "truncated": []})
    if len(json.dumps(полный, ensure_ascii=False).encode("utf-8")) <= МАКС_ОТВЕТ:
        return полный

    сокращено = []

    def объект(value):
        return value if isinstance(value, dict) else {}

    def текст(value, field, limit=128):
        if value is None:
            return None
        if not isinstance(value, str):
            сокращено.append(field)
            return None
        if len(value) > limit:
            сокращено.append(field)
            return value[:limit] + "… [сокращено]"
        return value

    data = объект(ответ.get("data"))
    fetch = объект(data.get("fetch"))
    metadata = объект(data.get("metadata"))
    decision = объект(fetch.get("_decision"))
    name = объект(fetch.get("егрюл")).get("название") or fetch.get("название")
    outcomes = объект(metadata.get("manual_outcomes"))
    actions = ответ.get("pending_actions") or []
    warnings = ответ.get("warnings") or []
    if not isinstance(actions, list):
        сокращено.append("pending_actions")
        actions = []
    if not isinstance(warnings, list):
        сокращено.append("warnings")
        warnings = []
    if len(actions) > 128:
        сокращено.append("pending_actions")
    if len(warnings) > 16:
        сокращено.append("warnings")
    projection = {
        "version": 1, "kind": "binding", "complete": False,
        "omitted": ["fetch_source_details", "fin", "assessment_details",
                    "metadata_evidence_refs", "decision_details", "pending_action_details",
                    "warning_details", "nonbrowser_manual_outcomes"],
        "truncated": сокращено,
        "pending_actions_total": len(actions), "warnings_total": len(warnings),
        "full_payload_access": "CLI/MCP",
    }
    projected_data = None
    if ответ.get("data") is not None:
        projected_data = {"fetch": {
            "инн": текст(fetch.get("инн"), "subject_inn", 12),
            "егрюл": {"название": текст(name, "subject_name", 1024)},
            "_decision": {
                "decision_status": текст(decision.get("decision_status"), "decision_status"),
                "can_recommend": (decision.get("can_recommend")
                                  if type(decision.get("can_recommend")) is bool else None),
            }},
            "assessment": {"светофор": текст(объект(data.get("assessment")).get("светофор"),
                                              "assessment_status")},
            "metadata": {
                "profile": текст(metadata.get("profile"), "profile"),
                "workflow_status": текст(metadata.get("workflow_status"), "workflow_status"),
                "manual_outcomes": {sid: текст(outcomes[sid], "manual_outcome." + sid, 64)
                                    for sid in ХОСТЫ if sid in outcomes},
            }}
    error = объект(ответ.get("error"))
    bounded = {
        "schema_version": 1, "status": ответ["status"], "run_id": ответ.get("run_id"),
        "revision": ответ.get("revision"), "data": projected_data,
        "error": ({"code": текст(error.get("code"), "error.code", 64),
                   "message": текст(error.get("message"), "error.message", 1024),
                   "retryable": error.get("retryable") is True} if error else None),
        "warnings": [{"code": текст(объект(w).get("code"), "warning.code", 64),
                      "message": текст(объект(w).get("message"), "warning.message", 512)}
                     for w in warnings[:16]],
        "pending_actions": [{key: текст(объект(action).get(key), "pending_action." + key)
                             for key in ("source_id", "kind", "reason_code")}
                            for action in actions[:128]],
        "native_projection": projection,
    }
    # Defense in depth for future schema changes: still a workflow envelope,
    # with an explicit projection error rather than the legacy frame fallback.
    if len(json.dumps(bounded, ensure_ascii=False).encode("utf-8")) > МАКС_ОТВЕТ:
        import run_workflow
        fallback = run_workflow.failure(run_workflow.WorkflowError(
            "native_projection_too_large", "Ответ для привязки не помещается; перечитайте проверку через CLI/MCP."),
            run_id=ответ.get("run_id"), revision=ответ.get("revision"))
        fallback["native_projection"] = {"version": 1, "kind": "binding", "complete": False,
                                         "omitted": ["data"], "truncated": ["response"]}
        return fallback
    return bounded


# --- настройка и файлы ------------------------------------------------------

def каталог():
    """Корень данных хоста: host.json «каталог» или ~/inn-check-ru. Создаётся."""
    конфиг = Path(os.path.expanduser("~/.config/inn-check-ru/host.json"))
    корень = Path(os.path.expanduser("~/inn-check-ru"))
    if конфиг.is_file():
        try:
            указан = json.loads(конфиг.read_text(encoding="utf-8")).get("каталог")
        except (OSError, ValueError, AttributeError):
            указан = None
        if isinstance(указан, str) and указан.strip():
            корень = Path(os.path.expanduser(указан.strip()))
    корень.mkdir(parents=True, exist_ok=True)
    return корень.resolve()


def _папка(корень, *части):
    """Подпапка внутри корня, без symlink на пути: имена — ИНН и константы."""
    путь = корень
    for ч in части:
        путь = путь / ч
        if путь.is_symlink():
            raise Отказ("на пути данных symlink: %s" % путь.name)
        путь.mkdir(exist_ok=True)
    if корень not in путь.resolve().parents and путь.resolve() != корень:
        raise Отказ("путь вне каталога хоста")
    return путь


def _записать_атомарно(папка, имя, данные):
    fd, tmp = tempfile.mkstemp(dir=папка, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(данные)
        цель = папка / имя
        if цель.is_symlink():
            raise Отказ("файл назначения — symlink")
        os.replace(tmp, цель)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return цель


class Журнал:
    """Операции с id: повтор той же операции отдаёт прежний ответ, а не дубль."""

    def __init__(self, корень):
        self.путь = корень / ".операции.json"

    def _все(self):
        try:
            данные = json.loads(self.путь.read_text(encoding="utf-8"))
            return данные if isinstance(данные, dict) else {}
        except (OSError, ValueError):
            return {}

    def найти(self, операция):
        return self._все().get(операция)

    def запомнить(self, операция, ответ):
        все = self._все()
        все[операция] = ответ
        if len(все) > 500:                      # хранится хвост, не вся история
            все = dict(list(все.items())[-500:])
        _записать_атомарно(self.путь.parent, self.путь.name,
                           json.dumps(все, ensure_ascii=False).encode("utf-8"))


# --- проверки входа ---------------------------------------------------------

def _строго(сообщение):
    команда = сообщение.get("команда")
    if not isinstance(команда, str) or команда not in ПОЛЯ_КОМАНД:
        raise Отказ("команда %r не поддерживается: %s" % (команда, ", ".join(ПОЛЯ_КОМАНД)))
    лишние = set(сообщение) - ПОЛЯ_КОМАНД[команда]
    if лишние:
        raise Отказ("лишние поля: %s" % ", ".join(sorted(лишние)))
    if команда in {"run_read", "run_manual"}:
        if set(сообщение) != ПОЛЯ_КОМАНД[команда]:
            raise Отказ("нужны все поля команды сохранённой проверки")
        import run_store
        run_store._id(сообщение["run_id"])
        if команда == "run_manual":
            run_store._revision(сообщение["expected_revision"])
            run_store._id(сообщение["operation_id"], operation=True)
            blocks = сообщение["blocks"]
            if not isinstance(blocks, dict) or not blocks:
                raise Отказ("blocks: непустой объект ручных блоков")
            for sid, block in blocks.items():
                if not isinstance(block, dict):
                    raise Отказ("ручной блок должен быть объектом")
                completed = block.get("outcome") == "completed" or (
                    block.get("outcome") is None and block.get("статус") == "проверено")
                if completed and type(block.get("pagination_complete")) is not bool:
                    raise Отказ("завершённый шаг требует явный pagination_complete: true или false")
                _инн(block.get("инн"))
                _сайт_источника(sid, block.get("url"))
        return команда
    for k, v in сообщение.items():
        if k in ("поля", "png"):
            continue
        if v is not None and not isinstance(v, str):
            raise Отказ("поле %s — ожидается строка" % k)
    return команда


def _инн(v):
    инн = str(v or "").strip()
    if profiles._первый_инн(инн) != инн:
        raise Отказ("ИНН %r: нужно 10/12 цифр с верной контрольной суммой" % инн)
    return инн


def _операция(v):
    if not isinstance(v, str) or not ОПЕРАЦИЯ.match(v):
        raise Отказ("операция: id из 8–64 латинских букв, цифр и дефисов")
    return v


def _сайт_источника(блок, url):
    if блок not in ХОСТЫ:
        raise Отказ("блок %r: расширение знает только %s" % (блок, ", ".join(ХОСТЫ)))
    url = str(url or "")
    хост = ""
    # обратную косую, пробелы и управляющие символы браузер читает иначе, чем
    # Python (в «https://evil.example<\>.kad.arbitr.ru/» домен — evil.example): такие
    # адреса не принимаются вовсе (ревью Codex)
    if not re.search(r"[\\\s\x00-\x1f\x7f]", url):
        try:
            части = urllib.parse.urlsplit(url)
            if (части.scheme == "https" and not части.username and not части.password
                    and части.port in (None, 443)):
                хост = (части.hostname or "").lower()
        except ValueError:
            хост = ""
    if not any(хост == х or хост.endswith("." + х) for х in ХОСТЫ[блок]):
        raise Отказ("url не с сайта источника %s (%s)" % (блок, ", ".join(ХОСТЫ[блок])))


# --- команды ----------------------------------------------------------------

def проверить(сообщение, запуск=None):
    инн = _инн(сообщение.get("инн"))
    профиль = сообщение.get("профиль") or "нейтрально"
    if профиль not in profiles.load()["профили"]:
        raise Отказ("неизвестный профиль %r" % профиль)
    if запуск is None:
        try:                                  # установка колесом: пакет MCP-обёртки
            from inn_check_ru_mcp import tools_impl
        except ImportError:                   # репозиторий: mcp/ рядом со scripts/
            sys.path.insert(0, str(HERE.parent / "mcp"))
            import tools_impl
        запуск = tools_impl.run_script
    # быстрая проверка одной строкой батча: argv фиксирован, ИНН и профиль проверены
    out = запуск("batch_check.py", ["--json", "--тихо", "--профиль", профиль, инн])
    строки = out.get("результаты") if isinstance(out, dict) else None
    if not isinstance(строки, list) or not строки:
        return {"ok": False, "ошибка": (out or {}).get("причина") or "движок не ответил",
                "инн": инн}
    return {"ok": True, "инн": инн, "профиль": профиль, "результат": строки[0]}


def ручной_блок(сообщение, корень, журнал):
    операция = _операция(сообщение.get("операция"))
    прежний = журнал.найти(операция)
    if прежний is not None:
        return dict(прежний, повтор=True)
    инн = _инн(сообщение.get("инн"))
    блок = сообщение.get("блок")
    _сайт_источника(блок, сообщение.get("url"))
    поля = сообщение.get("поля") or {}
    if not isinstance(поля, dict) or not all(
            isinstance(k, str) and isinstance(v, (str, int, float, bool))
            for k, v in поля.items()):
        raise Отказ("поля: объект {имя: строка|число|да-нет}")
    поля = {k: ("да" if v is True else "нет" if v is False else v) for k, v in поля.items()}
    try:
        данные = manual_block.собрать(блок, инн, сообщение.get("url"), сообщение.get("дата"),
                                      сообщение.get("итог"), поля, сообщение.get("найдено"),
                                      сообщение.get("параметры"), сообщение.get("причина"),
                                      текст=сообщение.get("текст"))
    except ValueError as e:
        raise Отказ(str(e))
    папка = _папка(корень, инн, "ручные")
    путь = _записать_атомарно(папка, "%s.json" % блок, json.dumps(
        данные, ensure_ascii=False, indent=2).encode("utf-8"))
    fetch = {"инн": инн, блок: данные,
             "_доступность": {блок: {"состояние": "не проверено",
                                     "причина": "не покрыто: браузерный источник"}}}
    состояние, причина, _ = profiles._состояние_блока(fetch, блок)
    ответ = {"ok": True, "записано": str(путь), "состояние": состояние, "причина": причина}
    if данные.get("уведомлений") is not None and данные.get("найдено"):
        ответ["найдено"] = данные["найдено"]      # итог разбора текста — показать в окне
    журнал.запомнить(операция, ответ)
    return ответ


def доказательство(сообщение, корень, журнал, добавить=None):
    операция = _операция(сообщение.get("операция"))
    прежний = журнал.найти(операция)
    if прежний is not None:
        return dict(прежний, повтор=True)
    инн = _инн(сообщение.get("инн"))
    if len(инн) != 10:
        # у ИП на странице реестра — его ФИО: снимок не делаем, и хост проверяет
        # это сам, а не только расширение (ревью Kimi)
        raise Отказ("снимок только для юрлица (10 цифр): у ИП на странице ФИО")
    блок = сообщение.get("блок")
    _сайт_источника(блок, сообщение.get("url"))
    png = сообщение.get("png")
    if not isinstance(png, str) or len(png) > МАКС_PNG * 4 // 3 + 4:
        raise Отказ("png: строка base64 до %d МБ" % (МАКС_PNG // 1024 // 1024))
    try:
        данные = base64.b64decode(png.split(",", 1)[-1], validate=True)
    except ValueError:
        raise Отказ("png: не base64")
    if not данные.startswith(PNG_ПОДПИСЬ):
        raise Отказ("png: не PNG")
    пакет = корень / инн / "пакет"
    # symlink на любом звене пути (ИНН, пакет) — отказ: пакет только внутри
    # каталога хоста (ревью Codex: ссылка на месте <ИНН> уводила запись наружу)
    if (корень / инн).is_symlink() or пакет.is_symlink() or \
            корень not in пакет.resolve().parents:
        raise Отказ("на пути к пакету symlink или выход из каталога хоста")
    if not (пакет / "manifest.json").is_file():
        raise Отказ("пакета доказательств нет: соберите его командой "
                    "`python3 scripts/fetch_counterparty.py %s --пакет %s`" % (инн, пакет))
    if добавить is None:
        import evidence_pack
        добавить = evidence_pack.добавить
    # Отметка «в процессе» — до добавления: если хост упадёт после записи
    # версии, повтор той же операции не добавит второе вложение (ревью Codex)
    журнал.запомнить(операция, {"ok": False, "в_процессе": True,
                                "ошибка": "операция уже выполнялась и не завершилась — "
                                          "проверьте пакет: evidence_pack.py --проверить %s"
                                          % пакет})
    временная = _папка(корень, ".снимки")
    файл = _записать_атомарно(временная, "%s_%s.png" % (операция, блок), данные)
    try:
        итог = добавить(str(пакет), str(файл), блок, сообщение.get("url"),
                        сообщение.get("дата"), параметры="ИНН %s" % инн,
                        найдено=manual_block._текст(сообщение.get("найдено"), "найдено"))
    except ValueError as e:
        raise Отказ("пакет: %s" % e)
    finally:
        файл.unlink(missing_ok=True)
    штамп = str((итог or {}).get("штамп") or "")
    ответ = {"ok": True, "пакет": str(пакет), "версия": (итог or {}).get("версия"),
             "штамп": штамп or None,
             "статус": ("сохранено, штамп не поставлен" if штамп.startswith("не поставлен")
                        else "сохранено")}
    журнал.запомнить(операция, ответ)
    return ответ


def обработать(сообщение, корень=None, запуск=None, добавить=None):
    """Одно сообщение -> ответ. Никогда не бросает: отказ и сбой — в ответе."""
    try:
        команда = _строго(сообщение)
        if команда in {"run_read", "run_manual"}:
            import run_workflow
            # run_store resolves INN_CHECK_RUNS_DIR or its local default. Never
            # reuse the legacy output directory or accept a root from a message.
            if команда == "run_read":
                return _native_workflow(run_workflow.resume(сообщение["run_id"]))
            return _native_workflow(run_workflow.import_manual(сообщение["run_id"], сообщение["blocks"],
                expected_revision=сообщение["expected_revision"], operation_id=сообщение["operation_id"]))
        if команда == "проверить":
            return проверить(сообщение, запуск)
        корень = корень or каталог()
        журнал = Журнал(корень)
        if команда == "ручной_блок":
            return ручной_блок(сообщение, корень, журнал)
        return доказательство(сообщение, корень, журнал, добавить)
    except Отказ as e:
        if сообщение.get("команда") in ("run_read", "run_manual"):
            import run_workflow
            return _native_workflow(run_workflow.failure(run_workflow.WorkflowError("invalid_input", str(e))))
        return {"ok": False, "ошибка": str(e)}
    except Exception as e:               # хост не падает traceback'ом в канал
        if сообщение.get("команда") in ("run_read", "run_manual"):
            import run_workflow
            return _native_workflow(run_workflow.failure(e))
        sys.stderr.write("native_host: %s: %s\n" % (type(e).__name__, e))
        return {"ok": False, "ошибка": "сбой хоста (%s)" % type(e).__name__}


def main():
    if sys.platform == "win32":
        # кадры двоичные: 0A/0D/1A в длине не должны превращаться в перевод строки
        # или EOF. CPython 3 уже ставит O_BINARY сам — это страховка.
        import msvcrt
        for f in (sys.stdin, sys.stdout):
            msvcrt.setmode(f.fileno(), os.O_BINARY)
    вход, выход = sys.stdin.buffer, sys.stdout.buffer
    while True:
        try:
            сообщение = читать_кадр(вход)
        except Отказ as e:
            писать_кадр(выход, {"ok": False, "ошибка": str(e)})
            return 1                     # поток рассинхронизирован — выходим
        if сообщение is None:
            return 0
        писать_кадр(выход, обработать(сообщение))


if __name__ == "__main__":
    sys.exit(main())
