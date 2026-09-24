#!/usr/bin/env python3
"""
evaluate.py — метрики порогов fin_scoring на собранной выборке (задача 5).

    python3 evaluate.py                  # report.json + cohort_2021.csv рядом
    python3 evaluate.py --повторы 200    # быстрый прогон бутстрепа

Признаки считает КОД ДВИЖКА, а не копия: сырые ответы ГИР БО (только периоды ≤
2021) -> fetch_counterparty.parse_finance -> fin_scoring.score. Калибруется ровно
то, что видит пользователь.

Веса двухфазной выборки. Фаза 1 — равномерная когорта (случайные id ГИР БО),
вес 1. Фаза 2 — все случаи и доля f выживших. Выживший в
фазе 2 весит w/f, случай — w. На этих весах точность (PPV) и лифт относятся к
генеральной совокупности, а не к искусственно обогащённой выборке.
"""

import argparse
import csv
import json
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import collect
import label

ОТЧЁТНЫЙ_ГОД = label.ОТЧЁТНЫЙ_ГОД
ОПЕРАЦИОННАЯ_ВЫРУЧКА = 10000     # тыс. руб. (ГИР БО в тысячах): 10 млн ₽

# Сетки перебора порогов: (id признака, направление, значения). Действующий порог
# движка обязан входить в сетку — иначе сравнивать новое не с чем.
СЕТКИ = {
    "автономия": ("<", [0.0, 0.05, 0.1, 0.15, 0.2, 0.3, 0.4]),
    "ликвидность": ("<", [0.5, 0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.5]),
    "падение_выручки": (">", [0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]),
}


def _движок():
    import fetch_counterparty as fc
    import fin_scoring
    return fc, fin_scoring


def _форма_прошлого_года(detail):
    """Форма года N -> форма года N-1 из её колонок previous* (как current*).

    Список /bfo/ ГИР БО отдаёт только 5 последних периодов: в 2026 это 2021–2025,
    отдельной формы за 2020 нет. Пользователь в 2022 году её видел бы — а её
    цифры есть в форме 2021 как «на предыдущую дату / за предыдущий год».
    """
    form = detail[0] if isinstance(detail, list) and detail else detail
    if not isinstance(form, dict):
        return None
    out = {}
    for часть in ("balance", "financialResult"):
        блок = form.get(часть)
        if isinstance(блок, dict):
            out[часть] = {"current" + k[len("previous"):]: v
                          for k, v in блок.items() if k.startswith("previous")}
    return [out] if out else None


def только_до(raw, год=ОТЧЁТНЫЙ_ГОД):
    """Сырые ответы ГИР БО без периодов позже отчётного года (без утечки будущего).
    Нет отдельной формы за год-1 — она собирается из колонок previous* формы года."""
    bfo = []
    for rec in raw.get("bfo") or []:
        try:
            if int(rec.get("period")) <= год:
                bfo.append(rec)
        except (TypeError, ValueError):
            continue
    periods = {str(r.get("period")) for r in bfo}
    details = raw.get("details") if isinstance(raw.get("details"), dict) else {}
    if str(год) in periods and str(год - 1) not in periods:
        rec = next(r for r in bfo if str(r.get("period")) == str(год))
        прошлая = _форма_прошлого_года(details.get(str(rec.get("id"))))
        if прошлая:
            ид = "%s-previous" % rec.get("id")
            bfo.append({"id": ид, "period": str(год - 1)})
            details = dict(details, **{ид: прошлая})
    return dict(raw, bfo=bfo, details=details)


def признаки(raw, inn, fc=None, fs=None):
    """Сырые ответы -> {флаги движка, непрерывные показатели} или {"нет": причина}."""
    if fc is None:
        fc, fs = _движок()
    fin, av = fc.parse_finance(только_до(raw), inn)
    if not fin:
        return {"нет": "ГИР БО: %s" % (av or {}).get("причина", "нет отчётности")}
    годы = sorted(fin["отчётность_по_годам"], key=fs._год_карточки, reverse=True)
    if not годы or fs._год_карточки(годы[0]) != ОТЧЁТНЫЙ_ГОД:
        return {"нет": "нет отчётности за %d" % ОТЧЁТНЫЙ_ГОД}
    cur = годы[0]
    prev = годы[1] if len(годы) > 1 and fs._год_карточки(годы[1]) == ОТЧЁТНЫЙ_ГОД - 1 else None
    s = lambda g, c: fs._строка(g, c) if g is not None else None
    ча, активы = s(cur, "1300"), s(cur, "1600")
    об, кр = s(cur, "1200"), s(cur, "1500")
    в0, в1 = s(cur, "2110"), s(prev, "2110")
    п0, п1 = s(cur, "2400"), s(prev, "2400")
    ча1 = s(prev, "1300")
    out = {
        "флаги": sorted({f["id"] for f in fs.score(fin).get("флаги", [])}),
        "выручка": в0,
        "автономия": ча / активы if ча is not None and активы else None,
        "ликвидность": об / кр if об is not None and кр else None,
        "падение_выручки": (в1 - в0) / в1 if в0 is not None and в1 and в1 > 0 else None,
        "ча_отрицательные_2года": (ча < 0 and ча1 < 0) if ча is not None and ча1 is not None else None,
        "убыток_2года": (п0 < 0 and п1 < 0) if п0 is not None and п1 is not None else None,
    }
    return out


# ---------------------------------------------------------------------------
# Метрики
# ---------------------------------------------------------------------------

def метрики(строки, флаг):
    """строки: [{"случай": bool, "вес": float, ...}]; флаг(строка) -> True/False/None.
    None — показатель не посчитан: строка не входит ни в числитель, ни в знаменатель
    (доля таких — «покрытие»). Веса — двухфазные (см. docstring модуля)."""
    tp = fn = fp = tn = 0.0
    вес_всего = пропущено = 0.0
    for r in строки:
        вес_всего += r["вес"]
        f = флаг(r)
        if f is None:
            пропущено += r["вес"]
            continue
        if r["случай"]:
            tp, fn = (tp + r["вес"], fn) if f else (tp, fn + r["вес"])
        else:
            fp, tn = (fp + r["вес"], tn) if f else (fp, tn + r["вес"])
    n = tp + fn + fp + tn
    if not n or not (tp + fn):
        return None
    база = (tp + fn) / n
    ppv = tp / (tp + fp) if tp + fp else None
    return {
        "доля_флагнутых": (tp + fp) / n,
        "полнота": tp / (tp + fn),
        "ложные_срабатывания": fp / (fp + tn) if fp + tn else None,
        "точность": ppv,
        "базовая_частота": база,
        "лифт": ppv / база if ppv is not None and база else None,
        "покрытие": (вес_всего - пропущено) / вес_всего if вес_всего else None,
    }


def бутстреп(строки, флаг, повторы, seed):
    """95% ДИ перцентилями; случаи и контроли ресэмплятся раздельно (стратифицированно:
    так сохраняется дизайн фазы 2)."""
    rng = random.Random(seed)
    случаи = [r for r in строки if r["случай"]]
    контроли = [r for r in строки if not r["случай"]]
    выборки = {"полнота": [], "точность": [], "лифт": []}
    for _ in range(повторы):
        б = ([rng.choice(случаи) for _ in случаи] + [rng.choice(контроли) for _ in контроли])
        m = метрики(б, флаг)
        if not m:
            continue
        for k, значения in выборки.items():
            if m.get(k) is not None:
                значения.append(m[k])
    ди = {}
    for k, v in выборки.items():
        if len(v) >= max(20, повторы // 2):
            v.sort()
            ди[k] = [v[int(0.025 * (len(v) - 1))], v[int(0.975 * (len(v) - 1))]]
    return ди


def _порог(имя, знак, t):
    def f(r):
        v = r.get(имя)
        if v is None:
            return None
        return v < t if знак == "<" else v > t
    return f


# Флаг движка -> показатель, без которого движок его не считает (уводит в
# «не_проверено»). Строка без показателя — не «не флагнута», а вне расчёта: иначе
# ликвидность, которой у ~2/3 малых компаний нет (упрощённый баланс без строк
# 1200/1500), показала бы покрытие 100% и заниженную долю флагнутых.
ПОКАЗАТЕЛЬ_ФЛАГА = {
    "коэффициент_автономии": "автономия",
    "текущая_ликвидность": "ликвидность",
    "падение_выручки": "падение_выручки",
    "ча_отрицательные_2года": "ча_отрицательные_2года",
    "убыток_2года": "убыток_2года",
}


def _флаг_движка(fid):
    показатель = ПОКАЗАТЕЛЬ_ФЛАГА.get(fid)

    def f(r):
        if fid in r["флаги"]:
            return True
        if показатель and r.get(показатель) is None:
            return None
        return False
    return f


def _булев(имя):
    return lambda r: r.get(имя)


# ---------------------------------------------------------------------------
# Сборка
# ---------------------------------------------------------------------------

def собрать():
    """Кэш collect.py -> (строки фазы 2, сводка по когорте)."""
    fc, fs = _движок()
    rows = collect.прочитать_когорту()
    по_инн = {r["inn"]: r for r in rows}
    классы = {}
    for r in rows:
        ef = collect._читать_json(collect._путь("efrsb", r["inn"] + ".json"))
        if ef is None:
            continue
        классы[r["inn"]] = label.исход(r["inn"], ef["ответ"])
    p2 = collect._читать_json(collect._путь("phase2.json"))
    if not p2:
        raise collect.Стоп("сначала: collect.py гирбо")
    выжившие = [i for i, к in классы.items() if к["класс"] == "выжил"]
    f = p2["контролей"] / len(выжившие) if выжившие else 1.0
    строки, выпало = [], {}
    for inn in p2["инн"]:
        raw = collect._читать_json(collect._путь("girbo", inn + ".json"))
        к = классы.get(inn, {}).get("класс")
        if raw is None or к not in ("банкрот", "выжил"):
            выпало["нет сырых данных/исхода"] = выпало.get("нет сырых данных/исхода", 0) + 1
            continue
        п = признаки(raw, inn, fc, fs)
        if "нет" in п:
            выпало[п["нет"]] = выпало.get(п["нет"], 0) + 1
            continue
        случай = к == "банкрот"
        строки.append(dict(п, inn=inn, случай=случай, класс=к,
                           год_дела=классы[inn].get("год_дела"),
                           стадия=классы[inn].get("стадия"),
                           okved=по_инн[inn].get("okved2"),
                           окопф=по_инн[inn].get("okopf"),
                           вес=1.0 if случай else 1.0 / f))
    сводка = {
        "когорта": len(rows), "с_исходом": len(классы),
        "классы": {k: sum(1 for x in классы.values() if x["класс"] == k)
                   for k in ("банкрот", "выжил", "исключён", "не проверено")},
        "доля_контролей_фазы2": f, "фаза2_в_расчёте": len(строки),
        "фаза2_выпало": выпало,
    }
    return строки, сводка


def отчёт(строки, сводка, повторы, seed):
    def блок(подмножество):
        res = {"n": len(подмножество), "случаев": sum(1 for r in подмножество if r["случай"]),
               "флаги_движка": {}, "сетки": {}}
        for fid in ("ча_отрицательные_2года", "коэффициент_автономии", "текущая_ликвидность",
                    "падение_выручки", "убыток_2года", "ча_ниже_уставного_капитала"):
            m = метрики(подмножество, _флаг_движка(fid))
            if m:
                m["ди95"] = бутстреп(подмножество, _флаг_движка(fid), повторы, seed)
            res["флаги_движка"][fid] = m
        for k in ("любой", "2+", "3+"):
            порог = {"любой": 1, "2+": 2, "3+": 3}[k]
            fn = lambda r, п=порог: len(r["флаги"]) >= п
            m = метрики(подмножество, fn)
            if m:
                m["ди95"] = бутстреп(подмножество, fn, повторы, seed)
            res["флаги_движка"]["флагов_" + k] = m
        for имя, (знак, сетка) in СЕТКИ.items():
            res["сетки"][имя] = []
            for t in сетка:
                m = метрики(подмножество, _порог(имя, знак, t))
                if m:
                    m["ди95"] = бутстреп(подмножество, _порог(имя, знак, t), повторы, seed)
                res["сетки"][имя].append({"порог": "%s %s" % (знак, t), "метрики": m})
        return res

    return {
        "дизайн": "docs/superpowers/specs/2026-09-23-task5-threshold-calibration-design.md",
        "отчётный_год": ОТЧЁТНЫЙ_ГОД, "окно_исхода": list(label.ОКНО),
        "бутстреп": {"повторы": повторы, "seed": seed},
        "сводка": сводка,
        "все": блок(строки),
        "операционные": блок([r for r in строки
                              if (r.get("выручка") or 0) >= ОПЕРАЦИОННАЯ_ВЫРУЧКА]),
        # Чувствительность: «производство прекращено» (ProceedingsStopped) бывает и
        # без введённой процедуры (долг погашен, нет средств на процедуру). Если
        # цифры без таких случаев заметно другие — это видно здесь, а не спрятано.
        "без_прекращённых": блок([r for r in строки
                                  if not (r["случай"] and r.get("стадия") == "ProceedingsStopped")]),
    }


# Таблицы для references/kalibrovka.md — только из report.json, руками не набираются.
# eval/run_calibration_eval.py сверяет раздел между маркерами с отчётом.
СПРАВОЧНИК = os.path.join(ROOT, "references", "kalibrovka.md")
МАРКЕР_НАЧАЛО = "<!-- таблицы из calibration/report.json: начало -->"
МАРКЕР_КОНЕЦ = "<!-- таблицы из calibration/report.json: конец -->"
ДЕЙСТВУЮЩИЕ = {"автономия": "< 0.1", "ликвидность": "< 1.0", "падение_выручки": "> 0.5"}
_ИМЕНА_БЛОКОВ = {
    "все": "Все компании когорты",
    "операционные": "Операционные: выручка 2021 ≥ 10 млн ₽",
    "без_прекращённых": "Чувствительность: без дел «производство прекращено»",
}


def _п(x, знаков=1):
    return "—" if x is None else ("%.*f%%" % (знаков, 100 * x)).replace(".", ",")


def _ч(x):
    return "—" if x is None else ("%.2f" % x).replace(".", ",")


def _ди(m, ключ, fmt):
    ди = (m.get("ди95") or {}).get(ключ)
    return "—" if not ди else "%s–%s" % (fmt(ди[0]), fmt(ди[1]))


def _строка_md(имя, m):
    if not m:
        return "| %s | нет случаев | | | | | |" % имя
    return "| %s | %s | %s (%s) | %s | %s (%s) | %s |" % (
        имя, _п(m["доля_флагнутых"]), _п(m["полнота"], 0), _ди(m, "полнота", lambda x: _п(x, 0)),
        _п(m["точность"], 2), _ч(m["лифт"]), _ди(m, "лифт", _ч), _п(m["покрытие"], 0))


def таблицы_md(rep):
    шапка = ("| | флагнуто | полнота (95% ДИ) | точность | лифт (95% ДИ) | покрытие |\n"
             "|---|---|---|---|---|---|")
    out = []
    for ключ, заголовок in _ИМЕНА_БЛОКОВ.items():
        б = rep[ключ]
        база = next((m["базовая_частота"] for m in б["флаги_движка"].values() if m), None)
        out.append("### %s\n\nВ расчёте %d компаний, из них %d банкротов; базовая частота "
                   "банкротства на весах — %s.\n" % (заголовок, б["n"], б["случаев"], _п(база, 2)))
        out.append("**Флаги движка при действующих порогах**\n\n" + шапка)
        for fid, m in б["флаги_движка"].items():
            out.append(_строка_md("`%s`" % fid, m))
        for имя, ряд in б["сетки"].items():
            out.append("\n**%s: перебор порога**\n\n%s" % (имя, шапка))
            for т in ряд:
                метка = "%s%s" % (т["порог"], " ← действующий" if т["порог"] == ДЕЙСТВУЮЩИЕ.get(имя) else "")
                out.append(_строка_md(метка, т["метрики"]))
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def вписать_в_справочник(rep, путь=СПРАВОЧНИК):
    with open(путь, encoding="utf-8") as f:
        текст = f.read()
    до, _, хвост = текст.partition(МАРКЕР_НАЧАЛО)
    _, _, после = хвост.partition(МАРКЕР_КОНЕЦ)
    if not хвост or МАРКЕР_КОНЕЦ not in хвост:
        raise SystemExit("в %s нет маркеров таблиц" % путь)
    with open(путь, "w", encoding="utf-8") as f:
        f.write(до + МАРКЕР_НАЧАЛО + "\n\n" + таблицы_md(rep) + "\n" + МАРКЕР_КОНЕЦ + после)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--повторы", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=2021)
    ap.add_argument("--справочник", action="store_true",
                    help="только переписать таблицы references/kalibrovka.md из report.json")
    a = ap.parse_args(argv)
    if a.справочник:
        with open(os.path.join(HERE, "report.json"), encoding="utf-8") as f:
            вписать_в_справочник(json.load(f))
        return 0
    try:
        строки, сводка = собрать()
    except collect.Стоп as e:
        print("СТОП: %s" % e, file=sys.stderr)
        return 3
    rep = отчёт(строки, сводка, a.повторы, a.seed)
    with open(os.path.join(HERE, "report.json"), "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)
    поля = ["inn", "класс", "год_дела", "стадия", "вес", "okved", "окопф", "выручка",
            "автономия", "ликвидность", "падение_выручки", "ча_отрицательные_2года",
            "убыток_2года", "флаги"]
    with open(os.path.join(HERE, "cohort_2021.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(поля)
        for r in sorted(строки, key=lambda x: x["inn"]):
            w.writerow([";".join(r[k]) if k == "флаги" else
                        ("%.6g" % r[k] if isinstance(r.get(k), float) else r.get(k))
                        for k in поля])
    print(json.dumps(rep["сводка"], ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
