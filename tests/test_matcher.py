from __future__ import annotations

import unittest

from helpers import make_checkpoint, make_file

from core.matcher import Matcher
from core.models import STATE_MATCHED, STATE_MISSING, STATE_NA, STATE_SUSPECT


def run_match(checkpoints, files, project_name="测试工程"):
    matcher = Matcher(checkpoints, project_name=project_name)
    return matcher, matcher.match(files)


class MatcherTests(unittest.TestCase):
    def test_exact_name_matches(self):
        cp = make_checkpoint("混凝土试块报告及报审", archive="05施工用混凝土")
        file = make_file("AY-8232-4 测试工程混凝土试块报告及报审表.pdf")
        _, results = run_match([cp], [file])
        self.assertEqual(results[0].state, STATE_MATCHED)
        self.assertGreaterEqual(results[0].score, Matcher.MATCH_THRESHOLD)
        self.assertTrue(results[0].evidence)

    def test_code_only_does_not_match(self):
        cp = make_checkpoint("钢筋出厂合格证", archive="09钢筋")
        file = make_file("AY-8232-9 测试工程扫描件.pdf", codes=["8232"])
        _, results = run_match([cp], [file])
        self.assertNotEqual(results[0].state, STATE_MATCHED)

    def test_optional_missing_is_na(self):
        cp = make_checkpoint("其他文件", optional=True)
        _, results = run_match([cp], [])
        self.assertEqual(results[0].state, STATE_NA)

    def test_archive_context_disambiguates_duplicate_text(self):
        cp_cement = make_checkpoint("见证取样记录", archive="04水泥", seq=1)
        cp_sand = make_checkpoint("见证取样记录", archive="05施工用砂", seq=2)
        f_cement = make_file("AY-8232-2 测试工程水泥见证取样记录.pdf")
        f_sand = make_file("AY-8232-3 测试工程建筑用砂见证取样记录.pdf")
        _, results = run_match([cp_cement, cp_sand], [f_cement, f_sand])
        self.assertEqual(results[0].state, STATE_MATCHED)
        self.assertEqual(results[1].state, STATE_MATCHED)
        self.assertIn("水泥", results[0].evidence[0].rel_path)
        self.assertIn("砂", results[1].evidence[0].rel_path)

        # 消歧词不命中时不能被“要素齐全”补分规则硬拉到已匹配。
        # 必须同时传入两条重复检查点，消歧逻辑才会启用。
        matcher = Matcher([cp_cement, cp_sand], project_name="测试工程")
        matcher.set_file_context([f_sand])
        scored = {item.index: item for item in matcher.score_file(f_sand)}
        self.assertLess(scored[0].score, Matcher.MATCH_THRESHOLD)

    def test_reordered_connector_phrase_matches(self):
        cp = make_checkpoint(
            "开工报审及开工报告", code="8230", archive="01工程开工及报审"
        )
        file = make_file(
            "AX-8230-1 成立文件、开工报告、验评划分表、技术清单、施工单位体系认证、"
            "法定授权书、终身责任承诺书、施工进度计划、安全技术交底/"
            "6.项目A（220kV输电线路工程）开工报告及报审.pdf"
        )
        _, results = run_match([cp], [file])
        self.assertEqual(results[0].state, STATE_MATCHED)
        self.assertGreaterEqual(results[0].score, Matcher.MATCH_THRESHOLD)

    def test_boost_requires_every_connector_part(self):
        # 只交了三份材料中的一份，不能靠其中一个分句补成已匹配
        cp = make_checkpoint(
            "应急预案汇编、安全风险分析（含项目基准风险分析表）、"
            "危险源辨识策划报审及控制文件",
            code="8230",
            archive="安全管理",
        )
        file = make_file(
            "2.项目A（220kV输电线路工程）危险源辨识策划及报审表.pdf"
        )
        _, results = run_match([cp], [file])
        self.assertNotEqual(results[0].state, STATE_MATCHED)

    def test_reordered_establishment_document_matches(self):
        cp = make_checkpoint(
            "项目部成立、公章启用文件", code="8230", archive="01工程开工及报审"
        )
        file = make_file(
            "AX-8230-1 成立文件、开工报告、验评划分表/"
            "3.项目A（220kV输电线路工程）关于成立某市220千伏变电站扩建"
            "第三台主变及其配套线路工程项目部及启用项目部印章的函.pdf"
        )
        _, results = run_match([cp], [file])
        self.assertEqual(results[0].state, STATE_MATCHED)

    def test_anchor_matches_base_substation_start_report(self):
        cp = make_checkpoint(
            "基础分部工程开工及报审表", code="8232", archive="01基础工程"
        )
        file = make_file(
            "AX-8232-1 基础开工报告、施工方案、挖孔桩、掏挖、灌注桩、"
            "承台连梁质量验收记录表/"
            "1. 220千伏变电站扩建第三台主变及其配套线路工程分部工程开工报审表及开工报告.pdf"
        )
        _, results = run_match([cp], [file])
        self.assertEqual(results[0].state, STATE_MATCHED)

    def test_anchor_does_not_match_water_report_to_cement(self):
        cp = make_checkpoint(
            "水泥进场报审表", code="8232", archive="05施工用水泥"
        )
        file = make_file(
            "AX-8232-10 水泥、砂、碎石/"
            "10、220千伏变电站线路工程施工土建部分1A0-2A2塔基用水报告报审与取样记录、检测报告.pdf"
        )
        _, results = run_match([cp], [file])
        self.assertNotEqual(results[0].state, STATE_MATCHED)

    def test_folder_context_cannot_complete_subject(self):
        # 文件夹名罗列了“项目部成立”，但文件只有管理人员任命和印章启用。
        # 没有文书类型可核对的分句必须完整出现在文件名里，卷内目录的
        # “成立”不能借给文件名；这条最多是疑似，不能判已匹配。
        cp = make_checkpoint(
            "项目部成立、公章启用文件", code="8230", archive="01工程开工及报审"
        )
        file = make_file(
            "AY-8230-1 220千伏项目C甲乙线解口入某站线路工程项目部成立、"
            "委员会成立、管理人员任命和启用章的函、开工报告/"
            "3.220千伏项目C甲乙线解口入某站线路工程关于项目管理人员任合书的"
            "通知和启用项目部印章的函.pdf"
        )
        _, results = run_match([cp], [file])
        self.assertNotEqual(results[0].state, STATE_MATCHED)

    def test_folder_scoped_semantic_match(self):
        # 文件名和清单不必逐字相同：先按分类号锁定文件夹，再在文件夹内做语义匹配。
        cp = make_checkpoint(
            "架线分部工程开工报审表及附件", code="8234", archive="01架线工程"
        )
        file = make_file(
            "AX-8234-1 架线分部开工报告、专项施工方案/"
            "1.项目A（220kV输电线路工程）架线分部开工报告及报审表.pdf"
        )
        _, results = run_match([cp], [file])
        self.assertEqual(results[0].state, STATE_MATCHED)
        self.assertIn("按分类号在文件夹", results[0].reason)

    def test_folder_partial_coverage_stays_suspect(self):
        # 文件夹里只找到相近但文书类型不同的文件，只能判疑似，不能直接判缺失。
        cp = make_checkpoint(
            "基础施工方案、作业指导书报审及附件、执行文件",
            code="8232",
            archive="02基础工程",
        )
        file = make_file(
            "AX-8232-4 2A3-3A33基础混凝土施工记录/"
            "2.项目A（220kV输电线路工程）（商品）混凝土工程浇筑施工记录"
            "[2A3-3A33基础].pdf"
        )
        _, results = run_match([cp], [file])
        self.assertEqual(results[0].state, STATE_SUSPECT)

    def test_folder_label_covers_listed_annex(self):
        # 清单写“立塔施工方案、作业指导书报审及附件、执行文件”，
        # 实际只归了一卷高塔组立方案；卷内目录标签已写明，算找到。
        cp = make_checkpoint(
            "立塔施工方案、作业指导书报审及附件、执行文件",
            code="8233",
            archive="01杆塔工程",
        )
        file = make_file(
            "AX-8233-1 塔组立开工报告、人工挖孔桩方案、高塔组立方案、"
            "吊车组立方案、内拉线抱杆方案/"
            "3.项目A（220kV输电线路工程）高塔组立专项施工方案及报审表.pdf"
        )
        _, results = run_match([cp], [file])
        self.assertEqual(results[0].state, STATE_MATCHED)

    def test_folder_evidence_prefers_doc_type_coverage(self):
        # 同卷既有开工报告又有施工方案：清单要“施工方案报审表”，
        # 只带“报审表”的开工报告不能压过带“施工方案”的那份。
        cp = make_checkpoint(
            "架线分部工程施工方案报审表及附件", code="8234", archive="01架线工程"
        )
        base = "AX-8234-1 架线分部开工报告、专项施工方案/"
        files = [
            make_file(base + "1.项目A（220kV输电线路工程）架线分部开工报告及报审表.pdf"),
            make_file(base + "2.项目A（220kV输电线路工程）导线架设专项施工方案及报审表.pdf"),
            # 同为“专项施工方案及报审表”，分类不同（跨越 ≠ 架线）：
            # 主语首词“架线”出现的才是同一分部的那份。
            make_file(base + "4.项目A（220kV输电线路工程）跨越施工专项施工方案及报审表.pdf"),
        ]
        _, results = run_match([cp], files)
        self.assertEqual(results[0].state, STATE_MATCHED)
        self.assertIn("导线架设专项施工方案", results[0].evidence[0].rel_path)

    def test_folder_label_matches_completion_report(self):
        cp = make_checkpoint(
            "项目竣工验收报告", code="8271", archive="02工程验收"
        )
        file = make_file(
            "AX-8271-4 竣工报告、验收发现缺陷及处理情况汇总表、三级验收报告、"
            "工程临时延期报审、施工总结/"
            "1.项目A（220kV输电线路工程）竣工报告及报审表.pdf"
        )
        _, results = run_match([cp], [file])
        self.assertEqual(results[0].state, STATE_MATCHED)

    def test_folder_label_matches_defect_closing_list(self):
        cp = make_checkpoint(
            "工程竣工验收消缺清单及闭环", code="8271", archive="02工程验收"
        )
        file = make_file(
            "AX-8271-4 竣工报告、验收发现缺陷及处理情况汇总表、三级验收报告、"
            "工程临时延期报审、施工总结/"
            "2.项目A（220kV输电线路工程）验收发现缺陷及处理情况汇总表.pdf"
        )
        _, results = run_match([cp], [file])
        self.assertEqual(results[0].state, STATE_MATCHED)

    def test_folder_label_matches_special_acceptance_report(self):
        cp = make_checkpoint(
            "各专项验收报告、整改清单及闭环", code="8271", archive="03项目专项验收"
        )
        file = make_file(
            "AX-8271-4 竣工报告、验收发现缺陷及处理情况汇总表、三级验收报告、"
            "工程临时延期报审、施工总结/"
            "3.项目A（220kV输电线路工程）线路土建工程三级验收报告.pdf"
        )
        _, results = run_match([cp], [file])
        self.assertEqual(results[0].state, STATE_MATCHED)

    def test_folder_listed_text_cannot_prove_subject(self):
        # 文件夹名罗列“路径复测”，但文件是土石方分部开工报告，不能算路径复测记录。
        cp = make_checkpoint(
            "路径复测记录及报审表", code="8231", archive="01土石方工程"
        )
        file = make_file(
            "AX-8231-1 施工控制测量成果、路径复测验收记录、普通基础分坑与开挖验收记录/"
            "1.项目C（220kV输电线路工程）土石方工程分部工程开工报告及报审表.pdf"
        )
        _, results = run_match([cp], [file])
        self.assertNotEqual(results[0].state, STATE_MATCHED)

    def test_folder_listed_doc_type_cannot_prove_compliance(self):
        # 文件夹名有“安全技术交底”，文件却是管理体系认证；清单要的是交底。
        cp = make_checkpoint(
            "安全管理交底", code="8230", archive="安全管理"
        )
        file = make_file(
            "AX-8230-1 法定授权书、施工进度计划、安全技术交底/"
            "9.项目A（220kV输电线路工程）质量管理、环境管理、"
            "职业健康安全管理体系认证及报审表.pdf"
        )
        _, results = run_match([cp], [file])
        self.assertNotEqual(results[0].state, STATE_MATCHED)

    def test_concrete_report_does_not_match_pile_test_report(self):
        # 文件夹以“桩基础”开头，商品混凝土检测报告也有“检测报告”，
        # 但文件名缺少“桩基”，不能算桩基检测报告。
        cp = make_checkpoint(
            "地基、桩基检测报告", code="8232", archive="19桩基础检测"
        )
        file = make_file(
            "AX-8232-19 2A3-3A33桩基础商品混凝土报告报审与见证记录、第三方检测报告/"
            "10、项目A（220kV输电线路工程）3A24基础I-IV腿部位灌注桩商品混凝土"
            "检测报审表、取样记录、检测报告.pdf"
        )
        _, results = run_match([cp], [file])
        self.assertNotEqual(results[0].state, STATE_MATCHED)

    def test_anchor_does_not_match_crushed_stone_to_sand(self):
        cp = make_checkpoint(
            "砂使用跟踪记录表", code="8232", archive="06施工用砂"
        )
        file = make_file(
            "AX-8232-11 砂、碎石、商混/"
            "1.220千伏变电站线路工程碎石使用跟踪记录表.pdf"
        )
        _, results = run_match([cp], [file])
        self.assertNotEqual(results[0].state, STATE_MATCHED)

    def test_reordered_phrase_requires_exact_code(self):
        # 同一份文件名不能跨项目类型（8330 检查点 vs 8230 文件夹）命中
        cp = make_checkpoint(
            "开工报审及开工报告", code="8330", archive="01工程开工及报审"
        )
        file = make_file(
            "AX-8230-1 成立文件、开工报告/6.220千伏变电站线路工程开工报告及报审.pdf"
        )
        matcher = Matcher([cp], project_name="测试工程", known_codes=["8230", "8330"])
        results = matcher.match([file])
        self.assertNotEqual(results[0].state, STATE_MATCHED)

    def test_subject_guard_blocks_generic_report_match(self):
        cp = make_checkpoint("地脚螺栓第三方试验报告及报审表", archive="08角钢、地脚螺栓")
        file = make_file("AY-8232-9 测试工程钢筋原材料检测试验报告及报审表.pdf")
        _, results = run_match([cp], [file])
        self.assertNotEqual(results[0].state, STATE_MATCHED)
        self.assertLess(results[0].score, Matcher.MATCH_THRESHOLD)

    def test_shared_flag_when_one_file_supports_many(self):
        texts = ["开工报告", "施工日志", "安全交底", "质量验收", "材料报审"]
        cps = [
            make_checkpoint(text, code="8230", archive="01工程开工及报审", seq=i + 1)
            for i, text in enumerate(texts)
        ]
        file = make_file(
            "AY-8230-1 测试工程开工报告施工日志安全交底质量验收材料报审.pdf"
        )
        _, results = run_match(cps, [file])
        self.assertTrue(any(item.shared for item in results))
        matched = [item for item in results if item.state == STATE_MATCHED]
        self.assertEqual(len(matched), len(texts))

    def test_evidence_is_capped_at_three(self):
        cp = make_checkpoint("开工报告")
        files = [
            make_file(f"AY-8232-{i} 测试工程开工报告{i}.pdf") for i in range(1, 6)
        ]
        _, results = run_match([cp], files)
        self.assertLessEqual(len(results[0].evidence), 3)

    def test_suspect_state_between_thresholds(self):
        cp = make_checkpoint("混凝土试块强度评定", archive="05施工用混凝土")
        file = make_file("AY-8232-4 测试工程混凝土试块报告及报审表.pdf")
        _, results = run_match([cp], [file])
        self.assertIn(results[0].state, (STATE_SUSPECT, STATE_MISSING))

    def test_exclude_term_penalizes(self):
        cp = make_checkpoint(
            "混凝土试块报告及报审",
            archive="05施工用混凝土",
            exclude_terms=["用水"],
        )
        file = make_file("AY-8232-4 测试工程混凝土用水试块报告及报审表.pdf")
        _, results = run_match([cp], [file])
        self.assertLess(results[0].score, Matcher.MATCH_THRESHOLD)

    def test_opgw_word_order_outranks_test_report(self):
        # 现场写“复合光缆OPGW展放施工 分项工程质量验收记录”，清单写
        # “OPGW光缆展放施工检查及评级记录”：光缆/OPGW 顺序相反，不能因为
        # “光缆OPGW现场开盘测试报告”共享前缀就把这份验收记录判给测试报告。
        cp_report = make_checkpoint(
            "光缆OPGW现场开盘测试报告", code="8234", archive="04光缆测试报告", seq=1
        )
        cp_loose = make_checkpoint(
            "OPGW光缆展放施工检查及评级记录", code="8234", archive="03光缆施工", seq=2
        )
        cp_tight = make_checkpoint(
            "OPGW光缆紧线施工检查及评级记录", code="8234", archive="03光缆施工", seq=3
        )
        file = make_file(
            "AX-8234-3 光缆架设开工报告、分部分项验评记录表、光缆盘测报告/"
            "2.项目A（220kV输电线路工程）复合光缆OPGW展放施工 "
            "分项工程质量验收记录（线光1）.pdf",
            codes=["8234"],
        )
        _, results = run_match([cp_report, cp_loose, cp_tight], [file])
        by_text = {item.checkpoint.text: item for item in results}
        self.assertGreater(
            by_text[cp_loose.text].score, by_text[cp_report.text].score
        )
        self.assertEqual(by_text[cp_loose.text].state, STATE_SUSPECT)
        self.assertEqual(by_text[cp_tight.text].state, STATE_MISSING)

    def test_quality_acceptance_record_counts_as_rating_record(self):
        # “分项工程质量验收记录”和清单的“检查及评级记录”是同一张表，
        # 文书类型词要能互通；否则单据类检查点永远差一档。
        self.assertEqual(
            Matcher._doc_family_hit("评级记录", "测试工程分项工程质量验收记录"),
            1.0,
        )

    def test_test_plan_is_not_test_report(self):
        # 族里的上位词“检测”不能拿来顶替“检测报告”：检测方案是另一类文件。
        cp = make_checkpoint("地基、桩基检测报告", archive="02综合")
        report = make_file(
            "AX-8232-2 测试工程桩基础检测报告及报审表.pdf", codes=["8232"]
        )
        plan = make_file(
            "AX-8232-2 测试工程桩基础及基础实体质量检测方案及报审表.pdf",
            codes=["8232"],
        )
        _, results = run_match([cp], [report, plan])
        self.assertTrue(results[0].evidence)
        self.assertEqual(results[0].evidence[0].rel_path, report.rel_path)


if __name__ == "__main__":
    unittest.main()
