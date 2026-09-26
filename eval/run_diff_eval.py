#!/usr/bin/env python3
"""
run_diff_eval.py — офлайн-прогон diff_counterparty.py по парам снимков-фикстур.

Сверяет ожидаемые изменения (поле + severity) с фактическими, PASS/FAIL.
Плюс кейсы отпечатков (спек волны 2, §3.1): один и тот же переход, поданный
полными снимками и отпечатками scripts/snapshot.py, даёт одинаковый дифф —
включая смешанную пару «полный снимок ↔ отпечаток»; и отдельный кейс на ℹ️
«изменилось что-то вне отслеживаемого» (те же поля, другой хеш_остального).

Кейсы против тихой деградации (фикстуры states_*, fin_same_*, mode_*):
  * источник ушёл в «не проверено» — отдельная запись, поля источника в
    сравнение НЕ идут (никаких «статус → None» и «отметка о недостоверности
    снята», когда источник просто промолчал);
  * обратный переход «не проверено → ok» — тоже запись, значения не сравниваются;
  * ухудшение ЧА/выручки внутри уже известного года (пересдача форм) замечено;
  * снимки, собранные разными режимами/профилями, помечены как несравнимые;
  * карточка --human при молчащих deal-killer-источниках не выдаёт 🟢.

Чистый python3 stdlib, сети не требует — гоняется в CI.
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIFF = ROOT / "scripts" / "diff_counterparty.py"
SNAPSHOT = ROOT / "scripts" / "snapshot.py"
FIXTURES = ROOT / "eval" / "fixtures" / "snapshots"

# Пара снимков -> (ожидаемые изменения {поле: severity}, запрещённые поля,
# ожидаемый статус).
CASES = {
    ("director_a.json", "director_b.json"): (
        {"руководитель": "🟡"},
        ["статус", "адрес", "недостоверность_сведений"],
        "сравнено",
    ),
    ("status_a.json", "status_b.json"): (
        {"статус": "🔴", "дата_прекращения": "🔴",
         "чистые_активы": "🔴", "выручка": "🟡"},
        ["руководитель"],
        "сравнено",
    ),
    ("same_a.json", "same_b.json"): (
        {},
        ["статус", "руководитель", "адрес", "чистые_активы"],
        "изменений нет",
    ),
    # Дефект: источник замолчал, а дифф читал его молчание как значения.
    ("states_a.json", "states_b.json"): (
        {"источник.егрюл": "🟡", "источник.риски": "🟡"},
        ["статус", "руководитель", "адрес", "недостоверность_сведений",
         "налоговая_задолженность"],
        "проверка не состоялась",
    ),
    # Обратный переход: источник снова отвечает — запись есть, значения ждут.
    ("states_b.json", "states_a.json"): (
        {"источник.егрюл": "ℹ️", "источник.риски": "ℹ️"},
        ["статус", "руководитель", "адрес", "недостоверность_сведений"],
        "сравнено",
    ),
    # Все источники на месте — правила по значениям работают как раньше.
    ("states_a.json", "states_c.json"): (
        {"статус": "🔴", "дата_прекращения": "🔴",
         "чистые_активы": "🔴", "выручка": "🟡"},
        ["руководитель", "источник.егрюл", "источник.риски"],
        "сравнено",
    ),
    # Блок ЕГРЮЛ «ok», но его статус и дата прекращения приходят из pb: молчащий
    # pb обнуляет их, и раньше это читалось как «статус → None».
    ("cross_a.json", "cross_b.json"): (
        {"источник.риски": "🟡"},
        ["статус", "дата_прекращения", "недостоверность_сведений",
         "источник.егрюл"],
        "проверка не состоялась",
    ),
    # Дефект: ухудшение внутри уже известного года (формы пересданы) молчало.
    ("fin_same_a.json", "fin_same_b.json"): (
        {"чистые_активы": "🔴", "выручка": "🟡"},
        ["статус", "руководитель", "источник.финансы", "финансы_новый_год"],
        "сравнено",
    ),
    # Дефект: снимки разных режимов/профилей сравнивались как равные.
    ("mode_a.json", "mode_b.json"): (
        {"условия_сбора": "ℹ️"},
        ["статус", "руководитель", "чистые_активы"],
        "сравнено",
    ),
}


def run_case(pair, expected, forbidden, expected_status):
    a, b = (str(FIXTURES / name) for name in pair)
    proc = subprocess.run(
        [sys.executable, str(DIFF), "--a", a, "--b", b],
        capture_output=True, text=True, check=False,
    )
    errors = []
    if proc.returncode != 0:
        return ["exit code %s, stderr: %s" % (proc.returncode, proc.stderr.strip())]
    try:
        out = json.loads(proc.stdout)
    except ValueError:
        return ["вывод не JSON: %.200s" % proc.stdout]
    if out.get("статус") != expected_status:
        errors.append("статус %r != %r" % (out.get("статус"), expected_status))
    изменения = {c["поле"]: c for c in out.get("изменения", [])}
    for поле, sev in expected.items():
        got = изменения.get(поле)
        if got is None:
            errors.append("ожидалось изменение %s (%s), его нет" % (поле, sev))
        elif got.get("severity") != sev:
            errors.append("%s: severity %r != %r"
                          % (поле, got.get("severity"), sev))
        else:
            for ключ in ("было", "стало", "дата_было", "дата_стало"):
                if ключ not in got:
                    errors.append("%s: нет поля %r" % (поле, ключ))
    for поле in forbidden:
        if поле in изменения:
            errors.append("изменения %s быть не должно" % поле)
    return errors


def _дифф(path_a, path_b):
    """Сырой вывод diff_counterparty.py --a --b (dict) или None."""
    proc = subprocess.run(
        [sys.executable, str(DIFF), "--a", str(path_a), "--b", str(path_b)],
        capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return None


def _отпечаток(полный, куда):
    """Отпечаток полного снимка под тем же именем файла (даты в диффе берутся
    из имени файла — так два дифф-вывода сравнимы дословно)."""
    proc = subprocess.run(
        [sys.executable, str(SNAPSHOT), "--из", str(полный)],
        capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        return None, "snapshot.py код %s: %s" % (proc.returncode, proc.stderr.strip())
    куда.write_text(proc.stdout, encoding="utf-8")
    return куда, None


def case_отпечатки(pair, tmp):
    """Один и тот же переход: полные снимки vs отпечатки vs смешанная пара."""
    полный_a, полный_b = (FIXTURES / name for name in pair)
    эталон = _дифф(полный_a, полный_b)
    if эталон is None:
        return ["дифф по полным снимкам не отработал"]
    errors = []
    отп = {}
    for роль, полный in (("a", полный_a), ("b", полный_b)):
        каталог = tmp / ("отпечатки_" + роль)
        каталог.mkdir(parents=True, exist_ok=True)
        путь, err = _отпечаток(полный, каталог / полный.name)
        if err:
            return [err]
        отп[роль] = путь
    варианты = {
        "отпечаток ↔ отпечаток": (отп["a"], отп["b"]),
        "полный ↔ отпечаток": (полный_a, отп["b"]),
        "отпечаток ↔ полный": (отп["a"], полный_b),
    }
    for имя, (pa, pb) in варианты.items():
        факт = _дифф(pa, pb)
        if факт is None:
            errors.append("%s: дифф не отработал" % имя)
            continue
        if факт.get("статус") != эталон.get("статус"):
            errors.append("%s: статус %r != %r"
                          % (имя, факт.get("статус"), эталон.get("статус")))
        if факт.get("изменения") != эталон.get("изменения"):
            errors.append("%s: изменения расходятся с диффом по полным снимкам:\n"
                          "      эталон: %s\n      факт:   %s"
                          % (имя,
                             json.dumps(эталон.get("изменения"), ensure_ascii=False),
                             json.dumps(факт.get("изменения"), ensure_ascii=False)))
    return errors


def case_хеш(tmp):
    """Поля те же, изменилось что-то вне отслеживаемого → ровно одно ℹ️-изменение."""
    исходный = json.loads((FIXTURES / "same_a.json").read_text(encoding="utf-8"))
    изменённый = json.loads(json.dumps(исходный))
    изменённый["егрюл"]["наименование_краткое"] = "ООО «СТАБИЛЬНОСТЬ» (переименовано)"
    изменённый["егрюл"]["огрн"] = "1234567890123"
    каталог = tmp / "хеш"
    каталог.mkdir(parents=True, exist_ok=True)
    пути = []
    for имя, данные in (("hash_a.json", исходный), ("hash_b.json", изменённый)):
        полный = каталог / ("полный_" + имя)
        полный.write_text(json.dumps(данные, ensure_ascii=False), encoding="utf-8")
        путь, err = _отпечаток(полный, каталог / имя)
        if err:
            return [err]
        пути.append(путь)
    факт = _дифф(*пути)
    if факт is None:
        return ["дифф по отпечаткам не отработал"]
    изменения = факт.get("изменения") or []
    if len(изменения) != 1 or изменения[0].get("поле") != "хеш_остального":
        return ["ожидалось одно изменение «хеш_остального», получено: %s"
                % json.dumps(изменения, ensure_ascii=False)]
    if изменения[0].get("severity") != "ℹ️":
        return ["severity %r != 'ℹ️'" % изменения[0].get("severity")]
    # Сессионный токен источника меняется каждый запрос: он НЕ должен давать
    # ℹ️ «изменилось что-то вне отслеживаемого», иначе сигнал шумит на каждом
    # прогоне и его перестают читать.
    с_токеном = json.loads(json.dumps(исходный))
    с_токеном.setdefault("риски", {})["_token_pb"] = "AAAA1111"
    другой_токен = json.loads(json.dumps(с_токеном))
    другой_токен["риски"]["_token_pb"] = "BBBB2222"
    пути_токена = []
    for имя, данные in (("token_a.json", с_токеном), ("token_b.json", другой_токен)):
        полный = каталог / ("полный_" + имя)
        полный.write_text(json.dumps(данные, ensure_ascii=False), encoding="utf-8")
        путь, err = _отпечаток(полный, каталог / имя)
        if err:
            return [err]
        пути_токена.append(путь)
    факт_токен = _дифф(*пути_токена)
    if (факт_токен or {}).get("изменения"):
        return ["смена сессионного токена показана как изменение: %s"
                % json.dumps(факт_токен.get("изменения"), ensure_ascii=False)]

    # Контроль: у одинаковых снимков хеш совпадает и ℹ️ не появляется.
    одинаковые, err = _отпечаток(FIXTURES / "same_b.json", каталог / "hash_a.json.copy")
    if err:
        return [err]
    факт2 = _дифф(пути[0], одинаковые)
    if (факт2 or {}).get("изменения"):
        return ["одинаковые данные дали изменения: %s"
                % json.dumps(факт2.get("изменения"), ensure_ascii=False)]
    return []


def _карточка(a, b):
    """Текст `--human` по паре фикстур (или None)."""
    proc = subprocess.run(
        [sys.executable, str(DIFF), "--a", str(FIXTURES / a), "--b",
         str(FIXTURES / b), "--human"],
        capture_output=True, text=True, check=False)
    return proc.stdout if proc.returncode == 0 else None


КАРТОЧКИ = (
    # (пара, обязательные куски, запрещённые куски)
    (("states_a.json", "states_b.json"),
     ["⚠️ Не проверены deal-killer-источники", "егрюл", "риски"],
     ["\n🟢 ", "недостоверност"]),   # вердикт-строка не смеет быть зелёной
    (("states_silent.json", "states_silent.json"),
     ["⚪", "Проверка не состоялась"],
     ["\n🟢 "]),
    (("states_a.json", "states_a.json"),
     ["🟢 Изменений нет", "Скриптом не покрыты"],
     ["⚠️ Не проверены deal-killer-источники"]),
    # Старый полный снимок без _доступность: поведение прежнее, но карточка
    # честно говорит, что состояния источников неизвестны.
    (("same_a.json", "same_b.json"),
     ["🟢 Изменений нет", "Состояния источников в этих снимках неизвестны"],
     ["⚠️ Не проверены deal-killer-источники"]),
)


def case_карточка(пара, обязательные, запрещённые):
    текст = _карточка(*пара)
    if текст is None:
        return ["карточка --human не отработала"]
    errors = []
    for кусок in обязательные:
        if кусок not in текст:
            errors.append("в карточке нет %r" % кусок)
    for кусок in запрещённые:
        if кусок in текст:
            errors.append("в карточке есть запрещённое %r" % кусок)
    return errors


def case_не_покрыты():
    """«Не покрыт» судится по снимку: источник, ставший сетевым (ЕФРСБ в 1.13),
    не превращает старые снимки в «проверка не состоялась», а свежий сбой — ловится."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("diff_counterparty", DIFF)
    dc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dc)
    errors = []
    сост = {"егрюл": "ok", "банкротство": "не проверено"}
    база = {"версия_формата": 2, "состояния": сост, "_итог": {"проверка_состоялась": True}}
    старый = dict(база)
    итог = dc.итог_снимка(старый)
    if "банкротство" not in итог["не_покрыты_deal_killer"] or dc.провал_проверки(итог):
        errors.append("старый отпечаток без «не_покрыты»: ЕФРСБ должен быть пределом, "
                      "а не сбоем: %r" % итог)
    новый = dict(база, не_покрыты=[])
    итог = dc.итог_снимка(новый)
    if "банкротство" not in итог["не_ответили_deal_killer"] or not dc.провал_проверки(итог):
        errors.append("новый отпечаток, ЕФРСБ не ответил: должен быть сбой: %r" % итог)
    полный = {"_доступность": {
        "егрюл": {"состояние": "ok"},
        "банкротство": {"состояние": "не проверено", "требует": "сеть",
                        "причина": "антибот: ЕФРСБ ответил 403"}}}
    итог = dc.итог_снимка(полный)
    if итог["не_ответили_deal_killer"] != ["банкротство"]:
        errors.append("полный слепок с «антибот:» — не ответил: %r" % итог)
    # дифф ЕФРСБ: сравниваются номер дела и код стадии, не дата и не текст
    def дело(код, дата="2026-09-01", номер="А07-1/2026", стадия="Наблюдение"):
        return {"номер": номер, "стадия_код": код, "стадия": стадия, "дата_стадии": дата}

    def отп(д, сост_банкр="ok"):
        return {"версия_формата": 2, "поля": {"банкротство": д},
                "состояния": {"егрюл": "ok", "банкротство": сост_банкр},
                "не_покрыты": [], "_итог": {"проверка_состоялась": True}}

    def банкр(a, b):
        return [x for x in dc.diff(a, b, "d1", "d2") if x.get("поле") == "банкротство"]
    идёт = отп(дело("Watching"))
    for имя, a, b, ждём in (
            ("появилось дело", отп(None), идёт, "🔴"),
            ("сменилась стадия", идёт, отп(дело("Tender", стадия="Конкурсное")), "🔴"),
            ("прекращено", идёт, отп(дело("ProceedingsStopped", стадия="Прекращено")), "🟡"),
            ("дело пропало", идёт, отп(None), "ℹ️"),
            ("только дата", идёт, отп(дело("Watching", дата="2026-09-20")), None),
            ("только текст стадии", идёт, отп(дело("Watching", стадия="НАБЛЮДЕНИЕ")), None)):
        изм = банкр(a, b)
        got = изм[0].get("severity") if изм else None
        if got != ждём or len(изм) > 1:
            errors.append("дифф ЕФРСБ, %s: ждали %r, получили %r" % (имя, ждём, изм))
    старый = отп(None, "не проверено")
    старый.pop("не_покрыты")
    if банкр(старый, идёт):
        errors.append("старый снимок без ЕФРСБ: сравнение значения должно быть подавлено")
    полный["_доступность"]["банкротство"]["причина"] = "схема: выдача усечена (15 из 40)"
    итог = dc.итог_снимка(полный)
    if итог["не_ответили_deal_killer"] != ["банкротство"] or not dc.провал_проверки(итог):
        errors.append("усечённая выдача — сбой проверки, а не предел: %r" % итог)
    полный["_доступность"]["банкротство"]["причина"] = "не покрыто: INN_CHECK_BEZ_REFERER=1"
    итог = dc.итог_снимка(полный)
    if итог["не_покрыты_deal_killer"] != ["банкротство"]:
        errors.append("полный слепок с «не покрыто:» — предел: %r" % итог)

    # список ЦБ: появление совпадения — изменение 🟡, без изменения — тишина
    def цб(v, сост="ok"):
        return {"версия_формата": 2, "поля": {"список_цб": v},
                "состояния": {"егрюл": "ok", "список_цб": сост},
                "не_покрыты": [], "_итог": {"проверка_состоялась": True}}

    def изм_цб(a, b):
        return [x for x in dc.diff(a, b, "d1", "d2") if x.get("поле") == "список_цб"]
    появилось = изм_цб(цб(None, "пусто"), цб(True))
    if not появилось or появилось[0].get("severity") != "🟡":
        errors.append("дифф списка ЦБ: появление не замечено: %r" % появилось)
    if изм_цб(цб(True), цб(True)):
        errors.append("дифф списка ЦБ: шум без изменения")
    пропало = изм_цб(цб(True), цб(None, "пусто"))
    if not пропало or пропало[0].get("severity") != "ℹ️":
        errors.append("дифф списка ЦБ: исчезновение не ℹ️: %r" % пропало)
    if изм_цб(цб(True), цб(None, "не проверено")):
        errors.append("дифф списка ЦБ: молчание источника выдано за исчезновение")
    return errors


def main():
    failed = 0
    for pair, (expected, forbidden, status) in sorted(CASES.items()):
        errors = run_case(pair, expected, forbidden, status)
        label = "%s -> %s" % pair
        if errors:
            failed += 1
            print("FAIL %s" % label)
            for e in errors:
                print("  - %s" % e)
        else:
            print("PASS %s" % label)
    всего = len(CASES)
    with tempfile.TemporaryDirectory(prefix="diff-eval-") as tmp:
        tmpdir = Path(tmp)
        for pair in sorted(CASES):
            всего += 1
            errors = case_отпечатки(pair, tmpdir / ("пара_%s_%s" % pair))
            label = "отпечатки: %s -> %s" % pair
            if errors:
                failed += 1
                print("FAIL %s" % label)
                for e in errors:
                    print("  - %s" % e)
            else:
                print("PASS %s" % label)
        for пара, обязательные, запрещённые in КАРТОЧКИ:
            всего += 1
            errors = case_карточка(пара, обязательные, запрещённые)
            label = "карточка --human: %s -> %s" % пара
            if errors:
                failed += 1
                print("FAIL %s" % label)
                for e in errors:
                    print("  - %s" % e)
            else:
                print("PASS %s" % label)
        всего += 1
        errors = case_хеш(tmpdir)
        if errors:
            failed += 1
            print("FAIL отпечатки: хеш_остального (вне отслеживаемого)")
            for e in errors:
                print("  - %s" % e)
        else:
            print("PASS отпечатки: хеш_остального (вне отслеживаемого)")

    всего += 1
    errors = case_не_покрыты()
    if errors:
        failed += 1
        print("FAIL не-покрыты-по-снимку")
        for e in errors:
            print("  - %s" % e)
    else:
        print("PASS не-покрыты-по-снимку")

    if failed:
        print("FAIL: %d/%d кейсов упало" % (failed, всего))
        return 1
    print("PASS: все %d кейсов зелёные" % всего)
    return 0


if __name__ == "__main__":
    sys.exit(main())
