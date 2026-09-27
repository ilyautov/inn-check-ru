#!/usr/bin/env python3
"""
run_extension_eval.py — браузерное расширение (extension/) офлайн: манифест MV3
без лишних прав (нет host_permissions и content_scripts), иконки PNG, в окне нет
inline-скриптов и удалённого кода; сайты, поля сигналов, профили и имя хоста
совпадают с Python-стороной (native_host, manual_block, profiles,
install_native_host); чистые функции lib.js — в Node. PASS/FAIL, CI.
"""

import importlib.util
import json
import re
import shutil
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXT = ROOT / "extension"
sys.path.insert(0, str(ROOT / "scripts"))


def load(имя):
    spec = importlib.util.spec_from_file_location(имя, ROOT / "scripts" / (имя + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check(errors, cond, msg):
    if not cond:
        errors.append(msg)


def версия_скилла():
    fm = (ROOT / "SKILL.md").read_text(encoding="utf-8").split("---")[1]
    return re.search(r'^\s*version:\s*"([^"]+)"', fm, re.MULTILINE).group(1)


def case_манифест():
    errors = []
    м = json.loads((EXT / "manifest.json").read_text(encoding="utf-8"))
    check(errors, м.get("manifest_version") == 3, "не MV3")
    check(errors, м.get("version") == версия_скилла(),
          "версия %r ≠ SKILL.md %r" % (м.get("version"), версия_скилла()))
    check(errors, sorted(м.get("permissions", [])) ==
          sorted(["activeTab", "contextMenus", "nativeMessaging", "storage"]),
          "права: %r" % м.get("permissions"))
    for лишнее in ("host_permissions", "optional_host_permissions", "content_scripts",
                   "optional_permissions", "externally_connectable", "web_accessible_resources"):
        check(errors, лишнее not in м, "в манифесте %s — расширение читает страницы только "
              "по клику (activeTab)" % лишнее)
    иконки = dict(м.get("icons", {}))
    иконки.update(м.get("action", {}).get("default_icon", {}))
    for размер, путь in иконки.items():
        файл = EXT / путь
        check(errors, путь.endswith(".png") and файл.is_file(), "иконка %s: %s" % (размер, путь))
        if файл.is_file():
            голова = файл.read_bytes()[:24]
            check(errors, голова[:8] == b"\x89PNG\r\n\x1a\n", "%s не PNG" % путь)
            w, h = struct.unpack(">II", голова[16:24])
            check(errors, (w, h) == (int(размер), int(размер)), "%s: %dx%d" % (путь, w, h))
    for файл in ("background.js", "popup.html", "popup.js", "lib.js"):
        check(errors, (EXT / файл).is_file(), "нет %s" % файл)
    html = (EXT / "popup.html").read_text(encoding="utf-8")
    check(errors, not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html),
          "inline <script> в popup.html (CSP MV3)")
    check(errors, not re.search(r"\son[a-z]+\s*=", html), "inline-обработчики в popup.html")
    # запись и снимок — через фоновый процесс со сверкой отправителя; снимок
    # сверяется со страницей после захвата (ревью Codex)
    bg = (EXT / "background.js").read_text(encoding="utf-8")
    check(errors, "своёОкно(отправитель" in bg, "background.js не сверяет отправителя")
    pj = (EXT / "popup.js").read_text(encoding="utf-8")
    захват = pj.find("captureVisibleTab(")
    check(errors, захват > 0 and "таЖеСтраница(" in pj[захват:],
          "popup.js не сверяет страницу после снимка")
    check(errors, "chrome.runtime.sendNativeMessage" not in pj
          and "connectNative" not in pj, "окно пишет в хост само — запись оборвётся "
          "при закрытии окна")
    check(errors, "chrome.runtime.connectNative(" in bg
          and "chrome.runtime.sendNativeMessage" not in bg,
          "фоновый процесс без порта connectNative: Chrome погасит его через 30 с")
    присвоений = pj.count('$("инн").value = последнийИнн')
    check(errors, присвоений == pj.count('() => { $("инн").value = последнийИнн'),
          "ИНН последней проверки подставляется в форму без клика")
    for js in EXT.glob("*.js"):
        код = js.read_text(encoding="utf-8")
        for плохо in (r"\beval\(", r"new Function\(", r"https?://[^\s\"'`]+\.js",
                      r"innerHTML\s*=", r"importScripts\("):
            check(errors, not re.search(плохо, код), "%s: %s" % (js.name, плохо))
    return errors


def _экспорт_lib():
    код = ("import(%s).then(m => console.log(JSON.stringify({ХОСТ: m.ХОСТ, ХОСТЫ: m.ХОСТЫ, "
           "ПОЛЯ: m.ПОЛЯ})))" % json.dumps((EXT / "lib.js").as_uri()))
    out = subprocess.run(["node", "--input-type=module", "-e", код], capture_output=True,
                         text=True, timeout=60, check=True)
    return json.loads(out.stdout)


def case_сверка_с_python():
    errors = []
    lib = _экспорт_lib()
    хост, mb, inst = load("native_host"), load("manual_block"), load("install_native_host")
    import profiles
    check(errors, lib["ХОСТ"] == inst.ИМЯ, "имя хоста: %r ≠ %r" % (lib["ХОСТ"], inst.ИМЯ))
    check(errors, {k: list(v) for k, v in хост.ХОСТЫ.items()} == lib["ХОСТЫ"],
          "сайты: lib %r ≠ хост %r" % (lib["ХОСТЫ"], хост.ХОСТЫ))
    for блок, поля in lib["ПОЛЯ"].items():
        check(errors, [п["имя"] for п in поля] == list(mb.ПОЛЯ.get(блок, {})),
              "%s: поля формы %r ≠ manual_block %r" % (
                  блок, [п["имя"] for п in поля], list(mb.ПОЛЯ.get(блок, {}))))
        обязательные = {п["имя"] for п in поля if п["обязательно"]}
        check(errors, обязательные == set(profiles._поля_сигналов(блок)),
              "%s: обязательные %r ≠ поля сигналов %r" % (
                  блок, sorted(обязательные), profiles._поля_сигналов(блок)))
    check(errors, set(lib["ПОЛЯ"]) == set(mb.ПОЛЯ), "блоки формы ≠ manual_block")
    popup = (EXT / "popup.js").read_text(encoding="utf-8")
    m = re.search(r"const ПРОФИЛИ = \[(.*?)\];", popup, re.DOTALL)
    в_окне = re.findall(r'"([^"]+)"', m.group(1)) if m else []
    check(errors, в_окне == list(profiles.load()["профили"]),
          "профили окна %r ≠ data/profiles_ru.json" % в_окне)
    return errors


ТЕСТ_LIB = r"""
import * as m from %s;
const ош = [];
const ok = (c, s) => { if (!c) ош.push(s); };
ok(m.иннВерен("7707083893") && m.иннВерен("504110181262") && !m.иннВерен("7707083894"), "контрольная");
ok(m.иннИзТекста("Поставщик ООО Тест, ИНН 7707083893, КПП 770701001") === "7707083893", "ИНН в тексте");
ok(m.иннИзТекста("ИНН 7707 083893") === "7707083893", "ИНН с пробелом");
ok(m.иннИзТекста("ИНН 7707 083893") === "7707083893", "ИНН с NBSP");
ok(m.иннИзТекста("12 7707083893") === "7707083893", "соседнее число");
ok(m.иннИзТекста("ОГРН 1027700132195, дело А40-123456/2025") === null, "не ИНН");
ok(m.иннИзТекста("1234567890") === null, "битая сумма");
ok(m.источникПоUrl("https://kad.arbitr.ru/Card/x") === "суды", "kad");
ok(m.источникПоUrl("https://fssp.gov.ru/iss/ip") === "фссп", "fssp");
ok(m.источникПоUrl("https://www.fssp.gov.ru/") === "фссп", "поддомен");
for (const u of ["http://kad.arbitr.ru/", "https://kad.arbitr.ru.evil.ru/",
                 "https://evil.ru/?kad.arbitr.ru", "https://kad.arbitr.ru@evil.ru/",
                 "https://u:p@kad.arbitr.ru/", "javascript:alert(1)", "мусор",
                 "https://kad.arbitr.ru:8443/"]) {
  ok(m.источникПоUrl(u) === null, "не источник: " + u);
}
ok(m.сегодня(new Date(2026, 8, 3)) === "2026-09-03", "дата");
ok(m.новаяОперация("ab_cd-12/34") === "abcd-1234", "операция");
const форма = { инн: "7707083893", итог: "проверено", дата: "2026-09-28",
  поля: { ответчик_крупные: "нет", ответчик_дел: "3" }, найдено: " 3 дела ", параметры: "" };
let r = m.сообщениеБлока(форма, "https://kad.arbitr.ru/", "op-1");
ok(r.ошибки.length === 0 && r.сообщение.блок === "суды" && r.сообщение.поля.ответчик_крупные === false
   && r.сообщение.поля.ответчик_дел === "3" && r.сообщение.найдено === "3 дела"
   && !("параметры" in r.сообщение), "сообщение: " + JSON.stringify(r));
r = m.сообщениеБлока({ ...форма, поля: {} }, "https://kad.arbitr.ru/", "op-1");
ok(r.ошибки.some((e) => e.includes("Крупные иски")), "обязательное поле");
r = m.сообщениеБлока({ ...форма, итог: "не проверено", поля: {} }, "https://kad.arbitr.ru/", "op-1");
ok(r.ошибки.some((e) => e.includes("почему")), "причина при «не проверено»");
r = m.сообщениеБлока({ ...форма, поля: { ответчик_крупные: "да", ответчик_дел: "-1" } },
                      "https://kad.arbitr.ru/", "op-1");
ok(r.ошибки.some((e) => e.includes("число")), "отрицательное число");
r = m.сообщениеБлока(форма, "https://example.org/", "op-1");
ok(r.ошибки.some((e) => e.includes("страница")), "не сайт источника");
ok(m.снимокРазрешён("504110181262", true) !== null, "снимок ИП запрещён");
ok(m.снимокРазрешён("7707083893", false) !== null, "снимок без подтверждения");
ok(m.снимокРазрешён("7707083893", true) === null, "снимок юрлица с подтверждением");
const вкладка = { id: 7, url: "https://kad.arbitr.ru/Card/1" };
ok(m.таЖеСтраница({ ...вкладка }, вкладка), "та же страница");
ok(!m.таЖеСтраница({ ...вкладка, url: "https://kad.arbitr.ru/Card/2" }, вкладка), "другая страница");
ok(!m.таЖеСтраница({ ...вкладка, id: 8 }, вкладка) && !m.таЖеСтраница(undefined, вкладка), "другая вкладка");
const окно = "chrome-extension://abc/popup.html";
ok(m.своёОкно({ id: "abc", url: окно }, "abc", окно), "своё окно");
ok(!m.своёОкно({ id: "xyz", url: окно }, "abc", окно), "чужое расширение");
ok(!m.своёОкно({ id: "abc", url: "https://kad.arbitr.ru/" }, "abc", окно), "не окно");
console.log(JSON.stringify(ош));
"""


def case_lib_в_node():
    код = ТЕСТ_LIB % json.dumps((EXT / "lib.js").as_uri())
    out = subprocess.run(["node", "--input-type=module", "-e", код], capture_output=True,
                         text=True, timeout=60, check=False)
    if out.returncode != 0:
        return ["node упал: %s" % out.stderr[:500]]
    return json.loads(out.stdout.strip().splitlines()[-1])


def main():
    if not shutil.which("node"):
        print("FAIL нужен node (есть на раннерах GitHub) — проверка lib.js не состоялась")
        return 1
    cases = {
        "манифест-и-права": case_манифест(),
        "сверка-с-python": case_сверка_с_python(),
        "lib-в-node": case_lib_в_node(),
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
