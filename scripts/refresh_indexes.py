#!/usr/bin/env python3
"""
refresh_indexes.py — обновление локальных индексов по конфигу пользователя.
Только стандартная библиотека.

    python3 refresh_indexes.py --пример-конфига          # шаблон, ничего не пишет
    python3 refresh_indexes.py --прогон [--конфиг путь]
    python3 refresh_indexes.py --установить-расписание [--конфиг путь] [--день 5] [--час 4]

Без конфига пользователя ничего не качается: какие индексы обновлять и через
какую сеть — решает пользователь. Конфиг: ~/.config/inn-check-ru/refresh.json
(переопределяется INN_CHECK_REFRESH_CONFIG или --конфиг):

    {"индексы": {"erknm": {"сеть": "iface://en0"},
                 "sanctions_check": {}}}

«сеть» — "iface://<интерфейс или IPv4>" (выход через свою сеть), "env" (взять
INN_CHECK_PROXY из окружения прогона) или не указана (прямое соединение).
Системные HTTP(S)_PROXY/ALL_PROXY процессу модуля не передаются никогда, так что
«не указана» — действительно напрямую. Сеть можно задать только модулям, которые
ходят через dump_tools (СЕТЬ_ПОНИМАЮТ); остальные вызывают urlopen, прокси проекта
не знают, и «сеть» для них — ошибка конфига, а не тихий прямой запрос. Адрес
прокси с логином и паролем в конфиг не пишется — только в переменной окружения;
любой ключ, кроме «сеть», отвергается.

Каждый индекс обновляется отдельным процессом `<модуль>.py --refresh`: сбой
одного не роняет остальные. Статус модуля берётся из его JSON-вывода; «обновлено
k/n» при k < n — «частично», это не «ок». Учётки из URL в выводе вырезаются.
Код выхода прогона 1, если хотя бы один не «ок».

--установить-расписание НИЧЕГО НЕ УСТАНАВЛИВАЕТ: печатает строки crontab и
launchd plist (раз в месяц) с путями текущей установки. Значение
INN_CHECK_PROXY в них не печатается.
"""

import json
import os
import re
import shlex
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))

# модуль -> что обновляет; у всех CLI `--refresh`
ИНДЕКСЫ = {
    "opendata_refresh": "дампы открытых данных ФНС",
    "sanctions_check": "санкционные списки",
    "cbr_registries": "реестры ЦБ",
    "cbr_warning": "предупредительный список ЦБ",
    "disq_dump": "дисквалифицированные лица (ФНС)",
    "rzn_licenses": "лицензии Росздравнадзора",
    "rospatent_tz": "товарные знаки Роспатента",
    "fts_registries": "реестры ФТС",
    "rkn_registries": "реестры РКН",
    "rar_licenses": "лицензии РАР",
    "fsa_registries": "сертификаты и декларации Росаккредитации",
    "minpromtorg": "реестры Минпромторга",
    "uc_mintsifry": "аккредитованные УЦ Минцифры",
    "erknm": "проверки ЕРКНМ",
}
# сайты, которые не-РФ адресам отвечают отказом (проверено живьём 28–29.09.2026)
НУЖЕН_РФ_IP = ("fts_registries", "rkn_registries", "rar_licenses", "fsa_registries",
               "minpromtorg", "uc_mintsifry", "erknm")

# ходят через dump_tools.opener -> proxy.py и понимают INN_CHECK_PROXY
СЕТЬ_ПОНИМАЮТ = НУЖЕН_РФ_IP
ПРОКСИ_ОКРУЖЕНИЯ = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
                    "ALL_PROXY", "all_proxy")

МЕТКА_LAUNCHD = "tech.aifrontier.inn-check-ru.refresh"


def путь_конфига(путь=None):
    return (путь or os.environ.get("INN_CHECK_REFRESH_CONFIG")
            or os.path.join(os.path.expanduser("~"), ".config", "inn-check-ru",
                            "refresh.json"))


def пример():
    return {"индексы": {м: ({"сеть": "iface://en0"} if м in НУЖЕН_РФ_IP else {})
                        for м in ИНДЕКСЫ}}


def прочитать(путь=None):
    """Конфиг -> {модуль: сеть|None}. Ошибка -> ValueError с понятной причиной."""
    путь = путь_конфига(путь)
    try:
        with open(путь, encoding="utf-8") as f:
            данные = json.load(f)
    except FileNotFoundError:
        raise ValueError("конфига нет (%s) — создайте его: --пример-конфига" % путь) from None
    except (OSError, ValueError) as e:
        raise ValueError("конфиг не читается (%s): %s" % (путь, e)) from None
    индексы = данные.get("индексы") if isinstance(данные, dict) else None
    if not isinstance(индексы, dict) or not индексы:
        raise ValueError("в конфиге нет непустого объекта «индексы»")
    итог = {}
    for модуль, опции in индексы.items():
        if модуль not in ИНДЕКСЫ:
            raise ValueError("неизвестный индекс «%s»; есть: %s"
                             % (модуль, ", ".join(ИНДЕКСЫ)))
        if not isinstance(опции, dict):
            raise ValueError("«%s»: ожидался объект" % модуль)  # noqa: TRY004 — ошибка конфига
        if set(опции) - {"сеть"}:
            raise ValueError("«%s»: допустим только ключ «сеть»" % модуль)
        сеть = опции.get("сеть")
        if сеть is not None and модуль not in СЕТЬ_ПОНИМАЮТ:
            raise ValueError("«%s»: этот модуль ходит только напрямую, «сеть» ему не задать"
                             % модуль)
        if сеть is not None and сеть != "env" and not (
                isinstance(сеть, str) and сеть.startswith("iface://") and len(сеть) > 8
                and "@" not in сеть):
            # не повторяем значение: там может оказаться логин и пароль
            raise ValueError("«%s»: сеть — только \"iface://…\" или \"env\"; адрес прокси "
                             "держите в INN_CHECK_PROXY, не в файле" % модуль)
        итог[модуль] = сеть
    return итог


def окружение(сеть, база=None):
    env = dict(os.environ if база is None else база)
    for имя in ПРОКСИ_ОКРУЖЕНИЯ:
        env.pop(имя, None)
    if сеть == "env":
        if not env.get("INN_CHECK_PROXY"):
            return None
    elif сеть:
        env["INN_CHECK_PROXY"] = сеть
    else:
        env.pop("INN_CHECK_PROXY", None)
    return env


def _без_учёток(текст):
    """Логин и пароль из любых URL в выводе модуля — вон: итог прогона идёт в лог."""
    # до последнего «@» перед путём: в пароле бывает свой «@»
    return re.sub(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/]*@", r"\1***@", текст)


def _статус(р):
    """Результат процесса -> (статус, вывод). JSON-вывод модуля читается целиком."""
    текст = (р.stdout or "").strip()
    try:
        данные = json.loads(текст)
    except ValueError:
        данные = None
    if isinstance(данные, dict) and isinstance(данные.get("статус"), str):
        вывод = данные["статус"]
    else:
        строки = (текст or (р.stderr or "").strip()).splitlines()
        вывод = строки[-1] if строки else ""
    if р.returncode != 0 or вывод.startswith("индекс не обновлён"):
        статус = "ошибка"
    else:
        m = re.search(r"(\d+)\s*/\s*(\d+)", вывод)
        статус = "частично" if m and int(m.group(1)) < int(m.group(2)) else "ок"
    return статус, _без_учёток(вывод)[:300]


def прогон(путь=None, запуск=subprocess.run):
    индексы = прочитать(путь)
    итог, упали = {}, 0
    for модуль, сеть in индексы.items():
        env = окружение(сеть)
        if env is None:
            итог[модуль] = {"статус": "пропущен", "причина": "сеть \"env\", а INN_CHECK_PROXY "
                                                             "в окружении не задана"}
            упали += 1
            continue
        try:
            р = запуск([sys.executable, os.path.join(_HERE, модуль + ".py"), "--refresh"],
                       env=env, capture_output=True, text=True, check=False)
            статус, вывод = _статус(р)
            итог[модуль] = {"статус": статус, "код": р.returncode, "вывод": вывод}
        except OSError as e:
            итог[модуль] = {"статус": "ошибка", "код": None, "вывод": _без_учёток(str(e))[:300]}
        упали += итог[модуль]["статус"] != "ок"
    return итог, упали


def расписание(путь=None, день=5, час=4, минута=0):
    путь = os.path.abspath(путь_конфига(путь))
    прочитать(путь)
    лог = os.path.join(os.path.expanduser("~"), ".cache", "inn-check-ru", "refresh.log")
    аргументы = [os.path.abspath(sys.executable), os.path.abspath(__file__), "--прогон",
                 "--конфиг", путь]
    # в crontab «%» — перевод строки, его экранируют
    cron = ("%d %d %d * * mkdir -p %s && %s >> %s 2>&1" % (
        минута, час, день, shlex.quote(os.path.dirname(лог)),
        " ".join(shlex.quote(а) for а in аргументы), shlex.quote(лог))).replace("%", "\\%")
    from xml.sax.saxutils import escape
    plist = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" \
"http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>%s</string>
  <key>ProgramArguments</key>
  <array>
%s
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Day</key><integer>%d</integer>
    <key>Hour</key><integer>%d</integer>
    <key>Minute</key><integer>%d</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>%s</string>
  <key>StandardErrorPath</key>
  <string>%s</string>
</dict>
</plist>""" % (МЕТКА_LAUNCHD, "\n".join("    <string>%s</string>" % escape(a) for a in аргументы),
               день, час, минута, escape(лог), escape(лог))
    текст = [
        "Ничего не установлено — ниже готовые строки, поставьте их сами.",
        "Индексы и сеть берутся из конфига %s при каждом прогоне." % путь,
        "Если в конфиге есть \"env\", добавьте INN_CHECK_PROXY в окружение задания сами.",
        "",
        "crontab, %d-го числа каждого месяца в %02d:%02d:" % (день, час, минута),
        "  crontab -e   # и вставьте строку:",
        "  " + cron,
        "",
        "launchd (macOS):",
        "  mkdir -p %s   # каталог лога: launchd сам его не создаст"
        % shlex.quote(os.path.dirname(лог)),
        "  сохраните plist ниже как ~/Library/LaunchAgents/%s.plist" % МЕТКА_LAUNCHD,
        "  launchctl load ~/Library/LaunchAgents/%s.plist" % МЕТКА_LAUNCHD,
        "",
        plist,
    ]
    return {"cron": cron, "plist": plist, "текст": "\n".join(текст)}


def _опция(args, имя):
    return args[args.index(имя) + 1] if имя in args and args.index(имя) + 1 < len(args) \
        else None


def main(argv):
    args = argv[1:]
    конфиг = _опция(args, "--конфиг")
    try:
        if "--пример-конфига" in args:
            print(json.dumps(пример(), ensure_ascii=False, indent=2))
            return 0
        if "--прогон" in args:
            итог, упали = прогон(конфиг)
            print(json.dumps(итог, ensure_ascii=False, indent=2))
            return 1 if упали else 0
        if "--установить-расписание" in args:
            числа = {к: int(_опция(args, "--" + к)) for к in ("день", "час")
                     if _опция(args, "--" + к)}
            if not (1 <= числа.get("день", 5) <= 28 and 0 <= числа.get("час", 4) <= 23):
                raise ValueError("день — 1..28, час — 0..23")
            print(расписание(конфиг, **числа)["текст"])
            return 0
    except ValueError as e:
        sys.stderr.write("ошибка: %s\n" % e)
        return 2
    sys.stderr.write(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
