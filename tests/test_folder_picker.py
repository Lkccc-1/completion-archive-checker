from __future__ import annotations

import unittest

from helpers import BASE  # noqa: F401  让 core 可导入

from core.folder_picker import parse_output


class ParseOutputTests(unittest.TestCase):
    """对话框跑在独立进程里，输出格式就两种加一个空，解析要稳。"""

    def test_ok_path(self):
        path, error = parse_output("OK:D:\\资料\\项目A".encode("utf-8"))
        self.assertEqual(path, "D:\\资料\\项目A")
        self.assertEqual(error, "")

    def test_cancel_is_empty(self):
        self.assertEqual(parse_output(b"OK:"), ("", ""))

    def test_error_line_is_returned(self):
        path, error = parse_output("ERR:No module named 'tkinter'".encode("utf-8"))
        self.assertEqual(path, "")
        self.assertIn("tkinter", error)

    def test_garbage_is_treated_as_error(self):
        path, error = parse_output(b"\x00\x01broken")
        self.assertEqual(path, "")
        self.assertTrue(error)


if __name__ == "__main__":
    unittest.main()
