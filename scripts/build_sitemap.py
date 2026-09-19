#!/usr/bin/env python3
"""Карта сайта docs/: lastmod считается по git, а не правится руками.
Паттерн humanizer-ru scripts/build_sitemap.py.

    python3 scripts/build_sitemap.py            # записать
    python3 scripts/build_sitemap.py --check    # сверить, ничего не трогая
"""

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
SITE = "https://inn-check-ru.aifrontier.tech"
PAGES = [
    ("index.html", "/", "weekly", "1.0"),
    ("proverit-kontragenta-po-inn.html", "/proverit-kontragenta-po-inn.html", "monthly", "0.9"),
    ("proverka-ip-po-inn.html", "/proverka-ip-po-inn.html", "monthly", "0.8"),
    ("priznaki-odnodnevki.html", "/priznaki-odnodnevki.html", "monthly", "0.8"),
    ("droblenie-biznesa.html", "/droblenie-biznesa.html", "monthly", "0.8"),
    ("monitoring-kontragentov.html", "/monitoring-kontragentov.html", "monthly", "0.7"),
]


def last_commit(rel):
    out = subprocess.run(["git", "log", "-1", "--format=%cs", "--", "docs/%s" % rel],
                         cwd=ROOT, capture_output=True, text=True, check=True)
    return out.stdout.strip() or "1970-01-01"


def build():
    missing = [rel for rel, *_ in PAGES if not (DOCS / rel).exists()]
    if missing:
        raise SystemExit("в docs нет страниц из списка: %s" % ", ".join(missing))
    body = "\n".join(
        "  <url>\n    <loc>%s%s</loc>\n"
        "    <lastmod>%s</lastmod>\n"
        "    <changefreq>%s</changefreq>\n    <priority>%s</priority>\n  </url>"
        % (SITE, url, last_commit(rel), freq, pri)
        for rel, url, freq, pri in PAGES
    )
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            "%s\n</urlset>\n" % body)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    target = DOCS / "sitemap.xml"
    fresh = build()
    if a.check:
        if not target.exists() or target.read_text(encoding="utf-8") != fresh:
            print("карта сайта разошлась с датами коммитов", file=sys.stderr)
            sys.exit(1)
        print("карта сайта совпадает с датами коммитов")
        return
    target.write_text(fresh, encoding="utf-8")
    print("записано: docs/sitemap.xml (%d адресов)" % len(PAGES))


if __name__ == "__main__":
    main()
