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

Сети НЕТ: _http_get подменяется на исключение — любой сетевой вызов = FAIL.
Чистый stdlib, гоняется в CI.
"""

import copy
import importlib.util
import json
import os
import sys
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
    check(errors, n >= 5, "фикстур меньше пяти (%d)" % n)
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
        for sid in ("фссп", "суды", "банкротство"):
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


def main():
    fc = load_module("fetch_counterparty", ROOT / "scripts" / "fetch_counterparty.py")
    fc._http_get = _no_network
    os.environ["COUNTERPARTY_IGNORE_ACCESS_CACHE"] = "1"
    cases = {
        "парсеры-против-raw": case_parsers(fc),
        "дрейф-схемы": case_schema_drift(fc),
        "инварианты-состояний": case_state_invariants(fc),
        "run_source-канарейка-probe-профиль": case_run_source(fc),
        "итог-проверки": case_summary(fc),
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
