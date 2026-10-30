from __future__ import annotations

import unittest

from helpers import BASE  # noqa: F401  (设置 sys.path)

from core.text import (
    clean_for_match,
    extract_codes,
    extract_terms,
    safe_excel_text,
    split_units,
    strip_serial,
)


class StripSerialTests(unittest.TestCase):
    def test_strips_short_serial_only(self):
        self.assertEqual(strip_serial("4光传输"), "光传输")
        self.assertEqual(strip_serial("1.开工报告"), "开工报告")
        self.assertEqual(strip_serial("（3）施工日志"), "（3）施工日志")

    def test_keeps_voltage_and_year(self):
        self.assertEqual(strip_serial("110千伏线路"), "110千伏线路")
        self.assertEqual(strip_serial("2023年资料"), "2023年资料")


class CodeTests(unittest.TestCase):
    def test_extracts_classification_codes(self):
        codes = extract_codes("AY-8232-1 220千伏线路")
        self.assertIn("8232", codes)
        self.assertNotIn("AY", codes)
        self.assertIn("220", codes)

    def test_ignores_year(self):
        self.assertNotIn("2023", extract_codes("2023-8230-开工报告"))

    def test_deduplicates(self):
        self.assertEqual(extract_codes("8232 8232"), ["8232"])


class UnitTests(unittest.TestCase):
    def test_split_units(self):
        self.assertEqual(split_units("设计//施工"), ["设计", "施工"])
        self.assertEqual(split_units("建设/物资供应"), ["建设", "物资供应"])
        self.assertEqual(split_units("施工等"), ["施工"])
        self.assertEqual(split_units(""), [])


class TermTests(unittest.TestCase):
    def test_connectors_are_split(self):
        terms = extract_terms("开工报审及开工报告")
        self.assertIn("开工报审", terms)
        self.assertIn("开工报告", terms)
        self.assertIn("开工", terms)

    def test_subject_removed_from_doc_type(self):
        terms = extract_terms("施工组织设计报审及附件")
        self.assertIn("施工组织设计", terms)

    def test_clean_for_match_removes_punctuation(self):
        self.assertEqual(clean_for_match("施工组织设计（报审）"), "施工组织设计报审")


class ExcelSafetyTests(unittest.TestCase):
    def test_formula_prefix(self):
        self.assertEqual(safe_excel_text("=1+1"), "'=1+1")
        self.assertEqual(safe_excel_text("正常文本"), "正常文本")
        self.assertEqual(safe_excel_text(3), 3)


if __name__ == "__main__":
    unittest.main()
