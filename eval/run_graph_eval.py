#!/usr/bin/env python3
"""
run_graph_eval.py — офлайн-прогон affiliates_graph + droblenie_check
по синтетическим ответам checko. PASS/FAIL, чистый stdlib, гоняется в CI.

Сценарий группы: 4 компании, общий директор/учредитель, единый адрес, УСН,
выручка 4×18 млн = 72 млн при пороге НДС 20 млн -> >=4 признаков, 🔴.
Контрольная «здоровая»: одиночная компания без связей -> 0-1 признак, 🟢.
"""

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "eval" / "fixtures" / "graph"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_fixture_fetcher():
    index = {}
    for f in sorted(FIXTURES.glob("checko_*.json")):
        j = json.loads(f.read_text(encoding="utf-8"))
        data = j.get("data", {})
        for key in (data.get("ОГРН"), data.get("ИНН")):
            if key:
                index[str(key)] = data

    def fetcher(kind, value):
        data = index.get(str(value))
        if data is None:
            return None, "в фикстурах нет %s=%s" % (kind, value)
        return data, "ok"
    return fetcher


def check(errors, cond, msg):
    if not cond:
        errors.append(msg)


def case_group(ag, dc, canon):
    errors = []
    graph = ag.build_graph(make_fixture_fetcher(), "7700000001", pause=0)
    check(errors, graph.get("статус") == "ок", "граф: статус %r" % graph.get("статус"))
    check(errors, graph.get("узлов") == 4, "узлов %r != 4" % graph.get("узлов"))
    типы = {r["тип"] for r in graph.get("рёбра", [])}
    for t in ("общий_директор", "общий_учредитель", "адрес"):
        check(errors, t in типы, "нет рёбер типа %s (есть: %s)" % (t, типы))
    # tier-маркировка: рёбра ⚠️ один источник, цель ✅ ЕГРЮЛ, оговорка в корне
    check(errors, bool(graph.get("оговорка")), "нет корневой «оговорка»")
    for r in graph.get("рёбра", []):
        check(errors, "⚠️" in (r.get("tier") or ""),
              "у ребра нет ⚠️-tier: %s" % r)
    целевые = [u for u in graph.get("узлы", []) if u.get("глубина") == 0]
    check(errors, len(целевые) == 1 and "✅" in (целевые[0].get("tier") or ""),
          "у целевого узла нет ✅-tier: %s" % (целевые or None))
    for u in graph.get("узлы", []):
        if u.get("глубина") != 0:
            check(errors, "⚠️" in (u.get("tier") or ""),
                  "у связанного узла нет ⚠️-tier: %s" % u.get("название"))

    revenues = json.loads((FIXTURES / "revenues_group.json").read_text())
    out = dc.score(graph, выручка_map=revenues, canon=canon)
    ids = {p["id"] for p in out.get("признаки", [])}
    check(errors, out.get("признаков_совпало", 0) >= 4,
          "признаков %r < 4 (%s)" % (out.get("признаков_совпало"), ids))
    for fid in ("общий_директор", "единый_адрес", "все_на_усн",
                "одинаковый_оквэд", "группа_над_порогом_ндс"):
        check(errors, fid in ids, "нет признака %s (есть: %s)" % (fid, ids))
    check(errors, out.get("severity") == "🔴",
          "severity %r != 🔴" % out.get("severity"))
    check(errors, "совпадающих с типовыми доводами ФНС" in out.get("оценка", ""),
          "оценка не по формулировочной дисциплине: %r" % out.get("оценка"))
    check(errors, "дробление подтверждено" not in json.dumps(out, ensure_ascii=False),
          "запрещённая формулировка «дробление подтверждено»")
    check(errors, "одного агрегатора" in out.get("предупреждение_источников", ""),
          "нет предупреждения об одном источнике: %r"
          % out.get("предупреждение_источников"))
    vg = out.get("выручка_группы", {})
    check(errors, vg.get("сумма") == 72000000,
          "выручка группы %r != 72000000" % vg.get("сумма"))
    check(errors, vg.get("покрыто_компаний") == "по 4 из 4 компаний",
          "покрытие %r" % vg.get("покрыто_компаний"))
    return errors


def case_healthy(ag, dc, canon):
    errors = []
    graph = ag.build_graph(make_fixture_fetcher(), "7700000099", pause=0)
    check(errors, graph.get("статус") == "ок", "граф: статус %r" % graph.get("статус"))
    check(errors, graph.get("узлов") == 1, "узлов %r != 1" % graph.get("узлов"))
    revenues = json.loads((FIXTURES / "revenues_healthy.json").read_text())
    out = dc.score(graph, выручка_map=revenues, canon=canon)
    check(errors, out.get("признаков_совпало", 9) <= 1,
          "признаков %r > 1" % out.get("признаков_совпало"))
    check(errors, out.get("severity") == "🟢",
          "severity %r != 🟢" % out.get("severity"))
    return errors


def case_offline_graph(ag, dc, canon):
    """Офлайн-вход: граф, собранный build_graph, подаём обратно через
    normalize_offline — checko заменяем ручным слоем, droblenie работает."""
    errors = []
    graph = ag.build_graph(make_fixture_fetcher(), "7700000001", pause=0)
    # имитируем ручной сбор: убираем tier/оговорку, как будто файл собран руками
    ручной = {"узлы": [{k: v for k, v in u.items() if k != "tier"}
                       for u in graph["узлы"]],
              "рёбра": [{k: v for k, v in r.items() if k != "tier"}
                        for r in graph["рёбра"]]}
    off = ag.normalize_offline(ручной, имя_файла="тест.json")
    check(errors, off.get("статус") == "ок", "offline: статус %r" % off.get("статус"))
    check(errors, bool(off.get("оговорка")), "offline: нет «оговорка»")
    for r in off.get("рёбра", []):
        check(errors, "⚠️" in (r.get("tier") or ""),
              "offline: у ребра нет ⚠️-tier: %s" % r)
    целевые = [u for u in off.get("узлы", []) if u.get("глубина") == 0]
    check(errors, len(целевые) == 1 and "✅" in (целевые[0].get("tier") or ""),
          "offline: у цели нет ✅-tier")
    revenues = json.loads((FIXTURES / "revenues_group.json").read_text())
    out = dc.score(off, выручка_map=revenues, canon=canon)
    check(errors, out.get("признаков_совпало", 0) >= 4,
          "offline: признаков %r < 4" % out.get("признаков_совпало"))
    check(errors, "одного агрегатора" in out.get("предупреждение_источников", ""),
          "offline: нет предупреждения об одном источнике")
    # битый вход -> честное «не проверено»
    bad = ag.normalize_offline({"узлы": "не-список"})
    check(errors, bad.get("статус") == "не проверено",
          "битый offline-вход: статус %r" % bad.get("статус"))
    return errors


def case_no_canon(ag, dc):
    errors = []
    graph = ag.build_graph(make_fixture_fetcher(), "7700000001", pause=0)
    revenues = json.loads((FIXTURES / "revenues_group.json").read_text())
    out = dc.score(graph, выручка_map=revenues, canon=None)
    статус = out.get("выручка_группы", {}).get("статус", "")
    check(errors, "не проверено" in статус,
          "без канона выручка должна быть «не проверено», есть %r" % статус)
    return errors



# --- ленивый обход (задача 20) -----------------------------------------------

def считающий_fetcher(статусы=None):
    """Фикстурный fetcher + счётчик запросов + подмена статуса у узла.

    Подмена нужна, чтобы проверить остановку на значимом сигнале, не заводя
    отдельный набор фикстур: меняется ровно одно поле карточки.
    """
    базовый = make_fixture_fetcher()
    счёт = {"n": 0}

    def fetcher(kind, value):
        счёт["n"] += 1
        data, note = базовый(kind, value)
        if data is not None and статусы and str(value) in статусы:
            data = json.loads(json.dumps(data))
            data["Статус"] = {"Наим": статусы[str(value)]}
        return data, note
    return fetcher, счёт


def case_lazy_signal(ag):
    """Стоп на значимом сигнале: ликвидация у связанной — уже ответ."""
    errors = []
    f, счёт = считающий_fetcher({"1027700000002": "Ликвидировано"})
    g = ag.build_graph(f, "7700000001", pause=0)
    обход = g.get("обход") or {}
    check(errors, обход.get("полный") is False,
          "стоп на сигнале: обход.полный %r, ожидался False" % обход.get("полный"))
    check(errors, "ликвид" in (обход.get("причина_остановки") or "").lower(),
          "причина остановки не называет статус: %r" % обход.get("причина_остановки"))
    check(errors, g.get("усечено") is True,
          "ранняя остановка обязана поднимать «усечено» (на него смотрит "
          "droblenie_check), а там %r" % g.get("усечено"))
    сигналы = g.get("значимые_сигналы") or []
    check(errors, len(сигналы) == 1 and сигналы[0].get("глубина") == 1,
          "значимые_сигналы: %r" % сигналы)
    check(errors, any("не развёрнут" in s for s in g.get("не_проверено") or []),
          "неразвёрнутые связи не попали в «не_проверено»: %r" % g.get("не_проверено"))
    лениво = счёт["n"]

    # Контроль: без стопа обход идёт дальше и стоит дороже — ради этого всё
    f2, счёт2 = считающий_fetcher({"1027700000002": "Ликвидировано"})
    g2 = ag.build_graph(f2, "7700000001", pause=0, стоп_на_сигнале=False)
    check(errors, (g2.get("обход") or {}).get("полный") is True,
          "без стопа обход должен дойти до конца: %r" % g2.get("обход"))
    check(errors, счёт2["n"] > лениво,
          "ленивый обход не сэкономил ни одного запроса: %d против %d"
          % (лениво, счёт2["n"]))
    check(errors, g2.get("узлов", 0) > g.get("узлов", 0),
          "полный обход не нашёл больше узлов: %r против %r"
          % (g2.get("узлов"), g.get("узлов")))
    return errors


def case_lazy_budget(ag):
    """Бюджет запросов: платный ключ не выкачивает полсотни компаний молча."""
    errors = []
    f, счёт = считающий_fetcher()
    g = ag.build_graph(f, "7700000001", pause=0, бюджет=2)
    обход = g.get("обход") or {}
    check(errors, счёт["n"] <= 2, "бюджет 2 превышен: сделано %d" % счёт["n"])
    check(errors, обход.get("запросов_сделано") == счёт["n"],
          "счётчик запросов врёт: в выводе %r, на деле %d"
          % (обход.get("запросов_сделано"), счёт["n"]))
    check(errors, обход.get("полный") is False and "бюджет" in
          (обход.get("причина_остановки") or ""),
          "остановка по бюджету не названа: %r" % обход.get("причина_остановки"))
    check(errors, g.get("статус") == "ок",
          "обход по бюджету обязан отдать частичный граф, а не ошибку: %r"
          % g.get("статус"))
    return errors


def case_lazy_intersection(ag):
    """«Есть ли пересечение с уже известными» — ради этого обход и затевался."""
    errors = []
    f, _ = считающий_fetcher()
    g = ag.build_graph(f, "7700000001", pause=0, известные=["7700000003"])
    пересечение = g.get("пересечение_с_известными") or {}
    найдено = пересечение.get("найдено") or []
    check(errors, len(найдено) == 1 and найдено[0].get("инн") == "7700000003",
          "пересечение не найдено: %r" % найдено)
    check(errors, "остановлен" in (пересечение.get("полнота") or ""),
          "полнота не говорит, что смотрели не всё: %r" % пересечение.get("полнота"))

    # Пустой результат при неполном обходе — не «пересечений нет»
    f2, _ = считающий_fetcher()
    g2 = ag.build_graph(f2, "7700000001", pause=0, известные=["7799999999"],
                        бюджет=2)
    п2 = g2.get("пересечение_с_известными") or {}
    check(errors, (п2.get("найдено") == []) and "остановлен" in (п2.get("полнота") or ""),
          "пустое пересечение на усечённом обходе обязано нести оговорку: %r" % п2)

    # Без «известных» блока нет вовсе — пустой список читался бы как «нет»
    f3, _ = считающий_fetcher()
    g3 = ag.build_graph(f3, "7700000001", pause=0)
    check(errors, g3.get("пересечение_с_известными") is None,
          "без списка известных блок пересечения должен отсутствовать, а он %r"
          % g3.get("пересечение_с_известными"))
    return errors


def case_droblenie_on_partial(ag, dc, canon):
    """🟢 по половине группы не должен читаться как 🟢 по группе."""
    errors = []
    f, _ = считающий_fetcher()
    частичный = ag.build_graph(f, "7700000001", pause=0, бюджет=2)
    out = dc.score(частичный, выручка_map=None, canon=canon)
    check(errors, isinstance(out.get("граф_неполон"), dict),
          "дробление по усечённому графу молчит о неполноте: ключа «граф_неполон» нет")
    check(errors, any("граф неполон" in s for s in out.get("не_проверено") or []),
          "неполнота не попала в «не_проверено»: %r" % out.get("не_проверено"))

    полный = ag.build_graph(make_fixture_fetcher(), "7700000001", pause=0)
    out2 = dc.score(полный, выручка_map=None, canon=canon)
    check(errors, "граф_неполон" not in out2,
          "на полном графе пометка о неполноте лишняя: %r" % out2.get("граф_неполон"))
    return errors



def case_lazy_known_file(ag, tmp):
    """Файл с прозой не должен молча стать «списком известных»."""
    errors = []
    import pathlib as _p
    списком = _p.Path(tmp) / "изв.txt"
    списком.write_text("7700000003\n# комментарий\n1027700000004\n", encoding="utf-8")
    значения, ош, мусор = ag._известные_из(str(списком))
    check(errors, ош is None and значения == ["7700000003", "1027700000004"],
          "простой список не разобрался: %r / %r" % (значения, ош))

    объектом = _p.Path(tmp) / "снимки.json"
    объектом.write_text('{"7700000003": {"дата": "2026-09-22"}}', encoding="utf-8")
    значения2, ош2, _ = ag._известные_из(str(объектом))
    check(errors, ош2 is None and значения2 == ["7700000003"],
          "снимки как объект не разобрались: %r / %r" % (значения2, ош2))

    проза = _p.Path(tmp) / "проза.txt"
    проза.write_text("список контрагентов\nООО «Ромашка»\n", encoding="utf-8")
    значения3, ош3, мусор3 = ag._известные_из(str(проза))
    check(errors, значения3 is None and ош3 and len(мусор3) == 2,
          "проза принята за список известных: %r / %r" % (значения3, ош3))

    один = _p.Path(tmp) / "один.txt"
    один.write_text("7700000003\n", encoding="utf-8")
    значения4, ош4, _ = ag._известные_из(str(один))
    check(errors, ош4 is None and значения4 == ["7700000003"],
          "файл из одной строки (валидный JSON-int) не разобрался: %r / %r"
          % (значения4, ош4))
    return errors


def case_explainable_metadata(ag):
    errors = []
    graph = ag.build_graph(make_fixture_fetcher(), '7700000001', pause=0)
    for edge in graph.get('рёбра', []):
        check(errors, all(k in edge for k in ('edge_type', 'evidence_ids', 'effective_at', 'origin_family')), 'missing edge provenance')
        if edge['тип'] == 'адрес':
            check(errors, edge.get('edge_type') == 'common_address', 'address promoted to ownership')
        check(errors, edge.get('evidence_ids') == [], 'invented evidence IDs for legacy fixtures')
        check(errors, edge.get('evidence_status') == 'unknown', 'unsupported raw pointer not unknown')
    check(errors, graph.get('ownership_chains') == [], 'ownership inferred from shared director/address')
    return errors


def case_frontier_limits(ag):
    errors = []
    cards = {
        'a': {'ОГРН': 'a', 'ИНН': '7700000001', 'ЮрАдрес': {'МассАдрес': ['b', 'c']}},
        'b': {'ОГРН': 'b', 'ЮрАдрес': {'МассАдрес': ['d']}},
        'c': {'ОГРН': 'c'}, 'd': {'ОГРН': 'd'},
    }
    def fetch(kind, value):
        return cards.get('a' if kind == 'inn' else value), 'unavailable'
    for kwargs, reason in [({'max_depth': 1}, 'depth'), ({'бюджет': 1}, 'budget'), ({'max_nodes': 1}, 'max_nodes')]:
        g = ag.build_graph(fetch, '7700000001', pause=0, **kwargs)
        check(errors, g.get('усечено') is True, 'limit silently complete: %s' % reason)
        check(errors, any(x.get('stop_reason') == reason for x in g.get('frontier', [])), 'missing frontier reason: %s' % reason)
    del cards['c']
    g = ag.build_graph(fetch, '7700000001', pause=0)
    check(errors, any(x.get('stop_reason') == 'unavailable' for x in g.get('frontier', [])), 'missing unavailable frontier')
    check(errors, g.get('усечено') is True, 'unavailable reported complete')
    off = ag.normalize_offline(g)
    check(errors, off.get('усечено') is True and off.get('frontier') == g.get('frontier'), 'offline erased truncation')
    return errors


def case_offline_ownership_guard(ag):
    errors = []
    data = {'узлы': [{'огрн': 'a', 'глубина': 0}, {'огрн': 'b'}],
            'рёбра': [{'от': 'a', 'до': 'b', 'тип': 'адрес', 'edge_type': 'ownership', 'evidence_ids': ['invented']}]}
    result = ag.normalize_offline(data)
    edge = result['рёбра'][0]
    check(errors, edge.get('edge_type') == 'common_address', 'offline address promoted by supplied metadata')
    check(errors, edge.get('evidence_ids') == [], 'dangling graph evidence accepted')
    check(errors, result.get('ownership_chains') == [], 'unsupported ownership chain accepted')
    check(errors, result.get('усечено') is True, 'legacy offline completeness invented')
    return errors


def case_supported_ownership_and_mutation(ag):
    import copy
    evidence = ag._load_sibling('evidence_contract')
    errors = []
    traced = evidence.enrich({'инн': '7700000001'}, now='2026-10-16T09:00:00Z')['_evidence']
    claim = copy.deepcopy(next(iter(traced['claims'].values())))
    claim.update(field='graph.ownership', value={'from': 'a', 'to': 'b', 'edge_type': 'ownership'},
                 knowledge='known', reason_code=None, source_id='manual', origin_family='egrul',
                 raw_ref={'response_id': 'saved1', 'sha256': 'b' * 64, 'locator': '/owner', 'availability': 'available'},
                 effective_at='2026-10-01T00:00:00Z', depends_on=[])
    claim['claim_id'] = evidence._claim_id(claim)
    traced['claims'] = {claim['claim_id']: claim}
    data = {'узлы': [{'огрн': 'a', 'глубина': 0}, {'огрн': 'b'}],
            'рёбра': [{'от': 'a', 'до': 'b', 'тип': 'владелец', 'evidence_ids': [claim['claim_id']]}],
            '_evidence': traced, 'усечено': False, 'обход': {'полный': True}}
    result = ag.normalize_offline(data)
    check(errors, len(result.get('ownership_chains', [])) == 1, 'supported legal ownership missing')
    check(errors, result['рёбра'][0].get('effective_at') == '2026-10-01T00:00:00Z', 'effective date lost')
    changed = copy.deepcopy(data)
    changed['рёбра'][0]['до'] = 'c'
    mutated = ag.normalize_offline(changed)
    check(errors, mutated.get('ownership_chains') == [], 'foreign endpoints accepted as supported')
    changed = copy.deepcopy(data)
    changed['_evidence']['claims'][claim['claim_id']]['value']['to'] = 'c'
    check(errors, ag.normalize_offline(changed).get('ownership_chains') == [], 'mutated claim accepted')
    # Checko legal entities produce explicit legal ownership but no invented raw reference.
    cards = {'a': {'ОГРН': 'a', 'ИНН': '7700000001', 'Учред': {'ЮЛ': [{'ОГРН': 'b'}]}}, 'b': {'ОГРН': 'b'}}
    graph = ag.build_graph(lambda kind, value: (cards.get('a' if kind == 'inn' else value), 'ok'), '7700000001', pause=0)
    check(errors, graph['рёбра'][0]['edge_type'] == 'ownership', 'legal owner confused with shared founder')
    check(errors, graph['ownership_chains'] == [], 'untraced Checko relationship used as supported chain')
    # Presentation keeps source links and unexpanded frontier visible.
    dossier = ag._load_sibling('dossier')
    rendered = json.dumps(dossier._подраздел_граф(ag.build_graph(make_fixture_fetcher(), '7700000001', pause=0, бюджет=1)), ensure_ascii=False)
    check(errors, 'budget' in rendered and 'неразвёрнутые' in rendered, 'dossier hides frontier')
    check(errors, 'не доказывают владение' in rendered, 'dossier equates associations with control')
    return errors


def main():
    import tempfile
    ag = load_module("affiliates_graph", ROOT / "scripts" / "affiliates_graph.py")
    dc = load_module("droblenie_check", ROOT / "scripts" / "droblenie_check.py")
    canon = dc._load_canon()
    if canon is None:
        print("FAIL: канон data/canon_ru.json не читается")
        return 1
    with tempfile.TemporaryDirectory() as td:
        cases = _собрать(ag, dc, canon, td)
    return _итог(cases)


def case_unresolved_legal_founders(ag):
    errors = []
    for founder in ({'ИНН': '7702070139', 'НаимПолн': 'Known owner without OGRN'},
                    {'НаимПолн': 'Named unresolved owner'}, {}, None, [], 'malformed'):
        data = {'ИНН': '7707083893', 'ОГРН': '1027700132195', 'Учред': {'ЮЛ': [founder]}}
        calls = []
        def fetch(*args, calls=calls, data=data):
            calls.append(args)
            return data, 'ok'
        graph = ag.build_graph(fetch, '7707083893', pause=0)
        check(errors, graph.get('усечено') is True, 'unresolved founder silently complete: %r' % founder)
        check(errors, graph['обход'].get('полный') is False, 'full traversal claimed for unresolved founder')
        check(errors, bool(graph.get('frontier')) and graph.get('stop_reason') == 'unavailable', 'missing unresolved frontier')
        check(errors, bool(graph.get('не_проверено')), 'missing founder parsing limitation')
        check(errors, graph.get('ownership_chains') == [], 'unresolved owner promoted to proven chain')
        check(errors, len(calls) == 1, 'unresolved owner invented OGRN/network traversal')
        if isinstance(founder, dict) and founder.get('НаимПолн'):
            check(errors, any(item.get('edge_type') == 'ownership' and item.get('name') == founder['НаимПолн']
                              for item in graph.get('frontier', [])), 'named legal ownership relation lost')
        if isinstance(founder, dict) and founder.get('ИНН'):
            check(errors, any(item.get('to') == founder['ИНН'] for item in graph.get('frontier', [])), 'known owner INN lost')
        normalized = ag.normalize_offline(graph)
        check(errors, normalized.get('усечено') is True, 'normalization erased unresolved founder')
    return errors


def _собрать(ag, dc, canon, td):
    return {
        "unresolved-legal-founders": case_unresolved_legal_founders(ag),
        "edge-provenance": case_explainable_metadata(ag),
        "supported-ownership-mutation": case_supported_ownership_and_mutation(ag),
        "frontier-limits": case_frontier_limits(ag),
        "offline-ownership-guard": case_offline_ownership_guard(ag),
        "группа-4-компании": case_group(ag, dc, canon),
        "здоровая-контрольная": case_healthy(ag, dc, canon),
        "offline-граф": case_offline_graph(ag, dc, canon),
        "канон-отсутствует": case_no_canon(ag, dc),
        "ленивый-стоп-на-сигнале": case_lazy_signal(ag),
        "ленивый-бюджет-запросов": case_lazy_budget(ag),
        "ленивый-пересечение": case_lazy_intersection(ag),
        "дробление-по-неполному-графу": case_droblenie_on_partial(ag, dc, canon),
        "разбор-файла-известных": case_lazy_known_file(ag, td),
    }


def _итог(cases):
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
