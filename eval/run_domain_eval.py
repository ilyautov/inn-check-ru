#!/usr/bin/env python3
"""
run_domain_eval.py — domain_check.py офлайн: живые ответы WHOIS .ru/.рф
(eval/fixtures/domain/, сняты 27.09.2026), нормализация ввода, тип регистранта без
имени, «не найден» — «пусто», чужая зона — «не покрыто». PASS/FAIL, stdlib, CI.
"""

import contextlib
import datetime
import importlib.util
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
F = ROOT / "eval" / "fixtures" / "domain"
СЕГОДНЯ = datetime.date(2026, 9, 27)


def load():
    spec = importlib.util.spec_from_file_location("domain_check", ROOT / "scripts" / "domain_check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check(errors, cond, msg):
    if not cond:
        errors.append(msg)


def текст(имя):
    return (F / (имя + ".txt")).read_text(encoding="utf-8")


def case_ответы(d):
    errors = []
    данные, сост, _ = d.оценить("kontur.ru", текст("kontur.ru"), СЕГОДНЯ)
    check(errors, сост == "ok" and данные["регистрант"] == "организация"
          and данные["создан"] == "1997-10-08" and данные["делегирован"]
          and данные["верифицирован"] and данные["регистратор"] == "RU-CENTER-RU"
          and данные["возраст_лет"] == 29.0 and данные["оплачен_до"] == "2026-10-31",
          "kontur.ru: %r" % данные)
    check(errors, "примечание" not in данные, "34 дня до оплаты — с примечанием: %r" % данные)
    данные, _, _ = d.оценить("kontur.ru", текст("kontur.ru"), datetime.date(2026, 10, 15))
    check(errors, данные.get("примечание"), "16 дней до оплаты — без примечания: %r" % данные)
    данные, сост, _ = d.оценить("checko.ru", текст("checko.ru"), СЕГОДНЯ)
    check(errors, сост == "ok" and данные["регистрант"] == "частное лицо", "checko.ru: %r" % данные)
    check(errors, "Private Person" not in str(данные), "имя регистранта в выводе")
    check(errors, "примечание" not in данные, "оплата через год — с примечанием: %r" % данные)
    рф = "xn--80ajghhoc2aj1c8b.xn--p1ai"
    данные, сост, _ = d.оценить(рф, текст(рф), СЕГОДНЯ, дата_регистрации="2012-03-12")
    check(errors, сост == "ok" and данные["домен_моложе_компании_лет"] == 6.0, ".рф: %r" % данные)
    данные, сост, _ = d.оценить("sber.su", текст("sber.su"), СЕГОДНЯ)
    check(errors, сост == "ok" and данные["делегирован"] is False
          and данные["верифицирован"] is None
          and "NOT DELEGATED" in данные["состояние"], "NOT DELEGATED принят за делегирован: %r" % данные)
    # нет строки state или незнакомая метка — «не знаем», а не «нет»
    for имя, правка in (
            ("нет state", lambda t: "\n".join(x for x in t.splitlines()
                                               if not x.startswith("state:"))),
            ("незнакомый state", lambda t: "\n".join(
                "state: UNRECOGNIZED" if x.startswith("state:") else x
                for x in t.splitlines()))):
        данные, сост, _ = d.оценить("kontur.ru", правка(текст("kontur.ru")), СЕГОДНЯ)
        check(errors, сост == "ok" and данные["делегирован"] is None
              and данные["верифицирован"] is None, "%s: %r" % (имя, данные))
    данные, _, _ = d.оценить("kontur.ru", текст("kontur.ru"), datetime.date(2026, 11, 2))
    check(errors, "прошёл" in str(данные.get("примечание")),
          "истёкшая оплата описана как будущая: %r" % данные)
    данные, сост, причина = d.оценить("nety-takogo-domena-12345.ru",
                                      текст("nety-takogo-domena-12345.ru"), СЕГОДНЯ)
    check(errors, сост == "пусто" and данные is None, "не найден: %r %r" % (сост, причина))
    for имя, правка in (
            ("чужой домен в ответе", lambda t: t.replace("KONTUR.RU", "OTHER.RU")),
            ("нет created", lambda t: "\n".join(x for x in t.splitlines()
                                                if not x.startswith("created:"))),
            ("битая дата", lambda t: t.replace("1997-10-08", "08.10.1997")),
            ("пустой ответ", lambda t: "")):
        данные, сост, причина = d.оценить("kontur.ru", правка(текст("kontur.ru")), СЕГОДНЯ)
        check(errors, сост == "не проверено" and данные is None
              and str(причина).startswith("схема:"), "%s: %r %r" % (имя, сост, причина))
    без_типа = "\n".join(x for x in текст("kontur.ru").splitlines()
                         if not x.startswith(("org:", "taxpayer-id:")))
    данные, _, _ = d.оценить("kontur.ru", без_типа, СЕГОДНЯ)
    check(errors, данные["регистрант"] is None, "тип регистранта выдуман: %r" % данные)
    return errors


def case_ввод(d):
    errors = []
    for ввод, ждём in (("https://www.Kontur.ru/путь?x=1", "kontur.ru"), ("kontur.ru.", "kontur.ru"),
                       ("Пример.РФ", "xn--e1afmkfd.xn--p1ai")):
        try:
            check(errors, d.нормализовать(ввод) == ждём, "%r -> %r" % (ввод, d.нормализовать(ввод)))
        except ValueError as e:
            errors.append("%r отвергнут: %s" % (ввод, e))
    for плохой in ("a b", "kontur", "evil.ru\r\nother.ru", "-x.ru", "", "x..ru"):
        try:
            d.нормализовать(плохой)
            errors.append("принят не домен %r" % плохой)
        except ValueError:
            pass
    р = d.проверить("example.com")
    check(errors, р["состояние"] == "не проверено" and р["причина"].startswith("не покрыто:"),
          "чужая зона: %r" % р)
    р = d.проверить("evil.ru\r\nother.ru")
    check(errors, р["состояние"] == "не проверено" and р["причина"].startswith("ввод:"),
          "перевод строки в запрос WHOIS: %r" % р)
    return errors


def _тихо(f, argv, вывод):
    with contextlib.redirect_stdout(вывод):
        return f(argv)


def case_транспорт(d):
    """проверить() с подменённым сокетом: один запрос к whois.tcinet.ru:43, домен
    с CRLF, разбор ответа; сбой сети — «не проверено: сеть:», не «пусто»."""
    errors = []
    журнал, ответ = [], {"текст": текст("kontur.ru"), "сбой": None}

    class Сокет:
        def __init__(self):
            # ответ приходит несколькими кусками, как из сети: читать до пустого
            тело = ответ["текст"].encode("utf-8")
            шаг = max(1, len(тело) // 3)
            self.куски = [тело[i:i + шаг] for i in range(0, len(тело), шаг)] + [b""]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def sendall(self, b):
            журнал[-1]["послано"] = b

        def recv(self, n):
            return self.куски.pop(0)

    def соединиться(адрес, timeout=None):
        журнал.append({"адрес": адрес})
        if ответ["сбой"]:
            raise ответ["сбой"]
        return Сокет()
    orig = d.socket.create_connection
    d.socket.create_connection = соединиться
    вывод = io.StringIO()
    try:
        р = d.проверить("https://www.Kontur.ru/")
        check(errors, р["состояние"] == "ok" and р["данные"]["создан"] == "1997-10-08"
              and р["данные"]["оплачен_до"] == "2026-10-31"
              and журнал == [{"адрес": ("whois.tcinet.ru", 43), "послано": b"kontur.ru\r\n"}],
              "успешный запрос: %r %r" % (р["состояние"], журнал))
        ответ["сбой"] = TimeoutError()
        р = d.проверить("kontur.ru")
        check(errors, р["состояние"] == "не проверено" and р["причина"].startswith("сеть:")
              and len(журнал) == 2, "таймаут: %r" % р)
        ответ.update(сбой=None, текст="")
        р = d.проверить("kontur.ru")
        check(errors, р["состояние"] == "не проверено" and р["причина"].startswith("схема:"),
              "пустой ответ: %r" % р)
        ответ["текст"] = текст("nety-takogo-domena-12345.ru")
        р = d.проверить("nety-takogo-domena-12345.ru")
        check(errors, р["состояние"] == "пусто" and _тихо(d.main, ["nety-takogo-domena-12345.ru"], вывод) == 0,
              "не найден: %r" % р)
        ответ["сбой"] = OSError()
        check(errors, _тихо(d.main, ["kontur.ru"], вывод) == 2, "CLI при сбое сети не код 2")
    finally:
        d.socket.create_connection = orig
    return errors


def main():
    d = load()
    cases = {"ответы WHOIS .ru/.рф": case_ответы(d), "ввод домена": case_ввод(d),
             "транспорт: один запрос, сбой — не «пусто»": case_транспорт(d)}
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
