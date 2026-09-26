#!/usr/bin/env python3
"""
run_fetch_eval.py — офлайн-eval движка fetch_counterparty.py (волна 1, §3 спека).

1. Парсеры (чистые функции raw -> (данные, _доступность)) гоняются против реальных
   сырых ответов eval/fixtures/net/<id>_<инн>.raw.json и сверяются с
   <id>_<инн>.expected.json (данные — точное равенство, состояние и причина).
2. Дрейф схемы: из сырой записи удаляется каждое поле контракта по очереди —
   парсер обязан вернуть «не проверено» с причиной «схема: …», никогда «ok».
3. Инварианты трёх состояний: «ok» никогда без данных, «пусто»/«не проверено»
   всегда с данными null.
4. run_source офлайн (фейковые fetcher'ы): канарейка «провал»/«ok», probe-кэш,
   фильтр профиля, источники «требует: браузер».
5. _итог_проверки: правило «проверка состоялась».
6. Волна 2, §1: фазы сбора, режимы quick/полный/всё и ранний выход по deal-killer'у
   (включая то, что в профиле «нейтрально» он не срабатывает никогда), параллельный
   сбор со своим opener на поток, потокобезопасность кэша канареек, реестр
   дисквалифицированных лиц (контекст из «егрюл», встроенная самопроверка).

Сети НЕТ: _http_get подменяется на исключение — любой сетевой вызов = FAIL.
Чистый stdlib, гоняется в CI.
"""

import copy
import datetime as _dt
import importlib.util
import json
import os
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "eval" / "fixtures" / "net"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check(errors, cond, msg):
    if not cond:
        errors.append(msg)


class NetworkTouched(Exception):
    pass


def _no_network(*args, **kwargs):
    raise NetworkTouched("eval не ходит в сеть")


def _drop_path(node, path):
    """Удаляет лист по пути «a.b» / «a.записи[].поле» (молча, если пути нет)."""
    head, _, rest = path.partition(".")
    if head.endswith("[]"):
        seq = node.get(head[:-2]) if isinstance(node, dict) else None
        for item in seq if isinstance(seq, list) else []:
            _drop_path(item, rest)
        return
    if not isinstance(node, dict):
        return
    if not rest:
        node.pop(head, None)
        return
    _drop_path(node.get(head), rest)


def _без_волатильных(данные, пути):
    """Копия данных без полей, зависящих от сегодняшней даты (например «действует»)."""
    if not пути:
        return данные
    копия = copy.deepcopy(данные)
    for path in пути:
        _drop_path(копия, path)
    return копия


def _fixture_pairs():
    for raw_path in sorted(FIXTURES.glob("*.raw.json")):
        stem = raw_path.name[:-len(".raw.json")]
        source_id, inn = stem.rsplit("_", 1)
        expected_path = FIXTURES / (stem + ".expected.json")
        yield source_id, inn, raw_path, expected_path


def case_parsers(fc):
    errors = []
    n = 0
    for source_id, inn, raw_path, expected_path in _fixture_pairs():
        n += 1
        label = raw_path.name
        parser = fc.PARSERS.get(source_id)
        if parser is None:
            errors.append("%s: нет парсера для источника %r" % (label, source_id))
            continue
        if not expected_path.exists():
            errors.append("%s: нет expected-файла %s" % (label, expected_path.name))
            continue
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
        expected = json.loads(expected_path.read_text(encoding="utf-8"))
        data, av = parser(raw, inn)
        пропустить = expected.get("пропустить_поля") or []
        data = _без_волатильных(data, пропустить)
        expected["данные"] = _без_волатильных(expected["данные"], пропустить)
        check(errors, av.get("состояние") == expected["_доступность"]["состояние"],
              "%s: состояние %r != %r" % (label, av.get("состояние"),
                                          expected["_доступность"]["состояние"]))
        check(errors, av.get("причина") == expected["_доступность"]["причина"],
              "%s: причина %r != %r" % (label, av.get("причина"),
                                        expected["_доступность"]["причина"]))
        if data != expected["данные"]:
            diff_keys = []
            if isinstance(data, dict) and isinstance(expected["данные"], dict):
                for k in sorted(set(data) | set(expected["данные"])):
                    if data.get(k) != expected["данные"].get(k):
                        diff_keys.append("%s: %r != %r" % (
                            k, data.get(k), expected["данные"].get(k)))
            errors.append("%s: данные расходятся с expected%s" % (
                label, ("\n      " + "\n      ".join(diff_keys)) if diff_keys
                else " (%r vs %r)" % (data, expected["данные"])))
        # полный словарь _доступность — фиксированная форма §1.1
        for key in ("состояние", "причина", "канарейка", "tier", "требует", "дата"):
            check(errors, key in av, "%s: в _доступность нет ключа %r" % (label, key))
    check(errors, n >= 7, "фикстур меньше семи (%d)" % n)
    return errors


def case_schema_drift(fc):
    """Удаление любого поля контракта -> «не проверено» с «схема:»."""
    errors = []
    tested = 0
    for source_id, inn, raw_path, _ in _fixture_pairs():
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
        record = fc.RAW_RECORD[source_id](raw, inn)
        if record is None:
            continue  # пустой ответ — контракт проверять не на чём (для этого канарейка)
        contract = fc.contract_for(source_id, inn)
        check(errors, contract, "%s: пустой контракт у сетевого источника" % raw_path.name)
        for field in contract:
            check(errors, field in record,
                  "%s: поле контракта %r отсутствует в реальной записи — контракт "
                  "расходится с живым ответом" % (raw_path.name, field))
            mutated = copy.deepcopy(raw)
            rec = fc.RAW_RECORD[source_id](mutated, inn)
            rec.pop(field, None)
            data, av = fc.PARSERS[source_id](mutated, inn)
            tested += 1
            check(errors, av.get("состояние") == "не проверено",
                  "%s без %r: состояние %r, ожидалось «не проверено»"
                  % (raw_path.name, field, av.get("состояние")))
            check(errors, str(av.get("причина") or "").startswith("схема:"),
                  "%s без %r: причина %r не начинается с «схема:»"
                  % (raw_path.name, field, av.get("причина")))
            check(errors, field in str(av.get("причина") or ""),
                  "%s без %r: причина не называет поле" % (raw_path.name, field))
            check(errors, data is None,
                  "%s без %r: данные должны быть null" % (raw_path.name, field))
    check(errors, tested >= 10, "проверено слишком мало полей контракта (%d)" % tested)
    return errors


def case_state_invariants(fc):
    errors = []
    for source_id, inn, raw_path, _ in _fixture_pairs():
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
        data, av = fc.PARSERS[source_id](raw, inn)
        st = av.get("состояние")
        check(errors, st in ("ok", "пусто", "не проверено"),
              "%s: неизвестное состояние %r" % (raw_path.name, st))
        if st == "ok":
            check(errors, isinstance(data, dict) and data,
                  "%s: «ok» без данных" % raw_path.name)
        else:
            check(errors, data is None,
                  "%s: %r с данными не-null" % (raw_path.name, st))
            check(errors, av.get("причина"), "%s: %r без причины" % (raw_path.name, st))
    # не-JSON / пустой ответ никогда не «ok»
    for source_id in fc.PARSERS:
        for junk in (None, {}, [], "<html>", {"ERRORS": {"pbSearchCaptcha": ["x"]}}):
            data, av = fc.PARSERS[source_id](junk, "7707083893")
            check(errors, av.get("состояние") != "ok" and data is None,
                  "%s: мусор %r дал %r" % (source_id, junk, av.get("состояние")))
    return errors


def case_run_source(fc):
    """run_source офлайн: канарейка, probe-кэш, профиль, браузерные источники."""
    errors = []
    orig_fetchers = dict(fc.FETCHERS)
    orig_cache_loader = fc._load_access_cache
    fc._load_access_cache = lambda: None
    fc._CANARY_CACHE.clear()
    try:
        canary_inn = fc.SOURCES["мсп"]["канарейка"]["инн"]
        calls = []

        def fake_all_empty(opener, inn):
            calls.append(inn)
            return None, fc._av("мсп", "пусто", "нет записи")

        fc.FETCHERS["мсп"] = fake_all_empty
        data, av = fc.run_source("мсп", "7707083893")
        check(errors, av["состояние"] == "не проверено",
              "канарейка пуста: состояние %r" % av["состояние"])
        check(errors, av["причина"] == "схема: канарейка пуста — парсер сломан",
              "канарейка пуста: причина %r" % av["причина"])
        check(errors, av["канарейка"] == "провал", "канарейка пуста: %r" % av["канарейка"])
        check(errors, data is None, "канарейка пуста: данные не null")
        check(errors, calls == ["7707083893", canary_inn],
              "порядок вызовов %r" % calls)
        # кэш канарейки в процессе: второй запрос не ходит на канареечный ИНН повторно
        fc.run_source("мсп", "7707083894")
        check(errors, calls.count(canary_inn) == 1, "канарейка вызвана дважды: %r" % calls)

        fc._CANARY_CACHE.clear()

        def fake_canary_ok(opener, inn):
            if inn == canary_inn:
                return ({"статус_мсп": "в реестре", "категория": "микропредприятие"},
                        fc._av("мсп", "ok"))
            return None, fc._av("мсп", "пусто", "нет записи")

        fc.FETCHERS["мсп"] = fake_canary_ok
        data, av = fc.run_source("мсп", "7707083893")
        check(errors, av["состояние"] == "пусто" and av["канарейка"] == "ok",
              "канарейка ok: %r/%r" % (av["состояние"], av["канарейка"]))
        # канарейка на самом канареечном ИНН не запускается (пусто — и есть ответ)
        fc._CANARY_CACHE.clear()
        fc.FETCHERS["мсп"] = fake_all_empty
        data, av = fc.run_source("мсп", canary_inn)
        check(errors, av["состояние"] == "пусто" and av["канарейка"] == "не запускалась",
              "канарейка на канареечном ИНН: %r/%r" % (av["состояние"], av["канарейка"]))

        # исключение сетевого слоя -> «не проверено» с префиксом, не traceback
        def fake_boom(opener, inn):
            raise fc.SourceUnavailable("сеть: таймаут")

        fc.FETCHERS["мсп"] = fake_boom
        data, av = fc.run_source("мсп", "7707083893")
        check(errors, av["состояние"] == "не проверено" and av["причина"] == "сеть: таймаут",
              "исключение fetcher'а: %r" % av)

        def fake_crash(opener, inn):
            raise KeyError("x")

        fc.FETCHERS["мсп"] = fake_crash
        data, av = fc.run_source("мсп", "7707083893")
        check(errors, av["состояние"] == "не проверено"
              and str(av["причина"]).startswith("схема:"),
              "неожиданное исключение: %r" % av)

        # probe-кэш: гео/tls/dns -> «probe: …», fetcher не вызывается
        marker = {"маркер": "fetcher вызван"}
        fc.FETCHERS["егрюл"] = lambda opener, inn: (dict(marker), fc._av("егрюл", "ok"))
        for probe_state in ("гео", "tls", "dns"):
            fc._load_access_cache = lambda st=probe_state: {
                "источники": {"егрюл": {"состояние": st, "http": None}}}
            data, av = fc.run_source("егрюл", "7707083893")
            check(errors, data is None and av["состояние"] == "не проверено"
                  and av["причина"] == "probe: %s" % probe_state,
                  "probe-кэш %s: %r" % (probe_state, av))
        fc._load_access_cache = lambda: {
            "источники": {"егрюл": {"состояние": "доступен", "http": 307}}}
        data, av = fc.run_source("егрюл", "7707083893")
        check(errors, data == marker and av["состояние"] == "ok",
              "probe «доступен» должен пропускать к fetcher'у: %r" % av)
        # probe-кэш не распространяется на источники «кэш»/«браузер» — только «сеть»
        fc._load_access_cache = lambda: {
            "источники": {"фссп": {"состояние": "гео"}}}
        data, av = fc.run_source("фссп", "7707083893")
        check(errors, str(av["причина"]).startswith("не покрыто:"),
              "probe для браузерного источника: %r" % av)
        fc._load_access_cache = lambda: None

        # профиль
        data, av = fc.run_source("финансы", "7707083893", профиль="клиент_115фз")
        check(errors, av["состояние"] == "не проверено"
              and av["причина"] == "профиль: не требуется для клиент_115фз",
              "профиль: %r" % av)
        # браузерные источники — всегда «не покрыто»
        for sid in ("фссп", "суды"):
            data, av = fc.run_source(sid, "504110181262")
            check(errors, av["состояние"] == "не проверено"
                  and str(av["причина"]).startswith("не покрыто:"),
                  "%s: %r" % (sid, av))
            check(errors, av["требует"] == "браузер" and av["tier"] == "🔴",
                  "%s: tier/требует %r/%r" % (sid, av["tier"], av["требует"]))
        data, av = fc.run_source("фссп", "504110181262")
        check(errors, "ДВА поиска" in av["причина"], "фссп для ИП без напоминания о двух поисках")
    finally:
        fc.FETCHERS.clear()
        fc.FETCHERS.update(orig_fetchers)
        fc._load_access_cache = orig_cache_loader
        fc._CANARY_CACHE.clear()
    return errors


def case_summary(fc):
    errors = []
    dk = fc.sources.deal_killer_ids()
    check(errors, len(dk) == 7, "deal-killer источников %d, ожидалось 7" % len(dk))

    def av_map(states):
        return {sid: fc._av(sid, states.get(sid, "не проверено"), "x")
                for sid in fc.SOURCES}

    # 4 из 7 не проверено -> не состоялась
    summary = fc.build_summary(av_map({"егрюл": "ok", "риски": "ok", "санкции": "ok"}))
    check(errors, summary["проверка_состоялась"] is False, "4/7: %r" % summary)
    check(errors, summary["deal_killer_не_проверено"] == [
        "спецреестры", "фссп", "суды", "банкротство"],
          "4/7 список: %r" % summary["deal_killer_не_проверено"])
    check(errors, summary["источников"] == len(fc.SOURCES), "источников")
    check(errors, "НЕ состоялась" in summary["вывод"], "вывод: %r" % summary["вывод"])
    # 3 из 7 -> состоялась (3*2 <= 7)
    summary = fc.build_summary(av_map({"егрюл": "ok", "риски": "ok", "санкции": "ok",
                                       "спецреестры": "пусто"}))
    check(errors, summary["проверка_состоялась"] is True, "3/7: %r" % summary)
    check(errors, summary["ok"] == 3 and summary["пусто"] == 1
          and summary["не_проверено"] == len(fc.SOURCES) - 4,
          "счётчики: %r" % summary)
    return errors




# ---------------------------------------------------------------------------
# Волна 2, §1: фазы, ранний выход, параллельный сбор, спецреестры
# ---------------------------------------------------------------------------

def _fake_env(fc, состояния, журнал=None):
    """Подменяет все FETCHERS заглушками. состояния: {id: «ok»|«пусто»|«не проверено»}.

    Данные «ok»-блока — то, что передали в состояния через кортеж (состояние, данные).
    Возвращает функцию-восстановитель.
    """
    orig = dict(fc.FETCHERS)

    def make(sid):
        зн = состояния.get(sid, ("не проверено", None))
        состояние, данные = зн if isinstance(зн, tuple) else (зн, None)

        def fetcher(opener, inn, контекст=None):
            if журнал is not None:
                журнал.append(sid)
            if состояние == "ok":
                return (данные or {"маркер": sid}), fc._av(sid, "ok")
            if состояние == "пусто":
                return None, fc._av(sid, "пусто", "нет записи")
            return None, fc._av(sid, "не проверено", "сеть: заглушка")

        return fetcher

    for sid in list(fc.FETCHERS):
        fc.FETCHERS[sid] = make(sid)

    def restore():
        fc.FETCHERS.clear()
        fc.FETCHERS.update(orig)
    return restore


def case_phases(fc):
    """§1.1: у каждого источника есть фаза, фазы покрывают реестр, зависимости внутри фазы."""
    errors = []
    src = fc.sources
    check(errors, not src.validate(), "sources.validate(): %r" % src.validate())
    quick = src.sources_for_phase("quick")
    досье = src.sources_for_phase("досье")
    check(errors, set(quick) | set(досье) == set(fc.SOURCES),
          "фазы не покрывают реестр: %r" % (set(fc.SOURCES) - set(quick) - set(досье)))
    check(errors, not (set(quick) & set(досье)), "источник в двух фазах")
    # Quick-фаза обязана содержать источники, дающие deal-killer дёшево (§1.1)
    for sid in ("егрюл", "риски", "спецреестры", "санкции"):
        check(errors, sid in quick, "%s не в quick-фазе" % sid)
    for sid in ("финансы", "мсп", "нпд", "еркнм", "рнп"):
        check(errors, sid in досье, "%s не в досье-фазе" % sid)
    # Порядок обхода в фазе — порядок SOURCES
    check(errors, quick == [s for s in fc.SOURCES if s in quick], "порядок quick-фазы")
    # Зависимость собирается второй волной той же фазы
    check(errors, src.depends_on("спецреестры") == ["егрюл"],
          "спецреестры.зависит_от %r" % src.depends_on("спецреестры"))
    check(errors, not src.depends_on("егрюл"), "егрюл ни от кого не зависит")
    # Ломаная фаза ловится валидатором
    сохранено = fc.SOURCES["мсп"]["фаза"]
    try:
        fc.SOURCES["мсп"]["фаза"] = "быстро"
        check(errors, any("фаза" in e for e in src.validate()),
              "validate() не ловит неизвестную фазу")
    finally:
        fc.SOURCES["мсп"]["фаза"] = сохранено
    return errors


def case_modes(fc):
    """§1.2: режимы quick / полный / всё и ранний выход по deal-killer'у."""
    errors = []
    orig_cache = fc._load_access_cache
    fc._load_access_cache = lambda: None
    ликвидируется = {
        "егрюл": ("ok", {"наименование_полное": "ООО «ЛИКВИДИРУЕТСЯ»", "огрн": "1" * 13,
                         "руководитель": "ДИРЕКТОР: Иванов Иван Иванович", "вид": "ul"}),
        "риски": ("ok", {"статус": "в стадии ликвидации", "ликвидация": True,
                         "недостоверность_сведений": False, "оквэд": "46.90"}),
        "санкции": ("ok", {"статус": "кэш свежий"}),
        "спецреестры": ("пусто", None),
        "финансы": ("ok", {"наименование": "x", "отчётность_по_годам": []}),
        "мсп": ("ok", {"статус_мсп": "в реестре"}),
        "нпд": ("ok", {"статус_нпд": False}),
        "еркнм": ("пусто", None),
        "рнп": ("пусто", None),
    }
    досье = fc.sources.sources_for_phase("досье")
    quick = fc.sources.sources_for_phase("quick")
    try:
        # --- quick: досье-фаза не собирается вовсе
        журнал = []
        restore = _fake_env(fc, ликвидируется, журнал)
        try:
            r = fc.collect("7707083893", профиль="отсрочка", режим="quick")
        finally:
            restore()
        сбор = r.get("_сбор") or {}
        check(errors, сбор.get("режим") == "quick", "quick: режим %r" % сбор.get("режим"))
        check(errors, сбор.get("фаза_остановки") == "quick",
              "quick: фаза_остановки %r" % сбор.get("фаза_остановки"))
        check(errors, сбор.get("ранний_выход") is False,
              "quick: ранний_выход %r (это не ранний выход, а запрошенный режим)"
              % сбор.get("ранний_выход"))
        check(errors, isinstance(сбор.get("секунд"), float), "quick: нет секунд")
        for key in ("режим", "фаза_остановки", "секунд", "источников_собрано",
                    "ранний_выход", "причина_остановки"):
            check(errors, key in сбор, "quick: в _сборе нет ключа %r" % key)
        for sid in досье:
            av = r["_доступность"][sid]
            check(errors, av["состояние"] == "не проверено"
                  and str(av["причина"]).startswith("режим:"),
                  "quick: %s -> %r" % (sid, av))
            check(errors, r[sid] is None, "quick: %s с данными" % sid)
        check(errors, not [sid for sid in журнал if sid in досье],
              "quick: досье-источники всё-таки дёрнулись: %r" % журнал)
        check(errors, "спецреестры" in журнал, "quick: спецреестры не собирались")

        # --- полный + найден deal-killer -> ранний выход
        журнал = []
        restore = _fake_env(fc, ликвидируется, журнал)
        try:
            r = fc.collect("7707083893", профиль="отсрочка", режим="полный")
        finally:
            restore()
        сбор = r.get("_сбор") or {}
        check(errors, сбор.get("ранний_выход") is True, "полный+🔴: %r" % сбор)
        check(errors, сбор.get("фаза_остановки") == "quick",
              "полный+🔴: фаза_остановки %r" % сбор.get("фаза_остановки"))
        check(errors, "ликвидация" in str(сбор.get("причина_остановки")),
              "полный+🔴: причина не называет сигнал: %r" % сбор.get("причина_остановки"))
        for sid in досье:
            av = r["_доступность"][sid]
            check(errors, av["причина"] == "ранний выход: deal-killer найден на quick-фазе",
                  "полный+🔴: %s -> %r" % (sid, av["причина"]))
        check(errors, not [sid for sid in журнал if sid in досье],
              "полный+🔴: досье всё-таки собиралось: %r" % журнал)

        # --- профиль «нейтрально» светофор не выдаёт -> раннего выхода нет НИКОГДА
        журнал = []
        restore = _fake_env(fc, ликвидируется, журнал)
        try:
            r = fc.collect("7707083893", профиль="нейтрально", режим="полный")
        finally:
            restore()
        сбор = r.get("_сбор") or {}
        check(errors, сбор.get("ранний_выход") is False,
              "нейтрально: ранний выход сработал, а не должен: %r" % сбор)
        check(errors, сбор.get("фаза_остановки") == "досье",
              "нейтрально: фаза_остановки %r" % сбор.get("фаза_остановки"))
        check(errors, all(sid in журнал for sid in досье if sid in fc.FETCHERS),
              "нейтрально: досье-фаза не собралась: %r" % журнал)
        # то же самое без профиля вообще (по умолчанию «нейтрально»)
        restore = _fake_env(fc, ликвидируется)
        try:
            r2 = fc.collect("7707083893", режим="полный")
        finally:
            restore()
        check(errors, (r2.get("_сбор") or {}).get("ранний_выход") is False,
              "без профиля: ранний выход сработал: %r" % r2.get("_сбор"))

        # --- режим «всё»: ранний выход отключён даже при 🔴
        журнал = []
        restore = _fake_env(fc, ликвидируется, журнал)
        try:
            r = fc.collect("7707083893", профиль="отсрочка", режим="всё")
        finally:
            restore()
        сбор = r.get("_сбор") or {}
        check(errors, сбор.get("ранний_выход") is False and сбор.get("режим") == "всё",
              "всё: %r" % сбор)
        нужны = [sid for sid in досье
                 if sid in fc.FETCHERS and sid in fc.sources.sources_for_profile("отсрочка")]
        check(errors, all(sid in журнал for sid in нужны),
              "всё: досье-фаза не собралась: %r (ждали %r)" % (журнал, нужны))
        # профиль по-прежнему отсекает лишние источники и в режиме «всё»
        for sid in досье:
            if sid not in fc.sources.sources_for_profile("отсрочка"):
                check(errors, str(r["_доступность"][sid]["причина"]).startswith("профиль:"),
                      "всё: %s не отсечён профилем: %r" % (sid, r["_доступность"][sid]))

        # --- совместимость формата: блоки, _доступность, _итог_проверки на месте
        check(errors, set(fc.SOURCES) <= set(r), "в выводе не все блоки источников")
        check(errors, set(r["_доступность"]) == set(fc.SOURCES), "_доступность неполна")
        check(errors, isinstance(r["_итог_проверки"].get("проверка_состоялась"), bool),
              "_итог_проверки сломан")
        check(errors, list(r)[:3] == ["инн", "тип", "профиль"], "порядок ключей вывода")
        check(errors, [k for k in r if k in fc.SOURCES] == list(fc.SOURCES),
              "блоки источников не в порядке SOURCES")

        # --- неизвестный режим не роняет сбор, откатывается в «полный»
        restore = _fake_env(fc, ликвидируется)
        try:
            r = fc.collect("7707083893", профиль="нейтрально", режим="бегом")
        finally:
            restore()
        check(errors, (r.get("_сбор") or {}).get("режим") == "полный",
              "неизвестный режим: %r" % r.get("_сбор"))
        check(errors, len(quick) + len(досье) == len(fc.SOURCES), "фазы не сходятся")
    finally:
        fc._load_access_cache = orig_cache
    return errors


def case_parallel(fc):
    """§1.3: параллельный сбор, свой opener на поток, COUNTERPARTY_SEQUENTIAL=1."""
    import threading
    errors = []
    orig_cache = fc._load_access_cache
    fc._load_access_cache = lambda: None

    def шпион(sid, потоки, openers, пары):
        """Fetcher-шпион: помечает, в каком потоке и с каким opener его позвали."""
        def fetcher(opener, inn, контекст=None):
            потоки.add(threading.current_thread().name)
            openers.add(id(opener))
            пары.add((id(opener), threading.get_ident()))
            return {"маркер": sid}, fc._av(sid, "ok")
        return fetcher

    try:
        for seq in ("1", None):
            потоки, openers, пары = set(), set(), set()
            orig = dict(fc.FETCHERS)
            for sid in list(fc.FETCHERS):
                fc.FETCHERS[sid] = шпион(sid, потоки, openers, пары)
            if seq:
                os.environ["COUNTERPARTY_SEQUENTIAL"] = seq
            else:
                os.environ.pop("COUNTERPARTY_SEQUENTIAL", None)
            try:
                r = fc.collect("7707083893", режим="всё")
            finally:
                fc.FETCHERS.clear()
                fc.FETCHERS.update(orig)
                os.environ.pop("COUNTERPARTY_SEQUENTIAL", None)
            метка = "последовательно" if seq else "параллельно"
            check(errors, (r.get("_сбор") or {}).get("параллельно") is (not seq),
                  "%s: пометка в _сборе %r" % (метка, r.get("_сбор")))
            собрано = [sid for sid in fc.SOURCES if r["_доступность"][sid]["состояние"] == "ok"]
            check(errors, len(собрано) == len(fc.FETCHERS),
                  "%s: собрано %d из %d" % (метка, len(собрано), len(fc.FETCHERS)))
            for sid in собрано:
                check(errors, r[sid] == {"маркер": sid},
                      "%s: блок %s перепутан: %r" % (метка, sid, r[sid]))
            if seq:
                check(errors, len(потоки) == 1,
                      "последовательно: потоков %d" % len(потоки))
                check(errors, len(openers) == 1,
                      "последовательно: openers %d (должен быть один общий)" % len(openers))
            else:
                check(errors, len(потоки) > 1,
                      "параллельно: сбор шёл в одном потоке (%r)" % потоки)
                # В пуле не больше MAX_WORKERS имён; волна из одного источника
                # («спецреестры» после «егрюл») идёт в главном потоке — её имя
                # в лимит пула не входит.
                рабочие = {n for n in потоки if n.startswith("источник")}
                check(errors, len(рабочие) <= fc.MAX_WORKERS,
                      "параллельно: рабочих потоков %d > MAX_WORKERS %d (%r)"
                      % (len(рабочие), fc.MAX_WORKERS, sorted(потоки)))
                check(errors, рабочие, "параллельно: пул не использовался (%r)" % потоки)
                # Главный инвариант §1.3: один opener — один поток. CookieJar не
                # потокобезопасен, поэтому один и тот же opener НЕ должен встретиться
                # в двух потоках (обратное — один поток с несколькими openers за
                # несколько волн — допустимо и безвредно).
                хозяева = {}
                for op_id, ident in пары:
                    хозяева.setdefault(op_id, set()).add(ident)
                делят = [op for op, ids in хозяева.items() if len(ids) > 1]
                check(errors, not делят,
                      "параллельно: opener(ы) %r шарятся между потоками" % делят)
                check(errors, len(openers) >= len(потоки),
                      "параллельно: openers %d < потоков %d" % (len(openers), len(потоки)))

        # дедлайн общий: исчерпан -> сетевые источники падают, сбор не виснет
        orig_start, orig_deadline = fc._START, fc.DEADLINE
        orig = dict(fc.FETCHERS)
        зашли = []

        def медленный(opener, inn, контекст=None):
            зашли.append(1)
            if fc._time_left() <= 0:
                raise fc.DeadlineExceeded("бюджет времени исчерпан")
            return {"x": 1}, fc._av("мсп", "ok")

        for sid in list(fc.FETCHERS):
            fc.FETCHERS[sid] = медленный
        try:
            fc.DEADLINE = -1.0        # бюджет исчерпан в момент старта
            r = fc.collect("7707083893", режим="всё")
        finally:
            fc.FETCHERS.clear()
            fc.FETCHERS.update(orig)
            fc.DEADLINE, fc._START = orig_deadline, orig_start
        состояния_ = {sid: r["_доступность"][sid]["состояние"] for sid in fc.FETCHERS}
        check(errors, all(v == "не проверено" for v in состояния_.values()),
              "исчерпанный дедлайн: %r" % состояния_)
        check(errors, all(str(r["_доступность"][sid]["причина"]).split(":")[0]
                          in ("дедлайн", "сеть", "схема") for sid in fc.FETCHERS),
              "исчерпанный дедлайн: причины без машинного префикса")
        check(errors, (r.get("_сбор") or {}).get("секунд") < 30,
              "исчерпанный дедлайн: сбор висел %r с" % (r.get("_сбор") or {}).get("секунд"))
    finally:
        fc._load_access_cache = orig_cache
        os.environ.pop("COUNTERPARTY_SEQUENTIAL", None)
    return errors


def case_canary_threadsafe(fc):
    """§1.3: канарейка одного источника запускается РОВНО один раз на процесс."""
    import threading
    errors = []
    orig = dict(fc.FETCHERS)
    fc._CANARY_CACHE.clear()
    вызовы = []
    барьер = threading.Barrier(8)

    def медленная_канарейка(opener, inn):
        вызовы.append(inn)
        time.sleep(0.05)      # окно гонки: без замка сюда войдут все потоки сразу
        return {"статус_мсп": "в реестре", "категория": "микро"}, fc._av("мсп", "ok")

    fc.FETCHERS["мсп"] = медленная_канарейка
    canary = fc.SOURCES["мсп"]["канарейка"]
    результаты = []

    def гонка():
        барьер.wait()
        результаты.append(fc._run_canary("мсп", canary, object()))

    try:
        потоки = [threading.Thread(target=гонка) for _ in range(8)]
        for t in потоки:
            t.start()
        for t in потоки:
            t.join(timeout=10)
        check(errors, len(вызовы) == 1,
              "канарейка запущена %d раз вместо одного (гонка потоков)" % len(вызовы))
        check(errors, len(результаты) == 8 and all(r == ("ok", None) for r in результаты),
              "гонка канарейки дала разные результаты: %r" % результаты)
    finally:
        fc.FETCHERS.clear()
        fc.FETCHERS.update(orig)
        fc._CANARY_CACHE.clear()
    return errors


def case_special_registries(fc):
    """§1.4: реестр дисквалифицированных — контекст, самопроверка, честные причины."""
    errors = []
    # без контекста (блока «егрюл» нет) — «не проверено», а не «пусто»
    for контекст in (None, {}, {"егрюл": None}, {"егрюл": {"руководитель": ""}}):
        try:
            fc.raw_special_registries(object(), "7707083893", контекст)
        except fc.SourceUnavailable as e:
            check(errors, str(e).startswith("не покрыто:"),
                  "без контекста: причина %r" % str(e))
        else:
            errors.append("без контекста %r: исключения не было" % (контекст,))
    # ФИО достаётся из «руководитель» (ЮЛ) и из наименования (ИП, вид fl)
    фио, откуда, отказ = fc._disq_head_name(
        {"егрюл": {"руководитель": "ГЕНЕРАЛЬНЫЙ ДИРЕКТОР: Иванов  Александр Борисович"}})
    check(errors, фио == "ИВАНОВ АЛЕКСАНДР БОРИСОВИЧ", "ФИО из ЕГРЮЛ: %r" % фио)
    check(errors, "ЕГРЮЛ" in (откуда or "") and отказ is None,
          "не сказано, откуда ФИО: %r / %r" % (откуда, отказ))
    фио_ип, _, _ = fc._disq_head_name(
        {"егрюл": {"вид": "fl", "наименование_полное": "Иванов Димитър Иванов",
                   "руководитель": None}})
    check(errors, фио_ип == "ИВАНОВ ДИМИТЪР ИВАНОВ", "ФИО ИП: %r" % фио_ип)
    # руководитель-организация (живой случай 5036045205 АО «ДИКСИ ЮГ») — «не проверено»
    # с подсказкой, а НЕ «пусто»: искать организацию в реестре физлиц бессмысленно
    ук = fc._disq_head_name({"егрюл": {
        "вид": "ul",
        "руководитель": 'Управляющая организация: АКЦИОНЕРНОЕ ОБЩЕСТВО "ДИКСИ ГРУПП"'}})
    check(errors, ук[0] is None, "управляющая организация принята за ФИО: %r" % (ук,))
    check(errors, "управляющей организации" in (ук[2] or ""),
          "нет подсказки про управляющую организацию: %r" % (ук[2],))
    for не_фио in ('ООО "РОМАШКА"', "ИВАНОВ", "ИВАН ИВАНОВ ИВАН ИВАНОВИЧ ПЕТРОВ",
                   "ООО РОМАШКА", "АО ДИКСИ", "Фонд поддержки"):
        check(errors, not fc._похоже_на_фио(не_фио), "%r принято за ФИО" % не_фио)
    for фио_ok in ("ИВАНОВ АЛЕКСАНДР БОРИСОВИЧ", "Петров Пётр",
                   "Иванова-Сидорова Анна Петровна"):
        check(errors, fc._похоже_на_фио(фио_ok), "%r не принято за ФИО" % фио_ok)
    # пустой ответ + сломанная самопроверка -> «не проверено», НИКОГДА не «пусто»
    пусто_сломано = {"запрос": {"тип": "ФИО", "значение": "ИВАНОВ ИВАН ИВАНОВИЧ"},
                     "ответ": {"data": [], "rowCount": 0},
                     "самопроверка": {"записей_в_реестре": 0}}
    data, av = fc.parse_special_registries(пусто_сломано, "7707083893")
    check(errors, av["состояние"] == "не проверено" and data is None,
          "нулевая самопроверка: %r" % av)
    check(errors, "самопроверка" in str(av["причина"]),
          "нулевая самопроверка: причина %r" % av["причина"])
    for самопроверка in (None, {}, {"записей_в_реестре": None}):
        _, av2 = fc.parse_special_registries(
            dict(пусто_сломано, самопроверка=самопроверка), "7707083893")
        check(errors, av2["состояние"] == "не проверено",
              "самопроверка %r -> %r" % (самопроверка, av2["состояние"]))
    # живая самопроверка -> «пусто» (подтверждённое отсутствие)
    _, av3 = fc.parse_special_registries(
        dict(пусто_сломано, самопроверка={"записей_в_реестре": 8188}), "7707083893")
    check(errors, av3["состояние"] == "пусто", "живая самопроверка -> %r" % av3["состояние"])
    # срок дисквалификации считается по датам записи
    import datetime as dt
    запись = {"ДатаНачДискв": "25.08.2026 00:00:00", "ДатаКонДискв": "24.08.2027 00:00:00"}
    check(errors, fc._disq_действует(запись, dt.date(2026, 12, 1)) is True, "действует")
    check(errors, fc._disq_действует(запись, dt.date(2028, 1, 1)) is False, "истекла")
    check(errors, fc._disq_действует(запись, dt.date(2026, 1, 1)) is False, "ещё не началась")
    check(errors, fc._disq_действует({"ДатаКонДискв": "—"}) is None, "мусорная дата")
    # честные пометки по выбывшим сервисам ФНС — в каждом ok-блоке
    raw_ok = json.loads((FIXTURES / "спецреестры_5836898322.raw.json")
                        .read_text(encoding="utf-8"))
    data, av = fc.parse_special_registries(raw_ok, "5836898322")
    check(errors, av["состояние"] == "ok", "фикстура ok: %r" % av)
    for ключ in ("налоговая_задолженность", "недостоверность_сведений"):
        check(errors, (data.get(ключ) or {}).get("статус") == "не покрыто",
              "%s должен оставаться «не покрыто»: %r" % (ключ, data.get(ключ)))
        check(errors, "риски" in str((data.get(ключ) or {}).get("причина")),
              "%s: причина не отправляет к живому источнику" % ключ)
    дискв = data["дисквалификация_руководителя"]
    check(errors, дискв["данные"]["точных_совпадений_фио"] == 1, "точное совпадение ФИО")
    # profiles.py читает подблок как {"статус", "данные"} — форма обязана совпадать
    profiles = load_module("profiles", ROOT / "scripts" / "profiles.py")
    check(errors, profiles._истина(дискв) is True,
          "profiles._истина не видит найденную дисквалификацию: %r" % (дискв,))
    for ключ in ("налоговая_задолженность", "недостоверность_сведений"):
        check(errors, profiles._истина(data[ключ]) is None,
              "%s должен читаться как «не проверен», а не как факт" % ключ)
    check(errors, "ручной сверки" in дискв["примечание"]
          or "НЕ идентификация" in дискв["примечание"],
          "нет пометки об однофамильцах: %r" % дискв["примечание"])
    # дескриптор фиксирует разведку дословно
    разведка = fc.SOURCES["спецреестры"].get("разведка")
    check(errors, isinstance(разведка, dict) and len(разведка) >= 8,
          "в дескрипторе нет разведки живыми запросами")
    check(errors, any("zd.do" in k for k in разведка or {}), "нет записи про zd.do")
    check(errors, any("ИНН" in k for k in разведка or {}),
          "не зафиксировано, что поиск по ИНН не работает")
    return errors


def case_proxy(fc):
    """§18.2: свой прокси — и probe-кэш, снятый из другой сети, не переиспользуется."""
    errors = []
    сохранено = (fc._ПРОКСИ, fc.ACCESS_CACHE, fc._КЭШ_ДОСТУПА_ОТКЛОНЁН)
    # main() глушит probe-кэш на весь прогон; этому кейсу он как раз нужен живым.
    игнор = os.environ.pop("COUNTERPARTY_IGNORE_ACCESS_CACHE", None)
    all_proxy = os.environ.pop("ALL_PROXY", None)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            путь = os.path.join(tmp, "access.json")
            fc.ACCESS_CACHE = путь
            # check_access пишет локальное время БЕЗ зоны — воспроизводим как есть.
            свежая_дата = (_dt.datetime.now(_dt.timezone.utc).astimezone()
                           .replace(tzinfo=None).isoformat(timespec="seconds"))

            def записать(сеть):
                кэш = {"дата": свежая_дата, "ip_класс": "РФ",
                       "источники": {"егрюл": {"состояние": "доступен"}}}
                if сеть is not None:
                    кэш["сеть"] = сеть
                with open(путь, "w", encoding="utf-8") as fh:
                    json.dump(кэш, fh, ensure_ascii=False)

            # 1. Прямой кэш при прямом прогоне — берётся.
            записать({"отпечаток": "прямое"})
            fc.установить_прокси(None)
            fc._КЭШ_ДОСТУПА_ОТКЛОНЁН = None
            check(errors, fc._load_access_cache() is not None,
                  "свой же прямой кэш обязан читаться")

            # 2. Тот же кэш, но идём через прокси — сеть другая, кэш не про неё.
            fc.установить_прокси("http://rf.example:3128")
            fc._КЭШ_ДОСТУПА_ОТКЛОНЁН = None
            check(errors, fc._load_access_cache() is None,
                  "кэш прямого прогона не должен подставляться прокси-прогону: "
                  "иначе «гео: недоступно» читалось бы как «доступно»")
            check(errors, fc._КЭШ_ДОСТУПА_ОТКЛОНЁН,
                  "отклонение кэша обязано быть названо, а не произойти молча")

            # 3. Обратное направление: кэш снят через ноду, идём напрямую.
            записать({"отпечаток": fc.proxy.отпечаток("http://rf.example:3128")})
            fc.установить_прокси(None)
            fc._КЭШ_ДОСТУПА_ОТКЛОНЁН = None
            check(errors, fc._load_access_cache() is None,
                  "кэш прокси-прогона не должен подставляться прямому")

            # 4. Кэш старого формата: чем снят — неизвестно, значит не годится.
            записать(None)
            fc.установить_прокси(None)
            fc._КЭШ_ДОСТУПА_ОТКЛОНЁН = None
            check(errors, fc._load_access_cache() is None,
                  "кэш без отметки сети (версия до 1.11.0) не годится ни для чего")

        # 5. Прокси реально попадает в opener, а не только в вывод.
        fc.установить_прокси("http://rf.example:3128")
        прокси_хендлеры = [h for h in fc._make_opener().handlers
                           if isinstance(h, urllib.request.ProxyHandler)]
        check(errors, len(прокси_хендлеры) == 1,
              "ProxyHandler должен быть ровно один: %r" % прокси_хендлеры)
        check(errors, прокси_хендлеры and прокси_хендлеры[0].proxies.get("https")
              == "http://rf.example:3128",
              "opener не ходит через заданный прокси: %r"
              % (прокси_хендлеры[0].proxies if прокси_хендлеры else None))

        # 6. Прямое соединение — ЯВНО прямое. urllib по умолчанию подхватывает
        #    ЛЮБУЮ переменную вида *_proxy (ALL_PROXY, ftp_proxy…), а проект
        #    честно знает только про три. Без явного пустого ProxyHandler трафик
        #    ушёл бы через ALL_PROXY, а «_сеть» говорила бы «прямое соединение» —
        #    ровно та тихая деградация, от которой вся эта возня.
        os.environ["ALL_PROXY"] = "http://мимо.example:9999"
        try:
            fc.установить_прокси(None)
            check(errors, fc._ПРОКСИ["url"] is None,
                  "ALL_PROXY не входит в поддерживаемые переменные: %r" % fc._ПРОКСИ)
            живые = [h.proxies for h in fc._make_opener().handlers
                     if isinstance(h, urllib.request.ProxyHandler) and h.proxies]
            check(errors, not живые,
                  "при «прямом соединении» opener не должен знать ни одного "
                  "прокси, а знает: %r" % живые)
        finally:
            os.environ.pop("ALL_PROXY", None)

        # 7. Негодный URL — отказ на уровне opener'а, а не тихий прямой выход.
        ошибка = fc.установить_прокси("socks5://host:1080")
        check(errors, ошибка, "socks5 обязан быть отвергнут")
        try:
            fc._make_opener()
        except ValueError as e:
            check(errors, "прокси" in str(e), "текст отказа: %r" % str(e))
        else:
            errors.append("_make_opener с негодным прокси обязан упасть, "
                          "а не пойти напрямую в обход настройки")

        # 8. Блок «_сеть» в выводе: через что шли, видно без чтения окружения.
        fc.установить_прокси("http://логин:секрет@rf.example:3128")
        restore = _fake_env(fc, {sid: ("пусто", None) for sid in fc.SOURCES})
        try:
            r = fc.collect("7707083893", режим="quick")
        finally:
            restore()
        сеть = r.get("_сеть") or {}
        check(errors, сеть.get("отпечаток", "").startswith("прокси:"),
              "_сеть при прокси: %r" % сеть)
        сырое = json.dumps(r, ensure_ascii=False)
        check(errors, "секрет" not in сырое,
              "пароль прокси не должен утекать в вывод (его сохраняют в снимки)")
        check(errors, "rf.example" in сырое, "хост прокси в выводе: %r" % сеть)
        check(errors, "ИНН" in (сеть.get("оговорка") or ""),
              "оговорка обязана сказать, что узел видит проверяемые ИНН: %r" % сеть)

        # 9. Флаг разбирается в обеих формах.
        for argv, ждём in ((["x", "7707083893", "--прокси", "http://a:1"], "http://a:1"),
                           (["x", "7707083893", "--прокси=http://b:2"], "http://b:2"),
                           (["x", "7707083893"], None)):
            check(errors, fc._parse_args(argv)[-1] == ждём,
                  "разбор %r -> %r" % (argv, fc._parse_args(argv)[-1]))
    finally:
        fc._ПРОКСИ, fc.ACCESS_CACHE, fc._КЭШ_ДОСТУПА_ОТКЛОНЁН = сохранено
        for имя, значение in (("COUNTERPARTY_IGNORE_ACCESS_CACHE", игнор),
                              ("ALL_PROXY", all_proxy)):
            os.environ.pop(имя, None)
            if значение is not None:
                os.environ[имя] = значение
    return errors


def case_bankrupt(fc):
    """ЕФРСБ: стадии, чужой ИНН, усечение, ИП-эндпоинт, антибот, флаг без Referer."""
    errors = []
    base = json.loads((FIXTURES / "банкротство_0266051268.raw.json").read_text(encoding="utf-8"))

    def с_записью(**изм):
        raw = copy.deepcopy(base)
        rec = raw["pageData"][0]
        for k, v in изм.items():
            if k == "код":
                rec["lastLegalCase"]["status"]["code"] = v
            elif k == "номер":
                rec["lastLegalCase"]["number"] = v
            else:
                rec[k] = v
        return raw

    данные, av = fc.parse_bankrupt(с_записью(код="SomethingNew"), "0266051268")
    check(errors, av["состояние"] == "не проверено" and данные is None
          and str(av["причина"]).startswith("схема:") and "SomethingNew" in av["причина"],
          "неизвестная стадия — «не проверено: схема» с кодом: %r" % av)
    данные, av = fc.parse_bankrupt(с_записью(номер=""), "0266051268")
    check(errors, av["состояние"] == "не проверено" and "номера" in str(av["причина"]),
          "запись без номера дела — «не проверено»: %r" % av)
    данные, av = fc.parse_bankrupt(с_записью(код="ProceedingsStopped"), "0266051268")
    check(errors, данные["процедура"] is False
          and str(данные["прекращённое_дело"]).startswith("дело А07-40092/2023"),
          "прекращённое дело: %r" % данные)
    # чужой ИНН в выдаче (нечёткий поиск) — «пусто», а не чужое дело
    данные, av = fc.parse_bankrupt(с_записью(inn="0266000000"), "0266051268")
    check(errors, av["состояние"] == "пусто" and данные is None,
          "чужой ИНН в выдаче должен давать «пусто»: %r %r" % (av, данные))
    # усечённая выдача без точного ИНН — не «пусто»
    raw = с_записью(inn="0266000000")
    raw["total"] = 40
    данные, av = fc.parse_bankrupt(raw, "0266051268")
    check(errors, av["состояние"] == "не проверено" and str(av["причина"]).startswith("схема:")
          and "усечена" in str(av["причина"]), "усечённая выдача — сбой, не предел: %r" % av)
    for плохой in ([], {"total": 0}, {"pageData": {}}, {"pageData": [None], "total": 1},
                   {"pageData": [{"inn": None}], "total": 1}, {"pageData": [], "total": "0"},
                   {"pageData": [{"inn": ["0266051268"]}], "total": 1},
                   {"pageData": [{"inn": "02660512"}], "total": 1},
                   {"pageData": [], "total": -1},
                   {"pageData": [{"inn": "0266000000"}, {"inn": "0266000001"}], "total": 1},
                   {"pageData": []}, {"pageData": [], "total": True}):
        данные, av = fc.parse_bankrupt(плохой, "0266051268")
        check(errors, av["состояние"] == "не проверено"
              and str(av["причина"]).startswith("схема:"), "схема %r: %r" % (плохой, av))

    orig_get = fc._http_get
    журнал = []
    ответ = {"статус": 200, "тело": json.dumps(base, ensure_ascii=False)}

    параметры = []

    openers = []

    def фейк(opener, url, referer=None, accept=None, ua=None, повторы=True, xhr=True):
        журнал.append((url, referer, ua))
        openers.append(opener)
        параметры.append((повторы, xhr))
        return ответ["статус"], ответ["тело"]
    fc._http_get = фейк
    try:
        данные, av = fc.fetch_bankrupt(None, "0266051268")
        url, referer, ua = журнал[-1]
        check(errors, "/backend/cmpbankrupts?searchString=0266051268" in url,
              "юрлицо — cmpbankrupts: %s" % url)
        check(errors, referer == "https://bankrot.fedresurs.ru/",
              "Referer — главная ЕФРСБ: %r" % referer)
        check(errors, ua and "inn-check-ru" in ua and "github.com" in ua,
              "UA проекта со ссылкой на репозиторий: %r" % ua)
        check(errors, av["состояние"] == "ok" and данные["процедура"], "живой путь: %r" % av)
        check(errors, параметры[-1] == (False, False),
              "ЕФРСБ: один запрос без повторов и без X-Requested-With: %r" % (параметры[-1],))
        check(errors, any(isinstance(h, fc._БезРедиректов)
                          for h in getattr(openers[-1], "handlers", [])),
              "ЕФРСБ: opener без редиректов")
        # причина «антибот:» доживает до _доступность через run_source
        ответ["статус"] = 403
        _, av = fc.run_source("банкротство", "0266051268")
        check(errors, str(av.get("причина")).startswith("антибот:"),
              "run_source сохранил причину антибота: %r" % av)
        ответ["статус"] = 200
        n = len(журнал)
        данные, av = fc.fetch_bankrupt(None, "504110181262")
        check(errors, len(журнал) == n and str(av["причина"]).startswith("не покрыто:"),
              "ИП: без запроса и «не покрыто»: %r" % av)
        for код in (401, 403, 429, 302):
            ответ["статус"] = код
            n = len(журнал)
            try:
                fc.fetch_bankrupt(None, "0266051268")
                errors.append("%d: ожидалось SourceUnavailable" % код)
            except fc.SourceUnavailable as e:
                check(errors, str(e).startswith("антибот:") and str(код) in str(e),
                      "%d: причина %r" % (код, str(e)))
            check(errors, len(журнал) == n + 1, "%d: повтор после отказа" % код)
        ответ["статус"] = 451
        try:
            fc.fetch_bankrupt(None, "0266051268")
            errors.append("451: ожидалось SourceUnavailable")
        except fc.SourceUnavailable as e:
            check(errors, str(e).startswith("гео:"), "451: %r" % str(e))
        ответ.update(статус=200, тело="<html>challenge</html>")
        try:
            fc.fetch_bankrupt(None, "0266051268")
            errors.append("не-JSON: ожидалось SourceUnavailable")
        except fc.SourceUnavailable as e:
            check(errors, str(e).startswith("антибот:"), "не-JSON: %r" % str(e))
        os.environ["INN_CHECK_BEZ_REFERER"] = "1"
        n = len(журнал)
        данные, av = fc.fetch_bankrupt(None, "0266051268")
        check(errors, len(журнал) == n and av["состояние"] == "не проверено"
              and str(av["причина"]).startswith("не покрыто:"),
              "INN_CHECK_BEZ_REFERER=1: без запроса и «не покрыто»: %r" % av)
    finally:
        os.environ.pop("INN_CHECK_BEZ_REFERER", None)
        fc._http_get = orig_get

    # сам транспорт: повторы=False — ровно одно обращение и на 429, и на обрыв
    class Opener:
        def __init__(self, exc):
            self.exc, self.n = exc, 0

        def open(self, req, timeout=None):
            self.n += 1
            raise self.exc
    import urllib.error
    orig_sleep, orig_start = fc.time.sleep, fc._START
    fc.time.sleep = lambda s: None
    fc._START = fc.time.monotonic()  # бюджет времени сбора — заново, иначе 0 обращений
    try:
        for exc in (urllib.error.HTTPError("https://x", 429, "rate", {}, None),
                    ConnectionResetError("обрыв")):
            op = Opener(exc)
            try:
                fc._http_get_настоящий(op, "https://x", referer="https://x/", повторы=False)
            except Exception:
                pass
            check(errors, op.n == 1, "повторы=False: %d обращений на %r" % (op.n, exc))
            op = Opener(exc)
            try:
                fc._http_get_настоящий(op, "https://x")
            except Exception:
                pass
            check(errors, op.n > 1, "по умолчанию повторы остаются: %d на %r" % (op.n, exc))
    finally:
        fc.time.sleep, fc._START = orig_sleep, orig_start

    # редиректы: локальный сервер отвечает 302 на себя — без редиректов ровно 1 запрос
    import http.server
    import threading
    счёт = {"n": 0}

    class Обработчик(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            счёт["n"] += 1
            self.send_response(302)
            self.send_header("Location", "/next%d" % счёт["n"])
            self.end_headers()

        def log_message(self, *a):
            pass
    сервер = http.server.HTTPServer(("127.0.0.1", 0), Обработчик)
    поток = threading.Thread(target=сервер.serve_forever, daemon=True)
    поток.start()
    fc._START = fc.time.monotonic()
    try:
        url = "http://127.0.0.1:%d/start" % сервер.server_port
        статус, _ = fc._http_get_настоящий(fc._make_opener(редиректы=False), url,
                                           повторы=False)
        check(errors, статус == 302 and счёт["n"] == 1,
              "без редиректов: статус %r, запросов %d" % (статус, счёт["n"]))
        счёт["n"] = 0
        try:
            fc._http_get_настоящий(fc._make_opener(), url, повторы=False)
        except Exception:
            pass
        check(errors, счёт["n"] > 1, "обычный opener следует редиректам (%d)" % счёт["n"])
    finally:
        сервер.shutdown()
        fc._START = orig_start
    return errors


def case_fedresurs(fc):
    """Федресурс: роль субъекта, чужой должник, аннулирование, усечение, транспорт."""
    errors = []

    def загрузить(inn):
        return json.loads((FIXTURES / ("федресурс_%s.raw.json" % inn)).read_text(encoding="utf-8"))

    # компания-кредитор (Сбербанк публикует намерения о чужих банкротствах) — без сигнала
    данные, av = fc.parse_fedresurs(загрузить("7707083893"), "7707083893")
    check(errors, av["состояние"] == "ok" and not данные["намерение_кредитора"]
          and данные["сообщения"], "кредитор-публикатор не субъект: %r" % данные)
    # должник: намерения кредиторов — сигнал; чужое намерение должника, где он участник, — нет
    данные, av = fc.parse_fedresurs(загрузить("5029169023"), "5029169023")
    check(errors, str(данные["намерение_кредитора"]).count("сообщение") == 6,
          "должник: шесть намерений кредиторов: %r" % данные["намерение_кредитора"])
    чужое = [x for x in данные["сообщения"] if x["тип"].startswith("Намерение должника")]
    check(errors, чужое and чужое[0]["субъект"] is False and данные["намерение_должника"] is None,
          "чужое намерение должника (компания — кредитор): %r" % чужое)
    # усечённая выдача: «нет» не утверждается
    check(errors, данные["выдача"]["усечена"] and данные["решение_о_ликвидации"] is None,
          "усечена: отсутствие должно быть None: %r" % данные["выдача"])
    # аннулированное сообщение не считается
    raw = загрузить("9500007240")
    raw["публикации"]["pageData"][0]["isAnnulled"] = True
    данные, av = fc.parse_fedresurs(raw, "9500007240")
    check(errors, данные["решение_о_ликвидации"] is False and данные["аннулировано"] == 1,
          "аннулированная ликвидация не должна давать сигнал: %r" % данные)
    # роль не распознана: компании нет ни в публикаторах, ни в участниках
    raw = загрузить("9500007240")
    raw["публикации"]["pageData"][0]["publisher"] = {"guid": "чужой", "type": "Company",
                                                     "name": "ООО Другое"}
    данные, av = fc.parse_fedresurs(raw, "9500007240")
    check(errors, данные["решение_о_ликвидации"] is None and данные.get("роль_не_распознана"),
          "ликвидация, опубликованная другим лицом, — не сигнал и не «нет» (None): %r"
          % данные)
    # пустой публикатор у намерения кредитора: кредитор не установлен — не сигнал и не «нет»
    raw = загрузить("7707083893")
    for m in raw["публикации"]["pageData"]:
        if m["type"].startswith("Намерение кредитора"):
            m["publisher"] = {}
    raw["публикации"]["found"] = len(raw["публикации"]["pageData"])
    данные, av = fc.parse_fedresurs(raw, "7707083893")
    check(errors, данные["намерение_кредитора"] is None,
          "намерение кредитора без guid публикатора — None: %r" % данные["намерение_кредитора"])
    # собственное намерение должника — сигнал
    raw = загрузить("9500007240")
    raw["публикации"]["pageData"][0]["type"] = (
        "Намерение должника обратиться в суд с заявлением о банкротстве")
    данные, av = fc.parse_fedresurs(raw, "9500007240")
    check(errors, str(данные["намерение_должника"]).startswith("2023-04-19"),
          "своё намерение должника: %r" % данные["намерение_должника"])
    # кредитор-физлицо: ФИО в вывод не попадает
    raw = загрузить("5029169023")
    for m in raw["публикации"]["pageData"]:
        if m["type"].startswith("Намерение кредитора"):
            m["publisher"] = {"guid": "p1", "type": "Person", "name": "ТЕСТОВ ТЕСТ ТЕСТОВИЧ"}
    данные, av = fc.parse_fedresurs(raw, "5029169023")
    check(errors, данные["намерение_кредитора"]
          and "ТЕСТОВ" not in json.dumps(данные, ensure_ascii=False),
          "ФИО кредитора-физлица не должно попадать в вывод")
    # недостоверность: публикатор ЕГРЮЛ без guid, компания — участник
    данные, av = fc.parse_fedresurs(загрузить("7713392265"), "7713392265")
    check(errors, str(данные["недостоверность_сведений"]).startswith("2026-08-12"),
          "недостоверность (публикатор ЕГРЮЛ): %r" % данные["недостоверность_сведений"])
    # схема: битое сообщение, found не числом, нет компании при усечённом поиске
    for имя, порча in (
            ("type", lambda r: r["публикации"]["pageData"][0].pop("type")),
            ("publicationType", lambda r: r["публикации"]["pageData"][0].pop("publicationType")),
            ("пустой publicationType", lambda r: r["публикации"]["pageData"][0].__setitem__(
                "publicationType", "")),
            ("пустой type", lambda r: r["публикации"]["pageData"][0].__setitem__("type", " ")),
            ("participants", lambda r: r["публикации"]["pageData"][0].__setitem__(
                "participants", [None])),
            ("datePublish", lambda r: r["публикации"]["pageData"][0].__setitem__(
                "datePublish", "вчера")),
            ("found bool", lambda r: r["публикации"].__setitem__("found", True)),
            ("found", lambda r: r["публикации"].__setitem__("found", "1")),
            ("поиск", lambda r: r["компании"].__setitem__("pageData", [{"inn": "1"}]))):
        raw = загрузить("9500007240")
        порча(raw)
        if имя == "поиск":
            raw["компании"]["found"] = 5
        данные, av = fc.parse_fedresurs(raw, "9500007240")
        check(errors, av["состояние"] == "не проверено" and str(av["причина"]).startswith("схема:"),
              "схема %s: %r" % (имя, av))
    # чужой ИНН в выдаче поиска — не наша компания
    raw = загрузить("9500007240")
    raw["компании"]["pageData"][0]["inn"] = "9500007241"
    данные, av = fc.parse_fedresurs(raw, "9500007240")
    check(errors, av["состояние"] == "пусто" and данные is None,
          "чужой ИНН в поиске — «пусто», а не чужие сообщения: %r" % av)
    raw = загрузить("9500007240")
    raw["компании"] = {"pageData": [], "found": 0}
    данные, av = fc.parse_fedresurs(raw, "9500007240")
    check(errors, av["состояние"] == "пусто", "нет компании — «пусто»: %r" % av)

    # транспорт: два запроса, Referer страницы, без повторов/XHR, антибот
    orig_get = fc._http_get
    журнал = []
    ответы = {"поиск": загрузить("9500007240")["компании"],
              "публикации": загрузить("9500007240")["публикации"]}
    статус = {"код": 200}

    def фейк(opener, url, referer=None, accept=None, ua=None, повторы=True, xhr=True):
        журнал.append((url, referer, ua, повторы, xhr))
        тело = ответы["публикации" if "/publications" in url else "поиск"]
        return статус["код"], json.dumps(тело, ensure_ascii=False)
    fc._http_get = фейк
    try:
        данные, av = fc.fetch_fedresurs(None, "9500007240")
        check(errors, len(журнал) == 2 and av["состояние"] == "ok"
              and данные["решение_о_ликвидации"], "два запроса и сигнал: %r %r" % (журнал, av))
        check(errors, all(not п and not x and ua == fc.UA_ПРОЕКТА for _, _, ua, п, x in журнал),
              "без повторов, без XHR, UA проекта: %r" % журнал)
        check(errors, журнал[0][1] == "https://fedresurs.ru/"
              and журнал[1][1].startswith("https://fedresurs.ru/companies/"),
              "Referer страниц сайта: %r" % [j[1] for j in журнал])
        for код, префикс in ((403, "антибот:"), (302, "антибот:"), (500, "сеть:")):
            статус["код"] = код
            n = len(журнал)
            try:
                fc.fetch_fedresurs(None, "9500007240")
                errors.append("%d: ожидалось SourceUnavailable" % код)
            except fc.SourceUnavailable as e:
                check(errors, str(e).startswith(префикс) and len(журнал) == n + 1,
                      "%d: %s без второго запроса: %r" % (код, префикс, str(e)))
        статус["код"] = 200
        n = len(журнал)
        _, av = fc.fetch_fedresurs(None, "504110181262")
        check(errors, len(журнал) == n and str(av["причина"]).startswith("не покрыто:"),
              "ИП: без запроса: %r" % av)
        os.environ["INN_CHECK_BEZ_REFERER"] = "1"
        _, av = fc.fetch_fedresurs(None, "9500007240")
        check(errors, len(журнал) == n and str(av["причина"]).startswith("не покрыто:"),
              "INN_CHECK_BEZ_REFERER=1: %r" % av)
    finally:
        os.environ.pop("INN_CHECK_BEZ_REFERER", None)
        fc._http_get = orig_get
    return errors


def main():
    fc = load_module("fetch_counterparty", ROOT / "scripts" / "fetch_counterparty.py")
    fc._http_get_настоящий = fc._http_get  # для теста транспорта на фейковом opener
    fc._http_get = _no_network
    fc._http_post = _no_network
    os.environ["COUNTERPARTY_IGNORE_ACCESS_CACHE"] = "1"
    cases = {
        "парсеры-против-raw": case_parsers(fc),
        "дрейф-схемы": case_schema_drift(fc),
        "инварианты-состояний": case_state_invariants(fc),
        "run_source-канарейка-probe-профиль": case_run_source(fc),
        "итог-проверки": case_summary(fc),
        "фазы-сбора": case_phases(fc),
        "режимы-и-ранний-выход": case_modes(fc),
        "параллельный-сбор": case_parallel(fc),
        "канарейка-потокобезопасна": case_canary_threadsafe(fc),
        "спецреестры-дисквалификация": case_special_registries(fc),
        "прокси-и-кэш-доступа": case_proxy(fc),
        "ефрсб-банкротство": case_bankrupt(fc),
        "федресурс-роли": case_fedresurs(fc),
    }
    failed = 0
    for name, errors in cases.items():
        if errors:
            failed += 1
            print("FAIL %s" % name)
            for e in errors:
                print("  - %s" % e)
        else:
            print("PASS %s" % name)
    if failed:
        print("FAIL: %d/%d кейсов упало" % (failed, len(cases)))
        return 1
    print("PASS: все %d кейсов зелёные" % len(cases))
    return 0


if __name__ == "__main__":
    sys.exit(main())
