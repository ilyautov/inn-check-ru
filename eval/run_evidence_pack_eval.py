#!/usr/bin/env python3
"""
run_evidence_pack_eval.py — офлайн-eval пакета доказательств
(scripts/evidence_pack.py и --пакет в fetch_counterparty.py). Сети не требует.

Что проверяется:
  * пакет из синтетических ответов проходит проверку, сырые байты лежат
    байт в байт, индекс и манифест согласованы, тело POST сохранено;
  * любая подмена ловится: правка сырого файла, правка манифеста, удалённый
    файл, подброшенный файл, правка индекса сырья, путь из манифеста за
    пределы каталога, символическая ссылка наружу;
  * пакет не пишется в непустой каталог (смешанный пакет не проверить);
  * разбор настоящих ответов TSA (freetsa.org, DigiCert; eval/fixtures/tsa/)
    сходится со значениями, которые независимо показал `openssl ts -reply -text`:
    хеш, время, nonce, серийный номер;
  * штамп не записывается, если TSA ответил на чужой запрос или недоступен —
    и пакет остаётся годным без него; чужой штамп в пакете ловится проверкой;
  * ключи в query-строке URL маскируются до записи в пакет;
  * CLI: --пакет пишет пакет из того, что движок записал в _http_get/_http_post,
    а непустой каталог отклоняется ДО сбора (сеть не тратится);
  * досье --пакет: целостность названа честно (изменённый пакет — «НЕ
    подтверждена»), чужой --fetch пойман, критерий осмотрительности без
    данных источников назван «не подтверждён», а не пропущен.
"""

import io
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import evidence_pack as ep
import fetch_counterparty as fc

TSA = ROOT / "eval" / "fixtures" / "tsa"

# Независимо: `openssl ts -reply -in <tsr> -text` (OpenSSL 3.6.3, 26.09.2026).
ОЖИДАЕМО_TSA = {
    "freetsa": {
        "хеш_hex": "f4497a91f262cbc1b577adae9d6aa51061f62599a7e1efe74f7a2f0e322ece1d",
        "время_utc": "2026-09-26T14:03:12+00:00",
        "nonce": 0x29D7D94428C8A2C0,
        "серийный": 0x087C906F,
    },
    "digicert": {
        "хеш_hex": "f4497a91f262cbc1b577adae9d6aa51061f62599a7e1efe74f7a2f0e322ece1d",
        "время_utc": "2026-09-26T14:03:28+00:00",
        "nonce": 0x3AEE5301ACE723AE,
        "серийный": 0x0B39A2964F3C2967D84E4E0D97EEF0AE,
    },
}

ОТВЕТЫ = [
    {"источник": "егрюл", "метод": "POST", "url": "https://egrul.nalog.ru/",
     "статус": 200, "время_utc": "2026-09-26T10:00:00+00:00",
     "данные": b'{"t":"abc"}', "тело_запроса": b"query=7707083893"},
    {"источник": "риски (канарейка 7707083893)", "метод": "GET",
     "url": "https://pb.nalog.ru/x", "статус": 403,
     "время_utc": "2026-09-26T10:00:01+00:00", "данные": b"", "тело_запроса": None},
    {"источник": "фин", "метод": "GET", "url": "https://bo.nalog.ru/y",
     "статус": 200, "время_utc": "2026-09-26T10:00:02+00:00",
     "данные": bytes(range(256)), "тело_запроса": None},
]


def check(errors, cond, msg):
    if not cond:
        errors.append(msg)


def _пакет(tmp, имя="p"):
    каталог = Path(tmp) / имя
    ep.записать_пакет(каталог, "7707083893", {"егрюл": {"статус": "Действует"}}, ОТВЕТЫ)
    return каталог


def case_годный_пакет(tmp):
    e = []
    каталог = _пакет(tmp)
    итог = ep.проверить(каталог)
    check(e, итог["годен"], "свежий пакет не прошёл проверку: %s" % итог["ошибки"])
    индекс = json.loads((каталог / "raw" / "index.json").read_text(encoding="utf-8"))
    check(e, len(индекс) == 3, "в индексе %d записей вместо 3" % len(индекс))
    for запись, ответ in zip(индекс, ОТВЕТЫ):
        check(e, (каталог / запись["файл"]).read_bytes() == ответ["данные"],
              "сырые байты не сохранены как есть: %s" % запись["файл"])
        check(e, запись["статус"] == ответ["статус"] and запись["url"] == ответ["url"],
              "метаданные ответа потеряны: %s" % запись)
    check(e, (каталог / индекс[0]["запрос"]["файл"]).read_bytes() == b"query=7707083893",
          "тело POST не сохранено")
    check(e, "канарейка" in (индекс[1]["источник"] or ""),
          "канарейка не отличима от запроса по контрагенту")
    манифест = json.loads((каталог / ep.МАНИФЕСТ).read_text(encoding="utf-8"))
    пути = {ф["путь"] for ф in манифест["файлы"]}
    check(e, {"fetch.json", "raw/index.json"} <= пути, "манифест не покрывает fetch/индекс")
    check(e, "63-ФЗ" in манифест["оговорка"], "оговорка о статусе штампа пропала")
    check(e, (каталог / ep.ИНСТРУКЦИЯ).exists(), "нет инструкции по проверке")
    return e


def _подмена(tmp, имя, как, ждём):
    e = []
    каталог = _пакет(tmp, имя)
    как(каталог)
    итог = ep.проверить(каталог)
    check(e, not итог["годен"], "подмена «%s» не поймана" % имя)
    check(e, any(ждём in x for x in итог["ошибки"]),
          "подмена «%s»: нет ошибки «%s» в %s" % (имя, ждём, итог["ошибки"]))
    return e


def case_подмены(tmp):
    def правка_сырья(к):
        f = к / "raw" / "003_фин.bin"
        f.write_bytes(f.read_bytes()[:-1] + b"\x00")

    def правка_манифеста(к):
        f = к / ep.МАНИФЕСТ
        f.write_text(f.read_text(encoding="utf-8").replace("7707083893", "7707083894"),
                     encoding="utf-8")

    def правка_индекса(к):
        f = к / "raw" / "index.json"
        idx = json.loads(f.read_text(encoding="utf-8"))
        idx[0]["sha256"] = "0" * 64
        f.write_text(json.dumps(idx), encoding="utf-8")

    def согласованная_подмена(к):
        # Сырьё, строка манифеста и manifest.sha256 переписаны согласованно —
        # остаётся только индекс сырья со старым хешем.
        правка_сырья(к)
        f = к / ep.МАНИФЕСТ
        м = json.loads(f.read_text(encoding="utf-8"))
        for ф in м["файлы"]:
            if ф["путь"] == "raw/003_фин.bin":
                ф["sha256"] = ep._sha256((к / ф["путь"]).read_bytes())
        байты = (json.dumps(м, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        f.write_bytes(байты)
        (к / ep.ХЕШ_МАНИФЕСТА).write_text("%s  %s\n" % (ep._sha256(байты), ep.МАНИФЕСТ))

    def путь_наружу(к):
        # Файл вне пакета + манифест, согласованно ссылающийся на него.
        наружу = к.parent / "снаружи.bin"
        наружу.write_bytes(b"x")
        f = к / ep.МАНИФЕСТ
        м = json.loads(f.read_text(encoding="utf-8"))
        м["файлы"].append({"путь": "../снаружи.bin", "sha256": ep._sha256(b"x"), "байт": 1})
        м["файлы"].append({"путь": str(наружу.resolve()), "sha256": ep._sha256(b"x"),
                           "байт": 1})
        байты = (json.dumps(м, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        f.write_bytes(байты)
        (к / ep.ХЕШ_МАНИФЕСТА).write_text("%s  %s\n" % (ep._sha256(байты), ep.МАНИФЕСТ))

    def симлинк(к):
        f = к / "raw" / "003_фин.bin"
        (к.parent / "цель.bin").write_bytes(f.read_bytes())
        f.unlink()
        f.symlink_to(к.parent / "цель.bin")

    e = []
    e += _подмена(tmp, "путь наружу", путь_наружу, "недопустимый путь в манифесте: ../снаружи.bin")
    e += _подмена(tmp, "абсолютный путь", путь_наружу, "недопустимый путь в манифесте: /")
    e += _подмена(tmp, "симлинк", симлинк, "raw/003_фин.bin")
    e += _подмена(tmp, "согласованная", согласованная_подмена,
                  "индекс сырья расходится с манифестом: raw/003_фин.bin")
    e += _подмена(tmp, "сырьё", правка_сырья, "изменён файл raw/003_фин.bin")
    e += _подмена(tmp, "манифест", правка_манифеста, ep.ХЕШ_МАНИФЕСТА)
    e += _подмена(tmp, "удаление", lambda к: (к / "fetch.json").unlink(), "нет файла fetch.json")
    e += _подмена(tmp, "подброс", lambda к: (к / "raw" / "x.bin").write_bytes(b"1"),
                  "файл не из манифеста: raw/x.bin")
    # Индекс в манифесте — его правка ловится и как «изменён файл».
    e += _подмена(tmp, "индекс", правка_индекса, "raw/index.json")
    return e


def case_непустой_каталог(tmp):
    e = []
    каталог = Path(tmp) / "занят"
    каталог.mkdir()
    (каталог / "старое.txt").write_text("x")
    check(e, ep.каталог_годится(каталог) is not None, "непустой каталог принят")
    try:
        ep.записать_пакет(каталог, "7707083893", {}, ОТВЕТЫ)
        e.append("записать_пакет записал в непустой каталог")
    except ValueError:
        pass
    check(e, ep.каталог_годится(Path(tmp) / "новый") is None, "новый каталог отклонён")
    return e


def case_разбор_tsa(tmp):
    e = []
    for имя, ждём in ОЖИДАЕМО_TSA.items():
        р = ep.разобрать_ответ((TSA / (имя + ".tsr")).read_bytes())
        check(e, р["статус"] == 0, "%s: статус %s" % (имя, р["статус"]))
        for поле, значение in ждём.items():
            check(e, р[поле] == значение, "%s: %s = %r, openssl показал %r"
                  % (имя, поле, р[поле], значение))
        check(e, ep._nonce_запроса((TSA / (имя + ".tsq")).read_bytes()) == ждём["nonce"],
              "%s: nonce запроса не разобран" % имя)
    # Собранный нами запрос — тот же DER, что лежит в фикстуре.
    з = ep.запрос_штампа(ОЖИДАЕМО_TSA["freetsa"]["хеш_hex"], ОЖИДАЕМО_TSA["freetsa"]["nonce"])
    check(e, з == (TSA / "freetsa.tsq").read_bytes(), "DER запроса расходится с фикстурой")
    try:
        ep.разобрать_ответ(b"\x30\x05\x02\x01")
        e.append("обрезанный DER принят")
    except ValueError:
        pass
    return e


def case_штамп_чужой_и_отказ(tmp):
    e = []
    каталог = _пакет(tmp, "штамп")
    чужой = (TSA / "freetsa.tsr").read_bytes()
    итог = ep.поставить_штамп(каталог, "https://tsa.example/", post=lambda url, tsq: чужой)
    check(e, итог.startswith("не поставлен") and "не на наш запрос" in итог,
          "чужой штамп принят: %s" % итог)
    check(e, not (каталог / ep.ОТВЕТ_TSA).exists(), "чужой штамп записан в пакет")

    def недоступен(url, tsq):
        raise OSError("timed out")
    итог = ep.поставить_штамп(каталог, "https://a/,https://b/", post=недоступен)
    check(e, итог.startswith("не поставлен") and "https://b/" in итог,
          "отказ всех TSA не назван поимённо: %s" % итог)
    check(e, ep.проверить(каталог)["годен"], "пакет без штампа перестал быть годным")

    (каталог / ep.ОТВЕТ_TSA).write_bytes(чужой)
    итог = ep.проверить(каталог)
    check(e, not итог["годен"] and any("другой манифест" in x for x in итог["ошибки"]),
          "подброшенный чужой штамп не пойман: %s" % итог["ошибки"])
    check(e, ep._список_tsa("да") == list(ep.TSA_ПО_УМОЛЧАНИЮ), "«да» не даёт TSA по умолчанию")

    # Битый DER от первого TSA не роняет сбор: идём к запасному.
    tsr_freetsa = (TSA / "freetsa.tsr").read_bytes()
    вызваны = []

    def битый_потом_чужой(url, tsq):
        вызваны.append(url)
        return b"\x30\x00" if len(вызваны) == 1 else tsr_freetsa
    каталог2 = _пакет(tmp, "штамп2")
    итог = ep.поставить_штамп(каталог2, "https://a/,https://b/", post=битый_потом_чужой)
    check(e, len(вызваны) == 2, "после битого DER запасной TSA не вызван: %s" % вызваны)
    check(e, "https://a/: RFC 3161" in итог, "битый DER не назван причиной: %s" % итог)
    for мусор in (b"\x30\x00", b"\x30\x03\x30\x01\x02", b"", b"\x04\x01\x00"):
        try:
            ep.разобрать_ответ(мусор)
            e.append("мусорный DER %r принят" % мусор)
        except ValueError:
            pass
    (каталог2 / ep.ОТВЕТ_TSA).write_bytes(b"\x30\x00")
    итог = ep.проверить(каталог2)
    check(e, not итог["годен"] and any("штамп не разбирается" in x for x in итог["ошибки"]),
          "битый .tsr в пакете: %s" % итог["ошибки"])

    # Секреты в URL TSA не попадают в итог.
    итог = ep.поставить_штамп(каталог2, "https://user:pass@tsa.example:8443/ts?api_key=S",
                              post=недоступен)
    check(e, "pass" not in итог and "api_key" not in итог and "tsa.example:8443/ts" in итог,
          "секрет URL TSA в выводе: %s" % итог)
    return e


def case_маска_секретов(tmp):
    e = []
    for url, ждём in (
            ("https://api.checko.ru/v2/company?key=SECRET&inn=1",
             "https://api.checko.ru/v2/company?key=***&inn=1"),
            ("https://x/?inn=1&api_key=S&token=T", "https://x/?inn=1&api_key=***&token=***"),
            ("https://x/?inn=1", "https://x/?inn=1")):
        check(e, fc._без_секретов(url) == ждём, "маска: %r -> %r" % (url, fc._без_секретов(url)))
    return e


class _Ответ:
    status = 200

    def __init__(self, данные):
        self._д = данные

    def read(self):
        return self._д


class _Opener:
    def open(self, req, timeout=None):
        if "geo" in req.full_url:
            import urllib.error
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {},
                                         io.BytesIO(b"<h1>geo block</h1>"))
        return _Ответ(b'{"ok":1}')


def case_cli_пакет(tmp):
    e = []
    сохранено = fc.collect
    вызовов = []

    def collect(inn, профиль=None, режим="полный", opener=None):
        вызовов.append(inn)
        fc._ИСТОЧНИК.id = "егрюл"
        fc._http_get(_Opener(), "https://egrul.nalog.ru/search-result/t?key=SECRET")
        fc._http_get(_Opener(), "https://pb.nalog.ru/geo")
        fc._ИСТОЧНИК.id = None
        return {"егрюл": {"статус": "Действует"}}

    out = sys.stdout
    try:
        fc.collect = collect
        sys.stdout = io.StringIO()
        каталог = Path(tmp) / "cli"
        код = fc.main(["x", "7707083893", "--пакет", str(каталог)])
        check(e, код == 0, "main вернул %s" % код)
        итог = ep.проверить(каталог)
        check(e, итог["годен"], "пакет из CLI не годен: %s" % итог["ошибки"])
        индекс = json.loads((каталог / "raw" / "index.json").read_text(encoding="utf-8"))
        check(e, len(индекс) == 2 and индекс[0]["источник"] == "егрюл",
              "запрос движка не попал в пакет с id источника: %s" % индекс)
        check(e, len(индекс) == 2 and индекс[1]["статус"] == 403
              and (каталог / индекс[1]["файл"]).read_bytes() == b"<h1>geo block</h1>",
              "тело HTTP-ошибки (страница геоблока) не сохранено: %s" % индекс[1:])
        check(e, "SECRET" not in (каталог / "raw" / "index.json").read_text(encoding="utf-8"),
              "ключ из URL попал в пакет")
        check(e, not fc._ЗАПИСЬ["включена"], "запись не выключена после сбора")

        код = fc.main(["x", "7707083893", "--пакет=%s" % каталог])
        check(e, код == 2, "непустой каталог не отклонён (код %s)" % код)
        check(e, len(вызовов) == 1, "сбор запущен, хотя каталог непригоден")
        код = fc.main(["x", "7707083893", "--пакет"])
        check(e, код == 2 and len(вызовов) == 1, "--пакет без каталога не отклонён (код %s)" % код)
    finally:
        fc.collect = сохранено
        sys.stdout = out
        fc._ЗАПИСЬ["включена"] = False
    check(e, fc._parse_args(["x", "1", "--пакет", "d", "--прокси", "p"])[-2:] == ("d", "p"),
          "разбор --пакет")
    return e


def _досье(*args):
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "dossier.py"), *map(str, args)],
                       capture_output=True, text=True, check=False)
    return r.returncode, r.stdout, r.stderr


def case_досье(tmp):
    e = []
    каталог = _пакет(tmp, "досье")
    код, out, err = _досье("--пакет", каталог)
    check(e, код == 0, "dossier --пакет: код %s, %s" % (код, err.strip()))
    check(e, "Хеши пакета:** согласованы" in out, "согласованный пакет не назван согласованным")
    check(e, "неизменность после сбора НЕ доказана" in out,
          "пакет без штампа выдан за доказательство неизменности")
    check(e, "да — fetch.json пакета" in out, "не сказано, что досье по fetch.json пакета")
    check(e, "Штамп времени RFC 3161:** не ставился" in out, "отсутствие штампа не названо")
    for критерий in ("Деловая репутация", "Возможность исполнения обязательств",
                     "Платёжеспособность"):
        check(e, "| %s | открытыми источниками не подтверждён |" % критерий in out,
              "критерий «%s» без данных источников не назван неподтверждённым" % критерий)
    check(e, "БВ-4-7/3060@" in out, "нет ссылки на письмо ФНС БВ-4-7/3060@")
    check(e, "ЕД-4-2/13650@" not in out,
          "тройка критериев приписана ЕД-4-2/13650@ (письмо об умысле)")

    чужой = Path(tmp) / "чужой.json"
    чужой.write_text('{"егрюл": {"статус": "Ликвидирована"}}', encoding="utf-8")
    код, out, _ = _досье("--пакет", каталог, "--fetch", чужой)
    check(e, "НЕТ — --fetch не совпадает" in out, "чужой --fetch при пакете не пойман")

    (каталог / "fetch.json").write_text("{}", encoding="utf-8")
    код, out, _ = _досье("--пакет", каталог)
    check(e, "НЕ согласованы" in out and "изменён файл fetch.json" in out,
          "изменённый пакет в досье выдан за целый")
    return e


КЕЙСЫ = [
    ("годный пакет: сырьё байт в байт, индекс и манифест согласованы", case_годный_пакет),
    ("подмены ловятся: сырьё, манифест, удаление, подброс, индекс", case_подмены),
    ("непустой каталог отклоняется", case_непустой_каталог),
    ("разбор настоящих штампов TSA сходится с openssl", case_разбор_tsa),
    ("штамп: чужой и недоступный TSA не записываются", case_штамп_чужой_и_отказ),
    ("ключи в URL маскируются", case_маска_секретов),
    ("CLI --пакет: пакет из трафика движка, каталог проверяется до сбора", case_cli_пакет),
    ("досье: блок пакета и критерии осмотрительности без выдумок", case_досье),
]


def main():
    всего = failed = 0
    with tempfile.TemporaryDirectory() as tmp:
        for имя, fn in КЕЙСЫ:
            всего += 1
            ошибки = fn(tmp)
            print(("FAIL %s" if ошибки else "PASS %s") % имя)
            for x in ошибки:
                print("  - %s" % x)
            failed += bool(ошибки)
    if failed:
        print("FAIL: %d/%d кейсов упало" % (failed, всего))
        return 1
    print("PASS: все %d кейсов зелёные" % всего)
    return 0


if __name__ == "__main__":
    sys.exit(main())
