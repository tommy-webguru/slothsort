#!/usr/bin/env python3
"""slothsort - a lazy, offline tidier for your Downloads folder.

A local open-weight model (Gemma via Ollama) peeks at each file, picks a
folder and a sensible name, and slothsort shows you the plan before it
moves anything. Every run can be undone. Nothing leaves your machine.

Standard library only. Python 3.9+.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

__version__ = "0.1.0"

STATE_DIR = ".slothsort"
HISTORY_FILE = "history.jsonl"
CACHE_FILE = "cache.json"

CATEGORIES = [
    "Documents",
    "Invoices & Receipts",
    "Spreadsheets",
    "Slides",
    "Images",
    "Screenshots",
    "Audio",
    "Video",
    "Code",
    "Archives",
    "Installers",
    "Ebooks",
    "Other",
]

EXT_CATEGORY = {
    **dict.fromkeys(["pdf", "doc", "docx", "odt", "rtf", "txt", "md"], "Documents"),
    **dict.fromkeys(["xls", "xlsx", "ods", "csv", "tsv"], "Spreadsheets"),
    **dict.fromkeys(["ppt", "pptx", "odp", "key"], "Slides"),
    **dict.fromkeys(["jpg", "jpeg", "png", "gif", "webp", "heic", "bmp", "svg", "tiff"], "Images"),
    **dict.fromkeys(["mp3", "wav", "flac", "m4a", "ogg", "aac"], "Audio"),
    **dict.fromkeys(["mp4", "mkv", "mov", "avi", "webm"], "Video"),
    **dict.fromkeys(["py", "js", "ts", "html", "css", "json", "ipynb", "sh", "ps1", "java", "c", "cpp", "go", "rs", "yml", "yaml"], "Code"),
    **dict.fromkeys(["zip", "rar", "7z", "tar", "gz", "bz2", "xz"], "Archives"),
    **dict.fromkeys(["exe", "msi", "dmg", "pkg", "deb", "rpm", "appimage", "apk"], "Installers"),
    **dict.fromkeys(["epub", "mobi", "azw3"], "Ebooks"),
}

TEXT_EXTS = {"txt", "md", "csv", "tsv", "json", "py", "js", "ts", "html", "css", "yml", "yaml", "sh", "ps1", "log", "xml", "ini", "toml"}

# Files that are still downloading, or that belong to the OS.
SKIP_EXTS = {"crdownload", "part", "partial", "download", "tmp", "lnk"}
SKIP_NAMES = {"desktop.ini", "thumbs.db", ".ds_store"}

# Conservative guess at how long a human spends opening, renaming and
# filing one download by hand. Used only for the "time saved" summary.
SECONDS_SAVED_PER_FILE = 25

SNIPPET_CHARS = 1200


# ---------------------------------------------------------------- inspection

@dataclass
class FileInfo:
    path: Path
    ext: str
    size: int
    modified: float
    snippet: str

    @property
    def date(self) -> str:
        return datetime.fromtimestamp(self.modified).strftime("%Y-%m-%d")


def read_snippet(path: Path, ext: str) -> str:
    """Return a little readable text from the file, or '' if we can't."""
    try:
        if ext in TEXT_EXTS:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read(SNIPPET_CHARS)
        if ext in {"docx", "pptx", "xlsx"}:
            return _office_text(path, ext)
        if ext == "pdf":
            return _pdf_text(path)
    except (OSError, zipfile.BadZipFile, KeyError):
        pass
    return ""


def _office_text(path: Path, ext: str) -> str:
    member = {
        "docx": "word/document.xml",
        "pptx": "ppt/slides/slide1.xml",
        "xlsx": "xl/sharedStrings.xml",
    }[ext]
    with zipfile.ZipFile(path) as z:
        xml = z.read(member).decode("utf-8", errors="ignore")
    text = re.sub(r"<[^>]+>", " ", xml)
    return re.sub(r"\s+", " ", text).strip()[:SNIPPET_CHARS]


def _pdf_text(path: Path) -> str:
    try:
        from pypdf import PdfReader  # optional dependency
    except ImportError:
        return ""
    reader = PdfReader(str(path))
    text = " ".join((page.extract_text() or "") for page in reader.pages[:2])
    return re.sub(r"\s+", " ", text).strip()[:SNIPPET_CHARS]


def scan(folder: Path, settle_seconds: int) -> list[FileInfo]:
    now = time.time()
    files = []
    for entry in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
        if not entry.is_file() or entry.name.startswith("."):
            continue
        if entry.name.lower() in SKIP_NAMES:
            continue
        ext = entry.suffix.lower().lstrip(".")
        if ext in SKIP_EXTS:
            continue
        st = entry.stat()
        if now - st.st_mtime < settle_seconds:
            continue  # probably still being written
        files.append(FileInfo(entry, ext, st.st_size, st.st_mtime, read_snippet(entry, ext)))
    return files


def fingerprint(info: FileInfo) -> str:
    h = hashlib.sha1()
    h.update(info.path.name.encode())
    h.update(str(info.size).encode())
    h.update(str(int(info.modified)).encode())
    return h.hexdigest()


# ------------------------------------------------------------------- naming

def slugify(text: str, max_len: int = 60) -> str:
    text = text.lower()
    text = re.sub(r"\.[a-z0-9]{1,5}$", "", text)            # stray extension
    text = re.sub(r"\(\d+\)|\bcopy\b|\bfinal\b", " ", text)  # "(1)", "copy"
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    text = re.sub(r"-{2,}", "-", text)
    return text[:max_len].rstrip("-") or "untitled"


def heuristic_guess(info: FileInfo) -> dict:
    """What a sloth with no model would do: sort by extension, clean the name."""
    name = info.path.stem
    category = EXT_CATEGORY.get(info.ext, "Other")
    lowered = name.lower()
    if category == "Images" and re.search(r"screen ?shot|screenshot|capture|snip", lowered):
        category = "Screenshots"
    if re.search(r"invoice|receipt|factur|kwitansi|nota|order[-_ ]?\d", lowered):
        category = "Invoices & Receipts"
    return {"category": category, "name": slugify(name), "reason": "matched by file extension"}


# ---------------------------------------------------------------- the model

SYSTEM_PROMPT = (
    "You tidy a messy Downloads folder. For each file you get its current "
    "name, extension, size and, when available, the first lines of its "
    "content. Pick the single best category from the allowed list and "
    "propose a short, descriptive, lowercase file name of 2 to 6 words "
    "separated by hyphens, WITHOUT extension and WITHOUT a date. Prefer "
    "what the content is about (vendor, topic, project) over the original "
    "gibberish name. Keep it the same language as the content. If you are "
    "unsure, keep the meaning of the original name. Give a reason in under "
    "12 words."
)

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": CATEGORIES},
        "name": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["category", "name", "reason"],
}


class Ollama:
    def __init__(self, host: str, model: str, timeout: float = 120):
        self.host = host.rstrip("/")
        self.model = model
        self.timeout = timeout

    def available(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.host}/api/tags", timeout=3) as r:
                tags = json.load(r)
        except (urllib.error.URLError, OSError, ValueError):
            return False
        names = {m.get("name", "") for m in tags.get("models", [])}
        return self.model in names or f"{self.model}:latest" in names

    def classify(self, info: FileInfo) -> dict:
        user = (
            f"File name: {info.path.name}\n"
            f"Extension: .{info.ext or '(none)'}\n"
            f"Size: {human_size(info.size)}\n"
            f"Content preview: {info.snippet or '(not readable)'}"
        )
        body = {
            "model": self.model,
            "stream": False,
            "format": RESPONSE_SCHEMA,
            "options": {"temperature": 0},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
        }
        req = urllib.request.Request(
            f"{self.host}/api/chat",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            reply = json.load(r)
        data = json.loads(reply["message"]["content"])
        if data.get("category") not in CATEGORIES:
            data["category"] = "Other"
        data["name"] = slugify(str(data.get("name", "")))
        data["reason"] = str(data.get("reason", ""))[:120]
        return data


# ----------------------------------------------------------------- planning

@dataclass
class Move:
    src: str
    dst: str
    category: str
    reason: str
    source: str  # "model", "cache" or "heuristic"


def unique_destination(target: Path, taken: set[str]) -> Path:
    candidate, n = target, 2
    while candidate.exists() or str(candidate).lower() in taken:
        candidate = target.with_name(f"{target.stem}-{n}{target.suffix}")
        n += 1
    taken.add(str(candidate).lower())
    return candidate


def plan(folder: Path, files: list[FileInfo], model: Ollama | None,
         dated: bool, cache: dict, log=print) -> list[Move]:
    moves, taken = [], set()
    for i, info in enumerate(files, 1):
        key = fingerprint(info)
        source = "cache"
        guess = cache.get(key)
        if guess is None:
            source = "heuristic"
            guess = heuristic_guess(info)
            if model is not None:
                try:
                    guess = model.classify(info)
                    source = "model"
                except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
                    log(f"  ! model failed on {info.path.name} ({e}); using heuristic")
            cache[key] = guess
        log(f"  [{i}/{len(files)}] {info.path.name}")
        stem = f"{info.date} {guess['name']}" if dated else guess["name"]
        suffix = f".{info.ext}" if info.ext else ""
        target = unique_destination(folder / guess["category"] / f"{stem}{suffix}", taken)
        if target.resolve() == info.path.resolve():
            continue
        moves.append(Move(str(info.path), str(target), guess["category"], guess.get("reason", ""), source))
    return moves


# -------------------------------------------------------- apply and undo

def state_dir(folder: Path) -> Path:
    d = folder / STATE_DIR
    d.mkdir(exist_ok=True)
    return d


def load_cache(folder: Path) -> dict:
    try:
        return json.loads((folder / STATE_DIR / CACHE_FILE).read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def save_cache(folder: Path, cache: dict) -> None:
    (state_dir(folder) / CACHE_FILE).write_text(json.dumps(cache, indent=1), "utf-8")


def apply_moves(folder: Path, moves: list[Move]) -> str:
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    history = state_dir(folder) / HISTORY_FILE
    with open(history, "a", encoding="utf-8") as log:
        for m in moves:
            dst = Path(m.dst)
            if dst.exists():
                print(f"  skip (exists): {dst.name}")
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(m.src, m.dst)
            # Write each move as it happens so a crash mid-run is still undoable.
            log.write(json.dumps({"run": run_id, **asdict(m)}) + "\n")
            log.flush()
    return run_id


def undo_last(folder: Path) -> int:
    history = folder / STATE_DIR / HISTORY_FILE
    if not history.exists():
        print("Nothing to undo.")
        return 0
    entries = [json.loads(l) for l in history.read_text("utf-8").splitlines() if l.strip()]
    if not entries:
        print("Nothing to undo.")
        return 0
    last = entries[-1]["run"]
    undone = 0
    for e in reversed([e for e in entries if e["run"] == last]):
        src, dst = Path(e["src"]), Path(e["dst"])
        if dst.exists() and not src.exists():
            shutil.move(str(dst), str(src))
            undone += 1
            try:
                dst.parent.rmdir()  # only succeeds if the folder is now empty
            except OSError:
                pass
        else:
            print(f"  can't restore {src.name}: it moved again or the original name is taken")
    remaining = [e for e in entries if e["run"] != last]
    history.write_text("".join(json.dumps(e) + "\n" for e in remaining), "utf-8")
    print(f"Undid run {last}: {undone} file(s) back where they were.")
    return undone


# ---------------------------------------------------------------- output

def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def show_plan(folder: Path, moves: list[Move]) -> None:
    if not moves:
        print("\nAlready tidy. The sloth goes back to sleep. \U0001f9a5")
        return
    print()
    by_cat: dict[str, list[Move]] = {}
    for m in moves:
        by_cat.setdefault(m.category, []).append(m)
    for cat in sorted(by_cat):
        print(f"{cat}/")
        for m in by_cat[cat]:
            print(f"   {Path(m.src).name}")
            print(f"     -> {Path(m.dst).name}   ({m.reason})")
    minutes = len(moves) * SECONDS_SAVED_PER_FILE / 60
    print(f"\n{len(moves)} file(s), {len(by_cat)} folder(s). "
          f"Roughly {minutes:.0f} minute(s) of clicking you won't have to do.")


def default_downloads() -> Path:
    return Path.home() / "Downloads"


# ------------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="slothsort",
        description="Tidy a messy folder with a local open-weight model. Dry-run by default.",
    )
    p.add_argument("folder", nargs="?", type=Path, default=default_downloads(),
                   help="folder to tidy (default: ~/Downloads)")
    p.add_argument("--apply", action="store_true", help="actually move the files")
    p.add_argument("--undo", action="store_true", help="reverse the last applied run")
    p.add_argument("--model", default=os.environ.get("SLOTHSORT_MODEL", "gemma3:4b"),
                   help="Ollama model to use (default: gemma3:4b)")
    p.add_argument("--host", default=os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
                   help="Ollama server URL")
    p.add_argument("--no-ai", action="store_true", help="skip the model, sort by extension only")
    p.add_argument("--no-date", action="store_true", help="don't prefix names with the file date")
    p.add_argument("--settle", type=int, default=120,
                   help="ignore files modified in the last N seconds (default: 120)")
    p.add_argument("--limit", type=int, default=0, help="only look at the first N files")
    p.add_argument("--json", action="store_true", help="print the plan as JSON")
    p.add_argument("--version", action="version", version=f"slothsort {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    # Old Windows consoles (cp1252) can't print the sloth; degrade instead of crashing.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    args = build_parser().parse_args(argv)
    folder = args.folder.expanduser().resolve()
    if not folder.is_dir():
        print(f"Not a folder: {folder}", file=sys.stderr)
        return 2

    if args.undo:
        undo_last(folder)
        return 0

    model = None
    if not args.no_ai:
        candidate = Ollama(args.host, args.model)
        if candidate.available():
            model = candidate
        else:
            print(f"Couldn't reach model '{args.model}' at {args.host}. "
                  f"Run `ollama pull {args.model}` (or pass --no-ai). Falling back to extensions.\n")

    files = scan(folder, args.settle)
    if args.limit:
        files = files[: args.limit]
    log = (lambda *_: None) if args.json else print
    log(f"\U0001f9a5 slothsort is looking at {len(files)} file(s) in {folder} "
        f"using {'model ' + args.model if model else 'extensions only'}...")

    cache = load_cache(folder)
    moves = plan(folder, files, model, not args.no_date, cache, log=log)
    save_cache(folder, cache)

    if args.json:
        print(json.dumps([asdict(m) for m in moves], indent=2))
    else:
        show_plan(folder, moves)

    if args.apply and moves:
        run_id = apply_moves(folder, moves)
        log(f"\nDone. Run {run_id} applied. Changed your mind? `slothsort --undo \"{folder}\"`")
    elif moves and not args.json:
        print("\nThis was a dry run. Add --apply to move the files.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
