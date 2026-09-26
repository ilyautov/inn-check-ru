#!/usr/bin/env python3
"""
run_cbr_warning_eval.py — офлайн-eval списка ЦБ с признаками нелегальной
деятельности на финрынке (scripts/cbr_warning.py): разбор реальных записей
выгрузки 26.09.2026, дрейф схемы, обновление кэша без затирания годного,
устаревший и подменённый кэш, состояния блока движка и сигнал профиля.
PASS/FAIL, чистый stdlib, гоняется в CI.
"""

import copy
import datetime
import importlib.util
import io
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "eval" / "fixtures" / "cbr_warning" / "sample.json"
СЕЙЧАС = datetime.datetime(2026, 9, 26, 12, 0, tzinfo=datetime.timezone.utc)


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check(errors, cond, msg):
    if not cond:
        errors.append(msg)


class _Ответ(io.BytesIO):
    def __init__(self, тело, status=200):
        super().__init__(тело)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class _Opener:
    """Подставной opener: отдаёт заданное тело и считает обращения."""

    def __init__(self, тело, status=200):
        self.тело, self.status, self.вызовов = тело, status, 0

    def open(self, запрос, timeout=None):
        self.вызовов += 1
        return _Ответ(self.тело, self.status)


def _образец():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _кэш(cw, td, данные=None, now=СЕЙЧАС):
    тело = json.dumps(данные or _образец(), ensure_ascii=False).encode("utf-8")
    итог = cw.refresh(cache_dir=td, opener=_Opener(тело), now=now)
    return итог


def case_разбор_и_поиск(cw, td):
    errors = []
    итог = _кэш(cw, td)
    check(errors, итог.get("записано") and итог["записей"] == 7 and итог["с_инн"] == 4,
          "обновление по образцу: %r" % итог)
    рез, прич = cw.lookup("4800031693", cache_dir=td, now=СЕЙЧАС)
    check(errors, рез and рез["в_списке"] is True and "нелегального кредитора" in
          рез["формулировка"] and "в списке с 2026-09-24" in рез["формулировка"]
          and "мошенн" not in рез["формулировка"].lower(),
          "действующая запись: %r %r" % (рез, прич))
    рез, _ = cw.lookup("9102262964", cache_dir=td, now=СЕЙЧАС)
    check(errors, рез["в_списке"] is True and рез["записи"][0]["ликвидирована_по_данным_цб"]
          and "ликвидированной" in рез["формулировка"], "ликвидированная: %r" % рез)
    рез, _ = cw.lookup("1901135037", cache_dir=td, now=СЕЙЧАС)
    check(errors, рез["в_списке"] is None and рез["записи"][0]["клон"]
          and "не признак против неё" in рез["формулировка"], "клон: %r" % рез)
    рез, _ = cw.lookup("5904406758", cache_dir=td, now=СЕЙЧАС)
    check(errors, len(рез["записи"][0]["признаки"]) == 2,
          "два признака в одной строке не разделены: %r" % рез["записи"][0]["признаки"])
    рез, _ = cw.lookup("7707083893", cache_dir=td, now=СЕЙЧАС)
    check(errors, рез["в_списке"] is False and рез["записи"] == []
          and "не значит, что компании в списке нет" in рез["покрытие"],
          "нет совпадения без оговорки о покрытии: %r" % рез)
    # интернет-проекты без ИНН не совпадают ни с чем, в т.ч. с пустой строкой
    рез, прич = cw.lookup("504110181262", cache_dir=td, now=СЕЙЧАС)
    check(errors, рез is None and прич.startswith("не покрыто:"), "ИП: %r" % прич)
    # если ЦБ начнёт заполнять ИНН ИП, сверка включится сама (ИНН выдуман)
    с_ип = _образец()
    с_ип["RC"][3]["INN"] = "771234567890"
    with tempfile.TemporaryDirectory() as td2:
        _кэш(cw, td2, с_ип)
        рез, прич = cw.lookup("771234567890", cache_dir=td2, now=СЕЙЧАС)
        check(errors, рез and рез["в_списке"] is not False, "ИП в списке не найден: %r" % прич)
    try:
        cw.lookup("", cache_dir=td, now=СЕЙЧАС)
        errors.append("пустой ИНН принят")
    except ValueError:
        pass
    return errors


def case_дрейф_схемы(cw, td):
    errors = []
    образец = _образец()

    def с(правка):
        д = copy.deepcopy(образец)
        правка(д)
        return json.dumps(д, ensure_ascii=False)
    for имя, тело in (
            ("нет RC", с(lambda д: д.pop("RC"))),
            ("RC не список", с(lambda д: д.__setitem__("RC", {}))),
            ("пропало поле DT", с(lambda д: д["RC"][0].pop("DT"))),
            ("пропало поле INN", с(lambda д: д["RC"][0].pop("INN"))),
            ("пропало поле Comment", с(lambda д: д["RC"][2].pop("Comment"))),
            ("Comment числом", с(lambda д: д["RC"][2].__setitem__("Comment", 1))),
            ("Closed строкой", с(lambda д: д["RC"][1].__setitem__("Closed", "true"))),
            ("Id строкой", с(lambda д: д["RC"][1].__setitem__("Id", "45328"))),
            ("Id булев", с(lambda д: д["RC"][1].__setitem__("Id", True))),
            ("ИНН с дефисом", с(lambda д: д["RC"][0].__setitem__("INN", "48-00031693"))),
            ("ИНН 9 цифр", с(lambda д: д["RC"][0].__setitem__("INN", "480003169"))),
            ("дата не ISO", с(lambda д: д["RC"][0].__setitem__("DT", "24.09.2026"))),
            ("ЦБ перестал отдавать ИНН",
             с(lambda д: [r.__setitem__("INN", None) for r in д["RC"]])),
            ("не JSON", "<html>403</html>")):
        try:
            cw.разобрать(тело)
            errors.append("дрейф «%s» принят" % имя)
        except cw.СхемаНеТа:
            pass
    стар = cw.МИН_ЗАПИСЕЙ
    cw.МИН_ЗАПИСЕЙ = 10000
    try:
        cw.разобрать(json.dumps(образец))
        errors.append("обрезанная выгрузка (7 записей) принята при пороге 10000")
    except cw.СхемаНеТа:
        pass
    finally:
        cw.МИН_ЗАПИСЕЙ = стар
    return errors


def case_обновление(cw, td):
    errors = []
    итог = _кэш(cw, td)
    файл = Path(td) / json.loads((Path(td) / cw.МЕТА).read_text(encoding="utf-8"))["файл"]
    было = файл.read_bytes()
    for имя, opener in (
            ("битая схема", _Opener(b'{"RC": [{"Id": 1}]}')),
            ("HTTP 500", _Opener(json.dumps(_образец()).encode(), status=500)),
            ("пустой ответ", _Opener(b""))):
        итог = cw.refresh(cache_dir=td, opener=opener, now=СЕЙЧАС)
        check(errors, not итог["записано"] and файл.read_bytes() == было,
              "%s затёр кэш: %r" % (имя, итог))

    class Падает:
        def open(self, *a, **k):
            raise OSError("timed out")
    итог = cw.refresh(cache_dir=td, opener=Падает(), now=СЕЙЧАС)
    check(errors, not итог["записано"] and итог["причина"].startswith("сеть:"),
          "сетевой сбой: %r" % итог)
    op = _Opener(json.dumps(_образец()).encode())
    cw.refresh(cache_dir=td, opener=op, now=СЕЙЧАС)
    check(errors, op.вызовов == 1, "обновление — не один запрос: %d" % op.вызовов)
    # та же выгрузка дважды — два разных поколения: очистка одного --refresh
    # не удалит тело, на которое уже указывает meta другого
    с_первым = json.loads((Path(td) / cw.МЕТА).read_text(encoding="utf-8"))["файл"]
    cw.refresh(cache_dir=td, opener=_Opener(json.dumps(_образец()).encode()), now=СЕЙЧАС)
    мета = json.loads((Path(td) / cw.МЕТА).read_text(encoding="utf-8"))
    check(errors, мета["файл"] != с_первым and (Path(td) / с_первым).exists()
          and cw.lookup("4800031693", cache_dir=td, now=СЕЙЧАС)[0] is not None,
          "одинаковая выгрузка пишется в то же поколение: %r" % мета["файл"])
    return errors


def case_кэш(cw, td):
    errors = []
    рез, прич = cw.lookup("4800031693", cache_dir=td, now=СЕЙЧАС)
    check(errors, рез is None and прич.startswith("кэш:") and "--refresh" in прич,
          "нет кэша: %r" % прич)
    _кэш(cw, td, now=СЕЙЧАС - datetime.timedelta(days=cw.TTL_ДНЕЙ + 1))
    рез, прич = cw.lookup("4800031693", cache_dir=td, now=СЕЙЧАС)
    check(errors, рез is None and прич.startswith("кэш:") and "дн." in прич,
          "устаревший кэш дал ответ: %r %r" % (рез, прич))
    _кэш(cw, td, now=СЕЙЧАС - datetime.timedelta(days=cw.TTL_ДНЕЙ, hours=23))
    рез, прич = cw.lookup("4800031693", cache_dir=td, now=СЕЙЧАС)
    check(errors, рез is None and прич.startswith("кэш:"),
          "кэш 14 дн. 23 ч принят: %r" % прич)
    _кэш(cw, td, now=СЕЙЧАС + datetime.timedelta(days=2))
    рез, прич = cw.lookup("4800031693", cache_dir=td, now=СЕЙЧАС)
    check(errors, рез is None and "в будущем" in str(прич), "кэш из будущего: %r" % прич)
    _кэш(cw, td, now=СЕЙЧАС - datetime.timedelta(days=cw.TTL_ДНЕЙ))
    рез, _ = cw.lookup("4800031693", cache_dir=td, now=СЕЙЧАС)
    check(errors, рез is not None, "кэш ровно на пороге TTL отвергнут")
    # подмена файла выгрузки мимо refresh: запись вычеркнута
    д = _образец()
    д["RC"] = [r for r in д["RC"] if r["INN"] != "4800031693"]
    файл = Path(td) / json.loads((Path(td) / cw.МЕТА).read_text(encoding="utf-8"))["файл"]
    файл.write_text(json.dumps(д, ensure_ascii=False), encoding="utf-8")
    рез, прич = cw.lookup("4800031693", cache_dir=td, now=СЕЙЧАС)
    check(errors, рез is None and "не совпадает с meta.json" in прич,
          "подменённая выгрузка дала «нет совпадения»: %r %r" % (рез, прич))
    return errors


def case_движок_и_профиль(cw, fc, pf, td):
    errors = []
    _кэш(cw, td)
    orig = cw.CACHE_DIR
    orig_now = cw._сейчас
    cw.CACHE_DIR = Path(td)
    cw._сейчас = lambda: СЕЙЧАС
    orig_load = fc._load_sibling
    fc._load_sibling = lambda name: cw if name == "cbr_warning" else orig_load(name)
    try:
        ожидаем = {"4800031693": ("ok", True), "1901135037": ("не проверено", None),
                   "7707083893": ("пусто", None), "504110181262": ("не проверено", None)}
        блоки = {}
        for инн, (сост, в_списке) in ожидаем.items():
            данные, av = fc.fetch_cbr_warning(None, инн)
            блоки[инн] = (данные, av)
            check(errors, av["состояние"] == сост and
                  ((данные or {}).get("в_списке") if сост != "пусто" else None) == в_списке,
                  "%s: %s %r" % (инн, av, данные and данные.get("в_списке")))
            if сост == "не проверено":
                check(errors, str(av.get("причина")).startswith("не покрыто:"),
                      "%s: причина без префикса: %r" % (инн, av.get("причина")))
        check(errors, "покрытие" in "".join(str(блоки["7707083893"][1].get("причина"))) or
              "не значит" in str(блоки["7707083893"][1].get("причина")),
              "«пусто» без оговорки о покрытии: %r" % блоки["7707083893"][1])
    finally:
        cw.CACHE_DIR, cw._сейчас, fc._load_sibling = orig, orig_now, orig_load

    # сигнал профиля: 🟡 по умолчанию, 🔴 для предоплаты и клиента по 115-ФЗ
    профили = pf.load()
    check(errors, "предупреждение_цб" in профили["сигналы"], "сигнала нет в каталоге")
    for профиль, ждём in (("тендер", "🟡"), ("предоплата", "🔴"),
                          ("клиент_115фз", "🔴"), ("подрядчик", "🟡")):
        fetch = {"инн": "4800031693", "тип": "юрлицо",
                 "список_цб": блоки["4800031693"][0],
                 "_доступность": {"список_цб": блоки["4800031693"][1]}}
        рез = pf.resolve(fetch, None, профиль)
        ф = {f["сигнал"]: f for f in рез["факты"]}.get("предупреждение_цб") or {}
        поднят = [x for x in рез.get("поднят_сигналами") or []
                  if "предупреждение_цб" in json.dumps(x, ensure_ascii=False)]
        check(errors, ф.get("статус") == "найден" and поднят
              and ждём in json.dumps(поднят, ensure_ascii=False),
              "%s: %r / %r" % (профиль, ф.get("статус"), рез.get("поднят_сигналами")))
    fetch = {"инн": "1901135037", "тип": "юрлицо", "список_цб": блоки["1901135037"][0],
             "_доступность": {"список_цб": блоки["1901135037"][1]}}
    ф = {f["сигнал"]: f for f in pf.resolve(fetch, None, "предоплата")["факты"]}["предупреждение_цб"]
    check(errors, ф.get("статус") == "не проверен" and "клон" not in str(ф.get("статус"))
          and "не признак против" in str(ф.get("текст")),
          "клон в профиле: %r" % ф)
    # «нет совпадения» доходит до досье с оговоркой о покрытии
    fetch = {"инн": "7707083893", "тип": "юрлицо", "список_цб": None,
             "_доступность": {"список_цб": блоки["7707083893"][1]}}
    ф = {f["сигнал"]: f for f in pf.resolve(fetch, None, "предоплата")["факты"]}["предупреждение_цб"]
    check(errors, ф.get("статус") == "отсутствует" and "не значит" in str(ф.get("оговорка")),
          "«пусто» без оговорки в факте: %r" % ф)
    путь = Path(td) / "fetch_цб_пусто.json"
    путь.write_text(json.dumps(fetch, ensure_ascii=False), encoding="utf-8")
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "dossier.py"), "--fetch",
                          str(путь), "--профиль", "предоплата"],
                         capture_output=True, text=True, check=False).stdout
    раздел = out.split("4.2.", 1)[-1].split("## ", 1)[0]
    check(errors, "оговорка источника" in раздел and "не значит" in раздел,
          "досье 4.2 без оговорки о покрытии списка ЦБ: %s" % раздел[:600])

    # снимок хранит формулировку, ретро того же снимка даёт тот же вердикт
    sn = load_module("snapshot", ROOT / "scripts" / "snapshot.py")
    rv = load_module("retro_verdict", ROOT / "scripts" / "retro_verdict.py")
    слепок = {"инн": "4800031693", "тип": "юрлицо", "дата": "2026-09-26",
              "список_цб": блоки["4800031693"][0],
              "_доступность": {"список_цб": блоки["4800031693"][1]}}
    вердикт = sn.вердиктные(слепок)
    check(errors, (вердикт.get("список_цб") or {}).get("формулировка"),
          "снимок не хранит формулировку: %r" % вердикт.get("список_цб"))
    отпечаток = {"инн": "4800031693", "дата": "2026-09-26", "версия_формата": 2,
                 "поля": {}, "вердикт": вердикт, "состояния": {"список_цб": "ok"}}
    восст, _ = rv.восстановить(отпечаток)
    живой = pf.resolve(слепок, None, "предоплата")
    ретро = pf.resolve(восст, None, "предоплата")
    check(errors, живой.get("поднят_сигналами") and
          [x.get("сигнал") for x in живой["поднят_сигналами"]] ==
          [x.get("сигнал") for x in ретро.get("поднят_сигналами") or []],
          "ретро ≠ живой: %r / %r" % (живой.get("поднят_сигналами"),
                                      ретро.get("поднят_сигналами")))
    # ретро «пусто» по снимку сохраняет оговорку о покрытии; причина «пусто»
    # без даты выгрузки — иначе каждое обновление кэша меняло бы хеш снимка
    отпечаток = {"инн": "7707083893", "дата": "2026-09-26", "версия_формата": 2,
                 "поля": {}, "вердикт": {}, "состояния": {"список_цб": "пусто"}}
    восст, _ = rv.восстановить(отпечаток)
    ф = {f["сигнал"]: f for f in pf.resolve(восст, None, "предоплата")["факты"]}.get(
        "предупреждение_цб") or {}
    check(errors, ф.get("статус") == "отсутствует" and "не значит" in str(ф.get("оговорка")),
          "ретро «пусто» без оговорки: %r" % ф)
    check(errors, not re.search(r"\d{4}-\d{2}-\d{2}|\d{3,}",
                                str(блоки["7707083893"][1].get("причина"))),
          "в причине «пусто» дата или счётчик выгрузки: %r" % блоки["7707083893"][1])
    return errors


def main():
    cw = load_module("cbr_warning", ROOT / "scripts" / "cbr_warning.py")
    fc = load_module("fetch_counterparty", ROOT / "scripts" / "fetch_counterparty.py")
    pf = load_module("profiles", ROOT / "scripts" / "profiles.py")
    cw.МИН_ЗАПИСЕЙ = 1  # образец — 7 реальных записей, а не весь список
    cw.МИН_С_ИНН = 1
    cases = {}
    for имя, f in (("разбор и поиск по реальным записям", lambda td: case_разбор_и_поиск(cw, td)),
                   ("дрейф схемы — отказ целиком", lambda td: case_дрейф_схемы(cw, td)),
                   ("обновление не затирает годный кэш", lambda td: case_обновление(cw, td)),
                   ("кэш: нет, устарел, подменён", lambda td: case_кэш(cw, td)),
                   ("движок и сигнал профиля", lambda td: case_движок_и_профиль(cw, fc, pf, td))):
        with tempfile.TemporaryDirectory() as td:
            cases[имя] = f(td)
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
