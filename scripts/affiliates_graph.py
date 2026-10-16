#!/usr/bin/env python3
"""
affiliates_graph.py — граф связей контрагента (общие директора/учредители,
соседи по адресу, правопреемство) через checko API. Только stdlib.

Использование:
    export CHECKO_API_KEY=...        # бесплатно после регистрации: checko.ru/integration/api
    python3 affiliates_graph.py <ИНН>
    python3 affiliates_graph.py --offline-граф файл.json   # связи, собранные вручную

Без ключа — честный JSON «не проверено» (exit 0), quick-scan не ломается.
--offline-граф принимает заранее собранные узлы/рёбра из любого источника
(браузер, другой агрегатор) в том же формате — checko заменяемый слой,
а не требование.

Логика: целевая компания -> директор + учредители -> их компании
(СвязРуковод/СвязУчред) + соседи по адресу (МассАдрес) + правопреемство.
BFS на глубину 2 (компании связанных лиц, дальше не углубляемся), лимит
MAX_NODES узлов — при превышении пометка «усечено». Схема ответа checko
(v2/company) взята из документации 19.09.2026; парсинг defensive — всё, что
не распарсилось, уходит в «не_проверено».

Доверие: принцип «одиночному источнику не верим» действует и здесь — каждый
узел/ребро получает tier (⚠️ один источник checko / ✅ ЕГРЮЛ для цели),
корневой блок — «оговорка» о необходимости пересечения вторым источником.

Выход JSON: узлы (карточки с типом связи) + рёбра {от, до, тип, через, tier},
дата построения, источник. Граф — вход для droblenie_check.py.
"""

import copy
import json
import os
import pathlib
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def _ssl_context():
    """Общий TLS-контекст движка: fetch_counterparty._build_ssl_context() подхватывает
    корень УЦ Минцифры (scripts/install_ca.py) и COUNTERPARTY_CA_BUNDLE; без него
    fedsfm.ru и rosstat.gov.ru падают на верификации. Верификация всегда включена."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from fetch_counterparty import _build_ssl_context
        return _build_ssl_context()
    except Exception:
        return ssl.create_default_context()


try:
    from sibling_imports import load_sibling as _load_sibling
except ModuleNotFoundError as _error:
    if _error.name != "sibling_imports":
        raise
    import importlib.util as _import_util
    import sys as _import_sys
    from pathlib import Path as _ImportPath
    _spec = _import_util.spec_from_file_location(
        "sibling_imports", _ImportPath(__file__).resolve().with_name("sibling_imports.py"))
    _helper = _import_util.module_from_spec(_spec)
    _spec.loader.exec_module(_helper)
    # Publish only the complete, stateless bootstrap. Concurrent bootstrap calls
    # all select the same helper; dependency loads use Python's module locks.
    _load_sibling = _import_sys.modules.setdefault("sibling_imports", _helper).load_sibling


API_URL = "https://api.checko.ru/v2/company"
MAX_NODES = 100
MAX_DEPTH = 2
TIMEOUT = 25
PAUSE = 0.4  # вежливая задержка между запросами к API
UA = "inn-check-ru/1.3 (affiliates_graph)"

# Принцип проекта «одиночному источнику не верим» распространяется и на граф:
# рёбра из checko — ⚠️ один источник, целевая карточка сверяется с ЕГРЮЛ — ✅.
TIER_CHECKO = "⚠️ один источник (checko API), требует пересечения"
TIER_OFFLINE = "⚠️ ручной сбор (offline-граф), требует пересечения"
TIER_TARGET = "✅ ЕГРЮЛ"
ОГОВОРКА = ("рёбра графа построены по одному агрегатору; перед решением "
            "по группе пересечите ключевые связи вторым источником "
            "(ЕГРЮЛ-выписка, rusprofile, СБИС)")

# Ленивый обход (задача 20). Полный граф на 30 колен не нужен и не бесплатен:
# ключ checko платный за объём, а инструмент, выкачавший полсотни компаний на
# вопрос «кто за поставщиком», задаёт вопрос «зачем». Обход идёт в ширину и
# останавливается на первом из условий: бюджет запросов, значимый сигнал у
# найденного узла, пересечение с уже известными, заданная глубина.
БЮДЖЕТ_ЗАПРОСОВ = 25
# Статусы, ради которых дальше идти незачем: если у связанной компании
# ликвидация или банкротство, это уже ответ, а не промежуточное звено.
ЗНАЧИМЫЕ_МАРКЕРЫ = ("ликвид", "банкрот", "прекращ", "исключ", "недейств",
                    "реорганиз")


def fetch_company(key, inn=None, ogrn=None, *, policy=None, ledger=None):
    """Policy-governed graph request; CLI shares one ledger across its traversal."""
    policy_module = _load_sibling("provider_policy")
    policy = policy if policy is not None else policy_module.configured_policy()
    if policy is None:
        return _fetch_company_transport(key, inn=inn, ogrn=ogrn)
    policy = copy.deepcopy(policy)
    for entry in policy["providers"]:
        entry["request_limit"] = min(entry["request_limit"], БЮДЖЕТ_ЗАПРОСОВ)
    def request(provider, subject, fields):
        # DaData has no graph capability and is refused before this callback.
        data, note = _fetch_company_transport(key, inn=inn, ogrn=ogrn)
        if data is not None:
            safe = _load_sibling("redaction").redact(data, secrets=(key,))
            return {"status":"ok", "data":{"graph":safe}, "requests_used":1}
        match = re.search(r"HTTP (401|402|403|429|500|502|503|504)\b", note or "")
        reason = "http_" + match.group(1) if match else "unavailable"
        return {"status":"error", "data":None, "reason_code":reason, "requests_used":1}
    result = policy_module.fetch_with_policy(str(inn or ogrn), ["graph"], policy,
                request=request, ledger=ledger if ledger is not None else {})
    if ledger is not None:
        ledger.setdefault("transfers", []).extend(result["transfers"])
    if result["data"] is None:
        return None, "бюджет/политика провайдера: " + ", ".join(result["limitations"])
    return result["data"]["graph"], "ok"


def _fetch_company_transport(key, inn=None, ogrn=None):
    """Один запрос к checko v2/company. Возвращает (data|None, заметка)."""
    params = {"key": key}
    if ogrn:
        params["ogrn"] = str(ogrn)
    else:
        params["inn"] = str(inn)
    url = API_URL + "?" + urllib.parse.urlencode(params)
    ctx = _ssl_context()
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as resp:
            j = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return None, "checko HTTP %s" % e.code
    except Exception as e:
        return None, "checko недоступен (%s)" % type(e).__name__
    if not isinstance(j, dict):
        return None, "checko вернул не-объект"
    meta = j.get("meta") if isinstance(j.get("meta"), dict) else {}
    if meta.get("status") == "error":
        return None, "checko error: %s" % meta.get("message", "?")
    data = j.get("data")
    if not isinstance(data, dict):
        return None, "нет data (%s)" % (meta.get("message") or "компания не найдена?")
    return data, "ok"


def _g(d, *path):
    cur = d
    for key in path:
        if isinstance(cur, dict):
            cur = cur.get(key)
        else:
            return None
    return cur


def _str_list(x):
    if not isinstance(x, list):
        return []
    return [str(v) for v in x if v is not None and str(v).strip()]


def parse_card(data):
    """Defensive-разбор карточки checko в плоский вид графа.

    Возвращает (карточка, не_распарсилось[]). Имена полей — по документации
    v2/company от 19.09.2026; всё неузнанное — в не_распарсилось, не в выдумку.
    """
    gaps = []
    card = {
        "огрн": _g(data, "ОГРН"),
        "инн": _g(data, "ИНН"),
        "название": _g(data, "НаимСокр") or _g(data, "НаимПолн"),
        "статус": _g(data, "Статус", "Наим"),
        "оквэд": _g(data, "ОКВЭД", "Код"),
        "адрес": _g(data, "ЮрАдрес", "АдресРФ"),
        "спецрежим": _str_list(_g(data, "Налоги", "ОсобРежим")),
        "ссч": _g(data, "СЧР"),
        "руководители": [],
        "учредители": [],
        "учредители_юл": [],
        "масс_адрес": _str_list(_g(data, "ЮрАдрес", "МассАдрес")),
        "правопреемство": [],
    }
    руковод = data.get("Руковод")
    if isinstance(руковод, list):
        for p in руковод:
            if isinstance(p, dict):
                card["руководители"].append({
                    "фио": _g(p, "ФИО"),
                    "связ_руковод": _str_list(_g(p, "СвязРуковод")),
                    "связ_учред": _str_list(_g(p, "СвязУчред")),
                })
    elif руковод is not None:
        gaps.append("Руковод: неожиданный формат")
    фл = _g(data, "Учред", "ФЛ")
    if isinstance(фл, list):
        for p in фл:
            if isinstance(p, dict):
                card["учредители"].append({
                    "фио": _g(p, "ФИО"),
                    "связ_руковод": _str_list(_g(p, "СвязРуковод")),
                    "связ_учред": _str_list(_g(p, "СвязУчред")),
                })
    elif фл is not None:
        gaps.append("Учред.ФЛ: неожиданный формат")
    legal_founders = _g(data, "Учред", "ЮЛ")
    if isinstance(legal_founders, list):
        for owner in legal_founders:
            if not isinstance(owner, dict):
                gaps.append("Учред.ЮЛ: неожиданный формат записи")
                continue
            ogrn = owner.get("ОГРН")
            if type(ogrn) not in (str, int) or not str(ogrn).strip():
                ogrn = None
                gaps.append("Учред.ЮЛ: связь владельца без ОГРН не развёрнута")
            card["учредители_юл"].append({
                "огрн": str(ogrn) if ogrn is not None else None,
                "инн": owner.get("ИНН") if isinstance(owner.get("ИНН"), str) else None,
                "название": owner.get("НаимПолн") if isinstance(owner.get("НаимПолн"), str) else None})
    elif legal_founders is not None:
        gaps.append("Учред.ЮЛ: неожиданный формат")
    for поле in ("Правопреем", "Правопредш"):
        список = data.get(поле)
        if isinstance(список, list):
            for o in список:
                if isinstance(o, dict) and _g(o, "ОГРН"):
                    card["правопреемство"].append({
                        "огрн": _g(o, "ОГРН"),
                        "инн": _g(o, "ИНН"),
                        "название": _g(o, "НаимПолн"),
                    })
    return card, gaps


def _значимый(card):
    """Статус узла, ради которого дальше идти незачем (ликвидация, банкротство)."""
    статус = (card.get("статус") or "").lower()
    for м in ЗНАЧИМЫЕ_МАРКЕРЫ:
        if м in статус:
            return card.get("статус")
    return None


def _ключи(card):
    """ИНН и ОГРН узла строками — по ним ищется пересечение с известными."""
    return {str(v) for v in (card.get("инн"), card.get("огрн")) if v}


def _edge_type(legacy):
    return {'адрес': 'common_address', 'общий_директор': 'common_director',
            'общий_учредитель': 'common_founder', 'правопреемство': 'succession',
            'владелец': 'ownership'}.get(legacy, 'unknown')


def _edge_metadata(edge, evidence=None, origin=None):
    """Only exact relationship claims can support an edge. Never mint IDs.

    For ownership, direction is company -> its legal owner (owned_by). Shared
    natural-person founders/directors are associations, not an ownership chain.
    A legacy Checko response has no canonical raw pointer and stays untraced.
    """
    edge = dict(edge)
    edge['edge_type'] = _edge_type(edge.get('тип'))
    claims = (evidence or {}).get('claims', {})
    supported = []
    requested = edge.get('evidence_ids', [])
    for cid in requested if isinstance(requested, list) else []:
        claim = claims.get(cid) if isinstance(cid, str) else None
        if not isinstance(claim, dict):
            continue
        try:
            issues = _load_sibling('evidence_contract').validate_evidence(
                {'инн': claim.get('subject_inn'), '_evidence': evidence})
        except (TypeError, ValueError, KeyError):
            continue
        if issues or claim.get('knowledge') != 'known' or claim.get('raw_ref', {}).get('availability') != 'available':
            continue
        if (claim.get('field') == 'graph.' + edge['edge_type'] and
                claim.get('value') == {'from': edge['от'], 'to': edge['до'], 'edge_type': edge['edge_type']}):
            supported.append(claim)
    edge['evidence_ids'] = sorted({c['claim_id'] for c in supported})
    dates = {c.get('effective_at') for c in supported}
    origins = {c.get('origin_family') for c in supported}
    edge['effective_at'] = next(iter(dates)) if len(dates) == 1 else None
    edge['origin_family'] = next(iter(origins)) if len(origins) == 1 else origin
    edge['evidence_status'] = 'supported' if supported else 'unknown'
    edge['source_pointer_status'] = 'available' if supported else 'raw_unavailable'
    return edge


def _ownership_chains(edges):
    # Each displayed segment must itself be supported; no beneficial-owner or
    # control conclusion follows from this legal ownership path.
    supported = [e for e in edges if e.get('edge_type') == 'ownership' and e.get('evidence_status') == 'supported']
    return [{'nodes': [e['от'], e['до']], 'direction': 'owned_by',
             'evidence_ids': e['evidence_ids'], 'effective_at': e['effective_at']}
            for e in supported]


def _edges_of(card):
    out = []
    for person in card.get('руководители', []):
        out.extend(('общий_директор', ogrn, person.get('фио'))
                   for key in ('связ_руковод', 'связ_учред') for ogrn in person.get(key, []))
    for person in card.get('учредители', []):
        out.extend(('общий_учредитель', ogrn, person.get('фио'))
                   for key in ('связ_учред', 'связ_руковод') for ogrn in person.get(key, []))
    out.extend(('адрес', ogrn, None) for ogrn in card.get('масс_адрес', []))
    out.extend(('правопреемство', item['огрн'], None) for item in card.get('правопреемство', []))
    out.extend(('владелец', item['огрн'], None) for item in card.get('учредители_юл', []) if item.get('огрн'))
    return out


def build_graph(fetcher, inn, max_nodes=MAX_NODES, max_depth=MAX_DEPTH,
                pause=PAUSE, бюджет=БЮДЖЕТ_ЗАПРОСОВ, стоп_на_сигнале=True,
                известные=None):
    """Bounded BFS with explicit unexplored frontier; legacy JSON preserved."""
    бюджет = max(0, min(int(бюджет), БЮДЖЕТ_ЗАПРОСОВ))
    max_nodes = max(1, min(int(max_nodes), MAX_NODES))
    max_depth = max(0, int(max_depth))
    известные = {str(x) for x in (известные or []) if x}
    requests = 0

    def request(kind, value):
        nonlocal requests
        if requests >= бюджет:
            return None, 'бюджет запросов исчерпан (%d)' % бюджет
        requests += 1
        return fetcher(kind, value)

    data, note = request('inn', inn)
    if data is None:
        reason = 'budget' if requests >= бюджет and ('бюджет' in (note or '') or not бюджет) else 'unavailable'
        return {'статус': 'не проверено', 'причина': note, 'источник': 'checko API (%s)' % API_URL,
                'усечено': True, 'frontier': [{'from': None, 'to': inn, 'depth': 0, 'stop_reason': reason}],
                'stop_reason': reason, 'ownership_chains': []}
    target, gaps = parse_card(data)
    target_id = str(target.get('огрн') or target.get('инн') or '')
    if not target_id:
        return {'статус': 'не проверено', 'причина': 'карточка цели без ОГРН/ИНН',
                'источник': 'checko API (%s)' % API_URL, 'усечено': True,
                'frontier': [{'from': None, 'to': inn, 'depth': 0, 'stop_reason': 'unavailable'}],
                'stop_reason': 'unavailable', 'ownership_chains': []}
    nodes, edges, frontier, unchecked = {}, [], [], list(gaps)
    edge_keys, failed = set(), set()
    signals, intersections = [], []
    stop, stop_note = None, None

    def add(card, depth, relation=None):
        identity = str(card.get('огрн') or card.get('инн') or '')
        if not identity:
            return None
        if identity not in nodes:
            nodes[identity] = {key: card.get(key) for key in
                               ('огрн', 'инн', 'название', 'статус', 'оквэд', 'адрес', 'спецрежим', 'ссч')}
            nodes[identity].update(глубина=depth, типы_связи=[], tier=TIER_TARGET if depth == 0 else TIER_CHECKO)
        relation = relation or 'цель'
        if relation not in nodes[identity]['типы_связи']:
            nodes[identity]['типы_связи'].append(relation)
        return identity

    def pending(source, destination, depth, reason, **details):
        item = {'from': source, 'to': destination, 'depth': depth, 'stop_reason': reason}
        item.update(details)
        if item not in frontier:
            frontier.append(item)
            unchecked.append('связи компании %s не развёрнуты: %s' % (destination, reason))

    def mark_intersection(card, depth):
        match = _ключи(card) & известные
        if match:
            intersections.append({'огрн': card.get('огрн'), 'инн': card.get('инн'),
                                  'название': card.get('название'), 'совпало_по': sorted(match), 'глубина': depth})
        return match

    add(target, 0)
    if gaps:
        pending(target_id, target_id, 0, 'unavailable')
    if mark_intersection(target, 0):
        stop, stop_note = 'intersection', 'цель уже есть среди известных'
    queue = [(target_id, target, 0)]
    for from_id, card, depth in queue:
        for owner in card.get('учредители_юл', []):
            if owner.get('огрн') is None:
                # Preserve the reported legal-owner relationship explicitly.
                # This OGRN-based traversal cannot resolve it; no ID is invented.
                pending(from_id, owner.get('инн') or from_id, depth + 1, 'unavailable',
                        edge_type='ownership', name=owner.get('название'), inn=owner.get('инн'),
                        reason_code='owner_ogrn_unavailable', evidence_status='unknown')
        relations = _edges_of(card)
        for legacy, to_value, via in relations:
            to_ogrn = str(to_value)
            to_id = to_ogrn if to_ogrn in nodes else None
            if stop:
                pending(from_id, to_ogrn, depth + 1, stop)
                continue
            if to_id is None:
                reason = ('depth' if depth >= max_depth else 'max_nodes' if len(nodes) >= max_nodes
                          else 'budget' if requests >= бюджет else 'unavailable' if to_ogrn in failed else None)
                if reason:
                    pending(from_id, to_ogrn, depth + 1, reason)
                    continue
                child_data, note = request('ogrn', to_ogrn)
                if child_data is None:
                    failed.add(to_ogrn)
                    reason = 'budget' if 'бюджет' in (note or '') else 'unavailable'
                    pending(from_id, to_ogrn, depth + 1, reason)
                    unchecked.append('ОГРН %s: карточка не получена (%s)' % (to_ogrn, note))
                    continue
                child, child_gaps = parse_card(child_data)
                unchecked.extend(child_gaps)
                # A provider must not silently substitute a different company.
                if str(child.get('огрн') or '') != to_ogrn:
                    failed.add(to_ogrn)
                    pending(from_id, to_ogrn, depth + 1, 'unavailable')
                    continue
                to_id = add(child, depth + 1, legacy)
                if child_gaps:
                    pending(from_id, to_id, depth + 1, 'unavailable')
                queue.append((to_id, child, depth + 1))
                if pause:
                    time.sleep(pause)
                if mark_intersection(child, depth + 1):
                    stop, stop_note = 'intersection', 'найдено пересечение с известными'
                significant = _значимый(child)
                if significant and стоп_на_сигнале:
                    signals.append({'огрн': child.get('огрн'), 'инн': child.get('инн'),
                                    'название': child.get('название'), 'статус': significant, 'глубина': depth + 1})
                    stop, stop_note = stop or 'signal', stop_note or 'найден значимый сигнал: %s' % significant
            else:
                add({'огрн': to_ogrn}, depth + 1, legacy)
            key = (from_id, to_id, legacy, via)
            if to_id and key not in edge_keys:
                edge_keys.add(key)
                edges.append(_edge_metadata({'от': from_id, 'до': to_id, 'тип': legacy, 'через': via,
                                             'tier': TIER_CHECKO}, origin='checko'))
    if stop and not frontier:
        pending(target_id, target_id, 0, stop)
    complete = not frontier and not stop
    reason = stop or (frontier[0]['stop_reason'] if frontier else None)
    reason_labels = {'budget': 'бюджет запросов исчерпан', 'depth': 'достигнута глубина обхода',
                     'max_nodes': 'достигнут потолок узлов', 'unavailable': 'карточка недоступна'}
    stop_note = stop_note or reason_labels.get(reason)
    return {'статус': 'ок', 'цель': {k: target.get(k) for k in ('инн', 'огрн', 'название')},
            'узлов': len(nodes), 'рёбер': len(edges), 'усечено': not complete,
            'обход': {'полный': complete, 'глубина_задана': max_depth, 'бюджет_запросов': бюджет,
                      'запросов_сделано': requests, 'стоп_на_сигнале': bool(стоп_на_сигнале),
                      'известных_на_входе': len(известные), 'причина_остановки': stop_note,
                      'неразвёрнутые_узлы': sorted({x['to'] for x in frontier})},
            'значимые_сигналы': signals,
            'пересечение_с_известными': {'найдено': intersections, 'полнота':
                ('обход полный: проверены все узлы до глубины %d' % max_depth) if complete else
                ('обход остановлен (%s) — пересечение проверено только у %d найденных узлов, остальные не смотрели' % (stop_note, len(nodes)))} if известные else None,
            'узлы': list(nodes.values()), 'рёбра': edges, 'не_проверено': unchecked,
            'frontier': frontier, 'stop_reason': reason, 'ownership_chains': _ownership_chains(edges),
            'оговорка': ОГОВОРКА, 'источник': 'checko API (%s), схема v2/company по документации 19.09.2026' % API_URL,
            'дата_построения': time.strftime('%Y-%m-%d')}


def normalize_offline(data, имя_файла='offline-граф'):
    """Preserve supplied provenance and incompleteness, never infer completeness."""
    if not isinstance(data, dict) or not isinstance(data.get('узлы'), list) or not isinstance(data.get('рёбра'), list):
        return {'статус': 'не проверено', 'причина': 'offline-граф должен содержать списки «узлы» и «рёбра» в формате affiliates_graph (%s)' % имя_файла}
    result = copy.deepcopy(data)
    nodes = []
    for item in data['узлы']:
        if not isinstance(item, dict):
            continue
        node = copy.deepcopy(item)
        target = node.get('глубина') == 0 or 'цель' in (node.get('типы_связи') or [])
        node.setdefault('типы_связи', ['цель'] if target else [])
        node.setdefault('глубина', 0 if target else 1)
        node['tier'] = node.get('tier') or (TIER_TARGET if target else TIER_OFFLINE)
        nodes.append(node)
    edges = []
    for item in data['рёбра']:
        if not isinstance(item, dict) or not item.get('от') or not item.get('до'):
            continue
        edge = copy.deepcopy(item)
        edge.setdefault('тип', None)
        edge.setdefault('через', None)
        edge['tier'] = edge.get('tier') or TIER_OFFLINE
        edges.append(_edge_metadata(edge, data.get('_evidence'), origin=item.get('origin_family')))
    target = data.get('цель')
    if not target:
        target = next(({k: n.get(k) for k in ('инн', 'огрн', 'название')} for n in nodes if n.get('глубина') == 0), None)
    frontier = copy.deepcopy(data.get('frontier') or [])
    complete = (data.get('усечено') is False and (data.get('обход') or {}).get('полный') is True
                and not frontier and not data.get('stop_reason'))
    if not complete and not frontier:
        frontier = [{'from': None, 'to': None, 'depth': None, 'stop_reason': data.get('stop_reason') or 'unknown'}]
    result.update({'статус': 'ок', 'цель': target, 'узлов': len(nodes), 'рёбер': len(edges),
                   'усечено': not complete, 'узлы': nodes, 'рёбра': edges,
                   'frontier': frontier, 'stop_reason': data.get('stop_reason') or (frontier[0]['stop_reason'] if frontier else None),
                   'ownership_chains': _ownership_chains(edges),
                   'не_проверено': list(data.get('не_проверено') or []) + ([] if complete else ['Полнота offline-графа не подтверждена.']),
                   'оговорка': data.get('оговорка') or ОГОВОРКА,
                   'источник': 'offline-граф (ручной сбор): %s' % имя_файла,
                   'дата_построения': data.get('дата_построения') or time.strftime('%Y-%m-%d')})
    return result


def _известные_из(путь):
    """ИНН/ОГРН из файла: JSON-список, JSON-объект снимков или текст по строке.

    Возвращает (значения, ошибка, отброшено). Формат не угадывается молча:
    что не разобралось — в «ошибка», а строки, не похожие на ИНН/ОГРН (10, 12
    или 13 цифр), — в «отброшено». Иначе файл с прозой молча превращается в
    список известных, ни с чем не совпадает, и «пересечений не найдено»
    становится выдумкой.
    """
    try:
        сырое = pathlib.Path(путь).read_text(encoding="utf-8")
    except OSError as e:
        return None, "файл известных не читается: %s" % e, []
    def построчно():
        return [s.strip() for s in сырое.splitlines() if s.strip()
                and not s.strip().startswith("#")]

    try:
        данные = json.loads(сырое)
    except ValueError:
        return _просеять(построчно())
    # Файл из одной строки «7700000003» — валидный JSON-int, но это список
    # известных из одного ИНН, а не «неожиданный формат»: скаляр читаем
    # построчно, иначе разбор спотыкается ровно на самом простом входе.
    if not isinstance(данные, (list, dict)):
        return _просеять(построчно())
    if isinstance(данные, list):
        return _просеять(_известные_из_списка(данные))
    # снимки watchlist.py: {"ИНН": {...}} либо {"контрагенты": [...]}
    внутри = данные.get("контрагенты")
    if isinstance(внутри, list):
        return _просеять(_известные_из_списка(внутри))
    return _просеять([str(k) for k in данные])


_ПОХОЖ_НА_ИНН = re.compile(r"^\d{10}$|^\d{12}$|^\d{13}$")


def _просеять(значения):
    """(похожие на ИНН/ОГРН, None, отброшенное). Пустой результат — это ошибка."""
    годные = [s for s in значения if _ПОХОЖ_НА_ИНН.match(str(s).strip())]
    мусор = [s for s in значения if not _ПОХОЖ_НА_ИНН.match(str(s).strip())]
    if not годные:
        return None, ("файл известных: ни одной строки, похожей на ИНН или ОГРН "
                      "(10, 12 или 13 цифр); прочитано строк: %d" % len(значения)), мусор
    return годные, None, мусор


def _известные_из_списка(список):
    значения = []
    for x in список:
        if isinstance(x, dict):
            значения.extend(str(v) for v in (x.get("инн"), x.get("огрн")) if v)
        elif x:
            значения.append(str(x))
    return значения


def _число(args, флаг, умолчание):
    """Значение числового флага или (None, текст ошибки)."""
    if флаг not in args:
        return умолчание, None
    i = args.index(флаг)
    if i + 1 >= len(args):
        return None, "после %s нужно число" % флаг
    try:
        v = int(args[i + 1])
    except ValueError:
        return None, "после %s нужно число, получено %r" % (флаг, args[i + 1])
    if v < 1:
        return None, "%s должно быть больше нуля, получено %d" % (флаг, v)
    return v, None


def main(argv):
    args = argv[1:]
    if "--offline-граф" in args:
        i = args.index("--offline-граф")
        if i + 1 >= len(args):
            sys.stderr.write("После --offline-граф нужен путь к JSON\n")
            return 2
        path = args[i + 1]
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as e:
            out = {"статус": "не проверено",
                   "причина": "offline-граф не читается как JSON (%s)" % e}
            sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
            return 1
        out = normalize_offline(data, имя_файла=path)
        sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
        return 0
    глубина, ош = _число(args, "--глубина", MAX_DEPTH)
    if ош is None:
        бюджет, ош = _число(args, "--бюджет", БЮДЖЕТ_ЗАПРОСОВ)
    if ош:
        sys.stderr.write(ош + "\n")
        return 2
    стоп_на_сигнале = "--без-стопа" not in args
    известные, ош_изв, отброшено = [], None, []
    if "--известные" in args:
        i = args.index("--известные")
        if i + 1 >= len(args):
            sys.stderr.write("После --известные нужен путь к файлу\n")
            return 2
        известные, ош_изв, отброшено = _известные_из(args[i + 1])
        if ош_изв:
            sys.stdout.write(json.dumps(
                {"статус": "не проверено", "причина": ош_изв,
                 "отброшено_строк": len(отброшено)},
                ensure_ascii=False, indent=2) + "\n")
            return 1
    позиционные = []
    пропустить = False
    for k, a in enumerate(args):
        if пропустить:
            пропустить = False
            continue
        if a in ("--глубина", "--бюджет", "--известные"):
            пропустить = True
            continue
        if a == "--без-стопа":
            continue
        позиционные.append(a)
    if len(позиционные) != 1:
        sys.stderr.write(
            "Использование: python3 affiliates_graph.py <ИНН> "
            "[--глубина N] [--бюджет N] [--без-стопа] [--известные файл] | "
            "--offline-граф файл.json\n")
        return 2
    key = os.environ.get("CHECKO_API_KEY")
    if not key:
        out = {"статус": "не проверено",
               "причина": "нужен CHECKO_API_KEY, бесплатно: "
                          "checko.ru/integration/api — либо соберите связи "
                          "браузером и подайте через --offline-граф",
               "источник": "checko API (%s)" % API_URL}
        sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
        return 0

    policy = _load_sibling("provider_policy").configured_policy()
    if policy is not None:
        policy = copy.deepcopy(policy)
        for entry in policy["providers"]:
            entry["request_limit"] = min(entry["request_limit"], бюджет, БЮДЖЕТ_ЗАПРОСОВ)
    ledger = {}
    def fetcher(kind, value):
        return fetch_company(key, inn=value, policy=policy, ledger=ledger) if kind == "inn" \
            else fetch_company(key, ogrn=value, policy=policy, ledger=ledger)

    graph = build_graph(fetcher, позиционные[0], max_depth=глубина,
                        бюджет=бюджет, стоп_на_сигнале=стоп_на_сигнале,
                        известные=известные)
    if policy is not None:
        graph["_provider_policy"] = {"schema_version":1, "policy":copy.deepcopy(policy),
                                     "transfers":copy.deepcopy(ledger.get("transfers", []))}
    if отброшено:
        graph["известные_отброшено"] = {
            "строк": len(отброшено), "примеры": отброшено[:5],
            "почему": "не похоже на ИНН или ОГРН (10, 12 или 13 цифр) — "
                      "в сверку не вошло",
        }
    sys.stdout.write(json.dumps(graph, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
