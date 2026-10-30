from __future__ import annotations

import unittest
from collections import Counter

from helpers import TEMPLATE

from core.template_loader import load_template


@unittest.skipUnless(TEMPLATE.exists(), "归档范围模版未随仓库分发（内部资料）")
class DocxParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cs = load_template(TEMPLATE)
        cls.by_code = {}
        for cp in cls.cs.checkpoints:
            cls.by_code.setdefault(cp.code, []).append(cp)

    def test_counts(self):
        self.assertEqual(len(self.cs.checkpoints), 2907)
        self.assertEqual(len(self.cs.nodes), 410)
        self.assertEqual(self.cs.meta["doc_types"], ["8", "9"])

    def test_doc_type_split(self):
        counts = Counter(cp.code[0] for cp in self.cs.checkpoints)
        self.assertEqual(counts["8"], 2212)
        self.assertEqual(counts["9"], 695)

    def test_typo_hierarchy_warnings(self):
        warnings = "\n".join(self.cs.warnings)
        self.assertIn("8464", warnings)
        self.assertIn("8485", warnings)
        for cp in self.by_code["8464"]:
            self.assertEqual(cp.path[-2], "监理")
        for cp in self.by_code["8485"]:
            self.assertEqual(cp.path[-2], "竣工图")

    def test_continuation_rows_keep_archive_content(self):
        cp = next(
            item for item in self.by_code["8430"]
            if item.text == "水泥使用跟踪记录表"
        )
        self.assertEqual(cp.category, "综合")
        self.assertEqual(cp.archive_content, "04水泥")
        self.assertEqual(cp.path[-1], "综合")
        self.assertEqual(cp.filing, "施工")
        self.assertEqual(cp.retention["施工"], "30年")

    def test_optional_marked(self):
        optional = [cp for cp in self.cs.checkpoints if cp.optional]
        self.assertGreater(len(optional), 100)
        self.assertTrue(all(cp.skip_reason for cp in optional))

    def test_unit_filter_counts_match_reference(self):
        # 82 交流输电线路，按立卷列
        selected = {
            unit: [
                cp for cp in self.cs.checkpoints
                if cp.code.startswith("82") and unit in cp.filing_units
            ]
            for unit in ("建设", "设计", "监理", "施工")
        }
        self.assertEqual(len(selected["建设"]), 167)
        self.assertEqual(len(selected["设计"]), 38)
        self.assertEqual(len(selected["监理"]), 113)
        self.assertEqual(len(selected["施工"]), 250)


if __name__ == "__main__":
    unittest.main()
