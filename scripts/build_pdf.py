#!/usr/bin/env python3
"""Build docs/UKRLAW-AI-Infrastructure.pdf from README.md.

Needs: python-markdown, pypdf and a Chrome/Chromium binary (CHROME env var overrides the lookup).
Fonts: Inter, Inter Tight, JetBrains Mono are used when found in FONT_DIR (default scripts/fonts),
otherwise the system fonts apply.
"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import markdown
from pypdf import PdfReader, PdfWriter

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "README.md"
CSS = Path(__file__).resolve().parent / "print.css"
OUT = ROOT / "docs" / "UKRLAW-AI-Infrastructure.pdf"
BUILD = ROOT / "build"
FONT_DIR = Path(os.environ.get("FONT_DIR", Path(__file__).resolve().parent / "fonts"))
MAX_PAGES = 5

EMOJI = re.compile(r"^[\U0001F000-\U0001FAFF☀-➿⬀-⯿️‍\s]+")


def find_chrome():
    for c in (os.environ.get("CHROME"), "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
              "google-chrome", "chromium", "chromium-browser"):
        if c and (Path(c).exists() or shutil.which(c)):
            return c
    sys.exit("Chrome/Chromium not found: set CHROME=/path/to/chrome")


def font_faces():
    faces = []
    for family, fname in (("Inter", "Inter.ttf"), ("Inter Tight", "InterTight.ttf"), ("JetBrains Mono", "JetBrainsMono.ttf")):
        p = FONT_DIR / fname
        if p.exists():
            faces.append(f'@font-face{{font-family:"{family}";src:url("{p.as_uri()}");font-weight:100 900}}')
    return "\n".join(faces)


def to_html(md_text):
    md_text = re.sub(r"<!--\s*print:skip:start\s*-->.*?<!--\s*print:skip:end\s*-->", "", md_text, flags=re.S)
    html = markdown.markdown(md_text, extensions=["tables", "fenced_code", "sane_lists", "md_in_html"])
    html = re.sub(r"<(h[2-4])>(.*?)</\1>", lambda m: f"<{m.group(1)}>{EMOJI.sub('', m.group(2))}</{m.group(1)}>", html)
    return html.replace("<details>", "<details open>")


def main():
    BUILD.mkdir(exist_ok=True)
    body = to_html(SRC.read_text(encoding="utf-8"))
    page = (f'<!doctype html><html lang="uk"><head><meta charset="utf-8"><base href="{ROOT.as_uri()}/">'
            f"<title>Локальна AI-інфраструктура для генерації відео</title>"
            f"<style>{font_faces()}\n{CSS.read_text(encoding='utf-8')}</style></head><body>{body}</body></html>")
    html_path = BUILD / "document.html"
    html_path.write_text(page, encoding="utf-8")

    OUT.parent.mkdir(exist_ok=True)
    raw = BUILD / "raw.pdf"
    subprocess.run([find_chrome(), "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                    "--allow-file-access-from-files", "--virtual-time-budget=8000",
                    f"--print-to-pdf={raw}", html_path.as_uri()], check=True, capture_output=True)

    reader = PdfReader(raw)
    writer = PdfWriter()
    for p in reader.pages:
        writer.add_page(p)
    writer.add_metadata({"/Title": "Локальна AI-інфраструктура для генерації відео",
                         "/Author": "Bohdan Havryliuk", "/Subject": "UKRLAW · AI Infrastructure Engineer"})
    with open(OUT, "wb") as f:
        writer.write(f)
    n = len(reader.pages)
    print(f"{OUT.relative_to(ROOT)}: {n} pages")
    if n > MAX_PAGES:
        sys.exit(f"over the {MAX_PAGES}-page limit")


if __name__ == "__main__":
    main()
