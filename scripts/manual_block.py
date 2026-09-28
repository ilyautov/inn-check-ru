#!/usr/bin/env python3
"""
manual_block.py — ручной блок браузерной проверки (ФССП, суды): собрать по
контракту, проверить тем же кодом, что и резолвер, и вклеить в fetch-JSON.

    python3 scripts/manual_block.py --блок суды --инн 7707083893 \
        --url https://kad.arbitr.ru/ --дата 2026-09-28 --итог проверено \
        --поле ответчик_крупные=false --поле ответчик_дел=3 \
        --параметры "ИНН 7707083893, ответчик, 3 года" > суды.json
    python3 scripts/manual_block.py --блок фссп --инн 7707083893 \
        --url https://fssp.gov.ru/ --дата 2026-09-28 \
        --итог "не проверено" --причина "капча не пройдена" > фссп.json
    python3 scripts/manual_block.py --блок залоги --инн 1650032058 \
        --url https://www.reestr-zalogov.ru/search --дата 2026-09-28 \
        --итог проверено --текст страница.txt > залоги.json
    python3 scripts/manual_block.py --вклеить fetch.json суды.json фссп.json -o fetch.ручные.json

Контракт — references/brauzer.md, «Контракт ручного блока». Поля сигналов
(`производств`, `крупные`, `ответчик_крупные`) при итоге «проверено» обязательны:
без них резолвер блок не засчитает. Итог «не проверено» записывается, а не
отбрасывается: он перекрывает прежнее «проверено» (капча, вход, неполный
просмотр). Вклейка пишет НОВЫЙ файл: fetch.json пакета доказательств не
трогается — его хеш в манифесте.

ФИО, телефоны и адреса физлиц в свободный текст (`найдено`, `параметры`) не
пишите: код их не распознаёт, а блок попадает в досье и снимки.

`--текст` — скопированный текст страницы результатов (пока только залоги,
pledge_text.py): поля считает код, частичный текст (страниц больше одной) —
итог «не проверено».
"""

import datetime
import json
import math
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import pledge_text
import profiles
import sources

# Типы полей формы по источнику. Обязательные — те, что читают сигналы
# (profiles._поля_сигналов); eval сверяет, что их список здесь полон.
ПОЛЯ = {
    "фссп": {"производств": int, "крупные": bool, "сумма": float},
    "суды": {"ответчик_крупные": bool, "ответчик_дел": int, "ответчик_сумма": float},
    "залоги": {"уведомлений": int},
}
# Блоки, которые умеют разбирать вставленный текст страницы: поля считает код.
РАЗБОР_ТЕКСТА = {"залоги": pledge_text.разобрать}
МАКС_ТЕКСТ = 500
ИТОГИ = ("проверено", "не проверено")


def браузерные():
    return [k for k, v in sources.SOURCES.items() if v.get("требует") == "браузер"]


def _текст(v, что):
    """Строка без управляющих символов, не длиннее МАКС_ТЕКСТ; None — пусто."""
    if v is None:
        return None
    s = re.sub(r"[\x00-\x1f\x7f]", " ", str(v)).strip()
    if len(s) > МАКС_ТЕКСТ:
        raise ValueError("%s длиннее %d символов" % (что, МАКС_ТЕКСТ))
    return s or None


def _значение(тип, сырое, поле):
    s = str(сырое).strip().lower()
    if тип is bool:
        if s in ("true", "да", "1"):
            return True
        if s in ("false", "нет", "0"):
            return False
        raise ValueError("%s: ждали да/нет, получили %r" % (поле, сырое))
    try:
        v = тип(s.replace(" ", "").replace(",", "."))
    except ValueError:
        raise ValueError("%s: ждали число, получили %r" % (поле, сырое))
    if not math.isfinite(v) or v < 0:
        raise ValueError("%s: число должно быть конечным и ≥ 0" % поле)
    return v


def собрать(блок, инн, url, дата, итог, поля=None, найдено=None, параметры=None,
            причина=None, сегодня=None, текст=None):
    """-> блок по контракту. ValueError — вход не годится (что именно — в тексте).
    текст — вставленный текст страницы: поля блока из него считает код."""
    if блок not in ПОЛЯ or блок not in браузерные():
        raise ValueError("блок %r: клиппер знает только %s" % (блок, ", ".join(sorted(ПОЛЯ))))
    инн = str(инн or "").strip()
    if profiles._первый_инн(инн) != инн:
        raise ValueError("ИНН %r: нужно 10/12 цифр с верной контрольной суммой" % инн)
    url = str(url or "").strip()
    if not re.match(r"^https?://[^\s/]+", url) or len(url) > 2000:
        raise ValueError("url: нужна ссылка http/https на страницу, где смотрели")
    сегодня = сегодня or datetime.datetime.now(datetime.timezone.utc).astimezone().date()
    try:
        д = datetime.date.fromisoformat(str(дата or ""))
    except ValueError:
        raise ValueError("дата: ГГГГ-ММ-ДД, день, когда смотрели")
    if д > сегодня + datetime.timedelta(days=1):
        raise ValueError("дата в будущем: %s" % д)
    if итог not in ИТОГИ:
        raise ValueError("итог: %s" % " или ".join(ИТОГИ))
    out = {"инн": инн, "ввод": "браузер", "дата_проверки": д.isoformat(), "url": url,
           "проверил": "пользователь"}
    п = _текст(параметры, "параметры")
    упомянуты = profiles._инн_из(п or "")
    if упомянуты and инн not in упомянуты:
        # поле инн блока — объявленный объект, и резолвер параметрам не поверит:
        # противоречие отклоняется здесь, до записи (ревью Codex, P1)
        raise ValueError("в параметрах ИНН %s, а блок про %s" % (", ".join(упомянуты), инн))
    out["параметры_поиска"] = п or "ИНН %s" % инн
    н = _текст(найдено, "найдено")
    if н:
        out["найдено"] = н
    типы = ПОЛЯ[блок]
    if текст is not None:
        if блок not in РАЗБОР_ТЕКСТА:
            raise ValueError("текст страницы разбирается только у блоков: %s"
                             % ", ".join(РАЗБОР_ТЕКСТА))
        if поля:
            raise ValueError("с текстом страницы поля не вводятся: их считает код")
        if итог == "проверено":
            разбор = РАЗБОР_ТЕКСТА[блок](текст, инн)
            поля = {"уведомлений": разбор["уведомлений"]}
            for k in ("уведомления", "залогодержатели", "последнее", "примечание"):
                out[k] = разбор[k]
            if not н:
                out["найдено"] = "уведомлений %d, последнее %s (разобрано кодом из текста "\
                    "страницы)" % (разбор["уведомлений"], разбор["последнее"])
            if not разбор["полно"]:
                итог, причина = "не проверено", разбор["примечание"]
    for имя, сырое in (поля or {}).items():
        if имя not in типы:
            raise ValueError("поле %r у блока %s не предусмотрено: %s"
                             % (имя, блок, ", ".join(типы)))
        out[имя] = _значение(типы[имя], сырое, имя)
    if итог == "не проверено":
        пр = _текст(причина, "причина")
        if not пр:
            raise ValueError("итог «не проверено»: укажите причину (капча, вход, "
                             "поиск не завершён…)")
        out["статус"] = "не проверено: " + пр
        return out
    нет = [f for f in profiles._поля_сигналов(блок) if f not in out]
    if нет:
        raise ValueError("итог «проверено» без полей сигналов: %s" % ", ".join(нет))
    out["статус"] = "проверено"
    return out


def вклеить(fetch, блоки):
    """fetch-JSON + {id: блок} -> (новый fetch, {id: (состояние, причина)}).
    Блок другого ИНН или не браузерного источника — ValueError: вклейка не
    должна тихо подменить скриптовые данные или чужую компанию."""
    if not isinstance(fetch, dict) or not fetch.get("инн"):
        raise ValueError("fetch-JSON без поля инн")
    новый = json.loads(json.dumps(fetch, ensure_ascii=False))
    доступность = новый.get("_доступность")
    if not isinstance(доступность, dict):
        доступность = новый["_доступность"] = {}
    итоги = {}
    for id_, блок in блоки.items():
        if id_ not in браузерные():
            raise ValueError("блок %r не браузерный: вклеиваются только %s"
                             % (id_, ", ".join(браузерные())))
        if not isinstance(блок, dict) or str(блок.get("инн")) != str(fetch["инн"]):
            raise ValueError("блок %s: ИНН %r, а fetch — %s"
                             % (id_, (блок or {}).get("инн"), fetch["инн"]))
        новый[id_] = блок
        # Прежняя доступность (скрипт браузерный источник не собирает) не должна
        # решать за ручной блок: старое «пусто» перекрыло бы найденные
        # производства (ревью Codex, P1). Состояние решает контракт блока.
        доступность[id_] = {"состояние": "не проверено",
                            "причина": "не покрыто: браузерный источник — ручной блок"}
        сост, причина, _ = profiles._состояние_блока(новый, id_)
        доступность[id_] = {"состояние": сост, "причина": причина, "ввод": "браузер",
                            "дата": блок.get("дата_проверки"), "url": блок.get("url")}
        итоги[id_] = (сост, причина)
    # итог сбора пересчитывается по новым состояниям: старое «проверка не
    # состоялась» живьём давало null, а ретро — 🟡 (ревью Codex, P2)
    import fetch_counterparty
    # build_summary ждёт словари §1.1, а у ручного fetch бывает и строка
    # («проверено»): состояние нормализуется тем же кодом, что у резолвера
    новый["_итог_проверки"] = fetch_counterparty.build_summary({
        k: v if isinstance(v, dict) else {"состояние": profiles._норм_состояние(v)}
        for k, v in доступность.items()})
    return новый, итоги


def _в_пакете(путь):
    """fetch.json пакета доказательств (рядом manifest.json) менять нельзя."""
    p = Path(путь).resolve()
    return p.name == "fetch.json" and (p.parent / "manifest.json").exists()


def _записать(путь, данные):
    p = Path(путь)
    tmp = p.with_name(".%s.%d.tmp" % (p.name, os.getpid()))
    tmp.write_text(json.dumps(данные, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)


USAGE = __doc__.split("\n\n")[1] + "\n"


def main(argv):
    args = argv[1:]
    if not args or args[0] in ("-h", "--help"):
        sys.stdout.write(__doc__)
        return 0
    try:
        if args[0] == "--вклеить":
            if "-o" not in args or args.index("-o") != len(args) - 2 or len(args) < 5:
                sys.stderr.write(USAGE)
                return 2
            вход, файлы, выход = args[1], args[2:-2], args[-1]
            if Path(выход).resolve() == Path(вход).resolve() or _в_пакете(выход):
                raise ValueError("пишу только в новый файл: вход и fetch.json пакета не меняю")
            fetch = json.loads(Path(вход).read_text(encoding="utf-8"))
            блоки = {}
            for ф in файлы:
                блок = json.loads(Path(ф).read_text(encoding="utf-8"))
                id_ = Path(ф).stem
                блоки[id_] = блок
            новый, итоги = вклеить(fetch, блоки)
            _записать(выход, новый)
            sys.stdout.write(json.dumps({"записано": выход, "блоки": {
                k: {"состояние": v[0], "причина": v[1]} for k, v in итоги.items()}},
                ensure_ascii=False, indent=2) + "\n")
            return 0
        опции = {"--блок": None, "--инн": None, "--url": None, "--дата": None,
                 "--итог": None, "--найдено": None, "--параметры": None, "--причина": None,
                 "--текст": None}
        поля = {}
        i = 0
        while i < len(args):
            a = args[i]
            if a == "--поле" and i + 1 < len(args) and "=" in args[i + 1]:
                k, _, v = args[i + 1].partition("=")
                поля[k.strip()] = v
            elif a in опции and i + 1 < len(args):
                опции[a] = args[i + 1]
            else:
                sys.stderr.write(USAGE)
                return 2
            i += 2
        блок = собрать(опции["--блок"], опции["--инн"], опции["--url"], опции["--дата"],
                       опции["--итог"], поля, опции["--найдено"], опции["--параметры"],
                       опции["--причина"],
                       текст=None if опции["--текст"] is None else
                       Path(опции["--текст"]).read_text(encoding="utf-8"))
        sys.stdout.write(json.dumps(блок, ensure_ascii=False, indent=2) + "\n")
        return 0
    except (ValueError, OSError) as e:
        sys.stderr.write("не записано: %s\n" % e)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
