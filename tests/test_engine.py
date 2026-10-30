from __future__ import annotations

import unittest

from helpers import make_checkpoint, make_file, make_set

from core.engine import (
    add_inventory_surplus,
    build_category_gaps,
    build_stats,
    check,
    detect_project_types,
    filter_checkpoints,
    reapply_overrides,
)
from core.models import (
    STATE_MATCHED,
    STATE_MISSING,
    STATE_NA,
    CheckReport,
    Evidence,
    MatchResult,
    Node,
)


def result(cp, state, *, auto_state="", reason=""):
    return MatchResult(
        checkpoint=cp,
        state=state,
        score=0.0,
        reason=reason,
        auto_state=auto_state or state,
        auto_reason=reason,
    )


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.cps = [
            make_checkpoint("a", filing="建设 / 施工", seq=1),
            make_checkpoint("b", filing="施工", seq=2),
            make_checkpoint("c", filing="设计", seq=3),
            make_checkpoint("d", filing="相关", seq=4),
            make_checkpoint(
                "e", filing="建设", seq=5,
            ),
        ]
        self.cps[4].retention = {"建设": "永久", "施工": "30年"}

    def test_unit_filter(self):
        selected = filter_checkpoints(self.cps, "8", "82", "施工")
        self.assertEqual([cp.text for cp in selected], ["a", "b", "d"])

    def test_retention_fallback_is_opt_in(self):
        without = filter_checkpoints(self.cps, "8", "82", "施工", include_retention=False)
        self.assertNotIn("e", [cp.text for cp in without])
        with_retention = filter_checkpoints(self.cps, "8", "82", "施工", include_retention=True)
        self.assertIn("e", [cp.text for cp in with_retention])


class DetectAndCheckTests(unittest.TestCase):
    def test_detect_project_types(self):
        cs = make_set([])
        cs.nodes.append(Node("t83", "83", "变电站", 2, "root", 2))
        files = [
            make_file("8232/a.pdf"),
            make_file("8230/b.pdf"),
            make_file("8310/c.pdf"),
        ]
        detected = detect_project_types(files, cs, "8")
        self.assertEqual(detected[0], ("82", 2))
        self.assertIn(("83", 1), detected)

    def test_check_synthetic(self):
        cp1 = make_checkpoint("混凝土试块报告及报审", archive="05施工用混凝土", seq=1)
        cp2 = make_checkpoint("钢筋出厂合格证", archive="09钢筋", seq=2)
        cs = make_set([cp1, cp2])
        files = [
            make_file("AY-8232-4 测试工程混凝土试块报告及报审表.pdf"),
        ]
        report = check(cs, files, "8", "82", "施工", project_name="测试工程")
        self.assertEqual(report.stats["total"], 2)
        self.assertEqual(report.stats["matched"], 1)
        self.assertEqual(report.stats["missing"], 1)
        self.assertAlmostEqual(report.stats["completeness"], 0.5)
        self.assertEqual(report.meta["filtered_total"], 2)
        self.assertEqual(report.meta["project_type_name"], "82 交流输电线路")


class GapTests(unittest.TestCase):
    def test_three_digit_gap(self):
        cps = [
            make_checkpoint("a", code="8231", seq=1),
            make_checkpoint("b", code="8232", seq=2),
            make_checkpoint("c", code="8233", seq=3),
        ]
        results = [result(cp, STATE_MISSING) for cp in cps]
        gaps = build_category_gaps(results)
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0].code, "823")
        self.assertEqual(gaps[0].count, 3)

    def test_partial_missing_is_not_gap(self):
        cps = [
            make_checkpoint("a", code="8231", seq=1),
            make_checkpoint("b", code="8232", seq=2),
        ]
        results = [result(cps[0], STATE_MISSING), result(cps[1], STATE_MATCHED)]
        self.assertEqual(build_category_gaps(results), [])


class OverrideTests(unittest.TestCase):
    def test_removing_override_restores_auto_state(self):
        cp = make_checkpoint("钢筋出厂合格证", seq=1)
        item = result(cp, STATE_MISSING, reason="未发现匹配文件")
        report = CheckReport(
            meta={"template_hash": "x"},
            results=[item],
            category_gaps=[],
            orphan_files=[],
            stats=build_stats([item]),
        )
        corrected = reapply_overrides(
            report, {cp.id: {"state": "已提供", "note": "现场已提供"}}
        )
        self.assertEqual(corrected.results[0].state, STATE_MATCHED)
        self.assertTrue(corrected.results[0].manual)
        self.assertEqual(corrected.stats["matched"], 1)

        reverted = reapply_overrides(corrected, {})
        self.assertEqual(reverted.results[0].state, STATE_MISSING)
        self.assertFalse(reverted.results[0].manual)
        self.assertEqual(reverted.results[0].reason, "未发现匹配文件")
        self.assertEqual(reverted.stats["missing"], 1)

    def test_na_override(self):
        cp = make_checkpoint("其他文件", optional=True)
        item = result(cp, STATE_NA, reason="其他/备查类条目")
        report = CheckReport(
            meta={}, results=[item], category_gaps=[], orphan_files=[],
            stats=build_stats([item]),
        )
        corrected = reapply_overrides(report, {cp.id: {"state": "不适用"}})
        self.assertEqual(corrected.results[0].state, STATE_NA)
        self.assertTrue(corrected.results[0].manual)


class InventorySurplusTests(unittest.TestCase):
    """文件清单标准：目录里有、清单里没有的文件要能被点出来。"""

    def setUp(self):
        self.cp = make_checkpoint("开工报告", code="8230")
        self.used = make_file("AX-8230-1 成立文件/1.开工报告.pdf", codes=["8230"])
        self.result = result(self.cp, STATE_MATCHED, reason="")
        self.result.evidence = [Evidence(rel_path=self.used.rel_path, score=0.9)]

    def _surplus(self, files):
        return add_inventory_surplus([], files, [self.result], "8", "82")

    def test_evidence_file_is_not_surplus(self):
        self.assertEqual(self._surplus([self.used]), [])

    def test_unused_weak_file_is_surplus(self):
        extra = make_file("AX-8230-1 成立文件/99.现场签证单.pdf", codes=["8230"])
        extra.best_score = 0.52
        rows = self._surplus([self.used, extra])
        self.assertEqual([row["rel_path"] for row in rows], [extra.rel_path])
        self.assertIn("清单里没有", rows[0]["note"])

    def test_high_score_unused_file_is_not_flagged(self):
        # 证据每条只留前三份，同名的第四份会被挤出去，但它对得上清单
        extra = make_file("AX-8230-1 成立文件/2.开工报告（副本）.pdf", codes=["8230"])
        extra.best_score = 0.91
        self.assertEqual(self._surplus([self.used, extra]), [])

    def test_out_of_scope_code_is_not_flagged(self):
        extra = make_file("AX-9201-1 塔材/1.合格证.pdf", codes=["9201"])
        self.assertEqual(self._surplus([self.used, extra]), [])


if __name__ == "__main__":
    unittest.main()
