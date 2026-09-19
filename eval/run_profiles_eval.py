#!/usr/bin/env python3
"""
run_profiles_eval.py — офлайн-eval профилей цели (волна 1, поток E).

Синтетические входы: fetch-JSON нового формата (словари `_доступность` и блок
`_итог_проверки` по спеку §1.1/§1.3) + fin-JSON в формате fin_scoring.py →
ожидаемый светофор/рекомендация по профилям из data/profiles_ru.json.

Кейсы:
  (а) нейтрально → светофор null, список фактов;
  (б) отсрочка + ча_отрицательные_2года → 🔴;
  (в) доля + тот же флаг → не выше 🟡, в рекомендации «аргумент для переговоров»;
  (г) проверка_состоялась=false → светофор null и «проверка не состоялась» в ЛЮБОМ профиле;
  (д) клиент_115фз + санкции «не проверено» → deal-killer не проверен, проверка не состоялась;
  (е) валидация: все PROFILE_IDS есть в файле, источники_обязательные ⊆ SOURCES,
      id сигналов — из единого каталога, флаги fin_scoring покрыты каталогом;
  (ж) сигнал из блока «не проверено» → статус «не проверен», а не «отсутствует»;
  (з) CLI: --list и --профиль не падают traceback'ом на битом входе.

PASS/FAIL, чистый stdlib, сети не требует — гоняется в CI.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
PROFILES_JSON = ROOT / "data" / "profiles_ru.json"

ДАТА = "2026-09-19"

# id флагов fin_scoring.py (синхронно с eval/run_fin_eval.py) — каталог обязан их знать
FIN_FLAG_IDS = ("ча_отрицательные_2года", "ча_ниже_уставного_капитала",
                "коэффициент_автономии", "текущая_ликвидность", "падение_выручки",
                "убыток_2года", "транзитный_профиль")


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check(errors, cond, msg):
    if not cond:
        errors.append(msg)


# --- синтетические входы -----------------------------------------------------

def _av(state, reason=None, tier="🟢", требует="сеть"):
    """Словарь `_доступность[<id>]` по спеку §1.1."""
    return {"состояние": state, "причина": reason,
            "канарейка": "не запускалась", "tier": tier, "требует": требует,
            "дата": ДАТА}


def fetch_clean(**overrides):
    """Чистая действующая компания: все deal-killer-источники проверены, сигналов нет."""
    доступность = {
        "егрюл": _av("ok"), "риски": _av("ok"), "финансы": _av("ok"),
        "мсп": _av("ok"), "нпд": _av("пусто"), "спецреестры": _av("ok"),
        "еркнм": _av("пусто", tier="🟡", требует="кэш"),
        "рнп": _av("пусто", tier="🟡", требует="кэш"),
        "санкции": _av("ok", tier="🟡", требует="кэш"),
        "фссп": _av("ok", tier="🔴", требует="браузер"),
        "суды": _av("ok", tier="🔴", требует="браузер"),
        "банкротство": _av("ok", tier="🔴", требует="браузер"),
    }
    data = {
        "инн": "7707083893", "тип": "юрлицо",
        "егрюл": {"наименование_полное": "ООО «Тест»", "огрн": "1027700000001",
                  "руководитель": "Иванов Иван Иванович",
                  "дата_регистрации": "12.03.2015", "дата_прекращения": None,
                  "статус": "действующее", "адрес": None,
                  "адрес_источник": "не отдаётся egrul search; см. pb/выписка"},
        "риски": {"статус": "действующее", "налоговая_задолженность": False,
                  "дисквалификация_руководителя": False, "массовый_адрес": False,
                  "массовый_руководитель": False, "недостоверность_сведений": False,
                  "численность_сотрудников": 12, "спецрежим": "УСН"},
        "финансы": {"отчётность_по_годам": []},
        "мсп": {"статус_мсп": "в реестре", "категория": "малое предприятие"},
        "нпд": None,
        "спецреестры": {"дисквалификация_руководителя": False,
                        "налоговая_задолженность": False,
                        "недостоверность_сведений": False},
        "еркнм": None, "рнп": None,
        "санкции": {"статус": "проверено", "совпадений": 0, "совпадения": [],
                    "дата_проверки": ДАТА},
        "фссп": {"статус": "проверено", "производств": 0, "сумма": 0, "крупные": False},
        "суды": {"статус": "проверено", "ответчик_дел": 0, "ответчик_сумма": 0,
                 "ответчик_крупные": False},
        "банкротство": {"статус": "проверено", "процедура": False},
        "_доступность": доступность,
        "_итог_проверки": {
            "источников": 12, "ok": 9, "пусто": 3, "не_проверено": 0,
            "deal_killer_источники": ["егрюл", "риски", "спецреестры", "фссп",
                                      "суды", "банкротство", "санкции"],
            "deal_killer_не_проверено": [],
            "проверка_состоялась": True,
            "вывод": "проверка состоялась: 7 из 7 deal-killer-источников проверено",
        },
    }
    data.update(overrides)
    return data


def fin_ok(*flag_ids):
    """fin-JSON в формате fin_scoring.py с указанными флагами."""
    sev = {"ча_отрицательные_2года": "🔴"}
    return {
        "статус": "ок",
        "флаги": [{"id": f, "severity": sev.get(f, "🟡"),
                   "показатель": f, "значение": -1.0, "порог": "тест",
                   "пояснение": "синтетический флаг", "источник": "ГИР БО",
                   "год": "2024–2025", "строки_баланса": {}} for f in flag_ids],
        "финансовый_профиль": {"сводка": "тест", "не_проверено": [],
                               "строки_баланса": {}},
    }


def fin_not_checked():
    return {"статус": "не проверено", "причина": "ГИР БО не отдал отчётность",
            "источник": "ГИР БО"}


# --- кейсы -------------------------------------------------------------------

def case_a_neutral(pf):
    errors = []
    res = pf.resolve(fetch_clean(), fin_ok(), "нейтрально")
    check(errors, res.get("профиль") == "нейтрально", "профиль %r" % res.get("профиль"))
    check(errors, res.get("светофор") is None,
          "нейтрально: светофор должен быть null, есть %r" % res.get("светофор"))
    check(errors, res.get("рекомендация") is None,
          "нейтрально: рекомендация должна быть null, есть %r" % res.get("рекомендация"))
    факты = res.get("факты")
    check(errors, isinstance(факты, list) and факты, "нейтрально: нет списка фактов")
    if isinstance(факты, list) and факты:
        f0 = факты[0]
        check(errors, isinstance(f0, dict) and "источник" in f0 and "дата" in f0,
              "факт без источника/даты: %r" % (f0,))
        check(errors, any(f.get("дата") == ДАТА for f in факты),
              "ни один факт не датирован из _доступность")
    check(errors, res.get("проверка_состоялась") is True,
          "нейтрально: проверка_состоялась %r" % res.get("проверка_состоялась"))
    # даже с 🔴-флагом нейтральный профиль не выносит вердикт
    res2 = pf.resolve(fetch_clean(), fin_ok("ча_отрицательные_2года"), "нейтрально")
    check(errors, res2.get("светофор") is None,
          "нейтрально с 🔴-флагом: светофор %r, ожидался null" % res2.get("светофор"))
    check(errors, any(f.get("сигнал") == "ча_отрицательные_2года"
                      and f.get("статус") == "найден" for f in res2.get("факты", [])),
          "нейтрально: 🔴-флаг должен быть в фактах как найденный")
    # фрейм отсрочки не протекает в нейтральную карточку (факты и рекомендация);
    # перечисление id профилей в подсказке «уточните цель» — не фрейм
    тексты = " ".join(str(f.get("текст", "")) for f in res2.get("факты", [])).lower()
    check(errors, "отсрочк" not in тексты and "предоплат" not in тексты,
          "нейтрально: фраза про отсрочку/предоплату протекла в факты")
    check(errors, res2.get("рекомендация") is None and res2.get("поднят_сигналами") == [],
          "нейтрально: вердикт-поля должны быть пустыми")
    return errors


def case_b_otsrochka_red(pf):
    errors = []
    res = pf.resolve(fetch_clean(), fin_ok("ча_отрицательные_2года"), "отсрочка")
    check(errors, res.get("светофор") == "🔴",
          "отсрочка + ЧА<0 два года: светофор %r, ожидался 🔴" % res.get("светофор"))
    поднят = [s.get("сигнал") for s in res.get("поднят_сигналами", [])]
    check(errors, "ча_отрицательные_2года" in поднят,
          "отсрочка: в «поднят_сигналами» нет ча_отрицательные_2года: %r" % поднят)
    rec = (res.get("рекомендация") or "").lower()
    check(errors, "предоплат" in rec or "отсрочк" in rec,
          "отсрочка 🔴: рекомендация не про условия оплаты: %r" % res.get("рекомендация"))
    check(errors, res.get("проверка_состоялась") is True,
          "отсрочка: проверка_состоялась %r" % res.get("проверка_состоялась"))
    # чистая компания на отсрочке → 🟢 с рекомендацией профиля
    res_g = pf.resolve(fetch_clean(), fin_ok(), "отсрочка")
    check(errors, res_g.get("светофор") == "🟢",
          "отсрочка чистая: светофор %r, ожидался 🟢" % res_g.get("светофор"))
    check(errors, bool(res_g.get("рекомендация")), "отсрочка 🟢: пустая рекомендация")
    # 🟡-сигнал → 🟡, не выше
    res_y = pf.resolve(fetch_clean(), fin_ok("текущая_ликвидность"), "отсрочка")
    check(errors, res_y.get("светофор") == "🟡",
          "отсрочка + ликвидность<1: светофор %r, ожидался 🟡" % res_y.get("светофор"))
    return errors


def case_c_dolya_yellow(pf):
    errors = []
    res = pf.resolve(fetch_clean(), fin_ok("ча_отрицательные_2года"), "доля")
    check(errors, res.get("светофор") in ("🟡", "🟢"),
          "доля + ЧА<0 два года: светофор %r, ожидался не выше 🟡" % res.get("светофор"))
    check(errors, res.get("светофор") == "🟡",
          "доля + ЧА<0: ожидался ровно 🟡 (аргумент в торге), есть %r" % res.get("светофор"))
    rec = res.get("рекомендация") or ""
    check(errors, "аргумент для переговоров" in rec.lower(),
          "доля 🟡: нет формулировки «аргумент для переговоров»: %r" % rec)
    check(errors, "отсрочк" not in rec.lower(),
          "доля: фраза про отсрочку протекла в рекомендацию: %r" % rec)
    # но ликвидация — deal-killer и для доли
    fetch = fetch_clean()
    fetch["егрюл"]["статус"] = "в стадии ликвидации"
    res_r = pf.resolve(fetch, fin_ok(), "доля")
    check(errors, res_r.get("светофор") == "🔴",
          "доля + ликвидация: светофор %r, ожидался 🔴" % res_r.get("светофор"))
    return errors


def case_d_not_happened(pf, profiles):
    errors = []
    fetch = fetch_clean()
    fetch["_итог_проверки"]["проверка_состоялась"] = False
    fetch["_итог_проверки"]["deal_killer_не_проверено"] = ["фссп", "суды", "банкротство",
                                                           "спецреестры"]
    for src in ("фссп", "суды", "банкротство", "спецреестры"):
        fetch["_доступность"][src] = _av("не проверено", "не покрыто: нужен браузер")
        fetch[src] = None
    for pid in profiles["профили"]:
        res = pf.resolve(fetch, fin_ok(), pid)
        check(errors, res.get("светофор") is None,
              "%s: при проверка_состоялась=false светофор %r, ожидался null"
              % (pid, res.get("светофор")))
        check(errors, res.get("проверка_состоялась") is False,
              "%s: проверка_состоялась должна быть false" % pid)
        # у профилей без вердикта (нейтрально, ндс_вычет) рекомендации нет вовсе
        if profiles["профили"][pid].get("формулировка") != "факты":
            rec = res.get("рекомендация") or ""
            check(errors, "проверка не состоялась" in rec.lower(),
                  "%s: рекомендация без «проверка не состоялась»: %r" % (pid, rec))
    # найденный deal-killer поднимает 🔴 даже при неполной проверке (только вверх)
    fetch2 = json.loads(json.dumps(fetch))
    fetch2["егрюл"]["статус"] = "в стадии ликвидации"
    res2 = pf.resolve(fetch2, fin_ok(), "отсрочка")
    check(errors, res2.get("светофор") == "🔴",
          "отсрочка, ликвидация при неполной проверке: светофор %r, ожидался 🔴"
          % res2.get("светофор"))
    return errors


def case_e_115fz_sanctions_unchecked(pf):
    errors = []
    fetch = fetch_clean()
    fetch["_доступность"]["санкции"] = _av("не проверено", "кэш: перечни не скачаны",
                                           tier="🟡", требует="кэш")
    fetch["санкции"] = None
    # по правилу fetch (1 из 7 не проверено) проверка формально состоялась —
    # но для профиля 115-ФЗ РФМ — главный deal-killer, без него вердикта нет
    fetch["_итог_проверки"]["deal_killer_не_проверено"] = ["санкции"]
    fetch["_итог_проверки"]["не_проверено"] = 1
    res = pf.resolve(fetch, None, "клиент_115фз")
    нп = res.get("не_проверено_deal_killer") or []
    check(errors, "рфм_совпадение" in нп,
          "клиент_115фз: рфм_совпадение должен быть в не_проверено_deal_killer: %r" % нп)
    check(errors, res.get("проверка_состоялась") is False,
          "клиент_115фз: проверка_состоялась %r, ожидалось false"
          % res.get("проверка_состоялась"))
    check(errors, res.get("светофор") is None,
          "клиент_115фз: светофор %r, ожидался null" % res.get("светофор"))
    rec = res.get("рекомендация") or ""
    check(errors, "проверка не состоялась" in rec.lower(),
          "клиент_115фз: рекомендация без «проверка не состоялась»: %r" % rec)
    # с проверенными перечнями и совпадением → 🔴
    fetch_hit = fetch_clean()
    fetch_hit["санкции"]["совпадений"] = 1
    fetch_hit["санкции"]["совпадения"] = [{"список": "Росфинмониторинг",
                                           "совпадение": "по ИНН (точное)"}]
    res_hit = pf.resolve(fetch_hit, None, "клиент_115фз")
    check(errors, res_hit.get("светофор") == "🔴",
          "клиент_115фз + совпадение РФМ: светофор %r, ожидался 🔴" % res_hit.get("светофор"))
    # без fin-JSON профиль 115-ФЗ не требует финансов и выносит вердикт
    res_clean = pf.resolve(fetch_clean(), None, "клиент_115фз")
    check(errors, res_clean.get("светофор") == "🟢",
          "клиент_115фз чистый без fin: светофор %r, ожидался 🟢" % res_clean.get("светофор"))
    return errors


def case_f_validation(pf, profiles, sources):
    errors = []
    errs = pf.validate(profiles, sources)
    check(errors, errs == [], "validate() нашёл ошибки: %s" % "; ".join(errs))
    ids = set(profiles.get("профили", {}))
    for pid in sources.PROFILE_IDS:
        check(errors, pid in ids, "профиль %s из sources.PROFILE_IDS отсутствует в файле" % pid)
    check(errors, ids <= set(sources.PROFILE_IDS),
          "в файле лишние профили: %s" % sorted(ids - set(sources.PROFILE_IDS)))
    каталог = profiles.get("сигналы", {})
    for fid in FIN_FLAG_IDS:
        check(errors, fid in каталог, "флаг fin_scoring %s не описан в каталоге сигналов" % fid)
    for pid, p in profiles.get("профили", {}).items():
        for src in p.get("источники_обязательные", []):
            check(errors, src in sources.SOURCES,
                  "%s: источник %s не существует в sources.SOURCES" % (pid, src))
            check(errors, src in sources.sources_for_profile(pid),
                  "%s: источник %s обязателен, но реестр его для профиля не собирает"
                  % (pid, src))
        for sid in p.get("deal_killers", []):
            check(errors, sid in каталог, "%s: deal_killer %s не из каталога" % (pid, sid))
        for sid in p.get("severity_override", {}):
            check(errors, sid in каталог, "%s: severity_override %s не из каталога" % (pid, sid))
        if pid == "нейтрально":
            check(errors, p.get("рекомендации") is None and p.get("формулировка") == "факты",
                  "нейтрально: рекомендации должны быть null, формулировка «факты»")
        elif p.get("формулировка") == "факты":
            # профиль без вердикта (нейтрально, ндс_вычет): рекомендация была бы
            # заключением, которого инструмент не выносит
            check(errors, p.get("рекомендации") is None,
                  "%s: у профиля «факты» рекомендации должны быть null" % pid)
        else:
            check(errors, isinstance(p.get("рекомендации"), dict)
                  and set(p["рекомендации"]) == {"🟢", "🟡", "🔴"},
                  "%s: рекомендации должны содержать ровно 🟢/🟡/🔴" % pid)
    check(errors, profiles.get("по_умолчанию") == "нейтрально",
          "по умолчанию должен быть профиль «нейтрально»")
    # намеренно битый профиль → validate обязан ругаться
    сломан = json.loads(json.dumps(profiles))
    сломан["профили"]["отсрочка"]["источники_обязательные"].append("несуществующий")
    сломан["профили"]["отсрочка"]["deal_killers"].append("нет_такого_сигнала")
    errs2 = pf.validate(сломан, sources)
    check(errors, len(errs2) >= 2, "validate() не заметил битый профиль: %r" % errs2)
    # sources_for отдаёт список обязательных источников профиля
    check(errors, pf.sources_for("отсрочка") == profiles["профили"]["отсрочка"]["источники_обязательные"],
          "sources_for('отсрочка') не совпадает с файлом")
    check(errors, pf.sources_for("нет_такого") == [], "sources_for неизвестного профиля должен дать []")
    return errors


def case_g_not_checked_vs_absent(pf):
    errors = []
    fetch = fetch_clean()
    fetch["_доступность"]["риски"] = _av("не проверено", "гео: 403 с не-РФ IP")
    fetch["риски"] = None
    fetch["_доступность"]["спецреестры"] = _av("не проверено", "схема: не найдено поле t")
    fetch["спецреестры"] = None
    сигналы = {s["id"]: s for s in pf.extract_signals(fetch, fin_ok())}
    check(errors, сигналы.get("недостоверность_сведений", {}).get("статус") == "не проверен",
          "недостоверность из блока «не проверено» должна быть «не проверен», есть %r"
          % сигналы.get("недостоверность_сведений", {}).get("статус"))
    check(errors, "гео" in (сигналы.get("недостоверность_сведений", {}).get("причина") or ""),
          "у непроверенного сигнала нет причины из _доступность")
    check(errors, сигналы.get("ликвидация", {}).get("статус") == "отсутствует",
          "ликвидация при ok-ЕГРЮЛ должна быть «отсутствует», есть %r"
          % сигналы.get("ликвидация", {}).get("статус"))
    check(errors, сигналы.get("ча_отрицательные_2года", {}).get("статус") == "отсутствует",
          "fin-флаг без срабатывания должен быть «отсутствует»")
    # fin «не проверено» → fin-сигналы «не проверен»
    сигналы2 = {s["id"]: s for s in pf.extract_signals(fetch_clean(), fin_not_checked())}
    check(errors, сигналы2.get("ча_отрицательные_2года", {}).get("статус") == "не проверен",
          "fin «не проверено» должен давать «не проверен», есть %r"
          % сигналы2.get("ча_отрицательные_2года", {}).get("статус"))
    # старый формат _доступность (строка) не роняет экстрактор
    fetch_old = fetch_clean()
    fetch_old["_доступность"] = {k: "ok" for k in fetch_old["_доступность"]}
    fetch_old["_доступность"]["риски"] = "недоступен (URLError)"
    del fetch_old["_итог_проверки"]
    сигналы3 = {s["id"]: s for s in pf.extract_signals(fetch_old, fin_ok())}
    check(errors, сигналы3.get("массовый_адрес", {}).get("статус") == "не проверен",
          "строковая _доступность «недоступен» должна давать «не проверен»")
    # найденный сигнал несёт источник и дату
    fetch_hit = fetch_clean()
    fetch_hit["риски"]["массовый_адрес"] = True
    сигналы4 = {s["id"]: s for s in pf.extract_signals(fetch_hit, fin_ok())}
    m = сигналы4.get("массовый_адрес", {})
    check(errors, m.get("статус") == "найден" and m.get("дата") == ДАТА and m.get("источник"),
          "найденный массовый_адрес без источника/даты: %r" % m)
    # ИП: адресные сигналы и финансы «не применимо», а не «не проверен»
    fetch_ip = fetch_clean(инн="504110181262", тип="ип")
    сигналы5 = {s["id"]: s for s in pf.extract_signals(fetch_ip, fin_not_checked())}
    check(errors, сигналы5.get("массовый_адрес", {}).get("статус") == "не применимо",
          "ИП: массовый_адрес должен быть «не применимо», есть %r"
          % сигналы5.get("массовый_адрес", {}).get("статус"))
    check(errors, сигналы5.get("ча_отрицательные_2года", {}).get("статус") == "не применимо",
          "ИП: fin-сигнал должен быть «не применимо»")
    res_ip = pf.resolve(fetch_ip, None, "отсрочка")
    check(errors, res_ip.get("светофор") == "🟢",
          "ИП чистый на отсрочке: «не применимо» не должно блокировать вердикт, есть %r"
          % res_ip.get("светофор"))
    return errors


def case_h_cli(tmp):
    errors = []
    script = SCRIPTS / "profiles.py"
    p = subprocess.run([sys.executable, str(script), "--list"],
                       capture_output=True, text=True, check=False)
    check(errors, p.returncode == 0, "--list: exit %s, stderr %s" % (p.returncode, p.stderr[-300:]))
    check(errors, "нейтрально" in p.stdout and "клиент_115фз" in p.stdout,
          "--list: нет таблицы профилей")
    check(errors, "Traceback" not in p.stderr, "--list: traceback в stderr")
    # нормальный прогон
    fpath = tmp / "fetch.json"
    fipath = tmp / "fin.json"
    fpath.write_text(json.dumps(fetch_clean(), ensure_ascii=False), encoding="utf-8")
    fipath.write_text(json.dumps(fin_ok("ча_отрицательные_2года"), ensure_ascii=False),
                      encoding="utf-8")
    p2 = subprocess.run([sys.executable, str(script), "--профиль", "отсрочка",
                         "--fetch", str(fpath), "--fin", str(fipath)],
                        capture_output=True, text=True, check=False)
    check(errors, p2.returncode == 0, "CLI: exit %s, stderr %s" % (p2.returncode, p2.stderr[-300:]))
    try:
        out = json.loads(p2.stdout)
        check(errors, out.get("светофор") == "🔴", "CLI: светофор %r" % out.get("светофор"))
    except ValueError:
        errors.append("CLI: вывод не JSON: %.200s" % p2.stdout)
    # битый вход / неизвестный профиль — JSON «не проверено», не traceback
    bad = tmp / "bad.json"
    bad.write_text("{не json", encoding="utf-8")
    p3 = subprocess.run([sys.executable, str(script), "--профиль", "отсрочка",
                         "--fetch", str(bad)], capture_output=True, text=True, check=False)
    check(errors, "Traceback" not in p3.stderr, "битый JSON: traceback")
    try:
        out3 = json.loads(p3.stdout)
        check(errors, out3.get("светофор") is None and "не проверено" in json.dumps(out3, ensure_ascii=False),
              "битый JSON: ожидался JSON со статусом «не проверено»: %r" % out3)
    except ValueError:
        errors.append("битый JSON: вывод не JSON: %.200s" % p3.stdout)
    p4 = subprocess.run([sys.executable, str(script), "--профиль", "нет_такого",
                         "--fetch", str(fpath)], capture_output=True, text=True, check=False)
    check(errors, "Traceback" not in p4.stderr and p4.returncode != 0,
          "неизвестный профиль: должен быть ненулевой exit без traceback")
    # без --fin финансы считаются fin_scoring'ом из fetch (здесь пусто → «не проверен»)
    p5 = subprocess.run([sys.executable, str(script), "--профиль", "нейтрально",
                         "--fetch", str(fpath)], capture_output=True, text=True, check=False)
    check(errors, p5.returncode == 0 and "Traceback" not in p5.stderr,
          "без --fin: exit %s, stderr %s" % (p5.returncode, p5.stderr[-300:]))
    return errors


def main():
    import tempfile
    pf = load_module("profiles", SCRIPTS / "profiles.py")
    sources = load_module("sources", SCRIPTS / "sources.py")
    profiles = pf.load(PROFILES_JSON)
    with tempfile.TemporaryDirectory() as td:
        cases = {
            "а-нейтрально-факты": case_a_neutral(pf),
            "б-отсрочка-🔴": case_b_otsrochka_red(pf),
            "в-доля-🟡-аргумент": case_c_dolya_yellow(pf),
            "г-проверка-не-состоялась": case_d_not_happened(pf, profiles),
            "д-115фз-санкции-не-проверены": case_e_115fz_sanctions_unchecked(pf),
            "е-валидация-профилей": case_f_validation(pf, profiles, sources),
            "ж-не-проверен-vs-отсутствует": case_g_not_checked_vs_absent(pf),
            "з-cli": case_h_cli(Path(td)),
        }
    failed = 0
    for name, errors in cases.items():
        if errors:
            failed += 1
            print("FAIL %s" % name)
            for e in errors:
                print("  - %s" % e)
        else:
            print("PASS %s" % name)
    if failed:
        print("FAIL: %d/%d кейсов упало" % (failed, len(cases)))
        return 1
    print("PASS: все %d кейсов зелёные" % len(cases))
    return 0


if __name__ == "__main__":
    sys.exit(main())
