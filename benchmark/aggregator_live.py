#!/usr/bin/env python3
"""
aggregator_live.py — живая сверка контракта агрегатора (блок D волны 5) по ключу.

    CHECKO_API_KEY=… python3 benchmark/aggregator_live.py [ИНН ...]
    DADATA_API_KEY=… python3 benchmark/aggregator_live.py [ИНН ...]

Без аргументов — ИНН из benchmark/cohort.json (10 запросов на провайдера: у
Checko бесплатный лимит 100 в сутки). Для каждого провайдера, чей ключ задан:

- один запрос на ИНН тем же кодом, что в движке (fetch_counterparty);
- сверка контракта: какие узлы с отметками пришли на самом деле (сверка
  27.09.2026 — benchmark/контракт_агрегатора_2026-09-27.json);
- разбор парсером движка — что он сделал бы с этим ответом;
- ответ пишется фикстурой в eval/fixtures/aggregator/<провайдер>_<ИНН>.json по
  белому списку (минимизировать): ИНН компании, булевы отметки Недост/ДисквЛицо
  по узлам, дата отметки у юрадреса, invalid/branch_type DaData. Перед этим ФИО и ИНН физлиц маскируются, контакты
  и списки связанных компаний удаляются — на случай расширения белого списка.
  Если после этого остался ключ или 12-значный ИНН, фикстура НЕ пишется.

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
# Checko: списки связанных компаний (у физлица — его другие компании, у самой
# компании — связи через общего руководителя/учредителя). По набору ОГРН человек
# находится в ЕГРЮЛ и без ФИО (живой ответ 27.09.2026). Парсеру движка не нужны.
СВЯЗИ = {"СвязРуковод", "СвязУчред"}


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
    тип = узел.get("type")
    лицо = физлицо or "ФИО" in узел or (isinstance(тип, str) and тип in ТИПЫ_ФИЗЛИЦ)
    out = {}
    for k, v in узел.items():
        if k in КОНТАКТЫ or k in СВЯЗИ:
            continue
        if k == "fio" or (лицо and k in ("ФИО", "ИНН", "name", "inn", "hid")):
            out[k] = МАСКА if v not in (None, "") else v
        elif k == "management" and isinstance(v, dict):
            out[k] = замаскировать(v, ключ, физлицо=True)
        else:
            out[k] = замаскировать(v, ключ, физлицо=False)
    return out


class НеМинимизируется(ValueError):
    """Узел неожиданного типа: белый список его не разберёт — фикстуру не пишем."""


def _узел(x, поля):
    if not isinstance(x, dict):
        raise НеМинимизируется(type(x).__name__)
    out = {}
    for k in поля:
        if k in x:
            if not isinstance(x[k], (bool, int, float, str, type(None))):
                raise НеМинимизируется("%s: %s" % (k, type(x[k]).__name__))
            out[k] = x[k]
    return out


# Только булевы отметки: свободный текст (НедостОпис) может нести что угодно,
# вплоть до ФИО, а вердикт парсера от него не зависит.
ОТМЕТКИ = ("Недост", "ДисквЛицо")
# Дата отметки — только у юрадреса: это сведения о компании. У физлица должность и
# сроки дисквалификации в фикстуру не идут — по ним человек находится в реестре
# дисквалифицированных и без ФИО; вердикт парсера от них не зависит.
ОТМЕТКИ_АДРЕСА = ОТМЕТКИ + ("НедостДатаЗаписи",)


def _лицо(r):
    return _узел(r, ОТМЕТКИ)


def _список(x, как):
    if not isinstance(x, list):
        raise НеМинимизируется(type(x).__name__)
    return [как(r) for r in x]


def минимизировать(провайдер, j):
    """Фикстура по белому списку: только то, что читают вердикты парсера движка и
    сверка контракта. Адреса, даты записей о людях, должности, доли, ОГРН не
    попадают: по связке «компания + дата назначения + доля» человек находится в
    ЕГРЮЛ и без ФИО. Остаётся число узлов (руководителей, учредителей) — оно
    публично в ЕГРЮЛ. Вердикты парсера (состояние, недостоверность,
    дисквалификация) по минимизированному ответу те же, что по полному;
    неожиданный тип узла — НеМинимизируется (фикстура не пишется)."""
    if not isinstance(j, dict):
        raise НеМинимизируется(type(j).__name__)
    if провайдер == "dadata":
        def запись(x):
            if not isinstance(x, dict):
                return None
            d = x.get("data")
            if not isinstance(d, dict):
                return {"data": None}  # парсер такую пропускает, длина списка сохранена
            out = _узел(d, ("inn", "branch_type", "invalid"))
            if isinstance(d.get("management"), dict):
                out["management"] = _узел(d["management"], ("disqualified",))
            return {"data": out}
        return {"suggestions": _список(j.get("suggestions"), запись)}
    out = {"meta": None if j.get("meta") is None
           else _узел(j.get("meta"), ("status", "message"))}
    if "data" not in j:
        return out
    d = j["data"]
    if not isinstance(d, dict):
        raise НеМинимизируется("data: %s" % type(d).__name__)
    м = _узел(d, ("ИНН", "ДисквЛица"))
    if d and not м:
        # непустой data без ИНН: {} парсер прочёл бы как «организация не найдена»
        raise НеМинимизируется("data без ИНН")
    if "ЮрАдрес" in d:
        м["ЮрАдрес"] = _узел(d["ЮрАдрес"], ОТМЕТКИ_АДРЕСА)
    if "Руковод" in d:
        м["Руковод"] = _список(d["Руковод"], _лицо)
    if "УпрОрг" in d:
        у = d["УпрОрг"]
        if у is None:
            м["УпрОрг"] = None
        else:
            м["УпрОрг"] = _узел(у, ОТМЕТКИ)
            if у and not м["УпрОрг"]:
                # непустой узел без отметки: пустой {} парсер пропустил бы, и
                # неизвестность стала бы «нет»
                м["УпрОрг"]["Недост"] = у.get("Недост")
    if "Учред" in d:
        у = d["Учред"]
        if not isinstance(у, dict):
            raise НеМинимизируется("Учред: %s" % type(у).__name__)
        м["Учред"] = {г: _список(з, _лицо) for г, з in у.items()}
    out["data"] = м
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
    """Какие узлы с отметками пришли: сверка с контрактом парсера движка."""
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
        "meta": {k: v for k, v in j["meta"].items() if k == "status"}
        if isinstance(j.get("meta"), dict) else type(j.get("meta")).__name__,
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
    """Строка отчёта по одному ИНН; ключ вычищен отовсюду (message сервиса может
    его повторить)."""
    return _без_ключа(_проверить(провайдер, ключ, opener, inn), ключ)


def _без_ключа(x, ключ):
    """Копия без ключа — и в сырой форме, и в JSON-экранированной (кавычка или
    обратный слэш в ключе в дампе выглядят иначе, и простая замена промахнулась бы)."""
    if not ключ:
        return x
    текст = json.dumps(x, ensure_ascii=False)
    for форма in (json.dumps(ключ, ensure_ascii=False)[1:-1], ключ):
        текст = текст.replace(форма, КЛЮЧ_УБРАН)
    return json.loads(текст)


def _причина(v):
    """Причина парсера без текста сервиса: «сеть: checko — <message>» несёт
    произвольный текст (ИНН, ФИО) — в отчёт идёт только наш префикс."""
    if not isinstance(v, str):
        return v
    if v.startswith("сеть: checko — "):
        return "сеть: checko — (сообщение сервиса не сохраняется)"
    return v


def _проверить(провайдер, ключ, opener, inn):
    try:
        status, j = _сырой(провайдер, ключ, opener, inn)
    except Exception as e:  # любой сбой сети — строка отчёта
        return {"инн": inn, "ошибка": str(e)[:300]}  # ключ вычистит проверить()
    стр = {"инн": inn, "http": status}
    if j is None:
        стр["ошибка"] = "не-JSON"
        return стр
    # до разбора: парсер обрезает message сервиса, и обрезок ключа потом не
    # нашёлся бы полной заменой
    j = _без_ключа(j, ключ)
    try:
        стр["контракт"] = (контракт_checko(j) if провайдер == "checko"
                           else контракт_dadata(j, inn))
        данные, av = (fc.parse_checko if провайдер == "checko" else fc.parse_dadata)(j, inn)
    except Exception as e:  # битый ответ — строка отчёта, прогон идёт дальше
        # только тип: текст исключения может нести куски ответа
        стр["ошибка"] = "разбор: %s" % type(e).__name__
        стр["фикстура"] = "не записана — ответ не разобран"
        return стр
    данные = данные if isinstance(данные, dict) else {}  # отказ парсера — данных нет
    стр["движок"] = {"состояние": av.get("состояние"), "причина": _причина(av.get("причина")),
                     **{k: данные.get(k) for k in ("недостоверность_сведений",
                                                   "дисквалификация_руководителя")}}
    try:
        маск = минимизировать(провайдер, замаскировать(j, ключ))
    except НеМинимизируется as e:
        стр["фикстура"] = "не записана — неожиданная схема: %s" % e
        return стр
    except Exception as e:  # сбой маскирования — фикстуру не пишем, прогон идёт
        стр["фикстура"] = "не записана — сбой маскирования: %s" % type(e).__name__
        return стр
    стр["утечки"] = утечки(маск, ключ, inn)
    if стр["утечки"]:
        стр["фикстура"] = "не записана — утечки после маскирования"
    else:
        ФИКСТУРЫ.mkdir(parents=True, exist_ok=True)
        путь = ФИКСТУРЫ / ("%s_%s.json" % (провайдер, inn))
        путь.write_text(json.dumps(маск, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        стр["фикстура"] = str(путь.relative_to(ROOT))
    return стр


def инн_выборки():
    return [c["инн"] for c in json.loads(
        (HERE / "cohort.json").read_text(encoding="utf-8"))["контрагенты"]]


def годный_инн(inn):
    """Только юрлицо: 10 цифр с верной контрольной суммой (ИП — см. докстринг модуля)."""
    return len(inn) == 10 and fc.validate_inn(inn) is not None


def main(argv):
    инн = argv[1:] or инн_выборки()
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
