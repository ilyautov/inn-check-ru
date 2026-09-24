#!/usr/bin/env python3
"""
collect.py — сбор ретроспективной выборки для калибровки порогов (задача 5).

Инструмент сопровождающего, в пакет не входит. Только stdlib. Дизайн —
docs/superpowers/specs/2026-09-23-task5-threshold-calibration-design.md.

Стадии (каждая продолжает с места обрыва, всё сырое — в кэше):
    python3 collect.py когорта --n 15000   # фаза 1: случайные id ГИР БО с отчётностью за 2021
    python3 collect.py ефрсб [--следом]    # исход по каждому ИНН когорты (нужен прокси)
    python3 collect.py гирбо --контроли 3000   # фаза 2: детальные формы 2020–2021
    python3 collect.py статус

Почему случайный id, а не выдача поиска. Поиск ГИР БО не отдаёт глубже 10 000
результатов, а дробление по префиксу ОКВЭД покрыло только 46% из 2,33 млн
(у половины компаний код ровно равен префиксу — его не отделить), фильтр по
адресу — полнотекстовый и пересекается. Карточка же отчётности открывается по
числовому id организации; id выдаются до ~13,5 млн. Случайный id из [1, ВЕРХ_ID]
— равномерная выборка по всем организациям ГИР БО; условие «есть период 2021» —
та же когорта, что и у поиска, без смещения. Один запрос /bfo/ отдаёт и ИНН, и
периоды: это одновременно проверка и сырьё для фазы 2.

Прокси для ЕФРСБ — только из INN_CHECK_CALIBRATION_PROXY
(socks5://логин:пароль@host:port). В логи попадает маска, пароль — никогда.

Вежливость и честность:
  * общий темп на хост ограничен --rps (ГИР БО 3/с, ЕФРСБ 1.5/с по умолчанию),
    потоки только перекрывают задержку ответа, темп не растёт;
  * ЕФРСБ ответил не 200/JSON — стадия ОСТАНАВЛИВАЕТСЯ: это может быть защита от
    ботов, пробивать её повторами мы не будем (CONTRIBUTING: без обхода капч);
  * сетевой сбой — до 10 попыток с растущей паузой (~25 мин), затем остановка;
    5xx — три попытки; остальные ответы возвращаются как есть.
"""

import argparse
import concurrent.futures
import json
import os
import random
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import label
import socks5

КЭШ = os.environ.get("INN_CHECK_CALIBRATION_CACHE") or os.path.join(
    os.path.expanduser("~"), ".cache", "inn-check-ru", "calibration")
ГОД = label.ОТЧЁТНЫЙ_ГОД
ВЕРХ_ID = 14_000_000           # 13–20 млн: 12 из 12 проб пусты (23.09.2026)
UA = "Mozilla/5.0 (inn-check-ru calibration; +https://github.com/ilyautov/inn-check-ru)"
ГИРБО = "https://bo.nalog.gov.ru"
ЕФРСБ = "https://bankrot.fedresurs.ru"


class Стоп(Exception):
    """Источник ответил так, что продолжать нельзя (защита, схема, лимит)."""


def _путь(*части):
    p = os.path.join(КЭШ, *части)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def _читать_json(путь, default=None):
    try:
        with open(путь, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _писать_json(путь, obj):
    tmp = путь + ".tmp%d" % threading.get_ident()
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, путь)


class Клиент:
    """GET с общим на все потоки темпом и повторами только на сетевые сбои/5xx."""

    def __init__(self, opener, rps, referer):
        self.opener, self.шаг, self.referer = opener, 1.0 / rps, referer
        self._lock = threading.Lock()
        self._следующий = 0.0

    def _ждать_очередь(self):
        with self._lock:
            сейчас = time.monotonic()
            мой = max(сейчас, self._следующий)
            self._следующий = мой + self.шаг
        if мой > сейчас:
            time.sleep(мой - сейчас)

    СЕТЕВЫХ_ПОПЫТОК = 10     # обрыв Wi-Fi/прокси: ~25 минут терпения, потом стоп

    def get_json(self, url):
        сетевых = 0
        попытка = 0
        while True:
            self._ждать_очередь()
            req = urllib.request.Request(url, headers={
                "User-Agent": UA, "Accept": "application/json, text/plain, */*",
                "Referer": self.referer})
            try:
                with self.opener.open(req, timeout=40) as r:
                    status, body = r.status, r.read()
            except urllib.error.HTTPError as e:
                status, body = e.code, e.read() or b""
            except (urllib.error.URLError, OSError) as e:
                # сетевой сбой — не ответ источника: ждём дольше, но не вечно
                сетевых += 1
                if сетевых >= self.СЕТЕВЫХ_ПОПЫТОК:
                    raise Стоп("сеть: %s %s (%s)" % (type(e).__name__, e, url))
                time.sleep(min(300, 10 * 2 ** (сетевых - 1)))
                continue
            попытка += 1
            if status == 200:
                try:
                    return status, json.loads(body.decode("utf-8"))
                except ValueError:
                    raise Стоп("ответ не JSON (HTTP 200): %s" % url)
            if status >= 500 and попытка < 3:
                time.sleep(5 * попытка)
                continue
            return status, body[:300].decode("utf-8", "replace")


def гирбо_клиент(rps):
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return Клиент(op, rps, ГИРБО + "/")


def ефрсб_клиент(rps):
    url = os.environ.get("INN_CHECK_CALIBRATION_PROXY")
    if not url:
        raise Стоп("нет INN_CHECK_CALIBRATION_PROXY: ЕФРСБ отвечает только РФ-адресам")
    print("ЕФРСБ через %s" % socks5.маска(url), file=sys.stderr)
    return Клиент(socks5.opener(url), rps, ЕФРСБ + "/")


def _параллельно(fn, задачи, потоков):
    """fn по задачам в потоках; первая Стоп останавливает всё и поднимается."""
    стоп = threading.Event()

    def обёртка(x):
        if стоп.is_set():
            return None
        try:
            return fn(x)
        except Стоп:
            стоп.set()
            raise

    with concurrent.futures.ThreadPoolExecutor(потоков) as ex:
        for fut in concurrent.futures.as_completed([ex.submit(обёртка, x) for x in задачи]):
            fut.result()


# ---------------------------------------------------------------------------
# Фаза 1: когорта по случайным id
# ---------------------------------------------------------------------------

def последовательность_id(seed, верх=ВЕРХ_ID):
    """Детерминированная последовательность случайных id без повторов."""
    rng = random.Random(seed)
    виденные = set()
    while len(виденные) < верх:
        i = rng.randrange(1, верх + 1)
        if i not in виденные:
            виденные.add(i)
            yield i


def строка_когорты(id_орг, bfo):
    """Ответ /nbo/organizations/<id>/bfo/ -> строка когорты или None (нет отчётности за ГОД)."""
    if not isinstance(bfo, list) or not bfo:
        return None
    if not any(str(r.get("period")) == str(ГОД) for r in bfo if isinstance(r, dict)):
        return None
    info = next((r.get("organizationInfo") for r in bfo
                 if isinstance(r, dict) and isinstance(r.get("organizationInfo"), dict)), {})
    inn = str(info.get("inn") or "").strip()
    if not inn:
        return None
    return {"id": id_орг, "inn": inn, "shortName": info.get("fullName"),
            "okved2": info.get("okved2_id"), "okopf": info.get("okopf_id"), "bfo": bfo}


def когорта(cl, n, seed, потоков):
    путь_проб = _путь("probes.jsonl")
    пробы = {}
    if os.path.exists(путь_проб):
        with open(путь_проб, encoding="utf-8") as f:
            for line in f:
                r = json.loads(line)
                пробы[r["id"]] = r["итог"]
    в_когорте = sum(1 for v in пробы.values() if v == "когорта")
    lock = threading.Lock()
    with open(путь_проб, "a", encoding="utf-8") as out_p, \
            open(_путь("cohort.jsonl"), "a", encoding="utf-8") as out_c:
        _когорта_пробы(cl, n, seed, потоков, пробы, в_когорте, lock, out_p, out_c)
    open(_путь("cohort.done"), "w").close()
    print("когорта: готово, проб %d" % len(пробы))


def _когорта_пробы(cl, n, seed, потоков, пробы, в_когорте, lock, out_p, out_c):

    def проба(id_орг):
        nonlocal в_когорте
        status, bfo = cl.get_json(ГИРБО + "/nbo/organizations/%d/bfo/" % id_орг)
        if status == 200:
            row = строка_когорты(id_орг, bfo)
            итог = "когорта" if row else ("пусто" if not bfo else "без_%d" % ГОД)
        elif status in (400, 404):
            row, итог = None, "нет_id"
        else:
            raise Стоп("ГИР БО: /bfo/ id=%d -> %s %s" % (id_орг, status, str(bfo)[:200]))
        with lock:
            if row:
                out_c.write(json.dumps(row, ensure_ascii=False) + "\n")
                out_c.flush()
                в_когорте += 1
                if в_когорте % 250 == 0:
                    print("  когорта %d (проб %d)" % (в_когорте, len(пробы)), file=sys.stderr)
            out_p.write(json.dumps({"id": id_орг, "итог": итог}) + "\n")
            out_p.flush()
            пробы[id_орг] = итог

    ids = последовательность_id(seed)
    while в_когорте < n:
        # порция: сколько ещё нужно, с запасом на долю попаданий ~27%
        порция = []
        for i in ids:
            if i not in пробы:
                порция.append(i)
            if len(порция) >= max(50, int((n - в_когорте) / 0.25)) or len(порция) >= 2000:
                break
        if not порция:
            break
        _параллельно(проба, порция, потоков)
    print("когорта: %d компаний" % в_когорте)


def прочитать_когорту():
    путь = _путь("cohort.jsonl")
    if not os.path.exists(путь):
        raise Стоп("сначала: collect.py когорта")
    seen, rows = set(), []
    with open(путь, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue            # недописанная строка оборванного прогона
            if r["inn"] not in seen:     # два id на один ИНН — берётся первый
                seen.add(r["inn"])
                rows.append(r)
    return rows


# ---------------------------------------------------------------------------
# ЕФРСБ (исход) и ГИР БО (фаза 2)
# ---------------------------------------------------------------------------

def ефрсб(cl, следом, потоков):
    сделано = 0

    def одна(inn):
        nonlocal сделано
        url = (ЕФРСБ + "/backend/cmpbankrupts?searchString=%s&limit=15&offset=0"
               % urllib.parse.quote(inn))
        status, j = cl.get_json(url)
        if status != 200 or not isinstance(j, dict) or "pageData" not in j:
            raise Стоп("ЕФРСБ ответил %s — останавливаюсь, повторами не пробиваем: %s"
                       % (status, str(j)[:200]))
        _писать_json(_путь("efrsb", inn + ".json"),
                     {"дата": time.strftime("%Y-%m-%d"), "ответ": j})
        сделано += 1
        if сделано % 250 == 0:
            print("  ЕФРСБ: %d" % сделано, file=sys.stderr)

    while True:
        когорта_готова = os.path.exists(_путь("cohort.done"))
        try:
            rows = прочитать_когорту()
        except Стоп:
            rows = []
        нужно = [r["inn"] for r in rows
                 if not os.path.exists(_путь("efrsb", r["inn"] + ".json"))]
        if нужно:
            _параллельно(одна, нужно, потоков)
            continue
        if not следом or когорта_готова:
            break
        time.sleep(60)
    print("ЕФРСБ: новых %d" % сделано)


def гирбо(cl, контроли, seed, потоков):
    rows = прочитать_когорту()
    случаи, чистые, без_исхода = [], [], 0
    for r in rows:
        ef = _читать_json(_путь("efrsb", r["inn"] + ".json"))
        if ef is None:
            без_исхода += 1
            continue
        к = label.исход(r["inn"], ef["ответ"])["класс"]
        if к == "банкрот":
            случаи.append(r)
        elif к == "выжил":
            чистые.append(r)
    rng = random.Random(seed)
    выбор = случаи + rng.sample(чистые, min(контроли, len(чистые)))
    _писать_json(_путь("phase2.json"), {
        "seed": seed, "контроли_запрошено": контроли, "случаев": len(случаи),
        "выживших_всего": len(чистые), "контролей": len(выбор) - len(случаи),
        "без_исхода": без_исхода, "инн": [r["inn"] for r in выбор]})
    print("фаза 2: случаев %d, контролей %d из %d выживших (без исхода %d)"
          % (len(случаи), len(выбор) - len(случаи), len(чистые), без_исхода))

    def одна(r):
        путь = _путь("girbo", r["inn"] + ".json")
        if os.path.exists(путь):
            return
        details = {}
        for rec in r["bfo"]:
            if str(rec.get("period")) in (str(ГОД), str(ГОД - 1)) and rec.get("id") is not None:
                st, d = cl.get_json(ГИРБО + "/nbo/bfo/%s/details" % rec["id"])
                if st == 200:
                    details[str(rec["id"])] = d
        search_row = {k: r[k] for k in ("id", "inn", "shortName", "okved2")}
        _писать_json(путь, {"search": {"content": [search_row]}, "bfo": r["bfo"],
                            "details": details})

    _параллельно(одна, выбор, потоков)
    print("ГИР БО: фаза 2 собрана")


def статус():
    print("кэш: %s" % КЭШ)
    пробы = 0
    if os.path.exists(_путь("probes.jsonl")):
        with open(_путь("probes.jsonl"), encoding="utf-8") as f:
            пробы = sum(1 for _ in f)
    try:
        rows = прочитать_когорту()
    except Стоп:
        rows = []
    ef = sum(1 for r in rows if os.path.exists(_путь("efrsb", r["inn"] + ".json")))
    gdir = os.path.dirname(_путь("girbo", "x"))
    print("проб id: %d; когорта: %d ИНН%s; ЕФРСБ: %d; ГИР БО фаза 2: %d"
          % (пробы, len(rows), " (готова)" if os.path.exists(_путь("cohort.done")) else "",
             ef, len(os.listdir(gdir))))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("стадия", choices=["когорта", "ефрсб", "гирбо", "статус"])
    ap.add_argument("--n", type=int, default=15000)
    ap.add_argument("--контроли", type=int, default=3000)
    ap.add_argument("--seed", type=int, default=2021)
    ap.add_argument("--rps", type=float, default=None)
    ap.add_argument("--потоков", type=int, default=4)
    ap.add_argument("--следом", action="store_true",
                    help="ефрсб: ждать новых строк когорты, пока она не готова")
    a = ap.parse_args(argv)
    try:
        if a.стадия == "когорта":
            когорта(гирбо_клиент(a.rps or 3.0), a.n, a.seed, a.потоков)
        elif a.стадия == "ефрсб":
            ефрсб(ефрсб_клиент(a.rps or 1.5), a.следом, a.потоков)
        elif a.стадия == "гирбо":
            гирбо(гирбо_клиент(a.rps or 3.0), a.контроли, a.seed, a.потоков)
        else:
            статус()
    except Стоп as e:
        print("СТОП: %s" % e, file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
