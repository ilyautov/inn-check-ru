#!/usr/bin/env python3
"""Пересборка assets/social-preview.png из актуальных счётчиков.

Паттерн humanizer-ru: счётчики внутри растра не должны отставать от правды —
картинка собирается из тех же источников, что и остальное:
  - реестры  — строки таблицы каскада источников в SKILL.md;
  - сигналы  — флаги fin_scoring (вызовы флаг()), признаки droblenie_check
               (вызовы признак()) и пункты чеклиста однодневки в SKILL.md.
Палитра повторяет фирменную (docs/style.css семьи репозиториев автора).

Запуск:
    python3 scripts/make_social_preview.py            # перерисовать
    python3 scripts/make_social_preview.py --check    # гейт: растр на текущих счётчиках

Нужен Pillow (`pip install pillow`) и моноширинный шрифт с кириллицей
(macOS Menlo, Linux DejaVu Sans Mono).
"""

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets" / "social-preview.png"
STAMP = ROOT / "assets" / ".social-preview-counters"
SKILL = ROOT / "SKILL.md"
FIN = ROOT / "scripts" / "fin_scoring.py"
DROB = ROOT / "scripts" / "droblenie_check.py"

# Палитра семьи (docs/style.css humanizer-ru).
BG = (10, 13, 19)
BG_SOFT = (16, 21, 29)
BORDER = (35, 45, 60)
TEXT = (221, 229, 239)
TEXT_DIM = (148, 162, 180)
ACCENT = (122, 167, 255)
CTA = (255, 138, 61)
GOOD = (74, 222, 128)
WARN = (250, 204, 21)
BAD = (248, 113, 113)

FONT_CANDIDATES = (
    "/System/Library/Fonts/Menlo.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
)


def counters():
    """Источники истины — SKILL.md и скрипты, а не рукой вбитые числа."""
    skill = SKILL.read_text(encoding="utf-8")
    реестры = len(re.findall(r"^\|\s*[🟢🟡🔴⚪]", skill, re.MULTILINE))
    фин = len(re.findall(r"^\s+флаг\(\"", FIN.read_text(encoding="utf-8"),
                         re.MULTILINE))
    дроб = len(re.findall(r"^\s+признак\(\"",
                          DROB.read_text(encoding="utf-8"), re.MULTILINE))
    секция = skill.split("## Признаки однодневки")[1].split("## ")[0]
    однодневка = len(re.findall(r"^- ", секция, re.MULTILINE))
    return реестры, фин + дроб + однодневка


def _font(path, size, bold=False):
    from PIL import ImageFont
    try:  # у .ttc начертания по индексам: 0 обычное, 1 жирное
        return ImageFont.truetype(path, size,
                                  index=1 if bold and path.endswith(".ttc") else 0)
    except OSError:
        return ImageFont.truetype(path, size)


def draw(реестры, сигналы):
    from PIL import Image, ImageDraw

    font_path = next((p for p in FONT_CANDIDATES if Path(p).exists()), None)
    if font_path is None:
        raise SystemExit("[ошибка] не найден моноширинный шрифт с кириллицей")

    im = Image.new("RGB", (1200, 630), BG)
    d = ImageDraw.Draw(im)

    title = _font(font_path, 84, bold=True)
    sub = _font(font_path, 34)
    chip = _font(font_path, 22)
    foot = _font(font_path, 22)

    # Заголовок: «inn-check» и «ru» акцентом, следом квадрат-курсор.
    x = 70
    d.text((x, 120), "inn-check", font=title, fill=TEXT)
    x += int(d.textlength("inn-check", font=title))
    d.text((x, 120), "ru", font=title, fill=ACCENT)
    x += int(d.textlength("ru", font=title)) + 24
    d.rounded_rectangle((x, 146, x + 38, 200), radius=8, fill=CTA)

    d.text((70, 248), "Проверка контрагента по ИНН перед сделкой",
           font=sub, fill=TEXT_DIM)

    # Светофор — цветные круги (эмодзи в моношрифтах не живут).
    labels = ((GOOD, "можно работать"), (WARN, "осторожно"),
              (BAD, "избегать"))
    x = 70
    for color, label in labels:
        d.ellipse((x, 330, x + 30, 360), fill=color)
        d.text((x + 42, 330), label, font=chip, fill=TEXT_DIM)
        x += 42 + int(d.textlength(label, font=chip)) + 46

    chips = [
        ("%d реестров" % реестры, ACCENT),
        ("%d сигнала риска кодом" % сигналы, ACCENT),
        ("числа с источником и датой", TEXT_DIM),
        ("Apache-2.0", TEXT_DIM),
    ]
    x = 70
    for label, color in chips:
        w = int(d.textlength(label, font=chip)) + 32
        d.rounded_rectangle((x, 434, x + w, 478), radius=22, fill=BG_SOFT,
                            outline=BORDER, width=2)
        d.text((x + 16, 444), label, font=chip, fill=color)
        x += w + 14

    footer = "Claude Code · Codex CLI · Cursor · Gemini CLI · MCP"
    d.text((1130 - int(d.textlength(footer, font=foot)), 560), footer,
           font=foot, fill=TEXT_DIM)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    im.save(OUT)
    STAMP.write_text("%d/%d\n" % (реестры, сигналы), encoding="utf-8")
    print("[ok] %s: %d реестров, %d сигнала" % (OUT.relative_to(ROOT),
                                                реестры, сигналы))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="только проверить, что растр собран на текущих счётчиках")
    args = ap.parse_args()

    реестры, сигналы = counters()
    if args.check:
        want = "%d/%d" % (реестры, сигналы)
        have = (STAMP.read_text(encoding="utf-8").strip()
                if STAMP.exists() else "(нет отметки)")
        if have != want:
            print("[гейт] social-preview.png собран на %s, а в источниках %s.\n"
                  "       Перерисуйте: python3 scripts/make_social_preview.py"
                  % (have, want))
            return 1
        print("[гейт] ✓ social-preview.png на актуальных счётчиках (%s)" % want)
        return 0

    draw(реестры, сигналы)
    return 0


if __name__ == "__main__":
    sys.exit(main())
