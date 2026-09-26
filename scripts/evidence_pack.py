#!/usr/bin/env python3
"""
evidence_pack.py — пакет доказательств проверки: сырые ответы источников,
манифест с SHA-256 и (по желанию) штамп времени RFC 3161.

Зачем. Досье пересказывает разобранный JSON: «ЕГРЮЛ ответил, что организация
действует». Пакет хранит то, что источник отдал на самом деле, — байты ответа,
URL, HTTP-код и время. Хеши связывают все файлы с манифестом, но сами по себе
доказывают лишь согласованность: владелец пакета может всё пересчитать.
Неизменность после сбора доказывает внешняя привязка — штамп времени RFC 3161
на манифест (пакет существовал в этом виде не позже указанного момента) или
хеш манифеста, отправленный третьей стороне в день проверки.

Чем он НЕ является. Штамп RFC 3161 от публичного TSA — не квалифицированная
метка времени по 63-ФЗ и не нотариальное заверение. Ответы источников не
подписаны самими источниками: даже со штампом пакет доказывает неизменность
после сбора, а не то, что сервер ФНС отдал именно эти байты. Весомость пакета в споре с налоговой
определяет суд; практики по таким пакетам пока нет.

Использование:
    python3 fetch_counterparty.py <ИНН> --пакет ./пакет_7707083893
    INN_CHECK_TSA=https://freetsa.org/tsr python3 fetch_counterparty.py <ИНН> --пакет DIR
    python3 evidence_pack.py --проверить DIR
    python3 evidence_pack.py --штамп DIR [--tsa URL]     # поставить штамп позже

Состав пакета:
    fetch.json          — итоговый JSON движка (то, что пересказывает досье);
    raw/NNN_<ист>.bin   — тело ответа источника, байт в байт;
    raw/NNN_<ист>.req   — тело POST-запроса (если был);
    raw/index.json      — метод, URL, HTTP-код, время UTC, sha256 и размер;
    manifest.json       — sha256 и размер каждого файла выше + версия инструмента;
    manifest.sha256     — хеш манифеста в формате `sha256sum -c`;
    manifest.tsq / .tsr — запрос и ответ TSA (только со штампом);
    ПРОВЕРКА.txt        — как проверить пакет без этого инструмента.

Приватность штампа: на сервер TSA уходит только SHA-256 манифеста — ни ИНН,
ни ответов. По хешу восстановить содержимое нельзя.

Подпись TSA этот модуль не проверяет (нужна криптография вне stdlib): он
сверяет статус ответа, хеш в штампе, nonce и достаёт время. Подпись проверяет
`openssl ts -verify` — команда пишется в ПРОВЕРКА.txt.
"""

import datetime
import hashlib
import json
import os
import re
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

ФОРМАТ = "inn-check-ru/evidence-pack/1"
МАНИФЕСТ = "manifest.json"
ХЕШ_МАНИФЕСТА = "manifest.sha256"
ЗАПРОС_TSA = "manifest.tsq"
ОТВЕТ_TSA = "manifest.tsr"
ИНСТРУКЦИЯ = "ПРОВЕРКА.txt"
# Файлы вокруг манифеста: в самом манифесте их нет (иначе хеш зависел бы от себя).
ВНЕ_МАНИФЕСТА = {МАНИФЕСТ, ХЕШ_МАНИФЕСТА, ЗАПРОС_TSA, ОТВЕТ_TSA, ИНСТРУКЦИЯ}

# Публичные TSA для INN_CHECK_TSA=да. Оба бесплатны; порядок — порядок попыток.
TSA_ПО_УМОЛЧАНИЮ = ("https://freetsa.org/tsr", "http://timestamp.digicert.com")
TSA_TIMEOUT = 20

# DER: OID sha256 (2.16.840.1.101.3.4.2.1) и id-ct-TSTInfo (1.2.840.113549.1.9.16.1.4).
_OID_SHA256 = bytes.fromhex("0609608648016503040201")
_OID_TSTINFO = bytes.fromhex("060b2a864886f70d0109100104")


def _sha256(данные):
    return hashlib.sha256(данные).hexdigest()


def _версия():
    try:
        текст = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        m = re.search(r'^\s*version:\s*"([^"]+)"', текст, re.MULTILINE)
        if m:
            return m.group(1)
    except OSError:
        pass
    return "не определена"


def _имя_источника(ист):
    """id источника -> безопасный фрагмент имени файла (кириллица остаётся)."""
    return re.sub(r"[^\w-]+", "_", str(ист or "без_источника")).strip("_")[:60] or "x"


# --- запись -------------------------------------------------------------------

def каталог_годится(путь):
    """None, если в каталог можно писать пакет; иначе причина.

    Пишем только в новый или пустой каталог: смешанный со старыми файлами пакет
    не проверить — непонятно, что к какой проверке относится.
    """
    p = Path(путь)
    if p.exists() and not p.is_dir():
        return "%s — файл, а не каталог" % путь
    if p.is_dir() and any(p.iterdir()):
        return "каталог %s не пуст — пакет пишется только в новый или пустой" % путь
    return None


def записать_пакет(каталог, inn, fetch, ответы, штамп=None, now=None):
    """Пишет пакет; возвращает {каталог, ответов, файлов, манифест_sha256, штамп}.

    `ответы` — записи движка (_ЗАПИСЬ["ответы"]): источник, метод, url, статус,
    время_utc, данные (bytes), тело_запроса (bytes|None). `штамп` — URL TSA,
    список через запятую или «да» (TSA_ПО_УМОЛЧАНИЮ); None — без штампа.
    """
    ошибка = каталог_годится(каталог)
    if ошибка:
        raise ValueError(ошибка)
    корень = Path(каталог)
    raw = корень / "raw"
    raw.mkdir(parents=True, exist_ok=True)

    индекс = []
    for n, о in enumerate(ответы, 1):
        база = "%03d_%s" % (n, _имя_источника(о.get("источник")))
        данные = о.get("данные") or b""
        (raw / (база + ".bin")).write_bytes(данные)
        запись = {
            "файл": "raw/%s.bin" % база,
            "источник": о.get("источник"),
            "метод": о.get("метод"),
            "url": о.get("url"),
            "статус": о.get("статус"),
            "время_utc": о.get("время_utc"),
            "sha256": _sha256(данные),
            "байт": len(данные),
        }
        тело = о.get("тело_запроса")
        if тело:
            if isinstance(тело, str):
                тело = тело.encode("utf-8")
            (raw / (база + ".req")).write_bytes(тело)
            запись["запрос"] = {"файл": "raw/%s.req" % база, "sha256": _sha256(тело),
                                "байт": len(тело)}
        индекс.append(запись)
    (raw / "index.json").write_text(
        json.dumps(индекс, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (корень / "fetch.json").write_text(
        json.dumps(fetch, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    момент = now or datetime.datetime.now(datetime.timezone.utc)
    манифест = {
        "формат": ФОРМАТ,
        "инструмент": "inn-check-ru %s" % _версия(),
        "инн": inn,
        "создан_utc": момент.isoformat(timespec="seconds"),
        "ответов_источников": len(индекс),
        "файлы": _опись(корень),
        "оговорка": ("Без штампа времени хеши показывают только согласованность "
                     "файлов с манифестом; неизменность после сбора доказывает "
                     "штамп RFC 3161 или хеш манифеста, переданный третьей "
                     "стороне. Ответы источниками не подписаны. Штамп публичного "
                     "TSA — не квалифицированная метка времени по 63-ФЗ."),
    }
    байты_манифеста = (json.dumps(манифест, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    (корень / МАНИФЕСТ).write_bytes(байты_манифеста)
    хеш = _sha256(байты_манифеста)
    (корень / ХЕШ_МАНИФЕСТА).write_text("%s  %s\n" % (хеш, МАНИФЕСТ), encoding="utf-8")

    итог = {"каталог": str(корень), "ответов": len(индекс),
            "файлов": len(манифест["файлы"]), "манифест_sha256": хеш, "штамп": None}
    if штамп:
        итог["штамп"] = поставить_штамп(корень, штамп)
    _инструкция(корень)
    return итог


def _опись(корень):
    """[{путь, sha256, байт}] всех файлов пакета, кроме окружения манифеста."""
    опись = []
    for p in sorted(корень.rglob("*")):
        if not p.is_file():
            continue
        отн = p.relative_to(корень).as_posix()
        if отн in ВНЕ_МАНИФЕСТА:
            continue
        данные = p.read_bytes()
        опись.append({"путь": отн, "sha256": _sha256(данные), "байт": len(данные)})
    return опись


def _инструкция(корень):
    есть_штамп = (корень / ОТВЕТ_TSA).exists()
    строки = [
        "Пакет доказательств inn-check-ru. Проверка без этого инструмента:",
        "",
        "1. Манифест не изменён:",
        "     sha256sum -c %s        (macOS: shasum -a 256 -c %s)" % (ХЕШ_МАНИФЕСТА, ХЕШ_МАНИФЕСТА),
        "2. Файлы соответствуют манифесту: sha256 каждого файла из %s" % МАНИФЕСТ,
        "   совпадает с полем sha256 (python3 evidence_pack.py --проверить <каталог>",
        "   делает это и шаги 1, 3 разом).",
    ]
    if есть_штамп:
        строки += [
            "3. Штамп времени RFC 3161 (подпись TSA):",
            "     openssl ts -reply -in %s -text" % ОТВЕТ_TSA,
            "     openssl ts -verify -in %s -queryfile %s -CAfile <цепочка TSA>"
            % (ОТВЕТ_TSA, ЗАПРОС_TSA),
            "   Цепочку сертификатов берут у TSA (freetsa.org: cacert.pem и tsa.crt,",
            "   для openssl -CAfile cacert.pem -untrusted tsa.crt).",
            "   Штамп доказывает, что манифест — а через хеши и весь пакет —",
            "   существовал не позже указанного времени.",
        ]
    строки += [
        "",
        "Границы: без штампа хеши доказывают только согласованность файлов —",
        "владелец пакета мог пересчитать их все. Внешняя привязка — штамп или",
        "хеш манифеста, переданный третьей стороне в день проверки. Ответы",
        "источников ими не подписаны: пакет не доказывает подлинность ответа",
        "сервера. Штамп публичного TSA — не квалифицированная метка по 63-ФЗ.",
    ]
    (корень / ИНСТРУКЦИЯ).write_text("\n".join(строки) + "\n", encoding="utf-8")


# --- RFC 3161: запрос и разбор ответа (минимальный DER, stdlib) ----------------

def _der_len(n):
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def _der(tag, тело):
    return bytes([tag]) + _der_len(len(тело)) + тело


def _der_int(n):
    return _der(0x02, n.to_bytes(max(1, (n.bit_length() + 8) // 8), "big"))


def запрос_штампа(хеш_hex, nonce):
    """DER TimeStampReq v1: sha256-отпечаток, nonce, certReq=TRUE."""
    алгоритм = _der(0x30, _OID_SHA256 + b"\x05\x00")
    отпечаток = _der(0x30, алгоритм + _der(0x04, bytes.fromhex(хеш_hex)))
    return _der(0x30, _der_int(1) + отпечаток + _der_int(nonce) + b"\x01\x01\xff")


def _tlv(buf, i):
    """(tag, value, следующий_индекс) одного DER-элемента с позиции i."""
    if i + 2 > len(buf):
        raise ValueError("DER: обрыв на %d" % i)
    tag, длина = buf[i], buf[i + 1]
    i += 2
    if длина & 0x80:
        k = длина & 0x7F
        if k == 0 or k > 4 or i + k > len(buf):
            raise ValueError("DER: неподдерживаемая длина на %d" % i)
        длина = int.from_bytes(buf[i:i + k], "big")
        i += k
    if i + длина > len(buf):
        raise ValueError("DER: длина за пределами буфера на %d" % i)
    return tag, buf[i:i + длина], i + длина


def _дети(тело):
    out, i = [], 0
    while i < len(тело):
        tag, v, i = _tlv(тело, i)
        out.append((tag, v))
    return out


def _первый(дети, tag, что):
    for t, v in дети:
        if t == tag:
            return v
    raise ValueError("RFC 3161: нет %s" % что)


def разобрать_ответ(tsr):
    """TimeStampResp -> {статус, время_utc, хеш_hex, nonce, серийный}.

    Статус 0/1 — granted/grantedWithMods; иное — отказ TSA (время None).
    Любой битый DER — ValueError: ответ чужого сервера не должен ронять сбор.
    """
    try:
        return _разобрать_ответ(tsr)
    except ValueError:
        raise
    except Exception as e:
        raise ValueError("RFC 3161: ответ не разбирается (%s)" % type(e).__name__) from e


def _разобрать_ответ(tsr):
    tag, тело, _ = _tlv(tsr, 0)
    if tag != 0x30:
        raise ValueError("RFC 3161: ответ не SEQUENCE")
    части = _дети(тело)
    статус = int.from_bytes(_первый(_дети(части[0][1]), 0x02, "status"), "big")
    итог = {"статус": статус, "время_utc": None, "хеш_hex": None, "nonce": None,
            "серийный": None}
    if статус not in (0, 1) or len(части) < 2:
        return итог
    content_info = _дети(части[1][1])
    signed = _дети(_tlv(_первый(content_info, 0xA0, "SignedData"), 0)[1])
    encap = _дети(signed[2][1])
    if not encap or _der(0x06, encap[0][1]) != _OID_TSTINFO:
        raise ValueError("RFC 3161: в токене не TSTInfo")
    tst_der = _tlv(_первый(encap, 0xA0, "eContent"), 0)
    if tst_der[0] != 0x04:
        raise ValueError("RFC 3161: eContent не OCTET STRING")
    tst = _дети(_tlv(tst_der[1], 0)[1])
    imprint = _дети(tst[2][1])
    алгоритм = _дети(imprint[0][1])
    if _der(0x06, алгоритм[0][1]) != _OID_SHA256:
        raise ValueError("RFC 3161: отпечаток не sha256")
    итог["хеш_hex"] = imprint[1][1].hex()
    итог["серийный"] = int.from_bytes(tst[3][1], "big")
    время = tst[4][1].decode("ascii")
    m = re.match(r"(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})(?:[.,]\d+)?Z$", время)
    if not m:
        raise ValueError("RFC 3161: genTime %r" % время)
    итог["время_utc"] = "%s-%s-%sT%s:%s:%s+00:00" % m.groups()
    for t, v in tst[5:]:
        if t == 0x02:                          # первый INTEGER после genTime — nonce
            итог["nonce"] = int.from_bytes(v, "big")
            break
    return итог


def _url_без_секретов(url):
    """URL TSA для вывода и пакета: без user:password и query-строки —
    платные TSA принимают ключ в URL, а итог штампа печатается и пишется."""
    try:
        p = urllib.parse.urlsplit(url)
        хост = p.hostname or ""
        if p.port:
            хост += ":%d" % p.port
        return urllib.parse.urlunsplit((p.scheme, хост, p.path, "", ""))
    except ValueError:
        return "(URL TSA не разобран)"


def _список_tsa(штамп):
    if isinstance(штамп, (list, tuple)):
        return [u for u in штамп if u]
    if str(штамп).strip().lower() in ("1", "да", "yes", "true", "по_умолчанию"):
        return list(TSA_ПО_УМОЛЧАНИЮ)
    return [u.strip() for u in str(штамп).split(",") if u.strip()]


def _post_tsa(url, tsq):
    req = urllib.request.Request(url, data=tsq, method="POST", headers={
        "Content-Type": "application/timestamp-query",
        "Accept": "application/timestamp-reply"})
    with urllib.request.urlopen(req, timeout=TSA_TIMEOUT) as resp:
        return resp.read()


def поставить_штамп(каталог, штамп, post=_post_tsa):
    """Штамп на manifest.json: пишет .tsq/.tsr. Возвращает строку-итог;
    при отказе всех TSA — строку с причиной (пакет остаётся годным без штампа)."""
    корень = Path(каталог)
    хеш = _sha256((корень / МАНИФЕСТ).read_bytes())
    nonce = secrets.randbits(63)
    tsq = запрос_штампа(хеш, nonce)
    причины = []
    for url in _список_tsa(штамп):
        имя = _url_без_секретов(url)
        try:
            tsr = post(url, tsq)
            р = разобрать_ответ(tsr)
        except (OSError, ValueError, urllib.error.URLError) as e:
            причины.append("%s: %s" % (имя, type(e).__name__ if isinstance(e, OSError)
                                       else e))
            continue
        if р["статус"] not in (0, 1):
            причины.append("%s: отказ TSA, статус %s" % (имя, р["статус"]))
            continue
        if р["хеш_hex"] != хеш or р["nonce"] != nonce:
            причины.append("%s: штамп не на наш запрос (хеш или nonce не совпал)" % имя)
            continue
        (корень / ЗАПРОС_TSA).write_bytes(tsq)
        (корень / ОТВЕТ_TSA).write_bytes(tsr)
        _инструкция(корень)
        return "%s, %s" % (р["время_utc"], имя)
    return "не поставлен — " + ("; ".join(причины) or "не указан ни один TSA")


# --- проверка -----------------------------------------------------------------

def проверить(каталог):
    """{годен, ошибки, файлов, штамп}. Годен — только если всё сошлось."""
    корень = Path(каталог)
    ошибки = []
    итог = {"каталог": str(корень), "годен": False, "ошибки": ошибки,
            "файлов": 0, "штамп": None}
    try:
        байты = (корень / МАНИФЕСТ).read_bytes()
        манифест = json.loads(байты.decode("utf-8"))
    except (OSError, ValueError) as e:
        ошибки.append("манифест не читается: %s" % e)
        return итог
    хеш = _sha256(байты)
    try:
        записано = (корень / ХЕШ_МАНИФЕСТА).read_text(encoding="utf-8").split()[0]
    except (OSError, IndexError):
        записано = None
    if записано != хеш:
        ошибки.append("%s не совпадает с хешем манифеста" % ХЕШ_МАНИФЕСТА)
    if манифест.get("формат") != ФОРМАТ:
        ошибки.append("неизвестный формат пакета: %r" % манифест.get("формат"))

    объявлено = {ф.get("путь"): ф for ф in манифест.get("файлы") or []}
    итог["файлов"] = len(объявлено)
    for путь, ф in объявлено.items():
        p = корень / str(путь)
        # Манифест чужого пакета — недоверенный вход: путь за пределы каталога
        # или ссылка наружу сделали бы «годным» файл, которого в пакете нет.
        if (not путь or Path(str(путь)).is_absolute() or ".." in Path(str(путь)).parts
                or p.is_symlink()):
            ошибки.append("недопустимый путь в манифесте: %s" % путь)
            continue
        if not p.is_file():
            ошибки.append("нет файла %s" % путь)
            continue
        данные = p.read_bytes()
        if _sha256(данные) != ф.get("sha256") or len(данные) != ф.get("байт"):
            ошибки.append("изменён файл %s" % путь)
    for p in корень.rglob("*"):
        if p.is_symlink():
            ошибки.append("символическая ссылка в пакете: %s"
                          % p.relative_to(корень).as_posix())
    фактически = {ф["путь"] for ф in _опись(корень)}
    for лишний in sorted(фактически - set(объявлено)):
        ошибки.append("файл не из манифеста: %s" % лишний)

    # Индекс сырья ссылается на файлы с хешами — сверяем и его: подмена пары
    # «файл + строка манифеста» без правки индекса тоже должна ловиться.
    try:
        индекс = json.loads((корень / "raw" / "index.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        индекс = None
        ошибки.append("raw/index.json не читается")
    for запись in индекс or []:
        ф = объявлено.get(запись.get("файл"))
        if ф is None or ф.get("sha256") != запись.get("sha256"):
            ошибки.append("индекс сырья расходится с манифестом: %s" % запись.get("файл"))

    tsr_путь = корень / ОТВЕТ_TSA
    if tsr_путь.exists():
        try:
            р = разобрать_ответ(tsr_путь.read_bytes())
        except ValueError as e:
            ошибки.append("штамп не разбирается: %s" % e)
        else:
            if р["хеш_hex"] != хеш:
                ошибки.append("штамп поставлен на другой манифест")
            tsq_путь = корень / ЗАПРОС_TSA
            if tsq_путь.exists():
                nonce_запроса = _nonce_запроса(tsq_путь.read_bytes())
                if nonce_запроса is not None and nonce_запроса != р["nonce"]:
                    ошибки.append("nonce штампа не совпадает с запросом")
            итог["штамп"] = {"время_utc": р["время_utc"], "серийный": р["серийный"],
                             "подпись": "не проверялась — openssl ts -verify, см. %s"
                                        % ИНСТРУКЦИЯ}
    итог["годен"] = not ошибки
    # «годен» — про файлы и согласованность, не про криптографию штампа.
    итог["что_проверено"] = (
        "хеши и состав файлов против манифеста" + (
            "; штамп: статус, хеш манифеста, nonce — подпись TSA НЕ проверена "
            "(openssl ts -verify)" if итог["штамп"] else "; штампа нет"))
    return итог


def _nonce_запроса(tsq):
    try:
        дети = _дети(_tlv(tsq, 0)[1])
    except (ValueError, IndexError):
        return None
    целые = [v for t, v in дети if t == 0x02]
    return int.from_bytes(целые[1], "big") if len(целые) > 1 else None


# --- CLI ----------------------------------------------------------------------

USAGE = ("Использование: python3 evidence_pack.py --проверить <каталог>\n"
         "               python3 evidence_pack.py --штамп <каталог> [--tsa URL[,URL]]\n"
         "Пакет создаёт fetch_counterparty.py <ИНН> --пакет <каталог>.\n")


def main(argv):
    args = argv[1:]
    if len(args) == 2 and args[0] in ("--проверить", "--verify"):
        итог = проверить(args[1])
        sys.stdout.write(json.dumps(итог, ensure_ascii=False, indent=2) + "\n")
        return 0 if итог["годен"] else 1
    if len(args) in (2, 4) and args[0] in ("--штамп", "--timestamp"):
        tsa = os.environ.get("INN_CHECK_TSA") or "да"
        if len(args) == 4:
            if args[2] != "--tsa":
                sys.stderr.write(USAGE)
                return 2
            tsa = args[3]
        if not (Path(args[1]) / МАНИФЕСТ).exists():
            sys.stderr.write("нет %s в %s\n" % (МАНИФЕСТ, args[1]))
            return 2
        итог = поставить_штамп(args[1], tsa)
        sys.stdout.write("штамп: %s\n" % итог)
        return 1 if итог.startswith("не поставлен") else 0
    sys.stderr.write(USAGE)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
