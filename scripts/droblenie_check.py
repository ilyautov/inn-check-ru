#!/usr/bin/env python3
"""
droblenie_check.py — чеклист признаков дробления бизнеса, посчитанный КОДОМ
по графу связей (affiliates_graph.py) и выручке группы. Только stdlib.

Использование:
    python3 affiliates_graph.py <ИНН> | python3 droblenie_check.py --stdin
    python3 droblenie_check.py graph.json [--выручка revenues.json]

revenues.json (необязательно): {"<ИНН>": {"выручка": <руб/год>, "год": ...}} —
для офлайн-прогона. Без файла скрипт пытается добрать выручку из ГИР БО
через fetch_counterparty.py (importlib, как в diff_counterparty); что не
досталось — честно «по N из M компаний». ВНИМАНИЕ: ГИР БО отдаёт тыс. рублей,
канон — рубли; сетевая выручка конвертируется ×1000, в revenues.json кладите
рубли.

Пороги читаются из data/canon_ru.json (канон с цитатами, не константы кода);
файл отсутствует/битый -> блок выручки «не проверено», скрипт не падает.

Формулировочная дисциплина (суды: «аффилированность сама по себе не доказывает
дробление»): вывод — всегда «N признаков, совпадающих с типовыми доводами ФНС
по спорам о дроблении (письмо БВ-4-7/8051@ от 16.07.2024)» — никогда
«это дробление». Severity: 3+ признака -> 🟡, 5+ -> 🔴 (синхронно с чеклистом
однодневки в SKILL.md).
"""

import importlib.util
import json
import os
import sys

ПИСЬМО_ФНС = "письмо ФНС БВ-4-7/8051@ от 16.07.2024"
ДИСКЛЕЙМЕР = ("Это эвристика раннего предупреждения, не заключение: суды прямо "
              "указывают, что аффилированность сама по себе не доказывает "
              "дробление; ФНС доказывает совокупностью признаков + данными "
              "выемки (IP, финпотоки, единая 1С — вне открытых данных).")

CANON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "..", "data", "canon_ru.json")


def _read_canon_text(path):
    """Текст канона: путь репозитория (скилл, ZIP, git) -> package-data
    inn_check_ru_data из установленного колеса (pip/uvx). Нет нигде -> None."""
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        pass
    try:
        from importlib.resources import files
        return (files("inn_check_ru_data") / "canon_ru.json").read_text(
            encoding="utf-8")
    except Exception:  # нет пакета/ресурса/битый файл: канон «не проверено»
        return None


def _load_canon(path=CANON_PATH):
    text = _read_canon_text(path)
    if text is None:
        return None
    try:
        пороги = json.loads(text).get("пороги")
    except (ValueError, AttributeError):
        return None
    return пороги if isinstance(пороги, dict) else None


def _load_fetch_module():
    """fetch_counterparty.py через importlib (паттерн diff_counterparty)."""
    try:
        spec = importlib.util.spec_from_file_location(
            "fetch_counterparty",
            os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "fetch_counterparty.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


def _выручка_гирбо(инн, mod, opener):
    """Выручка последнего года из ГИР БО в РУБЛЯХ (источник отдаёт тыс. руб.)."""
    try:
        fin, _ = mod.fetch_finance(opener, инн)
        годы = (fin or {}).get("отчётность_по_годам") or []
        for y in годы:
            if isinstance(y, dict) and y.get("выручка") is not None:
                return float(y["выручка"]) * 1000, y.get("год")
    except Exception:
        pass
    return None, None


def score(graph, выручка_map=None, canon=None, собрать_сетью=True):
    """Считает чеклист по графу. Возвращает итоговый dict.

    выручка_map: {"ИНН": {"выручка": руб, "год": ...}} — внешний сбор (eval,
    офлайн). canon: dict порогов (или None -> «не проверено»).
    """
    if not isinstance(graph, dict) or graph.get("статус") != "ок":
        return {"статус": "не проверено",
                "причина": "на входе не граф affiliates_graph.py со статусом «ок» "
                           "(граф не построен — см. его причину)"}

    узлы = [u for u in graph.get("узлы", []) if isinstance(u, dict)]
    рёбра = [r for r in graph.get("рёбра", []) if isinstance(r, dict)]
    компаний = len(узлы)
    признаки = []
    не_проверено = []

    # Принцип «одиночному источнику не верим»: одноисточниковый граф
    # (оговорка или ⚠️-tier у рёбер) — предупреждение в вывод.
    один_источник = bool(graph.get("оговорка")) or any(
        "⚠️" in (r.get("tier") or "") for r in рёбра)

    # Граф мог быть усечён (потолок узлов) или остановлен рано (ленивый обход,
    # задача 20). Признаки «все на УСН», «единый адрес», «группа над порогом»
    # считаются ПО ТОМУ, ЧТО В ГРАФЕ: на неполном графе их отсутствие ничего не
    # доказывает, а сумма выручки заведомо занижена. Молчать об этом нельзя —
    # 🟢 по половине группы читается как 🟢 по группе.
    обход = graph.get("обход") if isinstance(graph.get("обход"), dict) else {}
    неполный = bool(graph.get("усечено")) or обход.get("полный") is False
    if неполный:
        причина = обход.get("причина_остановки") or "граф усечён по потолку узлов"
        не_проверено.append(
            "граф неполон (%s): признаки посчитаны по %d найденным компаниям, "
            "ненайденные связи в них не входят — отсутствие признака здесь не "
            "значит его отсутствия в группе" % (причина, компаний))

    def признак(fid, факт, источник):
        признаки.append({"id": fid, "факт": факт, "источник": источник})

    # --- структурные признаки из рёбер ---
    типы = {}
    for r in рёбра:
        через = r.get("через")
        if через:
            типы.setdefault(r.get("тип"), set()).add(через)
    директора = типы.get("общий_директор", set())
    учредители = типы.get("общий_учредитель", set())
    if директора and компаний >= 2:
        признак("общий_директор",
                "один директор у группы из %d компаний: %s"
                % (компаний, ", ".join(sorted(директора))),
                "checko СвязРуковод (ЕГРЮЛ)")
    if учредители and компаний >= 2:
        признак("общий_учредитель",
                "один учредитель у группы: %s" % ", ".join(sorted(учредители)),
                "checko СвязУчред (ЕГРЮЛ)")
    if any(r.get("тип") == "адрес" for r in рёбра):
        адреса = {u.get("адрес") for u in узлы if u.get("адрес")}
        признак("единый_адрес",
                "компании группы на одном адресе (%d шт., адресов: %d)"
                % (компаний, len(адреса)),
                "checko МассАдрес (ЕГРЮЛ)")

    # --- ОКВЭД и спецрежим (если доступны в карточках) ---
    оквэды = [u.get("оквэд") for u in узлы if u.get("оквэд")]
    if len(оквэды) >= 2:
        if len(set(оквэды)) == 1:
            признак("одинаковый_оквэд",
                    "идентичный основной ОКВЭД %s у %d компаний"
                    % (оквэды[0], len(оквэды)),
                    "checko ОКВЭД (ЕГРЮЛ)")
    else:
        не_проверено.append("основной ОКВЭД — нет данных по группе")
    режимы = [u for u in узлы if u.get("спецрежим")]
    if len(режимы) >= 2 and all("УСН" in (u.get("спецрежим") or [])
                                for u in режимы):
        признак("все_на_усн",
                "все компании группы (%d из %d с данными) на УСН"
                % (len(режимы), компаний),
                "checko Налоги.ОсобРежим")
    elif len(режимы) < 2:
        не_проверено.append("спецрежим — данных меньше чем по 2 компаниям")

    # --- выручка группы vs пороги канона ---
    выручки = {}   # инн -> (сумма руб, год)
    mod = opener = None
    if выручка_map is None and собрать_сетью:
        mod = _load_fetch_module()
        opener = mod._make_opener() if mod else None
    for u in узлы:
        инн = u.get("инн")
        if not инн:
            continue
        if выручка_map is not None:
            rec = выручка_map.get(str(инн))
            if isinstance(rec, dict) and rec.get("выручка") is not None:
                выручки[инн] = (float(rec["выручка"]), rec.get("год"))
        elif opener is not None:
            сумма, год = _выручка_гирбо(инн, mod, opener)
            if сумма is not None:
                выручки[инн] = (сумма, год)

    покрыто = "по %d из %d компаний" % (len(выручки), компаний)
    сумма = round(sum(v for v, _ in выручки.values()), 2)
    года = sorted({g for _, g in выручки.values() if g}, reverse=True)
    выручка_блок = {"сумма": сумма, "покрыто_компаний": покрыто,
                    "год": года[0] if года else None,
                    "источник": "ГИР БО (тыс. руб -> руб)"}

    if canon is None:
        выручка_блок["статус"] = ("не проверено — канон порогов "
                                  "data/canon_ru.json отсутствует или битый")
    elif len(выручки) >= 2:
        порог_ндс = (canon.get("ндс_усн_освобождение") or {}).get("значение")
        лимит_усн = (canon.get("лимит_усн") or {}).get("значение")
        if порог_ндс and all(v < порог_ндс for v, _ in выручки.values()) \
                and сумма > порог_ндс:
            признак("группа_над_порогом_ндс",
                    "каждая компания под %.0f млн ₽, группа в сумме %.1f млн ₽ "
                    "— профиль дробления под освобождение от НДС (%s)"
                    % (порог_ндс / 1e6, сумма / 1e6, покрыто),
                    "ГИР БО + data/canon_ru.json (проверено %s)"
                    % (canon["ндс_усн_освобождение"].get("дата_проверки")))
        if лимит_усн and сумма > лимит_усн:
            признак("группа_над_лимитом_усн",
                    "совокупная выручка группы %.1f млн ₽ над лимитом УСН "
                    "%.1f млн ₽ (%s)"
                    % (сумма / 1e6, лимит_усн / 1e6, покрыто),
                    "ГИР БО + data/canon_ru.json (проверено %s)"
                    % (canon["лимит_усн"].get("дата_проверки")))
    else:
        не_проверено.append("сравнение выручки с порогами — нужна выручка "
                            "минимум по 2 компаниям (%s)" % покрыто)

    # --- численность ~0 при выручке ---
    нулевые = [u for u in узлы
               if u.get("ссч") is not None and u.get("ссч") <= 1
               and u.get("инн") in выручки and выручки[u["инн"]][0] > 0]
    if len(нулевые) >= 2:
        признак("численность_ноль_при_выручке",
                "у %d компаний группы численность <= 1 чел. при ненулевой выручке"
                % len(нулевые),
                "checko СЧР + ГИР БО")

    n = len(признаки)
    severity = "🔴" if n >= 5 else ("🟡" if n >= 3 else "🟢")
    out = {
        "статус": "ок",
        "признаков_совпало": n,
        "severity": severity,
        "признаки": признаки,
        "выручка_группы": выручка_блок,
        "компаний_в_графе": компаний,
        "не_проверено": не_проверено,
        "оценка": ("%d признаков, совпадающих с типовыми доводами ФНС по "
                   "спорам о дроблении (%s)" % (n, ПИСЬМО_ФНС)),
        "дисклеймер": ДИСКЛЕЙМЕР,
    }
    if неполный:
        out["граф_неполон"] = {
            "причина": обход.get("причина_остановки")
                       or "граф усечён по потолку узлов",
            "компаний_в_графе": компаний,
            "неразвёрнутые_узлы": обход.get("неразвёрнутые_узлы") or [],
            "что_это_значит": "признаки считались по найденной части группы; "
                              "«признак не найден» здесь означает «не найден в "
                              "этой части», а не «его нет». Для полного обхода: "
                              "affiliates_graph.py <ИНН> --глубина 2 "
                              "--без-стопа --бюджет N",
        }
    if один_источник:
        out["предупреждение_источников"] = (
            "признаки посчитаны по данным одного агрегатора (⚠️); пересечите "
            "связи вторым источником перед выводом в досье")
    return out


def main(argv):
    args = argv[1:]
    выручка_map = None
    if "--выручка" in args:
        i = args.index("--выручка")
        if i + 1 >= len(args):
            sys.stderr.write("После --выручка нужен путь к JSON\n")
            return 2
        try:
            with open(args[i + 1], encoding="utf-8") as fh:
                выручка_map = json.load(fh)
        except (OSError, ValueError) as e:
            sys.stderr.write("файл выручки не читается: %s\n" % e)
            return 2
        args = args[:i] + args[i + 2:]
    if not args:
        sys.stderr.write(__doc__)
        return 2
    try:
        if args[0] == "--stdin":
            graph = json.loads(sys.stdin.read())
        else:
            with open(args[0], encoding="utf-8") as fh:
                graph = json.load(fh)
    except (OSError, ValueError) as e:
        out = {"статус": "не проверено", "причина": "вход не JSON (%s)" % e}
        sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
        return 1
    out = score(graph, выручка_map=выручка_map, canon=_load_canon(),
                собрать_сетью=выручка_map is None)
    sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
