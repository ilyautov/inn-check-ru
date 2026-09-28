#!/usr/bin/env python3
"""
run_registries_eval.py — офлайн-eval v1.4.0: разведение ИП/ООО, парсинг
МСП (реальные фикстуры из живого ответа), НПД. ЕРКНМ проверяется в
run_erknm_eval.py, РНП — в run_fsa_eval.py. PASS/FAIL, чистый stdlib, гоняется в CI.
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

    # форма страницы npd.nalog.ru/check-status (ASP.NET, multipart, ответ в lblInfo)
    инн = "504110181262"
    форма = ('<form method="post" action="./" id="MainForm" enctype="multipart/form-data">'
             '<input type="hidden" name="__VIEWSTATE" id="__VIEWSTATE" value="a&amp;b" />'
             '<input type="hidden" name="__VIEWSTATEGENERATOR" id="__VIEWSTATEGENERATOR" '
             'value="112E02C5" /><input name="ctl00$ctl00$tbINN" type="text" /></form>')

    def ответ(текст):
        return форма + '<span id="ctl00_ctl00_lblInfo">%s</span>' % текст

    class _Ответ:
        def __init__(self, тело):
            self.status, self._тело = 200, тело.encode("utf-8")

        def read(self, *a):
            return self._тело

    class _Опенер:
        def __init__(self, страница, итог):
            self.ответы, self.запросы = [страница, итог], []

        def open(self, req, timeout=None):
            self.запросы.append(req)
            return _Ответ(self.ответы.pop(0))

    for текст, ждём in (("%s является плательщиком налога на профессиональный доход" % инн, True),
                        ("%s не является плательщиком налога на профессиональный доход" % инн,
                         False)):
        оп = _Опенер(форма, ответ(текст))
        данные, av = fc.fetch_npd(оп, инн)
        check(errors, av["состояние"] == "ok" and данные["статус_нпд"] is ждём,
              "НПД %s: %r %r" % (ждём, данные, av))
        get, post = оп.запросы
        тело = post.data.decode("utf-8")
        check(errors, post.full_url == fc.NPD_СТРАНИЦА and post.get_header("Referer")
              == fc.NPD_СТРАНИЦА and post.get_header("User-agent") == fc.UA_ПРОЕКТА
              and get.get_header("User-agent") == fc.UA_ПРОЕКТА
              and "multipart/form-data; boundary=" in post.get_header("Content-type"),
              "НПД: заголовки %r" % post.headers)
        check(errors, 'name="ctl00$ctl00$tbINN"\r\n\r\n%s\r\n' % инн in тело
              and 'name="__VIEWSTATE"\r\n\r\na&b\r\n' in тело
              and 'name="ctl00$ctl00$btSend"' in тело, "НПД: тело формы %r" % тело[:300])
    for метка, страница, итог in (
            ("пусто", форма, ответ("")),
            ("чужой ИНН", форма, ответ("504110181263 является плательщиком налога на "
                                      "профессиональный доход")),
            ("чужой ИНН, отрицательный", форма, ответ(
                "504110181263 не является плательщиком налога на профессиональный доход")),
            ("лимит", форма, ответ("Превышено количество запросов")),
            ("нет lblInfo", форма, форма),
            ("форма сменилась", "<html>обновлённая страница</html>", "")):
        данные, av = fc._run_source("нпд", инн, opener=_Опенер(страница, итог))
        check(errors, данные is None and av["состояние"] == "не проверено"
              and av["причина"].startswith("схема:"), "НПД %s: %r" % (метка, av))
    оп = _Опенер("<html>обновлённая страница</html>", "")
    try:
        fc.fetch_npd(оп, инн)
        errors.append("НПД: сменившаяся форма без ошибки")
    except fc.SourceUnavailable as e:
        check(errors, "форма" in str(e) and len(оп.запросы) == 1,
              "НПД: форму отправили вслепую (%s, запросов %d)" % (e, len(оп.запросы)))
    оп = _Опенер(форма, форма)
    данные, av = fc.fetch_npd(оп, "7736207543")
    check(errors, av["причина"].startswith("профиль:") and not оп.запросы,
          "НПД для юрлица: %r, запросов %d" % (av, len(оп.запросы)))
    return errors


def main():
    fc = load_module("fetch_counterparty", ROOT / "scripts" / "fetch_counterparty.py")
    cases = {
        "ип-vs-юрлицо": case_ip_ul(fc),
        "мсп-парсинг": case_msp(fc),
        "нпд-парсинг": case_npd(fc),
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
