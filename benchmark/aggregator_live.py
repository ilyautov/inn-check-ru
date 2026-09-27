#!/usr/bin/env python3
"""
aggregator_live.py — живая сверка контракта агрегатора (блок D волны 5) по ключу.

    CHECKO_API_KEY=… python3 benchmark/aggregator_live.py [ИНН ...]
    DADATA_API_KEY=… python3 benchmark/aggregator_live.py [ИНН ...]

Без аргументов — ИНН из benchmark/cohort.json (10 запросов на провайдера: у
Checko бесплатный лимит 100 в сутки). Для каждого провайдера, чей ключ задан:

- один запрос на ИНН тем же кодом, что в движке (fetch_counterparty);
- сверка контракта: какие узлы с отметками пришли на самом деле (контракт в
  sources.py написан по документации и живьём не проверялся);
- разбор парсером движка — что он сделал бы с этим ответом;
- сырой ответ пишется фикстурой в eval/fixtures/aggregator/<провайдер>_<ИНН>.json:
  ФИО и ИНН физлиц -> «ФИО УБРАНО ИЗ ФИКСТУРЫ», контакты удалены. Если после
  маскирования в ответе остался ключ или 12-значный ИНН, фикстура НЕ пишется.

Только юрлица (10-значный ИНН): запись ИП в ответе агрегатора — сплошь
персональные данные (ФИО в названии, ИНН физлица, город проживания).

Ключ в отчёт, фикстуры и stdout не попадает. Отчёт — JSON в stdout.
"""

import json
import os
import re
import sys
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "scripts"))

import fetch_counterparty as fc

ФИКСТУРЫ = ROOT / "eval" / "fixtures" / "aggregator"
МАСКА = "ФИО УБРАНО ИЗ ФИКСТУРЫ"
КЛЮЧ_УБРАН = "КЛЮЧ УБРАН"
ПРОВАЙДЕРЫ = (("checko", "CHECKO_API_KEY"), ("dadata", "DADATA_API_KEY"))
# Контакты бывают личными (у ИП и у руководителей) — в фикстуре не нужны.
КОНТАКТЫ = {"Контакты", "phones", "emails"}
# DaData: физлицо в founders/managers и сама запись ИП.
ТИПЫ_ФИЗЛИЦ = {"PHYSICAL", "EMPLOYEE", "INDIVIDUAL"}


def _сырой(провайдер, ключ, opener, inn):
    """(HTTP-статус или None, JSON или None) — ровно те запросы, что шлёт движок."""
    if провайдер == "checko":
        url = "%s?%s" % (fc.CHECKO, urllib.parse.urlencode({"key": ключ, "inn": inn}))
        status, text = fc._http_get(opener, url, ua=fc.UA_ПРОЕКТА, повторы=False, xhr=False)
        return status, fc._safe_json(text)
    text = fc._http_post(opener, fc.DADATA, json.dumps({"query": inn}).encode("utf-8"), {
        "User-Agent": fc.UA_ПРОЕКТА, "Content-Type": "application/json",
        "Accept": "application/json", "Authorization": "Token " + ключ})
    return None, fc._safe_json(text)


def замаскировать(узел, ключ, физлицо=False):
    """Копия ответа без ФИО, ИНН физлиц, контактов и ключа.

    Checko: объект с полем «ФИО» — физлицо (руководитель, учредитель-ФЛ).
    DaData: management.name, объекты fio, founders/managers с type из ТИПЫ_ФИЗЛИЦ.
    """
    if isinstance(узел, list):
        return [замаскировать(x, ключ, физлицо) for x in узел]
    if isinstance(узел, str):
        return узел.replace(ключ, КЛЮЧ_УБРАН) if ключ and ключ in узел else узел
    if not isinstance(узел, dict):
        return узел
    лицо = физлицо or "ФИО" in узел or узел.get("type") in ТИПЫ_ФИЗЛИЦ
    out = {}
    for k, v in узел.items():
        if k in КОНТАКТЫ:
            continue
        if k == "fio" or (лицо and k in ("ФИО", "ИНН", "name", "inn", "hid")):
            out[k] = МАСКА if v not in (None, "") else v
        elif k == "management" and isinstance(v, dict):
            out[k] = замаскировать(v, ключ, физлицо=True)
        else:
            out[k] = замаскировать(v, ключ, физлицо=False)
    return out


def утечки(замаскированный, ключ, inn):
    """Что осталось после маскирования: ключ и 12-значные ИНН (физлица/ИП)."""
    текст = json.dumps(замаскированный, ensure_ascii=False)
    найдено = []
    if ключ and ключ in текст:
        найдено.append("ключ")
    лишние = sorted(set(re.findall(r"(?<!\d)\d{12}(?!\d)", текст)) - {inn})
    if лишние:
        найдено.append("12-значных ИНН: %d" % len(лишние))
    return найдено


def контракт_checko(j):
    """Какие узлы с отметками пришли: сверка с контрактом по документации."""
    d = (j or {}).get("data") if isinstance(j, dict) else None
    if not isinstance(d, dict):
        return {"data": False}
    рук = d.get("Руковод") if isinstance(d.get("Руковод"), list) else None
    учред = d.get("Учред")
    return {
        "data": True,
        "ЮрАдрес.Недост": isinstance(d.get("ЮрАдрес"), dict) and "Недост" in d["ЮрАдрес"],
        "Руковод": None if рук is None else len(рук),
        "Руковод[].Недост": рук is not None and all(
            isinstance(r, dict) and "Недост" in r for r in рук),
        "Руковод[].ДисквЛицо": рук is not None and all(
            isinstance(r, dict) and "ДисквЛицо" in r for r in рук),
        "Учред": sorted(учред) if isinstance(учред, dict) else type(учред).__name__,
        "УпрОрг в ответе": "УпрОрг" in d,
        "ДисквЛица": d.get("ДисквЛица"),
        "meta": {k: v for k, v in (j.get("meta") or {}).items() if k in ("status", "message")},
    }


def контракт_dadata(j, inn):
    """Та же запись, что выбирает parse_dadata: ИНН совпал, головная (MAIN/None)."""
    s = (j or {}).get("suggestions") if isinstance(j, dict) else None
    if not isinstance(s, list):
        return {"suggestions": False}
    головные = [x["data"] for x in s if isinstance(x, dict) and isinstance(x.get("data"), dict)
                and str(x["data"].get("inn") or "") == str(inn)
                and x["data"].get("branch_type") in ("MAIN", None)]
    if len(головные) != 1:
        return {"suggestions": len(s), "головных с этим ИНН": len(головные)}
    d = головные[0]
    return {"suggestions": len(s), "invalid в ответе": "invalid" in d,
            "invalid": d.get("invalid"),
            "management.disqualified": (d.get("management") or {}).get("disqualified")
            if isinstance(d.get("management"), dict) else "нет management",
            "branch_type": d.get("branch_type")}


def проверить(провайдер, ключ, opener, inn):
    try:
        status, j = _сырой(провайдер, ключ, opener, inn)
    except Exception as e:  # любой сбой сети — строка отчёта
        return {"инн": inn, "ошибка": str(e).replace(ключ, КЛЮЧ_УБРАН)[:300]}
    стр = {"инн": inn, "http": status}
    if j is None:
        стр["ошибка"] = "не-JSON"
        return стр
    стр["контракт"] = (контракт_checko(j) if провайдер == "checko"
                       else контракт_dadata(j, inn))
    данные, av = (fc.parse_checko if провайдер == "checko" else fc.parse_dadata)(j, inn)
    данные = данные if isinstance(данные, dict) else {}  # отказ парсера — данных нет
    стр["движок"] = {"состояние": av.get("состояние"), "причина": av.get("причина"),
                     **{k: данные.get(k) for k in ("недостоверность_сведений",
                                                   "дисквалификация_руководителя")}}
    маск = замаскировать(j, ключ)
    стр["утечки"] = утечки(маск, ключ, inn)
    if стр["утечки"]:
        стр["фикстура"] = "не записана — утечки после маскирования"
    else:
        ФИКСТУРЫ.mkdir(parents=True, exist_ok=True)
        путь = ФИКСТУРЫ / ("%s_%s.json" % (провайдер, inn))
        путь.write_text(json.dumps(маск, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        стр["фикстура"] = str(путь.relative_to(ROOT))
    return стр


def годный_инн(inn):
    """Только юрлицо: 10 цифр с верной контрольной суммой (ИП — см. докстринг модуля)."""
    return len(inn) == 10 and fc.validate_inn(inn) is not None


def main(argv):
    инн = argv[1:] or [c["инн"] for c in json.loads(
        (HERE / "cohort.json").read_text(encoding="utf-8"))["контрагенты"]]
    for i in инн:
        if not годный_инн(i):
            print(json.dumps({"ошибка": "нужен ИНН юрлица (10 цифр): %s" % i},
                             ensure_ascii=False))
            return 2
    ключи = [(п, (os.environ.get(имя) or "").strip()) for п, имя in ПРОВАЙДЕРЫ]
    ключи = [(п, к) for п, к in ключи if к]
    if not ключи:
        print(json.dumps({"ошибка": "нет ключа: CHECKO_API_KEY или DADATA_API_KEY"},
                         ensure_ascii=False))
        return 2
    opener = fc._make_opener()
    отчёт = {п: [проверить(п, к, opener, i) for i in инн] for п, к in ключи}
    текст = json.dumps(отчёт, ensure_ascii=False, indent=1)
    for _, к in ключи:
        текст = текст.replace(к, КЛЮЧ_УБРАН)
    print(текст)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
