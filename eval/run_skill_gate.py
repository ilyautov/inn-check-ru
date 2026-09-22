#!/usr/bin/env python3
"""
run_skill_gate.py — гейт разрезанного скилла.

SKILL.md читается агентом ЦЕЛИКОМ при каждом запуске, поэтому справочные куски
вынесены в `references/`. Разрезание создаёт свой класс тихой деградации: файл
есть в репозитории, ссылки на него нет — и раздел просто исчезает из работы
агента; или ссылка есть, а в релизный ZIP файл не кладётся — и скилл, скачанный
с GitHub Release, теряет кусок молча. Гейт закрывает обе дыры, а заодно следит,
чтобы SKILL.md не отрос обратно.

Чистый stdlib, офлайн, гоняется в CI.
"""

import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "SKILL.md"
REFS = ROOT / "references"

# Потолок SKILL.md. Взят с запасом ~8 КБ к текущему размеру: гейт должен
# кусаться, когда файл снова поедет вверх, а не через три релиза после этого.
ПОТОЛОК_БАЙТ = 60 * 1024

# Вынесенная история версий: её флаги проверять бессмысленно (см. ниже).
ИСТОРИЯ = "izmeneniya.md"


def кейсы():
    текст = SKILL.read_text(encoding="utf-8")
    ссылки = set(re.findall(r"references/([A-Za-zА-Яа-яёЁ0-9_-]+\.md)", текст))
    файлы = {p.name for p in REFS.glob("*.md")} if REFS.is_dir() else set()

    yield ("references существует", REFS.is_dir(),
           "нет каталога references/ — разрезание скилла сломано")

    битые = sorted(n for n in ссылки if not (REFS / n).is_file())
    yield ("ссылки из SKILL.md резолвятся", not битые,
           "SKILL.md ссылается на несуществующие файлы: %s" % ", ".join(битые))

    сироты = sorted(файлы - ссылки)
    yield ("сирот в references нет", not сироты,
           "файлы есть, ссылок из SKILL.md нет — агент их не откроет: %s"
           % ", ".join(сироты))

    пустые = sorted(n for n in файлы if len((REFS / n).read_text(encoding="utf-8")) < 500)
    yield ("вынесенные файлы не пусты", not пустые,
           "подозрительно короткие: %s" % ", ".join(пустые))

    # Без «когда читать» вынесенный файл не откроется никогда: агент не угадывает.
    без_условия = sorted(n for n in файлы
                         if "Читать" not in (REFS / n).read_text(encoding="utf-8")[:1200])
    yield ("у каждого файла сказано, когда его читать", not без_условия,
           "нет условия чтения в первых строках: %s" % ", ".join(без_условия))

    # Имена файлов — ASCII осознанно. Содержимое русское, а имя едет в ZIP, в
    # ссылку на GitHub и в файловую систему хоста: кириллица в любом из этих
    # трёх мест может не доехать, и раздел пропадёт молча.
    не_ascii = sorted(n for n in файлы if not n.isascii())
    yield ("имена вынесенных файлов в ASCII", not не_ascii,
           "кириллица в имени доедет не до каждого хоста: %s" % ", ".join(не_ascii))

    # Скилл — это инструкция к скриптам: обещание «запусти scripts/X.py --флаг»,
    # которого нет, агент выполнит и получит ошибку вместо отказа. Проверяем то,
    # что обещано в SKILL.md и вынесенных файлах.
    существуют = {f.name for f in (ROOT / "scripts").glob("*.py")}
    корпус = [(SKILL.name, текст)] + [(n, (REFS / n).read_text(encoding="utf-8"))
                                      for n in sorted(файлы)]
    нет_скрипта, нет_флага = [], []
    for имя, содержимое in корпус:
        for скрипт in set(re.findall(r"scripts/([a-z_0-9]+\.py)", содержимое)):
            if скрипт not in существуют:
                нет_скрипта.append("%s → scripts/%s" % (имя, скрипт))
                continue
        if имя == ИСТОРИЯ:
            # Changelog описывает прошлые версии: флаг, переименованный позже,
            # там остаётся исторической записью, а не обещанием агенту.
            continue
        # Флаги проверяются построчно и против ВСЕХ скриптов, названных в
        # строке: «affiliates_graph.py | droblenie_check.py --stdin» — обычная
        # конвейерная запись, и привязывать флаг к первому скрипту нельзя.
        for строка in содержимое.split("\n"):
            в_строке = [s for pair in re.findall(r"scripts/([a-z_0-9]+\.py)|"
                                                 r"\b([a-z_0-9]+\.py)\b", строка)
                        for s in pair if s and s in существуют]
            if not в_строке:
                continue
            исходники = "".join((ROOT / "scripts" / s).read_text(encoding="utf-8")
                                for s in dict.fromkeys(в_строке))
            for флаг in set(re.findall(r"`(--[а-яёa-z][а-яёa-z_-]+)`", строка)):
                if флаг not in исходники:
                    нет_флага.append("%s → %s %s"
                                     % (имя, "/".join(dict.fromkeys(в_строке)), флаг))
    yield ("скрипты, обещанные скиллом, существуют", not нет_скрипта,
           "ссылки в пустоту: %s" % ", ".join(sorted(set(нет_скрипта))))
    yield ("флаги, обещанные скиллом, существуют", not нет_флага,
           "флага нет в скрипте: %s" % ", ".join(sorted(set(нет_флага))))

    размер = len(текст.encode("utf-8"))
    yield ("SKILL.md в пределах потолка", размер <= ПОТОЛОК_БАЙТ,
           "SKILL.md %d байт > потолка %d — вынеси справочное в references/"
           % (размер, ПОТОЛОК_БАЙТ))

    yield ("safety-floor остался в SKILL.md", "## Safety-floor" in текст,
           "safety-floor нельзя выносить: он читается последним и не по ссылке")

    yield ("frontmatter с версией на месте",
           re.search(r'^\s*version:\s*"[^"]+"', текст, re.MULTILINE) is not None,
           "version во frontmatter — источник истины версионного гейта")

    # Ссылки из кода вида «SKILL.md «Проверка ИП»» после переноса раздела
    # указывают в пустоту. Заголовки ищем и в SKILL.md, и в вынесенных файлах.
    # Цитата в коде переносится по строкам, в скилле — по-своему, да ещё и
    # набрана жирным и с заглавной. Сравниваем по схлопнутым пробелам без
    # разметки и регистра: гейт должен ловить переехавший раздел, а не вёрстку.
    def плоско(s):
        return re.sub(r"\s+", " ", s.replace("*", "").replace("_", "")).lower()

    сырое_всё = текст + "".join((REFS / n).read_text(encoding="utf-8")
                                for n in sorted(файлы))
    протухшие = []
    for путь in sorted(list((ROOT / "scripts").glob("*.py"))
                       + list((ROOT / "mcp").glob("*.py"))):
        исходник = путь.read_text(encoding="utf-8")
        for m in re.finditer(r"(SKILL\.md|references/[\wА-Яа-яёЁ-]+\.md)"
                             r"([^)\n\"]{0,40})«([^»]{3,60})»", исходник):
            файл, раздел = m.group(1), m.group(3)
            # «см.» стоит и перед именем файла («см. SKILL.md «X»»), и после —
            # смотрим обе стороны, иначе навигация проскочит как цитата.
            связка = исходник[max(0, m.start() - 40):m.start()] + m.group(2)
            # Ссылка на конкретный вынесенный файл проверяется ПО НЕМУ: иначе
            # «см. references/сценарии.md «Проверка ИП»» останется зелёной после
            # переезда раздела в соседний файл — ровно та подмена, от которой гейт.
            if файл == "SKILL.md":
                где = сырое_всё
            elif (ROOT / файл).is_file():
                где = (ROOT / файл).read_text(encoding="utf-8")
            else:
                протухшие.append("%s → нет файла %s" % (путь.name, файл))
                continue
            # «см. ... «X»» — навигация: X обязан быть ЗАГОЛОВКОМ, иначе читатель
            # откроет файл и раздела не найдёт. Прочие «...» — цитаты прозы,
            # им достаточно совпасть с текстом.
            навигация = "см." in связка or "раздел" in связка
            если_где = "\n".join(l for l in где.split("\n")
                                  if l.startswith("#")) if навигация else где
            if плоско(раздел) not in плоско(если_где):
                протухшие.append("%s → %s «%s»%s" % (путь.name, файл, раздел,
                                                     " (нет такого заголовка)"
                                                     if навигация else ""))
    yield ("ссылки кода на разделы скилла живы", not протухшие,
           "раздел переехал или переименован: %s" % "; ".join(протухшие))

    # Релизный ZIP: файл, которого в архиве нет, у скачавшего скилл не существует.
    with tempfile.TemporaryDirectory() as tmp:
        zp = Path(tmp) / "skill.zip"
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "build_release_zip.py"),
                            "--output", str(zp), str(ROOT)],
                           capture_output=True, text=True, check=False)
        собрался = r.returncode == 0 and zp.is_file()
        yield ("релизный ZIP собирается", собрался,
               "build_release_zip.py: %s" % (r.stdout + r.stderr)[-300:])
        внутри = set(zipfile.ZipFile(zp).namelist()) if собрался else set()
    нет_в_архиве = sorted(n for n in файлы if "references/%s" % n not in внутри)
    yield ("вынесенные файлы едут в релизный ZIP", собрался and not нет_в_архиве,
           "в архиве нет: %s" % ", ".join(нет_в_архиве))


def main():
    провал = 0
    for имя, ок, почему in кейсы():
        print("%s %s%s" % ("PASS" if ок else "FAIL", имя, "" if ок else " — " + почему))
        провал += 0 if ок else 1
    print("RESULT: %s гейт скилла" % ("PASS" if not провал else "FAIL"))
    return 1 if провал else 0


if __name__ == "__main__":
    raise SystemExit(main())
