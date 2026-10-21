"""Tests for the live viewer. Run: python -m unittest discover -s tests"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from claude_context_profiler import serve  # noqa: E402
from test_profile import request  # noqa: E402


def write_call(run_dir: Path, index: int) -> None:
    record = request([{"role": "user", "content": "hi"}], usage={"input_tokens": 10, "output_tokens": 2}, index=index)
    (run_dir / f"{index:04d}.json").write_text(json.dumps(record), encoding="utf-8")


class Counting(unittest.TestCase):
    def test_counts_complete_files_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_call(run_dir, 1)
            write_call(run_dir, 2)
            (run_dir / "0003.json.part").write_text("{", encoding="utf-8")
            (run_dir / "meta.json").write_text("{}", encoding="utf-8")
            self.assertEqual(serve.count_records(run_dir), 2)


class LivePages(unittest.TestCase):
    def test_empty_run_waits_and_polls(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = serve.Pages(Path(tmp)).live(0)
            self.assertIn("Waiting for the first API call", page)
            self.assertIn("fetch('/status'", page)
            self.assertIn("var count = 0", page)

    def test_rendered_page_carries_the_count_and_the_saved_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_call(run_dir, 1)
            profiler = serve.Pages(run_dir).live(1)
            self.assertIn("What the prompt is made of", profiler)
            self.assertIn("var count = 1", profiler)
            self.assertIn(json.dumps(str(run_dir.resolve() / "context_profiler.html")), profiler)

    def test_one_render_per_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_call(run_dir, 1)
            pages = serve.Pages(run_dir)
            first = pages.page(1)
            self.assertIs(pages.page(1), first)
            write_call(run_dir, 2)
            second = pages.page(2)
            self.assertIsNot(second, first)
            self.assertNotIn(1, pages.cache)

    def test_final_page_is_static(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            self.assertEqual(serve.Pages(run_dir).write_final(), 0)
            self.assertFalse((run_dir / "context_profiler.html").exists())
            write_call(run_dir, 1)
            self.assertEqual(serve.Pages(run_dir).write_final(), 1)
            page = (run_dir / "context_profiler.html").read_text(encoding="utf-8")
            self.assertNotIn("fetch('/status'", page)
            self.assertIn("What the prompt is made of", page)


if __name__ == "__main__":
    unittest.main()
