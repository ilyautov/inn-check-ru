#!/usr/bin/env python3
"""
install_ca.py — установка корня Национального УЦ Минцифры для inn-check-ru.

Зачем. Часть государственных источников (fedsfm.ru, rosstat.gov.ru) отдаёт TLS-цепочку,
подписанную «Russian Trusted Root CA» → «Russian Trusted Sub CA» Минцифры. Этих корней
нет в системных хранилищах вне РФ, поэтому запросы падают на верификации (curl exit 60).
Скрипт скачивает корневой и промежуточные сертификаты, сверяет отпечаток SHA-256 с
опубликованным значением, зашитым ниже, и пишет PEM-бандл в
    ~/.cache/inn-check-ru/ca/russian_trusted_bundle.pem
fetch_counterparty._build_ssl_context() подхватывает этот файл автоматически.
Верификация TLS НЕ отключается никогда: корень добавляется к системному доверию, а не
заменяет проверку.

Использование:
    python3 scripts/install_ca.py                 # скачать, сверить, установить
    python3 scripts/install_ca.py --проверить     # (установить и) прогнать три GET
    python3 scripts/install_ca.py --из-файла корень.cer --из-файла sub.cer   # оффлайн
    python3 scripts/install_ca.py --удалить

Переменные окружения:
    COUNTERPARTY_CA_DIR — каталог для бандла (по умолчанию ~/.cache/inn-check-ru/ca).
        fetch_counterparty знает только путь по умолчанию: при нестандартном каталоге
        укажите ему COUNTERPARTY_CA_BUNDLE=<каталог>/russian_trusted_bundle.pem
        (для --проверить скрипт делает это сам).

Только стандартная библиотека. Вывод — JSON в stdout (UTF-8).
"""

import argparse
import base64
import hashlib
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# Каталог сертификатов и опубликованные отпечатки
# ---------------------------------------------------------------------------
#
# Официальная страница Минцифры/Госуслуг: https://www.gosuslugi.ru/crt — файлы с неё
# раздаются с CDN Госуслуг https://gu-st.ru/content/lending/ (та же ссылка стоит и на
# https://www.gosuslugi.ru/tls). Сама страница — SPA под антибот-заслоном и с не-РФ IP
# не открывается (таймаут), поэтому отпечатки здесь — SHA-256 DER-тела сертификатов,
# снятые с файлов официального CDN 19.09.2026 и сверенные с независимыми публикациями:
#   * корень, SHA-256 D2:6D:2D:02:… — «Official root fingerprint» в README
#     https://github.com/andreiborisov/ca-strated (ссылается на gosuslugi.ru/crt);
#     копия в CT-логах: https://crt.sh/?id=6316640888 (поиск crt.sh по этому SHA-256);
#     SHA-1 8F:F9:15:CC:… — https://winitpro.ru/index.php/2022/09/22/tls-sertifikaty-russian-trusted-root-ca/
#   * Sub CA 2022 (serial 1002) — копия в CT-логах: https://crt.sh/?id=6404804746
#   * Sub CA 2024 (serial 1005) — именно им подписаны листы fedsfm.ru и rosstat.gov.ru
#     (AKI листа = SKI 77:3D:D9:39:AF:42:BD:DC:5B:CA:76:EA:EE:FD:CE:3E:61:29:30:5F).
# Формат отпечатка — как у `openssl x509 -fingerprint -sha256`, без двоеточий, нижний регистр.
# Отпечаток ФАЙЛА (sha256sum *.crt) — другое число, здесь не используется.
#
# Если Минцифры выпустит новый Sub CA — добавить запись сюда и фикстуру в eval/fixtures/ca/.

КАТАЛОГ = (
    {
        "id": "root",
        "название": "Russian Trusted Root CA",
        "url": "https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt",
        "sha256": "d26d2d0231b7c39f92cc738512ba54103519e4405d68b5bd703e9788ca8ecf31",
        "sha1": "8ff915ccab7bc16f8c5c8099d53e0e115b3aec2f",
        "серийный": "1000",
        "действует_до": "2032-02-27",
        "обязателен": True,
    },
    {
        "id": "sub_2024",
        "название": "Russian Trusted Sub CA (2024)",
        "url": "https://gu-st.ru/content/lending/russian_trusted_sub_ca_2024_pem.crt",
        "sha256": "2155785036c900dbb5f1bb2a1569c80c55595bd6bf94867a29bbddbc7d88a3f2",
        "sha1": None,
        "серийный": "1005",
        "действует_до": "2029-07-19",
        "обязателен": True,
    },
    {
        "id": "sub_2022",
        "название": "Russian Trusted Sub CA (2022)",
        "url": "https://gu-st.ru/content/lending/russian_trusted_sub_ca_pem.crt",
        "sha256": "bbbde2103e790b999ec62bd03cf625a5a2e7c316e10afe6a490eedead8b3fd9b",
        "sha1": "335d43f53451b781535ff3882df713d3c14f8a01",
        "серийный": "1002",
        "действует_до": "2027-03-06",
        "обязателен": False,   # старый промежуточный: сайты на нём ещё встречаются
    },
)

ИМЯ_БАНДЛА = "russian_trusted_bundle.pem"
_PEM_BLOCK = re.compile(
    rb"-----BEGIN CERTIFICATE-----\s*(.*?)\s*-----END CERTIFICATE-----", re.DOTALL)


class ОшибкаУстановки(Exception):
    """Любой отказ: отпечаток не совпал, файл не разобран, сеть недоступна."""


# ---------------------------------------------------------------------------
# Пути
# ---------------------------------------------------------------------------

def _ca_dir(явный=None):
    """Каталог бандла: параметр → COUNTERPARTY_CA_DIR → ~/.cache/inn-check-ru/ca."""
    d = явный or os.environ.get("COUNTERPARTY_CA_DIR") or os.path.join(
        "~", ".cache", "inn-check-ru", "ca")
    return os.path.expanduser(d)


def _bundle_path(ca_dir=None):
    return os.path.join(_ca_dir(ca_dir), ИМЯ_БАНДЛА)


# ---------------------------------------------------------------------------
# DER / PEM / отпечатки
# ---------------------------------------------------------------------------

def _der_blocks(data):
    """Список DER-тел сертификатов из байтов файла.

    PEM определяется по маркеру BEGIN CERTIFICATE (файл может содержать несколько
    блоков — бандл), DER — по первому байту 0x30 (ASN.1 SEQUENCE). Иное — отказ.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise ОшибкаУстановки("ожидались байты сертификата, получено %s" % type(data).__name__)
    data = bytes(data)
    if b"-----BEGIN CERTIFICATE-----" in data:
        blocks = []
        for m in _PEM_BLOCK.finditer(data):
            body = re.sub(rb"\s+", b"", m.group(1))
            try:
                blocks.append(base64.b64decode(body, validate=True))
            except (ValueError, TypeError) as e:
                raise ОшибкаУстановки("PEM-блок не декодируется из base64: %s" % e) from e
        if not blocks:
            raise ОшибкаУстановки("PEM без завершённого блока CERTIFICATE")
        return blocks
    stripped = data.lstrip()
    if stripped and stripped[0] == 0x30:
        return [stripped]
    raise ОшибкаУстановки(
        "не похоже ни на PEM (BEGIN CERTIFICATE), ни на DER (0x30): первые байты %r"
        % data[:16])


def _der_to_pem(der):
    """DER → PEM: base64 строками по 64 символа в заголовках CERTIFICATE."""
    b64 = base64.b64encode(der).decode("ascii")
    lines = [b64[i:i + 64] for i in range(0, len(b64), 64)]
    return "-----BEGIN CERTIFICATE-----\n%s\n-----END CERTIFICATE-----\n" % "\n".join(lines)


def _отпечаток(der, алгоритм="sha256"):
    return hashlib.new(алгоритм, der).hexdigest()


def _норм(fp):
    """'D2:6D:2D…' / 'd2 6d 2d…' / 'd26d2d…' → 'd26d2d…'."""
    return re.sub(r"[^0-9a-f]", "", (fp or "").lower())


def _сверить(der, запись):
    """Отказ, если SHA-256 DER-тела не совпал с опубликованным (и SHA-1, если он задан)."""
    ожид = _норм(запись["sha256"])
    факт = _отпечаток(der, "sha256")
    if факт != ожид:
        raise ОшибкаУстановки(
            "отпечаток SHA-256 «%s» не совпал: ожидалось %s, получено %s — "
            "файл отвергнут, ничего не записано" % (запись["название"], ожид, факт))
    if запись.get("sha1"):
        ожид1 = _норм(запись["sha1"])
        факт1 = _отпечаток(der, "sha1")
        if факт1 != ожид1:
            raise ОшибкаУстановки(
                "отпечаток SHA-1 «%s» не совпал: ожидалось %s, получено %s"
                % (запись["название"], ожид1, факт1))
    return факт


# ---------------------------------------------------------------------------
# Получение сертификатов
# ---------------------------------------------------------------------------

def _скачать(url, таймаут=30, попыток=3):
    """GET с системным доверием (CDN Госуслуг под публичным УЦ). Верификация включена.

    gu-st.ru иногда рвёт первое TLS-соединение (UNEXPECTED_EOF) — повторяем до `попыток`.
    """
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": "inn-check-ru/install_ca"})
    последняя = None
    for n in range(попыток):
        try:
            with urllib.request.urlopen(req, timeout=таймаут, context=ctx) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            raise ОшибкаУстановки("HTTP %s при скачивании %s" % (e.code, url)) from e
        except (urllib.error.URLError, OSError) as e:
            последняя = getattr(e, "reason", e)
            time.sleep(0.5 * (n + 1))
    raise ОшибкаУстановки(
        "не скачалось %s (%d попытки): %s. Если TLS упирается в тот же УЦ — скачайте .cer с "
        "https://www.gosuslugi.ru/crt в браузере и поставьте через --из-файла"
        % (url, попыток, последняя))


def _собрать_по_отпечаткам(блобы, каталог):
    """Сопоставить DER-блобы записям каталога по SHA-256. Незнакомый блоб — отказ."""
    по_id = {}
    for der in блобы:
        fp = _отпечаток(der)
        запись = next((z for z in каталог if _норм(z["sha256"]) == fp), None)
        if запись is None:
            raise ОшибкаУстановки(
                "сертификат с SHA-256 %s не входит в каталог доверенных — отвергнут" % fp)
        _сверить(der, запись)
        по_id[запись["id"]] = der
    return по_id


def _получить(каталог, из_файлов, таймаут):
    """{id: DER} — скачиванием (все записи) или из локальных файлов (по отпечаткам)."""
    if из_файлов:
        блобы = []
        for путь in из_файлов:
            try:
                with open(путь, "rb") as f:
                    блобы.extend(_der_blocks(f.read()))
            except OSError as e:
                raise ОшибкаУстановки("не читается %s: %s" % (путь, e)) from e
        по_id = _собрать_по_отпечаткам(блобы, каталог)
    else:
        по_id = {}
        for запись in каталог:
            блобы = _der_blocks(_скачать(запись["url"], таймаут))
            if len(блобы) != 1:
                raise ОшибкаУстановки(
                    "%s: ожидался один сертификат, получено %d" % (запись["url"], len(блобы)))
            _сверить(блобы[0], запись)
            по_id[запись["id"]] = блобы[0]
    нет = [z["название"] for z in каталог if z.get("обязателен") and z["id"] not in по_id]
    if нет:
        raise ОшибкаУстановки("не хватает обязательных сертификатов: %s" % ", ".join(нет))
    return по_id


# ---------------------------------------------------------------------------
# Запись бандла
# ---------------------------------------------------------------------------

def _текст_бандла(по_id, каталог):
    части = [("# Russian Trusted CA bundle — Национальный УЦ Минцифры\n"
              "# Собран scripts/install_ca.py (inn-check-ru); отпечатки сверены с каталогом.\n"
              "# Порядок: корень, затем промежуточные.\n")]
    for запись in каталог:
        der = по_id.get(запись["id"])
        if der is None:
            continue
        части.append("\n# %s\n# serial=%s  до=%s\n# SHA-256 %s\n%s" % (
            запись["название"], запись["серийный"], запись["действует_до"],
            _отпечаток(der), _der_to_pem(der)))
    return "".join(части)


def _записать(текст, путь):
    os.makedirs(os.path.dirname(путь), exist_ok=True)
    tmp = путь + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(текст)
    os.chmod(tmp, 0o644)
    os.replace(tmp, путь)


def установить(ca_dir=None, из_файлов=None, каталог=КАТАЛОГ, таймаут=30):
    """Скачать/прочитать, сверить отпечатки, записать бандл. Возвращает dict-результат.

    Любая ошибка — ОшибкаУстановки до записи: файл на диске не меняется.
    """
    по_id = _получить(каталог, из_файлов, таймаут)
    путь = _bundle_path(ca_dir)
    _записать(_текст_бандла(по_id, каталог), путь)
    return {
        "статус": "ok",
        "бандл": путь,
        "источник": "файлы" if из_файлов else "gu-st.ru (gosuslugi.ru/crt)",
        "сертификаты": [
            {"id": z["id"], "название": z["название"], "sha256": _отпечаток(по_id[z["id"]]),
             "действует_до": z["действует_до"]}
            for z in каталог if z["id"] in по_id
        ],
    }


def удалить(ca_dir=None):
    путь = _bundle_path(ca_dir)
    было = os.path.isfile(путь)
    if было:
        os.remove(путь)
    return {"статус": "ok", "бандл": путь, "удалён": было}


# ---------------------------------------------------------------------------
# --проверить: три GET через контекст fetch_counterparty
# ---------------------------------------------------------------------------

def _модули_движка():
    """Импорт fetch_counterparty/sources из scripts/ (пакета нет — через sys.path)."""
    здесь = os.path.dirname(os.path.abspath(__file__))
    if здесь not in sys.path:
        sys.path.insert(0, здесь)
    # ленивый импорт: fetch_counterparty строит TLS-контекст при загрузке модуля
    import fetch_counterparty
    import sources
    return fetch_counterparty, sources


def проверить(ca_dir=None, таймаут=20):
    """GET к каждому URL из sources.TLS_MINCIFRY_PROBES с контекстом движка.

    Статусы: ok — TLS и HTTP 2xx/3xx; http — TLS прошёл, ответ 4xx/5xx (гео-фильтр и т.п.);
    tls — цепочка не проверена; сеть — таймаут/DNS/сброс.

    Замечание по npchk.nalog.ru (снимок 19.09.2026): его лист выпущен GlobalSign
    (GCC R3 DV TLS CA 2020), а сервер не отдаёт промежуточный сертификат — это не УЦ
    Минцифры, бандл здесь не поможет; статус «tls» для него ожидаем.
    """
    бандл = _bundle_path(ca_dir)
    if os.path.isfile(бандл):
        # fetch_counterparty знает только путь по умолчанию — подсказываем ему наш.
        os.environ.setdefault("COUNTERPARTY_CA_BUNDLE", бандл)
    fc, sources = _модули_движка()
    ctx = fc._build_ssl_context()
    итоги = []
    for имя, url in sources.TLS_MINCIFRY_PROBES.items():
        запись = {"источник": имя, "url": url}
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 inn-check-ru"})
        try:
            with urllib.request.urlopen(req, timeout=таймаут, context=ctx) as r:
                запись.update(статус="ok", http=r.status)
        except urllib.error.HTTPError as e:
            запись.update(статус="http", http=e.code)
        except urllib.error.URLError as e:
            причина = e.reason
            if isinstance(причина, ssl.SSLCertVerificationError):
                запись.update(статус="tls", ошибка=причина.verify_message or str(причина))
            elif isinstance(причина, ssl.SSLError):
                запись.update(статус="tls", ошибка=str(причина))
            else:
                запись.update(статус="сеть", ошибка=str(причина))
        except ssl.SSLCertVerificationError as e:
            запись.update(статус="tls", ошибка=e.verify_message or str(e))
        except (ssl.SSLError, OSError) as e:
            запись.update(статус="сеть", ошибка=str(e))
        итоги.append(запись)
    return {"бандл": бандл if os.path.isfile(бандл) else None, "проверка": итоги}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    p = argparse.ArgumentParser(
        description="Корень УЦ Минцифры для inn-check-ru: скачать, сверить отпечаток, установить.")
    p.add_argument("--проверить", action="store_true",
                   help="после установки (или без неё, если бандл уже есть) прогнать GET к "
                        "fedsfm/rosstat/npchk с контекстом fetch_counterparty")
    p.add_argument("--из-файла", action="append", metavar="ПУТЬ", default=None,
                   help="установить из локального .cer/.pem (можно несколько раз); "
                        "отпечатки сверяются так же")
    p.add_argument("--удалить", action="store_true", help="удалить бандл")
    p.add_argument("--каталог", metavar="ДИР", default=None,
                   help="каталог бандла (иначе COUNTERPARTY_CA_DIR или ~/.cache/inn-check-ru/ca)")
    p.add_argument("--таймаут", type=int, default=30, help="сетевой таймаут, сек")
    a = p.parse_args(argv)

    результат = {}
    код = 0
    try:
        if a.удалить:
            результат["удаление"] = удалить(a.каталог)
        else:
            нужна_установка = a.из_файла or not a.проверить or not os.path.isfile(
                _bundle_path(a.каталог))
            if нужна_установка:
                результат["установка"] = установить(
                    a.каталог, из_файлов=a.из_файла, таймаут=a.таймаут)
            if a.проверить:
                результат.update(проверить(a.каталог, таймаут=min(a.таймаут, 30)))
    except ОшибкаУстановки as e:
        результат["ошибка"] = str(e)
        код = 1
    print(json.dumps(результат, ensure_ascii=False, indent=2))
    return код


if __name__ == "__main__":
    sys.exit(main())
