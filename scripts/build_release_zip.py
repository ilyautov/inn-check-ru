#!/usr/bin/env python3
"""Сборка релизного ZIP скилла для claude.ai (паттерн humanizer-ru).

В архиве: SKILL.md в корне + references/ (вынесенные разделы скилла, без них
скачанный скилл теряет куски молча) + scripts/ + data/ + LICENSE — ровно то,
что нужно скиллу; eval/, mcp/, .github и прочая инфраструктура репозитория не едут.
"""

import argparse
import zipfile
from pathlib import Path

INCLUDE_FILES = ("SKILL.md", "LICENSE")
INCLUDE_DIRS = ("scripts", "data", "references")
BAD_NAMES = {".DS_Store", "__pycache__"}
BAD_SUFFIXES = {".pyc"}
# инфраструктура репозитория (релизная машина, генератор картинки, секьюрити-хуки)
EXCLUDE_FILES = {"scripts/build_release_zip.py", "scripts/make_social_preview.py"}
EXCLUDE_PREFIXES = ("scripts/security/",)


def build_release_zip(repo_root: Path, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in INCLUDE_FILES:
            path = repo_root / name
            if not path.is_file():
                raise FileNotFoundError("нет обязательного файла: %s" % path)
            archive.write(path, name)
        for dirname in INCLUDE_DIRS:
            src = repo_root / dirname
            if not src.is_dir():
                raise FileNotFoundError("нет обязательной папки: %s" % src)
            for path in sorted(src.rglob("*")):
                if path.is_dir():
                    continue
                rel = path.relative_to(repo_root)
                if any(part in BAD_NAMES for part in rel.parts) \
                        or path.suffix in BAD_SUFFIXES \
                        or rel.as_posix() in EXCLUDE_FILES \
                        or rel.as_posix().startswith(EXCLUDE_PREFIXES):
                    continue
                archive.write(path, rel.as_posix())


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Собрать inn-check-ru.zip для GitHub Release.")
    parser.add_argument("--output", default="dist/inn-check-ru.zip",
                        help="куда писать ZIP (по умолчанию dist/inn-check-ru.zip)")
    parser.add_argument("repo_root", nargs="?", default=".",
                        help="корень репозитория")
    args = parser.parse_args()

    repo_root = Path(args.repo_root).expanduser().resolve()
    output = Path(args.output).expanduser()
    if not output.is_absolute():
        output = repo_root / output

    try:
        build_release_zip(repo_root, output)
    except OSError as exc:
        print("ERROR: %s" % exc)
        print("RESULT: FAIL build-release-zip")
        return 1

    print("RELEASE_ZIP: %s" % output)
    print("RESULT: PASS build-release-zip")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
