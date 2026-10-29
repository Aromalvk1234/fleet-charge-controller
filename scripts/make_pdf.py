"""Render docs/project-brief.html to a print-quality A4 PDF.

Headless Chrome prints the page (its @media print layout), then PyMuPDF adds
bookmarks, page numbers and document metadata.

    python scripts/make_pdf.py            # writes docs/project-brief.pdf
"""
import os
import re
import subprocess
import sys
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "docs" / "project-brief.html"
OUT = ROOT / "docs" / "project-brief.pdf"
RAW = ROOT / "results" / "brief_raw.pdf"

CHROME = next((p for p in (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome", "/usr/bin/chromium", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
) if os.path.exists(p)), None)


def render() -> None:
    if CHROME is None:
        sys.exit("Chrome or Edge not found")
    RAW.parent.mkdir(exist_ok=True)
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                    "--virtual-time-budget=12000", f"--print-to-pdf={RAW}", SRC.as_uri()],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def finish() -> None:
    doc = fitz.open(RAW)
    html = SRC.read_text(encoding="utf8")
    heads = [(m.group(1), re.sub(r"<[^>]+>", "", m.group(2)).strip())
             for m in re.finditer(r'<h2 id="([^"]+)"[^>]*>(.*?)</h2>', html)]
    heads.insert(0, ("summary", "Fleet Charge Controller"))
    toc, seen = [], 0
    for _, title in heads:
        for pno in range(seen, len(doc)):
            hit = any(s["size"] >= 15 and s["text"].strip() == title
                      for blk in doc[pno].get_text("dict")["blocks"] if blk["type"] == 0
                      for ln in blk["lines"] for s in ln["spans"])
            if hit:
                toc.append([1, title, pno + 1])
                seen = pno
                break
    doc.set_toc(toc)
    n = len(doc)
    for i, page in enumerate(doc):
        r = page.rect
        y = r.height - 24
        page.draw_line((36, y - 9), (r.width - 36, y - 9), color=(0.80, 0.83, 0.88), width=0.5)
        page.insert_text((36, y), "Fleet Charge Controller · Project brief", fontsize=7.5, fontname="helv", color=(0.35, 0.40, 0.48))
        label = f"Page {i + 1} of {n}"
        w = fitz.get_text_length(label, fontname="helv", fontsize=7.5)
        page.insert_text((r.width - 36 - w, y), label, fontsize=7.5, fontname="helv", color=(0.35, 0.40, 0.48))
    doc.set_metadata({"title": "Fleet Charge Controller: project brief", "author": "Aromal VK",
                      "subject": "Nebius x NVIDIA Global AI Hackathon, Physical AI track",
                      "keywords": "EV fleet charging, scheduling, MILP, LP, Nemotron, Nebius Token Factory"})
    doc.save(OUT, garbage=4, deflate=True)
    print(f"{OUT}  {n} pages, {len(toc)} bookmarks, {OUT.stat().st_size // 1024} KB")


if __name__ == "__main__":
    render()
    finish()
