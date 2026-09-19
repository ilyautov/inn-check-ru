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

import json
import os
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


def fetch_company(key, inn=None, ogrn=None):
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


def build_graph(fetcher, inn, max_nodes=MAX_NODES, max_depth=MAX_DEPTH,
                pause=PAUSE):
    """Строит BFS-граф. fetcher(kind, value) -> (data|None, note) — отделяет
    логику графа от сети (в eval подставляется фикстурный fetcher, pause=0)."""
    target_data, note = fetcher("inn", inn)
    if target_data is None:
        return {"статус": "не проверено", "причина": note,
                "источник": "checko API (%s)" % API_URL}

    узлы = {}        # огрн -> узел
    рёбра = []
    не_проверено = []
    усечено = False

    def add_node(card, глубина, тип_связи=None):
        огрн = card.get("огрн") or card.get("инн")
        if not огрн:
            return None
        if огрн in узлы:
            if тип_связи and тип_связи not in узлы[огрн]["типы_связи"]:
                узлы[огрн]["типы_связи"].append(тип_связи)
            return огрн
        узлы[огрн] = {
            "огрн": card.get("огрн"),
            "инн": card.get("инн"),
            "название": card.get("название"),
            "статус": card.get("статус"),
            "оквэд": card.get("оквэд"),
            "адрес": card.get("адрес"),
            "спецрежим": card.get("спецрежим") or None,
            "ссч": card.get("ссч"),
            "глубина": глубина,
            "типы_связи": [тип_связи] if тип_связи else ["цель"],
            "tier": TIER_TARGET if глубина == 0 else TIER_CHECKO,
        }
        return огрн

    target, gaps = parse_card(target_data)
    не_проверено.extend(gaps)
    target_id = add_node(target, 0)
    if target_id is None:
        return {"статус": "не проверено",
                "причина": "карточка цели без ОГРН/ИНН",
                "источник": "checko API (%s)" % API_URL}

    # Рёбра из карточки: (тип, список огрн, через-фио)
    def edges_of(card):
        out = []
        for рук in card.get("руководители", []):
            for ogrn in рук.get("связ_руковод", []):
                out.append(("общий_директор", ogrn, рук.get("фио")))
            for ogrn in рук.get("связ_учред", []):
                out.append(("общий_директор", ogrn, рук.get("фио")))
        for учр in card.get("учредители", []):
            for ogrn in учр.get("связ_учред", []):
                out.append(("общий_учредитель", ogrn, учр.get("фио")))
            for ogrn in учр.get("связ_руковод", []):
                out.append(("общий_учредитель", ogrn, учр.get("фио")))
        for ogrn in card.get("масс_адрес", []):
            out.append(("адрес", ogrn, None))
        for o in card.get("правопреемство", []):
            out.append(("правопреемство", o["огрн"], None))
        return out

    frontier = [(target_id, target)]
    depth = 1
    while frontier and depth <= max_depth and not усечено:
        next_frontier = []
        for from_id, card in frontier:
            for тип, to_огрн, через in edges_of(card):
                if len(узлы) >= max_nodes:
                    усечено = True
                    break
                if to_огрн not in узлы:
                    data, note2 = fetcher("ogrn", to_огрн)
                    if data is None:
                        не_проверено.append(
                            "ОГРН %s: карточка не получена (%s)" % (to_огрн, note2))
                        continue
                    to_card, gaps = parse_card(data)
                    не_проверено.extend(gaps)
                    to_id = add_node(to_card, depth, тип)
                    if to_id and depth < max_depth:
                        next_frontier.append((to_id, to_card))
                    if pause:
                        time.sleep(pause)
                else:
                    add_node({"огрн": to_огрн}, depth, тип)
                    to_id = to_огрн
                if to_id and (from_id, to_id, тип) not in [
                        (r["от"], r["до"], r["тип"]) for r in рёбра]:
                    рёбра.append({"от": from_id, "до": to_id,
                                  "тип": тип, "через": через,
                                  "tier": TIER_CHECKO})
        frontier = next_frontier
        depth += 1

    return {
        "статус": "ок",
        "цель": {"инн": target.get("инн"), "огрн": target.get("огрн"),
                 "название": target.get("название")},
        "узлов": len(узлы),
        "рёбер": len(рёбра),
        "усечено": усечено,
        "узлы": list(узлы.values()),
        "рёбра": рёбра,
        "не_проверено": не_проверено,
        "оговорка": ОГОВОРКА,
        "источник": "checko API (%s), схема v2/company по документации 19.09.2026"
                    % API_URL,
        "дата_построения": time.strftime("%Y-%m-%d"),
    }


def normalize_offline(data, имя_файла="offline-граф"):
    """Нормализует заранее собранные связи (ручной сбор через браузер, другой
    агрегатор) в формат графа: checko становится заменяемым слоем, а не
    требованием. Вход: {"цель"?, "узлы": [...], "рёбра": [...]}. Сеть не
    дёргается; отсутствующие tier проставляются как ⚠️ ручной сбор."""
    if not isinstance(data, dict) or not isinstance(data.get("узлы"), list) \
            or not isinstance(data.get("рёбра"), list):
        return {"статус": "не проверено",
                "причина": "offline-граф должен содержать списки «узлы» и «рёбра» "
                           "в формате affiliates_graph (%s)" % имя_файла}
    узлы, рёбра = [], []
    for u in data["узлы"]:
        if not isinstance(u, dict):
            continue
        node = dict(u)
        целевой = node.get("глубина") == 0 or "цель" in (
            node.get("типы_связи") or [])
        node.setdefault("типы_связи", ["цель"] if целевой else [])
        node.setdefault("глубина", 0 if целевой else 1)
        node["tier"] = node.get("tier") or (
            TIER_TARGET if целевой else TIER_OFFLINE)
        узлы.append(node)
    for r in data["рёбра"]:
        if not isinstance(r, dict) or not r.get("от") or not r.get("до"):
            continue
        edge = {"от": r["от"], "до": r["до"],
                "тип": r.get("тип"), "через": r.get("через"),
                "tier": r.get("tier") or TIER_OFFLINE}
        рёбра.append(edge)
    цель = data.get("цель")
    if not цель:
        for node in узлы:
            if node.get("глубина") == 0:
                цель = {"инн": node.get("инн"), "огрн": node.get("огрн"),
                        "название": node.get("название")}
                break
    return {
        "статус": "ок",
        "цель": цель,
        "узлов": len(узлы),
        "рёбер": len(рёбра),
        "усечено": False,
        "узлы": узлы,
        "рёбра": рёбра,
        "не_проверено": [],
        "оговорка": ОГОВОРКА,
        "источник": "offline-граф (ручной сбор): %s" % имя_файла,
        "дата_построения": time.strftime("%Y-%m-%d"),
    }


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
    if len(args) != 1:
        sys.stderr.write(
            "Использование: python3 affiliates_graph.py <ИНН> | "
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

    def fetcher(kind, value):
        return fetch_company(key, inn=value) if kind == "inn" \
            else fetch_company(key, ogrn=value)

    graph = build_graph(fetcher, argv[1])
    sys.stdout.write(json.dumps(graph, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
