#!/usr/bin/env python3
"""
run_ca_eval.py — офлайн-eval scripts/install_ca.py (поток C волны 1):
DER→PEM на синтетическом блобе, распознавание DER/PEM по содержимому, отказ при неверном
отпечатке без записи на диск, корректная запись бандла во временный каталог
(COUNTERPARTY_CA_DIR), --удалить, и сверка зашитых отпечатков с реальными сертификатами
из eval/fixtures/ca/ (снимок файлов gu-st.ru от 19.09.2026). Сети нет. PASS/FAIL, stdlib.
"""

import base64
import hashlib
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "eval" / "fixtures" / "ca"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check(errors, cond, msg):
    if not cond:
        errors.append(msg)


# Синтетический DER: валидная ASN.1-обёртка SEQUENCE с мусором внутри — X.509 не нужен,
# проверяется кодирование и заголовки, а не разбор сертификата.
def _synthetic_der(seed):
    body = hashlib.sha512(seed).digest() * 3   # 192 байта — больше одной строки base64
    return b"\x30\x81" + bytes([len(body)]) + body


def _entry(идент, der, обязателен=True, sha256=None):
    return {
        "id": идент, "название": "Synthetic %s" % идент, "url": "https://example.invalid/%s" % идент,
        "sha256": sha256 or hashlib.sha256(der).hexdigest(), "sha1": None,
        "серийный": "1", "действует_до": "2099-01-01", "обязателен": обязателен,
    }


def case_der_pem(ca):
    errors = []
    der = _synthetic_der(b"root")
    pem = ca._der_to_pem(der)
    lines = pem.splitlines()
    check(errors, lines[0] == "-----BEGIN CERTIFICATE-----", "нет заголовка BEGIN")
    check(errors, lines[-1] == "-----END CERTIFICATE-----", "нет заголовка END")
    check(errors, pem.endswith("\n"), "PEM без завершающего перевода строки")
    check(errors, all(len(line) <= 64 for line in lines[1:-1]), "строки base64 длиннее 64")
    check(errors, base64.b64decode("".join(lines[1:-1])) == der, "PEM не декодируется обратно в DER")
    # распознавание по содержимому
    check(errors, ca._der_blocks(der) == [der], "DER (0x30) не распознан как DER")
    check(errors, ca._der_blocks(pem.encode()) == [der], "PEM не распознан / не декодирован")
    crlf = pem.replace("\n", "\r\n").encode()
    check(errors, ca._der_blocks(crlf) == [der], "PEM с CRLF (как отдаёт gu-st.ru) не разобран")
    two = (pem + ca._der_to_pem(_synthetic_der(b"sub"))).encode()
    check(errors, len(ca._der_blocks(two)) == 2, "бандл из двух PEM-блоков даёт не 2 сертификата")
    for мусор in (b"", b"hello", b"-----BEGIN CERTIFICATE-----\n@@@\n-----END CERTIFICATE-----\n"):
        try:
            ca._der_blocks(мусор)
            errors.append("мусор %r принят как сертификат" % мусор[:20])
        except ca.ОшибкаУстановки:
            pass
    check(errors, ca._норм("D2:6D:2d 02") == "d26d2d02", "нормализация отпечатка")
    return errors


def case_fingerprint_reject(ca):
    errors = []
    der_root, der_sub = _synthetic_der(b"root"), _synthetic_der(b"sub")
    with tempfile.TemporaryDirectory() as tmp:
        cer = os.path.join(tmp, "root.cer")
        Path(cer).write_bytes(der_root)
        sub = os.path.join(tmp, "sub.pem")
        Path(sub).write_text(ca._der_to_pem(der_sub))
        плохой = (_entry("root", der_root, sha256="00" * 32), _entry("sub", der_sub))
        try:
            ca.установить(ca_dir=tmp, из_файлов=[cer, sub], каталог=плохой)
            errors.append("неверный отпечаток корня не вызвал отказ")
        except ca.ОшибкаУстановки as e:
            check(errors, "отпечаток" in str(e) or "не входит" in str(e),
                  "текст отказа без слова «отпечаток»: %s" % e)
        check(errors, not os.path.exists(ca._bundle_path(tmp)),
              "после отказа бандл всё равно записан")
        # незнакомый сертификат из файла — отказ
        try:
            ca.установить(ca_dir=tmp, из_файлов=[cer], каталог=(_entry("sub", der_sub),))
            errors.append("незнакомый сертификат принят")
        except ca.ОшибкаУстановки:
            pass
        # не хватает обязательного — отказ
        try:
            ca.установить(ca_dir=tmp, из_файлов=[sub],
                          каталог=(_entry("root", der_root), _entry("sub", der_sub)))
            errors.append("отсутствие обязательного корня не вызвало отказ")
        except ca.ОшибкаУстановки as e:
            check(errors, "не хватает" in str(e), "текст отказа о нехватке: %s" % e)
        check(errors, not os.path.exists(ca._bundle_path(tmp)), "бандл записан при нехватке корня")
    return errors


def case_bundle_write(ca):
    errors = []
    der_root, der_sub, der_old = (_synthetic_der(b"root"), _synthetic_der(b"sub"),
                                  _synthetic_der(b"old"))
    каталог = (_entry("root", der_root), _entry("sub", der_sub), _entry("old", der_old, False))
    with tempfile.TemporaryDirectory() as tmp:
        ca_dir = os.path.join(tmp, "nested", "ca")   # каталога нет — должен создаться
        cer = os.path.join(tmp, "both.pem")
        Path(cer).write_text(ca._der_to_pem(der_root) + ca._der_to_pem(der_sub))
        старое = os.environ.get("COUNTERPARTY_CA_DIR")
        os.environ["COUNTERPARTY_CA_DIR"] = ca_dir
        try:
            check(errors, ca._ca_dir() == ca_dir, "COUNTERPARTY_CA_DIR не подхвачен")
            check(errors, ca._bundle_path().endswith("russian_trusted_bundle.pem"),
                  "имя бандла не russian_trusted_bundle.pem")
            res = ca.установить(из_файлов=[cer], каталог=каталог)
        finally:
            if старое is None:
                del os.environ["COUNTERPARTY_CA_DIR"]
            else:
                os.environ["COUNTERPARTY_CA_DIR"] = старое
        путь = os.path.join(ca_dir, "russian_trusted_bundle.pem")
        check(errors, res.get("статус") == "ok", "статус установки не ok: %s" % res)
        check(errors, res.get("бандл") == путь, "путь бандла в результате: %s" % res.get("бандл"))
        check(errors, [c["id"] for c in res.get("сертификаты", [])] == ["root", "sub"],
              "состав бандла: %s" % [c["id"] for c in res.get("сертификаты", [])])
        check(errors, os.path.isfile(путь), "бандл не записан")
        if os.path.isfile(путь):
            data = Path(путь).read_bytes()
            check(errors, ca._der_blocks(data) == [der_root, der_sub],
                  "порядок/состав DER в бандле неверен")
            check(errors, not os.path.exists(путь + ".tmp"), "остался .tmp")
            check(errors, b"Synthetic root" in data and b"SHA-256" in data,
                  "в бандле нет комментария с названием/отпечатком")
            # ssl должен уметь загрузить файл как cafile хотя бы синтаксически (PEM-разбор)
            # — реальный X.509 здесь не нужен, поэтому проверяем только чтение
            res2 = ca.удалить(ca_dir)
            check(errors, res2.get("удалён") is True and not os.path.exists(путь),
                  "--удалить не удалил бандл")
            check(errors, ca.удалить(ca_dir).get("удалён") is False,
                  "повторный --удалить не сообщил, что файла нет")
    return errors


def case_real_fixtures(ca):
    """Зашитые отпечатки соответствуют реальным сертификатам (снимок gu-st.ru)."""
    errors = []
    файлы = {
        "root": FIXTURES / "russian_trusted_root_ca_pem.crt",
        "sub_2024": FIXTURES / "russian_trusted_sub_ca_2024_pem.crt",
        "sub_2022": FIXTURES / "russian_trusted_sub_ca_pem.crt",
    }
    for z in ca.КАТАЛОГ:
        check(errors, len(z["sha256"]) == 64 and set(z["sha256"]) <= set("0123456789abcdef"),
              "%s: sha256 не 64 hex" % z["id"])
        check(errors, z["url"].startswith("https://gu-st.ru/content/lending/"),
              "%s: url не с официального CDN Госуслуг" % z["id"])
        f = файлы.get(z["id"])
        check(errors, f is not None and f.is_file(), "%s: нет фикстуры %s" % (z["id"], f))
        if f and f.is_file():
            блобы = ca._der_blocks(f.read_bytes())
            check(errors, len(блобы) == 1, "%s: в фикстуре не один сертификат" % z["id"])
            try:
                ca._сверить(блобы[0], z)
            except ca.ОшибкаУстановки as e:
                errors.append("%s: зашитый отпечаток расходится с фикстурой: %s" % (z["id"], e))
    ids = [z["id"] for z in ca.КАТАЛОГ]
    check(errors, len(ids) == len(set(ids)), "дубли id в каталоге")
    check(errors, "root" in ids and "sub_2024" in ids, "в каталоге нет root/sub_2024")
    # установка из реальных фикстур во временный каталог — полный офлайн-путь --из-файла
    with tempfile.TemporaryDirectory() as tmp:
        res = ca.установить(ca_dir=tmp, из_файлов=[str(p) for p in файлы.values()])
        check(errors, res.get("статус") == "ok" and len(res.get("сертификаты", [])) == 3,
              "установка из реальных фикстур: %s" % res)
        бандл = ca._bundle_path(tmp)
        try:
            import ssl
            ctx = ssl.create_default_context()
            ctx.load_verify_locations(cafile=бандл)
            stats = ctx.cert_store_stats()
            check(errors, stats.get("x509_ca", 0) >= 3,
                  "ssl загрузил из бандла %s CA вместо ≥3" % stats.get("x509_ca"))
        except ssl.SSLError as e:
            errors.append("ssl не смог загрузить бандл: %s" % e)
    return errors


def main():
    ca = load_module("install_ca", ROOT / "scripts" / "install_ca.py")
    cases = [
        ("DER→PEM и распознавание по содержимому", case_der_pem),
        ("отказ при неверном отпечатке / незнакомом / нехватке", case_fingerprint_reject),
        ("запись бандла в COUNTERPARTY_CA_DIR и --удалить", case_bundle_write),
        ("реальные фикстуры: отпечатки, бандл, загрузка в ssl", case_real_fixtures),
    ]
    failed = 0
    for name, fn in cases:
        try:
            errors = fn(ca)
        except Exception as e:  # eval должен дойти до конца
            errors = ["исключение: %r" % (e,)]
        status = "PASS" if not errors else "FAIL"
        print("[%s] %s" % (status, name))
        for err in errors:
            print("       - %s" % err)
        failed += bool(errors)
    print("\nИтог: %d/%d PASS" % (len(cases) - failed, len(cases)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
