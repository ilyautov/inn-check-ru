#!/usr/bin/env python3
"""
run_rzn_licenses_eval.py — офлайн-eval лицензий Росздравнадзора (rzn_licenses.py).

Сеть не трогается: выгрузки — синтетический XML той же структуры, что живая
выгрузка 27.09.2026 (юрлица с условными названиями, без адресов и ФИО).

Чем слой опасен и что проверяется:
  * «Возобновлено: <дата в будущем>» (так в живой выгрузке) — лицензия сегодня
    ПРИОСТАНОВЛЕНА, а не действует;
  * прекращение побеждает возобновление;
  * «записи нет» — `пусто` с постоянной оговоркой охвата, а не «лицензии нет»;
  * нет индекса — «не проверено» с причиной `кэш:`, а не тихое «пусто»;
  * скачались не все наборы — старый индекс не затирается частичным;
  * устаревшая выгрузка — предупреждение;
  * в индекс не попадает наименование лицензиата (у ИП это ФИО) и адреса.
"""

import gzip
import importlib.util
import io
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check(errors, cond, msg):
    if not cond:
        errors.append(msg)


def лицензия(инн, номер, приост="", прекр="", мест=1, имя="ООО «Условная аптека»",
             дата_прекр=""):
    места = "".join("<address_place><address>условный адрес %d</address></address_place>"
                    % i for i in range(мест))
    return ("<licenses><name>Условный лицензирующий орган</name>"
            "<activity_type>Фармацевтическая деятельность</activity_type>"
            "<full_name_licensee>%s</full_name_licensee><inn>%s</inn>"
            "<work_address_list>%s</work_address_list><number>%s</number>"
            "<date_register>01.02.2020</date_register><termination></termination>"
            "<date_termination>%s</date_termination>"
            "<information_suspension_resumption>%s</information_suspension_resumption>"
            "<information_cancellation>%s</information_cancellation></licenses>"
            % (имя, инн, места, номер, дата_прекр, приост, прекр))


def выгрузка(*записи):
    return ('<?xml version="1.0" encoding="utf-8"?><licenses_list>%s</licenses_list>'
            % "".join(записи)).encode("utf-8")


ВЫГРУЗКА_LS = выгрузка(
    лицензия("7700000001", "Л-1", мест=3),
    # плановое возобновление в будущем: сегодня (2026-09-28) — приостановлена
    лицензия("7700000002", "Л-2", приост="Приостановлено:  от 2026-07-16, Причина: "
             "Приостановление действия лицензии в части работ и услуг/Возобновлено: "
             "2026-10-14, Причина: Возобновление лицензии (по истечении срока "
             "приостановления)"),
    лицензия("7700000003", "Л-3", приост="Приостановлено: Приказ № 1-Э от 2026-03-31, "
             "Причина: Приостановление лицензии/Возобновлено: Приказ № 1-Э, 2026-05-10, "
             "Причина: Возобновление лицензии"),
    лицензия("7700000004", "Л-4", приост="Возобновлено: 2025-08-01, Причина: "
             "Возобновление лицензии", прекр="Причина: Прекращение лицензии"),
    лицензия("7700000005", "Л-5", приост="Приостановлено:  от 2026-09-01, Причина: "
             "Приостановление лицензии"),
    лицензия("500100000000", "Л-6", имя="ИП ФИО УБРАНО ИЗ ФИКСТУРЫ"),
    лицензия("123", "Л-7"),                          # мусорный ИНН — не в индекс
    # две приостановки, возобновление закрывает только первую (ревью Codex)
    лицензия("7700000010", "Л-10", приост="Приостановлено:  от 2026-09-20, Причина: "
             "Приостановление лицензии/Приостановлено:  от 2026-08-01, Причина: "
             "Приостановление лицензии/Возобновлено: 2026-08-10, Причина: Возобновление"),
    # приостановка назначена на будущее — сегодня ещё действует
    лицензия("7700000011", "Л-11", приост="Приостановлено:  от 2026-10-05, Причина: "
             "Приостановление лицензии"),
    # событие без даты — не угадываем
    лицензия("7700000012", "Л-12", приост="Приостановлено: по решению, Причина: "
             "Приостановление лицензии"),
    # в номере приказа «/» — не разделитель событий
    # прекращение назначено на будущее — сегодня ещё действует
    лицензия("7700000014", "Л-14", прекр="Причина: Прекращение лицензии",
             дата_прекр="2026-10-15"),
    лицензия("7700000013", "Л-13", приост="Приостановлено: Приказ № 12/3 от 2026-09-01, "
             "Причина: Приостановление лицензии"),
)
СЕГОДНЯ = "2026-09-28"


def meta(набор, даты, valid="20261004"):
    строки = ["property,value", "identifier,7710537160-%s" % набор, "valid,%s" % valid]
    строки += ["data-%s-structure-20210307,https://roszdravnadzor.gov.ru/opendata/"
               "7710537160-%s/data-%s.zip" % (д, набор, д) for д in даты]
    return "\n".join(строки).encode("utf-8")


def архив(xml):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("data.xml", xml)
    return buf.getvalue()


def получатель(сломать=None, выгрузки=None, valid="20261004"):
    """Подмена сети: meta.csv с двумя датами (берётся поздняя) и архивы.
    Сигнатура как у rzn_licenses._get: (url, лимит, куда=None)."""
    выгрузки = выгрузки or {}

    def получить(url, лимит, куда=None):
        for набор in ("ls_licenses", "nark_licenses", "md_licenses"):
            if url.endswith("7710537160-%s/meta.csv" % набор):
                if набор == сломать:
                    raise OSError("сеть")
                return meta(набор, ["20260920", "20260927"], valid)
            if "/7710537160-%s/data-" % набор in url:
                if "20260920" in url:
                    raise AssertionError("взята не последняя выгрузка")
                данные = архив(выгрузки.get(набор) or (
                    ВЫГРУЗКА_LS if набор == "ls_licenses"
                    else выгрузка(лицензия("7711111111", "Л-%s" % набор))))
                куда.write(данные)
                return len(данные)
        raise AssertionError("неожиданный URL %s" % url)
    return получить


def case_состояния(rzn):
    errors = []
    with tempfile.TemporaryDirectory() as td:
        res = rzn.refresh(cache_dir=td, получить=получатель())
        check(errors, res["статус"] == "обновлено", "refresh: %r" % res)
        def сост(инн):
            r, _ = rzn.lookup(инн, cache_dir=td, сегодня=СЕГОДНЯ)
            return [(л["состояние"], л.get("состояние_пояснение")) for л in r["лицензии"]]
        check(errors, сост("7700000001") == [("действует", None)], "действует: %r"
              % сост("7700000001"))
        check(errors, сост("7700000002") == [("приостановлена_частично",
                                               "с 2026-07-16 до 2026-10-14")],
              "возобновление в будущем: %r" % сост("7700000002"))
        check(errors, сост("7700000003") == [("действует", None)],
              "возобновлена в прошлом: %r" % сост("7700000003"))
        check(errors, сост("7700000004")[0][0] == "прекращена",
              "прекращение после возобновления: %r" % сост("7700000004"))
        check(errors, сост("7700000005") == [("приостановлена", "с 2026-09-01")],
              "без возобновления: %r" % сост("7700000005"))
        check(errors, сост("7700000010") == [("приостановлена", "с 2026-09-20")],
              "две приостановки, одно возобновление: %r" % сост("7700000010"))
        check(errors, сост("7700000011") == [("действует", None)],
              "приостановка в будущем: %r" % сост("7700000011"))
        check(errors, сост("7700000012")[0][0] == "неясно",
              "событие без даты: %r" % сост("7700000012"))
        check(errors, сост("7700000013") == [("приостановлена", "с 2026-09-01")],
              "«/» в номере приказа: %r" % сост("7700000013"))
        check(errors, сост("7700000014") == [("действует",
                                               "прекращение назначено на 2026-10-15")],
              "прекращение в будущем: %r" % сост("7700000014"))
        r, _ = rzn.lookup("7700000001", cache_dir=td, сегодня=СЕГОДНЯ)
        check(errors, r["лицензии"][0]["мест_деятельности"] == 3
              and r["дата_выгрузки"]["ls_licenses"] == "2026-09-27"
              and not r["предупреждения"], "запись: %r" % r)
        r, _ = rzn.lookup("7700000009", cache_dir=td, сегодня=СЕГОДНЯ)
        check(errors, r["в_реестре"] is False, "нет записи: %r" % r)
        r, _ = rzn.lookup("7700000001", cache_dir=td, сегодня="2026-10-05")
        check(errors, len(r["предупреждения"]) == 3, "устаревшая выгрузка: %r"
              % r["предупреждения"])
        with gzip.open(os.path.join(td, rzn.ИНДЕКС), "rt", encoding="utf-8") as fh:
            сырой = fh.read()
        check(errors, "ФИО УБРАНО" not in сырой and "условный адрес" not in сырой
              and '"123"' not in сырой, "в индекс попали имя, адрес или мусорный ИНН")
    return errors


def case_частичное_обновление(rzn):
    errors = []
    with tempfile.TemporaryDirectory() as td:
        rzn.refresh(cache_dir=td, получить=получатель())
        путь = os.path.join(td, rzn.ИНДЕКС)
        было = Path(путь).read_bytes()
        res = rzn.refresh(cache_dir=td, получить=получатель(сломать="md_licenses"))
        check(errors, res["статус"].startswith("индекс не обновлён")
              and res["наборы"]["md_licenses"]["статус"] == "не скачан",
              "частичное обновление: %r" % res)
        check(errors, Path(путь).read_bytes() == было, "частичный индекс затёр старый")
        orig = rzn._записи_xml

        def без_парсера(поток):
            raise ImportError("pyexpat")
            yield
        rzn._записи_xml = без_парсера
        try:
            res = rzn.refresh(cache_dir=td, получить=получатель())
        finally:
            rzn._записи_xml = orig
        check(errors, "XML-парсер" in res["статус"], "сломанный pyexpat: %r" % res)
        # смена схемы, пустой набор, падение вдвое — старый индекс цел
        ns = ('<?xml version="1.0"?><licenses_list xmlns="urn:x">%s</licenses_list>'
              % лицензия("7700000001", "Л-1")).encode("utf-8")
        чужой = выгрузка(*[лицензия("77000000%02d" % i, "Л-%d" % i) for i in range(1, 14)]
                         ).replace(b"licenses_list", b"other_root")
        мало = выгрузка(*[лицензия("77000000%02d" % i, "Л") for i in range(1, 3)])
        for что, выгр in (("namespace", {"ls_licenses": ns}),
                          ("другой корень", {"ls_licenses": чужой}),
                          ("пустой набор", {"md_licenses": выгрузка()}),
                          ("падение вдвое", {"ls_licenses": мало})):
            res = rzn.refresh(cache_dir=td, получить=получатель(выгрузки=выгр))
            check(errors, res["статус"].startswith("индекс не обновлён")
                  and Path(путь).read_bytes() == было, "%s: %r" % (что, res["статус"]))
        # занятая блокировка — второй refresh не пишет
        замок = os.path.join(td, ".refresh.lock")
        Path(замок).write_text("")
        res = rzn.refresh(cache_dir=td, получить=получатель())
        check(errors, "уже идёт" in res["статус"] and os.path.exists(замок),
              "блокировка: %r" % res)
        os.unlink(замок)
        check(errors, not [f for f in os.listdir(td) if f.endswith((".tmp", ".zip"))],
              "остались временные файлы: %r" % os.listdir(td))
    # valid не распознан — возраст считается от даты выгрузки (27.09), а не от сборки
    with tempfile.TemporaryDirectory() as td:
        rzn.refresh(cache_dir=td, получить=получатель(valid="нет"))
        индекс = rzn._прочитать(os.path.join(td, rzn.ИНДЕКС))
        check(errors, not rzn.устарел(индекс, "2026-10-11")
              and len(rzn.устарел(индекс, "2026-10-12")) == 3,
              "свежесть без valid: %r" % rzn.устарел(индекс, "2026-10-12"))
    # первый refresh с пустым набором (прежнего индекса нет) — индекс не создаётся
    with tempfile.TemporaryDirectory() as td:
        res = rzn.refresh(cache_dir=td, получить=получатель(
            выгрузки={"md_licenses": выгрузка()}))
        check(errors, res["статус"].startswith("индекс не обновлён")
              and not os.path.exists(os.path.join(td, rzn.ИНДЕКС)),
              "пустой набор без прежнего индекса: %r" % res["статус"])
    # чужой домен в meta.csv — отказ до запроса
    try:
        rzn._get("https://example.test/data.zip", 10)
        errors.append("скачан адрес вне открытых данных Росздравнадзора")
    except ValueError:
        pass
    return errors


def case_движок():
    """Блок движка: ok / пусто с оговоркой / «не проверено» с причиной кэш:."""
    errors = []
    дом = os.environ.get("HOME")
    with tempfile.TemporaryDirectory() as td:
        os.environ["HOME"] = td
        try:
            fc = load("fetch_counterparty")
            данные, av = fc.fetch_rzn_licenses(None, "7700000001")
            check(errors, данные is None and av["состояние"] == "не проверено"
                  and av["причина"].startswith("кэш:"), "нет индекса: %r" % av)
            rzn = load("rzn_licenses")
            rzn.refresh(cache_dir=rzn.CACHE_DIR, получить=получатель(valid="20991231"))
            данные, av = fc.fetch_rzn_licenses(None, "7700000001")
            check(errors, av["состояние"] == "ok" and данные["лицензии"][0]["номер"] == "Л-1",
                  "ok: %r %r" % (av, данные))
            данные, av = fc.fetch_rzn_licenses(None, "7700000009")
            check(errors, данные is None and av["состояние"] == "пусто"
                  and "не значит, что лицензии" in av["причина"], "пусто: %r" % av)
            check(errors, "лицензии_рзн" in fc.FETCHERS and "лицензии_рзн" in fc.SOURCES,
                  "источник не зарегистрирован")
            # снимок: смена одной лишь даты выгрузки не меняет хеш остального
            snap = load("snapshot")
            данные, av = fc.fetch_rzn_licenses(None, "7700000001")
            а = {"инн": "7700000001", "лицензии_рзн": данные,
                 "_доступность": {"лицензии_рзн": av}}
            б = json.loads(json.dumps(а, ensure_ascii=False))
            б["лицензии_рзн"]["дата_выгрузки"] = {k: "2026-10-04"
                                                  for k in б["лицензии_рзн"]["дата_выгрузки"]}
            check(errors, snap.хеш_остального(а) == snap.хеш_остального(б),
                  "дата выгрузки попала в хеш снимка")
            б["лицензии_рзн"]["лицензии"][0]["приостановка"] = "Приостановлено:  от 2026-09-01"
            check(errors, snap.хеш_остального(а) != snap.хеш_остального(б),
                  "изменение лицензии не видно в хеше снимка")
            # устаревший индекс: «записи нет» — не «пусто»
            rzn.refresh(cache_dir=rzn.CACHE_DIR, получить=получатель(valid="20200101"))
            данные, av = fc.fetch_rzn_licenses(None, "7700000009")
            check(errors, данные is None and av["состояние"] == "не проверено"
                  and av["причина"].startswith("кэш:") and "устарела" in av["причина"],
                  "устаревший индекс, записи нет: %r" % av)
            данные, av = fc.fetch_rzn_licenses(None, "7700000001")
            check(errors, av["состояние"] == "ok" and данные["предупреждения"],
                  "устаревший индекс, запись есть: %r" % av)
        finally:
            if дом is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = дом
    return errors


def main():
    rzn = load("rzn_licenses")
    cases = {
        "состояния-лицензий": case_состояния(rzn),
        "частичное-обновление": case_частичное_обновление(rzn),
        "блок-движка": case_движок(),
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
