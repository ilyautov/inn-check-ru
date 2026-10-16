#!/usr/bin/env python3
"""
fin_scoring.py — финансовая патология контрагента, посчитанная КОДОМ
(а не моделью) из строк бухгалтерской отчётности ГИР БО (bo.nalog.gov.ru).

Использование:
    python3 fin_scoring.py file.json        # сохранённый вывод fetch_counterparty.py
    python3 fetch_counterparty.py <ИНН> | python3 fin_scoring.py --stdin

Вход: JSON, который возвращает fetch_counterparty.py (берём блок «финансы»),
либо сам блок «финансы», либо список годов отчётности. Принимаются строки
баланса: 2110 выручка, 2400 чистая прибыль, 1600 активы, 1300 капитал и
резервы (чистые активы), 1500 краткосрочные обязательства, 1400 долгосрочные,
1200 оборотные активы, 1250 денежные средства.

Выход: JSON с флагами:
    {
      "статус": "ок",
      "флаги": [
        {"id": ..., "severity": "🔴"/"🟡", "показатель": ..., "значение": ...,
         "порог": ..., "пояснение": ..., "источник": "ГИР БО", "год": ...,
         "строки_баланса": {...}}          # сырые строки — traceability
      ],
      "финансовый_профиль": {
        "сводка": ..., "не_проверено": [...], "строки_баланса": {год: {...}}
      }
    }
Если входных финансов нет — честный {"статус": "не проверено", "причина": ...},
скрипт не падает.

Философия: каждая цифра считается кодом и трассируется к строке отчётности;
где строки нет — показатель уходит в «не_проверено», а не выдумывается.
"""

import hashlib
import json
import sys
from pathlib import Path

# Preserve flat installed imports and legacy absolute-file entry points.
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

# Коды строк бухгалтерской отчётности (приказ Минфина № 66н).
СТРОКИ = {
    "2110": "выручка",
    "2400": "чистая прибыль (убыток)",
    "1600": "активы (итог баланса)",
    "1300": "капитал и резервы (чистые активы)",
    "1500": "обязательства краткосрочные",
    "1400": "обязательства долгосрочные",
    "1200": "оборотные активы",
    "1250": "денежные средства",
}

# Пороги скоринга — единое место. Их же импортирует diff_counterparty.py
# (мониторинг снимков): менять только здесь, не дублировать магические числа.
# Автономия, ликвидность и падение выручки проверены на банкротствах 2022–2024
# (references/kalibrovka.md, calibration/): менять только по правилу оттуда —
# лучше по лифту при сопоставимой полноте и с непересекающимися ДИ.
ПОРОГ_АВТОНОМИИ = 0.1
ПОРОГ_ЛИКВИДНОСТИ = 1.0
ПОРОГ_ПАДЕНИЯ_ВЫРУЧКИ = 0.5      # доля падения выручки г/г
ПОРОГ_РОСТА_ТРАНЗИТА = 3.0       # рост > 300% == выручка в 4+ раза
ПОРОГ_ЧИСЛЕННОСТИ_ТРАНЗИТА = 1   # «численность ~0»

# Синонимы именованных полей года -> код строки: так фикстуры и ручной ввод
# читаются без знания кодов, а fetch_counterparty кладёт коды в «строки».
_ИМЕНА = {
    "2110": ("выручка",),
    "2400": ("прибыль_убыток", "прибыль", "чистая_прибыль"),
    "1600": ("активы",),
    "1300": ("капитал", "чистые_активы"),
    "1500": ("обязательства_краткосрочные",),
    "1400": ("обязательства_долгосрочные",),
    "1200": ("оборотные_активы",),
    "1250": ("денежные_средства",),
}


def _num(x):
    """Defensive-парсинг числа: None/строка с пробелами/«руб.» -> float или None."""
    if isinstance(x, bool) or x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        cleaned = (x.replace(" ", "").replace(" ", "")
                   .replace(",", ".").replace("руб.", "").replace("₽", ""))
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def _round(x):
    return round(x, 2) if x is not None else None


def _год_карточки(rec):
    year = rec.get("год")
    if isinstance(year, bool) or not isinstance(year, (str, int)):
        return 0
    text = str(year).strip()
    return int(text) if len(text) == 4 and text.isascii() and text.isdigit() else 0


def _строка(rec, code):
    """Значение строки отчётности: сначала из блока «строки», потом именованные поля."""
    строки = rec.get("строки")
    if isinstance(строки, dict):
        val = _num(строки.get(code))
        if val is not None:
            return val
    for name in _ИМЕНА[code]:
        val = _num(rec.get(name))
        if val is not None:
            return val
    return None


def _сырые_строки(rec):
    """Сырые строки баланса года для traceability рядом с флагами."""
    out = {}
    for code in СТРОКИ:
        val = _строка(rec, code)
        if val is not None:
            out[code] = _round(val)
    return out


def _найти_финансы(data):
    """Извлекает список годов отчётности из любой из трёх входных форм."""
    if isinstance(data, list):
        return data, {}
    if not isinstance(data, dict):
        return None, {}
    if isinstance(data.get("отчётность_по_годам"), list):
        return data["отчётность_по_годам"], data
    fin = data.get("финансы")
    if isinstance(fin, dict) and isinstance(fin.get("отчётность_по_годам"), list):
        return fin["отчётность_по_годам"], data
    return None, data


# ГИР БО публикует отчётность в ТЫСЯЧАХ рублей, а уставный капитал приходит из
# ЕГРЮЛ в рублях. Сравнивать их напрямую нельзя: чистые активы оказались бы
# занижены в тысячу раз, и флаг «ниже уставного капитала» встал бы почти
# каждому. Приводим УК к единицам отчётности — остальные проверки внутри
# fin_scoring сравнивают строки отчётности между собой, и им пересчёт не нужен.
# Сверено с дампом ФНС revexp 19.09.2026 по трём ИНН: отношение дохода в рублях
# к строке 2110 — 1010, 1002 и 1025.
РУБ_В_ТЫС = 1000.0


def _уставный_капитал(root):
    """УК из ЕГРЮЛ (рубли) В ТЫСЯЧАХ рублей — в единицах строк отчётности."""
    for holder in (root, root.get("егрюл") if isinstance(root, dict) else None):
        if isinstance(holder, dict):
            val = _num(holder.get("уставный_капитал"))
            if val is not None:
                return val / РУБ_В_ТЫС
    return None


def _численность(root):
    """Значение, год и источник ССЧ. Ноль сохраняется; закрытый источник не читается."""
    av = root.get("_доступность") or {}
    for source, holder in (("риски", root.get("риски")),
                           ("дампы_фнс", root.get("дампы_фнс")), ("вход", root)):
        if not isinstance(holder, dict):
            continue
        state = av.get(source) if isinstance(av, dict) else None
        if isinstance(state, dict) and state.get("состояние") not in ("ok", "ок"):
            continue
        if isinstance(state, str) and not state.lower().startswith(("ok", "ок", "проверено")):
            continue
        own = str(holder.get("статус") or "").strip().lower()
        if own.startswith(("не проверено", "не собрано")) or (
                own.startswith("проверено") and own != "проверено"):
            continue
        value = _num(holder.get("численность_сотрудников"))
        if value is not None and value >= 0:
            # Реальный сбор «риски» использует среднесписочная_за_год;
            # численность_год сохраняется для дампов и старых входных файлов.
            supplied_years = [holder[key] for key in ("среднесписочная_за_год", "численность_год")
                              if holder.get(key) is not None]
            years = [_год_карточки({"год": year}) for year in supplied_years]
            if years and (not all(years) or len(set(years)) != 1):
                # Не заменяем конфликт/ошибку периода «недатированной» ССЧ:
                # такая подмена могла бы подтвердить транзитный профиль.
                return None, None, source
            return value, years[0] if years else None, source
    return None, None, None


def _периоды(records):
    """Один отчёт на календарный год; конфликт редакций требует ручного выбора."""
    grouped, reasons = {}, []
    for rec in records:
        year = _год_карточки(rec) if isinstance(rec, dict) else 0
        if not year:
            reasons.append("отчётность — запись без корректного года не использована")
            continue
        grouped.setdefault(year, []).append(rec)
    years = []
    for year in sorted(grouped, reverse=True):
        rows = grouped[year]
        values = [tuple(_строка(r, code) for code in СТРОКИ) for r in rows]
        if any(row != values[0] for row in values[1:]):
            reasons.append("отчётность за %s — конфликт дубликатов; редакция не выбрана" % year)
            years.append({"год": year})
        else:
            years.append(rows[0])
    return years, sorted(set(reasons))


def input_identity(data):
    """Versioned identity of the actual normalized scorer inputs, without scoring."""
    contract = _load_sibling("evidence_contract")
    _digest, _resource = contract._digest, contract._resource
    records, root = _найти_финансы(data)
    inputs = {"subject_inn": str(root.get("инн") or ""),
              # Bind exact calculation inputs, including missing rows. The
              # display helper rounds and can merge opposite sides of a threshold.
              "records": [{"year": _год_карточки(row),
                           "rows": {code: _строка(row, code) for code in СТРОКИ}}
                          if isinstance(row, dict) else None for row in records or []],
              "capital": _уставный_капитал(root), "staff": _численность(root)}
    inputs["records"].sort(key=_digest)
    return {"schema_version": 1, "sha256": _digest(inputs),
            "scorer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "rules_sha256": _digest({"canon": _resource("canon_ru.json"),
                "policy": _resource("evidence_policy.json"),
                "thresholds": [ПОРОГ_АВТОНОМИИ, ПОРОГ_ЛИКВИДНОСТИ,
                    ПОРОГ_ПАДЕНИЯ_ВЫРУЧКИ, ПОРОГ_РОСТА_ТРАНЗИТА,
                    ПОРОГ_ЧИСЛЕННОСТИ_ТРАНЗИТА], "currency_scale": РУБ_В_ТЫС})}


def score(data):
    """Считает флаги по данным. Возвращает итоговый dict (см. docstring модуля)."""
    if isinstance(data, dict) and data.get("инн"):
        contract = _load_sibling("evidence_contract")
        normalized_financial_input, prepare = contract.normalized_financial_input, contract.prepare
        data = normalized_financial_input(prepare(data))
    identity = input_identity(data)
    годы_raw, root = _найти_финансы(data)
    if not годы_raw:
        return {
            "_input_identity": identity,
            "статус": "не проверено",
            "причина": "во входных данных нет блока «финансы»/«отчётность_по_годам» "
                       "(ГИР БО не отдал отчётность или скрипт не запускался)",
            "источник": "ГИР БО",
        }
    годы, проблемы_периодов = _периоды(годы_raw)
    if not годы:
        return {
            "_input_identity": identity,
            "статус": "не проверено",
            "причина": "нет отчётности с корректным календарным годом",
            "источник": "ГИР БО",
        }

    uk = _уставный_капитал(root)
    staff, staff_year, staff_source = _численность(root)
    cur = годы[0]
    cur_year = _год_карточки(cur)
    prev = next((g for g in годы if _год_карточки(g) == cur_year - 1), None)
    pair = [cur, prev or {}]

    флаги = []
    не_проверено = list(проблемы_периодов)
    оценки = {}

    def оценка(sid, dependencies, reason=None):
        оценки[sid] = {"статус": "не проверен" if reason else "отсутствует",
                       "зависимости": dependencies, "причина": reason,
                       "достаточные_зависимости": list(dependencies)}
        if reason:
            не_проверено.append(reason)

    оценка("ча_отрицательные_2года", ["1300:%s" % cur_year, "1300:%s" % (cur_year - 1)],
           "чистые активы два года подряд — нужны строки 1300 за %s и %s"
           % (cur_year, cur_year - 1) if any(_строка(g, "1300") is None for g in pair) else None)
    оценка("ча_ниже_уставного_капитала", ["1300:%s" % cur_year, "егрюл.уставный_капитал"],
           "чистые активы vs уставный капитал — нет строки 1300 или уставного капитала"
           if _строка(cur, "1300") is None or uk is None else None)

    def флаг(fid, severity, показатель, значение, порог, пояснение, год):
        оценки[fid]["статус"] = "найден"
        флаги.append({
            "id": fid,
            "severity": severity,
            "показатель": показатель,
            "значение": _round(значение),
            "порог": порог,
            "пояснение": пояснение,
            "источник": "ГИР БО",
            "год": год,
            # сырые строки всех оценённых лет — traceability к первичке
            "строки_баланса": {str(_год_карточки(g)): _сырые_строки(g) for g in годы},
        })

    # 1. Чистые активы (стр. 1300) vs ноль и уставный капитал.
    cha = [_строка(g, "1300") for g in pair]
    if len(cha) >= 2 and all(v is not None for v in cha[:2]) \
            and cha[0] < 0 and cha[1] < 0:
        флаг("ча_отрицательные_2года", "🔴",
             "чистые активы (стр. 1300)", cha[0], "< 0 два года подряд",
             "отрицательные чистые активы два года подряд — риск обязательной "
             "ликвидации по требованию ФНС (ст. 30 ФЗ-14 «Об ООО»). Deal-killer "
             "наравне с ликвидацией: компания формально должна снизить УК или "
             "ликвидироваться",
             "%s–%s" % (_год_карточки(prev), cur_year))
    if cha[0] is None:
        не_проверено.append("чистые активы (стр. 1300) — нет строки за последний год")
    elif uk is not None and cha[0] < uk:
        флаг("ча_ниже_уставного_капитала", "🟡",
             "чистые активы (стр. 1300)", cha[0],
             "< уставного капитала %.0f тыс. руб." % uk,
             "чистые активы ниже уставного капитала — по ст. 30 ФЗ-14 компания "
             "обязана снизить УК или ликвидироваться",
             cur_year)

    # 2. Коэффициент автономии = 1300/1600.
    активы = _строка(cur, "1600")
    оценка("коэффициент_автономии", ["1300:%s" % cur_year, "1600:%s" % cur_year],
           "коэффициент автономии — нет строк 1300/1600 или активы равны нулю"
           if cha[0] is None or not активы else None)
    if cha[0] is not None and активы:
        автономия = cha[0] / активы
        if автономия < ПОРОГ_АВТОНОМИИ:
            флаг("коэффициент_автономии", "🟡",
                 "автономия (1300/1600)", автономия, "< 0.1",
                 "компания живёт на чужие деньги: собственного капитала меньше "
                 "10% от активов", cur_year)
    else:
        не_проверено.append("коэффициент автономии — нет строк 1300/1600")

    # 3. Текущая ликвидность = 1200/1500.
    оборотные = _строка(cur, "1200")
    краткоср = _строка(cur, "1500")
    оценка("текущая_ликвидность", ["1200:%s" % cur_year, "1500:%s" % cur_year],
           "текущая ликвидность — нет строк 1200/1500 или обязательства равны нулю"
           if оборотные is None or not краткоср else None)
    if оборотные is not None and краткоср:
        ликвидность = оборотные / краткоср
        if ликвидность < ПОРОГ_ЛИКВИДНОСТИ:
            флаг("текущая_ликвидность", "🟡",
                 "ликвидность (1200/1500)", ликвидность, "< 1",
                 "краткосрочных обязательств больше, чем оборотных активов — "
                 "компания не закрывает текущие долги оборотом", cur_year)
    else:
        не_проверено.append("текущая ликвидность — нет строк 1200/1500")

    # 4. Динамика выручки (стр. 2110).
    выручка = [_строка(g, "2110") for g in pair]
    revenue_reason = ("динамика выручки — нужны строки 2110 за %s и %s и положительная база"
                      % (cur_year, cur_year - 1)
                      if prev is None or any(v is None for v in выручка) or выручка[1] <= 0 else None)
    оценка("падение_выручки", ["2110:%s" % cur_year, "2110:%s" % (cur_year - 1)], revenue_reason)
    оценка("транзитный_профиль", ["2110:%s" % cur_year, "2110:%s" % (cur_year - 1),
                                 "численность_сотрудников"], revenue_reason)
    if prev is not None and all(v is not None for v in выручка) and выручка[1] > 0:
        падение = (выручка[1] - выручка[0]) / выручка[1]
        if падение > ПОРОГ_ПАДЕНИЯ_ВЫРУЧКИ:
            флаг("падение_выручки", "🟡",
                 "выручка (стр. 2110)", падение * 100, "падение > 50% г/г",
                 "выручка упала на %.0f%% за год — бизнес схлопывается"
                 % (падение * 100), cur_year)
        рост = (выручка[0] - выручка[1]) / выручка[1]
        if рост <= ПОРОГ_РОСТА_ТРАНЗИТА:
            # This false clause alone proves the conjunction false. Preserve
            # the legacy input list; record the actual sufficient proof beside it.
            оценки["транзитный_профиль"]["достаточные_зависимости"] = [
                "2110:%s" % cur_year, "2110:%s" % (cur_year - 1)]
        # 6. Транзитный профиль: рост > 300% при численности ~0.
        staff_valid = staff is not None and (staff_year is None or staff_year == cur_year)
        if staff_source == "дампы_фнс" and staff_year is None:
            staff_valid = False
        if рост > ПОРОГ_РОСТА_ТРАНЗИТА and not staff_valid:
            оценка("транзитный_профиль", оценки["транзитный_профиль"]["зависимости"],
                   "транзитный профиль — численность не проверена за %s (источник: %s, год: %s)"
                   % (cur_year, staff_source or "нет", staff_year or "не указан"))
        if рост > ПОРОГ_РОСТА_ТРАНЗИТА and staff_valid \
                and staff <= ПОРОГ_ЧИСЛЕННОСТИ_ТРАНЗИТА:
            флаг("транзитный_профиль", "🟡",
                 "рост выручки (стр. 2110)", рост * 100, "> 300% при численности ~0",
                 "выручка выросла более чем в 4 раза при численности %s чел. — "
                 "профиль транзитной прокладки, а не операционного бизнеса"
                 % int(staff), cur_year)
    else:
        не_проверено.append("динамика выручки — нужна строка 2110 за 2 года")

    # 5. Убыток 2+ года подряд (стр. 2400).
    прибыль = [_строка(g, "2400") for g in pair]
    оценка("убыток_2года", ["2400:%s" % cur_year, "2400:%s" % (cur_year - 1)],
           "убыток 2+ года — нужны строки 2400 за %s и %s" % (cur_year, cur_year - 1)
           if prev is None or any(v is None for v in прибыль) else None)
    if prev is not None and all(v is not None for v in прибыль):
        if прибыль[0] < 0 and прибыль[1] < 0:
            флаг("убыток_2года", "🟡",
                 "чистая прибыль (стр. 2400)", прибыль[0], "< 0 два года подряд",
                 "хронический убыток — компания проедает капитал",
                 "%s–%s" % (_год_карточки(prev), cur_year))
    else:
        не_проверено.append("убыток 2+ года — нужна строка 2400 за 2 года")

    красных = sum(1 for f in флаги if f["severity"] == "🔴")
    сводка = ("флагов: %d (🔴 %d, 🟡 %d); оценено лет: %d"
              % (len(флаги), красных, len(флаги) - красных, len(годы)))
    return {
        "_input_identity": identity,
        "статус": "ок",
        "флаги": флаги,
        "версия_оценок": 1,
        "оценки_сигналов": оценки,
        "финансовый_профиль": {
            "сводка": сводка,
            "оценено_лет": len(годы),
            "года": [_год_карточки(g) for g in годы],
            # ключ переименован: значение приведено к единицам отчётности,
            # и старое имя читалось бы как рубли
            "уставный_капитал_тыс_руб": _round(uk),
            "численность": _round(staff),
            "численность_год": staff_year,
            "численность_источник": staff_source,
            "не_проверено": не_проверено,
            "строки_баланса": {str(_год_карточки(g)): _сырые_строки(g) for g in годы},
        },
    }


def main(argv):
    # JSON CLI protocol is UTF-8 independently of the host console code page.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = argv[1:]
    if not args:
        sys.stderr.write(
            "Использование: python3 fin_scoring.py <file.json> | --stdin\n")
        return 2
    try:
        if args[0] == "--stdin":
            raw = sys.stdin.read()
        else:
            with open(args[0], encoding="utf-8") as fh:
                raw = fh.read()
        data = json.loads(raw)
    except (OSError, ValueError) as e:
        out = {"статус": "не проверено",
               "причина": "вход не читается как JSON (%s)" % e}
        sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
        return 1
    out = score(data)
    sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
