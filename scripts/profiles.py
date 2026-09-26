#!/usr/bin/env python3
"""
profiles.py — профили цели проверки: одни и те же факты весят по-разному в разных
сценариях. Отрицательные чистые активы — стоп для отсрочки, аргумент в торге при
покупке доли; нейтральный режим «просто посмотреть» — карточка фактов без вердикта.

Данные — data/profiles_ru.json (спек волны 1, §6): семь профилей + единый каталог
сигналов. Идентификаторы профилей зафиксированы в scripts/sources.py (PROFILE_IDS).

Использование:
    python3 profiles.py --list
    python3 profiles.py --профиль отсрочка --fetch a.json [--fin b.json]
    python3 profiles.py --validate

Вход: fetch-JSON (вывод fetch_counterparty.py: блоки источников + `_доступность`
словарями по §1.1 + `_итог_проверки` по §1.3; старый строковый формат `_доступность`
читается тоже) и fin-JSON (вывод fin_scoring.py). Без --fin финансы считаются
fin_scoring'ом из блока «финансы» fetch-JSON.

Правило резолвера — только вверх по тревожности:
    🔴 — найден хоть один deal_killer профиля (или сигнал с severity 🔴 после override);
    🟡 — найден 🟡-сигнал и проверка состоялась;
    🟢 — сигналов нет, проверка состоялась и все обязательные источники профиля
         проверены;
    null — проверка не состоялась (deal-killer-источники не проверены), не проверен
           обязательный источник профиля при отсутствии сигналов (🟢 обещал бы
           «исков и долгов не найдено» о том, чего не смотрели) или профиль
           «нейтрально» (факты без вердикта).
Найденный 🔴 выдаётся и при неполной проверке — «не проверено» никогда не смягчает
найденный стоп-сигнал, но 🟢 без проверенных deal-killer недопустим.

Сигнал из блока со состоянием «не проверено» — «не проверен», а не «отсутствует».
Никогда не падает traceback'ом: любая проблема — JSON «не проверено» с причиной.
"""

import datetime
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_PATH = HERE.parent / "data" / "profiles_ru.json"

СТАТУСЫ = ("найден", "отсутствует", "не проверен", "не применимо")
SEVERITY = ("🔴", "🟡", "⚪")
_РАНГ = {"⚪": 0, "🟡": 1, "🔴": 2}
_ПОРЯДОК_ФАКТОВ = {"найден": 0, "не проверен": 1, "отсутствует": 2, "не применимо": 3}

# Резервный список deal-killer-источников, если sources.py не импортируется
# (спек §1.3); в норме берётся из sources.deal_killer_ids().
_DEAL_KILLER_ИСТОЧНИКИ = ("егрюл", "риски", "спецреестры", "фссп", "суды",
                          "банкротство", "санкции")

# Значения, означающие «признака нет» внутри ok-блока (спек §1.2).
_НЕТ = {"", "нет", "false", "0", "не выявлено", "отсутствует", "не найдено",
        "не числится", "-", "—", "none", "null", "no"}
_ЛИКВИДАЦИЯ_МАРКЕРЫ = ("ликвид", "прекращ", "реорганиз", "исключ", "недейств",
                       "банкрот")
_МСП_В_РЕЕСТРЕ = ("в реестре",)
_ДАТА_DMY = re.compile(r"^(?P<d>\d{2})\.(?P<m>\d{2})\.(?P<y>\d{4})")
_ДАТА_ISO = re.compile(r"^(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})")


class ProfilesError(Exception):
    """Профили не читаются / не проходят проверку формы."""


# --- загрузка ----------------------------------------------------------------

def _sources():
    """Модуль scripts/sources.py, если импортируется; иначе None (деградация: id вместо имён)."""
    try:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import sources
        return sources
    except Exception:
        return None


def load(path=None):
    """Читает data/profiles_ru.json (или path). Бросает ProfilesError с русской причиной."""
    p = Path(path) if path else DEFAULT_PATH
    raw = None
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as e:
        if path is None:
            # пакетная установка (поток D): данные едут как package-data
            try:
                from importlib import resources
                raw = (resources.files("inn_check_ru_data") / "profiles_ru.json") \
                    .read_text(encoding="utf-8")
            except Exception:
                raw = None
        if raw is None:
            raise ProfilesError("файл профилей не читается: %s (%s)" % (p, e)) from e
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise ProfilesError("файл профилей не JSON: %s (%s)" % (p, e)) from e
    if not isinstance(data, dict) or not isinstance(data.get("профили"), dict) \
            or not isinstance(data.get("сигналы"), dict):
        raise ProfilesError("файл профилей без ключей «профили»/«сигналы»: %s" % p)
    return data


def validate(profiles, sources):
    """Проверка формы профилей против реестра источников. Возвращает список ошибок."""
    errors = []
    if not isinstance(profiles, dict):
        return ["профили: не словарь"]
    каталог = profiles.get("сигналы")
    профили = profiles.get("профили")
    if not isinstance(каталог, dict) or not каталог:
        errors.append("нет каталога «сигналы»")
        каталог = {}
    if not isinstance(профили, dict) or not профили:
        errors.append("нет словаря «профили»")
        профили = {}
    for sid, s in каталог.items():
        if not isinstance(s, dict):
            errors.append("сигнал %s: не словарь" % sid)
            continue
        for k in ("описание", "откуда", "severity_по_умолчанию"):
            if not s.get(k):
                errors.append("сигнал %s: нет ключа %s" % (sid, k))
        if s.get("severity_по_умолчанию") not in SEVERITY:
            errors.append("сигнал %s: severity_по_умолчанию %r" % (sid, s.get("severity_по_умолчанию")))
        for path in s.get("откуда") or []:
            block = str(path).split(".")[0]
            if block != "fin" and sources is not None and block not in sources.SOURCES:
                errors.append("сигнал %s: «откуда» %s — источника %s нет в реестре" % (sid, path, block))
    известные = tuple(getattr(sources, "PROFILE_IDS", ())) if sources is not None else ()
    for pid in известные:
        if pid not in профили:
            errors.append("профиль %s из sources.PROFILE_IDS отсутствует в файле" % pid)
    req = ("описание", "вопрос", "источники_обязательные", "deal_killers",
           "severity_override", "формулировка")
    for pid, p in профили.items():
        if not isinstance(p, dict):
            errors.append("%s: не словарь" % pid)
            continue
        if известные and pid not in известные:
            errors.append("%s: неизвестный профиль (нет в sources.PROFILE_IDS)" % pid)
        for k in req:
            if k not in p:
                errors.append("%s: нет ключа %s" % (pid, k))
        if "рекомендации" not in p:
            errors.append("%s: нет ключа рекомендации" % pid)
        if p.get("формулировка") not in ("факты", "вердикт"):
            errors.append("%s: формулировка %r" % (pid, p.get("формулировка")))
        for src in p.get("источники_обязательные") or []:
            if sources is not None and src not in sources.SOURCES:
                errors.append("%s: источник %s не существует в sources.SOURCES" % (pid, src))
            elif sources is not None and hasattr(sources, "sources_for_profile") \
                    and src not in sources.sources_for_profile(pid):
                errors.append("%s: источник %s обязателен, но SOURCES[%s].профили его "
                              "для профиля не включает" % (pid, src, src))
        for sid in p.get("deal_killers") or []:
            if sid not in каталог:
                errors.append("%s: deal_killer %s не из каталога сигналов" % (pid, sid))
        ov = p.get("severity_override")
        if not isinstance(ov, dict):
            errors.append("%s: severity_override не словарь" % pid)
        else:
            for sid, sev in ov.items():
                if sid not in каталог:
                    errors.append("%s: severity_override %s не из каталога сигналов" % (pid, sid))
                if sev not in SEVERITY:
                    errors.append("%s: severity_override %s = %r" % (pid, sid, sev))
        rec = p.get("рекомендации")
        if p.get("формулировка") == "факты":
            if rec is not None:
                errors.append("%s: у профиля «факты» рекомендации должны быть null" % pid)
        elif not isinstance(rec, dict) or set(rec) != {"🟢", "🟡", "🔴"} \
                or not all(isinstance(v, str) and v for v in rec.values()):
            errors.append("%s: рекомендации должны быть ровно три фразы 🟢/🟡/🔴" % pid)
    default = profiles.get("по_умолчанию")
    if default not in профили:
        errors.append("по_умолчанию %r — нет такого профиля" % default)
    return errors


def sources_for(profile_id, profiles=None):
    """Обязательные источники профиля из файла ([] для неизвестного).
    Реестровый взгляд (что fetch собирает под профиль) — sources.sources_for_profile()."""
    try:
        profiles = profiles or load()
    except ProfilesError:
        return []
    p = profiles.get("профили", {}).get(profile_id)
    if not isinstance(p, dict):
        return []
    return list(p.get("источники_обязательные") or [])


# --- чтение fetch-JSON --------------------------------------------------------

def _истина(v):
    """True — признак есть; False — явно нет; None — неизвестно (null внутри блока)."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() not in _НЕТ
    if isinstance(v, dict):
        if "статус" in v:  # спецреестры: {"статус": "проверено"|"не проверено", "данные": ...}
            st = str(v.get("статус") or "").lower()
            if st.startswith("не") or not st:
                return None
            return _истина(v.get("данные"))
        for k in ("rows", "записи", "items", "data"):
            if isinstance(v.get(k), list):
                return len(v[k]) > 0
        for k in ("total", "count", "всего", "записей"):
            if isinstance(v.get(k), (int, float)):
                return v[k] > 0
        return bool(v)
    if isinstance(v, (list, tuple)):
        return len(v) > 0
    return bool(v)


def _состояние_блока(fetch, block):
    """(состояние, причина, дата) блока по `_доступность` (словарь §1.1 или строка)
    с поправкой «manual as truth»: блок со своим статусом «проверено» считается ok."""
    av = (fetch.get("_доступность") or {}).get(block) if isinstance(fetch.get("_доступность"), dict) else None
    blk = fetch.get(block)
    state, reason, date = None, None, None
    if isinstance(av, dict):
        state = av.get("состояние")
        reason = av.get("причина")
        date = av.get("дата")
    elif isinstance(av, str):
        s = av.strip().lower()
        if s.startswith(("ok", "проверено")) or s in ("собрано", "ок"):
            state = "ok"
        elif s.startswith("пусто"):
            state = "пусто"
        else:
            state, reason = "не проверено", av
    if isinstance(blk, dict):
        own = str(blk.get("статус") or "").strip().lower()
        if own in ("проверено", "ok", "ок", "собрано") and state != "ok":
            state = "ok"
            reason = reason or "по статусу блока (ручной ввод / браузер)"
        elif own.startswith(("не проверено", "не собрано")):
            state = "не проверено"
            reason = blk.get("причина") or blk.get("примечание") or reason or own
        date = date or blk.get("дата") or blk.get("дата_проверки")
    if state not in ("ok", "пусто", "не проверено"):
        state = "не проверено"
        reason = reason or ("блок «%s» отсутствует в fetch-JSON" % block
                            if blk is None and av is None else "состояние не распознано")
    date = date or fetch.get("дата") or fetch.get("дата_проверки") or fetch.get("_дата")
    return state, reason, date


def _значение(fetch, path):
    block, _, field = path.partition(".")
    blk = fetch.get(block)
    if not isinstance(blk, dict):
        return None
    return blk.get(field)


def _дата(s):
    """«dd.mm.yyyy» / «yyyy-mm-dd» -> date; иначе None (без tz — это календарные даты реестров)."""
    if not isinstance(s, str):
        return None
    m = _ДАТА_DMY.match(s.strip()) or _ДАТА_ISO.match(s.strip())
    if not m:
        return None
    try:
        g = m.groupdict()
        return datetime.date(int(g["y"]), int(g["m"]), int(g["d"]))
    except ValueError:
        return None


def _оценить_путь(sid, path, fetch, ref_date):
    """True/False/None для одного пути «откуда» внутри ok-блока."""
    v = _значение(fetch, path)
    if sid == "ликвидация":
        if path.endswith("дата_прекращения"):
            return bool(v) if v is not None else False
        if isinstance(v, str):
            low = v.lower()
            return any(m in low for m in _ЛИКВИДАЦИЯ_МАРКЕРЫ)
        return _истина(v) if v is not None else None
    if sid == "не_в_мсп":
        if isinstance(v, str):
            return not any(m in v.lower() for m in _МСП_В_РЕЕСТРЕ)
        return None
    if sid == "молодая_компания":
        d = _дата(v)
        if d is None:
            return None
        ref = _дата(ref_date) or datetime.datetime.now(tz=datetime.timezone.utc).date()
        return (ref - d).days < 365
    if sid == "рфм_совпадение":
        if isinstance(v, list):
            return len(v) > 0
        совп = _значение(fetch, "санкции.совпадений")
        if isinstance(совп, (int, float)):
            return совп > 0
        return _истина(v)
    if sid == "фссп_производства" and isinstance(v, (int, float)) and not isinstance(v, bool):
        return v > 0
    return _истина(v)


def _пусто_как(sid, block):
    """Что означает состояние «пусто» блока для сигнала: False (подтверждённое отсутствие),
    True (не_в_мсп) или None (финансы/егрюл — оценить нельзя)."""
    if block == "мсп":
        return True if sid == "не_в_мсп" else None
    if block in ("финансы", "егрюл"):
        return None
    return False


def _fin_оценка(fetch, fin_json):
    """(флаги {id: флаг}, статус, причина, не_проверено[]) из fin-JSON;
    без fin-JSON — считаем fin_scoring'ом из блока «финансы» fetch-JSON."""
    if fin_json is None:
        try:
            if str(HERE) not in sys.path:
                sys.path.insert(0, str(HERE))
            import fin_scoring
            fin_json = fin_scoring.score(fetch)
        except Exception as e:
            return {}, "не проверено", ("fin-JSON не передан, fin_scoring недоступен (%s)"
                                        % type(e).__name__), []
    if not isinstance(fin_json, dict):
        return {}, "не проверено", "fin-JSON не словарь", []
    st = str(fin_json.get("статус") or "").lower()
    if st not in ("ок", "ok"):
        return {}, "не проверено", (fin_json.get("причина")
                                    or "fin_scoring: статус %r" % fin_json.get("статус")), []
    флаги = {f.get("id"): f for f in fin_json.get("флаги") or [] if isinstance(f, dict)}
    fp = fin_json.get("финансовый_профиль")
    нп = list(fp.get("не_проверено") or []) if isinstance(fp, dict) else []
    return флаги, "ок", None, нп


_FIN_НЕ_ПРОВЕРЕНО = {
    "ча_отрицательные_2года": "чистые активы", "ча_ниже_уставного_капитала": "чистые активы",
    "коэффициент_автономии": "автоном", "текущая_ликвидность": "ликвидност",
    "падение_выручки": "выручк", "транзитный_профиль": "выручк", "убыток_2года": "убыток",
}


def _имя_источника(block, sources):
    if block == "fin":
        block = "финансы"
    if sources is not None and block in getattr(sources, "SOURCES", {}):
        d = sources.SOURCES[block]
        return "%s %s" % (d.get("tier", ""), d.get("название", block))
    return block


def extract_signals(fetch_json, fin_json=None, profiles=None):
    """Все сигналы каталога со статусом найден / отсутствует / не проверен / не применимо,
    источником и датой. Читает `_доступность`: блок «не проверено» → сигнал «не проверен»."""
    try:
        profiles = profiles or load()
    except ProfilesError:
        return []
    if not isinstance(fetch_json, dict):
        fetch_json = {}
    sources = _sources()
    тип = str(fetch_json.get("тип") or "юрлицо")
    fin = None  # (флаги, статус, причина, не_проверено) — считается один раз
    fin_дата = _состояние_блока(fetch_json, "финансы")[2]
    out = []
    for sid, s in profiles["сигналы"].items():
        rec = {"id": sid, "описание": s.get("описание"), "статус": "не проверен",
               "severity_по_умолчанию": s.get("severity_по_умолчанию", "🟡"),
               "источник": None, "источник_id": None, "дата": None,
               "значение": None, "причина": None}
        применимо = s.get("применимо_к")
        if применимо and тип not in применимо:
            rec["статус"] = "не применимо"
            rec["причина"] = "к %s не применимо (см. references/scenarii.md «Проверка ИП»)" % (
                "ИП" if тип == "ип" else тип)
            out.append(rec)
            continue
        пути = s.get("откуда") or []
        if пути and all(str(p).startswith("fin.") for p in пути):
            if fin is None:
                fin = _fin_оценка(fetch_json, fin_json)
            fin_флаги, fin_статус, fin_причина, fin_нп = fin
            rec["источник_id"] = "финансы"
            rec["источник"] = _имя_источника("финансы", sources)
            rec["дата"] = fin_дата
            флаг = fin_флаги.get(sid)
            if fin_статус != "ок":
                rec["причина"] = fin_причина
            elif флаг is not None:
                rec["статус"] = "найден"
                rec["значение"] = {k: флаг.get(k) for k in ("показатель", "значение", "порог", "год")
                                   if k in флаг}
                rec["пояснение"] = флаг.get("пояснение")
            else:
                ключ = _FIN_НЕ_ПРОВЕРЕНО.get(sid, sid)
                hit = [x for x in fin_нп if ключ in str(x).lower()]
                if hit:
                    rec["причина"] = "; ".join(hit)
                else:
                    rec["статус"] = "отсутствует"
            out.append(rec)
            continue
        найден, отсутствует, причины = None, [], []
        for path in пути:
            block = str(path).split(".")[0]
            state, reason, date = _состояние_блока(fetch_json, block)
            if state == "ok":
                v = _оценить_путь(sid, path, fetch_json, date)
            elif state == "пусто":
                v = _пусто_как(sid, block)
                if v is None:
                    причины.append("%s: источник ответил, записи по ИНН нет" % block)
            else:
                v = None
                причины.append("%s: %s" % (block, reason or "не проверено"))
            if v is True:
                найден = (block, date, _значение(fetch_json, path))
                break
            if v is False:
                отсутствует.append((block, date))
            elif state == "ok":
                причины.append("%s: поле %s не отдано (null внутри ok-блока)" % (block, path.split(".", 1)[-1]))
        if найден:
            block, date, val = найден
            rec.update(статус="найден", источник_id=block, источник=_имя_источника(block, sources),
                       дата=date, значение=val if not isinstance(val, dict) else json.dumps(val, ensure_ascii=False)[:200])
        elif отсутствует:
            block, date = отсутствует[0]
            rec.update(статус="отсутствует", источник_id=block,
                       источник=_имя_источника(block, sources), дата=date)
            if причины:
                rec["причина"] = "частично: " + "; ".join(dict.fromkeys(причины))
        else:
            rec["причина"] = "; ".join(dict.fromkeys(причины)) or "нет путей «откуда»"
        out.append(rec)
    return out


# --- резолвер ----------------------------------------------------------------

def _по_доступности(fetch, sources):
    """Правило §1.3, если fetch не дал `_итог_проверки`: (не проверено среди deal-killer) * 2 <= всего."""
    ids = list(sources.deal_killer_ids()) if sources is not None and hasattr(sources, "deal_killer_ids") \
        else list(_DEAL_KILLER_ИСТОЧНИКИ)
    нп = [i for i in ids if _состояние_блока(fetch, i)[0] == "не проверено"]
    return len(нп) * 2 <= len(ids), нп


def _факт(rec):
    d = rec.get("дата") or "дата неизвестна"
    src = rec.get("источник") or rec.get("источник_id") or "источник неизвестен"
    st = rec["статус"]
    if st == "найден":
        val = rec.get("значение")
        if isinstance(val, dict) and "показатель" in val:  # флаг fin_scoring
            val_s = " (%s = %s, порог %s, %s)" % (val.get("показатель"), val.get("значение"),
                                                  val.get("порог"), val.get("год"))
        elif val in (None, True):
            val_s = ""
        else:
            val_s = " (%s)" % (json.dumps(val, ensure_ascii=False) if isinstance(val, dict) else val)
        текст = "%s%s — %s, %s" % (rec["описание"], val_s, src, d)
    elif st == "отсутствует":
        текст = "%s: не выявлено — %s, %s" % (rec["описание"], src, d)
    elif st == "не применимо":
        текст = "%s: %s" % (rec["описание"], rec.get("причина") or "не применимо")
    else:
        текст = "%s: не проверено (%s)" % (rec["описание"], rec.get("причина") or "причина не указана")
    return {"сигнал": rec["id"], "статус": st, "текст": текст,
            "источник": src if st in ("найден", "отсутствует") else None, "дата": rec.get("дата")}


def _не_проверено(profile_id, причина):
    return {"профиль": profile_id, "светофор": None, "поднят_сигналами": [],
            "рекомендация": "не проверено: %s" % причина, "факты": [],
            "не_проверено_deal_killer": [], "проверка_состоялась": False,
            "статус": "не проверено", "причина": причина}


def resolve(fetch_json, fin_json, profile_id, profiles=None):
    """Светофор по профилю. См. правило в docstring модуля."""
    try:
        profiles = profiles or load()
    except ProfilesError as e:
        return _не_проверено(profile_id, str(e))
    if not isinstance(fetch_json, dict):
        return _не_проверено(profile_id, "fetch-JSON не словарь")
    if "ошибка" in fetch_json and len(fetch_json) <= 3:
        return _не_проверено(profile_id, "fetch_counterparty вернул ошибку: %s" % fetch_json.get("ошибка"))
    профили = profiles["профили"]
    if profile_id is None:
        profile_id = profiles.get("по_умолчанию", "нейтрально")
    prof = профили.get(profile_id)
    if not isinstance(prof, dict):
        return _не_проверено(profile_id, "неизвестный профиль %r; доступны: %s"
                             % (profile_id, ", ".join(профили)))
    sources = _sources()
    сигналы = extract_signals(fetch_json, fin_json, profiles)
    dk = list(prof.get("deal_killers") or [])
    ov = prof.get("severity_override") or {}

    def sev(rec):
        if rec["id"] in dk:
            return "🔴"
        return ov.get(rec["id"], rec.get("severity_по_умолчанию", "🟡"))

    итог = fetch_json.get("_итог_проверки")
    if isinstance(итог, dict) and isinstance(итог.get("проверка_состоялась"), bool):
        fetch_ok = итог["проверка_состоялась"]
        fetch_нп = list(итог.get("deal_killer_не_проверено") or [])
    else:
        fetch_ok, fetch_нп = _по_доступности(fetch_json, sources)
    найдены = [s for s in сигналы if s["статус"] == "найден"]
    не_проверены = [s for s in сигналы if s["статус"] == "не проверен"]
    dk_нп = [s["id"] for s in не_проверены if s["id"] in dk]
    состоялась = bool(fetch_ok) and not dk_нп
    источники_нп = [src for src in prof.get("источники_обязательные") or []
                    if _состояние_блока(fetch_json, src)[0] == "не проверено"]

    факты = [_факт(s) for s in sorted(
        сигналы, key=lambda r: (_ПОРЯДОК_ФАКТОВ.get(r["статус"], 9),
                                -_РАНГ.get(sev(r), 0), r["id"]))]
    поднят = []
    светофор, рекомендация = None, None
    if prof.get("формулировка") == "факты":
        рекомендация = None
    else:
        красные = sorted([s for s in найдены if sev(s) == "🔴"], key=lambda r: r["id"])
        жёлтые = sorted([s for s in найдены if sev(s) == "🟡"], key=lambda r: r["id"])
        if красные:
            светофор, поднят = "🔴", красные
        elif not состоялась:
            светофор, поднят = None, жёлтые
        elif жёлтые:
            светофор, поднят = "🟡", жёлтые
        elif источники_нп:
            # 🟢 обещает «крупных исков и долгов не найдено» — при непроверенных
            # судах/ФССП это утверждение о том, чего не смотрели. Жёлтый и красный
            # выдаются (только вверх), зелёный — нет.
            светофор = None
        else:
            светофор = "🟢"
        rec = prof.get("рекомендации") or {}
        if светофор is None and состоялась:
            рекомендация = ("🟢 не выдаётся: стоп-сигналов не найдено, но из обязательных "
                            "источников профиля не проверено — %s. Добрать по каскаду "
                            "(SKILL.md, шаг 4) и повторить." % ", ".join(источники_нп))
        elif светофор is None:
            что = dk_нп or fetch_нп or источники_нп
            рекомендация = ("проверка не состоялась: не проверены deal-killer-сигналы/источники — %s. "
                            "Светофор не выдаётся; добрать по каскаду (SKILL.md, шаг 4) и повторить."
                            % (", ".join(что) if что else "см. «не проверено» в фактах"))
        else:
            рекомендация = rec.get(светофор) or ""
            if not состоялась:
                рекомендация += " Проверка неполная: не проверено — %s." % ", ".join(
                    dk_нп or fetch_нп or источники_нп)
            elif источники_нп:
                рекомендация += " Из обязательных источников профиля не проверено: %s." % ", ".join(
                    источники_нп)
    out = {
        "профиль": profile_id,
        "вопрос": prof.get("вопрос"),
        "формулировка": prof.get("формулировка"),
        "светофор": светофор,
        "поднят_сигналами": [{"сигнал": s["id"], "severity": sev(s), "описание": s["описание"],
                              "источник": s["источник"], "дата": s["дата"], "значение": s["значение"]}
                             for s in поднят],
        "рекомендация": рекомендация,
        "факты": факты,
        "не_проверено_deal_killer": dk_нп,
        "проверка_состоялась": состоялась,
        "источники_обязательные_не_проверены": источники_нп,
        "вручную": list(prof.get("вручную") or []),
    }
    if prof.get("формулировка") == "факты":
        out["подсказка"] = prof.get("подсказка")
    if isinstance(итог, dict) and итог.get("вывод"):
        out["итог_fetch"] = итог.get("вывод")
    return out


# --- CLI ---------------------------------------------------------------------

def _таблица(profiles):
    rows = [("id", "вопрос", "формулировка", "deal-killers", "обязательные источники")]
    default = profiles.get("по_умолчанию")
    for pid, p in profiles["профили"].items():
        rows.append((pid + (" (по умолчанию)" if pid == default else ""), p.get("вопрос", ""),
                     p.get("формулировка", ""), str(len(p.get("deal_killers") or [])),
                     ", ".join(p.get("источники_обязательные") or [])))
    w = [max(len(r[i]) for r in rows) for i in range(4)]
    lines = []
    for i, r in enumerate(rows):
        lines.append("  ".join(r[j].ljust(w[j]) for j in range(4)) + "  " + r[4])
        if i == 0:
            lines.append("-" * (sum(w) + 8 + 30))
    return "\n".join(lines)


def _читать_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def main(argv):
    args = argv[1:]
    if not args or "-h" in args or "--help" in args:
        sys.stderr.write(__doc__)
        return 2
    opts = {"--профиль": None, "--fetch": None, "--fin": None, "--профили": None}
    flags = set()
    i = 0
    while i < len(args):
        a = args[i]
        if a in opts:
            if i + 1 >= len(args):
                sys.stderr.write("нет значения для %s\n" % a)
                return 2
            opts[a] = args[i + 1]
            i += 2
        elif a in ("--list", "--validate"):
            flags.add(a)
            i += 1
        else:
            sys.stderr.write("неизвестный аргумент %s\n" % a)
            return 2
    try:
        profiles = load(opts["--профили"])
    except ProfilesError as e:
        sys.stdout.write(json.dumps(_не_проверено(opts["--профиль"], str(e)),
                                    ensure_ascii=False, indent=2) + "\n")
        return 1
    if "--validate" in flags:
        errs = validate(profiles, _sources())
        if errs:
            sys.stderr.write("\n".join(errs) + "\n")
            return 1
        sys.stdout.write("профили ok: %d, сигналов: %d\n" % (len(profiles["профили"]),
                                                             len(profiles["сигналы"])))
        return 0
    if "--list" in flags:
        sys.stdout.write(_таблица(profiles) + "\n")
        return 0
    if not opts["--fetch"]:
        sys.stderr.write("нужен --fetch a.json (вывод fetch_counterparty.py); см. --help\n")
        return 2
    pid = opts["--профиль"] or profiles.get("по_умолчанию", "нейтрально")
    try:
        fetch_json = _читать_json(opts["--fetch"])
    except (OSError, ValueError) as e:
        sys.stdout.write(json.dumps(_не_проверено(pid, "fetch-JSON не читается: %s" % e),
                                    ensure_ascii=False, indent=2) + "\n")
        return 1
    fin_json = None
    if opts["--fin"]:
        try:
            fin_json = _читать_json(opts["--fin"])
        except (OSError, ValueError) as e:
            fin_json = {"статус": "не проверено", "причина": "fin-JSON не читается: %s" % e}
    out = resolve(fetch_json, fin_json, pid, profiles)
    sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
    return 1 if out.get("статус") == "не проверено" else 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception as e:  # никогда не traceback'ом
        sys.stdout.write(json.dumps(_не_проверено(None, "внутренняя ошибка: %s: %s"
                                                  % (type(e).__name__, e)),
                                    ensure_ascii=False, indent=2) + "\n")
        sys.exit(1)
