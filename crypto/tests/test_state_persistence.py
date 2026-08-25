"""
Tests for storage/state.py – atomic JSON and JSONL writes.
"""

import sys
import os
import json
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from storage.state import write_json_atomic, append_jsonl, read_json_safe


class TestWriteJsonAtomic(unittest.TestCase):
    def test_writes_and_reads_back(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "state.json")
            data = {"exchange": "MEXC", "mode": "signal", "cycle_count": 1}
            write_json_atomic(path, data)
            with open(path, "r") as f:
                result = json.load(f)
            self.assertEqual(result["exchange"], "MEXC")
            self.assertEqual(result["cycle_count"], 1)

    def test_creates_parent_dirs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "sub", "dir", "state.json")
            write_json_atomic(path, {"ok": True})
            self.assertTrue(os.path.exists(path))

    def test_overwrites_existing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "state.json")
            write_json_atomic(path, {"v": 1})
            write_json_atomic(path, {"v": 2})
            with open(path, "r") as f:
                result = json.load(f)
            self.assertEqual(result["v"], 2)

    def test_null_values_serialized(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "state.json")
            data = {"last_sync": None, "candidates": []}
            write_json_atomic(path, data)
            with open(path, "r") as f:
                result = json.load(f)
            self.assertIsNone(result["last_sync"])


class TestAppendJsonl(unittest.TestCase):
    def test_appends_multiple_records(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "audit.jsonl")
            append_jsonl(path, {"event": "start", "status": "ok"})
            append_jsonl(path, {"event": "end", "status": "ok"})
            with open(path, "r") as f:
                lines = f.readlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[0])["event"], "start")
            self.assertEqual(json.loads(lines[1])["event"], "end")

    def test_creates_parent_dirs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "logs", "audit.jsonl")
            append_jsonl(path, {"event": "test"})
            self.assertTrue(os.path.exists(path))


class TestReadJsonSafe(unittest.TestCase):
    def test_returns_empty_on_missing_file(self):
        result = read_json_safe("/nonexistent/path/state.json")
        self.assertEqual(result, {})

    def test_returns_empty_on_invalid_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "bad.json")
            with open(path, "w") as f:
                f.write("not valid json {{{")
            result = read_json_safe(path)
            self.assertEqual(result, {})

    def test_reads_valid_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "ok.json")
            with open(path, "w") as f:
                json.dump({"key": "value"}, f)
            result = read_json_safe(path)
            self.assertEqual(result["key"], "value")


if __name__ == "__main__":
    unittest.main()
