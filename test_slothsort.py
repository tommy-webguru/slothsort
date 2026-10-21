"""Tests for slothsort. Uses a fake Ollama server, so no model is needed.

Run: python -m unittest -v test_slothsort
"""

import io
import json
import os
import tempfile
import threading
import time
import unittest
import zipfile
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import slothsort


class FakeOllama(BaseHTTPRequestHandler):
    calls = 0

    def log_message(self, *args):
        pass

    def _send(self, payload):
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send({"models": [{"name": "gemma3:4b"}]})

    def do_POST(self):
        FakeOllama.calls += 1
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        prompt = body["messages"][-1]["content"]
        assert body["format"]["properties"]["category"]["enum"] == slothsort.CATEGORIES
        if "Tokopedia" in prompt:
            answer = {"category": "Invoices & Receipts", "name": "Tokopedia Order Headphones", "reason": "order receipt"}
        elif "Quarterly" in prompt:
            answer = {"category": "Documents", "name": "q3 quarterly report draft", "reason": "report text"}
        else:
            answer = {"category": "Made Up", "name": "!!!", "reason": "unsure"}
        self._send({"message": {"role": "assistant", "content": json.dumps(answer)}})


def make_docx(path: Path, text: str) -> None:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", f"<w:document><w:body><w:p><w:t>{text}</w:t></w:p></w:body></w:document>")


class SlothsortTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeOllama)
        cls.host = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        old = time.time() - 3600
        files = {
            "invoice_8812(1).txt": "Tokopedia invoice INV/2026/10/88 Wireless headphones Rp 450.000",
            "Doc1.docx": None,
            "Screenshot 2026-10-01 101010.png": "",
            "setup_v2.exe": "",
            "mystery.bin": "",
            "still-downloading.crdownload": "",
            "desktop.ini": "",
        }
        for name, content in files.items():
            p = self.dir / name
            if name.endswith(".docx"):
                make_docx(p, "Quarterly report Q3 draft for the board")
            else:
                p.write_text(content or "x", encoding="utf-8")
            os.utime(p, (old, old))
        fresh = self.dir / "just-arrived.pdf"
        fresh.write_text("x")
        FakeOllama.calls = 0

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = slothsort.main([str(self.dir), "--host", self.host, *args])
        return code, out.getvalue()

    def listing(self):
        return sorted(str(p.relative_to(self.dir)).replace("\\", "/")
                      for p in self.dir.rglob("*") if p.is_file() and slothsort.STATE_DIR not in p.parts)

    def test_dry_run_moves_nothing(self):
        before = self.listing()
        code, out = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("dry run", out)
        self.assertEqual(before, self.listing())

    def test_apply_uses_model_and_skips_unsafe_files(self):
        self.run_cli("--apply", "--no-date")
        files = self.listing()
        self.assertIn("Invoices & Receipts/tokopedia-order-headphones.txt", files)
        self.assertIn("Documents/q3-quarterly-report-draft.docx", files)
        # model answered with an invalid category and empty-ish name -> sanitised
        self.assertIn("Other/untitled.bin", files)
        # skipped: partial download, OS file, and a file that just arrived
        for kept in ("still-downloading.crdownload", "desktop.ini", "just-arrived.pdf"):
            self.assertIn(kept, files)

    def test_date_prefix(self):
        self.run_cli("--apply")
        self.assertTrue(any(f.startswith("Documents/") and f[10:20].count("-") == 2 for f in self.listing()))

    def test_undo_restores_everything(self):
        before = self.listing()
        self.run_cli("--apply")
        self.assertNotEqual(before, self.listing())
        self.run_cli("--undo")
        self.assertEqual(before, self.listing())
        self.assertFalse((self.dir / "Documents").exists(), "empty category folders are cleaned up")

    def test_cache_avoids_second_model_call(self):
        self.run_cli()
        first = FakeOllama.calls
        self.run_cli()
        self.assertEqual(first, FakeOllama.calls)

    def test_no_ai_heuristics(self):
        self.run_cli("--apply", "--no-ai", "--no-date")
        files = self.listing()
        self.assertIn("Screenshots/screenshot-2026-10-01-101010.png", files)
        self.assertIn("Installers/setup-v2.exe", files)
        self.assertIn("Invoices & Receipts/invoice-8812.txt", files)
        self.assertEqual(FakeOllama.calls, 0)

    def test_collisions_get_suffix(self):
        (self.dir / "Installers").mkdir()
        (self.dir / "Installers" / "setup-v2.exe").write_text("already here")
        self.run_cli("--apply", "--no-ai", "--no-date")
        self.assertIn("Installers/setup-v2-2.exe", self.listing())

    def test_missing_model_falls_back(self):
        code, out = self.run_cli("--model", "not-pulled")
        self.assertEqual(code, 0)
        self.assertIn("Falling back", out)

    def test_slugify(self):
        self.assertEqual(slothsort.slugify("Final  Report (2) copy.PDF"), "report")
        self.assertEqual(slothsort.slugify("***"), "untitled")


if __name__ == "__main__":
    unittest.main()
