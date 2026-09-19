#!/usr/bin/env python3
"""
snapshot.py — нормализованный отпечаток проверки вместо полного слепка
(спек волны 2, §3.1). Офлайн, только стандартная библиотека.

Противоречие: снимок нужен целиком для диффа и ретроспективы — и не нужен
целиком, потому что это чужие персданные, вес и время. Разрешение: хранить
отпечаток — ровно те поля, по которым идёт дифф, плюс состояния источников,
плюс SHA-256 всего остального.

Формат (версия_формата 1):
    {
      "версия_формата": 1, "инн": "…", "дата": "2026-09-19T20:00:00",
      "поля": {"статус": …, "руководитель": …, "адрес": …, "ликвидация": false,
               "недостоверность_сведений": …, "налоговая_задолженность": …,
               "дата_прекращения": …, "год_отчётности": …, "чистые_активы": …,
               "выручка": …, "мсп_категория": …, "мсп_статус": …,
               "финансы_годы": [...], "спецреестры": {...}},
      "состояния": {"егрюл": "ok", "риски": "ok", …},
      "сбор": {"режим": "всё", "профиль": "нейтрально"},
      "хеш_остального": "sha256:…",
      "_итог": {"проверка_состоялась": false, "не_проверено": 8}
    }

Блок «сбор» — вне хеша и вне «полей»: режим и профиль меняются от запуска к
запуску и сами по себе не изменение у контрагента. Но хранить их обязательно:
снимок, собранный `--профиль отсрочка` или с ранним выходом, содержит меньше
источников, и дифф против полного покажет артефакт сбора как изменение. Дифф
читает этот блок и предупреждает о разнородных снимках.

Перечень «полей» не дублируется: он импортируется из diff_counterparty.py
(ОТСЛЕЖИВАЕМЫЕ_ПОЛЯ), там же живёт разбор полного слепка — поэтому дифф по
отпечаткам даёт ровно те же изменения, что дифф по полным снимкам.

Что НЕ кладётся в отпечаток: наименования, ОГРН/КПП, ОКВЭД, даты регистрации,
численность, детальные строки баланса, тексты причин — всё это сворачивается в
`хеш_остального` (ловит «изменилось что-то вне отслеживаемого», но само по себе
персданных не хранит). ФИО руководителя и адрес остаются: по ним идёт дифф и
санкционная сверка. Персданные сверх перечисленного в отпечаток не кладём.

Использование:
    python3 snapshot.py --из полный_снимок.json            # печатает отпечаток
    python3 snapshot.py --из полный_снимок.json --сохранить # пишет в кэш снимков
    python3 snapshot.py --список <ИНН>                     # снимки ИНН в кэше
"""

import copy
import glob
import hashlib
import importlib.util
import json
import os
import sys
import time

ВЕРСИЯ_ФОРМАТА = 1
CACHE_DIR = os.path.expanduser(os.path.join("~", ".cache", "inn-check-ru"))

# Перечень отслеживаемых полей и разбор полного слепка — из diff_counterparty.py.
_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "diff_counterparty", os.path.join(_HERE, "diff_counterparty.py"))
_diff = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_diff)

ОТСЛЕЖИВАЕМЫЕ_ПОЛЯ = _diff.ОТСЛЕЖИВАЕМЫЕ_ПОЛЯ

# Служебные ключи _доступность, которые в хеш не идут: «состояние» уже лежит
# в блоке «состояния», а «дата» меняется каждый прогон и означала бы «что-то
# изменилось» каждый день.
_СЛУЖЕБНЫЕ_В_ДОСТУПНОСТИ = ("состояние", "дата")

# Телеметрия прогона, а не факты о контрагенте: секунды сбора и режим меняются
# от запуска к запуску, в хеше «остального» дали бы ложное «что-то изменилось».
# Из хеша они уходят — но не в никуда: режим и профиль кладутся в блок «сбор»
# отпечатка, иначе снимок урезанного профиля диффился бы с полным как равный.
_ТЕЛЕМЕТРИЯ = ("_итог_проверки", "_сбор", "профиль")

# Сессионные токены источников (pb.nalog.ru отдаёт новый `_token_pb` на каждый
# запрос). В хеше «остального» они давали бы ℹ️ «изменилось что-то вне
# отслеживаемого» при КАЖДОМ прогоне — сигнал, который через неделю перестают
# читать. Фактов о контрагенте не несут, поэтому вычищаются на любой глубине.
_ПРЕФИКС_ТОКЕНА = "_token"


# Разбор состояний живёт в diff_counterparty.py: по ним же дифф решает, какие
# поля вообще сравнивать, и раздваивать это правило нельзя.
_состояние = _diff.состояние_блока
состояния = _diff.состояния_снимка


def итог(result):
    """{проверка_состоялась, не_проверено} — из _итог_проверки или по состояниям."""
    st = состояния(result)
    не_проверено = sum(1 for v in st.values() if v == "не проверено")
    блок = result.get("_итог_проверки") if isinstance(result, dict) else None
    if isinstance(блок, dict):
        return {
            "проверка_состоялась": bool(блок.get("проверка_состоялась")),
            "не_проверено": блок.get("не_проверено", не_проверено),
        }
    return {"проверка_состоялась": False, "не_проверено": не_проверено}


def сбор(result):
    """Чем собран снимок: {"режим": …, "профиль": …} (None — неизвестно).

    Хранится в отпечатке вне хеша: снимок, собранный урезанным профилем или с
    ранним выходом, несравним с полным как равный.
    """
    if not isinstance(result, dict):
        return {"режим": None, "профиль": None}
    return _diff.условия_сбора(result)


def _удалить(узел, путь):
    """Удаляет лист по пути (молча, если пути нет)."""
    for ключ in путь[:-1]:
        if not isinstance(узел, dict):
            return
        узел = узел.get(ключ)
    if isinstance(узел, dict):
        узел.pop(путь[-1], None)


def _вычистить_токены(узел):
    """Убирает сессионные токены (_token…) на любой глубине дерева."""
    if isinstance(узел, dict):
        for ключ in [k for k in узел if str(k).startswith(_ПРЕФИКС_ТОКЕНА)]:
            узел.pop(ключ, None)
        for значение in узел.values():
            _вычистить_токены(значение)
    elif isinstance(узел, list):
        for значение in узел:
            _вычистить_токены(значение)
    return узел


def остальное(result):
    """Всё, что не попало в «поля»: копия результата без отслеживаемых листьев,
    без телеметрии прогона, без сессионных токенов и без посуточно меняющихся
    служебных отметок."""
    rest = copy.deepcopy(result) if isinstance(result, dict) else {}
    for ключ in _ТЕЛЕМЕТРИЯ:
        rest.pop(ключ, None)
    _вычистить_токены(rest)
    av_map = rest.get("_доступность")
    if isinstance(av_map, dict):
        for av in av_map.values():
            if isinstance(av, dict):
                for ключ in _СЛУЖЕБНЫЕ_В_ДОСТУПНОСТИ:
                    av.pop(ключ, None)
    for d in ОТСЛЕЖИВАЕМЫЕ_ПОЛЯ:
        вид, путь = d["вид"], d.get("путь")
        if not путь:
            continue
        if вид == "значение":
            _удалить(rest, путь)
        elif вид == "годы":
            блок = rest
            for ключ in путь:
                блок = блок.get(ключ) if isinstance(блок, dict) else None
            for rec in (блок or []):
                if not isinstance(rec, dict):
                    continue
                rec.pop("выручка", None)
                rec.pop("чистые_активы", None)
                строки = rec.get("строки")
                if isinstance(строки, dict):
                    строки.pop("1300", None)
                    строки.pop("2110", None)
        elif вид == "статусы":
            блок = rest
            for ключ in путь:
                блок = блок.get(ключ) if isinstance(блок, dict) else None
            for запись in (блок or {}).values() if isinstance(блок, dict) else []:
                if isinstance(запись, dict):
                    запись.pop("статус", None)
    return rest


def хеш_остального(result):
    """SHA-256 канонического JSON «остального» (sort_keys, ensure_ascii=False)."""
    канон = json.dumps(остальное(result), sort_keys=True, ensure_ascii=False,
                       separators=(",", ":"))
    return "sha256:" + hashlib.sha256(канон.encode("utf-8")).hexdigest()


def fingerprint(result, дата=None):
    """Нормализованный отпечаток полного результата fetch_counterparty.collect()."""
    if _diff.это_отпечаток(result):
        return copy.deepcopy(result)
    result = result if isinstance(result, dict) else {}
    return {
        "версия_формата": ВЕРСИЯ_ФОРМАТА,
        "инн": result.get("инн"),
        "дата": дата or time.strftime("%Y-%m-%dT%H:%M:%S"),
        "поля": _diff.поля_снимка(result),
        "состояния": состояния(result),
        "сбор": сбор(result),
        "хеш_остального": хеш_остального(result),
        "_итог": итог(result),
    }


def snapshots_dir(inn, dir=None):
    """Каталог снимков ИНН: <dir или ~/.cache/inn-check-ru>/snapshots/<ИНН>."""
    return os.path.join(dir or CACHE_DIR, "snapshots", str(inn))


def save(inn, result, dir=None):
    """Сохраняет отпечаток результата, возвращает путь (или None при сбое записи).

    Имя файла — <дата>_<время>.json, как у полных снимков: diff_counterparty.py
    берёт два последних файла каталога независимо от формата.
    """
    отпечаток = fingerprint(result)
    if not отпечаток.get("инн"):
        отпечаток["инн"] = str(inn)
    try:
        каталог = snapshots_dir(inn, dir)
        os.makedirs(каталог, exist_ok=True)
        имя = time.strftime("%Y-%m-%d_%H-%M-%S") + ".json"
        путь = os.path.join(каталог, имя)
        if os.path.exists(путь):  # два прогона в одну секунду не затирают друг друга
            путь = os.path.join(каталог, имя[:-5] + "_%d.json" % os.getpid())
        with open(путь, "w", encoding="utf-8") as fh:
            json.dump(отпечаток, fh, ensure_ascii=False, indent=2)
        return путь
    except OSError as e:
        sys.stderr.write("ВНИМАНИЕ: отпечаток не сохранён (%s)\n" % e)
        return None


def load(path):
    """Читает снимок с диска: отпечаток или полный слепок. None — не читается."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def list_snapshots(inn, dir=None):
    """Пути снимков ИНН по возрастанию имени (= по времени), оба формата."""
    return sorted(glob.glob(os.path.join(snapshots_dir(inn, dir), "*.json")))


USAGE = ("Использование: python3 snapshot.py --из <полный_снимок.json> "
         "[--сохранить] [--каталог <dir>]\n"
         "               python3 snapshot.py --список <ИНН> [--каталог <dir>]\n")


def main(argv):
    args = argv[1:]

    def опция(имя):
        if имя in args:
            i = args.index(имя)
            return args[i + 1] if i + 1 < len(args) else None
        return None

    каталог = опция("--каталог")
    из = опция("--из")
    список = опция("--список")
    if список:
        пути = list_snapshots(список, каталог)
        sys.stdout.write(json.dumps(
            {"инн": список, "снимков": len(пути), "снимки": пути},
            ensure_ascii=False, indent=2) + "\n")
        return 0
    if not из:
        sys.stderr.write(USAGE)
        return 2
    полный = load(из)
    if полный is None:
        sys.stdout.write(json.dumps(
            {"статус": "не проверено", "причина": "файл не читается как JSON: %s" % из},
            ensure_ascii=False, indent=2) + "\n")
        return 1
    отпечаток = fingerprint(полный)
    if "--сохранить" in args:
        путь = save(отпечаток.get("инн") or "неизвестный", отпечаток, dir=каталог)
        sys.stderr.write("отпечаток сохранён: %s\n" % путь)
    sys.stdout.write(json.dumps(отпечаток, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
