#!/usr/bin/env python3
"""
bench.py — бенчмарк волны 5 (блок G): перечень дефектов, а не рейтинг.

    python3 benchmark/bench.py выборка   # benchmark/cohort.json (seed, из калибровочной когорты)
    uv run --no-project --with pypdf==6.19.0 python3 benchmark/bench.py эталон
                                         # benchmark/etalon/: сырые ответы + etalon.json
    python3 benchmark/bench.py прогон    # benchmark/runs/<дата>_наше.json — плечо inn-check-ru
    python3 benchmark/bench.py прогон checko|dadata   # плечо агрегатора (CHECKO_API_KEY/DADATA_API_KEY)
    python3 benchmark/bench.py отчёт <прогон.json>   # перечень дефектов (markdown)

Порядок и правила (спека волны 5, раздел G):
- рубрика (benchmark/rubric_v2.json; v1 — rubric.json) и эталон коммитятся ДО прогона плеч: эталон
  нельзя подогнать под результат;
- эталон собирается из первоисточников (ЕГРЮЛ, ЕФРСБ) отдельным минимальным
  кодом — парсеры движка здесь не импортируются, чтобы ошибка разбора движка не
  попала в эталон. Источник при этом тот же: пропуск на стороне самого
  первоисточника бенчмарк не поймает. КАД — только браузер: не собирается;
- в выводе нет долей и рейтинга: по каждому контрагенту и полю — «совпало»,
  «ошибка факта», «функция не поддерживается» или «источник недоступен»;
- плечо без ключа или доступа вычёркивается до прогона (rubric_v2.json, «плечи»).
"""

import csv
import datetime
import hashlib
import hmac
import json
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
КОГОРТА = ROOT / "calibration" / "cohort_2021.csv"
ВЫБОРКА = HERE / "cohort.json"
ЭТАЛОНЫ = HERE / "etalon"        # снимки эталона по датам: etalon/<ГГГГ-ММ-ДД>/
РУБРИКА = HERE / "rubric_v2.json"   # действующая; rubric.json (v1) — для прогонов 26–27.09
ПРОГОНЫ = HERE / "runs"
UA = "inn-check-ru benchmark (+https://github.com/ilyautov/inn-check-ru)"
ЕГРЮЛ = "https://egrul.nalog.ru/"
ЕФРСБ = "https://bankrot.fedresurs.ru"
SEED = 2026
МАСКА = "ФИО УБРАНО ИЗ ФИКСТУРЫ"


КЛЮЧ_HMAC = Path(os.path.expanduser("~/.cache/inn-check-ru/benchmark_hmac.key"))


def _ключ():
    """Секрет HMAC вне репозитория: INN_CHECK_BENCH_KEY или локальный файл (создаётся).
    Голый sha256 не годится: строку руководителя из публичного ЕГРЮЛ можно
    захешировать и сверить с опубликованной."""
    env = os.environ.get("INN_CHECK_BENCH_KEY")
    if env:
        return env.encode()
    if not КЛЮЧ_HMAC.exists():
        КЛЮЧ_HMAC.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(КЛЮЧ_HMAC), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(os.urandom(32).hex())
    return КЛЮЧ_HMAC.read_text().strip().encode()


def ключ_id():
    """Отпечаток HMAC-ключа (не сам ключ): эталон и прогон под разными ключами
    несравнимы — руководитель разошёлся бы у всех, и это были бы ложные дефекты."""
    return hmac.new(_ключ(), b"inn-check-ru benchmark key id", hashlib.sha256).hexdigest()[:16]


ХЕШ_РУКОВОДИТЕЛЯ = "v2"   # v1 (прогоны 26–27.09): строка как есть


def норм_руководителя(v):
    """Рубрика v2: в выписке ФИО прописными, в поиске и у агрегаторов — с заглавной.
    Верхний регистр, Ё -> Е, пробелы схлопнуты; несколько лиц — по алфавиту."""
    лица = (" ".join(str(x).upper().replace("Ё", "Е").split()) for x in str(v).split(";"))
    return "; ".join(sorted(filter(None, лица)))


def хеш_фио(v):
    """ФИО физлиц в репозиторий не кладём: руководитель сравнивается по HMAC
    нормализованной строки (ХЕШ_РУКОВОДИТЕЛЯ)."""
    if not v or not норм_руководителя(v):
        return None
    return "hmac:" + hmac.new(_ключ(), норм_руководителя(v).encode(), hashlib.sha256).hexdigest()


def замаскировать(имя, данные):
    """Сырой ответ -> копия без ФИО (руководитель ЕГРЮЛ, арбитражный управляющий)."""
    if имя == "егрюл":
        for r in данные.get("rows") or []:
            if r.get("g"):
                r["g"] = МАСКА
    if имя == "ефрсб":
        for r in данные.get("pageData") or []:
            дело = r.get("lastLegalCase") or {}
            if дело.get("arbitrManagerFio"):
                дело["arbitrManagerFio"] = МАСКА
    return данные


# ---------------------------------------------------------------------------
# Выборка: середняки и свежие дела, не витринные компании
# ---------------------------------------------------------------------------

def выборка(n_середняков=5, n_свежих=5, seed=SEED):
    """Середняки — выжившие с выручкой 2021 между 40-м и 60-м перцентилем;
    свежие дела — дело о банкротстве 2024 года. Только коммерческие ОКОПФ-2012."""
    with open(КОГОРТА, encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f)
                if len(r["окопф"]) == 5 and r["окопф"][0] in "16"]
    живые = sorted((r for r in rows if r["класс"] == "выжил" and r["выручка"]),
                   key=lambda r: float(r["выручка"]))
    середина = живые[int(len(живые) * 0.4):int(len(живые) * 0.6)]
    свежие = [r for r in rows if r["класс"] == "банкрот" and r["год_дела"] == "2024"]
    rng = random.Random(seed)
    отбор = ([("середняк", r) for r in rng.sample(середина, n_середняков)]
             + [("свежее дело", r) for r in rng.sample(свежие, n_свежих)])
    return {"seed": seed, "источник": "calibration/cohort_2021.csv",
            "правило": "середняки: выжившие, выручка 2021 между 40 и 60 перцентилем; "
                       "свежие дела: дело о банкротстве 2024 года; только ОКОПФ на 1/6",
            "контрагенты": [{"инн": r["inn"], "группа": г} for г, r in отбор]}


# ---------------------------------------------------------------------------
# Эталон: минимальный независимый разбор первоисточников
# ---------------------------------------------------------------------------

def _запрос(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers=dict({"User-Agent": UA}, **(headers or {})))
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() or b""
    except (OSError, ValueError) as e:   # обрыв, таймаут: раздел эталона — «не собран»
        return 0, ("сеть: %s" % type(e).__name__).encode()


def _json(тело):
    try:
        j = json.loads(тело)
    except ValueError:
        return None
    return j if isinstance(j, dict) else None


def _егрюл(inn):
    body = urllib.parse.urlencode({"vyp3CaptchaToken": "", "page": "", "query": inn,
                                   "region": "", "PreventChromeAutocomplete": ""}).encode()
    код, тело = _запрос(ЕГРЮЛ, body, {"Content-Type": "application/x-www-form-urlencoded",
                                     "Referer": ЕГРЮЛ})
    t = (_json(тело) or {}).get("t") if код == 200 else None
    if not t:
        return код, тело, None
    for _ in range(10):
        time.sleep(1.5)
        код, тело = _запрос(ЕГРЮЛ + "search-result/" + t, headers={"Referer": ЕГРЮЛ})
        j = _json(тело) if код == 200 else None
        if j is not None and isinstance(j.get("rows"), list):
            return код, тело, j
    return код, тело, None


ПАПКА_ВЫПИСОК = Path(os.path.expanduser("~/.cache/inn-check-ru/vyp"))
ЛИЦО = re.compile(r"Сведения\s+о\s+лице,\s+имеющем\s+право\s+без\s+доверенности")
УК = "Сведения об управляющей организации"


def _выписка(inn, t):
    """Выписка ЕГРЮЛ (PDF) по токену строки поиска: vyp-request -> vyp-status ->
    vyp-download, как кнопка «Получить выписку» на сайте. Капчу не обходим: если
    сервис её попросил — выписки нет. PDF содержит ФИО и лежит только в кэше вне
    репозитория. -> (путь | None, причина)."""
    код, тело = _запрос(ЕГРЮЛ + "vyp-request/" + t, headers={"Referer": ЕГРЮЛ})
    j = _json(тело) if код == 200 else None
    if not j or j.get("captchaRequired") or not j.get("t"):
        return None, "выписка: запрос не принят (HTTP %s%s)" % (
            код, ", нужна капча" if j and j.get("captchaRequired") else "")
    for _ in range(20):
        time.sleep(1.5)
        код, тело = _запрос(ЕГРЮЛ + "vyp-status/" + j["t"], headers={"Referer": ЕГРЮЛ})
        if ((_json(тело) or {}).get("status") if код == 200 else None) == "ready":
            break
    else:
        return None, "выписка: не готова за 30 с"
    код, тело = _запрос(ЕГРЮЛ + "vyp-download/" + j["t"], headers={"Referer": ЕГРЮЛ})
    if код != 200 or not тело.startswith(b"%PDF"):
        return None, "выписка: скачивание не удалось (HTTP %s)" % код
    ПАПКА_ВЫПИСОК.mkdir(parents=True, exist_ok=True)
    путь = ПАПКА_ВЫПИСОК / ("%s.pdf" % inn)
    путь.write_bytes(тело)
    return путь, None


def _одной_строкой(v):
    return " ".join(v.split())


ФИО_ЧАСТЬ = re.compile(r"[А-ЯЁA-Z]+(?:-[А-ЯЁA-Z]+)*")
# должность бывает и строчными; слова колонтитула в ней — признак склейки страниц
ДОЛЖНОСТЬ = re.compile(r"(?!.*(?:Страница|Выписка|ЕГРЮЛ|ОГРН))[А-ЯЁа-яё][А-ЯЁа-яё ,.()«»\"-]*")


def _значение(v, образец, что):
    """Многострочное значение поля -> одна строка; перенос по дефису склеивается.
    Колонтитул страницы внутри значения (цифры, «Страница», «Выписка») не
    пролезает в HMAC: такое значение — ValueError, поле «не собрано»."""
    v = re.sub(r"-\s+", "-", " ".join(v.split()))
    части = v.split() if что == "ФИО" else [v]
    if not части or not all(образец.fullmatch(ч) for ч in части) \
            or (что == "ФИО" and not 1 <= len(части) <= 5):
        raise ValueError("%s не распознано (колонтитул или перенос страницы?)" % что)
    return v


def руководители_из_выписки(текст):
    """Текст выписки -> [(должность, ФИО)] из разделов «Сведения о лице, имеющем
    право без доверенности…». ValueError («эталон не собран», а не «пусто»):
    управляющая организация (юрлицо), раздел без ФИО или должности, значение с
    посторонним текстом и отсутствие разделов вовсе — у юрлица в ЕГРЮЛ раздел о
    лице есть, и его отсутствие скорее сбой извлечения, чем факт."""
    if УК in текст:
        raise ValueError("управляющая организация: руководитель — юрлицо")
    out = []
    for блок in ЛИЦО.split(текст)[1:]:
        блок = блок.split("\nСведения о", 1)[0]
        фио = re.search(r"\nОтчество\n(.+?)\n\d+ (?:ИНН|ГРН)", блок, re.S)
        должн = re.search(r"\n\d+ Должность (.+?)\n\d+ ", блок, re.S)
        if not фио or not должн:
            raise ValueError("раздел о лице без ФИО или должности")
        out.append((_значение(должн.group(1), ДОЛЖНОСТЬ, "должность"),
                    _значение(фио.group(1), ФИО_ЧАСТЬ, "ФИО")))
    if not out:
        raise ValueError("раздел о лице, действующем без доверенности, не найден")
    return out


def руководитель_из_pdf(путь):
    """PDF -> строка «ДОЛЖНОСТЬ: ФИО» (несколько — через «; »). pypdf нужен только
    здесь и только инструменту бенчмарка: uv run --with pypdf; движок — stdlib.
    Страница без извлечённого текста — ValueError: часть выписки не прочитана."""
    import pypdf
    страницы = [с.extract_text() for с in pypdf.PdfReader(str(путь)).pages]
    if not страницы or not all(т and т.strip() for т in страницы):
        raise ValueError("текст выписки извлечён не со всех страниц")
    return "; ".join("%s: %s" % пара for пара in руководители_из_выписки("\n".join(страницы)))


def _ефрсб(inn):
    url = "%s/backend/cmpbankrupts?searchString=%s&limit=15&offset=0" % (ЕФРСБ, inn)
    код, тело = _запрос(url, headers={"Referer": ЕФРСБ + "/"})
    return код, тело, _json(тело) if код == 200 else None


def эталон_записи(inn, егрюл, ефрсб):
    """Поля эталона из сырых ответов. None у раздела — первоисточник не ответил."""
    out = {"инн": inn}
    if егрюл is None:
        out["егрюл"] = None
    else:
        свои = [r for r in егрюл.get("rows") or [] if str(r.get("i")) == inn]
        r = свои[0] if len(свои) == 1 else None
        out["егрюл"] = None if r is None else {
            "огрн": r.get("o"), "наименование_полное": r.get("n"),
            "дата_регистрации": r.get("r"), "руководитель": r.get("g"),
            "дата_прекращения": r.get("e") or None}
    выдача = ефрсб.get("pageData") if isinstance(ефрсб, dict) else None
    всего = ефрсб.get("total") if isinstance(ефрсб, dict) else None
    # каждая строка — объект со строковым inn, иначе запись с нашим ИНН могла
    # не распознаться (как и в parse_bankrupt движка — это ошибка схемы)
    строки_целы = isinstance(выдача, list) and all(
        isinstance(r, dict) and isinstance(r.get("inn"), str) for r in выдача)
    свои = [r for r in выдача or [] if isinstance(r, dict) and str(r.get("inn")) == inn]
    if not строки_целы or not isinstance(всего, int) or isinstance(всего, bool) \
            or всего < len(выдача) or (not свои and всего > len(выдача)):
        # не список, нет total или выдача неполная — отсутствие записи не доказано
        out["банкротство"] = None
    else:
        if not свои:
            out["банкротство"] = {"есть_запись": False, "номер_дела": None, "стадия_код": None}
        else:
            дело = свои[0].get("lastLegalCase") or {}
            out["банкротство"] = {"есть_запись": True, "номер_дела": дело.get("number"),
                                  "стадия_код": (дело.get("status") or {}).get("code")}
    return out


def _руководитель_эталона(inn, данные):
    """Строка поиска -> выписка -> {"руководитель": HMAC | None, "должности"} или
    {"не_собрано": причина}. PDF и ФИО в репозиторий не попадают."""
    свои = [r for r in данные.get("rows") or [] if str(r.get("i")) == inn]
    if len(свои) != 1 or not свои[0].get("t"):
        return {"не_собрано": "выписка: в поиске нет одной строки с этим ИНН"}
    путь, причина = _выписка(inn, свои[0]["t"])
    if путь is None:
        return {"не_собрано": причина}
    try:
        строка = руководитель_из_pdf(путь)
    except ValueError as e:
        return {"не_собрано": "выписка: %s" % e}
    except Exception as e:  # битый PDF: ошибки pypdf не наследуют ValueError
        return {"не_собрано": "выписка: PDF не прочитан (%s)" % type(e).__name__}
    return {"руководитель": хеш_фио(строка),
            "должности": [x.split(":", 1)[0] for x in (строка or "").split("; ") if x]}


def собрать_эталон(дата=None):
    try:
        import pypdf  # noqa: F401 — проверка до сетевых запросов
    except ImportError:
        raise SystemExit("эталон v2 читает выписку ЕГРЮЛ (PDF): uv run --no-project "
                         "--with pypdf==6.19.0 python3 benchmark/bench.py эталон")
    дата = дата or datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    ЭТАЛОН = ЭТАЛОНЫ / дата
    выбор = json.loads(ВЫБОРКА.read_text(encoding="utf-8"))
    (ЭТАЛОН / "raw").mkdir(parents=True, exist_ok=True)
    записи, сырые = [], {}
    for к in выбор["контрагенты"]:
        inn = к["инн"]
        for имя, f in (("егрюл", _егрюл), ("ефрсб", _ефрсб)):
            код, тело, данные = f(inn)
            путь = ЭТАЛОН / "raw" / ("%s_%s.json" % (inn, имя))
            # хеш исходного тела не публикуется: по нему, как и по голому хешу
            # ФИО, можно было бы сверять кандидатов; хешируется сохранённый файл
            сырые[путь.name] = {"http": код}
            if данные is None:
                путь.write_bytes(тело)
            else:
                if имя == "егрюл":   # руководитель — из выписки, до маски строки поиска
                    сырые["%s_выписка" % inn] = _руководитель_эталона(inn, данные)
                путь.write_text(json.dumps(замаскировать(имя, данные), ensure_ascii=False,
                                           indent=1) + "\n", encoding="utf-8")
                сырые[путь.name]["замаскировано"] = "ФИО заменены на «%s»" % МАСКА
            сырые[путь.name]["sha256_файла"] = hashlib.sha256(путь.read_bytes()).hexdigest()
            time.sleep(1)
        записи.append(эталон_из_сырых(inn, сырые, ЭТАЛОН))
    мета = {"собрано_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "сеть": "не-РФ IP, без прокси", "ua": UA, "сырые": сырые,
            "руководитель": "из выписки ЕГРЮЛ (PDF); HMAC-SHA256 нормализованной строки "
                            "с локальным ключом вне репозитория",
            "хеш_руководителя": ХЕШ_РУКОВОДИТЕЛЯ, "рубрика": РУБРИКА.name,
            "рубрика_sha256": _sha(РУБРИКА),
            "ключ_id": ключ_id(),
            "не_собрано": {"суды": "КАД — только браузер (JS, капча): эталон не собирался"},
            "записи": записи}
    (ЭТАЛОН / "etalon.json").write_text(json.dumps(мета, ensure_ascii=False, indent=1) + "\n",
                                        encoding="utf-8")
    return мета


def эталон_из_сырых(inn, сырые, ЭТАЛОН):
    def прочесть(имя):
        путь = ЭТАЛОН / "raw" / ("%s_%s.json" % (inn, имя))
        try:
            return json.loads(путь.read_bytes())
        except (OSError, ValueError):
            return None
    егрюл = прочесть("егрюл")
    ефрсб = прочесть("ефрсб")
    запись = эталон_записи(inn, егрюл if isinstance(егрюл, dict) and "rows" in егрюл else None,
                           ефрсб if isinstance(ефрсб, dict) else None)
    выписка = сырые.get("%s_выписка" % inn)
    if запись["егрюл"] is not None and выписка is not None:   # эталон v2
        запись["егрюл"]["руководитель"] = выписка.get("руководитель")
        if "не_собрано" in выписка:
            запись["егрюл"]["_не_собрано"] = {"руководитель": выписка["не_собрано"]}
    elif запись["егрюл"] is not None:                          # эталон v1: поле g поиска
        запись["егрюл"]["руководитель"] = (
            сырые.get("%s_егрюл.json" % inn, {}).get("руководитель") or {}).get(inn)
    return запись


# ---------------------------------------------------------------------------
# Плечо inn-check-ru и сравнение
# ---------------------------------------------------------------------------

def последний_эталон():
    снимки = sorted(p for p in ЭТАЛОНЫ.glob("*/etalon.json"))
    if not снимки:
        raise SystemExit("эталона нет: сначала bench.py эталон (и коммит до прогона)")
    return снимки[-1]


def _sha(путь):
    return hashlib.sha256(Path(путь).read_bytes()).hexdigest()


def прогон_наше():
    эталон = последний_эталон()
    sys.path.insert(0, str(ROOT / "scripts"))
    import fetch_counterparty as fc
    выбор = json.loads(ВЫБОРКА.read_text(encoding="utf-8"))
    ответы = {}
    начало = time.monotonic()
    for к in выбор["контрагенты"]:
        т = time.monotonic()
        рез = fc.collect(к["инн"], режим="всё")
        егрюл = рез.get("егрюл")
        if isinstance(егрюл, dict):
            егрюл = dict(егрюл, руководитель=хеш_фио(егрюл.get("руководитель")))
        ответы[к["инн"]] = {"егрюл": егрюл, "банкротство": рез.get("банкротство"),
                            "_доступность": {s: рез["_доступность"].get(s)
                                             for s in ("егрюл", "банкротство")},
                            "секунд": round(time.monotonic() - т, 1)}
    версия = (ROOT / "pyproject.toml").read_text(encoding="utf-8").split('version = "', 1)[1]
    return {"плечо": "inn-check-ru CLI (fetch_counterparty.collect, режим всё)",
            "версия": версия.split('"', 1)[0],
            "дата_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "сеть": "не-РФ IP, без прокси; своей ноды нет — плечо «через свою ноду» не прогонялось",
            "тариф": "без ключей", "ручные_шаги": "нет", "секунд_всего": round(time.monotonic() - начало, 1),
            # прогон привязан к снимку эталона и рубрики: новый эталон через неделю
            # не переоценит старый прогон
            "эталон": str(эталон.relative_to(HERE)), "эталон_sha256": _sha(эталон),
            "рубрика": РУБРИКА.name, "рубрика_sha256": _sha(РУБРИКА), "ключ_id": ключ_id(),
            "хеш_руководителя": ХЕШ_РУКОВОДИТЕЛЯ,
            "ответы": ответы}


# ---------------------------------------------------------------------------
# Плечи агрегаторов: ответ API -> поля рубрики в формате ЕГРЮЛ/ЕФРСБ
# ---------------------------------------------------------------------------

ПРИМЕЧАНИЕ_ПЛЕЧА = ("в рубрике от 2026-09-26 плечо вычеркнуто до прогона: ключа не было; "
                    "ключ получен 2026-09-27. Поля, правила сравнения и эталон не менялись "
                    "(sha256 рубрики и эталона — в прогоне)")


def _дата_iso(v):
    """«2020-09-25» -> «25.09.2020», как в поиске ЕГРЮЛ; null -> None.
    Иной тип — TypeError, формат — ValueError: битая схема, а не «даты нет»."""
    if v is None:
        return None
    if not isinstance(v, str):
        raise TypeError("дата не строка: %s" % type(v).__name__)
    return datetime.date.fromisoformat(v[:10]).strftime("%d.%m.%Y")


def _дата_мс(v):
    """Миллисекунды DaData (полночь UTC) -> «ДД.ММ.ГГГГ»; null -> None."""
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise TypeError("дата не число: %s" % type(v).__name__)
    try:
        return datetime.datetime.fromtimestamp(v / 1000, datetime.timezone.utc).strftime(
            "%d.%m.%Y")
    except (OverflowError, OSError) as e:
        raise ValueError("дата вне диапазона") from e


def _тип(d, ключ, *типы):
    """Поле обязано быть одного из типов (None — только если он перечислен)."""
    v = d.get(ключ)
    if not isinstance(v, типы):
        raise TypeError("%s: %s" % (ключ, type(v).__name__))
    return v


def _строка_руководителя(должность, фио):
    """Как поле g поиска ЕГРЮЛ: «ДОЛЖНОСТЬ: Фамилия Имя Отчество»."""
    if not фио:
        return None
    return "%s: %s" % (должность, фио) if должность else str(фио)


def checko_в_поля(d):
    """Checko v2/company data -> ответ плеча. Номер дела — из сообщений ЕФРСБ:
    одно дело во всех сообщениях — оно. Несколько разных — «не покрыто»: рубрика
    спрашивает номер текущего дела (lastLegalCase), а список сообщений его не
    выделяет, и сравнивать объединение номеров с одним — сравнивать разное.
    Стадии дела Checko не отдаёт — «не покрыто». Руковод обязателен списком (во
    всех живых ответах 27.09 — список, пустой, если руководителя нет): его
    пропажа — битая схема, как и прочие поля ЕГРЮЛ. Битая схема полей ЕГРЮЛ —
    TypeError/ValueError (прогон_агрегатор ставит «не проверено» этому ИНН)."""
    for ключ in ("ОГРН", "НаимПолн"):
        _тип(d, ключ, str)
    рук = _тип(d, "Руковод", list)
    if not all(isinstance(r, dict) for r in рук):
        raise TypeError("Руковод: не список объектов")
    for r in рук:
        _тип(r, "ФИО", str, type(None))
        _тип(r, "НаимДолжн", str, type(None))
    строка = "; ".join(filter(None, (_строка_руководителя(r.get("НаимДолжн"), r.get("ФИО"))
                                     for r in рук))) or None
    ефрсб = d.get("ЕФРСБ")
    if not isinstance(ефрсб, list) or not all(
            isinstance(m, dict) and isinstance(m.get("Дело"), (str, type(None))) for m in ефрсб):
        # пропавшее или битое поле — не «банкротства нет»
        сост_б = {"состояние": "не проверено",
                  "причина": "схема: checko — поле ЕФРСБ не список объектов с номером-строкой"}
        сообщения = []
    else:
        сообщения = ефрсб
        сост_б = {"состояние": "ok" if сообщения else "пусто"}
    дела = sorted({m.get("Дело") for m in сообщения if m.get("Дело")})
    не_покрыто = {"стадия_код": "не покрыто: Checko v2/company отдаёт сообщения "
                                "ЕФРСБ без стадии дела"}
    if len(дела) > 1:
        не_покрыто["номер_дела"] = ("не покрыто: в сообщениях ЕФРСБ у Checko %d разных "
                                    "номера дел, текущее не выделено" % len(дела))
    return {
        "егрюл": {"огрн": d.get("ОГРН"), "наименование_полное": d.get("НаимПолн"),
                  "дата_регистрации": _дата_iso(d.get("ДатаРег")),
                  "руководитель": хеш_фио(строка),
                  "дата_прекращения": _дата_iso(d.get("ДатаЛикв"))},
        "банкротство": {"дело": {"номер": дела[0] if len(дела) == 1 else None}}
        if сообщения else None,
        "_доступность": {"егрюл": {"состояние": "ok"}, "банкротство": сост_б},
        "_не_покрыто": не_покрыто,
    }


def dadata_в_поля(d):
    """DaData findById/party (головная запись) -> ответ плеча. ЕФРСБ DaData не отдаёт.
    Битая схема — TypeError/ValueError, как у checko_в_поля."""
    _тип(d, "ogrn", str)
    сост = _тип(d, "state", dict)
    имя = _тип(d, "name", dict)
    _тип(имя, "full_with_opf", str)
    упр = _тип(d, "management", dict, type(None)) or {}
    _тип(упр, "name", str, type(None))
    _тип(упр, "post", str, type(None))
    нет = "не покрыто: DaData findById/party не отдаёт сведений ЕФРСБ"
    return {
        "егрюл": {"огрн": d.get("ogrn"), "наименование_полное": имя.get("full_with_opf"),
                  "дата_регистрации": _дата_мс(сост.get("registration_date")),
                  "руководитель": хеш_фио(_строка_руководителя(упр.get("post"), упр.get("name"))),
                  "дата_прекращения": _дата_мс(сост.get("liquidation_date"))},
        "банкротство": None,
        "_доступность": {"егрюл": {"состояние": "ok"},
                         "банкротство": {"состояние": "не проверено", "причина": нет}},
    }


def адаптировать(провайдер, j, inn):
    """Ответ API -> блок плеча или None (нет записи с этим ИНН). Ошибка схемы —
    исключение, прогон_агрегатор ловит его на уровне одного ИНН."""
    if not isinstance(j, dict):
        return None
    if провайдер == "checko":
        d = j.get("data")
        return checko_в_поля(d) if isinstance(d, dict) and str(d.get("ИНН")) == inn else None
    s = j.get("suggestions")
    головные = [x["data"] for x in s if isinstance(x, dict) and isinstance(x.get("data"), dict)
                and str(x["data"].get("inn")) == inn
                and x["data"].get("branch_type") in ("MAIN", None)] if isinstance(s, list) else []
    return dadata_в_поля(головные[0]) if len(головные) == 1 else None


def прогон_агрегатор(провайдер):
    эталон = последний_эталон()
    sys.path.insert(0, str(ROOT / "scripts"))
    sys.path.insert(0, str(HERE))
    import aggregator_live as al
    имя = dict(al.ПРОВАЙДЕРЫ)[провайдер]
    ключ = (os.environ.get(имя) or "").strip()
    if not ключ:
        raise SystemExit("нет ключа: %s" % имя)
    opener = al.fc._make_opener()
    выбор = json.loads(ВЫБОРКА.read_text(encoding="utf-8"))
    ответы = {}
    начало = time.monotonic()
    for к in выбор["контрагенты"]:
        inn, т = к["инн"], time.monotonic()
        try:
            _, j = al._сырой(провайдер, ключ, opener, inn)
        except Exception as e:  # сбой сети — «источник недоступен» по всем полям
            причина = "сеть: %s" % str(e).replace(ключ, "КЛЮЧ УБРАН")[:200]
            ответы[inn] = {"_доступность": {с: {"состояние": "не проверено", "причина": причина}
                                            for с in ("егрюл", "банкротство")}}
            continue
        try:
            б = адаптировать(провайдер, j, inn)
            причина = "схема: %s — нет записи с этим ИНН" % провайдер
        except (ValueError, TypeError, AttributeError, OverflowError, OSError) as e:
            б, причина = None, "схема: %s — ответ не разобран (%s)" % (провайдер, type(e).__name__)
        if б is None:
            б = {"_доступность": {с: {"состояние": "не проверено", "причина": причина}
                                  for с in ("егрюл", "банкротство")}}
        б["секунд"] = round(time.monotonic() - т, 1)
        ответы[inn] = б
    return {"плечо": {"checko": "Checko API v2/company",
                      "dadata": "DaData API findById/party"}[провайдер],
            "версия": "адаптер benchmark/bench.py (%s_в_поля)" % провайдер,
            "дата_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "сеть": "не-РФ IP, без прокси", "тариф": "ключ пользователя (тариф не проверялся)",
            "ручные_шаги": "нет", "секунд_всего": round(time.monotonic() - начало, 1),
            "примечание": ПРИМЕЧАНИЕ_ПЛЕЧА,
            "адаптер": "даты -> ДД.ММ.ГГГГ; руководитель — «ДОЛЖНОСТЬ: ФИО» как поле g "
                       "ЕГРЮЛ, под HMAC; поля, которых API не отдаёт, и несколько номеров "
                       "дел без выделенного текущего — «не покрыто»; битая схема — "
                       "«не проверено» по ИНН",
            "эталон": str(эталон.relative_to(HERE)), "эталон_sha256": _sha(эталон),
            "рубрика": РУБРИКА.name, "рубрика_sha256": _sha(РУБРИКА), "ключ_id": ключ_id(),
            "хеш_руководителя": ХЕШ_РУКОВОДИТЕЛЯ,
            # код адаптера меняется — прогон помнит bench.py целиком (адаптеры,
            # сравнение); грубо, зато любая правка видна
            "bench_py_sha256": _sha(Path(__file__)),
            "ответы": ответы}


# Поле рубрики -> (раздел эталона, ключ эталона, извлечь значение из блока плеча)
ПОЛЯ = {
    "огрн": ("егрюл", "огрн", lambda б: (б.get("егрюл") or {}).get("огрн")),
    "наименование_полное": ("егрюл", "наименование_полное",
                            lambda б: (б.get("егрюл") or {}).get("наименование_полное")),
    "дата_регистрации": ("егрюл", "дата_регистрации",
                         lambda б: (б.get("егрюл") or {}).get("дата_регистрации")),
    # в прогоне руководитель уже под HMAC (прогон_наше), как и в эталоне
    "руководитель": ("егрюл", "руководитель", lambda б: (б.get("егрюл") or {}).get("руководитель")),
    "дата_прекращения": ("егрюл", "дата_прекращения",
                         lambda б: (б.get("егрюл") or {}).get("дата_прекращения")),
    "банкротство_есть_запись": ("банкротство", "есть_запись",
                                lambda б: True if isinstance(б.get("банкротство"), dict)
                                else None),
    "номер_дела": ("банкротство", "номер_дела",
                   lambda б: ((б.get("банкротство") or {}).get("дело") or {}).get("номер")),
    "стадия_код": ("банкротство", "стадия_код",
                   lambda б: ((б.get("банкротство") or {}).get("дело") or {}).get("стадия_код")),
}
ИСТОЧНИК_ПОЛЯ = {"егрюл": "егрюл", "банкротство": "банкротство"}


def _пусто(v):
    """Пустое значение поля (не False: «записи нет» — тоже факт)."""
    return v is None or v == ""


def сравнить(эталон, прогон):
    """-> [{"инн", "поле", "вердикт", "эталон", "плечо", "причина"}] по всем полям рубрики."""
    строки = []
    for э in эталон["записи"]:
        inn = э["инн"]
        б = прогон["ответы"].get(inn) or {}
        for поле, (раздел, ключ, извлечь) in ПОЛЯ.items():
            эт = э.get(раздел)
            av = (б.get("_доступность") or {}).get(ИСТОЧНИК_ПОЛЯ[раздел]) or {}
            сост = av.get("состояние")
            эт_знач = эт.get(ключ) if эт is not None else None
            плечо = извлечь(б)
            if поле == "банкротство_есть_запись" and сост == "пусто":
                плечо = False
            не_покрыто = (б.get("_не_покрыто") or {}).get(поле)
            не_собрано = ((эт or {}).get("_не_собрано") or {}).get(ключ)
            if эт is None:
                вердикт = "эталон не собран"
            elif не_собрано:
                вердикт = "эталон не собран"
                av = dict(av, причина=не_собрано)
            elif не_покрыто and сост in ("ok", "пусто"):
                # поле, которого плечо не отдаёт при живом источнике
                вердикт = "функция не поддерживается"
                av = dict(av, причина=не_покрыто)
            elif сост == "не проверено":
                вердикт = ("функция не поддерживается"
                           if str(av.get("причина", "")).startswith("не покрыто:")
                           else "источник недоступен")
            elif сост not in ("ok", "пусто"):
                # нет ответа плеча или состояния источника — не «совпало»
                вердикт = "источник недоступен"
                av = dict(av, причина=av.get("причина") or "плечо не отдало состояние источника")
            elif эт_знач == плечо or (_пусто(эт_знач) and _пусто(плечо)):
                вердикт = "совпало"
            else:
                вердикт = "ошибка факта"
            строки.append({"инн": inn, "поле": поле, "вердикт": вердикт, "эталон": эт_знач,
                           "плечо": плечо, "причина": av.get("причина")})
    return строки


def отчёт_md(эталон, прогон, строки):
    out = ["# Бенчмарк: плечо %s" % прогон["плечо"], "",
           "Дата прогона: %s; версия %s; сеть: %s; тариф: %s; ручные шаги: %s; время: %s с." % (
               прогон["дата_utc"], прогон["версия"], прогон["сеть"], прогон["тариф"],
               прогон["ручные_шаги"], прогон["секунд_всего"]),
           "Эталон собран %s (%s). Формулировки — на дату прогона." % (
               эталон["собрано_utc"], эталон["сеть"]), "",
           "## Дефекты", ""]
    дефекты = [s for s in строки if s["вердикт"] != "совпало"]
    if not дефекты:
        out.append("Дефектов по полям рубрики не найдено.")
    for s in дефекты:
        out.append("- `%s` · %s — **%s**: эталон %s, плечо %s%s" % (
            s["инн"], s["поле"], s["вердикт"], json.dumps(s["эталон"], ensure_ascii=False),
            json.dumps(s["плечо"], ensure_ascii=False),
            ("; причина: %s" % s["причина"]) if s["причина"] and s["вердикт"] != "ошибка факта"
            else ""))
    out += ["", "Сверено полей: %d по %d контрагентам; не собрано в эталоне: %s." % (
        len(строки), len(эталон["записи"]),
        "; ".join("%s — %s" % kv for kv in эталон["не_собрано"].items()))]
    return "\n".join(out) + "\n"


def main(argv):
    if argv[1:2] == ["выборка"]:
        ВЫБОРКА.write_text(json.dumps(выборка(), ensure_ascii=False, indent=1) + "\n",
                           encoding="utf-8")
        return 0
    if argv[1:2] == ["эталон"]:
        м = собрать_эталон()
        print(json.dumps({k: v.get("http", v.get("не_собрано", "выписка разобрана"))
                          for k, v in м["сырые"].items()}, ensure_ascii=False))
        return 0
    if argv[1:2] == ["прогон"]:
        плечо = argv[2] if len(argv) > 2 else "наше"
        if плечо not in ("наше", "checko", "dadata"):
            sys.stderr.write(__doc__)
            return 2
        п = прогон_наше() if плечо == "наше" else прогон_агрегатор(плечо)
        ПРОГОНЫ.mkdir(exist_ok=True)
        путь = ПРОГОНЫ / ("%s_%s.json" % (п["дата_utc"][:10], плечо))
        путь.write_text(json.dumps(п, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(путь)
        return 0
    if argv[1:2] == ["отчёт"] and len(argv) == 3:
        прогон = json.loads(Path(argv[2]).read_text(encoding="utf-8"))
        путь = HERE / прогон["эталон"]
        if _sha(путь) != прогон["эталон_sha256"]:
            raise SystemExit("эталон %s изменён после прогона — отчёт не строится" % путь)
        рубрика = (HERE / прогон.get("рубрика", "rubric.json")).resolve()  # до v2 поля не было
        if рубрика.parent != HERE or _sha(рубрика) != прогон.get("рубрика_sha256"):
            raise SystemExit("рубрика изменена после прогона — отчёт не строится")
        эталон = json.loads(путь.read_text(encoding="utf-8"))
        # эталон v1 рубрику не записывал: он собран под rubric.json
        if (эталон.get("рубрика", "rubric.json"), эталон.get("рубрика_sha256",
                                                            прогон.get("рубрика_sha256"))) \
                != (рубрика.name, прогон.get("рубрика_sha256")):
            raise SystemExit("эталон собран под другую рубрику — отчёт не строится")
        if эталон.get("хеш_руководителя", "v1") != прогон.get("хеш_руководителя", "v1"):
            raise SystemExit("эталон и прогон хешируют руководителя по-разному — отчёт "
                             "не строится")
        if not эталон.get("ключ_id") or эталон.get("ключ_id") != прогон.get("ключ_id"):
            raise SystemExit("эталон и прогон под разными HMAC-ключами — руководитель "
                             "несравним, отчёт не строится")
        sys.stdout.write(отчёт_md(эталон, прогон, сравнить(эталон, прогон)))
        return 0
    sys.stderr.write(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
