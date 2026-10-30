from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from helpers import BASE  # noqa: F401

from core.scanner import scan_directory


class ScannerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "~$锁文件.docx").write_bytes(b"x")
        (self.root / ".hidden.pdf").write_bytes(b"x")
        (self.root / "说明.txt").write_text("x", encoding="utf-8")
        sub = self.root / "8232 基础"
        sub.mkdir()
        (sub / "隐蔽工程签证.pdf").write_bytes(b"x")
        nested = sub / "2023-8230-开工报告.docx"
        nested.write_bytes(b"x")

    def tearDown(self):
        self.tmp.cleanup()

    def test_scan_skips_lock_hidden_and_other_extensions(self):
        files = scan_directory(self.root, extensions={".pdf", ".docx"})
        paths = {item.rel_path for item in files}
        self.assertIn("8232 基础/隐蔽工程签证.pdf", paths)
        self.assertIn("8232 基础/2023-8230-开工报告.docx", paths)
        self.assertNotIn("~$锁文件.docx", paths)
        self.assertNotIn(".hidden.pdf", paths)
        self.assertNotIn("说明.txt", paths)
        self.assertEqual(len(files), 2)

    def test_codes_from_path(self):
        files = scan_directory(self.root, extensions={".pdf", ".docx"})
        first = next(item for item in files if item.name == "隐蔽工程签证.pdf")
        self.assertIn("8232", first.codes)
        second = next(item for item in files if item.name.startswith("2023"))
        self.assertIn("8230", second.codes)
        self.assertNotIn("2023", second.codes)

    def test_missing_dir_raises(self):
        with self.assertRaises(FileNotFoundError):
            scan_directory(self.root / "不存在")


if __name__ == "__main__":
    unittest.main()
