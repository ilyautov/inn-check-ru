#!/usr/bin/env python3
"""
cbr_warning.py — сверка по «Списку компаний с выявленными признаками
нелегальной деятельности на финансовом рынке» Банка России (warning-list).
Только stdlib.

Выгрузка списка целиком (JSON, ≈11 МБ, ≈27 тыс. записей) скачивается один раз
в кэш ~/.cache/inn-check-ru/cbr_warning/, сверка по ИНН идёт локально: ИНН
проверяемых контрагентов в ЦБ не уходит.

Использование:
    python3 cbr_warning.py --refresh           # скачать/обновить выгрузку
    python3 cbr_warning.py --inn 7707083893    # сверка по кэшу

Что это значит и чего НЕ значит (проверено на выгрузке 26.09.2026):
  * формулировка — «ЦБ сообщает о признаках нелегальной деятельности на
    финансовом рынке (<признак>), в списке с <дата>», а не «мошенник»: это
    признаки, выявленные регулятором, не приговор суда;
  * ИНН указан лишь у ≈3 тыс. из ≈27 тыс. записей (у юрлиц; интернет-проекты
    и «точки присутствия» — без ИНН). Совпадение ищется только по точному ИНН,
    поэтому «совпадения нет» ≠ «в списке нет»: под другим названием или
    сайтом компания может там быть;
  * ИНН в списке — только 10-значные (юрлица): для ИП сверка «не покрыто»;
  * Closed = true — ЦБ отмечает организацию ликвидированной (isLikvid в API
    карточки); запись из списка при этом не исчезает, признак остаётся фактом;
  * комментарий «Компания использует данные легального участника финансового
    рынка» — клон: чужие мошенники выдают себя за эту компанию. Совпадение по
    ИНН такой записи НЕ сигнал против владельца ИНН, а повод проверить вручную.

Источник: https://www.cbr.ru/inside/warning-list/ (ссылка «Скачать файл» →
/inside/warning-list/black-list-json); публичный API карточек —
https://www.cbr.ru/warninglistapi/swagger.
"""

import datetime
import hashlib
import json
import os
import re
import secrets
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

URL = "https://www.cbr.ru/inside/warning-list/black-list-json"
СТРАНИЦА = "https://www.cbr.ru/inside/warning-list/"
КАРТОЧКА = "https://www.cbr.ru/warninglistapi/DetailInfo?id=%d"
CACHE_DIR = Path(os.path.expanduser(os.path.join("~", ".cache", "inn-check-ru", "cbr_warning")))
ФАЙЛ = "black-list.json"
МЕТА = "meta.json"
# Список ЦБ пополняется ежедневно; двухнедельная выгрузка ещё годится как
# «на дату», старше — «не проверено», а не тихое «чисто» по устаревшему кэшу.
TTL_ДНЕЙ = 14
TIMEOUT = 120
МАКС_БАЙТ = 100 * 1024 * 1024
# Нижняя граница размера списка: выгрузка на 26.09.2026 — 27 264 записи.
# Обрезанный или подменённый ответ меньше этого — не повод затереть кэш.
МИН_ЗАПИСЕЙ = 10000
# И нижняя граница записей с ИНН (на 26.09.2026 — 2 997): если ЦБ перестанет
# заполнять INN у всех записей, схема формально цела, а каждая сверка дала бы
# ложное «совпадения нет». Такая выгрузка — дрейф, а не повод затереть кэш.
МИН_С_ИНН = 1500
КЛОН = "использует данные легального участника"

ПОЛЯ = {"Id": int, "DT": str, "Name": str, "INN": str, "Sign": str, "Closed": bool,
        "OrgType": str, "Comment": str}
# Эти поля могут быть null, но ключ обязан быть: пропавший ключ INN читался бы
# как «ИНН не указан» (ложное «совпадения нет»), пропавший Comment — как «не
# клон» (сигнал против владельца ИНН, чьими данными пользуются мошенники).
МОЖНО_NULL = {"INN", "Name", "OrgType", "Comment"}
_ДАТА = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_ИНН = re.compile(r"\d{10}\Z|\d{12}\Z")


class СхемаНеТа(ValueError):
    """Выгрузка не той формы, что разбиралась 26.09.2026."""


def разобрать(данные):
    """bytes/str выгрузки -> список записей; СхемаНеТа при дрейфе схемы.

    Проверяется каждая запись: пропажа поля, смена типа или формата ИНН/даты —
    отказ целиком, а не «пропустим битую строку»: молча выпавшая запись с ИНН
    дала бы «совпадения нет» ровно там, где оно есть.
    """
    try:
        корень = json.loads(данные)
    except (ValueError, UnicodeDecodeError) as e:
        raise СхемаНеТа("не JSON: %s" % e) from None
    записи = корень.get("RC") if isinstance(корень, dict) else None
    if not isinstance(записи, list):
        raise СхемаНеТа("нет массива RC")
    if len(записи) < МИН_ЗАПИСЕЙ:
        raise СхемаНеТа("записей %d < %d — выгрузка обрезана?" % (len(записи), МИН_ЗАПИСЕЙ))
    for i, r in enumerate(записи):
        if not isinstance(r, dict):
            raise СхемаНеТа("запись %d не объект" % i)
        for поле, тип in ПОЛЯ.items():
            if поле not in r:
                raise СхемаНеТа("запись %d: нет поля %s" % (i, поле))
            v = r[поле]
            if поле in МОЖНО_NULL and v is None:
                continue
            if not isinstance(v, тип) or (тип is int and isinstance(v, bool)):
                raise СхемаНеТа("запись %d: поле %s не %s" % (i, поле, тип.__name__))
        if not _ДАТА.match(r["DT"]):
            raise СхемаНеТа("запись %d: дата включения %r" % (i, r["DT"]))
        инн = (r.get("INN") or "").strip()
        if инн and not _ИНН.match(инн):
            raise СхемаНеТа("запись %d: ИНН %r" % (i, инн))
    с_инн = sum(1 for r in записи if (r.get("INN") or "").strip())
    if с_инн < МИН_С_ИНН:
        raise СхемаНеТа("записей с ИНН %d < %d — ЦБ перестал отдавать ИНН?" % (с_инн, МИН_С_ИНН))
    return записи


def _сейчас():
    return datetime.datetime.now(datetime.timezone.utc)


def refresh(cache_dir=None, opener=None, now=None):
    """Скачать выгрузку одним запросом; кэш заменяется только годной выгрузкой.
    Возвращает словарь-итог (записано ли и почему)."""
    каталог = Path(cache_dir) if cache_dir else CACHE_DIR
    if opener is None:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import fetch_counterparty as fc  # прокси, TLS с корнем Минцифры
        opener = fc._make_opener()
        ua = fc.UA_ПРОЕКТА
    else:
        ua = "inn-check-ru"
    import urllib.request
    запрос = urllib.request.Request(URL, headers={"User-Agent": ua,
                                                  "Accept": "application/json"})
    try:
        with opener.open(запрос, timeout=TIMEOUT) as ответ:
            тело = ответ.read(МАКС_БАЙТ + 1)
            код = getattr(ответ, "status", 200)
    except Exception as e:
        return {"записано": False, "причина": "сеть: %s" % (getattr(e, "reason", None) or
                                                         type(e).__name__)}
    if код != 200 or len(тело) > МАКС_БАЙТ:
        return {"записано": False, "причина": "сеть: HTTP %s, %d байт" % (код, len(тело))}
    try:
        записи = разобрать(тело)
    except СхемаНеТа as e:
        return {"записано": False, "причина": "схема: %s — кэш не тронут" % e}
    момент = now or _сейчас()
    мета = {"url": URL, "скачано_utc": момент.isoformat(timespec="seconds"),
            "sha256": hashlib.sha256(тело).hexdigest(), "байт": len(тело),
            "записей": len(записи),
            "с_инн": sum(1 for r in записи if (r.get("INN") or "").strip()),
            "последнее_включение": max(r["DT"] for r in записи)}
    # Поколение кэша: тело — в файл с хешем в имени, затем meta.json атомарно
    # (os.replace) указывает на него. Два одновременных --refresh не смешают тело
    # одного с meta другого: meta, какая бы ни победила, ссылается на своё тело.
    каталог.mkdir(parents=True, exist_ok=True)
    # Имя поколения уникально (хеш + случайный хвост): два --refresh с одной и
    # той же выгрузкой не пишут в один файл, и очистка одного не удалит тело,
    # на которое уже указывает meta другого.
    мета["файл"] = "black-list.%s.%s.json" % (мета["sha256"][:16], secrets.token_hex(4))
    _атомарно(каталог / мета["файл"], тело)
    _атомарно(каталог / МЕТА, (json.dumps(мета, ensure_ascii=False, indent=2) + "\n")
              .encode("utf-8"))
    # Прежние поколения убираются, только когда они старше 10 минут: свежее
    # чужое тело может принадлежать параллельному --refresh, чья meta ещё не
    # записана.
    порог = time.time() - 600
    for старый in каталог.glob("black-list*.json"):
        try:  # параллельный --refresh мог убрать файл между glob и stat
            if старый.name != мета["файл"] and старый.stat().st_mtime < порог:
                старый.unlink(missing_ok=True)
        except FileNotFoundError:
            pass
    return dict(мета, записано=True)


def _атомарно(путь, байты):
    """Запись через уникальный временный файл в том же каталоге + os.replace."""
    fd, tmp = tempfile.mkstemp(dir=str(путь.parent), prefix=путь.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(байты)
        os.replace(tmp, путь)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _запись(r):
    клон = КЛОН in str(r.get("Comment") or "")
    return {
        "id": r["Id"],
        "наименование": r.get("Name"),
        "в_списке_с": r["DT"],
        "признаки": [p.strip() for p in re.split(r",\s*(?=Признаки)", r["Sign"]) if p.strip()],
        "тип": r.get("OrgType"),
        "сайты": r.get("Site"),
        "адрес": r.get("ADDR"),
        "ликвидирована_по_данным_цб": r["Closed"],
        "комментарий_цб": r.get("Comment") or None,
        "клон": клон,
        "карточка": КАРТОЧКА % r["Id"],
    }


def lookup(inn, cache_dir=None, now=None):
    """(результат, None) или (None, причина «префикс: …») для «не проверено».

    результат: {"в_списке": True|False|None, "записи": [...], "формулировка",
                "дата_выгрузки", "покрытие"}; None у «в_списке» — совпало
    только с записью-клоном (кто-то использует данные этой компании).
    """
    inn = str(inn or "").strip()
    if not _ИНН.match(inn):
        raise ValueError("ИНН — 10 или 12 цифр: %r" % inn)
    каталог = Path(cache_dir) if cache_dir else CACHE_DIR
    try:
        мета = json.loads((каталог / МЕТА).read_text(encoding="utf-8"))
        имя = str(мета.get("файл") or ФАЙЛ)
        if "/" in имя or "\\" in имя or not имя.startswith("black-list"):
            raise ValueError(имя)
        тело = (каталог / имя).read_bytes()
    except (OSError, ValueError):
        return None, "кэш: выгрузка списка ЦБ не скачана — python3 scripts/cbr_warning.py --refresh"
    if hashlib.sha256(тело).hexdigest() != мета.get("sha256"):
        return None, "кэш: файл выгрузки не совпадает с meta.json — обновите (--refresh)"
    try:
        скачано = datetime.datetime.fromisoformat(мета["скачано_utc"])
    except (KeyError, TypeError, ValueError):
        return None, "кэш: meta.json без даты скачивания — обновите (--refresh)"
    if скачано.tzinfo is None:
        return None, "кэш: meta.json без часового пояса — обновите (--refresh)"
    возраст = (now or _сейчас()) - скачано
    if возраст > datetime.timedelta(days=TTL_ДНЕЙ):
        return None, ("кэш: выгрузке списка ЦБ %d дн. (порог %d) — python3 "
                      "scripts/cbr_warning.py --refresh" % (возраст.days, TTL_ДНЕЙ))
    if возраст < -datetime.timedelta(hours=1):
        return None, "кэш: дата скачивания в будущем — обновите (--refresh)"
    try:
        записи = разобрать(тело)
    except СхемаНеТа as e:
        return None, "схема: %s" % e
    # На 26.09.2026 12-значных ИНН в списке нет. Если ЦБ начнёт их заполнять,
    # сверка ИП включится сама, а до тех пор «не покрыто», а не ложное «чисто».
    if len(inn) == 12 and not any(len((r.get("INN") or "").strip()) == 12 for r in записи):
        return None, ("не покрыто: в списке ЦБ ИНН указан только у юрлиц; ИП — поиск "
                      "по наименованию и сайту вручную на %s" % СТРАНИЦА)
    найдено = [_запись(r) for r in записи if (r.get("INN") or "").strip() == inn]
    настоящие = [z for z in найдено if not z["клон"]]
    покрытие = ("ИНН есть у %d из %d записей списка; сверка — по точному ИНН, поэтому "
                "отсутствие совпадения не значит, что компании в списке нет"
                % (мета.get("с_инн", 0), мета.get("записей", 0)))
    результат = {
        "в_списке": True if настоящие else (None if найдено else False),
        "записи": найдено,
        "дата_выгрузки": мета["скачано_utc"][:10],
        "покрытие": покрытие,
    }
    if настоящие:
        z = настоящие[0]
        результат["формулировка"] = (
            "ЦБ сообщает о признаках нелегальной деятельности на финансовом рынке (%s), "
            "в списке с %s%s" % ("; ".join(z["признаки"]).lower(), z["в_списке_с"],
                                 ", ЦБ отмечает организацию ликвидированной"
                                 if z["ликвидирована_по_данным_цб"] else ""))
    elif найдено:
        результат["формулировка"] = (
            "ЦБ отмечает, что данные компании «%s» использует нелегальный участник "
            "рынка — это не признак против неё самой; проверьте, с кем именно имеете "
            "дело (реквизиты, сайт)" % найдено[0]["наименование"])
    return результат, None


def main(argv):
    args = argv[1:]
    if args == ["--refresh"]:
        итог = refresh()
        sys.stdout.write(json.dumps(итог, ensure_ascii=False, indent=2) + "\n")
        return 0 if итог.get("записано") else 1
    if len(args) == 2 and args[0] == "--inn":
        try:
            рез, причина = lookup(args[1])
        except ValueError as e:
            sys.stderr.write("%s\n" % e)
            return 2
        sys.stdout.write(json.dumps(рез if рез is not None else
                                    {"состояние": "не проверено", "причина": причина},
                                    ensure_ascii=False, indent=2) + "\n")
        return 0
    sys.stderr.write(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
