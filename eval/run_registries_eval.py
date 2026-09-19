#!/usr/bin/env python3
"""
run_registries_eval.py — офлайн-eval v1.4.0: разведение ИП/ООО, парсинг
МСП (реальные фикстуры из живого ответа), НПД, ЕРКНМ/РНП по кэшу-фикстуре,
«не проверено» без кэша. PASS/FAIL, чистый stdlib, гоняется в CI.
"""

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "eval" / "fixtures" / "registries"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check(errors, cond, msg):
    if not cond:
        errors.append(msg)


def case_ip_ul(fc):
    errors = []
    check(errors, fc._тип_контрагента("7707083893") == "юрлицо",
          "10-значный ИНН не распознан как юрлицо")
    check(errors, fc._тип_контрагента("504110181262") == "ип",
          "12-значный ИНН не распознан как ИП")
    check(errors, fc._pb_mode("504110181262") == ("search-ip", "queryIp"),
          "для ИП выбран не search-ip: %s" % (fc._pb_mode("504110181262"),))
    check(errors, fc._pb_mode("7707083893") == ("search-ul", "queryUl"),
          "для юрлица выбран не search-ul")
    # разбор результата search-ip: спецрежим и ССЧ
    fixture = json.loads((FIXTURES / "pb_ip_result.json").read_text())
    ul = (fixture.get("ip", {}).get("data") or [{}])[0]
    risks = fc._map_pb_flags(ul)
    check(errors, risks.get("спецрежим") == "УСН",
          "спецрежим ИП не распарсился: %s" % risks.get("спецрежим"))
    check(errors, risks.get("численность_сотрудников") == 0,
          "ССЧ ИП не распарсилась: %s" % risks.get("численность_сотрудников"))
    return errors


def case_msp(fc):
    errors = []
    юл = json.loads((FIXTURES / "msp_4707048043.json").read_text())
    row = fc._parse_msp_row(юл["data"][0])
    check(errors, row.get("статус_мсп") == "в реестре",
          "юл: статус %r" % row.get("статус_мсп"))
    check(errors, row.get("категория") == "микропредприятие",
          "юл: категория %r" % row.get("категория"))
    check(errors, row.get("тип") == "юрлицо", "юл: тип %r" % row.get("тип"))
    ип = json.loads((FIXTURES / "msp_504110181262.json").read_text())
    row_ip = fc._parse_msp_row(ип["data"][0])
    check(errors, row_ip.get("тип") == "ип", "ип: тип %r" % row_ip.get("тип"))
    check(errors, row_ip.get("статус_мсп") == "в реестре",
          "ип: статус %r" % row_ip.get("статус_мсп"))
    искл = json.loads((FIXTURES / "msp_561017190346.json").read_text())
    row_x = fc._parse_msp_row(искл["data"][0])
    check(errors, row_x.get("статус_мсп") == "исключена из реестра",
          "исключённая: статус %r" % row_x.get("статус_мсп"))
    return errors


def case_npd(fc):
    errors = []
    j = json.loads((FIXTURES / "npd_ok.json").read_text())
    out = fc._parse_npd(j, "504110181262")
    check(errors, out.get("статус_нпд") is True,
          "статус_нпд %r" % out.get("статус_нпд"))
    check(errors, out.get("сообщение"), "нет сообщения НПД")
    check(errors, fc._parse_npd("<html>406</html>", "1") is None,
          "не-JSON НПД должен деградировать в None")
    return errors


def case_registries(rr):
    errors = []
    res, note = rr.lookup("еркнм", "504110181262",
                          cache_dir=str(FIXTURES / "cache_erknm"))
    check(errors, res is not None, "еркнм: lookup вернул None (%s)" % note)
    if res:
        check(errors, res.get("в_реестре") is True,
              "еркнм: ИНН 504110181262 не найден")
        check(errors, res.get("записей", 0) >= 1, "еркнм: записей 0")
        check(errors, "контрольный надзор" in json.dumps(res, ensure_ascii=False),
              "еркнм: контекст записи пуст")
    res2, _ = rr.lookup("еркнм", "7700000000",
                        cache_dir=str(FIXTURES / "cache_erknm"))
    check(errors, res2 is not None and res2.get("в_реестре") is False,
          "еркнм: чужой ИНН должен дать в_реестре=False")
    res3, note3 = rr.lookup("еркнм", "7707083893",
                            cache_dir=str(FIXTURES / "cache_empty"))
    check(errors, res3 is None and "кэш отсутствует" in (note3 or ""),
          "еркнм без кэша: должно быть «не проверено», есть %r" % res3)
    res4, _ = rr.lookup("рнп", "561017190346",
                        cache_dir=str(FIXTURES / "cache_rnp"))
    check(errors, res4 is not None and res4.get("в_реестре") is True,
          "рнп: ИНН 561017190346 не найден")
    res5, _ = rr.lookup("рнп", "7707083893",
                        cache_dir=str(FIXTURES / "cache_rnp"))
    check(errors, res5 is not None and res5.get("в_реестре") is False,
          "рнп: чистый ИНН должен дать в_реестре=False")
    return errors


def main():
    fc = load_module("fetch_counterparty", ROOT / "scripts" / "fetch_counterparty.py")
    rr = load_module("registries_refresh", ROOT / "scripts" / "registries_refresh.py")
    cases = {
        "ип-vs-юрлицо": case_ip_ul(fc),
        "мсп-парсинг": case_msp(fc),
        "нпд-парсинг": case_npd(fc),
        "еркнм-рнп-lookup": case_registries(rr),
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
