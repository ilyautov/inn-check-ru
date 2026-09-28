#!/usr/bin/env python3
"""
run_refresh_indexes_eval.py — офлайн-eval refresh_indexes.py: без конфига ничего
не запускается, адрес прокси в конфиге отвергается и не повторяется в ошибке,
сеть каждого индекса попадает только в его процесс, сбой одного не роняет
остальные, расписание ничего не ставит и не печатает INN_CHECK_PROXY.
PASS/FAIL, stdlib, CI.
"""

import importlib.util
import json
import os
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load(имя):
    spec = importlib.util.spec_from_file_location(имя, ROOT / "scripts" / ("%s.py" % имя))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check(errors, cond, msg):
    if not cond:
        errors.append(msg)


def записать(td, данные):
    путь = os.path.join(td, "refresh.json")
    with open(путь, "w", encoding="utf-8") as f:
        f.write(данные if isinstance(данные, str) else json.dumps(данные, ensure_ascii=False))
    return путь


def ошибка(ri, путь):
    try:
        ri.прочитать(путь)
    except ValueError as e:
        return str(e)
    return None


def case_конфиг(ri):
    errors = []
    with tempfile.TemporaryDirectory() as td:
        нет = os.path.join(td, "нет.json")
        check(errors, "конфига нет" in (ошибка(ri, нет) or ""), ошибка(ri, нет))
        вызовы = []
        try:
            ri.прогон(нет, запуск=lambda *a, **k: вызовы.append(a))
            errors.append("прогон без конфига не отказал")
        except ValueError:
            pass
        check(errors, not вызовы, "без конфига что-то запущено")
        секрет = "http://логин:секрет@прокси.пример:3128"
        for метка, данные in (
                ("битый json", "{"),
                ("без индексов", {"индексы": {}}),
                ("чужой модуль", {"индексы": {"rm_rf": {}}}),
                ("опции не объект", {"индексы": {"erknm": "iface://en0"}}),
                ("прокси с учёткой", {"индексы": {"erknm": {"сеть": секрет}}}),
                ("iface с учёткой", {"индексы": {"erknm": {"сеть": "iface://логин:секрет@x"}}}),
                ("пустой iface", {"индексы": {"erknm": {"сеть": "iface://"}}}),
                ("учётка в чужом ключе", {"индексы": {"erknm": {"сеть": "env",
                                                                 "прокси": секрет}}}),
                ("сеть модулю на urlopen", {"индексы": {"sanctions_check": {
                    "сеть": "iface://en0"}}})):
            т = ошибка(ri, записать(td, данные))
            check(errors, т is not None and "секрет" not in т, "%s: %r" % (метка, т))
        путь = записать(td, {"индексы": {"erknm": {"сеть": "iface://en0"},
                                         "sanctions_check": {}, "minpromtorg": {"сеть": "env"}}})
        env = ri.окружение(None, {"HTTPS_PROXY": "x", "http_proxy": "x", "ALL_PROXY": "x",
                                  "INN_CHECK_PROXY": "x", "PATH": "/bin"})
        check(errors, env == {"PATH": "/bin"}, env)
        env = ri.окружение("iface://en0", {"HTTPS_PROXY": "x"})
        check(errors, env == {"INN_CHECK_PROXY": "iface://en0"}, env)
        check(errors, ri._без_учёток("через http://логин:па@роль@хост:1/п и socks5://a@b")
              == "через http://***@хост:1/п и socks5://***@b", ri._без_учёток("x"))
        check(errors, ri.прочитать(путь) == {"erknm": "iface://en0", "sanctions_check": None,
                                            "minpromtorg": "env"}, ri.прочитать(путь))
    пример = ri.пример()["индексы"]
    check(errors, set(пример) == set(ri.ИНДЕКСЫ) and пример["erknm"] == {"сеть": "iface://en0"}
          and пример["sanctions_check"] == {}, пример)
    for м in ri.ИНДЕКСЫ:
        check(errors, (ROOT / "scripts" / (м + ".py")).exists()
              and "--refresh" in (ROOT / "scripts" / (м + ".py")).read_text(encoding="utf-8"),
              "у %s нет --refresh" % м)
    return "конфиг", errors


def case_прогон(ri):
    errors = []
    with tempfile.TemporaryDirectory() as td:
        путь = записать(td, {"индексы": {"erknm": {"сеть": "iface://en0"},
                                         "sanctions_check": {}, "minpromtorg": {"сеть": "env"},
                                         "uc_mintsifry": {}}})
        вызовы = []

        def запуск(cmd, env, **k):
            вызовы.append((cmd, env.get("INN_CHECK_PROXY")))
            if cmd[1].endswith("uc_mintsifry.py"):
                raise OSError("нет файла")
            if cmd[1].endswith("sanctions_check.py"):
                return types.SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(
                    {"статус": "обновлено 1/3 списков", "результаты": []}, indent=2))
            if cmd[1].endswith("erknm.py"):
                return types.SimpleNamespace(returncode=0, stdout="шаг\nитог 0\n", stderr="")
            return types.SimpleNamespace(returncode=1, stdout="", stderr=(
                "шаг\nотказ через socks5://логин:секрет@x:1080\n"))

        старое = os.environ.get("INN_CHECK_PROXY")
        os.environ["INN_CHECK_PROXY"] = "socks5://логин:секрет@x:1080"
        try:
            итог, упали = ri.прогон(путь, запуск=запуск)
            os.environ.pop("INN_CHECK_PROXY")
            итог2, упали2 = ri.прогон(путь, запуск=lambda cmd, env, **k:
                                      types.SimpleNamespace(returncode=0, stdout="", stderr=""))
        finally:
            if старое is None:
                os.environ.pop("INN_CHECK_PROXY", None)
            else:
                os.environ["INN_CHECK_PROXY"] = старое
        сеть = {os.path.basename(c[1]): p for c, p in вызовы}
        check(errors, сеть == {"erknm.py": "iface://en0", "sanctions_check.py": None,
                               "minpromtorg.py": "socks5://логин:секрет@x:1080",
                               "uc_mintsifry.py": None}, сеть)
        check(errors, all(c[2:] == ["--refresh"] for c, _ in вызовы), вызовы)
        check(errors, упали == 3 and итог["erknm"] == {"статус": "ок", "код": 0, "вывод": "итог 0"}
              and итог["sanctions_check"] == {"статус": "частично", "код": 0,
                                               "вывод": "обновлено 1/3 списков"}
              and итог["minpromtorg"] == {"статус": "ошибка", "код": 1,
                                           "вывод": "отказ через socks5://***@x:1080"}
              and итог["uc_mintsifry"] == {"статус": "ошибка", "код": None,
                                            "вывод": "нет файла"}, итог)
        итог3, _ = ri.прогон(путь, запуск=lambda cmd, env, **k: types.SimpleNamespace(
            returncode=0, stderr="", stdout='{"статус": "индекс не обновлён: другой refresh"}'))
        check(errors, итог3["erknm"]["статус"] == "ошибка", итог3)
        check(errors, упали2 == 1 and итог2["minpromtorg"]["статус"] == "пропущен"
              and итог2["erknm"]["статус"] == "ок", итог2)
    return "прогон", errors


def case_расписание(ri):
    errors = []
    with tempfile.TemporaryDirectory() as td:
        td = os.path.join(td, "мой каталог 100%")
        os.mkdir(td)
        путь = записать(td, {"индексы": {"erknm": {"сеть": "env"}}})
        старое = os.environ.get("INN_CHECK_PROXY")
        os.environ["INN_CHECK_PROXY"] = "http://логин:секрет@x:3128"
        try:
            р = ri.расписание(путь, день=7, час=3)
        finally:
            if старое is None:
                os.environ.pop("INN_CHECK_PROXY", None)
            else:
                os.environ["INN_CHECK_PROXY"] = старое
        check(errors, "секрет" not in р["текст"] and "Ничего не установлено" in р["текст"], р)
        check(errors, р["cron"].startswith("0 3 7 * * mkdir -p ")
              and ("'--конфиг' '%s'" % путь.replace("%", "\\%")) in р["cron"]
              and "100%/" not in р["cron"] and "100%/refresh.json</string>" in р["plist"]
              and "<key>Day</key><integer>7</integer>" in р["plist"], р["cron"])
        try:
            ri.расписание(os.path.join(td, "нет.json"))
            errors.append("расписание без конфига")
        except ValueError:
            pass
        check(errors, ri.main(["x", "--установить-расписание", "--конфиг", путь,
                               "--день", "31"]) == 2, "день 31 принят")
        check(errors, ri.main(["x", "--прогон", "--конфиг", os.path.join(td, "нет")]) == 2,
              "прогон без конфига: код")
    return "расписание", errors


def main():
    ri = load("refresh_indexes")
    failed = 0
    for case in (case_конфиг, case_прогон, case_расписание):
        имя, errors = case(ri)
        if errors:
            failed += 1
            print("FAIL %s" % имя)
            for e in errors:
                print("  - %s" % (e,))
        else:
            print("PASS %s" % имя)
    print("PASS: все кейсы зелёные" if not failed else "FAIL: %d кейсов упало" % failed)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
