#!/usr/bin/env python3
"""
run_watchlist_eval.py — офлайн-прогон scripts/watchlist.py (спек волны 2, §3.2).

Сети не требует: движок сбора подменяется заглушкой (INN_CHECK_ENGINE), которая
печатает фикстуру-снимок; список наблюдения и кэш снимков — во временной
директории (INN_CHECK_WATCHLIST / INN_CHECK_CACHE). Проверяется:
  1. --добавить / --список / --убрать пишут и читают файл списка;
  2. --прогон делает отпечаток (версия_формата 1), первый прогон — «первый снимок»;
  3. повторный прогон на той же фикстуре изменений не даёт, --только-изменения
     молчит (пустой stdout, код 0);
  4. смена фикстуры (статус → ликвидация, новый год отчётности) даёт ожидаемые
     изменения с нужными severity; --human печатает killer-карточку;
  5. движку уходит `--режим всё` РОВНО ОДИН раз: молчаливого повтора без флага
     нет, потому что снимок другого режима несравним с полным — отказ движка
     это «не проверено», а не повод собрать что попало;
  6. --установить-расписание печатает crontab и launchd plist с реальными
     абсолютными путями и ничего не устанавливает;
  7. прогон, где НЕ ПРОВЕРЕН НИ ОДИН источник, даёт статус «не проверено» с
     перечнем и печатается даже в --только-изменения (тихая деградация: крон
     промолчал бы о ликвидации контрагента);
  8. источник, ушедший в «не проверено», виден как отдельное изменение, а его
     поля в сравнение значений не идут;
  9. сбой по одному ИНН не роняет прогон всего списка.
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WATCHLIST = ROOT / "scripts" / "watchlist.py"
FIXTURES = ROOT / "eval" / "fixtures" / "snapshots"
ИНН = "7707083893"

# Заглушка движка: печатает фикстуру из INN_CHECK_STUB_FIXTURE, пишет полученные
# аргументы в INN_CHECK_STUB_LOG. При INN_CHECK_STUB_MODE != "1" ведёт себя как
# движок, который флага --режим не знает: usage в stderr и код 2 — такой прогон
# обязан стать «не проверено», а не тихо собраться без флага.
ЗАГЛУШКА = '''import json, os, sys
args = sys.argv[1:]
with open(os.environ["INN_CHECK_STUB_LOG"], "a", encoding="utf-8") as fh:
    fh.write(" ".join(args) + "\\n")
if os.environ.get("INN_CHECK_STUB_MODE") != "1" and "--режим" in args:
    sys.stderr.write("Использование: fetch_counterparty.py <ИНН> [--save]\\n")
    sys.exit(2)
with open(os.environ["INN_CHECK_STUB_FIXTURE"], encoding="utf-8") as fh:
    sys.stdout.write(json.dumps(json.load(fh), ensure_ascii=False))
'''


def запуск(args, окружение, ожидаемый_код=0):
    proc = subprocess.run([sys.executable, str(WATCHLIST), *args],
                          capture_output=True, text=True, check=False,
                          env=окружение)
    errors = []
    if proc.returncode != ожидаемый_код:
        errors.append("%s: код %s != %s (stderr: %.200s)"
                      % (" ".join(args), proc.returncode, ожидаемый_код,
                         proc.stderr.strip()))
    return proc, errors


def json_запуск(args, окружение):
    proc, errors = запуск(args, окружение)
    if errors:
        return None, errors
    try:
        return json.loads(proc.stdout), []
    except ValueError:
        return None, ["%s: вывод не JSON: %.200s" % (" ".join(args), proc.stdout)]


def _вызовы(окружение):
    """Строки лога заглушки: по строке на вызов движка."""
    путь = Path(окружение["INN_CHECK_STUB_LOG"])
    if not путь.exists():
        return []
    return [s for s in путь.read_text(encoding="utf-8").split("\n") if s.strip()]


def кейс_список(окружение):
    errors = []
    итог, err = json_запуск(
        ["--добавить", ИНН, "--профиль", "отсрочка", "--комментарий", "тестовый"],
        окружение)
    errors += err
    if итог and итог.get("статус") != "добавлено":
        errors.append("--добавить: статус %r" % итог.get("статус"))
    if not os.path.exists(окружение["INN_CHECK_WATCHLIST"]):
        errors.append("файл списка не создан: %s" % окружение["INN_CHECK_WATCHLIST"])

    итог, err = json_запуск(["--список"], окружение)
    errors += err
    if итог:
        if итог.get("всего") != 1:
            errors.append("--список: всего %r != 1" % итог.get("всего"))
        запись = (итог.get("контрагенты") or [{}])[0]
        if запись.get("инн") != ИНН:
            errors.append("--список: ИНН %r" % запись.get("инн"))
        if запись.get("профиль") != "отсрочка":
            errors.append("--список: профиль %r" % запись.get("профиль"))
        if запись.get("снимков") != 0:
            errors.append("--список: снимков %r != 0 (прогонов ещё не было)"
                          % запись.get("снимков"))

    # Некорректный ИНН не добавляется молча.
    итог, err = запуск(["--добавить", "123"], окружение, ожидаемый_код=1)
    if "ошибка" not in (итог.stdout or ""):
        errors.append("--добавить 123: короткий ИНН принят молча")
    return errors


def кейс_убрать(окружение):
    errors = []
    итог, err = json_запуск(["--убрать", ИНН], окружение)
    errors += err
    if итог and итог.get("статус") != "убрано":
        errors.append("--убрать: статус %r" % итог.get("статус"))
    итог, err = json_запуск(["--список"], окружение)
    errors += err
    if итог and итог.get("всего") != 0:
        errors.append("--список после --убрать: всего %r != 0" % итог.get("всего"))
    proc, _ = запуск(["--убрать", ИНН], окружение, ожидаемый_код=1)
    if "не найдено" not in proc.stdout:
        errors.append("повторное --убрать не сказало «не найдено»")
    return errors


def кейс_прогон(окружение):
    """Прогон на движке, который знает `--режим всё` (INN_CHECK_STUB_MODE=1)."""
    errors = []
    окружение["INN_CHECK_STUB_FIXTURE"] = str(FIXTURES / "states_a.json")

    # 1. Первый прогон: сравнивать не с чем, отпечаток сохранён.
    отчёт, err = json_запуск(["--прогон"], окружение)
    errors += err
    if отчёт:
        результаты = отчёт.get("результаты") or []
        if not результаты or результаты[0].get("статус") != "первый снимок":
            errors.append("первый прогон: статус %r"
                          % (результаты[0].get("статус") if результаты else None))
        снимок = результаты[0].get("снимок") if результаты else None
        if not снимок or not os.path.exists(снимок):
            errors.append("первый прогон: снимок не сохранён (%r)" % снимок)
        else:
            with open(снимок, encoding="utf-8") as fh:
                отпечаток = json.load(fh)
            if отпечаток.get("версия_формата") != 1:
                errors.append("снимок не отпечаток: версия_формата %r"
                              % отпечаток.get("версия_формата"))
            for ключ in ("поля", "состояния", "сбор", "хеш_остального", "_итог"):
                if ключ not in отпечаток:
                    errors.append("в отпечатке нет ключа %r" % ключ)
            # Дефект: отпечаток не хранил, каким режимом и профилем собран.
            сбор = отпечаток.get("сбор") or {}
            if сбор.get("режим") != "всё" or сбор.get("профиль") != "нейтрально":
                errors.append("отпечаток не сохранил условия сбора: %r" % сбор)

    # Движку ушёл `--режим всё` — ровно одним вызовом, без повторов.
    вызовы = _вызовы(окружение)
    if not вызовы or "--режим всё" not in вызовы[0]:
        errors.append("движку не ушёл `--режим всё`: %r" % вызовы[:2])
    if len(вызовы) != 1:
        errors.append("движок знает флаг, но вызовов %d != 1" % len(вызовы))

    # 2. Тот же снимок: изменений нет, --только-изменения молчит.
    отчёт, err = json_запуск(["--прогон"], окружение)
    errors += err
    if отчёт:
        if отчёт.get("изменилось") != 0 or отчёт.get("результаты"):
            errors.append("повтор той же фикстуры дал изменения: %s"
                          % json.dumps(отчёт.get("результаты"), ensure_ascii=False))
        if отчёт.get("без_изменений") != 1:
            errors.append("без_изменений %r != 1" % отчёт.get("без_изменений"))
    proc, err = запуск(["--прогон", "--только-изменения"], окружение)
    errors += err
    if proc.stdout != "":
        errors.append("--только-изменения не молчит на прошедшем прогоне: %.200s"
                      % proc.stdout)

    # 3. Смена фикстуры: ожидаемые изменения с нужными severity.
    окружение["INN_CHECK_STUB_FIXTURE"] = str(FIXTURES / "states_c.json")
    отчёт, err = json_запуск(["--прогон", "--только-изменения"], окружение)
    errors += err
    ожидание = {"статус": "🔴", "дата_прекращения": "🔴",
                "чистые_активы": "🔴", "выручка": "🟡"}
    if отчёт:
        if отчёт.get("изменилось") != 1:
            errors.append("изменилось %r != 1" % отчёт.get("изменилось"))
        результаты = отчёт.get("результаты") or [{}]
        изменения = {c["поле"]: c.get("severity")
                     for c in результаты[0].get("изменения") or []}
        for поле, sev in ожидание.items():
            if изменения.get(поле) != sev:
                errors.append("изменение %s: severity %r != %r"
                              % (поле, изменения.get(поле), sev))
        if результаты[0].get("комментарий") != "тестовый":
            errors.append("в отчёте потерялся комментарий записи")

    # 4. --human печатает карточку изменений.
    окружение["INN_CHECK_STUB_FIXTURE"] = str(FIXTURES / "states_a.json")
    proc, err = запуск(["--прогон", "--human"], окружение)
    errors += err
    for кусок in ("🚦 Мониторинг контрагента", ИНН, "Что НЕ проверено"):
        if кусок not in proc.stdout:
            errors.append("--human: в карточке нет %r" % кусок)
    return errors


def кейс_молчание(окружение):
    """Прогон, где не проверен НИ ОДИН источник, обязан кричать.

    Это дефект класса «тихая деградация»: раньше такой прогон давал
    «без изменений», выбрасывался из результатов и в кроновом
    --только-изменения не печатал ничего — контрагент мог быть ликвидирован,
    а крон молчал.
    """
    errors = []
    окружение["INN_CHECK_STUB_FIXTURE"] = str(FIXTURES / "states_silent.json")
    отчёт, err = json_запуск(["--прогон"], окружение)      # первый снимок
    errors += err
    отчёт, err = json_запуск(["--прогон"], окружение)
    errors += err
    if отчёт:
        if отчёт.get("без_изменений") != 0:
            errors.append("прогон без единого проверенного источника засчитан "
                          "как «без изменений»: %r" % отчёт.get("без_изменений"))
        if отчёт.get("не_проверено") != 1:
            errors.append("не_проверено %r != 1" % отчёт.get("не_проверено"))
        результаты = отчёт.get("результаты") or []
        if not результаты:
            errors.append("строка «не проверено» выброшена из результатов")
        else:
            r = результаты[0]
            if r.get("статус") != "не проверено":
                errors.append("статус %r != 'не проверено'" % r.get("статус"))
            if not r.get("не_ответили_deal_killer"):
                errors.append("в строке нет перечня непроверенных "
                              "deal-killer-источников")
            for sid in ("егрюл", "риски"):
                if sid not in (r.get("причина") or ""):
                    errors.append("в причине нет источника %r: %r"
                                  % (sid, r.get("причина")))
    # Тихий режим крона обязан напечатать такую строку.
    proc, err = запуск(["--прогон", "--только-изменения"], окружение)
    errors += err
    if not proc.stdout.strip():
        errors.append("--только-изменения промолчал о прогоне, где не проверен "
                      "ни один источник")
    proc, err = запуск(["--прогон", "--human"], окружение)
    errors += err
    if "проверка не состоялась" not in proc.stdout:
        errors.append("--human не сказал «проверка не состоялась»: %.200s"
                      % proc.stdout)

    # Источник, ушедший в «не проверено», виден как изменение, а его поля —
    # не сравниваются (никаких «статус → None»).
    окружение["INN_CHECK_STUB_FIXTURE"] = str(FIXTURES / "states_a.json")
    json_запуск(["--прогон"], окружение)
    окружение["INN_CHECK_STUB_FIXTURE"] = str(FIXTURES / "states_b.json")
    отчёт, err = json_запуск(["--прогон"], окружение)
    errors += err
    if отчёт:
        результаты = отчёт.get("результаты") or [{}]
        изменения = {c["поле"]: c.get("severity")
                     for c in результаты[0].get("изменения") or []}
        if изменения.get("источник.егрюл") != "🟡":
            errors.append("нет записи «источник.егрюл перестал проверяться»: %r"
                          % изменения)
        for поле in ("статус", "руководитель", "недостоверность_сведений"):
            if поле in изменения:
                errors.append("поле %r сравнивалось, хотя источник не проверен"
                              % поле)
        if результаты[0].get("статус") != "не проверено":
            errors.append("статус прогона с замолчавшим ЕГРЮЛ: %r"
                          % результаты[0].get("статус"))
    return errors


def кейс_движок_без_флага(окружение):
    """Движок, не знающий `--режим всё`: одна попытка, честное «не проверено».

    Молчаливый повтор без флага собрал бы снимок другим режимом — несравнимый
    с предыдущими; такие снимки и порождают «изменения», которых не было.
    """
    errors = []
    окружение["INN_CHECK_STUB_FIXTURE"] = str(FIXTURES / "states_a.json")
    отчёт, err = json_запуск(["--прогон"], окружение)
    errors += err
    вызовы = _вызовы(окружение)
    if len(вызовы) != 1:
        errors.append("вызовов движка %d != 1 (молчаливый повтор без флага?): %r"
                      % (len(вызовы), вызовы))
    if вызовы and "--режим всё" not in вызовы[0]:
        errors.append("движку не ушёл `--режим всё`: %r" % вызовы[:1])
    if отчёт:
        результаты = отчёт.get("результаты") or [{}]
        if результаты[0].get("статус") != "не проверено":
            errors.append("отказ движка не стал «не проверено»: %r"
                          % результаты[0].get("статус"))
        if отчёт.get("не_проверено") != 1:
            errors.append("не_проверено %r != 1" % отчёт.get("не_проверено"))
        if "--режим всё" not in (результаты[0].get("причина") or ""):
            errors.append("в причине не назван флаг: %r" % результаты[0].get("причина"))
    proc, err = запуск(["--прогон", "--только-изменения"], окружение)
    errors += err
    if not proc.stdout.strip():
        errors.append("--только-изменения промолчал о несостоявшемся прогоне")
    return errors


def кейс_сбой_не_роняет_список(tmpdir):
    """Сбой по одному ИНН даёт строку «не проверено» и не роняет список.

    Проверяется в процессе: snapshot.save() подменяется на бросок исключения
    (раньше None из save() уходил в _load(None) → TypeError, и остальные
    контрагенты списка не проверялись вовсе — в кроне это выглядело как тишина).
    """
    errors = []
    spec = importlib.util.spec_from_file_location(
        "watchlist_под_тестом", str(WATCHLIST))
    wl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wl)

    каталог = tmpdir / "сбой"
    каталог.mkdir(parents=True, exist_ok=True)
    файл = str(каталог / "watchlist.json")
    кэш = str(каталог / "cache")
    for inn in (ИНН, "5036045205"):
        wl.добавить(inn, файл=файл)

    фикстура = json.loads((FIXTURES / "states_a.json").read_text(encoding="utf-8"))

    def движок_ок(inn, профиль=None, движок=None):
        снимок = json.loads(json.dumps(фикстура))
        снимок["инн"] = inn
        return снимок, None

    wl._запустить_движок = движок_ок
    падал = {"раз": 0}
    настоящий_save = wl.snapshot.save

    def save_падает(inn, result, dir=None):
        if str(inn) == ИНН:
            падал["раз"] += 1
            raise RuntimeError("диск кончился")
        return настоящий_save(inn, result, dir=dir)

    wl.snapshot.save = save_падает
    try:
        отчёт = wl.прогон(файл=файл, каталог=кэш)
    except Exception as e:   # именно это и чинится
        return ["прогон упал целиком: %s: %s" % (type(e).__name__, e)]
    finally:
        wl.snapshot.save = настоящий_save

    if отчёт.get("проверено") != 2:
        errors.append("проверено %r != 2 — список не дошёл до конца"
                      % отчёт.get("проверено"))
    по_инн = {r["инн"]: r for r in отчёт.get("результаты") or []}
    if по_инн.get(ИНН, {}).get("статус") != "не проверено":
        errors.append("упавший ИНН не стал «не проверено»: %r" % по_инн.get(ИНН))
    if "5036045205" not in по_инн:
        errors.append("второй ИНН не проверен вовсе (сбой уронил список)")
    if not падал["раз"]:
        errors.append("подмена save() не сработала — кейс ничего не проверил")

    # Прямой повод падения: путь несохранённого снимка (None) уходил в _load().
    d = wl.diff_counterparty
    if d._load(None) is not None:
        errors.append("_load(None) вернул не None")
    try:
        d.diff({"инн": ИНН}, {"инн": ИНН}, "было", "стало",
               все_снимки=[None, str(FIXTURES / "states_a.json")])
    except Exception as e:   # именно это и чинится
        errors.append("diff с None в списке снимков упал: %s: %s"
                      % (type(e).__name__, e))
    return errors


def кейс_расписание(окружение):
    proc, errors = запуск(["--установить-расписание"], окружение)
    текст = proc.stdout
    обязательно = [
        str(WATCHLIST),                       # реальный абсолютный путь скрипта
        os.path.abspath(sys.executable),      # реальный интерпретатор
        "--прогон", "--только-изменения",
        "crontab -e", "launchctl load", "<plist version=\"1.0\">",
        "Label", "StartCalendarInterval",
        окружение["INN_CHECK_WATCHLIST"],     # окружение прокинуто в задание
        "Ничего не установлено",
    ]
    for кусок in обязательно:
        if кусок not in текст:
            errors.append("--установить-расписание: в выводе нет %r" % кусок)
    if not os.path.isabs(str(WATCHLIST)):
        errors.append("путь скрипта не абсолютный")
    return errors


def main():
    кейсы = [
        ("список: --добавить / --список / --убрать", None),
        ("прогон: движок знает --режим всё", "1"),
        ("прогон: движок не знает --режим всё → «не проверено» без повтора", "0"),
        ("тихая деградация: ни один источник не проверен", "1"),
        ("расписание: печать crontab + launchd plist", None),
    ]
    failed = 0
    with tempfile.TemporaryDirectory(prefix="watchlist-eval-") as tmp:
        tmpdir = Path(tmp)
        движок = tmpdir / "stub_engine.py"
        движок.write_text(ЗАГЛУШКА, encoding="utf-8")

        def окружение(поддон, режим=None):
            каталог = tmpdir / поддон
            каталог.mkdir(parents=True, exist_ok=True)
            env = dict(os.environ)
            env.update({
                "INN_CHECK_WATCHLIST": str(каталог / "watchlist.json"),
                "INN_CHECK_CACHE": str(каталог / "cache"),
                "INN_CHECK_ENGINE": str(движок),
                "INN_CHECK_STUB_LOG": str(каталог / "движок.log"),
                "INN_CHECK_STUB_FIXTURE": str(FIXTURES / "states_a.json"),
                "PYTHONIOENCODING": "utf-8",
            })
            if режим is not None:
                env["INN_CHECK_STUB_MODE"] = режим
            return env

        for i, (название, режим) in enumerate(кейсы):
            env = окружение("кейс%d" % i, режим)
            if название.startswith("список"):
                errors = кейс_список(env) + кейс_убрать(env)
            elif название.startswith("прогон: движок знает"):
                errors = кейс_список(env) + кейс_прогон(env)
            elif название.startswith("прогон: движок не знает"):
                errors = кейс_список(env) + кейс_движок_без_флага(env)
            elif название.startswith("тихая деградация"):
                errors = кейс_список(env) + кейс_молчание(env)
            else:
                errors = кейс_список(env) + кейс_расписание(env)
            if errors:
                failed += 1
                print("FAIL %s" % название)
                for e in errors:
                    print("  - %s" % e)
            else:
                print("PASS %s" % название)

        название = "устойчивость: сбой по одному ИНН не роняет список"
        errors = кейс_сбой_не_роняет_список(tmpdir)
        if errors:
            failed += 1
            print("FAIL %s" % название)
            for e in errors:
                print("  - %s" % e)
        else:
            print("PASS %s" % название)
    всего = len(кейсы) + 1
    if failed:
        print("FAIL: %d/%d кейсов упало" % (failed, всего))
        return 1
    print("PASS: все %d кейсов зелёные" % всего)
    return 0


if __name__ == "__main__":
    sys.exit(main())
