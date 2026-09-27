#!/usr/bin/env python3
"""
run_manual_block_eval.py — manual_block.py офлайн: форма ручного блока знает все
поля сигналов браузерных источников, отказывает на плохом вводе, «не проверено»
записывается и перекрывает прежнее «проверено», вклейка не меняет вход и fetch.json
пакета и не принимает чужой ИНН. PASS/FAIL, stdlib, CI.
"""

import contextlib
import datetime
import importlib.util
import io
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
СЕГОДНЯ = datetime.date(2026, 9, 28)
ИНН = "7707083893"


def load():
    sys.path.insert(0, str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location("manual_block",
                                                  ROOT / "scripts" / "manual_block.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check(errors, cond, msg):
    if not cond:
        errors.append(msg)


def суды(m, **kw):
    арг = {"блок": "суды", "инн": ИНН, "url": "https://kad.arbitr.ru/", "дата": "2026-09-28",
           "итог": "проверено", "поля": {"ответчик_крупные": "нет"}, "сегодня": СЕГОДНЯ}
    арг.update(kw)
    return m.собрать(**арг)


def отказ(m, msg, **kw):
    try:
        суды(m, **kw)
    except ValueError as e:
        return None if msg in str(e) else "другая причина отказа: %s" % e
    return "не отказал: %r" % kw


def case_поля_сигналов(m):
    """Каждое поле, которое читают сигналы браузерного источника, есть в форме —
    иначе форма не даст записать блок, который резолвер засчитает."""
    errors = []
    check(errors, set(m.браузерные()) == set(m.ПОЛЯ),
          "браузерные %s ≠ блоки формы %s" % (m.браузерные(), sorted(m.ПОЛЯ)))
    for блок in m.браузерные():
        for поле in m.profiles._поля_сигналов(блок):
            check(errors, поле in m.ПОЛЯ.get(блок, {}),
                  "%s.%s читают сигналы, а в форме его нет" % (блок, поле))
    return errors


def case_сборка(m):
    errors = []
    б = суды(m, поля={"ответчик_крупные": "да", "ответчик_дел": "5",
                      "ответчик_сумма": "1 200 000,50"},
             найдено="ответчик\x00 в 5 делах\n")
    check(errors, б["статус"] == "проверено" and б["ответчик_крупные"] is True
          and б["ответчик_дел"] == 5 and б["ответчик_сумма"] == 1200000.5, "типы: %r" % б)
    check(errors, б["найдено"] == "ответчик  в 5 делах", "управляющие символы: %r" % б["найдено"])
    check(errors, б["инн"] == ИНН and б["параметры_поиска"] == "ИНН " + ИНН,
          "объект проверки: %r" % б)
    for msg, kw in (
            ("ИНН", {"инн": "7707083894"}),
            ("url", {"url": "javascript:alert(1)"}),
            ("дата в будущем", {"дата": "2026-10-05"}),
            ("дата", {"дата": "28.09.2026"}),
            ("итог", {"итог": "ок"}),
            ("не предусмотрено", {"поля": {"ответчик_крупные": "нет", "фио": "x"}}),
            ("да/нет", {"поля": {"ответчик_крупные": "может быть"}}),
            ("≥ 0", {"поля": {"ответчик_крупные": "нет", "ответчик_дел": "-1"}}),
            ("≥ 0", {"поля": {"ответчик_крупные": "нет", "ответчик_сумма": "nan"}}),
            ("без полей сигналов", {"поля": {}}),
            ("причину", {"итог": "не проверено", "поля": {}}),
            ("длиннее", {"найдено": "х" * 501}),
            ("клиппер знает только", {"блок": "санкции"})):
        e = отказ(m, msg, **kw)
        check(errors, e is None, "%s: %s" % (msg, e))
    return errors


def case_резолвер(m):
    """Собранный блок резолвер засчитывает; «не проверено» перекрывает прежнее ok."""
    errors = []
    fetch = {"инн": ИНН, "_доступность": {}}
    новый, итоги = m.вклеить(fetch, {"суды": суды(m)})
    check(errors, итоги["суды"][0] == "ok", "собранный блок не засчитан: %r" % (итоги,))
    check(errors, "суды" not in fetch, "вклейка изменила входной fetch")
    повтор, итоги = m.вклеить(новый, {"суды": суды(m, итог="не проверено", поля={},
                                                    причина="капча не пройдена")})
    check(errors, итоги["суды"][0] == "не проверено",
          "«не проверено» не перекрыло прежнее ok: %r" % (итоги,))
    for блоки, msg in (({"суды": dict(суды(m), инн="7736050003")}, "ИНН"),
                       ({"санкции": суды(m)}, "не браузерный")):
        try:
            m.вклеить(fetch, блоки)
            errors.append("вклейка приняла: %s" % msg)
        except ValueError as e:
            check(errors, msg in str(e), "%s: %s" % (msg, e))
    return errors


def case_живой_равно_ретро(m):
    """Вклейка обновляет доступность и итог сбора так, что живой вердикт и ретро
    того же снимка совпадают (ревью Codex): старое «пусто» ФССП не перекрывает
    найденные производства, старое «проверка не состоялась» пересчитывается,
    строковая доступность ручного блока одинакова живьём и после снимка."""
    errors = []
    sys.path.insert(0, str(ROOT / "eval"))
    import copy

    import retro_verdict as rv
    import run_retro_eval as re_
    import snapshot

    def оба(fetch, профиль):
        живой = m.profiles.resolve(copy.deepcopy(fetch), None, профиль)["светофор"]
        восстановленный, _ = rv.восстановить(snapshot.fingerprint(fetch))
        return живой, rv.profiles.resolve(восстановленный, None, профиль)["светофор"]

    фссп = m.собрать("фссп", re_.ИНН, "https://fssp.gov.ru/", "2026-09-28", "проверено",
                     {"производств": "1", "крупные": "да"}, сегодня=СЕГОДНЯ)
    f = re_.полный_снимок()
    f["_доступность"]["фссп"] = re_._av("пусто")
    новый, _ = m.вклеить(f, {"фссп": фссп})
    check(errors, оба(новый, "отсрочка") == ("🔴", "🔴"),
          "старое «пусто» ФССП: %r, ждали 🔴 и живьём, и в ретро" % (оба(новый, "отсрочка"),))

    f = re_.полный_снимок()
    for src in ("риски", "спецреестры", "фссп", "суды"):
        f["_доступность"][src] = re_._av("не проверено", "сеть: тест")
    f["_итог_проверки"] = __import__("fetch_counterparty").build_summary(f["_доступность"])
    суды_ = m.собрать("суды", re_.ИНН, "https://kad.arbitr.ru/", "2026-09-28", "проверено",
                      {"ответчик_крупные": "нет"}, сегодня=СЕГОДНЯ)
    фссп = dict(фссп, производств=1, крупные=False)
    новый, _ = m.вклеить(f, {"суды": суды_, "фссп": фссп})
    ждём = __import__("fetch_counterparty").build_summary(новый["_доступность"])
    check(errors, новый["_итог_проверки"] == ждём, "итог сбора не пересчитан")
    for профиль in ("предоплата", "подрядчик", "отсрочка"):
        живой, ретро = оба(новый, профиль)
        check(errors, живой == ретро, "%s после вклейки: живой %r ≠ ретро %r"
              % (профиль, живой, ретро))

    f = re_.полный_снимок()
    # без «ввод»: ручным блок признаётся только по своему статусу поверх
    # строковой доступности — ровно этот путь расходился живьём и в ретро
    f["риски"] = {"статус": "проверено", "дата_проверки": "2026-02-28",
                  "url": "https://pb.nalog.ru/"}
    f["_доступность"]["риски"] = "проверено"
    живой, ретро = оба(f, "клиент_115фз")
    check(errors, живой == ретро,
          "строковая доступность ручных рисков: живой %r, ретро %r" % (живой, ретро))
    return errors


def case_cli(m):
    errors = []
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        (t / "fetch.json").write_text(json.dumps({"инн": ИНН}), encoding="utf-8")
        (t / "суды.json").write_text(json.dumps(суды(m), ensure_ascii=False), encoding="utf-8")
        пакет = t / "пакет"
        пакет.mkdir()
        (пакет / "fetch.json").write_text("{}", encoding="utf-8")
        (пакет / "manifest.json").write_text("{}", encoding="utf-8")
        for выход, ждём in ((t / "fetch.json", 2), (пакет / "fetch.json", 2),
                            (t / "fetch.ручные.json", 0)):
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                rc = m.main(["manual_block.py", "--вклеить", str(t / "fetch.json"),
                             str(t / "суды.json"), "-o", str(выход)])
            check(errors, rc == ждём, "%s: код %s, ждали %s" % (выход.name, rc, ждём))
        check(errors, json.loads((t / "fetch.json").read_text()) == {"инн": ИНН},
              "вход изменён")
        check(errors, (пакет / "fetch.json").read_text() == "{}", "fetch.json пакета изменён")
        вышло = json.loads((t / "fetch.ручные.json").read_text(encoding="utf-8"))
        check(errors, вышло.get("суды", {}).get("статус") == "проверено", "вклейка: %r" % вышло)
        буф = io.StringIO()
        with contextlib.redirect_stdout(буф):
            rc = m.main(["manual_block.py", "--блок", "фссп", "--инн", ИНН,
                         "--url", "https://fssp.gov.ru/", "--дата", "2026-09-27",
                         "--итог", "проверено", "--поле", "производств=0",
                         "--поле", "крупные=нет"])
        check(errors, rc == 0 and json.loads(буф.getvalue())["крупные"] is False,
              "CLI сборки: %s %r" % (rc, буф.getvalue()[:200]))
    return errors


def main():
    m = load()
    cases = {
        "поля-сигналов-в-форме": case_поля_сигналов(m),
        "сборка-и-отказы": case_сборка(m),
        "резолвер-и-вклейка": case_резолвер(m),
        "cli-не-трогает-вход-и-пакет": case_cli(m),
        "живой-равно-ретро-после-вклейки": case_живой_равно_ретро(m),
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
