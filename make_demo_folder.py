#!/usr/bin/env python3
"""Build a realistically messy folder to try slothsort on (or record a demo).

    python make_demo_folder.py demo-downloads
    python slothsort.py demo-downloads

Every file is small and harmless. The content is what a real Downloads
folder tends to hold, so the model has something to read.
"""

import os
import struct
import sys
import time
import zipfile
import zlib
from pathlib import Path

TEXT_FILES = {
    "invoice_8812(1).txt": "TOKOPEDIA - INVOICE INV/20261003/88123\nItem: Wireless Headphones WH-200\nTotal: Rp 450.000\nPaid via GoPay",
    "download.txt": "Dear tenant,\nThis letter confirms your lease renewal for Apartment 4B, 12 Elm Street, from 1 November 2026 to 31 October 2027.\nMonthly rent: $1,250.",
    "notes(2).md": "# Trip to Bali\n- Flight GA-404 Fri 07:15\n- Hotel: Ubud Garden, 3 nights\n- Rent scooter, visit Tegallalang rice terraces",
    "export_2026.csv": "date,category,amount\n2026-09-01,groceries,82.10\n2026-09-03,transport,15.00\n2026-09-04,rent,1250.00",
    "untitled.json": '{"name": "weather-dashboard", "version": "1.0.0", "scripts": {"dev": "vite"}}',
    "script (1).py": "import requests\n\n# scrape hacker news front page titles\nfor item in requests.get('https://hacker-news.firebaseio.com/v0/topstories.json').json()[:10]:\n    print(item)",
    "receipt.txt": "GRAB RIDE RECEIPT\nPickup: Sudirman Station  Dropoff: Kemang\n21 Oct 2026  Fare: Rp 38.000",
}

DOCX_FILES = {
    "Doc1.docx": "Quarterly report Q3 2026 draft. Revenue grew 12 percent. Prepared for the board meeting on 30 October.",
    "Document (3).docx": "Cover letter. Dear hiring manager, I am applying for the Frontend Developer position at Acme Corp.",
    "CV_final_FINAL.docx": "Curriculum Vitae. Experience: 3 years React, TypeScript, Node.js. Education: BSc Computer Science.",
}

EMPTY_FILES = [
    "Screenshot 2026-10-01 101010.png",
    "Screenshot 2026-10-14 220145.png",
    "IMG_20260914_183022.jpg",
    "setup_v2.exe",
    "setup_v2 (1).exe",
    "VSCodeUserSetup-x64-1.105.0.exe",
    "Archive.zip",
    "slides-final-v3.pptx",
    "podcast_ep42.mp3",
    "the-pragmatic-programmer.epub",
    "big-file.mp4.crdownload",   # still downloading: slothsort leaves it alone
]


def tiny_png() -> bytes:
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    raw = b"\x00\xff\xcc\x66"
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def docx(path: Path, text: str) -> None:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        z.writestr("word/document.xml", f"<w:document><w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>")


def main() -> int:
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "demo-downloads")
    target.mkdir(parents=True, exist_ok=True)
    for name, text in TEXT_FILES.items():
        (target / name).write_text(text, encoding="utf-8")
    for name, text in DOCX_FILES.items():
        docx(target / name, text)
    for name in EMPTY_FILES:
        p = target / name
        if name.endswith(".png"):
            p.write_bytes(tiny_png())
        elif name.endswith(".zip"):
            with zipfile.ZipFile(p, "w") as z:
                z.writestr("readme.txt", "old project backup")
        else:
            p.write_bytes(b"\0" * 64)
    # Pretend everything arrived over the last few weeks, so --settle doesn't skip it.
    now = time.time()
    for i, p in enumerate(sorted(target.iterdir())):
        if p.is_file() and not p.name.endswith(".crdownload"):
            t = now - (i + 1) * 86400 * 1.7
            os.utime(p, (t, t))
    count = sum(1 for p in target.iterdir() if p.is_file())
    print(f"Made {count} messy files in {target.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
