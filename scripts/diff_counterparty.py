#!/usr/bin/env python3
"""
diff_counterparty.py — мониторинг изменений контрагента по снимкам
fetch_counterparty.py --save. Офлайн: сравниваются файлы, сеть не нужна.

Использование:
    python3 diff_counterparty.py <ИНН>                 # два последних снимка из кэша
    python3 diff_counterparty.py --a old.json --b new.json
    python3 diff_counterparty.py <ИНН> --human          # светофорная карточка

Выход JSON:
    {
      "статус": "сравнено" | "изменений нет" | "сравнивать не с чем" | "нет снимков",
      "изменения": [
        {"поле": ..., "было": ..., "стало": ..., "severity": "🔴"/"🟡"/"ℹ️",
         "дата_было": ..., "дата_стало": ...}
      ]
    }
Один снимок — честное «сравнивать не с чем, снимок сохранён, вернитесь позже».

Пороги финансового ухудшения не дублируются: импортируются из fin_scoring.py
(единое место констант). Leading indicators: смена статуса на ликвидацию/
реорганизацию 🔴, недостоверность сведений 🔴, смена директора/адреса 🟡,
налоговая задолженность 🟡, новый год отчётности с ЧА < 0 🔴 или падением
выручки > порога 🟡.
"""

import glob
import importlib.util
import json
import os
import sys

CACHE_DIR = os.path.expanduser(os.path.join("~", ".cache", "inn-check-ru"))
SEV_ORDER = {"🔴": 2, "🟡": 1, "ℹ️": 0}

# Константы порогов и парсеры строк баланса — из fin_scoring.py, не дублируем.
_fin = None
try:
    _spec = importlib.util.spec_from_file_location(
        "fin_scoring",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "fin_scoring.py"))
    _fin = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_fin)
except Exception:
    _fin = None  # финансовый блок diff'а деградирует в «не проверено»

RED_STATUS_HINTS = ("ликвид", "реорганиз", "прекращ", "недейств", "банкрот")


def _load(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _g(d, *path):
    cur = d
    for key in path:
        if isinstance(cur, dict):
            cur = cur.get(key)
        else:
            return None
    return cur


def _snap_date(path):
    return os.path.splitext(os.path.basename(path))[0].replace("_", " ")


def _snapshots(inn):
    snap_dir = os.path.join(CACHE_DIR, "snapshots", inn)
    return sorted(glob.glob(os.path.join(snap_dir, "*.json")))


def _изм(поле, было, стало, severity, дата_было, дата_стало, пояснение=None):
    out = {"поле": поле, "было": было, "стало": стало, "severity": severity,
           "дата_было": дата_было, "дата_стало": дата_стало}
    if пояснение:
        out["пояснение"] = пояснение
    return out


def _директора_изменений_за_год(paths):
    """Число смен директора по истории снимков за последние 365 дней."""
    import datetime as dt
    from itertools import pairwise
    серия = []
    for p in paths:
        d = _load(p) or {}
        рук = _g(d, "егрюл", "руководитель")
        try:
            y, m, dd = os.path.basename(p)[:10].split("-")
            date = dt.date(int(y), int(m), int(dd))
        except ValueError:
            date = None
        if рук:
            серия.append((date, рук))
    if len(серия) < 2:
        return 0
    последняя = серия[-1][0]
    изменений = 0
    for (d1, r1), (d2, r2) in pairwise(серия):
        if r1 != r2 and (последняя is None or d2 is None
                         or (последняя - d2).days <= 365):
            изменений += 1
    return изменений


def diff(a, b, дата_было, дата_стало, все_снимки=None):
    """Сравнивает два снимка (dict'ы вывода fetch_counterparty.py)."""
    изменения = []

    # Статус: переход в ликвидацию/реорганизацию — 🔴 leading indicator.
    s_a, s_b = _g(a, "егрюл", "статус"), _g(b, "егрюл", "статус")
    if s_a != s_b and (s_a or s_b):
        red = any(h in (s_b or "").lower() for h in RED_STATUS_HINTS)
        изменения.append(_изм(
            "статус", s_a, s_b, "🔴" if red else "🟡", дата_было, дата_стало,
            "смена статуса — leading indicator, в ЕГРЮЛ уже зафиксировано"
            if red else None))
    if not _g(a, "егрюл", "дата_прекращения") and _g(b, "егрюл", "дата_прекращения"):
        изменения.append(_изм(
            "дата_прекращения", None, _g(b, "егрюл", "дата_прекращения"),
            "🔴", дата_было, дата_стало, "появилась дата прекращения деятельности"))

    # Руководитель и адрес.
    r_a, r_b = _g(a, "егрюл", "руководитель"), _g(b, "егрюл", "руководитель")
    if r_a != r_b and (r_a or r_b):
        изменения.append(_изм(
            "руководитель", r_a, r_b, "🟡", дата_было, дата_стало,
            "смена директора перед сделкой — переспросите, кто реально управляет"))
    if все_снимки and _директора_изменений_за_год(все_снимки) >= 2:
        изменения.append(_изм(
            "руководитель_частота", None,
            "%d+ смены за год" % 2, "🟡", дата_было, дата_стало,
            "частая смена директора по истории снимков — признак однодневки"))
    adr_a, adr_b = _g(a, "егрюл", "адрес"), _g(b, "егрюл", "адрес")
    if adr_a != adr_b and (adr_a or adr_b):
        изменения.append(_изм("адрес", adr_a, adr_b, "🟡", дата_было, дата_стало))

    # Риск-флаги ФНС.
    inv_a = _g(a, "риски", "недостоверность_сведений")
    inv_b = _g(b, "риски", "недостоверность_сведений")
    if not inv_a and inv_b:
        изменения.append(_изм(
            "недостоверность_сведений", inv_a, inv_b, "🔴", дата_было, дата_стало,
            "появилась отметка о недостоверности сведений ЕГРЮЛ — deal-killer"))
    elif inv_a and not inv_b:
        изменения.append(_изм(
            "недостоверность_сведений", inv_a, inv_b, "ℹ️", дата_было, дата_стало,
            "отметка о недостоверности снята"))
    debt_a = _g(a, "риски", "налоговая_задолженность")
    debt_b = _g(b, "риски", "налоговая_задолженность")
    if not debt_a and debt_b:
        изменения.append(_изм(
            "налоговая_задолженность", debt_a, debt_b, "🟡", дата_было, дата_стало,
            "появилась налоговая задолженность"))

    # Новый год финотчётности с ухудшением — пороги из fin_scoring.
    годы_a = {_g(y, "год"): y for y in _g(a, "финансы", "отчётность_по_годам") or []
              if isinstance(y, dict)}
    годы_b = {_g(y, "год"): y for y in _g(b, "финансы", "отчётность_по_годам") or []
              if isinstance(y, dict)}
    новые = sorted(set(годы_b) - set(годы_a))
    if новые:
        if _fin is None:
            изменения.append(_изм(
                "финансы_новый_год", None, новые[-1], "ℹ️", дата_было, дата_стало,
                "новый год отчётности, но fin_scoring.py не импортировался — "
                "ухудшение не проверено"))
        else:
            год = новые[-1]
            rec, prev_rec = годы_b[год], None
            пред_годы = sorted(g for g in годы_b if g < год)
            if пред_годы:
                prev_rec = годы_b[пред_годы[-1]]
            cha_new = _fin._строка(rec, "1300")
            cha_old = _fin._строка(prev_rec, "1300") if prev_rec else None
            if cha_new is not None and cha_new < 0 and (cha_old is None or cha_old >= 0):
                изменения.append(_изм(
                    "чистые_активы", cha_old, cha_new, "🔴", дата_было, дата_стало,
                    "новый год %s: чистые активы ушли ниже нуля — движение к "
                    "ст. 30 ФЗ-14" % год))
            v_new = _fin._строка(rec, "2110")
            v_old = _fin._строка(prev_rec, "2110") if prev_rec else None
            if v_new is not None and v_old and v_old > 0:
                падение = (v_old - v_new) / v_old
                if падение > _fin.ПОРОГ_ПАДЕНИЯ_ВЫРУЧКИ:
                    изменения.append(_изм(
                        "выручка", v_old, v_new, "🟡", дата_было, дата_стало,
                        "новый год %s: выручка упала на %.0f%% г/г"
                        % (год, падение * 100)))

    # МСП и спецреестры — если блоки присутствуют в обоих снимках.
    for поле in ("статус_мсп", "категория"):
        m_a, m_b = _g(a, "мсп", поле), _g(b, "мсп", поле)
        if m_a is not None and m_b is not None and m_a != m_b:
            изменения.append(_изм("мсп." + поле, m_a, m_b, "🟡", дата_было, дата_стало))
    спец_a, спец_b = _g(a, "спецреестры"), _g(b, "спецреестры")
    if isinstance(спец_a, dict) and isinstance(спец_b, dict):
        for ключ in спец_b:
            st_a = _g(спец_a, ключ, "статус")
            st_b = _g(спец_b, ключ, "статус")
            if st_a and st_b and st_a != st_b:
                изменения.append(_изм(
                    "спецреестры." + ключ, st_a, st_b, "🟡", дата_было, дата_стало))
    return изменения


def human_card(inn, изменения, дата_было, дата_стало):
    если = max((SEV_ORDER.get(c["severity"], 0) for c in изменения), default=-1)
    значок = {2: "🔴", 1: "🟡"}.get(если, "🟢")
    вердикт = {2: "Появились deal-killer-сигналы — сделку стоп, перепроверить",
               1: "Есть изменения, требующие внимания",
               0: "Изменений нет"}.get(если, "Изменений нет")
    lines = [
        "🚦 Мониторинг контрагента — ИНН %s — %s → %s" % (inn, дата_было, дата_стало),
        "",
        "%s %s" % (значок, вердикт),
        "",
    ]
    if изменения:
        lines.append("Изменения:")
        for c in изменения:
            lines.append("- %s %s: %s → %s" % (
                c["severity"], c["поле"], c["было"], c["стало"]))
            if c.get("пояснение"):
                lines.append("  %s" % c["пояснение"])
        lines.append("")
    lines += [
        "Что проверено: снимки fetch_counterparty.py --save (%s, %s)"
        % (дата_было, дата_стало),
        ("Что НЕ проверено: суды/банкротства снимками не покрываются — "
         "перед крупной отгрузкой проверяйте вручную"),
    ]
    return "\n".join(lines)


def main(argv):
    args = argv[1:]
    human = "--human" in args
    args = [a for a in args if a != "--human"]
    path_a = path_b = inn = None
    inn_mode = False
    if "--a" in args and "--b" in args:
        try:
            path_a = args[args.index("--a") + 1]
            path_b = args[args.index("--b") + 1]
        except IndexError:
            path_a = path_b = None
    elif args:
        inn = args[0]
        inn_mode = True
        snaps = _snapshots(inn)
        if not snaps:
            out = {"статус": "нет снимков",
                   "причина": "для ИНН %s нет снимков — сначала запустите "
                              "fetch_counterparty.py %s --save" % (inn, inn)}
            sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
            return 1
        if len(snaps) == 1:
            out = {"статус": "сравнивать не с чем",
                   "причина": "снимок сохранён (%s), вернитесь позже — "
                              "нужен второй снимок для сравнения" % snaps[0],
                   "снимок": snaps[0]}
            sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
            return 0
        path_a, path_b = snaps[-2], snaps[-1]
    if not path_a or not path_b:
        sys.stderr.write(
            "Использование: diff_counterparty.py <ИНН> [--human] | --a f1 --b f2\n")
        return 2

    a, b = _load(path_a), _load(path_b)
    if a is None or b is None:
        out = {"статус": "не проверено",
               "причина": "снимок не читается как JSON: %s"
                          % (path_a if a is None else path_b)}
        sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
        return 1
    inn = inn or b.get("инн") or a.get("инн")
    дата_было, дата_стало = _snap_date(path_a), _snap_date(path_b)
    # история «частой смены директора» — только в режиме ИНН (реальный кэш),
    # в режиме --a/--b сравниваем ровно два файла, без фона из кэша
    все = _snapshots(inn) if (inn_mode and inn) else None
    изменения = diff(a, b, дата_было, дата_стало, все_снимки=все)

    if human:
        sys.stdout.write(human_card(inn or "?", изменения, дата_было, дата_стало)
                         + "\n")
        return 0
    out = {
        "статус": "сравнено" if изменения else "изменений нет",
        "инн": inn,
        "снимки": {"было": path_a, "стало": path_b,
                   "дата_было": дата_было, "дата_стало": дата_стало},
        "изменений": len(изменения),
        "изменения": изменения,
    }
    sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
